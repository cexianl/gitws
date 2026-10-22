from __future__ import annotations

import unittest
from pathlib import Path

from gitws.git import common_git_dir, run_git
from tests.support import HAS_COW, RepositoryTestCase


class SubmoduleUpdateTests(RepositoryTestCase):
    def prepare_nested(self) -> tuple[Path, Path]:
        child = self.add_child()
        leaf = self.make_repo("leaf")
        run_git("submodule", "add", str(leaf), "nested", cwd=child)
        run_git("commit", "-am", "add nested", cwd=child)
        local_child = self.source / "lib"
        run_git("fetch", "origin", "main", cwd=local_child)
        run_git("checkout", "--detach", "FETCH_HEAD", cwd=local_child)
        run_git("submodule", "update", "--init", "--recursive", cwd=local_child)
        run_git("commit", "-am", "pin nested", cwd=self.source)
        return child, leaf

    def check_update_isolation(self, *, cow: bool) -> None:
        child, leaf = self.prepare_nested()
        first = self.new("a", cow=cow)
        a = Path(first["path"])
        b = Path(self.new("b", cow=cow)["path"])
        untouched = [a, self.source]
        if cow:
            untouched.append(Path(first["base"]))
        expected = {
            relative: run_git("rev-parse", "HEAD", cwd=a / relative)
            for relative in ("lib", "lib/nested")
        }
        for relative in expected:
            self.assertEqual(
                str(b / relative), run_git("rev-parse", "--show-toplevel", cwd=b / relative)
            )
            for args in (
                ("--absolute-git-dir",),
                ("--path-format=absolute", "--git-path", "index"),
            ):
                self.assertNotEqual(
                    run_git("rev-parse", *args, cwd=a / relative),
                    run_git("rev-parse", *args, cwd=b / relative),
                )
        (a / "lib/file").write_text("task a private edit\n")
        run_git("add", "file", cwd=a / "lib")

        new_leaf = self.commit(leaf, "leaf updated\n")
        run_git("submodule", "update", "--remote", "--recursive", cwd=child)
        run_git("commit", "-am", "pin new leaf", cwd=child)
        new_child = run_git("rev-parse", "HEAD", cwd=child)

        # Plain update follows the gitlinks, even when upstream has newer commits.
        run_git("submodule", "update", "--init", "--recursive", cwd=b)
        for root in [b, *untouched]:
            for relative, commit in expected.items():
                self.assertEqual(
                    commit, run_git("rev-parse", "HEAD", cwd=root / relative, check=False)
                )

        run_git("submodule", "sync", "--recursive", cwd=b)
        run_git("submodule", "update", "--init", "--remote", "--recursive", cwd=b)
        self.assertEqual(new_child, run_git("rev-parse", "HEAD", cwd=b / "lib"))
        self.assertEqual(new_leaf, run_git("rev-parse", "HEAD", cwd=b / "lib/nested"))
        self.assertEqual("leaf updated\n", (b / "lib/nested/file").read_text())
        for root in untouched:
            for relative, commit in expected.items():
                self.assertEqual(commit, run_git("rev-parse", "HEAD", cwd=root / relative))
        self.assertEqual("task a private edit\n", (a / "lib/file").read_text())
        self.assertEqual("file", run_git("diff", "--cached", "--name-only", cwd=a / "lib"))
        self.assertFalse(run_git("diff", "--cached", "--name-only", cwd=b / "lib"))

        # Restore B and reuse the same base for a third task.
        run_git("submodule", "update", "--init", "--recursive", cwd=b)
        c = Path(self.new("c", cow=cow)["path"])
        for root in (b, c):
            for relative, commit in expected.items():
                self.assertEqual(commit, run_git("rev-parse", "HEAD", cwd=root / relative))
        for name in ("a", "b", "c"):
            self.store.remove_workspace(name, force=True)

    def test_ordinary_recursive_update_is_isolated(self) -> None:
        self.check_update_isolation(cow=False)

    @unittest.skipUnless(HAS_COW, "fuse-overlayfs is unavailable")
    def test_cow_recursive_update_is_isolated(self) -> None:
        self.check_update_isolation(cow=True)

    @unittest.skipUnless(HAS_COW, "fuse-overlayfs is unavailable")
    def test_cow_reused_legacy_base_supports_submodule_update(self) -> None:
        self.add_child()
        first = self.new("a", cow=True)
        base_child = Path(first["base"]) / "lib"
        config = common_git_dir(base_child) / "config"
        # Reproduce the configuration layout used before worktreeConfig support.
        run_git("config", "--file", str(config), "core.worktree", str(base_child))
        run_git(
            "config", "--file", str(config), "--unset", "extensions.worktreeConfig", check=False
        )
        b = Path(self.new("b", cow=True)["path"])
        run_git("submodule", "update", "--init", "--recursive", cwd=b)
        self.assertEqual(
            str(base_child),
            run_git("rev-parse", "--show-toplevel", cwd=base_child, check=False),
        )
        self.assertEqual("initial\n", (Path(first["path"]) / "lib/file").read_text())
