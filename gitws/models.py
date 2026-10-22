"""Repository values and the JSON record shapes persisted by gitws."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict

from .storage import repo_id


@dataclass
class Repo:
    name: str
    path: Path | None
    remote: str
    branch: str
    mirror: Path
    source: str = "local"

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path) if self.path else None,
            "remote": self.remote,
            "branch": self.branch,
            "mirror": str(self.mirror),
            "source": self.source,
        }


def storage_id(repo: Repo) -> str:
    return repo_id(repo.remote or f"local:{repo.path}")


class WorkspaceInfo(TypedDict):
    name: str
    repo: str
    path: str
    commit: str
    submodules: dict[str, str]


class WorkspaceRecord(WorkspaceInfo, total=False):
    # Older records omit mirror; ordinary workspaces omit the COW fields.
    mirror: str
    mode: Literal["cow"]
    backend: str
    base: str
    upper: str
    work: str


class DiskUsage(TypedDict):
    logical: int
    physical: int
