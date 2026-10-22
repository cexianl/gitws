from __future__ import annotations

import io
import shutil
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from gitws.cli import main
from gitws.errors import GitwsError
from gitws.git import run_git
from tests.support import HAS_COW, RepositoryTestCase


class LatestTests(RepositoryTestCase):
    def upstream(self):
        path = self.root / "upstream-work"
        run_git("clone", str(self.remote), str(path))
        return path

    def publish(self, path):
        commit = self.commit(path, "upstream\n")
        run_git("push", "origin", "HEAD", cwd=path)
        return commit

    def test_latest_reuses_mirror_and_preserves_local_head_and_active_task(self):
        target = Path(self.new("task-a")["path"])
        run_git("switch", "-c", "task-a", cwd=target)
        task_commit = self.commit(target, "task commit\n")
        (target / "file").write_text("task edits\n")
        (self.source / "file").write_text("source edits\n")
        original = run_git("rev-parse", "HEAD", cwd=self.source)
        upstream = self.upstream()
        run_git("switch", "-c", "trunk", cwd=upstream)
        latest = self.publish(upstream)
        run_git("symbolic-ref", "HEAD", "refs/heads/trunk", cwd=self.remote)
        item = self.store.create_workspace(self.repo, "task-b", None, latest=True)
        self.assertEqual(latest, item["commit"])
        self.assertEqual(str(self.repo.mirror), item["mirror"])
        self.assertEqual([self.repo.mirror], list(self.store.repos_dir.glob("*.git")))
        self.assertEqual(task_commit, run_git("rev-parse", "HEAD", cwd=target))
        self.assertEqual("task edits\n", (target / "file").read_text())
        self.assertEqual(original, run_git("rev-parse", "HEAD", cwd=self.source))
        self.assertEqual("source edits\n", (self.source / "file").read_text())
        self.assertEqual(original, self.new("local-again")["commit"])

    def test_latest_initial_creation_and_submodule_updates(self):
        child = self.add_child()
        old_child = run_git("rev-parse", "HEAD", cwd=self.source / "lib")
        run_git("push", "origin", "main", cwd=self.source)
        upstream = self.upstream()
        run_git("submodule", "update", "--init", cwd=upstream)
        child_commit = self.commit(child, "new child\n")
        run_git("fetch", "origin", cwd=upstream / "lib")
        run_git("switch", "--detach", child_commit, cwd=upstream / "lib")
        run_git("commit", "-am", "update child", cwd=upstream)
        run_git("push", "origin", "main", cwd=upstream)
        item = self.store.create_workspace(self.repo, "task", None, latest=True)
        self.assertEqual(child_commit, run_git("rev-parse", "HEAD", cwd=Path(item["path"]) / "lib"))
        self.assertEqual(old_child, run_git("rev-parse", "HEAD", cwd=self.source / "lib"))
        self.assertEqual([], self.store.doctor(self.repo))

    def test_latest_cli_and_mutually_exclusive_revision(self):
        latest = self.publish(self.upstream())
        with redirect_stdout(io.StringIO()):
            main(["--home", str(self.store.root), "new", "project", "task", "--latest"])
        self.assertEqual(
            latest, run_git("rev-parse", "HEAD", cwd=self.store.workspaces_dir / "task")
        )
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(
                [
                    "--home",
                    str(self.store.root),
                    "new",
                    "project",
                    "bad",
                    "--at",
                    latest,
                    "--latest",
                ]
            )
        self.assertFalse(self.store.record_path("bad").exists())

    def test_latest_requires_upstream_and_rejects_at_in_store(self):
        run_git("remote", "remove", "origin", cwd=self.source)
        repo = self.store.add("offline", self.source)
        with self.assertRaisesRegex(GitwsError, "origin"):
            self.store.create_workspace(repo, "bad", None, latest=True)
        with self.assertRaisesRegex(GitwsError, "--at"):
            self.store.create_workspace(self.repo, "bad-at", "main", latest=True)

    def test_sync_uses_current_remote_default_branch(self):
        upstream = self.upstream()
        run_git("switch", "-c", "trunk", cwd=upstream)
        latest = self.publish(upstream)
        run_git("symbolic-ref", "HEAD", "refs/heads/trunk", cwd=self.remote)
        self.assertEqual(latest, self.store.switch_source(self.repo))
        self.assertEqual(latest, run_git("rev-parse", "HEAD", cwd=self.source))

    def test_latest_remote_source_and_relative_local_origin(self):
        latest = self.publish(self.upstream())
        remote_repo = self.store.add("remote", self.remote.as_uri())
        self.assertEqual(
            latest, self.store.create_workspace(remote_repo, "url", None, latest=True)["commit"]
        )
        run_git("remote", "set-url", "origin", "../remote.git", cwd=self.source)
        local_repo = self.store.add("relative", self.source)
        self.assertEqual(
            latest, self.store.create_workspace(local_repo, "relative", None, latest=True)["commit"]
        )

    @unittest.skipUnless(shutil.which("git-lfs"), "git-lfs is unavailable")
    def test_latest_fetches_new_lfs_content_without_changing_existing_data(self):
        run_git("lfs", "install", "--local", cwd=self.source)
        run_git("lfs", "track", "*.bin", cwd=self.source)
        (self.source / "asset.bin").write_bytes(b"old content\n")
        run_git("add", ".gitattributes", "asset.bin", cwd=self.source)
        run_git("commit", "-m", "asset", cwd=self.source)
        run_git("push", "origin", "main", cwd=self.source)
        old = Path(self.new("old")["path"])
        upstream = self.upstream()
        run_git("lfs", "install", "--local", cwd=upstream)
        (upstream / "asset.bin").write_bytes(b"new content\n")
        run_git("commit", "-am", "new asset", cwd=upstream)
        run_git("push", "origin", "main", cwd=upstream)
        for use_cow in (False, True) if HAS_COW else (False,):
            item = self.store.create_workspace(
                self.repo, f"new-{use_cow}", None, cow=use_cow, latest=True
            )
            self.assertEqual(b"new content\n", (Path(item["path"]) / "asset.bin").read_bytes())
        self.assertEqual(b"old content\n", (old / "asset.bin").read_bytes())
        self.assertEqual(b"old content\n", (self.source / "asset.bin").read_bytes())

    @unittest.skipUnless(HAS_COW, "fuse-overlayfs is unavailable")
    def test_latest_cow_preserves_old_base(self):
        old = self.new("task-a", cow=True)
        latest = self.publish(self.upstream())
        new = self.store.create_workspace(self.repo, "task-b", None, cow=True, latest=True)
        self.assertEqual(latest, new["commit"])
        self.assertEqual("initial\n", (Path(old["path"]) / "file").read_text())
        self.assertEqual("upstream\n", (Path(new["path"]) / "file").read_text())
        self.assertNotEqual(old["base"], new["base"])

    @unittest.skipUnless(shutil.which("git-lfs"), "git-lfs is unavailable")
    def test_recursive_lfs_hydrates_without_global_filters(self):
        child = self.add_child()
        leaf = self.make_repo("leaf")
        run_git("lfs", "install", "--local", cwd=leaf)
        run_git("lfs", "track", "*.bin", cwd=leaf)
        (leaf / "asset.bin").write_bytes(b"nested LFS content\n")
        run_git("add", ".gitattributes", "asset.bin", cwd=leaf)
        run_git("commit", "-m", "leaf asset", cwd=leaf)
        run_git("submodule", "add", str(leaf), "nested", cwd=child)
        run_git("lfs", "install", "--local", cwd=child / "nested")
        run_git("lfs", "pull", cwd=child / "nested")
        run_git("commit", "-am", "nested child", cwd=child)
        local_child = self.source / "lib"
        run_git("fetch", "origin", "main", cwd=local_child)
        run_git("switch", "--detach", "FETCH_HEAD", cwd=local_child)
        run_git("submodule", "update", "--init", "--recursive", cwd=local_child)
        run_git("lfs", "install", "--local", cwd=local_child / "nested")
        run_git("lfs", "pull", cwd=local_child / "nested")
        run_git("commit", "-am", "nested gitlink", cwd=self.source)
        run_git("push", "origin", "main", cwd=self.source)
        for latest in (False, True):
            for use_cow in (False, True) if HAS_COW else (False,):
                with self.subTest(latest=latest, cow=use_cow):
                    item = self.store.create_workspace(
                        self.repo, f"nested-{latest}-{use_cow}", None, cow=use_cow, latest=latest
                    )
                    self.assertEqual(
                        b"nested LFS content\n",
                        (Path(item["path"]) / "lib/nested/asset.bin").read_bytes(),
                    )
