import contextlib
import importlib.util
import io
import pathlib
import subprocess
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("full_system", ROOT / "scripts/run-full-system.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class FullSystemGate(unittest.TestCase):
    def test_default_includes_short_then_long_board_workflow(self):
        commands = []
        with patch.object(runner, "obtain", side_effect=lambda name, root: root / name), \
             patch.object(runner.shutil, "which", return_value="/bin/tool"), \
             patch.object(runner, "run", side_effect=lambda cmd, **kw: commands.append(cmd)), \
             patch("sys.argv", ["run-full-system.py"]), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(runner.main(), 0)
        self.assertIn("All firmware and linked full-system validation stages passed.", out.getvalue())
        linked = commands[-1]
        self.assertIn("--all", linked)
        self.assertIn("--ultra-soak", linked)
        self.assertIn("--release", linked)
        self.assertEqual(len(commands), 8)

    def test_late_failure_cannot_be_reported_as_a_pass(self):
        with patch.object(runner, "obtain", side_effect=lambda name, root: root / name), \
             patch.object(runner.shutil, "which", return_value="/bin/tool"), \
             patch.object(runner, "run", side_effect=subprocess.CalledProcessError(1, ["soak"])), \
             patch("sys.argv", ["run-full-system.py", "--skip-unit-tests"]), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(runner.main(), 1)
        # the runner's operator-facing failure matrix, captured here so a passing test run prints no FAIL line
        self.assertRegex(err.getvalue(), r"Full-system qualification\s+FAIL")
        self.assertIn("returned non-zero exit status 1", err.getvalue())
