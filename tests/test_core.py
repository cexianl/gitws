import json
import tempfile
import unittest
from pathlib import Path

from gitws.core import GitwsError, Store, run_git
from gitws.cli import human_size


class GitwsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
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
        result = self.store.size(repo)
        self.assertIn("total", result)
        self.assertIn("workspace:a", result)
        self.assertGreater(result["total"]["logical"], 0)

    def test_human_size(self):
        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(1024 * 1024), "1.00 MiB")

if __name__ == "__main__":
    unittest.main()
