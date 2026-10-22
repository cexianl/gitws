from __future__ import annotations

import subprocess
import sys

from tests.support import RepositoryTestCase


class ConcurrencyTests(RepositoryTestCase):
    def test_multiple_cli_processes_create_distinct_workspaces(self) -> None:
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "gitws",
                    "--home",
                    str(self.store.root),
                    "new",
                    "project",
                    name,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for name in ("a", "b", "same", "same")
        ]
        try:
            results = [process.communicate(timeout=20) for process in processes]
            self.assertEqual([0, 0], [process.returncode for process in processes[:2]], results)
            self.assertEqual(
                [0, 1], sorted(process.returncode for process in processes[2:]), results
            )
            self.assertEqual({"a", "b", "same"}, {item["name"] for item in self.store.workspaces()})
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate()

    def test_slow_creation_does_not_block_another_project(self) -> None:
        self.store.add("other", self.make_repo("other"))
        code = "\nimport sys\nfrom pathlib import Path\nfrom gitws.core import Store\nclass PausedStore(Store):\n    def sync_mirror(self, repo):\n        print('locked', flush=True)\n        sys.stdin.read(1)\n        super().sync_mirror(repo)\nstore = PausedStore(Path(sys.argv[1]))\nstore.create_workspace(store.get('project'), 'slow', None)\n"
        slow = subprocess.Popen(
            [sys.executable, "-c", code, str(self.store.root)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual("locked\n", slow.stdout.readline())
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "gitws",
                    "--home",
                    str(self.store.root),
                    "new",
                    "other",
                    "fast",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(0, result.returncode, result.stderr)
        finally:
            slow.communicate(input="x", timeout=20)
