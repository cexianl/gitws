import json
import os
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from gitws.core import GitwsError, Store, run_git
from gitws.cli import human_size


class GitwsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.git_config = self.root / "gitconfig"
        self.git_config.write_text('[protocol "file"]\n\tallow = always\n')
        self.old_git_config = os.environ.get("GIT_CONFIG_GLOBAL")
        os.environ["GIT_CONFIG_GLOBAL"] = str(self.git_config)
        self.remote = self.root / "remote.git"
        self.source = self.root / "source"
        run_git("init", "--bare", str(self.remote))
        run_git("init", "-b", "main", str(self.source))
        run_git("config", "user.email", "test@example.com", cwd=self.source)
        run_git("config", "user.name", "Test", cwd=self.source)
        (self.source / "README").write_text("one\n")
        run_git("add", "README", cwd=self.source)
        run_git("commit", "-m", "one", cwd=self.source)
        run_git("remote", "add", "origin", str(self.remote), cwd=self.source)
        run_git("push", "-u", "origin", "main", cwd=self.source)
        self.store = Store(self.root / "state")

    def tearDown(self):
        if self.old_git_config is None:
            os.environ.pop("GIT_CONFIG_GLOBAL", None)
        else:
            os.environ["GIT_CONFIG_GLOBAL"] = self.old_git_config
        self.tmp.cleanup()

    def test_add_sync_and_new_record_fixed_commit(self):
        repo = self.store.add("demo", self.source)
        first = run_git("rev-parse", "HEAD", cwd=self.source)
        item = self.store.create_workspace(repo, "a", first)
        self.assertEqual(first, item["commit"])
        self.assertTrue(Path(item["path"]).is_dir())
        self.assertEqual(first, json.loads((self.store.records_dir / "a.json").read_text())["commit"])

    def test_dirty_source_rejected_by_clean_guard(self):
        repo = self.store.add("demo", self.source)
        (self.source / "README").write_text("dirty\n")
        with self.assertRaises(GitwsError):
            from gitws.core import ensure_clean
            ensure_clean(repo.path)

    def test_size_reports_total_and_workspace(self):
        repo = self.store.add("demo", self.source)
        first = run_git("rev-parse", "HEAD", cwd=self.source)
        self.store.create_workspace(repo, "a", first)
        result = self.store.usage(repo)
        self.assertIn("total", result)
        self.assertIn("workspace:a", result)
        self.assertGreater(result["total"]["logical"], 0)

    def test_doctor_reports_healthy_workspace(self):
        repo = self.store.add("demo", self.source)
        self.store.create_workspace(repo, "doctor-a", None)
        self.assertEqual(self.store.doctor(repo), [])

    def test_parallel_workspace_creation_is_serialized(self):
        repo = self.store.add("demo", self.source)
        first = run_git("rev-parse", "HEAD", cwd=self.source)
        with ThreadPoolExecutor(max_workers=2) as pool:
            items = list(pool.map(
                lambda name: self.store.create_workspace(repo, name, first),
                ("parallel-a", "parallel-b"),
            ))
        self.assertEqual({item["name"] for item in items}, {"parallel-a", "parallel-b"})

    def test_human_size(self):
        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(1024 * 1024), "1.00 MiB")

    def test_submodule_objects_use_shared_reference_mirror(self):
        sub_remote = self.root / "sub-remote.git"
        sub_source = self.root / "sub-source"
        run_git("init", "--bare", str(sub_remote))
        run_git("init", "-b", "main", str(sub_source))
        run_git("config", "user.email", "test@example.com", cwd=sub_source)
        run_git("config", "user.name", "Test", cwd=sub_source)
        (sub_source / "lib.txt").write_text("shared\n")
        run_git("add", "lib.txt", cwd=sub_source)
        run_git("commit", "-m", "submodule", cwd=sub_source)
        run_git("remote", "add", "origin", sub_remote.as_uri(), cwd=sub_source)
        run_git("push", "-u", "origin", "main", cwd=sub_source)
        run_git("symbolic-ref", "HEAD", "refs/heads/main", cwd=sub_remote)

        main_remote = self.root / "main-remote.git"
        main_source = self.root / "main-source"
        run_git("init", "--bare", str(main_remote))
        run_git("init", "-b", "main", str(main_source))
        run_git("config", "user.email", "test@example.com", cwd=main_source)
        run_git("config", "user.name", "Test", cwd=main_source)
        (main_source / "README").write_text("main\n")
        run_git("add", "README", cwd=main_source)
        run_git("commit", "-m", "main", cwd=main_source)
        run_git("-c", "protocol.file.allow=always", "submodule", "add",
                sub_remote.as_uri(), "modules/lib", cwd=main_source)
        run_git("commit", "-am", "add submodule", cwd=main_source)
        run_git("remote", "add", "origin", main_remote.as_uri(), cwd=main_source)
        run_git("push", "-u", "origin", "main", cwd=main_source)
        run_git("symbolic-ref", "HEAD", "refs/heads/main", cwd=main_remote)

        repo = self.store.add("nested", main_source)
        first = self.store.create_workspace(repo, "one", None)
        second = self.store.create_workspace(repo, "two", first["commit"])
        mirrors = list(self.store.repos_dir.glob("*.git"))
        sub_mirror = next(path for path in mirrors if path != repo.mirror)

        alternates = []
        for item in (first, second):
            submodule_path = Path(item["path"]) / "modules/lib"
            git_path = Path(run_git("rev-parse", "--git-path", "objects", cwd=submodule_path))
            if not git_path.is_absolute():
                git_path = (submodule_path / git_path).resolve()
            alternates_path = git_path / "info" / "alternates"
            alternates.append(alternates_path.read_text().strip())
        self.assertEqual(alternates[0], alternates[1])
        self.assertIn(str(sub_mirror / "objects"), alternates[0])

        cow = self.store.create_workspace(repo, "cow-submodule", first["commit"], cow=True)
        try:
            submodule_path = Path(cow["path"]) / "modules/lib"
            self.assertEqual("shared\n", (submodule_path / "lib.txt").read_text())
            (submodule_path / "lib.txt").write_text("changed\n")
            self.assertEqual("shared\n", (Path(first["path"]) / "modules/lib/lib.txt").read_text())
        finally:
            self.store.remove_workspace("cow-submodule")

    def test_workspaces_share_lfs_storage_and_plain_repo_still_works(self):
        repo = self.store.add("demo", self.source)
        first = self.store.create_workspace(repo, "lfs-one", None)
        second = self.store.create_workspace(repo, "lfs-two", first["commit"])

        expected = str(self.store.lfs_path(repo))
        for item in (first, second):
            workspace = Path(item["path"])
            self.assertEqual(expected, run_git("config", "--get", "lfs.storage", cwd=workspace))

        self.assertTrue(Path(expected).is_dir())

    @unittest.skipUnless(shutil.which("fuse-overlayfs") or os.geteuid() == 0,
                         "overlayfs or fuse-overlayfs is unavailable")
    def test_cow_workspace_has_full_tree_and_isolated_changes(self):
        repo = self.store.add("demo", self.source)
        first = self.store.create_workspace(repo, "cow-one", None, cow=True)
        second = self.store.create_workspace(repo, "cow-two", first["commit"], cow=True)
        first_path = Path(first["path"])
        second_path = Path(second["path"])

        self.assertEqual("one\n", (second_path / "README").read_text())
        (first_path / "README").write_text("changed\n")
        self.assertEqual("changed\n", (first_path / "README").read_text())
        self.assertEqual("one\n", (second_path / "README").read_text())
        self.assertTrue(run_git("status", "--porcelain", cwd=first_path))
        usage = self.store.usage(repo)
        self.assertTrue(any(key.startswith("shared base:") for key in usage))
        self.assertIn("workspace:cow-one writes", usage)
        self.store.remove_workspace("cow-one")
        self.store.remove_workspace("cow-two")

if __name__ == "__main__":
    unittest.main()
