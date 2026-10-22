from __future__ import annotations

import shutil
from pathlib import Path

from gitws.git import run_git
from gitws.storage import repo_id
from tests.support import RepositoryTestCase


class SubmodulesTests(RepositoryTestCase):
    def test_local_submodule_new_detached_commit(self) -> None:
        self.add_child()
        first = self.new("a")
        child = self.source / "lib"
        run_git("switch", "--detach", cwd=child)
        commit = self.commit(child, "detached\n")
        run_git("commit", "-am", "update gitlink", cwd=self.source)
        second = self.new("b")
        self.assertEqual(commit, run_git("rev-parse", "HEAD", cwd=Path(second["path"]) / "lib"))
        self.assertEqual("initial\n", (Path(first["path"]) / "lib/file").read_text())

    def test_relative_submodule_url_for_local_and_remote_sources(self) -> None:
        self.add_child("../child")
        run_git("push", "origin", "main", cwd=self.source)
        remote = self.store.add("remote", self.remote.as_uri())
        for index, repo in enumerate((self.repo, remote)):
            item = self.new(f"task{index}", repo)
            self.assertEqual("initial\n", (Path(item["path"]) / "lib/file").read_text())

    def test_submodule_cache_survives_deleted_source_and_url(self) -> None:
        child = self.add_child()
        first = self.new("a")
        shutil.rmtree(self.source)
        shutil.rmtree(child)
        second = self.new("b")
        self.assertEqual(first["commit"], second["commit"])
        self.assertEqual("initial\n", (Path(second["path"]) / "lib/file").read_text())

    def test_usage_includes_submodule_objects_after_last_workspace_removed(self) -> None:
        self.add_child()
        self.new("a")
        self.store.remove_workspace("a")
        rows = self.store.usage(self.repo)
        self.assertTrue(any(key.startswith("shared submodule:") for key in rows))
        self.assertGreater(
            sum(
                value["physical"]
                for key, value in rows.items()
                if key.startswith("shared submodule:")
            ),
            0,
        )

    def test_nested_relative_submodules_survive_source_removal(self) -> None:
        child = self.add_child()
        leaf = self.make_repo("leaf")
        run_git("submodule", "add", str(leaf), "nested", cwd=child)
        run_git("config", "-f", ".gitmodules", "submodule.nested.url", "../leaf", cwd=child)
        run_git("commit", "-am", "nested child", cwd=child)
        local_child = self.source / "lib"
        run_git("fetch", "origin", "main", cwd=local_child)
        run_git("switch", "--detach", "FETCH_HEAD", cwd=local_child)
        run_git("submodule", "update", "--init", "--recursive", cwd=local_child)
        run_git("commit", "-am", "nested gitlink", cwd=self.source)
        self.new("a")
        for path in (self.source, child, leaf):
            shutil.rmtree(path)
        item = self.new("b")
        self.assertEqual("initial\n", (Path(item["path"]) / "lib/nested/file").read_text())
        self.store.remove_workspace("a")
        self.store.remove_workspace("b")

    def test_legacy_relative_submodule_cache_can_be_reused_offline(self) -> None:
        child = self.add_child("../child")
        self.store.sync_mirror(self.repo)
        legacy = self.store.repos_dir / f"{repo_id('../child')}.git"
        run_git("clone", "--mirror", str(child), str(legacy))
        shutil.rmtree(self.source)
        shutil.rmtree(child)
        item = self.new("a")
        self.assertEqual("initial\n", (Path(item["path"]) / "lib/file").read_text())

    def test_new_cache_index_preserves_caches_used_by_legacy_workspaces(self) -> None:
        self.add_child()
        other = self.make_repo("second-child")
        run_git("submodule", "add", str(other), "second", cwd=self.source)
        run_git("commit", "-am", "second child", cwd=self.source)
        self.new("a")
        index = self.store.root / "cache-index" / f"{self.repo.mirror.stem}.json"
        index.unlink()  # Legacy stores discover caches through alternates.
        self.assertEqual(2, len(self.store.submodule_caches(self.repo)))
        run_git("rm", "-f", "second", cwd=self.source)
        run_git("commit", "-am", "remove second child", cwd=self.source)
        self.new("b")
        self.assertEqual(2, len(self.store.submodule_caches(self.repo)))
        self.store.remove_workspace("a")
        self.store.remove_workspace("b")
        self.assertEqual(2, len(self.store.submodule_caches(self.repo)))
