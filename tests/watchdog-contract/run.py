#!/usr/bin/env python3
"""Exercise actual Renode watchdog timing, write protection and reset-cause flags."""
import argparse
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, help='FirmwareSimulator Docker image with Renode')
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[2] / 'renode/peripherals/SedsStm32System.cs'
    with tempfile.TemporaryDirectory(prefix='watchdog-contract-') as temp:
        root = Path(temp)
        root.chmod(0o755)
        (root/'SedsStm32System.cs').write_text(source.read_text())
        commands = []
        for variant, offset in [('g4', 0x94), ('h5', 0xF4), ('u5', 0xF4)]:
            name = 'test-'+variant
            (root/(name+'.repl')).write_text(
                'rcc: Miscellaneous.SedsStm32Rcc @ sysbus 0x40021000\n'
                f'    variant: "{variant}"\n'
                '    preinit:\n        include @/work/SedsStm32System.cs\n'
                'iwdg: Miscellaneous.SedsStm32Iwdg @ sysbus 0x40003000\n    rcc: rcc\n')
            commands += ['mach create "'+name+'"', 'machine LoadPlatformDescription @/work/'+name+'.repl']
            def check(expression):
                # Expressions use Renode's Python monitor, not a mocked register interface.
                commands.append('python "assert '+expression.replace('"','\\"')+'"')
            commands += ['iwdg WriteDoubleWord 8 63']
            check('monitor.Machine["sysbus.iwdg"].ReadDoubleWord(8) == 4095')
            commands += ['iwdg WriteDoubleWord 0 0xCCCC', 'iwdg WriteDoubleWord 0 0x5555',
                         'iwdg WriteDoubleWord 4 0', 'iwdg WriteDoubleWord 8 63',
                         'iwdg WriteDoubleWord 0 0xAAAA', 'emulation RunFor "0.004"',
                         'iwdg WriteDoubleWord 0 0xAAAA', 'iwdg WriteDoubleWord 8 1',
                         'emulation RunFor "0.005"']
            check('monitor.Machine["sysbus.iwdg"].GetResetCount() == 0')
            check('monitor.Machine["sysbus.iwdg"].ReadDoubleWord(8) == 63')
            commands += ['emulation RunFor "0.004"']
            check('monitor.Machine["sysbus.iwdg"].GetResetCount() == 1')
            check(f'(monitor.Machine["sysbus.rcc"].ReadDoubleWord({offset}) & 0x20000000) != 0')
            commands += [f'rcc WriteDoubleWord {offset} 0x800000', 'iwdg WriteDoubleWord 0 0xAAAA',
                         'emulation RunFor "0.020"']
            check(f'(monitor.Machine["sysbus.rcc"].ReadDoubleWord({offset}) & 0x20000000) == 0')
            check('monitor.Machine["sysbus.iwdg"].GetResetCount() == 1')
            # Exercise the production /256 prescaler too: (1+1)*256/32000 = 16 ms.
            commands += ['iwdg WriteDoubleWord 0 0xCCCC', 'iwdg WriteDoubleWord 0 0x5555',
                         'iwdg WriteDoubleWord 4 6', 'iwdg WriteDoubleWord 8 1',
                         'iwdg WriteDoubleWord 0 0xAAAA', 'emulation RunFor \"0.015\"']
            check('monitor.Machine["sysbus.iwdg"].GetResetCount() == 1')
            commands += ['emulation RunFor \"0.002\"']
            check('monitor.Machine["sysbus.iwdg"].GetResetCount() == 2')
            commands += ['mach rem "'+name+'"']
        commands += ['echo "IWDG_REGISTER_TIMING_RESET_PASS"', 'quit']
        (root/'test.resc').write_text('\n'.join(commands)+'\n')
        result = subprocess.run(['docker', 'run', '--rm', '--entrypoint', '/opt/renode/renode',
            '-v', str(root)+':/work:ro', args.image, '--disable-xwt', '--console', '-p', '/work/test.resc'],
            capture_output=True, text=True, check=True)
        output = result.stdout+result.stderr
        errors = ('AssertionError', 'there was an error executing command', 'could not compile',
                  'CPU abort', 'Traceback', 'fatal error')
        if 'IWDG_REGISTER_TIMING_RESET_PASS' not in output or any(e.lower() in output.lower() for e in errors):
            raise RuntimeError(output)
        print('IWDG_REGISTER_TIMING_RESET_PASS')

if __name__ == '__main__':
    main()
