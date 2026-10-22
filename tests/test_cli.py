from __future__ import annotations

import io
import shutil
from contextlib import redirect_stdout

from gitws.cli import main
from tests.support import RepositoryTestCase


class CliTests(RepositoryTestCase):
    def test_status_lists_workspaces_after_source_removed(self) -> None:
        self.new("a")
        shutil.rmtree(self.source)
        output = io.StringIO()
        with redirect_stdout(output):
            main(["--home", str(self.store.root), "status"])
        self.assertIn("unavailable", output.getvalue())
        self.assertIn("a  ", output.getvalue())
