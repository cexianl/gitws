from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gitws.core import Store
from gitws.git import run_git
from gitws.models import Repo, WorkspaceRecord

HAS_COW = bool(shutil.which("fuse-overlayfs")) and Path("/dev/fuse").exists()


class RepositoryTestCase(unittest.TestCase):
    """An isolated local source, remote, and gitws store for integration tests."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gitws-test-")
        self.root = Path(self.tmp.name)
        config = self.root / "gitconfig"
        config.write_text(
            '[protocol "file"]\nallow = always\n[user]\nname = Test\nemail = test@example.com\n'
        )
        self.env = patch.dict(
            os.environ, {"GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1"}
        )
        self.env.start()
        self.source = self.make_repo("source")
        self.remote = self.root / "remote.git"
        run_git("clone", "--bare", str(self.source), str(self.remote))
        run_git("remote", "add", "origin", self.remote.as_uri(), cwd=self.source)
        self.store = Store(self.root / "store")
        self.repo = self.store.add("project", self.source)

    def tearDown(self) -> None:
        # Always unmount before TemporaryDirectory deletes anything, even if
        # the mount-detection regression itself failed.
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            target = line.split()[4].replace("\\040", " ").replace("\\134", "\\")
            if target.startswith(str(self.root) + "/"):
                subprocess.run(["umount", target], check=True, capture_output=True)
        self.env.stop()
        self.tmp.cleanup()

    def make_repo(self, name: str) -> Path:
        path = self.root / name
        run_git("init", "-b", "main", str(path))
        (path / "file").write_text("initial\n")
        run_git("add", ".", cwd=path)
        run_git("commit", "-m", "initial", cwd=path)
        return path

    def commit(self, path: Path, content: str = "changed\n") -> str:
        (path / "file").write_text(content)
        run_git("add", "file", cwd=path)
        run_git("commit", "-m", "change", cwd=path)
        return run_git("rev-parse", "HEAD", cwd=path)

    def new(self, name: str, repo: Repo | None = None, *, cow: bool = False) -> WorkspaceRecord:
        return self.store.create_workspace(repo or self.repo, name, None, cow=cow)

    def add_child(self, url: str | None = None) -> Path:
        child = self.make_repo("child")
        run_git("submodule", "add", str(child), "lib", cwd=self.source)
        if url:
            run_git("config", "-f", ".gitmodules", "submodule.lib.url", url, cwd=self.source)
        run_git("commit", "-am", "add child", cwd=self.source)
        return child
