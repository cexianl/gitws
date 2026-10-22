"""Git commands and checkout/submodule inspection; no gitws state."""

from __future__ import annotations

import re
from pathlib import Path

from .errors import GitwsError
from .process import run_command


def run_git(
    *args: str, cwd: Path | None = None, check: bool = True, env: dict[str, str] | None = None
) -> str:
    return run_command("git", *args, cwd=cwd, check=check, env=env)


def common_git_dir(path: Path) -> Path:
    result = Path(run_git("rev-parse", "--git-common-dir", cwd=path))
    return result.resolve() if result.is_absolute() else (path / result).resolve()


def ensure_repo(path: Path) -> Path:
    path = path.expanduser().resolve()
    if not path.is_dir():
        raise GitwsError(f"repository path does not exist: {path}")
    return Path(run_git("rev-parse", "--show-toplevel", cwd=path)).resolve()


def local_clone_option(path: Path) -> str:
    alternates = Path(run_git("rev-parse", "--git-path", "objects/info/alternates", cwd=path))
    if not alternates.is_absolute():
        alternates = (path / alternates).resolve()
    return "--no-local" if alternates.exists() else "--local"


def ensure_clean(path: Path) -> None:
    status = run_git("status", "--porcelain", "--ignore-submodules=none", cwd=path)
    if status:
        raise GitwsError(f"repository has local changes: {path}")
    for line in run_git("submodule", "status", "--recursive", cwd=path).splitlines():
        if line and line[0] in "+-U":
            raise GitwsError(f"submodule is not at the recorded clean commit: {line}")


def has_private_objects(path: Path) -> bool:
    counts = dict(
        line.split(": ", 1)
        for line in run_git("count-objects", "-v", cwd=path).splitlines()
        if ": " in line
    )
    return bool(int(counts.get("count", 0)) or int(counts.get("in-pack", 0)))


def ensure_removable(path: Path, *, protect_submodule_objects: bool = True) -> None:
    """Protect worktree files, including ignored files in nested submodules."""
    pending = [path]
    while pending:
        current = pending.pop()
        entries = run_git("ls-files", "-v", "-z", cwd=current).split("\0")
        if any(entry and (entry[0].islower() or entry[0] == "S") for entry in entries):
            raise GitwsError(
                f"workspace has assume-unchanged or skip-worktree entries: {current}; "
                "inspect them before using remove --force"
            )
        status = run_git(
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--ignored=matching",
            "--ignore-submodules=none",
            cwd=current,
        )
        if status:
            raise GitwsError(
                f"workspace has local changes, untracked or ignored files: {current}; "
                "use remove --force to discard them"
            )
        if protect_submodule_objects and current != path and has_private_objects(current):
            raise GitwsError(
                f"submodule has private Git objects: {current}; "
                "preserve needed commits before using remove --force"
            )
        pending.extend(
            current / subpath
            for subpath in submodule_urls(current)
            if (current / subpath / ".git").exists()
        )


def submodule_snapshot(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    output = run_git("submodule", "status", "--recursive", cwd=path)
    for line in output.splitlines():
        if not line:
            continue
        parts = line[1:].split()
        if len(parts) >= 2:
            result[parts[1]] = parts[0]
    return result


def submodule_urls(path: Path) -> dict[str, str]:
    """Return submodule path -> URL entries declared by this checkout."""
    gitmodules = path / ".gitmodules"
    if not gitmodules.exists():
        return {}
    output = run_git(
        "config",
        "--file",
        str(gitmodules),
        "--get-regexp",
        r"^submodule\..*\.url$",
        cwd=path,
        check=False,
    )
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, url = line.split(None, 1)
        match = re.match(r"^submodule\.(.+)\.url$", key)
        if match:
            section = match.group(1)
            subpath = run_git(
                "config", "--file", str(gitmodules), "--get", f"submodule.{section}.path", cwd=path
            )
            index_entry = run_git(
                "ls-files", "--error-unmatch", "--stage", "--", subpath, cwd=path, check=False
            )
            if not index_entry.startswith("160000 "):
                continue
            values[subpath] = url
    return values


def submodule_is_optional(path: Path, relative: str) -> bool:
    """Return whether a missing recursive submodule is disabled or stale."""
    for gitmodules in path.rglob(".gitmodules"):
        parent = gitmodules.parent
        try:
            prefix = parent.relative_to(path)
        except ValueError:
            continue
        output = run_git(
            "config",
            "--file",
            str(gitmodules),
            "--get-regexp",
            r"^submodule\..*\.path$",
            cwd=parent,
            check=False,
        )
        for key, subpath in (line.split(None, 1) for line in output.splitlines()):
            section = re.match(r"^submodule\.(.+)\.path$", key)
            if not section:
                continue
            candidate = str(prefix / subpath) if str(prefix) != "." else subpath
            if candidate != relative:
                continue
            entry = run_git(
                "ls-files", "--error-unmatch", "--stage", "--", relative, cwd=path, check=False
            )
            return not entry.startswith("160000 ")
    return False


def submodule_update_override(path: Path, relative: str) -> str | None:
    """Return a config override for submodules disabled by .gitmodules."""
    gitmodules = path / ".gitmodules"
    if not gitmodules.exists():
        return None
    output = run_git(
        "config",
        "--file",
        str(gitmodules),
        "--get-regexp",
        r"^submodule\..*\.path$",
        cwd=path,
        check=False,
    )
    for key, subpath in (line.split(None, 1) for line in output.splitlines()):
        if subpath != relative:
            continue
        match = re.match(r"^submodule\.(.+)\.path$", key)
        if not match:
            return None
        section = match.group(1)
        update = run_git(
            "config",
            "--file",
            str(gitmodules),
            "--get",
            f"submodule.{section}.update",
            cwd=path,
            check=False,
        )
        if update == "none":
            return f"submodule.{section}.update=checkout"
        return None
    return None


def absolute_remote(remote: str, source: Path | None) -> str:
    if not remote or source is None or "://" in remote or re.match(r"^[^/]+:", remote):
        return remote
    return str((source / Path(remote).expanduser()).resolve())
