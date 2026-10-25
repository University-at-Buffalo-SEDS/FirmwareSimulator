#!/usr/bin/env python3
"""Validate a bounded emulator UART trace; never infer complete data from a cap."""
import argparse
import hashlib
import json
from pathlib import Path
import struct

MAX_BYTES = 4 * 1024 * 1024
HEADER = struct.Struct('<8sIIIIQ')
RECORD = struct.Struct('<QB')


def inspect(path, allow_incomplete=False, allow_capped=False, max_clock_backstep_us=0):
    path = Path(path)
    with path.open('rb') as handle:
        blob = handle.read(MAX_BYTES + 1)
    if len(blob) > MAX_BYTES or len(blob) < HEADER.size:
        raise ValueError('trace is oversized or missing its32-byte header')
    magic, version, width, station, direction, until = HEADER.unpack_from(blob)
    if magic != b'SEDSRBT1' or version != 1 or width != 9 or station > 15 or direction > 1:
        raise ValueError('invalid radio trace header')
    if not 1000 <= until <= 3_600_000_000 or (len(blob) - HEADER.size) % RECORD.size:
        raise ValueError('invalid trace window or truncated record')
    sidecar = Path(str(path) + '.status.json')
    with sidecar.open('rb') as handle:
        encoded_status = handle.read(4097)
    if len(encoded_status) > 4096:
        raise ValueError('oversized trace status')
    status = json.loads(encoded_status)
    records = list(RECORD.iter_unpack(blob[HEADER.size:]))
    backwards = [a[0] - b[0] for a, b in zip(records, records[1:]) if a[0] > b[0]]
    if not 0 <= max_clock_backstep_us <= 100:
        raise ValueError('invalid explicit timestamp jitter limit')
    if any(at > until for at, _ in records) or max(backwards, default=0) > max_clock_backstep_us:
        raise ValueError('trace timestamp out of scope or reversed beyond explicit limit')
    if status.get('version') != 1 or status.get('max_bytes') != MAX_BYTES or status.get('header_bytes') != 32 or status.get('record_bytes') != 9 or status.get('until_us') != until:
        raise ValueError('trace status/header bounds mismatch')
    if type(status.get('capped')) is not bool or type(status.get('finished')) is not bool:
        raise ValueError('trace status does not declare cap/completion')
    if status['records'] != len(records) or status.get('last_record_us') != (records[-1][0] if records else 0):
        raise ValueError('trace status does not reconcile with binary records')
    if not status['finished'] and not allow_incomplete:
        raise ValueError('trace window did not finish; explicit --allow-incomplete is diagnostic only')
    if status['capped'] and not allow_capped:
        raise ValueError('trace hit4MiB cap; explicit --allow-capped is diagnostic only')
    raw = bytes(value for _, value in records)
    return {'version': 1, 'station': station, 'direction': 'rx' if direction else 'tx',
            'records': len(records), 'first_us': records[0][0] if records else None,
            'last_us': records[-1][0] if records else None, 'until_us': until,
            'finished': status['finished'], 'capped': status['capped'],
            'raw_sha256': hashlib.sha256(raw).hexdigest(), 'file_bytes': len(blob),
            'backward_timestamp_count': len(backwards), 'max_clock_backstep_us': max(backwards, default=0),
            'fully_monotonic': not backwards,
            'explicit_max_clock_backstep_us': max_clock_backstep_us}, raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('trace', type=Path)
    parser.add_argument('--raw-output', type=Path, help='extract raw UART bytes for a separate decoder')
    parser.add_argument('--output', type=Path, help='write the summary JSON')
    parser.add_argument('--allow-incomplete', action='store_true')
    parser.add_argument('--allow-capped', action='store_true')
    parser.add_argument('--max-clock-backstep-us', type=int, default=0, help='explicit allowed Renode clock-context jitter; byte order is preserved and reversals reported')
    args = parser.parse_args()
    try:
        result, raw = inspect(args.trace, args.allow_incomplete, args.allow_capped, args.max_clock_backstep_us)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f'Invalid radio trace: {error}\n')
    encoded = json.dumps(result, indent=2) + '\n'
    if args.raw_output:
        args.raw_output.write_bytes(raw)
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end='')


if __name__ == '__main__':
    main()
