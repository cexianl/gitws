import tempfile
import unittest
from pathlib import Path

from gitws.cli import main
from gitws.core import GitwsError, run_command


class ErrorTests(unittest.TestCase):
    def test_missing_external_command_is_a_domain_error(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "missing-command")
            with self.assertRaises(GitwsError):
                run_command(missing)

    def test_invalid_configuration_has_a_cli_error_with_its_path(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text('{"repos":')
            with self.assertRaises(SystemExit) as raised:
                main(["--home", directory, "list"])
            self.assertIn("gitws: error:", str(raised.exception))
            self.assertIn(str(config), str(raised.exception))

    def test_storage_path_that_is_a_file_has_a_cli_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "file"
            root.touch()
            with self.assertRaises(SystemExit) as raised:
                main(["--home", str(root), "list"])
            self.assertIn("gitws: error:", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
