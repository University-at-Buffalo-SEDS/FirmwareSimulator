"""The image records the FirmwareSimulator commit it was built from.

Board runners reuse local tags such as local-dev-v0.4.15 across simulator
revisions. A label with the source commit and its commit time lets a runner
refuse an image older than the simulator behaviour its layout needs.
"""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("sim_build", ROOT / "build.py")
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


class ImageRevisionTests(unittest.TestCase):
    def test_final_stage_labels_source_revision_and_commit_time(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        final = dockerfile[dockerfile.rindex("\nFROM "):]
        # Build arguments are scoped to a stage: declare them in the final one.
        self.assertIn("ARG SOURCE_REVISION", final)
        self.assertIn("ARG SOURCE_COMMIT_TIME", final)
        self.assertIn('org.opencontainers.image.revision="${SOURCE_REVISION}"', final)
        self.assertIn('edu.buffalo.seds.firmware-sim.commit-time="${SOURCE_COMMIT_TIME}"', final)

    def test_image_command_passes_the_checkout_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git = ["git", "-C", str(root)]
            subprocess.run([*git, "init", "-q"], check=True)
            (root / "file").write_text("one")
            subprocess.run([*git, "add", "file"], check=True)
            env = {"GIT_AUTHOR_DATE": "2026-11-10T04:49:55-05:00",
                   "GIT_COMMITTER_DATE": "2026-11-10T04:49:55-05:00",
                   "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
                   "PATH": "/usr/bin:/bin"}
            subprocess.run([*git, "commit", "-q", "-m", "one"], check=True, env=env)
            head = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
            command = BUILD.image_command(root, "firmware-simulator:local")
            self.assertEqual(command[:2], ["docker", "build"])
            self.assertIn(f"SOURCE_REVISION={head}", command)
            # UTC, so runners compare it as text-free timestamps.
            self.assertIn("SOURCE_COMMIT_TIME=2026-11-10T09:49:55Z", command)
            self.assertEqual(command[-3:], ["-t", "firmware-simulator:local", str(root)])
            # Uncommitted changes are visible in the revision.
            (root / "file").write_text("two")
            self.assertIn(f"SOURCE_REVISION={head}-dirty", BUILD.image_command(root, "x"))

    def test_image_subcommand_uses_the_labelled_command(self):
        with mock.patch.object(BUILD, "run", return_value=0) as execute, \
             mock.patch.object(BUILD, "maintenance_lock"), \
             mock.patch.object(BUILD, "usage", return_value={}), \
             mock.patch.object(BUILD, "record"):
            self.assertEqual(BUILD.main(["image", "--tag", "t:1"]), 0)
        command = execute.call_args.args[0]
        self.assertTrue(any(argument.startswith("SOURCE_REVISION=") for argument in command))


if __name__ == "__main__":
    unittest.main()
