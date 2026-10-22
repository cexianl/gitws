"""Overlay mounts and per-workspace Git metadata; lifecycle locks live in Store."""

from __future__ import annotations

import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .errors import GitwsError
from .git import run_git, submodule_urls
from .process import run_command


def prepare_metadata(base: Path, target: Path, upper: Path, storage: Path) -> None:
    """Create only Git metadata; source files come from the lower layer."""
    run_git(
        "worktree",
        "add",
        "--detach",
        "--no-checkout",
        str(target),
        run_git("rev-parse", "HEAD", cwd=base),
        cwd=base,
    )
    run_git("reset", "--mixed", "HEAD", cwd=target)
    run_git("config", "lfs.storage", str(storage), cwd=target)
    metadata = target / ".git"
    destination = upper / ".git"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(metadata, destination)
    _prepare_children(base, target, upper, storage)


def _prepare_children(base: Path, target: Path, upper: Path, storage: Path) -> None:
    entries = list(submodule_urls(base))

    def prepare(subpath: str) -> None:
        base_child = base / subpath
        target_child = target / subpath
        if not base_child.is_dir():
            return
        target_child.parent.mkdir(parents=True, exist_ok=True)
        commit = run_git("rev-parse", "HEAD", cwd=base_child)
        run_git(
            "worktree",
            "add",
            "--detach",
            "--no-checkout",
            str(target_child),
            commit,
            cwd=base_child,
        )
        run_git("reset", "--mixed", "HEAD", cwd=target_child)
        run_git("config", "lfs.storage", str(storage), cwd=target_child)
        child_metadata = target_child / ".git"
        child_destination = upper / subpath / ".git"
        child_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(child_metadata, child_destination)
        _prepare_children(base_child, target_child, upper / subpath, storage)

    if len(entries) < 2:
        for subpath in entries:
            prepare(subpath)
        return
    with ThreadPoolExecutor(max_workers=min(8, len(entries))) as pool:
        list(pool.map(prepare, entries))


def remove_metadata(base: Path, target: Path) -> None:
    for subpath in submodule_urls(base):
        base_child = base / subpath
        target_child = target / subpath
        if base_child.is_dir() and target_child.exists():
            remove_metadata(base_child, target_child)
            run_git("worktree", "remove", "--force", str(target_child), cwd=base_child, check=False)


def mount(lower: Path, upper: Path, work: Path, target: Path) -> str:
    options = f"lowerdir={lower},upperdir={upper},workdir={work}"
    if os.geteuid() == 0 and shutil.which("mount"):
        try:
            run_command("mount", "-t", "overlay", "overlay", "-o", options, str(target))
            return "overlayfs"
        except GitwsError:
            pass
    fuse = shutil.which("fuse-overlayfs")
    if fuse:
        run_command(fuse, "-o", options, str(target))
        return "fuse-overlayfs"
    raise GitwsError("copy-on-write workspaces require overlayfs or fuse-overlayfs")


def is_mounted(path: Path) -> bool:
    try:
        mounts = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return False
    needle = str(path.resolve())
    for line in mounts:
        fields = line.split()
        if len(fields) > 4:
            target = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4])
            if target == needle:
                return True
    return False


def unmount(target: Path) -> None:
    if not is_mounted(target):
        return
    if shutil.which("umount") is None:
        raise GitwsError("cannot remove copy-on-write workspace: umount is unavailable")
    run_command("umount", str(target))


def has_private_submodule_data(base: Path) -> bool:
    pending = [base]
    while pending:
        parent = pending.pop()
        for subpath in submodule_urls(parent):
            child = parent / subpath
            if not (child / ".git").exists():
                continue
            if run_git("worktree", "list", "--porcelain", cwd=child).count("worktree ") > 1:
                return True
            counts = dict(
                line.split(": ", 1)
                for line in run_git("count-objects", "-v", cwd=child).splitlines()
                if ": " in line
            )
            # A reference clone normally has no objects of its own. Keep
            # loose/packed objects, including detached or dangling task
            # commits whose worktree reflog was removed with the workspace.
            if int(counts.get("count", 0)) or int(counts.get("in-pack", 0)):
                return True
            pending.append(child)
    return False
