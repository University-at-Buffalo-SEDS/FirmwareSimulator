"""Exercise the production clock publisher on Renode's actual timer/filesystem."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('FIRMWARE_SIM_RENODE_TEST_IMAGE'),
                     'Explicit local Renode image required')
class SimulationClockTests(unittest.TestCase):
    def test_millisecond_snapshots_pause_and_survive_board_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            (fixture / 'clock.repl').write_text(
                'simulationClock: Miscellaneous.SedsSimulationClock\n'
                '    path: "/fixture/clock-ms"\n')
            (fixture / 'clock.resc').write_text('''include @/src/renode/peripherals/SedsSimulationClock.cs
mach create "clock-contract"
machine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/stm32g491.repl
cpu IsHalted true
machine LoadPlatformDescription @/fixture/clock.repl
emulation RunFor "0.003s"
python "from System.IO import File; value=int(File.ReadAllText('/fixture/clock-ms')); assert value==3, str(value); assert not File.Exists('/fixture/clock-ms.next')"
python "import time; time.sleep(0.02); assert int(File.ReadAllText('/fixture/clock-ms'))==value"
machine Reset
cpu IsHalted true
emulation RunFor "0.002s"
python "value=int(File.ReadAllText('/fixture/clock-ms')); assert value==5, str(value); assert not File.Exists('/fixture/clock-ms.next'); print('SIMULATION_CLOCK_SNAPSHOT_RESET_PAUSE_PASS')"
quit
''')
            result = subprocess.run([
                'docker', 'run', '--rm', '--network=none',
                '--user', f'{os.getuid()}:{os.getgid()}', '--env', 'HOME=/tmp',
                '--mount', f'type=bind,source={ROOT},target=/src,readonly',
                '--mount', f'type=bind,source={fixture},target=/fixture',
                '--entrypoint', '/opt/renode/renode',
                os.environ['FIRMWARE_SIM_RENODE_TEST_IMAGE'],
                '--disable-xwt', '--console', '--execute',
                'include @/fixture/clock.resc'], capture_output=True, text=True, timeout=90)
            log = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, log)
            self.assertNotIn('There was an error executing command', log)
            self.assertIn('SIMULATION_CLOCK_SNAPSHOT_RESET_PAUSE_PASS', log)


if __name__ == '__main__':
    unittest.main()
