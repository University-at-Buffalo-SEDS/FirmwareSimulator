"""Captured-load sensitivity using the production opaque-byte radio medium.

Effective air capacity is a stress envelope, not an implementation of the
proprietary ASYNC collision/retry algorithm. No production source rate changes.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("CSHARP_TEST_DOTNET_IMAGE") and shutil.which("docker"),
                     "Explicit existing offline .NET image required")
class RadioCongestionEnvelope(unittest.TestCase):
    def test_captured_load_exposes_buffering_and_framing_loss_when_air_capacity_falls(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            # Reuse the existing clock/UART stubs and exact production medium.
            original = (ROOT / "tests/radio-medium/Program.cs").read_text()
            stubs = original[original.index("class FakeUart :"):]
            source = (ROOT / "tests/radio-medium/CongestionEnvelope.cs").read_text()
            (fixture / "Program.cs").write_text(source + stubs)
            shutil.copyfile(ROOT / "renode/peripherals/SedsRadioMedium.cs", fixture / "Model.cs")
            (fixture / "research.csproj").write_text(
                '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType>'
                '<TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>')
            result = subprocess.run([
                "docker", "run", "--rm", "--network=none", "--user", f"{os.getuid()}:{os.getgid()}",
                "--env", "DOTNET_CLI_HOME=/tmp", "--env", "HOME=/tmp",
                "--mount", f"type=bind,source={fixture},target=/fixture", "--workdir", "/fixture",
                "--entrypoint", "dotnet", os.environ["CSHARP_TEST_DOTNET_IMAGE"],
                "run", "--project", "research.csproj"],
                capture_output=True, text=True, timeout=120)
            log = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, log)
            rows = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
            self.assertEqual([row["air_service_cost_factor"] for row in rows], [1, 2, 4, 6, 8, 10])
            nominal, saturated = rows[0], rows[3]
            self.assertTrue(all(value is not None and value < 500 for value in nominal["first_ack_ms"]))
            self.assertEqual(nominal["command_uart_copies"], [1] * 4)
            self.assertEqual(nominal["gs_refused_bytes"] + nominal["rf_refused_bytes"], 0)
            self.assertEqual(nominal["gs_invalid_records"] + nominal["rf_invalid_records"], 0)
            self.assertGreater(saturated["first_ack_ms"][0], 3000)
            self.assertGreater(sum(saturated["command_uart_copies"]), 40)
            self.assertGreater(saturated["rf_refused_bytes"], 0)
            self.assertGreater(saturated["gs_invalid_records"], 0)
            self.assertEqual(saturated["rf_peak"], 8192)
            print("RADIO_CAPTURED_LOAD_CAPACITY_ENVELOPE " + json.dumps(rows))
            print("RADIO_CONGESTION_BUFFERING_AND_FRAMING_NEGATIVE_PASS")


if __name__ == "__main__":
    unittest.main()
