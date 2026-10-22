from __future__ import annotations

from pathlib import Path

from gitws.errors import GitwsError
from gitws.git import run_git
from tests.support import RepositoryTestCase


class IsolationTests(RepositoryTestCase):
    def test_new_preserves_task_branch_and_commit_for_local_and_remote(self) -> None:
        remote_repo = self.store.add("remote", self.remote.as_uri())
        for index, repo in enumerate((self.repo, remote_repo)):
            first = Path(self.new(f"a{index}", repo)["path"])
            run_git("switch", "-c", "task", cwd=first)
            commit = self.commit(first)
            self.new(f"b{index}", repo)
            self.assertEqual(commit, run_git("rev-parse", "HEAD", cwd=first))
            self.assertEqual("task", run_git("branch", "--show-current", cwd=first))

    def test_source_branch_checked_out_does_not_block_new(self) -> None:
        first = Path(self.new("a")["path"])
        run_git("switch", "main", cwd=first)
        original = run_git("rev-parse", "HEAD", cwd=first)
        updated = self.commit(self.source)
        self.assertEqual(updated, self.new("b")["commit"])
        self.assertEqual(original, run_git("rev-parse", "HEAD", cwd=first))
        named = self.store.create_workspace(self.repo, "named", "main")
        self.assertEqual(updated, named["commit"])

    def test_push_targets_upstream_without_changing_source_refs(self) -> None:
        source_refs = run_git("show-ref", cwd=self.source)
        first = Path(self.new("a")["path"])
        run_git("switch", "-c", "task", cwd=first)
        commit = self.commit(first)
        run_git("push", "-u", "origin", "task", cwd=first)
        self.assertEqual(commit, run_git("rev-parse", "task", cwd=self.remote))
        self.assertEqual(source_refs, run_git("show-ref", cwd=self.source))
        self.assertFalse(run_git("for-each-ref", "refs/gitws", cwd=self.remote))

    def test_existing_mirror_config_is_migrated_without_pruning_task(self) -> None:
        run_git("clone", "--mirror", str(self.source), str(self.repo.mirror))
        run_git("branch", "task", cwd=self.repo.mirror)
        self.new("a")
        self.assertTrue(run_git("rev-parse", "refs/heads/task", cwd=self.repo.mirror))
        self.assertEqual(
            self.remote.as_uri(), run_git("remote", "get-url", "origin", cwd=self.repo.mirror)
        )
        self.assertNotEqual(
            "true",
            run_git("config", "--get", "remote.origin.mirror", cwd=self.repo.mirror, check=False),
        )

    def test_alias_cannot_silently_change_source(self) -> None:
        self.new("a")
        with self.assertRaises(GitwsError):
            self.store.add("project", self.make_repo("other"))
        self.assertEqual(self.source, self.store.get("project").path)
        self.assertEqual(self.repo.mirror, self.store.add("project", self.source).mirror)
        self.store.remove_workspace("a")

    def test_doctor_accepts_normal_task_commits(self) -> None:
        first = Path(self.new("a")["path"])
        self.commit(first)
        self.assertEqual([], self.store.doctor(self.repo))

    def test_local_source_without_origin_has_no_push_remote(self) -> None:
        source = self.make_repo("no-origin")
        repo = self.store.add("no-origin", source)
        item = self.new("no-origin", repo)
        self.assertFalse(run_git("remote", cwd=Path(item["path"])))

    def test_relative_local_origin_pushes_to_original_remote_location(self) -> None:
        run_git("remote", "set-url", "origin", "../remote.git", cwd=self.source)
        repo = self.store.add("relative-origin", self.source)
        first = Path(self.new("a", repo)["path"])
        run_git("switch", "-c", "task", cwd=first)
        commit = self.commit(first)
        run_git("push", "origin", "task", cwd=first)
        self.assertEqual(commit, run_git("rev-parse", "task", cwd=self.remote))

    def test_new_branch_revision_expression_resolves_before_fetch(self) -> None:
        self.new("a")
        before = run_git("rev-parse", "HEAD", cwd=self.source)
        run_git("switch", "-c", "new-branch", cwd=self.source)
        self.commit(self.source)
        item = self.store.create_workspace(self.repo, "b", "new-branch~1")
        self.assertEqual(before, item["commit"])
