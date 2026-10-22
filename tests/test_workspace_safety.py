from __future__ import annotations

import io
import shutil
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from gitws import cow
from gitws.cli import main
from gitws.errors import GitwsError
from gitws.git import run_git
from tests.support import HAS_COW, RepositoryTestCase


class WorkspaceSafetyTests(RepositoryTestCase):
    def test_remove_refuses_tracked_untracked_and_ignored_changes(self):
        for kind in ("tracked", "untracked", "ignored"):
            with self.subTest(kind=kind):
                target = Path(self.new(kind)["path"])
                filename = "file" if kind == "tracked" else "output"
                if kind == "ignored":
                    run_git("config", "core.excludesFile", str(self.root / "ignore"), cwd=target)
                    (self.root / "ignore").write_text("output\n")
                (target / filename).write_text("valuable data\n")
                with self.assertRaisesRegex(GitwsError, "--force"):
                    self.store.remove_workspace(kind)
                self.assertEqual("valuable data\n", (target / filename).read_text())
                self.assertTrue(self.store.record_path(kind).is_file())

    def test_remove_refuses_ignored_files_in_submodules(self):
        self.add_child()
        target = Path(self.new("task")["path"])
        child = target / "lib"
        ignore = self.root / "ignore"
        ignore.write_text("output\n")
        run_git("config", "core.excludesFile", str(ignore), cwd=child)
        (child / "output").write_text("valuable data\n")
        self.assertEqual("", run_git("status", "--porcelain", cwd=target))
        with self.assertRaisesRegex(GitwsError, "--force"):
            self.store.remove_workspace("task")
        self.assertTrue((child / "output").exists())

    def test_remove_refuses_files_hidden_by_index_flags(self):
        for flag in ("--assume-unchanged", "--skip-worktree"):
            with self.subTest(flag=flag):
                name = flag.removeprefix("--")
                target = Path(self.new(name)["path"])
                run_git("update-index", flag, "file", cwd=target)
                (target / "file").write_text("hidden edits\n")
                self.assertEqual("", run_git("status", "--porcelain", cwd=target))
                with self.assertRaisesRegex(GitwsError, "--force"):
                    self.store.remove_workspace(name)
                self.assertEqual("hidden edits\n", (target / "file").read_text())

    def test_remove_preserves_clean_submodule_with_private_commits(self):
        self.add_child()
        target = Path(self.new("task")["path"])
        child = target / "lib"
        run_git("switch", "-c", "child-task", cwd=child)
        commit = self.commit(child, "private commit\n")
        run_git("switch", "-c", "parent-task", cwd=target)
        run_git("commit", "-am", "record child commit", cwd=target)
        self.assertEqual("", run_git("status", "--porcelain", cwd=target))
        self.assertEqual("", run_git("status", "--porcelain", cwd=child))
        with self.assertRaisesRegex(GitwsError, "private Git objects"):
            self.store.remove_workspace("task")
        self.assertEqual(commit, run_git("rev-parse", "child-task", cwd=child))

    def test_force_remove_discards_only_the_selected_task(self):
        target = Path(self.new("task")["path"])
        other = Path(self.new("other")["path"])
        (target / "file").write_text("discard\n")
        (other / "file").write_text("keep\n")
        with redirect_stdout(io.StringIO()):
            main(["--home", str(self.store.root), "remove", "task", "--force"])
        self.assertFalse(target.exists())
        self.assertEqual("keep\n", (other / "file").read_text())
        self.assertEqual("initial\n", (self.source / "file").read_text())

    def test_path_rejects_missing_directory_without_printing_a_path(self):
        target = Path(self.new("task")["path"])
        shutil.rmtree(target)
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaisesRegex(SystemExit, "missing"):
            main(["--home", str(self.store.root), "path", "task"])
        self.assertEqual("", output.getvalue())

    @unittest.skipUnless(HAS_COW, "fuse-overlayfs is unavailable")
    def test_unmounted_cow_requires_mount_before_path_or_safe_removal(self):
        target = Path(self.new("task", cow=True)["path"])
        (target / "file").write_text("keep edits\n")
        cow.unmount(target)
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaisesRegex(SystemExit, "mount"):
            main(["--home", str(self.store.root), "path", "task"])
        self.assertEqual("", output.getvalue())
        with self.assertRaisesRegex(GitwsError, "mount"):
            self.store.remove_workspace("task")
        self.store.mount_workspace("task")
        with self.assertRaisesRegex(GitwsError, "--force"):
            self.store.remove_workspace("task")
        self.assertEqual("keep edits\n", (target / "file").read_text())
        self.assertTrue(self.store.is_mounted(target))
        self.store.remove_workspace("task", force=True)
        self.assertFalse(target.exists())

    @unittest.skipUnless(HAS_COW, "fuse-overlayfs is unavailable")
    def test_force_can_remove_an_unmounted_cow_workspace(self):
        target = Path(self.new("task", cow=True)["path"])
        (target / "file").write_text("discard\n")
        cow.unmount(target)
        self.store.remove_workspace("task", force=True)
        self.assertFalse(target.exists())
        self.assertFalse(self.store.record_path("task").exists())
