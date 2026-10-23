from __future__ import annotations

import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from gitws import checkout, cow, lfs
from gitws.errors import GitwsError
from gitws.git import run_git
from gitws.storage import write_json
from tests.support import HAS_COW, RepositoryTestCase


class SharedBasesTests(RepositoryTestCase):
    def base(self) -> Path:
        self.store.sync_mirror(self.repo)
        commit = run_git("rev-parse", "HEAD", cwd=self.source)
        return self.store._ensure_base(self.repo, commit, lfs.environment())

    def add_file(self, name: str, data: bytes) -> None:
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        run_git("add", "--", name, cwd=self.source)
        run_git("commit", "-m", "add file", cwd=self.source)

    def test_distinct_commits_share_files_without_linking_original_checkout(self):
        self.add_file("large", b"unchanged\n" * 8192)
        first = self.base()
        self.commit(self.source)
        second = self.base()
        self.assertNotEqual(first, second)
        self.assertTrue((first / "large").samefile(second / "large"))
        self.assertFalse((self.source / "large").samefile(first / "large"))
        self.assertEqual("initial\n", (first / "file").read_text())
        self.assertEqual("changed\n", (second / "file").read_text())
        self.assertFalse((first / ".git").samefile(second / ".git"))
        self.assertEqual("", run_git("status", "--porcelain", cwd=second))

    def test_identical_files_in_one_base_keep_distinct_inodes(self):
        self.add_file("duplicate-a", b"same content\n")
        self.add_file("duplicate-b", b"same content\n")
        first = self.base()
        self.commit(self.source)
        second = self.base()
        for base in (first, second):
            self.assertFalse((base / "duplicate-a").samefile(base / "duplicate-b"))
        for name in ("duplicate-a", "duplicate-b"):
            self.assertTrue((first / name).samefile(second / name))

    def test_modes_symlinks_and_checkout_attributes_are_preserved(self):
        self.add_file(".gitattributes", b"line text eol=crlf\n")
        self.add_file("line", b"hello\n")
        self.add_file("script", b"#!/bin/sh\n")
        (self.source / "link").symlink_to("line")
        run_git("add", "link", cwd=self.source)
        run_git("commit", "-m", "link", cwd=self.source)
        first = self.base()
        (self.source / ".gitattributes").write_text("line text eol=lf\n")
        (self.source / "script").chmod(0o755)
        run_git("add", ".", cwd=self.source)
        run_git("commit", "-m", "attributes", cwd=self.source)
        second = self.base()
        self.assertEqual(b"hello\r\n", (first / "line").read_bytes())
        self.assertEqual(b"hello\n", (second / "line").read_bytes())
        self.assertFalse((first / "line").samefile(second / "line"))
        self.assertFalse((first / "script").samefile(second / "script"))
        self.assertEqual(0o755, (second / "script").stat().st_mode & 0o777)
        self.assertEqual("line", os.readlink(second / "link"))
        self.assertEqual("", run_git("status", "--porcelain", cwd=second))

    def test_submodule_files_are_shared_across_parent_commits(self):
        self.add_child()
        first = self.base()
        self.commit(self.source)
        second = self.base()
        self.assertTrue((first / "lib/file").samefile(second / "lib/file"))
        self.assertFalse((first / "lib/.git").samefile(second / "lib/.git"))
        self.assertEqual("", run_git("status", "--porcelain", cwd=second))

    @unittest.skipUnless(shutil.which("git-lfs"), "git-lfs is unavailable")
    def test_materialized_lfs_content_is_shared_without_linking_lfs_cache(self):
        run_git("lfs", "install", "--local", cwd=self.source)
        run_git("lfs", "track", "*.bin", cwd=self.source)
        self.add_file("asset.bin", b"large asset\n" * 8192)
        run_git("add", ".gitattributes", cwd=self.source)
        run_git("commit", "-m", "attributes", cwd=self.source)
        first = self.base()
        self.commit(self.source)
        second = self.base()
        self.assertTrue((first / "asset.bin").samefile(second / "asset.bin"))
        self.assertEqual(b"large asset\n" * 8192, (second / "asset.bin").read_bytes())
        oid = run_git("lfs", "ls-files", "--long", cwd=self.source).split()[0]
        cached = self.store.lfs_path(self.repo) / "objects" / oid[:2] / oid[2:4] / oid
        self.assertFalse(cached.samefile(first / "asset.bin"))

    def test_new_base_does_not_reuse_or_modify_legacy_base(self):
        self.store.sync_mirror(self.repo)
        commit = run_git("rev-parse", "HEAD", cwd=self.source)
        legacy = self.store.bases_dir / self.repo.mirror.stem / commit
        legacy.parent.mkdir(parents=True)
        run_git("worktree", "add", "--detach", str(legacy), commit, cwd=self.repo.mirror)
        (legacy / "file").write_text("legacy content must survive\n")
        before = (legacy / "file").stat()
        new = self.base()
        self.assertNotEqual(legacy, new)
        self.assertEqual("initial\n", (new / "file").read_text())
        self.assertEqual("legacy content must survive\n", (legacy / "file").read_text())
        self.assertEqual(before, (legacy / "file").stat())
        self.assertIn(legacy, self.store.retained_bases(self.repo))

    def test_cleaning_one_base_preserves_shared_files_in_another(self):
        self.add_file("large", b"unchanged\n" * 8192)
        first = self.base()
        self.commit(self.source)
        second = self.base()
        # Keep the second base referenced without requiring a real mount.
        write_json(
            self.store.record_path("kept"),
            {
                "name": "kept",
                "repo": self.repo.name,
                "mode": "cow",
                "base": str(second),
                "path": str(self.store.workspaces_dir / "kept"),
            },
        )
        self.assertTrue((first / "large").samefile(second / "large"))
        self.assertIn(first, self.store.clean(self.repo, unused=True))
        self.assertEqual(b"unchanged\n" * 8192, (second / "large").read_bytes())
        self.assertEqual("", run_git("status", "--porcelain", cwd=second))

    def test_sharing_uses_bounded_checkout_batches(self):
        for index in range(5):
            self.add_file(f"large-{index}", bytes([index]) * 4096)
        first = self.base()
        self.commit(self.source)
        git_input = checkout._git_input
        batches = []

        def observe(path, args, data, env):
            if "checkout-index" in args:
                batches.append(data)
                # Before each next batch, unchanged files from previous batches
                # must already be shared instead of accumulating a second tree.
                own = sum(
                    file.stat().st_size
                    for file in path.glob("large-*")
                    if file.stat().st_nlink == 1
                )
                self.assertEqual(0, own)
            return git_input(path, args, data, env)

        with (
            patch.object(checkout, "BATCH_BYTES", 8192),
            patch.object(checkout, "_git_input", side_effect=observe),
        ):
            second = self.base()
        self.assertGreater(len(batches), 1)
        for index in range(5):
            self.assertTrue((first / f"large-{index}").samefile(second / f"large-{index}"))

    def test_failed_build_leaves_published_base_and_no_partial_base(self):
        self.add_file("large", b"keep me\n")
        first = self.base()
        commit = self.commit(self.source)
        original = checkout.SharedCheckout._batch

        def fail(builder, *args):
            original(builder, *args)
            raise GitwsError("interrupted build")

        with patch.object(checkout.SharedCheckout, "_batch", fail):
            with self.assertRaisesRegex(GitwsError, "interrupted build"):
                self.base()
        second = self.store.base_path(self.repo, commit)
        self.assertFalse(second.exists())
        self.assertFalse(checkout.manifest_path(second).exists())
        self.assertEqual(b"keep me\n", (first / "large").read_bytes())
        self.assertEqual(1, (first / "large").stat().st_nlink)
        self.assertEqual([first], self.store.retained_bases(self.repo))

    def test_incomplete_base_is_not_repaired_in_place(self):
        first = self.base()
        checkout.manifest_path(first).unlink()
        (first / "file").write_text("must not overwrite\n")
        with self.assertRaisesRegex(GitwsError, "incomplete"):
            self.base()
        self.assertEqual("must not overwrite\n", (first / "file").read_text())

    def test_invalid_manifest_is_reported_without_changing_existing_base(self):
        first = self.base()
        checkout.manifest_path(first).write_text("[]\n")
        with self.assertRaisesRegex(GitwsError, "manifest"):
            self.base()
        self.assertEqual("initial\n", (first / "file").read_text())

    @unittest.skipUnless(shutil.which("git-lfs"), "git-lfs is unavailable")
    def test_missing_local_lfs_object_fails_before_smudge_or_network_access(self):
        run_git("lfs", "install", "--local", cwd=self.source)
        run_git("lfs", "track", "*.bin", cwd=self.source)
        self.add_file("asset.bin", b"local asset\n")
        run_git("add", ".gitattributes", cwd=self.source)
        run_git("commit", "-m", "attributes", cwd=self.source)
        shutil.rmtree(self.source / ".git/lfs/objects")
        with patch.object(
            checkout.SharedCheckout, "checkout", side_effect=AssertionError("smudge started")
        ):
            with self.assertRaisesRegex(GitwsError, "local Git LFS object is missing"):
                self.base()
        self.assertEqual([], self.store.retained_bases(self.repo))

    def test_modified_candidate_is_not_linked_into_new_base(self):
        self.add_file("unchanged", b"original\n")
        first = self.base()
        (first / "unchanged").write_bytes(b"tampered\n")
        self.commit(self.source)
        second = self.base()
        self.assertEqual(b"original\n", (second / "unchanged").read_bytes())
        self.assertFalse((first / "unchanged").samefile(second / "unchanged"))

    def test_unavailable_hardlinks_preserve_correct_file_content(self):
        self.add_file("shared", b"original\n")
        first = self.base()
        self.commit(self.source)
        with patch("gitws.checkout.os.link", side_effect=OSError("hardlinks unavailable")):
            second = self.base()
        self.assertEqual(b"original\n", (second / "shared").read_bytes())
        self.assertFalse((first / "shared").samefile(second / "shared"))

    def test_checkout_handles_unusual_filenames_and_custom_filters(self):
        run_git("config", "--global", "filter.demo.smudge", "sed s/raw/cooked/g")
        run_git("config", "--global", "filter.demo.clean", "sed s/cooked/raw/g")
        self.add_file(".gitattributes", b"*.filtered filter=demo\n")
        self.add_file("value.filtered", b"raw\n")
        names = [
            "with space",
            "with\nnewline",
            "with\ttab",
            "carriage\rreturn",
            os.fsdecode(b"invalid-\xff"),
            "--option",
            "x.gitws-link",
        ]
        for name in names:
            self.add_file(name, os.fsencode(name))
        first = self.base()
        self.commit(self.source)
        second = self.base()
        self.assertEqual(b"cooked\n", (second / "value.filtered").read_bytes())
        for name in [*names, "value.filtered"]:
            self.assertTrue((first / name).samefile(second / name))
        self.assertEqual("", run_git("status", "--porcelain", cwd=second))

    def test_usage_counts_shared_sources_once_and_reports_reclaimable_bytes(self):
        self.add_file("large", b"same\n" * 8192)
        first = self.base()
        self.commit(self.source)
        second = self.base()
        rows = self.store.usage(self.repo)
        a, b = (rows[f"shared base:{base.name}"] for base in (first, second))
        for row in (a, b):
            self.assertGreaterEqual(row["shared"], (first / "large").stat().st_blocks * 512)
            self.assertEqual(row["exclusive"], row["reclaimable"])
        self.assertEqual(
            a["exclusive"] + b["exclusive"] + a["shared"], a["physical"] + b["physical"]
        )
        self.assertIn("base metadata", rows)

    @unittest.skipUnless(HAS_COW, "fuse-overlayfs is unavailable")
    def test_legacy_mounted_task_survives_new_base_creation_and_cleanup(self):
        self.store.sync_mirror(self.repo)
        commit = run_git("rev-parse", "HEAD", cwd=self.source)
        legacy = self.store.bases_dir / self.repo.mirror.stem / commit
        legacy.parent.mkdir(parents=True)
        run_git("worktree", "add", "--detach", str(legacy), commit, cwd=self.repo.mirror)
        with patch.object(self.store, "_ensure_base", return_value=legacy):
            old = self.new("old", cow=True)
        target = Path(old["path"])
        (target / "file").write_text("existing task edits\n")
        run_git("add", "file", cwd=target)
        before = self.store.record_path("old").read_bytes()
        new = self.new("new", cow=True)
        self.assertNotEqual(old["base"], new["base"])
        self.assertEqual(before, self.store.record_path("old").read_bytes())
        self.assertEqual("existing task edits\n", (target / "file").read_text())
        self.assertEqual("file", run_git("diff", "--cached", "--name-only", cwd=target))
        rows = self.store.usage(self.repo)
        self.assertIn(f"shared base:{commit} (legacy)", rows)
        self.assertEqual(0, rows[f"shared base:{commit}"]["reclaimable"])
        self.store.remove_workspace("new")
        self.store.clean(self.repo, unused=True)
        cow.unmount(target)
        self.store.mount_workspace("old")
        self.assertEqual("existing task edits\n", (target / "file").read_text())

    @unittest.skipUnless(HAS_COW, "fuse-overlayfs is unavailable")
    def test_unusual_filenames_survive_workspace_and_submodule_checkout(self):
        self.add_child()
        name = os.fsdecode(b"carriage\r-invalid-\xff")
        self.add_file(name, b"main source\n")
        child = self.source / "lib"
        (child / name).write_bytes(b"child source\n")
        run_git("add", "--", name, cwd=child)
        run_git("commit", "-m", "unusual filename", cwd=child)
        run_git("commit", "-am", "pin child", cwd=self.source)
        item = self.new("unusual", cow=True)
        target = Path(item["path"])
        self.assertEqual(b"main source\n", (target / name).read_bytes())
        self.assertEqual(b"child source\n", (target / "lib" / name).read_bytes())
        self.assertEqual("", run_git("status", "--porcelain", cwd=target))

    @unittest.skipUnless(HAS_COW, "fuse-overlayfs is unavailable")
    def test_task_writes_and_remount_preserve_other_versions(self):
        self.add_child()
        self.add_file("shared", b"shared content\n")
        self.add_file("equal", b"shared content\n")
        a = self.new("a", cow=True)
        self.commit(self.source)
        b = self.new("b", cow=True)
        first, second = Path(a["path"]), Path(b["path"])
        self.assertTrue((Path(a["base"]) / "shared").samefile(Path(b["base"]) / "shared"))
        self.assertTrue((Path(a["base"]) / "lib/file").samefile(Path(b["base"]) / "lib/file"))
        (first / "shared").write_text("private edit\n")
        (first / "lib/file").write_text("private child edit\n")
        (first / "shared").chmod(0o755)
        run_git("add", "shared", cwd=first)
        self.assertEqual(b"shared content\n", (first / "equal").read_bytes())
        self.assertEqual(b"shared content\n", (second / "shared").read_bytes())
        self.assertEqual(b"shared content\n", (Path(a["base"]) / "shared").read_bytes())
        self.assertEqual("initial\n", (second / "lib/file").read_text())
        self.assertEqual("initial\n", (Path(a["base"]) / "lib/file").read_text())
        cow.unmount(first)
        self.store.mount_workspace("a")
        self.assertEqual("private edit\n", (first / "shared").read_text())
        self.assertEqual("private child edit\n", (first / "lib/file").read_text())
        self.assertIn("shared", run_git("diff", "--cached", "--name-only", cwd=first))
        self.store.remove_workspace("a", force=True)
        self.store.clean(self.repo, unused=True)
        self.assertEqual(b"shared content\n", (second / "shared").read_bytes())
