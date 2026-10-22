"""Stable cache identities, atomic JSON files, and process locks."""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .errors import GitwsError

try:
    import fcntl
except ImportError:  # pragma: no cover - gitws requires Linux
    fcntl = None


def repo_id(remote: str) -> str:
    return hashlib.sha256(remote.encode()).hexdigest()[:16]


def write_json(path: Path, value: Any) -> None:
    """Publish complete records so concurrent readers never see partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, suffix=".tmp", delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise GitwsError(f"invalid JSON in {path}: {exc.msg}") from exc


@contextmanager
def locked(root: Path, kind: str = "state") -> Iterator[None]:
    lock_path = root / "state.lock" if kind == "state" else root / "locks" / f"{repo_id(kind)}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)
    handle = lock_path.open("r+")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
