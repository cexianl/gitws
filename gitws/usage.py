"""Streaming disk measurements with cross-directory hard-link deduplication."""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from pathlib import Path

from .models import DiskUsage


def measure(
    path: Path, seen: set[tuple[int, int]], exclude_git: bool = False, *, details: bool = False
) -> DiskUsage:
    logical = 0
    physical = 0
    pending = [str(path)]
    inodes: dict[tuple[int, int], list[int]] = {}
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                children = []
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not (exclude_git and entry.name == ".git"):
                                children.append(entry.path)
                            continue
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        # Files may disappear while a build is running.
                        continue
                    logical += stat.st_size
                    identity = (stat.st_dev, stat.st_ino)
                    blocks = getattr(stat, "st_blocks", None)
                    allocated = blocks * 512 if blocks is not None else stat.st_size
                    if details:
                        inode = inodes.setdefault(identity, [allocated, stat.st_nlink, 0])
                        inode[2] += 1
                    if identity not in seen:
                        seen.add(identity)
                        physical += allocated
                pending.extend(reversed(children))
        except OSError:
            continue
    value: DiskUsage = {"logical": logical, "physical": physical}
    if details:
        value["exclusive"] = sum(size for size, links, count in inodes.values() if links <= count)
        value["shared"] = sum(size for size, links, count in inodes.values() if links > count)
    return value


def measure_paths(
    paths: Iterable[tuple[str, Path, bool]], *, detailed: set[Path] | None = None
) -> Iterator[tuple[str, DiskUsage]]:
    seen: set[tuple[int, int]] = set()
    total: DiskUsage = {"logical": 0, "physical": 0}
    for label, path, exclude_git in paths:
        value = measure(path, seen, exclude_git, details=bool(detailed and path in detailed))
        total["logical"] += value["logical"]
        total["physical"] += value["physical"]
        yield label, value
    yield "total", total
