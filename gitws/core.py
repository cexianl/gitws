from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class GitwsError(RuntimeError):
    pass


def run_git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and proc.returncode:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise GitwsError(f"git {' '.join(args)} failed: {detail}")
    return proc.stdout.strip()


def git_dir(path: Path) -> Path:
    value = run_git("rev-parse", "--git-dir", cwd=path)
    result = Path(value)
    return result if result.is_absolute() else (path / result).resolve()


def ensure_repo(path: Path) -> Path:
    path = path.expanduser().resolve()
    if not path.is_dir():
        raise GitwsError(f"repository path does not exist: {path}")
    run_git("rev-parse", "--show-toplevel", cwd=path)
    return path


def ensure_clean(path: Path) -> None:
    status = run_git("status", "--porcelain", "--ignore-submodules=none", cwd=path)
    if status:
        raise GitwsError(f"repository has local changes: {path}")
    for line in run_git("submodule", "status", "--recursive", cwd=path).splitlines():
        if line and line[0] in "+-U":
            raise GitwsError(f"submodule is not at the recorded clean commit: {line}")


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
    output = run_git("config", "--file", str(gitmodules), "--get-regexp",
                    r"^submodule\..*\.url$", cwd=path, check=False)
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, url = line.split(None, 1)
        match = re.match(r"^submodule\.(.+)\.url$", key)
        if match:
            section = match.group(1)
            subpath = run_git("config", "--file", str(gitmodules), "--get",
                              f"submodule.{section}.path", cwd=path)
            values[subpath] = url
    return values


def repo_id(remote: str) -> str:
    return hashlib.sha256(remote.encode()).hexdigest()[:16]


@dataclass
class Repo:
    name: str
    path: Path
    remote: str
    branch: str
    mirror: Path

    def as_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "remote": self.remote,
                "branch": self.branch, "mirror": str(self.mirror)}


class Store:
    def __init__(self, root: Path | None = None):
        self.root = (root or Path(os.environ.get("GITWS_HOME", "~/.gitws"))).expanduser().resolve()
        self.repos_dir = self.root / "repos"
        self.workspaces_dir = self.root / "workspaces"
        self.records_dir = self.root / "records"
        for directory in (self.repos_dir, self.workspaces_dir, self.records_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.config = self.root / "config.json"

    def _load(self) -> dict[str, Any]:
        if not self.config.exists():
            return {"repos": {}}
        return json.loads(self.config.read_text())

    def _save(self, value: dict[str, Any]) -> None:
        tmp = self.config.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
        tmp.replace(self.config)

    def get(self, name: str) -> Repo:
        raw = self._load().get("repos", {}).get(name)
        if not raw:
            raise GitwsError(f"unknown repository alias: {name}")
        return Repo(name, Path(raw["path"]), raw["remote"], raw["branch"], Path(raw["mirror"]))

    def add(self, name: str, path: Path) -> Repo:
        if not name or "/" in name or name in {".", ".."}:
            raise GitwsError("repository alias must be a simple name")
        path = ensure_repo(path)
        remote = run_git("remote", "get-url", "origin", cwd=path)
        branch = run_git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=path,
                         check=False)
        branch = branch.removeprefix("origin/") or run_git("branch", "--show-current", cwd=path)
        if not branch:
            branch = "main"
        mirror = self.repos_dir / f"{repo_id(remote)}.git"
        data = self._load()
        data.setdefault("repos", {})[name] = Repo(name, path, remote, branch, mirror).as_dict()
        self._save(data)
        return self.get(name)

    def all_repos(self) -> dict[str, Repo]:
        return {name: self.get(name) for name in self._load().get("repos", {})}

    def sync_mirror(self, repo: Repo) -> None:
        if not repo.mirror.exists():
            repo.mirror.parent.mkdir(parents=True, exist_ok=True)
            run_git("clone", "--mirror", repo.remote, str(repo.mirror))
        else:
            run_git("fetch", "--prune", "origin", cwd=repo.mirror)

    def submodule_mirror(self, url: str) -> Path:
        mirror = self.repos_dir / f"{repo_id(url)}.git"
        if not mirror.exists():
            run_git("clone", "--mirror", url, str(mirror))
        else:
            run_git("fetch", "--prune", "origin", cwd=mirror)
        return mirror

    def submodule_configs(self, path: Path) -> list[str]:
        configs: list[str] = []
        pending = [path]
        seen: set[Path] = set()
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            for subpath, url in submodule_urls(current).items():
                mirror = self.submodule_mirror(url)
                configs.append(f"url.{mirror}.insteadOf={url}")
                child = current / subpath
                if child.is_dir():
                    pending.append(child)
        return configs

    def update_submodules(self, path: Path) -> None:
        run_git("submodule", "sync", "--recursive", cwd=path)
        # Initialize direct children first so nested .gitmodules become visible;
        # then build mappings for the complete recursive tree.
        configs = []
        for url in submodule_urls(path).values():
            mirror = self.submodule_mirror(url)
            configs.extend(["-c", f"url.{mirror}.insteadOf={url}"])
        run_git(*configs, "submodule", "update", "--init", cwd=path)
        configs = self.submodule_configs(path)
        flat: list[str] = []
        for config in configs:
            flat.extend(["-c", config])
        run_git(*flat, "submodule", "update", "--init", "--recursive", cwd=path)

    def latest(self, repo: Repo) -> str:
        self.sync_mirror(repo)
        return run_git("rev-parse", f"refs/heads/{repo.branch}", cwd=repo.mirror)

    def record_path(self, name: str) -> Path:
        return self.records_dir / f"{name}.json"

    def workspaces(self, repo_name: str | None = None) -> list[dict[str, Any]]:
        values = []
        for path in sorted(self.records_dir.glob("*.json")):
            item = json.loads(path.read_text())
            if repo_name is None or item["repo"] == repo_name:
                values.append(item)
        return values

    def create_workspace(self, repo: Repo, name: str, commit: str | None) -> dict[str, Any]:
        if not name or "/" in name or name in {".", ".."}:
            raise GitwsError("workspace name must be a simple name")
        record = self.record_path(name)
        target = self.workspaces_dir / name
        if record.exists() or target.exists():
            raise GitwsError(f"workspace already exists: {name}")
        self.sync_mirror(repo)
        commit = commit or self.latest(repo)
        run_git("cat-file", "-e", f"{commit}^{{commit}}", cwd=repo.mirror)
        target.parent.mkdir(parents=True, exist_ok=True)
        run_git("worktree", "add", "--detach", str(target), commit, cwd=repo.mirror)
        try:
            self.update_submodules(target)
            item = {"name": name, "repo": repo.name, "path": str(target),
                    "commit": run_git("rev-parse", "HEAD", cwd=target),
                    "submodules": submodule_snapshot(target)}
            record.write_text(json.dumps(item, indent=2, sort_keys=True) + "\n")
            return item
        except Exception:
            run_git("worktree", "remove", "--force", str(target), cwd=repo.mirror, check=False)
            raise

    def remove_workspace(self, name: str) -> None:
        record = self.records_dir / f"{name}.json"
        if not record.exists():
            raise GitwsError(f"unknown workspace: {name}")
        item = json.loads(record.read_text())
        repo = self.get(item["repo"])
        target = Path(item["path"]).resolve()
        if self.workspaces_dir not in target.parents:
            raise GitwsError("refusing to remove a path outside gitws workspaces")
        run_git("worktree", "remove", "--force", str(target), cwd=repo.mirror)
        record.unlink(missing_ok=True)

    def size(self, repo: Repo) -> dict[str, int]:
        def apparent(path: Path) -> int:
            if not path.exists():
                return 0
            return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
        workspaces = [Path(item["path"]) for item in self.workspaces(repo.name)]
        return {"mirror": apparent(repo.mirror), "source": apparent(repo.path),
                **{f"workspace:{p.name}": apparent(p) for p in workspaces}}

    def clean(self, repo: Repo) -> None:
        for item in self.workspaces(repo.name):
            path = Path(item["path"])
            if not path.exists():
                self.record_path(item["name"]).unlink(missing_ok=True)
        if repo.mirror.exists():
            run_git("worktree", "prune", cwd=repo.mirror)
