"""Drive the production raw-byte modem model with production host pacing.

The long-packet/50ms idle case is a configuration stress envelope, not a claim
that the active hardware's uncollected register values have been verified.
"""
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
GS_ROOT = Path(os.environ.get('GS_REPO', ROOT.parent / 'GroundStation26'))


@unittest.skipUnless(os.environ.get('CSHARP_TEST_DOTNET_IMAGE') and shutil.which('rustc'),
                     'Explicit local .NET image and Rust compiler required')
class RadioPacketizationLatency(unittest.TestCase):
    def test_production_host_pacing_bounds_small_command_packetization(self):
        capacity = GS_ROOT / 'backend/src/comms/rfd_capacity.rs'
        self.assertTrue(capacity.is_file(), 'GS_REPO must point to the production backend')
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            declaration = re.search(r'pub\(crate\) const RADIO_COMMAND_BATCH_MAX_BYTES: usize = \d+;', (GS_ROOT / 'backend/src/comms.rs').read_text())
            self.assertIsNotNone(declaration, 'Load the actual production parent packet limit')
            rust = declaration.group(0) + '\n#[path = ' + json.dumps(str(capacity)) + '] mod capacity;\n' + r'''
fn main() {
    let mut gate = capacity::Capacity::from_env(115_200);
    let wall = std::time::Instant::now();
    let mut times = Vec::new();
    for now in 0..10_000u64 {
        if gate.ready(wall, Some(now)) {
            times.push(now.to_string());
            gate.committed(wall, Some(now), 42);
        }
    }
    println!("[{}]", times.join(","));
}
'''
            (fixture / 'schedule.rs').write_text(rust)
            compiled = subprocess.run(['rustc', '--edition=2024', str(fixture / 'schedule.rs'),
                            '-o', str(fixture / 'schedule')], text=True, capture_output=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            # Child-only environment: no shared process/global configuration races.
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith('GS_RADIO_') and not key.startswith('GS_RRLS_')}
            env.update(GS_RRLS_ROLE='primary', GS_RADIO_AIR_SHARE_PERCENT='15')
            schedule = subprocess.check_output([str(fixture / 'schedule')], env=env, text=True)
            self.assertGreater(len(json.loads(schedule)), 100)
            (fixture / 'schedule.json').write_text(schedule)
            shutil.copyfile(ROOT / 'tests/radio-medium/Program.cs', fixture / 'Program.cs')
            shutil.copyfile(ROOT / 'renode/peripherals/SedsRadioMedium.cs', fixture / 'Model.cs')
            (fixture / 'contract.csproj').write_text(
                '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType>'
                '<TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>')
            command = ['docker', 'run', '--rm', '--network=none',
                '--user', f'{os.getuid()}:{os.getgid()}', '--env', 'HOME=/tmp',
                '--env', 'DOTNET_CLI_HOME=/tmp',
                '--env', 'RADIO_PACKETIZATION_SCHEDULE=/fixture/schedule.json',
                '--mount', f'type=bind,source={fixture},target=/fixture',
                '--workdir', '/fixture', '--entrypoint', 'dotnet',
                os.environ['CSHARP_TEST_DOTNET_IMAGE'], 'run', '--project', 'contract.csproj']
            result = subprocess.run(command, capture_output=True, text=True, timeout=120)
            log = result.stdout + result.stderr
            print(log)
            self.assertEqual(result.returncode, 0, log)
            self.assertIn('RADIO_PRODUCTION_PACKETIZATION_LATENCY_PASS', log)
            setting = command.index('RADIO_PACKETIZATION_SCHEDULE=/fixture/schedule.json')
            contracts = command[:setting-1] + command[setting+1:]
            result = subprocess.run(contracts, capture_output=True, text=True, timeout=120)
            contract_log = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, contract_log)
            self.assertIn('RADIO_HOST_GUEST_DIRECTION_FAIRNESS_LINKS_PASS', contract_log)
            self.assertIn('RADIO_SATURATED_AIRTIME_AND_FAIRNESS_PASS', contract_log)
            print('RADIO_EXISTING_MODEM_CONTRACTS_PASS')
            if os.environ.get('FIRMWARE_SIM_RENODE_TEST_IMAGE'):
                self.check_real_clock(fixture, json.loads(schedule))
            # Keep the failure reproducible without depending on a Git checkout
            # of an old source revision. Disabling only the new packet boundary
            # must expose the original production 49ms stream and fail the gate.
            env['GS_RADIO_PACKET_GAP_MS'] = '0'
            negative = subprocess.check_output([str(fixture / 'schedule')], env=env, text=True)
            (fixture / 'schedule.json').write_text(negative)
            result = subprocess.run(command, capture_output=True, text=True, timeout=120)
            log = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, log)
            self.assertIn('worst_us=5110000', log)
            self.assertIn('small-command transport latency exceeded500ms', log)
            self.assertNotIn('RADIO_PRODUCTION_PACKETIZATION_LATENCY_PASS', log)
            print('RADIO_PACKETIZATION_DISABLED_BOUNDARY_NEGATIVE_5110MS_PASS')

    def check_real_clock(self, fixture, times):
        script = r'''include @/src/renode/peripherals/SedsRadioMedium.cs
mach create "packetization-latency"
machine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/stm32g491.repl
cpu IsHalted true
machine LoadPlatformDescriptionFromString "gs: UART.SedsRadioHostPort @ sysbus 0x7fff0000"
machine LoadPlatformDescriptionFromString "receiver: UART.SedsRadioHostPort @ sysbus 0x7fff0100"
emulation CreateSedsRadioMedium "air" 64000 115200 8192 8192 3968 50000 160 2000
air AttachHostByName 0 "packetization-latency" "sysbus.gs"
air AttachHostByName 1 "packetization-latency" "sysbus.receiver"
python "received=[]; monitor.Machine['sysbus.receiver'].CharReceived += lambda v: received.append(int(v))"
'''
        for i, stamp in enumerate(times):
            script += 'python "[monitor.Machine[\'sysbus.gs\'].WriteChar(v) for v in range(42)]"\n'
            next_stamp = times[i + 1] if i + 1 < len(times) else stamp + 500
            script += f'emulation RunFor "{(next_stamp-stamp)/1000:.3f}s"\n'
            due = sum(value <= next_stamp - 500 for value in times[:i + 1])
            script += f'python "assert len(received)>={due*42}, str(len(received))"\n'
        script += f'python "assert received==list(range(42))*{len(times)}, str(len(received)); print(\'RADIO_PACKETIZATION_REAL_CLOCK_PASS\')"\nquit\n'
        (fixture / 'packetization.resc').write_text(script)
        result = subprocess.run(['docker', 'run', '--rm', '--network=none',
            '--user', f'{os.getuid()}:{os.getgid()}', '--env', 'HOME=/tmp',
            '--mount', f'type=bind,source={ROOT},target=/src,readonly',
            '--mount', f'type=bind,source={fixture},target=/fixture,readonly',
            '--entrypoint', '/opt/renode/renode', os.environ['FIRMWARE_SIM_RENODE_TEST_IMAGE'],
            '--disable-xwt', '--console', '--execute', 'include @/fixture/packetization.resc'],
            capture_output=True, text=True, timeout=90)
        log = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, log)
        self.assertIn('RADIO_PACKETIZATION_REAL_CLOCK_PASS', log)
        self.assertNotIn('There was an error executing command', log)
        print('RADIO_PACKETIZATION_REAL_CLOCK_PASS')


if __name__ == '__main__':
    unittest.main()
