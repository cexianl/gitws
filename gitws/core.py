"""Repository registration and workspace lifecycle orchestration.

Store owns lock ordering and persisted records. Filesystem, Git, LFS and COW
operations live in small modules that do not depend on Store.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import cow as overlay
from . import lfs
from .cache import RepositoryCache
from .errors import GitwsError
from .git import (
    common_git_dir,
    ensure_clean,
    ensure_removable,
    ensure_repo,
    run_git,
    submodule_is_optional,
    submodule_snapshot,
)
from .models import DiskUsage, Repo, WorkspaceRecord, storage_id
from .process import run_command as run_command
from .storage import locked, read_json, repo_id, write_json
from .usage import measure_paths


class Store:
    """Manage one storage root; create/remove lock workspace before repository."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or Path(os.environ.get("GITWS_HOME", "~/.gitws"))).expanduser().resolve()
        self.repos_dir = self.root / "repos"
        self.workspaces_dir = self.root / "workspaces"
        self.records_dir = self.root / "records"
        self.bases_dir = self.root / "bases"
        self.cow_dir = self.root / "cow"
        for directory in (
            self.repos_dir,
            self.workspaces_dir,
            self.records_dir,
            self.bases_dir,
            self.cow_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.config = self.root / "config.json"
        self.cache = RepositoryCache(self.root)

    @contextmanager
    def locked(self, kind: str = "state") -> Iterator[None]:
        with locked(self.root, kind):
            yield

    @staticmethod
    def is_mounted(path: Path) -> bool:
        return overlay.is_mounted(path)

    def _load(self) -> dict[str, Any]:
        if not self.config.exists():
            return {"repos": {}}
        return read_json(self.config)

    def _save(self, value: dict[str, Any]) -> None:
        write_json(self.config, value)

    def get(self, name: str) -> Repo:
        raw = self._load().get("repos", {}).get(name)
        if not raw:
            raise GitwsError(f"unknown repository alias: {name}")
        return Repo(
            name,
            Path(raw["path"]) if raw.get("path") else None,
            raw["remote"],
            raw["branch"],
            Path(raw["mirror"]),
            raw.get("source", "local"),
        )

    def add(self, name: str, location: Path | str) -> Repo:
        if not name or "/" in name or name in {".", ".."}:
            raise GitwsError("repository alias must be a simple name")
        value = str(location)
        is_url = "://" in value or bool(re.match(r"^[^/]+@?[^/]*:[^/].*", value))
        if is_url:
            path = None
            remote = value
            branch = ""
            source = "remote"
            identity = f"remote:{remote}"
        else:
            path = ensure_repo(Path(location))
            remote = run_git("remote", "get-url", "origin", cwd=path, check=False)
            branch = run_git(
                "symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=path, check=False
            ).removeprefix("origin/")
            branch = branch or run_git("branch", "--show-current", cwd=path)
            source = "local"
            identity = f"local:{path}"
        mirror = self.repos_dir / f"{repo_id(identity)}.git"
        with self.locked():
            data = self._load()
            existing = data.get("repos", {}).get(name)
            if existing:
                if (
                    existing.get("path") == (str(path) if path else None)
                    and existing.get("source", "local") == source
                    and (source == "local" or existing["remote"] == remote)
                ):
                    return self.get(name)
                raise GitwsError(f"repository alias already exists: {name}")
            data.setdefault("repos", {})[name] = Repo(
                name, path, remote, branch, mirror, source
            ).as_dict()
            self._save(data)
        return self.get(name)

    def all_repos(self) -> dict[str, Repo]:
        return {name: self.get(name) for name in self._load().get("repos", {})}

    def sync_mirror(self, repo: Repo) -> None:
        self.cache.sync_mirror(repo)

    def submodule_caches(self, repo: Repo) -> list[Path]:
        return self.cache.submodule_caches(repo)

    def lfs_path(self, repo: Repo) -> Path:
        path = self.root / "lfs" / storage_id(repo)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def seed_local_lfs(self, source: Path | None, storage: Path) -> None:
        with self.locked(f"lfs:{storage}"):
            lfs.seed(source, storage)

    def switch_source(self, repo: Repo, revision: str | None = None) -> str:
        """Explicitly switch a clean original checkout; None selects remote latest."""
        command = "sync" if revision is None else "switch"
        if repo.path is None:
            raise GitwsError(
                f"{command} changes the original checkout and requires a local source; "
                "for a URL source, new already fetches updates"
            )
        if revision is None and not repo.remote:
            raise GitwsError("local repository has no origin URL")
        with self.locked(f"repo:{repo.mirror}"):
            ensure_clean(repo.path)
            self.sync_mirror(repo)
            commit = (
                self.cache.sync_remote(repo)
                if revision is None
                else self.cache.resolve_commit(repo, revision)
            )
            run_git("fetch", str(repo.mirror), commit, cwd=repo.path)
            run_git("switch", "--detach", commit, cwd=repo.path)
            self.cache.update_submodules(repo.path)
        return commit

    def record_path(self, name: str) -> Path:
        return self.records_dir / f"{name}.json"

    def base_path(self, repo: Repo, commit: str) -> Path:
        legacy = self.bases_dir / storage_id(repo) / commit
        if legacy.is_dir():
            try:
                if common_git_dir(legacy) == repo.mirror:
                    return legacy
            except GitwsError:
                pass
        return self.bases_dir / repo.mirror.stem / commit

    def retained_bases(self, repo: Repo) -> list[Path]:
        values = {path for path in (self.bases_dir / repo.mirror.stem).glob("*") if path.is_dir()}
        for path in (self.bases_dir / storage_id(repo)).glob("*"):
            if path.is_dir():
                try:
                    if common_git_dir(path) == repo.mirror:
                        values.add(path)
                except GitwsError:
                    continue
        values.update(
            Path(item["base"]) for item in self.workspaces(repo.name) if item.get("mode") == "cow"
        )
        return sorted(values)

    def _ensure_base(self, repo: Repo, commit: str, env: dict[str, str]) -> Path:
        base = self.base_path(repo, commit)
        if base.is_dir():
            incomplete = False
            for line in run_git("submodule", "status", "--recursive", cwd=base).splitlines():
                parts = line[1:].split() if line else []
                if line and line[0] == "-" and len(parts) >= 2:
                    if not submodule_is_optional(base, parts[1]):
                        incomplete = True
                        break
            if incomplete:
                if repo.path is not None:
                    self.seed_local_lfs(repo.path, self.lfs_path(repo))
                self.cache.update_submodules(base, env, repo.path, repo.source == "local")
                if repo.source == "remote":
                    lfs.pull(base, self.lfs_path(repo))
                else:
                    lfs.checkout(base, self.lfs_path(repo))
            return base
        base.parent.mkdir(parents=True, exist_ok=True)
        if repo.path is not None:
            self.seed_local_lfs(repo.path, self.lfs_path(repo))
        run_git("worktree", "add", "--detach", str(base), commit, cwd=repo.mirror, env=env)
        try:
            self.cache.update_submodules(base, env, repo.path, repo.source == "local")
            run_git("config", "lfs.storage", str(self.lfs_path(repo)), cwd=base)
            if repo.source == "remote":
                lfs.pull(base, self.lfs_path(repo))
            else:
                lfs.checkout(base, self.lfs_path(repo))
            return base
        except Exception:
            run_git("worktree", "remove", "--force", str(base), cwd=repo.mirror, check=False)
            raise

    def mount_workspace(self, name: str) -> Path:
        with self.workspace_locked(name) as (item, _):
            record = self.record_path(name)
            if item.get("mode") != "cow":
                raise GitwsError(f"workspace does not use COW: {name}")
            target = Path(item["path"])
            if self.is_mounted(target):
                return target
            for key in ("path", "base", "upper", "work"):
                if not Path(item[key]).is_dir():
                    raise GitwsError(f"workspace {key} is missing: {item[key]}")
            for root, dirs, files in os.walk(target):
                if any(filename != ".git" for filename in files) or any(
                    (Path(root) / directory).is_symlink() for directory in dirs
                ):
                    raise GitwsError(
                        "files were written into the unmounted workspace; "
                        "move them out before mounting"
                    )
            item["backend"] = overlay.mount(
                Path(item["base"]), Path(item["upper"]), Path(item["work"]), target
            )
            write_json(record, item)
            return target

    def workspaces(self, repo_name: str | None = None) -> list[WorkspaceRecord]:
        values = []
        for path in sorted(self.records_dir.glob("*.json")):
            try:
                item = read_json(path)
            except FileNotFoundError:
                continue  # A concurrent remove completed after glob().
            if repo_name is None or item["repo"] == repo_name:
                values.append(item)
        return values

    def create_workspace(
        self, repo: Repo, name: str, commit: str | None, cow: bool = False, *, latest: bool = False
    ) -> WorkspaceRecord:
        if latest and commit is not None:
            raise GitwsError("--latest cannot be combined with --at")
        if latest and not repo.remote:
            raise GitwsError("--latest requires an origin URL")
        with self.locked(f"workspace:{name}"), self.locked(f"repo:{repo.mirror}"):
            if not name or "/" in name or name in {".", ".."}:
                raise GitwsError("workspace name must be a simple name")
            record = self.record_path(name)
            target = self.workspaces_dir / name
            if record.exists() or target.exists():
                raise GitwsError(f"workspace already exists: {name}")
            self.sync_mirror(repo)
            revision = (
                self.cache.sync_remote(repo)
                if latest and repo.source == "local"
                else commit or self.cache.latest(repo)
            )
            try:
                commit = self.cache.resolve_commit(repo, revision)
            except GitwsError:
                if repo.source != "local" or not repo.path or not repo.path.is_dir():
                    raise
                # Fetch a concrete object, never a revision expression/refspec.
                commit = run_git(
                    "rev-parse",
                    "--verify",
                    f"{revision}^{{commit}}",
                    cwd=repo.path,
                    env=lfs.environment(),
                )
                run_git(
                    "fetch",
                    "--no-tags",
                    str(repo.path),
                    commit,
                    cwd=repo.mirror,
                    env=lfs.environment(),
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            lfs_storage = self.lfs_path(repo)
            lfs_env = lfs.environment()
            if repo.path is not None and not cow:
                self.seed_local_lfs(repo.path, lfs_storage)
            if latest:
                # Allow upstream submodule/LFS transfers for this creation only.
                # Keep the alias, mirror, source path and cache identities intact.
                repo = replace(repo, source="remote")
            if cow:
                return self._create_cow_workspace(repo, name, commit, lfs_storage, lfs_env)
            return self._create_worktree_workspace(repo, name, commit, lfs_storage, lfs_env)

    def _create_cow_workspace(
        self, repo: Repo, name: str, commit: str, lfs_storage: Path, lfs_env: dict[str, str]
    ) -> WorkspaceRecord:
        """Create and record a workspace while the caller holds lifecycle locks."""
        target = self.workspaces_dir / name
        record = self.record_path(name)
        base = self._ensure_base(repo, commit, lfs_env)
        upper = self.cow_dir / name / "upper"
        work = self.cow_dir / name / "work"
        upper.mkdir(parents=True, exist_ok=True)
        work.mkdir(parents=True, exist_ok=True)
        try:
            overlay.prepare_metadata(base, target, upper, lfs_storage)
            backend = overlay.mount(base, upper, work, target)
            item = {
                "name": name,
                "repo": repo.name,
                "path": str(target),
                "mirror": str(common_git_dir(target)),
                "commit": run_git("rev-parse", "HEAD", cwd=target),
                "submodules": submodule_snapshot(target),
                "mode": "cow",
                "backend": backend,
                "base": str(base),
                "upper": str(upper),
                "work": str(work),
            }
            write_json(record, item)
            return item
        except Exception:
            overlay.unmount(target)
            overlay.remove_metadata(base, target)
            run_git("worktree", "remove", "--force", str(target), cwd=repo.mirror, check=False)
            shutil.rmtree(self.cow_dir / name, ignore_errors=True)
            raise

    def _create_worktree_workspace(
        self, repo: Repo, name: str, commit: str, lfs_storage: Path, lfs_env: dict[str, str]
    ) -> WorkspaceRecord:
        """Create and record a workspace while the caller holds lifecycle locks."""
        target = self.workspaces_dir / name
        record = self.record_path(name)
        run_git("worktree", "add", "--detach", str(target), commit, cwd=repo.mirror, env=lfs_env)
        try:
            self.cache.update_submodules(target, lfs_env, repo.path, repo.source == "local")
            run_git("config", "lfs.storage", str(lfs_storage), cwd=target)
            if repo.source == "remote":
                lfs.pull(target, lfs_storage)
            else:
                lfs.checkout(target, lfs_storage)
            item = {
                "name": name,
                "repo": repo.name,
                "path": str(target),
                "mirror": str(repo.mirror),
                "commit": run_git("rev-parse", "HEAD", cwd=target),
                "submodules": submodule_snapshot(target),
            }
            write_json(record, item)
            return item
        except Exception:
            run_git("worktree", "remove", "--force", str(target), cwd=repo.mirror, check=False)
            raise

    @contextmanager
    def workspace_locked(self, name: str) -> Iterator[tuple[WorkspaceRecord, Path]]:
        # All lifecycle commands take workspace -> repository locks in this order.
        with self.locked(f"workspace:{name}"):
            record = self.record_path(name)
            if not record.exists():
                raise GitwsError(f"unknown workspace: {name}")
            item = read_json(record)
            repo = self.get(item["repo"])
            target = Path(item["path"]).resolve()
            mirror = (
                Path(item["mirror"])
                if item.get("mirror")
                else common_git_dir(target)
                if (target / ".git").exists()
                else repo.mirror
            )
            if mirror.parent != self.repos_dir:
                raise GitwsError("workspace Git metadata is outside gitws storage")
            with self.locked(f"repo:{mirror}"):
                yield item, mirror

    def _available_path(self, item: WorkspaceRecord) -> Path:
        target = Path(item["path"])
        if not target.is_dir():
            raise GitwsError(f"workspace directory is missing: {target}")
        if item.get("mode") == "cow" and not self.is_mounted(target):
            command = shlex.join(["gitws", "--home", str(self.root), "mount", item["name"]])
            raise GitwsError(f"workspace is not mounted: {item['name']}; run {command}")
        if ensure_repo(target) != target.resolve():
            raise GitwsError(f"workspace is not a Git checkout: {target}")
        return target

    def workspace_path(self, name: str) -> Path:
        with self.workspace_locked(name) as (item, _):
            return self._available_path(item)

    def remove_workspace(self, name: str, force: bool = False) -> None:
        with self.workspace_locked(name) as (item, mirror):
            record = self.record_path(name)
            repo = self.get(item["repo"])
            target = Path(item["path"]).resolve()
            if self.workspaces_dir not in target.parents:
                raise GitwsError("refusing to remove a path outside gitws workspaces")
            if not force:
                ensure_removable(
                    self._available_path(item), protect_submodule_objects=item.get("mode") != "cow"
                )
            if item.get("mode") == "cow":
                overlay.unmount(target)
                overlay.remove_metadata(Path(item["base"]), target)
            # Preserve knowledge of legacy submodule caches before metadata goes.
            for cache in self.submodule_caches(repo):
                self.cache.remember_submodule(repo.mirror, cache)
            run_git("worktree", "remove", "--force", str(target), cwd=mirror)
            record.unlink(missing_ok=True)
            if item.get("mode") == "cow":
                shutil.rmtree(self.cow_dir / name, ignore_errors=True)

    def usage(self, repo: Repo) -> dict[str, DiskUsage]:
        return dict(self.iter_usage(repo))

    def iter_usage(self, repo: Repo) -> Iterator[tuple[str, DiskUsage]]:
        """Measure each row when requested, without scanning mounted COW trees."""

        def paths() -> Iterator[tuple[str, Path, bool]]:
            yield "shared Git objects", repo.mirror, False
            for cache in self.submodule_caches(repo):
                yield f"shared submodule:{cache.stem}", cache, False
            yield "shared LFS objects", self.root / "lfs" / storage_id(repo), False
            if repo.path:
                yield "original checkout", repo.path, True
            for base in self.retained_bases(repo):
                yield f"shared base:{base.name}", base, True
            for item in self.workspaces(repo.name):
                if item.get("mode") == "cow":
                    yield f"workspace:{item['name']} writes", Path(item["upper"]), True
                else:
                    yield f"workspace:{item['name']}", Path(item["path"]), True

        yield from measure_paths(paths())

    def doctor(self, repo: Repo) -> list[str]:
        problems: list[str] = []
        if repo.path is not None:
            if not repo.path.exists():
                problems.append(f"original repository is missing: {repo.path}")
            else:
                try:
                    ensure_repo(repo.path)
                except GitwsError as exc:
                    problems.append(str(exc))
        if not repo.mirror.is_dir():
            problems.append(f"shared Git mirror is missing: {repo.mirror}")
        else:
            try:
                if run_git("rev-parse", "--is-bare-repository", cwd=repo.mirror) != "true":
                    problems.append(f"mirror is not bare: {repo.mirror}")
            except GitwsError as exc:
                problems.append(str(exc))
        for item in self.workspaces(repo.name):
            path = Path(item["path"])
            if not path.is_dir():
                problems.append(f"workspace is missing: {item['name']}")
                continue
            try:
                if item.get("mode") == "cow" and not self.is_mounted(path):
                    problems.append(f"workspace is not mounted: {item['name']}")
                    continue
                run_git("rev-parse", "--verify", "HEAD^{commit}", cwd=path)
                for line in run_git("submodule", "status", "--recursive", cwd=path).splitlines():
                    parts = line[1:].split() if line else []
                    subpath = parts[1] if len(parts) >= 2 else ""
                    if (line and line[0] in "+U") or (
                        line and line[0] == "-" and not submodule_is_optional(path, subpath)
                    ):
                        problems.append(f"submodule is not pinned: {item['name']}: {line}")
            except GitwsError as exc:
                problems.append(f"workspace unavailable: {item['name']}: {exc}")
        return problems

    def clean(self, repo: Repo, unused: bool = False, dry_run: bool = False) -> list[Path]:
        with self.locked(f"repo:{repo.mirror}"):
            removed: list[Path] = []
            if unused:
                used = {item["base"] for item in self.workspaces() if item.get("mode") == "cow"}
                for base in self.retained_bases(repo):
                    if str(base) in used or self.is_mounted(base):
                        continue
                    if overlay.has_private_submodule_data(base):
                        continue
                    if not dry_run:
                        owner = common_git_dir(base)
                        if owner.parent != self.repos_dir:
                            raise GitwsError("base Git metadata is outside gitws storage")
                        for cache in self.submodule_caches(repo):
                            self.cache.remember_submodule(repo.mirror, cache)
                        run_git("worktree", "remove", "--force", str(base), cwd=owner)
                    removed.append(base)
            for item in self.workspaces(repo.name):
                path = Path(item["path"])
                if not dry_run and not path.exists() and item.get("mode") != "cow":
                    self.record_path(item["name"]).unlink(missing_ok=True)
            if not dry_run and repo.mirror.exists():
                run_git("worktree", "prune", cwd=repo.mirror)
            return removed
