"""Bounded Git checkouts shared only between completed, immutable COW bases."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path

from .errors import GitwsError
from .storage import read_json, write_json

FORMAT = 1
BATCH_BYTES = 4 * 1024 * 1024
BATCH_FILES = 1024


def manifest_path(base: Path) -> Path:
    return base.parent / ".manifests" / f"{base.name}.json"


def complete(base: Path) -> bool:
    marker = manifest_path(base)
    if not marker.is_file():
        return False
    _manifest(base)
    return True


def _manifest(base: Path) -> dict:
    marker = manifest_path(base)
    data = read_json(marker)
    if (
        not isinstance(data, dict)
        or data.get("version") != FORMAT
        or data.get("commit") != base.name
        or not isinstance(data.get("files"), dict)
    ):
        raise GitwsError(f"invalid base manifest: {marker}")
    for relative, entry in data["files"].items():
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or any(part in {"..", ".git"} for part in Path(relative).parts)
            or not isinstance(entry, dict)
            or not isinstance(entry.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
            or not isinstance(entry.get("stat"), list)
            or len(entry["stat"]) != 7
            or not all(isinstance(value, int) for value in entry["stat"])
        ):
            raise GitwsError(f"invalid base manifest: {marker}")
    return data


def _fingerprint(info: os.stat_result) -> list[int]:
    # Creating another hardlink changes ctime and nlink, but not these fields.
    return [
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_uid,
        info.st_gid,
    ]


def _git_input(path: Path, args: list[str], data: bytes, env: dict[str, str]) -> bytes:
    try:
        result = subprocess.run(["git", *args], cwd=path, input=data, capture_output=True, env=env)
    except OSError as exc:
        raise GitwsError(f"cannot run git in {path}: {exc}") from exc
    if result.returncode:
        raise GitwsError(
            f"git {' '.join(args)} failed: {result.stderr.decode(errors='replace').strip()}"
        )
    return result.stdout


class SharedCheckout:
    """The caller holds the repository lifecycle lock for the entire build."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.files: dict[str, dict] = {}
        self.candidates: dict[str, list[tuple[str, Path, list[int]]]] = defaultdict(list)
        self.used: set[tuple[int, int]] = set()
        for marker in sorted((base.parent / ".manifests").glob("*.json")):
            other = base.parent / marker.stem
            if other == base or not other.is_dir():
                continue
            data = _manifest(other)
            for relative, entry in data["files"].items():
                self.candidates[entry["sha256"]].append((relative, other / relative, entry["stat"]))

    def __enter__(self) -> SharedCheckout:
        self.scratch = tempfile.TemporaryDirectory(dir=self.base.parent, prefix=".links-")
        return self

    def __exit__(self, *args) -> None:
        self.scratch.cleanup()

    def checkout(self, path: Path, env: dict[str, str]) -> None:
        entries = []
        records = _git_input(path, ["ls-files", "--stage", "-z"], b"", env)
        for record in records.split(b"\0"):
            if not record:
                continue
            metadata, raw_name = record.split(b"\t", 1)
            mode, oid, stage = metadata.split()
            if stage != b"0":
                raise GitwsError(f"cannot share an unmerged checkout: {path}")
            if mode != b"160000":
                entries.append((os.fsdecode(raw_name), oid))
        if not entries:
            return
        # checkout-index uses the index's attributes, including those not yet on
        # disk. Let Git perform all conversions rather than emulating filters.
        names = b"".join(os.fsencode(name) + b"\0" for name, _ in entries)
        attributes = _git_input(
            path,
            ["check-attr", "--cached", "-z", "--stdin", "filter", "working-tree-encoding", "ident"],
            names,
            env,
        )
        fields = attributes.split(b"\0")[:-1]
        individually = {
            os.fsdecode(fields[index])
            for index in range(0, len(fields), 3)
            if fields[index + 2] not in {b"unspecified", b"unset"}
        }
        oids = b"".join(oid + b"\n" for _, oid in entries)
        sizes = _git_input(path, ["cat-file", "--batch-check=%(objectsize)"], oids, env)
        sizes = [int(value) for value in sizes.splitlines()]
        batch: list[str] = []
        total = 0
        for (name, _), size in zip(entries, sizes, strict=True):
            if batch and (
                name in individually or total + size > BATCH_BYTES or len(batch) >= BATCH_FILES
            ):
                self._batch(path, batch, env)
                batch, total = [], 0
            batch.append(name)
            total += size
            # Arbitrary filters can expand a tiny blob into a very large file.
            if name in individually or total >= BATCH_BYTES:
                self._batch(path, batch, env)
                batch, total = [], 0
        if batch:
            self._batch(path, batch, env)

    def _batch(self, path: Path, names: list[str], env: dict[str, str]) -> None:
        data = b"".join(os.fsencode(name) + b"\0" for name in names)
        _git_input(path, ["-c", "checkout.workers=1", "checkout-index", "--stdin", "-z"], data, env)
        for name in names:
            file = path / name
            info = file.lstat()
            if not stat.S_ISREG(info.st_mode):
                continue  # Preserve symlinks without linking or following them.
            digest = hashlib.sha256()
            with file.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            self._share(file, digest.hexdigest(), info)

    def _share(self, file: Path, digest: str, info: os.stat_result) -> None:
        relative = str(file.relative_to(self.base))
        candidates = sorted(self.candidates[digest], key=lambda entry: entry[0] != relative)
        for _, candidate, expected in candidates:
            try:
                current = candidate.lstat()
                identity = (current.st_dev, current.st_ino)
                if (
                    _fingerprint(current) != expected
                    or identity in self.used
                    or not stat.S_ISREG(current.st_mode)
                    or (
                        current.st_dev,
                        current.st_mode,
                        current.st_size,
                        current.st_uid,
                        current.st_gid,
                    )
                    != (info.st_dev, info.st_mode, info.st_size, info.st_uid, info.st_gid)
                    or os.listxattr(candidate)
                    or os.listxattr(file)
                ):
                    continue
                # A temporary link allows atomic replacement without losing the
                # freshly materialized file if hardlinks are unavailable.
                temporary = Path(self.scratch.name) / "file"
                try:
                    os.link(candidate, temporary, follow_symlinks=False)
                    temporary.replace(file)
                    info = file.lstat()
                    break
                finally:
                    temporary.unlink(missing_ok=True)
            except OSError:
                continue
        self.used.add((info.st_dev, info.st_ino))
        self.files[relative] = {"sha256": digest, "stat": _fingerprint(info)}

    def publish(self) -> None:
        write_json(
            manifest_path(self.base),
            {
                "version": FORMAT,
                "commit": self.base.name,
                "files": self.files,
            },
        )
