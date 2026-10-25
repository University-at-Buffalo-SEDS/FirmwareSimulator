#!/usr/bin/env python3
"""Decode bounded simulator radio captures with the actual local SEDSnet codec."""
import argparse
import importlib.util
import json
import hashlib
import re
import tempfile
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('radio_trace_validation', ROOT / 'scripts/inspect-radio-trace.py')
VALIDATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATE)


def relay_fixtures(gs_source):
    """Execute the exact production GS envelope functions on canonical fixtures."""
    source = (gs_source / 'backend/src/rrls.rs').read_text()
    functions = []
    for name in ('crc', 'encode', 'decode'):
        start = source.index('fn ' + name + '(')
        body = source.index('{', start)
        depth, end = 1, body + 1
        while depth:
            depth += (source[end] == '{') - (source[end] == '}')
            end += 1
        functions.append(source[start:end])
    maximum = re.search(r'const MAX_PAYLOAD: usize = ([0-9_]+);', source).group(0)
    contract = maximum + '\n' + '\n'.join(functions)
    program = contract + r'''
fn main() {
    for kind in [1u8, 2, 5] {
        let mut bytes = encode(kind, &[0, 1, 2, 128, 255]);
        assert_eq!(decode(&bytes), Some((kind, &[0, 1, 2, 128, 255][..])));
        println!("{}:true:{:?}", kind, bytes);
        bytes[4] ^= 1;
        assert!(decode(&bytes).is_none());
        println!("{}:false:{:?}", kind, bytes);
    }
}
'''
    with tempfile.TemporaryDirectory(prefix='seds-relay-codec-') as directory:
        root = Path(directory)
        (root / 'codec.rs').write_text(program)
        subprocess.run(['rustc', '--edition', '2024', str(root / 'codec.rs'), '-o', str(root / 'codec')], check=True)
        result = subprocess.run([str(root / 'codec')], text=True, capture_output=True, check=True)
    fixtures = []
    for line in result.stdout.splitlines():
        kind, valid, data = line.split(':', 2)
        fixtures.append(dict(kind=int(kind), valid=valid == 'true', bytes=json.loads(data)))
    if len(fixtures) != 6:
        raise ValueError('actual GroundStation relay codec did not produce six fixtures')
    return fixtures, hashlib.sha256(contract.encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--groundstation-source', type=Path, default=ROOT.parent / 'GroundStation26')
    parser.add_argument('--sdk-source', type=Path, default=ROOT.parent / 'SEDSnet')
    parser.add_argument('--schema', type=Path, action='append', default=[])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-clock-backstep-us', type=int, default=0)
    args = parser.parse_args()
    traces = sorted(args.directory.resolve().glob('*.bin'))
    if not 1 <= len(traces) <= 32:
        parser.error('capture directory must contain1..32bounded trace streams')
    for trace in traces:
        VALIDATE.inspect(trace, max_clock_backstep_us=args.max_clock_backstep_us)  # Strict completed/no-cap reconciliation first.
    schemas = args.schema or [ROOT.parent / 'RFBoard26/config/sedsnet.json']
    if len(schemas) > 16 or any(not path.is_file() for path in schemas):
        parser.error('expected1..16existing source schema files')
    output = args.output.resolve()
    fixtures, relay_contract_sha256 = relay_fixtures(args.groundstation_source.resolve())
    manifest = output.with_suffix('.manifest.json')
    manifest.write_text(json.dumps({'version': 1, 'streams': [str(path) for path in traces],
                                   'schemas': [str(path.resolve()) for path in schemas], 'max_clock_backstep_us': args.max_clock_backstep_us,
                                   'relay_fixtures': fixtures, 'relay_contract_sha256': relay_contract_sha256}, indent=2) + '\n')
    env = os.environ.copy()
    env.update(SEDSNET_RADIO_TRACE_MANIFEST=str(manifest), SEDSNET_RADIO_TRACE_OUTPUT=str(output))
    env.setdefault('CARGO_BUILD_JOBS', '8')
    command = ['cargo', 'test', '--lib',
               'router::radio_trace_decoder::decode_capture', '--offline', '--', '--ignored', '--exact', '--nocapture']
    result = subprocess.run(command, cwd=args.sdk_source.resolve(), env=env, check=False)
    if result.returncode:
        raise SystemExit(result.returncode)
    report = json.loads(output.read_text())
    for stream in report['streams']:
        counts = {}
        for packet in stream['packets']:
            counts[packet['type']] = counts.get(packet['type'], 0) + 1
        print(json.dumps({'file': Path(stream['file']).name, 'packet_count': len(stream['packets']),
                          'codec_errors': len(stream['errors']), 'types': counts}, sort_keys=True))
    print(f'Detailed SDK decode: {output}')


if __name__ == '__main__':
    main()
