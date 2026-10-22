"""Shared LFS content transfer and checkout."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

from .errors import GitwsError
from .git import run_git, submodule_urls


def environment() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    env["GIT_NO_LAZY_FETCH"] = "1"
    return env


def pull(path: Path, storage: Path) -> None:
    if shutil.which("git-lfs") is None:
        return
    run_git("config", "lfs.storage", str(storage), cwd=path)
    run_git("lfs", "pull", cwd=path)
    for subpath in submodule_urls(path):
        child = path / subpath
        if child.is_dir():
            pull(child, storage)


def seed(source: Path | None, storage: Path) -> None:
    """Import local objects; the caller must hold the storage's LFS lock."""
    if source is None or not source.is_dir() or shutil.which("git-lfs") is None:
        return
    _seed(source, storage)


def _seed(source: Path, storage: Path) -> None:
    details = run_git("lfs", "env", cwd=source)
    media = next(
        (
            line.split("=", 1)[1]
            for line in details.splitlines()
            if line.startswith("LocalMediaDir=")
        ),
        "",
    )
    if media:
        media_dir = Path(media)
        if media_dir.is_dir():
            for original in media_dir.rglob("*"):
                if not original.is_file():
                    continue
                destination = storage / "objects" / original.relative_to(media_dir)
                if destination.exists():
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(original, destination)
                except OSError:
                    with tempfile.NamedTemporaryFile(
                        dir=destination.parent, delete=False
                    ) as stream:
                        temporary = Path(stream.name)
                    try:
                        shutil.copy2(original, temporary)
                        temporary.replace(destination)
                    finally:
                        temporary.unlink(missing_ok=True)
    for subpath in submodule_urls(source):
        child = source / subpath
        if (child / ".git").exists():
            _seed(child, storage)


def checkout(path: Path, storage: Path) -> None:
    if shutil.which("git-lfs") is None:
        return
    run_git("config", "lfs.storage", str(storage), cwd=path)
    for line in run_git("lfs", "ls-files", "--long", cwd=path).splitlines():
        object_id = line.split(None, 1)[0]
        if not re.fullmatch(r"[0-9a-f]{64}", object_id):
            continue
        object_path = storage / "objects" / object_id[:2] / object_id[2:4] / object_id
        if not object_path.exists():
            raise GitwsError(f"local Git LFS object is missing: {object_id}")
    run_git("lfs", "checkout", cwd=path)
    for subpath in submodule_urls(path):
        child = path / subpath
        if child.is_dir():
            checkout(child, storage)
