"""Contracts for actual G4 backup-register source and its real Renode mapping."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class G4TampTests(unittest.TestCase):
    @unittest.skipUnless((shutil.which("mcs") and shutil.which("mono"))
                         or os.environ.get("CSHARP_TEST_DOTNET_IMAGE"),
                         "Mono or explicitly selected cached .NET image required")
    def test_actual_model_register_gates_retention_and_readback_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            sources = {
                "Program.cs": ROOT / "tests/g4-tamp/Program.cs",
                "SedsStm32System.cs": ROOT / "renode/peripherals/SedsStm32System.cs",
                "SedsStm32G4Tamp.cs": ROOT / "renode/peripherals/SedsStm32G4Tamp.cs",
            }
            for name, source in sources.items():
                shutil.copyfile(source, fixture / name)
            image = os.environ.get("CSHARP_TEST_DOTNET_IMAGE")
            if image:
                (fixture / "contract.csproj").write_text(
                    '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
                    '<OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework>'
                    '<TreatWarningsAsErrors>true</TreatWarningsAsErrors>'
                    '</PropertyGroup></Project>')
                command = ["docker", "run", "--rm", "--pull=never", "--network=none",
                           "--cpus=1", "--memory=512m", "--memory-swap=512m", "--pids-limit=128",
                           "--user", f"{os.getuid()}:{os.getgid()}", "--env", "HOME=/tmp",
                           "--env", "DOTNET_CLI_HOME=/tmp", "--env", "DOTNET_CLI_TELEMETRY_OPTOUT=1",
                           "--mount", f"type=bind,source={fixture},target=/fixture",
                           "--workdir", "/fixture", "--entrypoint", "dotnet", image,
                           "run", "--project", "contract.csproj"]
                result = subprocess.run(command, capture_output=True, text=True, timeout=120)
            else:
                executable = fixture / "contract.exe"
                compiled = subprocess.run(["mcs", "-warnaserror", f"-out:{executable}",
                                           *map(str, (fixture / name for name in sources))],
                                          capture_output=True, text=True, timeout=60)
                self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
                result = subprocess.run(["mono", str(executable)], capture_output=True,
                                        text=True, timeout=30)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("PASS G4 TAMP actual register storage", output)

    @unittest.skipUnless(os.environ.get("FIRMWARE_SIM_RENODE_TEST_IMAGE"),
                         "Explicit cached local Renode image required")
    def test_actual_platform_mapping_machine_reset_and_backup_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            (fixture / "g4-tamp.resc").write_text('''mach create "g4-tamp-contract"
machine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/stm32g491.repl
macro reset
"""
cpu IsHalted true
"""
cpu IsHalted true
python "from System import UInt32; b=monitor.Machine.SystemBus; assert b.ReadDoubleWord(0x40021058)==0x400; assert b.ReadDoubleWord(0x40002578)==0; b.WriteDoubleWord(0x40002578,0x41425254); assert b.ReadDoubleWord(0x40002578)==0; b.WriteDoubleWord(0x40007000,0x100); b.WriteDoubleWord(0x40021058,0); b.WriteDoubleWord(0x40002578,0x41425254); assert b.ReadDoubleWord(0x40002578)==0; b.WriteDoubleWord(0x40021058,0x10000400); assert b.ReadDoubleWord(0x40002578)==0; [b.WriteDoubleWord(0x40002500+4*i,UInt32(0x13579BDF ^ ((i*0x10203041)&0xFFFFFFFF))) for i in range(32)]; assert all(b.ReadDoubleWord(0x40002500+4*i)==(0x13579BDF ^ ((i*0x10203041)&0xFFFFFFFF)) for i in range(32))"
machine Reset
cpu IsHalted true
python "b=monitor.Machine.SystemBus; assert b.ReadDoubleWord(0x40021058)==0x400; assert (b.ReadDoubleWord(0x40007000)&0x100)==0; assert b.ReadDoubleWord(0x40002578)==(0x13579BDF ^ ((30*0x10203041)&0xFFFFFFFF)); b.WriteDoubleWord(0x40002578,0x41425254); assert b.ReadDoubleWord(0x40002578)!=(0x41425254); b.WriteDoubleWord(0x40021090,0x10000); assert (b.ReadDoubleWord(0x40021090)&0x10000)==0"
python "from System import UInt32; b=monitor.Machine.SystemBus; b.WriteDoubleWord(0x40007000,0x100); b.WriteDoubleWord(0x40002578,0x41425254); assert b.ReadDoubleWord(0x40002578)==0x41425254; assert b.ReadDoubleWord(0x4000257C)!=0xBEBDADAB; b.WriteDoubleWord(0x4000257C,UInt32(0xBEBDADAB)); assert b.ReadDoubleWord(0x4000257C)==0xBEBDADAB; b.WriteDoubleWord(0x40021090,0x10000); b.WriteDoubleWord(0x40002578,0x41425254); assert b.ReadDoubleWord(0x40002578)==0; b.WriteDoubleWord(0x40021090,0); assert all(b.ReadDoubleWord(0x40002500+4*i)==0 for i in range(32)); b.WriteDoubleWord(0x40002578,0x12345678); assert b.ReadDoubleWord(0x40002578)==0x12345678"
echo "G4_TAMP_REAL_MAPPING_RESET_READBACK_PASS"
quit
''')
            command = ["docker", "run", "--rm", "--pull=never", "--network=none",
                       "--cpus=1", "--memory=1g", "--memory-swap=1g", "--pids-limit=128",
                       "--user", f"{os.getuid()}:{os.getgid()}", "--env", "HOME=/tmp",
                       "--mount", f"type=bind,source={fixture},target=/fixture"]
            for relative in ("peripherals/SedsStm32System.cs", "peripherals/SedsStm32G4Tamp.cs",
                             "platforms/stm32g491.repl"):
                command.extend(["--mount", f"type=bind,source={ROOT / 'renode' / relative},"
                                f"target=/opt/firmware-sim/renode/{relative},readonly"])
            command.extend(["--entrypoint", "/opt/renode/renode",
                            os.environ["FIRMWARE_SIM_RENODE_TEST_IMAGE"],
                            "--disable-xwt", "--console", "--execute", "include @/fixture/g4-tamp.resc"])
            result = subprocess.run(command, capture_output=True, text=True, timeout=90)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertNotIn("There was an error executing command", output)
            self.assertNotIn("non existing peripheral at 0x400025", output)
            self.assertIn("G4_TAMP_REAL_MAPPING_RESET_READBACK_PASS", output)


if __name__ == "__main__":
    unittest.main()
