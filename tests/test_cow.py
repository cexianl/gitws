from __future__ import annotations

import io
import json
import shutil
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from gitws import cow
from gitws.cli import main
from gitws.core import Store
from gitws.errors import GitwsError
from gitws.git import run_git
from tests.support import HAS_COW, RepositoryTestCase


class CowTests(RepositoryTestCase):
    @unittest.skipUnless(HAS_COW, "fuse-overlayfs is unavailable")
    def test_cow_same_remote_different_sources_can_be_removed(self) -> None:
        other = self.root / "other"
        run_git("clone", str(self.source), str(other))
        run_git("remote", "set-url", "origin", self.remote.as_uri(), cwd=other)
        second_repo = self.store.add("other", other)
        self.new("a", cow=True)
        self.new("b", second_repo, cow=True)
        self.store.remove_workspace("b")
        self.store.remove_workspace("a")
        self.assertEqual([], self.store.workspaces())

    @unittest.skipUnless(HAS_COW, "fuse-overlayfs is unavailable")
    def test_cow_space_path_mount_detection_and_restore_edits(self) -> None:
        self.store = Store(self.root / "store with spaces")
        self.repo = self.store.add("project", self.source)
        item = self.new("task", cow=True)
        target = Path(item["path"])
        self.assertTrue(self.store.is_mounted(target))
        (target / "file").write_text("edited\n")
        (target / "untracked").write_text("build output\n")
        run_git("add", "file", cwd=target)
        cow.unmount(target)
        with redirect_stdout(io.StringIO()):
            main(["--home", str(self.store.root), "mount", "task"])
        self.assertEqual("edited\n", (target / "file").read_text())
        self.assertEqual("build output\n", (target / "untracked").read_text())
        self.assertIn("file", run_git("diff", "--cached", "--name-only", cwd=target))
        self.store.remove_workspace("task", force=True)
        self.assertFalse(target.exists())

    @unittest.skipUnless(HAS_COW, "fuse-overlayfs is unavailable")
    def test_retained_base_is_reported_and_explicitly_cleaned(self) -> None:
        self.add_child()
        one = self.new("a", cow=True)
        two = self.new("b", cow=True)
        base = Path(one["base"])
        self.store.remove_workspace("a")
        # An unmounted workspace still owns its base and submodule metadata.
        cow.unmount(Path(two["path"]))
        self.assertEqual([], self.store.clean(self.repo, unused=True))
        self.assertTrue(base.is_dir())
        self.store.remove_workspace("b", force=True)
        self.assertTrue(base.is_dir())
        usage = self.store.usage(self.repo)
        self.assertTrue(any(key.startswith("shared base:") for key in usage))
        self.assertIn(base, self.store.clean(self.repo, unused=True, dry_run=True))
        self.assertTrue(base.is_dir())
        self.assertIn(base, self.store.clean(self.repo, unused=True))
        self.assertFalse(base.exists())
        self.assertFalse(any(key.startswith("shared base:") for key in self.store.usage(self.repo)))

    @unittest.skipUnless(HAS_COW, "fuse-overlayfs is unavailable")
    def test_warm_cow_does_not_rescan_local_lfs_objects(self) -> None:
        self.new("a", cow=True)
        with patch.object(
            self.store, "seed_local_lfs", side_effect=AssertionError("warm base rescanned LFS")
        ):
            self.new("b", cow=True)
        self.store.remove_workspace("a")
        self.store.remove_workspace("b")

    @unittest.skipUnless(HAS_COW, "fuse-overlayfs is unavailable")
    def test_legacy_cow_record_uses_actual_mirror_for_removal(self) -> None:
        item = self.new("a", cow=True)
        other = self.make_repo("other")
        wrong = self.store.add("wrong", other)
        self.store.sync_mirror(wrong)
        item.pop("mirror", None)
        item["repo"] = wrong.name
        self.store.record_path("a").write_text(json.dumps(item))
        self.store.remove_workspace("a")
        self.assertFalse(Path(item["path"]).exists())

    @unittest.skipUnless(HAS_COW, "fuse-overlayfs is unavailable")
    def test_mount_refuses_to_hide_files_written_while_unmounted(self) -> None:
        item = self.new("a", cow=True)
        target = Path(item["path"])
        cow.unmount(target)
        (target / "file").write_text("written while unmounted\n")
        with self.assertRaisesRegex(GitwsError, "unmounted"):
            self.store.mount_workspace("a")
        self.assertFalse(self.store.is_mounted(target))
        self.assertEqual("written while unmounted\n", (target / "file").read_text())

    @unittest.skipUnless(HAS_COW, "fuse-overlayfs is unavailable")
    def test_clean_preserves_base_referenced_by_another_alias(self) -> None:
        alias = self.store.add("alias", self.source)
        item = self.new("a", alias, cow=True)
        self.assertEqual([], self.store.clean(self.repo, unused=True))
        self.assertEqual("initial\n", (Path(item["path"]) / "file").read_text())
        self.store.remove_workspace("a")

    @unittest.skipUnless(HAS_COW, "fuse-overlayfs is unavailable")
    def test_relative_submodule_without_origin_reuses_cache_across_modes(self) -> None:
        run_git("remote", "remove", "origin", cwd=self.source)
        repo = self.store.add("no-origin", self.source)
        child = self.add_child("../child")
        self.new("a", repo)
        shutil.rmtree(self.source)
        shutil.rmtree(child)
        item = self.new("cow", repo, cow=True)
        self.assertEqual("initial\n", (Path(item["path"]) / "lib/file").read_text())
        self.store.remove_workspace("cow")

    @unittest.skipUnless(HAS_COW, "fuse-overlayfs is unavailable")
    def test_clean_preserves_submodule_task_commits(self) -> None:
        self.add_child()
        item = self.new("a", cow=True)
        child = Path(item["path"]) / "lib"
        run_git("switch", "-c", "task", cwd=child)
        commit = self.commit(child, "task commit\n")
        run_git("commit", "-am", "record child commit", cwd=Path(item["path"]))
        self.store.remove_workspace("a")
        self.assertEqual([], self.store.clean(self.repo, unused=True))
        self.assertEqual(commit, run_git("rev-parse", "task", cwd=Path(item["base"]) / "lib"))
