import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gitws.cli import main
from gitws.core import Repo, Store


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.store = Store(self.root / "store")
        self.repo = Repo("project", self.source, "https://example.test/repo.git",
                         "main", self.store.repos_dir / "project.git")

    def test_symlinks_count_the_link_and_never_follow_the_target(self):
        outside = self.root / "outside"
        outside.mkdir()
        payload = outside / "payload"
        payload.write_bytes(b"x" * 65536)
        links = [self.source / name for name in ("file", "directory", "missing")]
        links[0].symlink_to(payload)
        links[1].symlink_to(outside, target_is_directory=True)
        links[2].symlink_to(outside / "absent")

        measured = self.store.usage(self.repo)["original checkout"]
        self.assertEqual(sum(p.lstat().st_size for p in links), measured["logical"])
        self.assertEqual(sum(p.lstat().st_blocks * 512 for p in links), measured["physical"])

    def test_sparse_file_uses_allocated_blocks_even_when_zero(self):
        sparse = self.source / "sparse"
        with sparse.open("wb") as stream:
            stream.truncate(64 * 1024 * 1024)
        measured = self.store.usage(self.repo)["original checkout"]
        self.assertEqual(sparse.stat().st_size, measured["logical"])
        self.assertEqual(sparse.stat().st_blocks * 512, measured["physical"])

    def test_hardlinks_are_deduplicated_across_rows(self):
        self.repo.mirror.mkdir()
        obj = self.repo.mirror / "object"
        obj.write_bytes(b"object\n")
        os.link(obj, self.source / "copy")
        rows = self.store.usage(self.repo)
        self.assertEqual(obj.stat().st_blocks * 512, rows["shared Git objects"]["physical"])
        self.assertEqual(0, rows["original checkout"]["physical"])
        self.assertEqual(obj.stat().st_size, rows["original checkout"]["logical"])

    def test_git_directories_are_pruned_but_build_outputs_are_counted(self):
        git = self.source / "module" / ".git"
        git.mkdir(parents=True)
        (git / "objects").write_bytes(b"git data")
        build = self.source / "build"
        build.mkdir()
        output = build / "output"
        output.write_bytes(b"build output\n")
        scanned = []
        scandir = os.scandir

        def track(path):
            scanned.append(Path(path))
            return scandir(path)

        with patch("gitws.core.os.scandir", side_effect=track):
            measured = self.store.usage(self.repo)["original checkout"]
        self.assertEqual(output.stat().st_size, measured["logical"])
        self.assertNotIn(git, scanned)

    def test_cow_scans_one_base_and_each_write_layer_without_mounts(self):
        base = self.root / "base"
        base.mkdir()
        (base / "shared").write_bytes(b"shared\n")
        workspaces = []
        for name in ("one", "two"):
            upper = self.store.cow_dir / name / "upper"
            upper.mkdir(parents=True)
            (upper / "edited").write_bytes(name.encode())
            workspaces.append({"name": name, "mode": "cow", "base": str(base),
                               "path": str(self.root / "mounted" / name),
                               "upper": str(upper)})
        scanned = []
        scandir = os.scandir

        def track(path):
            scanned.append(Path(path))
            return scandir(path)

        with patch.object(self.store, "workspaces", return_value=workspaces), \
                patch("gitws.core.os.scandir", side_effect=track):
            rows = self.store.usage(self.repo)
        self.assertEqual(1, scanned.count(base))
        self.assertFalse(any(self.root / "mounted" in p.parents for p in scanned))
        self.assertEqual(7, rows["shared base:base"]["logical"])
        self.assertEqual(3, rows["workspace:one writes"]["logical"])
        self.assertEqual(3, rows["workspace:two writes"]["logical"])
        self.assertEqual(13, rows["total"]["logical"])

    def test_usage_iterator_yields_before_scanning_later_directories(self):
        iterator = self.store.iter_usage(self.repo)
        label, _ = next(iterator)
        self.assertEqual("shared Git objects", label)
        (self.source / "created-after-first-row").write_bytes(b"new\n")
        rows = dict(iterator)
        self.assertEqual(4, rows["original checkout"]["logical"])
        self.assertIn("total", rows)

    def test_cli_prints_and_flushes_each_row_before_next_scan(self):
        class Output(io.StringIO):
            def __init__(self):
                super().__init__()
                self.flushes = 0

            def flush(self):
                self.flushes += 1
                super().flush()

        output = Output()

        def rows(_):
            yield "shared Git objects", {"logical": 1024, "physical": 4096}
            self.assertIn("shared Git objects\tlogical=1024\tphysical=4096", output.getvalue())
            self.assertGreater(output.flushes, 0)
            yield "total", {"logical": 1024, "physical": 4096}

        with patch("gitws.cli.Store", return_value=self.store), \
                patch.object(self.store, "get", return_value=self.repo), \
                patch.object(self.store, "iter_usage", side_effect=rows), \
                patch("sys.stdout", output):
            main(["usage", "project", "--bytes"])
        self.assertIn("total\tlogical=1024\tphysical=4096", output.getvalue())
