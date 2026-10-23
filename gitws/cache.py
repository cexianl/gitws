"""Shared Git/submodule object caches and source reference synchronization."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from . import lfs
from .errors import GitwsError
from .git import (
    absolute_remote,
    common_git_dir,
    local_clone_option,
    run_git,
    submodule_update_override,
    submodule_urls,
)
from .models import Repo
from .storage import locked, read_json, repo_id, write_json


class RepositoryCache:
    """Manage object stores without owning workspace lifecycle or records."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.repos_dir = root / "repos"

    @contextmanager
    def locked(self, kind: str) -> Iterator[None]:
        with locked(self.root, kind):
            yield

    def sync_mirror(self, repo: Repo) -> None:
        with self.locked(f"mirror:{repo.mirror}"):
            local = repo.source == "local"
            available = not local or bool(repo.path and repo.path.is_dir())
            env = lfs.environment() if local else None
            source = str(repo.path) if local else repo.remote
            if not repo.mirror.exists():
                if not available:
                    raise GitwsError(f"local repository is missing: {repo.path}")
                options = [local_clone_option(repo.path)] if local else []
                run_git("clone", "--mirror", *options, source, str(repo.mirror), env=env)
            self._configure_origin(repo.mirror, absolute_remote(repo.remote, repo.path))
            if available:
                # Never fetch or prune workspace branches/tags. HEAD also tracks
                # detached local commits and changes to a remote default branch.
                run_git(
                    "fetch",
                    "--prune",
                    "--no-tags",
                    source,
                    "+refs/heads/*:refs/gitws/source/heads/*",
                    "+refs/tags/*:refs/gitws/source/tags/*",
                    "+HEAD:refs/gitws/source-head",
                    cwd=repo.mirror,
                    env=env,
                )
            if shutil.which("git-lfs") is not None:
                run_git("lfs", "install", "--local", cwd=repo.mirror)

    @staticmethod
    def _configure_origin(mirror: Path, remote: str) -> None:
        """Migrate mirror defaults without overwriting later user settings."""
        if run_git("config", "--bool", "remote.origin.mirror", cwd=mirror, check=False) != "true":
            return
        run_git("config", "--remove-section", "remote.origin", cwd=mirror)
        if remote:
            run_git("remote", "add", "origin", remote, cwd=mirror)

    def submodule_mirror(
        self,
        url: str,
        local_path: Path | None = None,
        local_only: bool = False,
        commit: str | None = None,
        legacy_url: str | None = None,
    ) -> Path:
        mirror = self.repos_dir / f"{repo_id(url)}.git"
        if not mirror.exists() and legacy_url and commit:
            legacy = self.repos_dir / f"{repo_id(legacy_url)}.git"
            if legacy.is_dir() and run_git(
                "rev-parse",
                "--verify",
                f"{commit}^{{commit}}",
                cwd=legacy,
                check=False,
                env=lfs.environment(),
            ):
                # Keep its path: existing submodules may still reference it.
                mirror = legacy
        with self.locked(f"mirror:{mirror}"):
            if local_only:
                if local_path and (local_path / ".git").exists():
                    local_env = os.environ.copy()
                    local_env["GIT_NO_LAZY_FETCH"] = "1"
                    if not mirror.exists():
                        run_git(
                            "clone",
                            "--mirror",
                            local_clone_option(local_path),
                            str(local_path),
                            str(mirror),
                            env=local_env,
                        )
                    else:
                        run_git(
                            "fetch",
                            "--no-tags",
                            str(local_path),
                            "+refs/heads/*:refs/gitws/source/heads/*",
                            cwd=mirror,
                            env=local_env,
                        )
                elif not mirror.exists():
                    raise GitwsError(f"local submodule is unavailable: {local_path}")
            elif not mirror.exists():
                if local_path and (local_path / ".git").exists():
                    run_git(
                        "clone",
                        "--mirror",
                        local_clone_option(local_path),
                        str(local_path),
                        str(mirror),
                        env=lfs.environment(),
                    )
                else:
                    run_git("clone", "--mirror", url, str(mirror))
            else:
                run_git(
                    "fetch",
                    "--prune",
                    url,
                    "+refs/heads/*:refs/gitws/source/heads/*",
                    "+refs/tags/*:refs/gitws/source/tags/*",
                    cwd=mirror,
                )
            if commit:
                present = run_git(
                    "rev-parse",
                    "--verify",
                    f"{commit}^{{commit}}",
                    cwd=mirror,
                    check=False,
                    env=lfs.environment(),
                )
                if not present:
                    source = str(local_path) if local_only else url
                    if local_only and not (local_path and (local_path / ".git").exists()):
                        raise GitwsError(f"local submodule commit is missing: {commit}")
                    run_git(
                        "fetch",
                        "--no-tags",
                        source,
                        commit,
                        cwd=mirror,
                        env=lfs.environment() if local_only else None,
                    )
                # Keep gitlink commits reachable even when no branch contains them.
                run_git("update-ref", f"refs/gitws/pins/{commit}", commit, cwd=mirror)
            self._configure_origin(mirror, url)
        return mirror

    def update_submodules(
        self,
        path: Path,
        env: dict[str, str] | None = None,
        source_path: Path | None = None,
        local_only: bool = False,
        *,
        checkout: Callable[[Path], None] | None = None,
    ) -> None:
        run_git("submodule", "sync", "--recursive", cwd=path, env=env)
        self._update_submodule_tree(
            path, env, source_path, local_only, common_git_dir(path), checkout
        )

    def remember_submodule(self, owner: Path, mirror: Path) -> None:
        if owner.parent != self.repos_dir:
            return
        index = self.root / "cache-index" / f"{owner.stem}.json"
        values = self._submodule_cache_paths(owner)
        values.add(mirror)
        write_json(index, {"mirrors": sorted(str(value) for value in values)})

    def submodule_caches(self, repo: Repo) -> list[Path]:
        return sorted(self._submodule_cache_paths(repo.mirror))

    def _submodule_cache_paths(self, owner: Path) -> set[Path]:
        index = self.root / "cache-index" / f"{owner.stem}.json"
        values: set[Path] = set()
        if index.exists():
            saved = read_json(index)
            if isinstance(saved, dict):
                return {Path(value) for value in saved["mirrors"]}
            values.update(Path(value) for value in saved)
        # Discover alternates from stores created before cache indexes existed.
        # A partial old index also needs discovery. Persist the union on the
        # next write so subsequent usage calls avoid repeatedly scanning it.
        for alternate in (owner / "worktrees").glob("**/objects/info/alternates"):
            for value in alternate.read_text().splitlines():
                objects = Path(value)
                if not objects.is_absolute():
                    objects = (alternate.parent.parent / objects).resolve()
                if objects.name == "objects" and objects.parent.parent == self.repos_dir:
                    values.add(objects.parent)
        return values

    def _update_submodule_tree(
        self,
        path: Path,
        env: dict[str, str] | None = None,
        source_path: Path | None = None,
        local_only: bool = False,
        owner: Path | None = None,
        checkout: Callable[[Path], None] | None = None,
    ) -> None:
        # Initialize one child at a time so each clone gets its own mirror as
        # an explicit alternates/reference store. Nested .gitmodules become
        # visible only after their parent has been checked out.
        urls = submodule_urls(path)
        if not urls:
            return
        # A source without origin resolves relative URLs against its own path,
        # not the location of an ordinary/COW checkout created from it.
        origin = run_git("remote", "get-url", "origin", cwd=path, check=False)
        basis = origin or (str(source_path) if source_path else "")
        options = ["-c", f"remote.origin.url={basis}"] if basis else []
        run_git(*options, "submodule", "sync", cwd=path, env=env)
        run_git(*options, "submodule", "init", cwd=path, env=env)
        entries = run_git(
            "config",
            "--file",
            str(path / ".gitmodules"),
            "--get-regexp",
            r"^submodule\..*\.path$",
            cwd=path,
        )
        names = {
            value: key[len("submodule.") : -len(".path")]
            for key, value in (line.split(None, 1) for line in entries.splitlines())
        }
        for subpath in urls:
            name = names[subpath]
            url = run_git("config", "--get", f"submodule.{name}.url", cwd=path)
            local_child = source_path / subpath if source_path else None
            commit = run_git("ls-files", "--stage", "--", subpath, cwd=path).split()[1]
            mirror = self.submodule_mirror(url, local_child, local_only, commit, urls[subpath])
            if owner:
                self.remember_submodule(owner, mirror)
            child = path / subpath
            if checkout is not None:
                # Initialize private metadata without allocating a complete
                # checkout before bounded materialization and deduplication.
                if (child / ".git").exists():
                    raise GitwsError(f"shared base submodule already exists: {child}")
                run_git(
                    "-c",
                    "protocol.file.allow=always",
                    "clone",
                    "--no-checkout",
                    "--reference",
                    str(mirror),
                    mirror.as_uri(),
                    str(child),
                    cwd=path,
                    env=env,
                )
                run_git("submodule", "absorbgitdirs", "--", subpath, cwd=path, env=env)
                run_git("reset", "--mixed", "--quiet", commit, cwd=child, env=env)
                run_git("remote", "set-url", "origin", url, cwd=child)
                checkout(child)
                self._update_submodule_tree(child, env, local_child, local_only, owner, checkout)
                continue
            if (child / ".git").exists() and not run_git(
                "rev-parse",
                "--verify",
                f"{commit}^{{commit}}",
                cwd=child,
                check=False,
                env=lfs.environment(),
            ):
                run_git("fetch", "--no-tags", str(mirror), commit, cwd=child, env=env)
            override = submodule_update_override(path, subpath)
            update_args = ["-c", override] if override else []
            run_git(
                *update_args,
                "-c",
                "protocol.file.allow=always",
                "-c",
                f"submodule.{name}.url={mirror.as_uri()}",
                "submodule",
                "update",
                "--init",
                "--no-fetch",
                "--reference",
                str(mirror),
                "--",
                subpath,
                cwd=path,
                env=env,
            )
            if child.is_dir():
                run_git("remote", "set-url", "origin", url, cwd=child)
                self._update_submodule_tree(child, env, local_child, local_only, owner)

    def latest(self, repo: Repo) -> str:
        return run_git("rev-parse", "refs/gitws/source-head", cwd=repo.mirror)

    def resolve_commit(self, repo: Repo, revision: str) -> str:
        for ref in (
            f"refs/gitws/source/heads/{revision}",
            f"refs/gitws/source/tags/{revision}",
            revision,
        ):
            value = run_git(
                "rev-parse",
                "--verify",
                f"{ref}^{{commit}}",
                cwd=repo.mirror,
                check=False,
                env=lfs.environment(),
            )
            if value:
                return value
        raise GitwsError(f"unknown commit: {revision}")

    def sync_remote(self, repo: Repo) -> str:
        if not repo.remote:
            raise GitwsError("repository has no origin URL")
        with self.locked(f"mirror:{repo.mirror}"):
            run_git(
                "fetch",
                "--no-tags",
                absolute_remote(repo.remote, repo.path),
                "+HEAD:refs/gitws/remote-head",
                cwd=repo.mirror,
            )
            return run_git("rev-parse", "refs/gitws/remote-head", cwd=repo.mirror)
