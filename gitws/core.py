from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None


class GitwsError(RuntimeError):
    pass


def run_git(*args: str, cwd: Path | None = None, check: bool = True,
            env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=env,
    )
    if check and proc.returncode:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise GitwsError(f"git {' '.join(args)} failed: {detail}")
    return proc.stdout.strip()


def run_command(*args: str, cwd: Path | None = None,
                check: bool = True) -> str:
    proc = subprocess.run(
        list(args), cwd=cwd, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and proc.returncode:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise GitwsError(f"{' '.join(args)} failed: {detail}")
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
            index_entry = run_git("ls-files", "--error-unmatch", "--stage", "--",
                                  subpath, cwd=path, check=False)
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
        output = run_git("config", "--file", str(gitmodules), "--get-regexp",
                        r"^submodule\..*\.path$", cwd=parent, check=False)
        for key, subpath in (line.split(None, 1) for line in output.splitlines()):
            section = re.match(r"^submodule\.(.+)\.path$", key)
            if not section:
                continue
            candidate = str(prefix / subpath) if str(prefix) != "." else subpath
            if candidate != relative:
                continue
            entry = run_git("ls-files", "--error-unmatch", "--stage", "--",
                            relative, cwd=path, check=False)
            return not entry.startswith("160000 ")
    return False


def submodule_update_override(path: Path, relative: str) -> str | None:
    """Return a config override for submodules disabled by .gitmodules."""
    gitmodules = path / ".gitmodules"
    if not gitmodules.exists():
        return None
    output = run_git("config", "--file", str(gitmodules), "--get-regexp",
                    r"^submodule\..*\.path$", cwd=path, check=False)
    for key, subpath in (line.split(None, 1) for line in output.splitlines()):
        if subpath != relative:
            continue
        match = re.match(r"^submodule\.(.+)\.path$", key)
        if not match:
            return None
        section = match.group(1)
        update = run_git("config", "--file", str(gitmodules), "--get",
                         f"submodule.{section}.update", cwd=path, check=False)
        if update == "none":
            return f"submodule.{section}.update=checkout"
        return None
    return None


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
        self.bases_dir = self.root / "bases"
        self.cow_dir = self.root / "cow"
        for directory in (self.repos_dir, self.workspaces_dir, self.records_dir,
                          self.bases_dir, self.cow_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.config = self.root / "config.json"
        self.state_lock_path = self.root / "state.lock"
        self.mirror_lock_path = self.root / "mirror.lock"

    @contextmanager
    def locked(self, kind: str = "state"):
        lock_path = self.state_lock_path if kind == "state" else self.mirror_lock_path
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
        return Repo(name, Path(raw["path"]), raw["remote"], raw["branch"],
                    Path(raw["mirror"]))

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
        with self.locked():
            data = self._load()
            data.setdefault("repos", {})[name] = Repo(name, path, remote, branch, mirror).as_dict()
            self._save(data)
        return self.get(name)

    def all_repos(self) -> dict[str, Repo]:
        return {name: self.get(name) for name in self._load().get("repos", {})}

    def sync_mirror(self, repo: Repo) -> None:
        with self.locked("mirror"):
            if not repo.mirror.exists():
                repo.mirror.parent.mkdir(parents=True, exist_ok=True)
                run_git("clone", "--mirror", repo.remote, str(repo.mirror))
            else:
                run_git("fetch", "--prune", "origin", cwd=repo.mirror)

    def submodule_mirror(self, url: str) -> Path:
        mirror = self.repos_dir / f"{repo_id(url)}.git"
        with self.locked("mirror"):
            if not mirror.exists():
                run_git("clone", "--mirror", url, str(mirror))
            else:
                run_git("fetch", "--prune", "origin", cwd=mirror)
        return mirror

    def lfs_path(self, repo: Repo) -> Path:
        path = self.root / "lfs" / repo_id(repo.remote)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _lfs_env() -> dict[str, str]:
        env = os.environ.copy()
        env["GIT_LFS_SKIP_SMUDGE"] = "1"
        return env

    def update_submodules(self, path: Path, env: dict[str, str] | None = None) -> None:
        run_git("submodule", "sync", "--recursive", cwd=path, env=env)
        self._update_submodule_tree(path, env)

    def _update_submodule_tree(self, path: Path, env: dict[str, str] | None = None) -> None:
        # Initialize one child at a time so each clone gets its own mirror as
        # an explicit alternates/reference store. Nested .gitmodules become
        # visible only after their parent has been checked out.
        for subpath, url in submodule_urls(path).items():
            mirror = self.submodule_mirror(url)
            override = submodule_update_override(path, subpath)
            update_args = ["-c", override] if override else []
            run_git(
                *update_args, "-c", "protocol.file.allow=always",
                "-c", f"url.{mirror.as_uri()}.insteadOf={url}",
                "submodule", "update", "--init", "--reference", str(mirror),
                subpath, cwd=path, env=env,
            )
            child = path / subpath
            if child.is_dir():
                self._update_submodule_tree(child, env)

    def pull_lfs(self, path: Path, storage: Path) -> None:
        if shutil.which("git-lfs") is None:
            return
        run_git("config", "lfs.storage", str(storage), cwd=path)
        run_git("lfs", "pull", cwd=path)
        for subpath in submodule_urls(path):
            child = path / subpath
            if child.is_dir():
                self.pull_lfs(child, storage)

    def latest(self, repo: Repo) -> str:
        self.sync_mirror(repo)
        return run_git("rev-parse", f"refs/heads/{repo.branch}", cwd=repo.mirror)

    def record_path(self, name: str) -> Path:
        return self.records_dir / f"{name}.json"

    def base_path(self, repo: Repo, commit: str) -> Path:
        return self.bases_dir / repo_id(repo.remote) / commit

    @staticmethod
    def _metadata_files(path: Path) -> list[Path]:
        values: list[Path] = []
        for root, dirs, files in os.walk(path):
            dirs[:] = [name for name in dirs if name != ".git"]
            if ".git" in files:
                values.append(Path(root) / ".git")
        return values

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
                self.update_submodules(base, env)
                self.pull_lfs(base, self.lfs_path(repo))
            return base
        base.parent.mkdir(parents=True, exist_ok=True)
        run_git("worktree", "add", "--detach", str(base), commit,
                cwd=repo.mirror, env=env)
        try:
            self.update_submodules(base, env)
            self.pull_lfs(base, self.lfs_path(repo))
            return base
        except Exception:
            run_git("worktree", "remove", "--force", str(base),
                    cwd=repo.mirror, check=False)
            raise

    def _prepare_cow_metadata(self, base: Path, target: Path, upper: Path,
                              storage: Path) -> None:
        """Create only Git metadata; source files come from the lower layer."""
        run_git("worktree", "add", "--detach", "--no-checkout", str(target),
                run_git("rev-parse", "HEAD", cwd=base), cwd=base)
        run_git("reset", "--mixed", "HEAD", cwd=target)
        run_git("config", "lfs.storage", str(storage), cwd=target)
        metadata = target / ".git"
        destination = upper / ".git"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(metadata, destination)
        self._prepare_cow_children(base, target, upper, storage)

    def _prepare_cow_children(self, base: Path, target: Path, upper: Path,
                              storage: Path) -> None:
        entries = list(submodule_urls(base))

        def prepare(subpath: str) -> None:
            base_child = base / subpath
            target_child = target / subpath
            if not base_child.is_dir():
                return
            target_child.parent.mkdir(parents=True, exist_ok=True)
            commit = run_git("rev-parse", "HEAD", cwd=base_child)
            run_git("worktree", "add", "--detach", "--no-checkout",
                    str(target_child), commit, cwd=base_child)
            run_git("reset", "--mixed", "HEAD", cwd=target_child)
            run_git("config", "lfs.storage", str(storage), cwd=target_child)
            child_metadata = target_child / ".git"
            child_destination = upper / subpath / ".git"
            child_destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child_metadata, child_destination)
            self._prepare_cow_children(base_child, target_child, upper / subpath,
                                       storage)

        if len(entries) < 2:
            for subpath in entries:
                prepare(subpath)
            return
        with ThreadPoolExecutor(max_workers=min(8, len(entries))) as pool:
            list(pool.map(prepare, entries))

    def _remove_cow_metadata(self, base: Path, target: Path) -> None:
        for subpath in submodule_urls(base):
            base_child = base / subpath
            target_child = target / subpath
            if base_child.is_dir() and target_child.exists():
                self._remove_cow_metadata(base_child, target_child)
                run_git("worktree", "remove", "--force", str(target_child),
                        cwd=base_child, check=False)

    def _mount_cow(self, lower: Path, upper: Path, work: Path,
                   target: Path) -> str:
        options = f"lowerdir={lower},upperdir={upper},workdir={work}"
        if os.geteuid() == 0 and shutil.which("mount"):
            try:
                run_command("mount", "-t", "overlay", "overlay", "-o", options,
                            str(target))
                return "overlayfs"
            except GitwsError:
                pass
        fuse = shutil.which("fuse-overlayfs")
        if fuse:
            run_command(fuse, "-o", options, str(target))
            return "fuse-overlayfs"
        raise GitwsError("copy-on-write workspaces require overlayfs or fuse-overlayfs")

    @staticmethod
    def is_mounted(path: Path) -> bool:
        try:
            mounts = Path("/proc/self/mountinfo").read_text().splitlines()
        except OSError:
            return False
        needle = str(path.resolve())
        return any(f" {needle} " in line or line.endswith(f" {needle}") for line in mounts)

    def _unmount_cow(self, target: Path) -> None:
        if not self.is_mounted(target):
            return
        if shutil.which("umount") is None:
            raise GitwsError("cannot remove copy-on-write workspace: umount is unavailable")
        run_command("umount", str(target))

    def workspaces(self, repo_name: str | None = None) -> list[dict[str, Any]]:
        values = []
        for path in sorted(self.records_dir.glob("*.json")):
            item = json.loads(path.read_text())
            if repo_name is None or item["repo"] == repo_name:
                values.append(item)
        return values

    def create_workspace(self, repo: Repo, name: str, commit: str | None,
                         cow: bool = False) -> dict[str, Any]:
        with self.locked("state"):
            if not name or "/" in name or name in {".", ".."}:
                raise GitwsError("workspace name must be a simple name")
            record = self.record_path(name)
            target = self.workspaces_dir / name
            if record.exists() or target.exists():
                raise GitwsError(f"workspace already exists: {name}")
            self.sync_mirror(repo)
            commit = commit or self.latest(repo)
            commit = run_git("rev-parse", "--verify", f"{commit}^{{commit}}",
                             cwd=repo.mirror)
            target.parent.mkdir(parents=True, exist_ok=True)
            lfs_storage = self.lfs_path(repo)
            lfs_env = self._lfs_env()
            if cow:
                base = self._ensure_base(repo, commit, lfs_env)
                upper = self.cow_dir / name / "upper"
                work = self.cow_dir / name / "work"
                upper.mkdir(parents=True, exist_ok=True)
                work.mkdir(parents=True, exist_ok=True)
                try:
                    self._prepare_cow_metadata(base, target, upper, lfs_storage)
                    backend = self._mount_cow(base, upper, work, target)
                    item = {"name": name, "repo": repo.name, "path": str(target),
                            "commit": run_git("rev-parse", "HEAD", cwd=target),
                            "submodules": submodule_snapshot(target),
                            "mode": "cow", "backend": backend,
                            "base": str(base), "upper": str(upper), "work": str(work)}
                    record.write_text(json.dumps(item, indent=2, sort_keys=True) + "\n")
                    return item
                except Exception:
                    self._unmount_cow(target)
                    self._remove_cow_metadata(base, target)
                    run_git("worktree", "remove", "--force", str(target),
                            cwd=repo.mirror, check=False)
                    shutil.rmtree(self.cow_dir / name, ignore_errors=True)
                    raise
            run_git("worktree", "add", "--detach", str(target), commit,
                    cwd=repo.mirror, env=lfs_env)
            try:
                self.update_submodules(target, lfs_env)
                self.pull_lfs(target, lfs_storage)
                item = {"name": name, "repo": repo.name, "path": str(target),
                        "commit": run_git("rev-parse", "HEAD", cwd=target),
                        "submodules": submodule_snapshot(target)}
                record.write_text(json.dumps(item, indent=2, sort_keys=True) + "\n")
                return item
            except Exception:
                run_git("worktree", "remove", "--force", str(target), cwd=repo.mirror, check=False)
                raise

    def remove_workspace(self, name: str) -> None:
        with self.locked("state"):
            record = self.records_dir / f"{name}.json"
            if not record.exists():
                raise GitwsError(f"unknown workspace: {name}")
            item = json.loads(record.read_text())
            repo = self.get(item["repo"])
            target = Path(item["path"]).resolve()
            if self.workspaces_dir not in target.parents:
                raise GitwsError("refusing to remove a path outside gitws workspaces")
            if item.get("mode") == "cow":
                self._unmount_cow(target)
                self._remove_cow_metadata(Path(item["base"]), target)
            run_git("worktree", "remove", "--force", str(target), cwd=repo.mirror)
            record.unlink(missing_ok=True)
            if item.get("mode") == "cow":
                shutil.rmtree(self.cow_dir / name, ignore_errors=True)

    def usage(self, repo: Repo) -> dict[str, dict[str, int]]:
        def measure(path: Path, seen: set[tuple[int, int]], exclude_git: bool = False) -> dict[str, int]:
            logical = 0
            physical = 0
            if not path.exists():
                return {"logical": 0, "physical": 0}
            for root, dirs, files in os.walk(path, followlinks=False):
                if exclude_git and Path(root).name == ".git":
                    dirs[:] = []
                    continue
                dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink()]
                for name in files:
                    file_path = Path(root) / name
                    try:
                        stat = file_path.stat()
                    except OSError:
                        continue
                    logical += stat.st_size
                    identity = (stat.st_dev, stat.st_ino)
                    if identity not in seen:
                        seen.add(identity)
                        physical += stat.st_blocks * 512 if stat.st_blocks else stat.st_size
            return {"logical": logical, "physical": physical}

        seen: set[tuple[int, int]] = set()
        lfs_path = self.root / "lfs" / repo_id(repo.remote)
        result = {"shared Git objects": measure(repo.mirror, seen),
                  "shared LFS objects": measure(lfs_path, seen),
                  "original checkout": measure(repo.path, seen, exclude_git=True)}
        cow_bases: set[str] = set()
        for item in self.workspaces(repo.name):
            if item.get("mode") == "cow":
                base = Path(item["base"])
                if str(base) not in cow_bases:
                    result[f"shared base:{base.name}"] = measure(base, seen, exclude_git=True)
                    cow_bases.add(str(base))
                result[f"workspace:{item['name']} writes"] = measure(
                    Path(item["upper"]), seen, exclude_git=True)
            else:
                result[f"workspace:{item['name']}"] = measure(
                    Path(item["path"]), seen, exclude_git=True)
        result["total"] = {
            key: sum(value[key] for name, value in result.items() if name != "total")
            for key in ("logical", "physical")
        }
        return result

    def doctor(self, repo: Repo) -> list[str]:
        problems: list[str] = []
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
                current = run_git("rev-parse", "HEAD", cwd=path)
                if current != item["commit"]:
                    problems.append(f"workspace commit changed: {item['name']}")
                for line in run_git("submodule", "status", "--recursive", cwd=path).splitlines():
                    parts = line[1:].split() if line else []
                    subpath = parts[1] if len(parts) >= 2 else ""
                    if (line and line[0] in "+U") or (
                            line and line[0] == "-" and
                            not submodule_is_optional(path, subpath)):
                        problems.append(f"submodule is not pinned: {item['name']}: {line}")
            except GitwsError as exc:
                problems.append(f"workspace unavailable: {item['name']}: {exc}")
        return problems

    def clean(self, repo: Repo) -> None:
        with self.locked("state"):
            for item in self.workspaces(repo.name):
                path = Path(item["path"])
                if not path.exists():
                    self.record_path(item["name"]).unlink(missing_ok=True)
            if repo.mirror.exists():
                run_git("worktree", "prune", cwd=repo.mirror)
