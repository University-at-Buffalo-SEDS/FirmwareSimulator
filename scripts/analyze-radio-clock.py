#!/usr/bin/env python3
"""Compare SDK-decoded time-sync announces with one explicit simulator UTC epoch."""
import argparse
import csv
import json
from pathlib import Path


def analyze(report, epoch_ms, station=1, sender='GS'):
    source = report['known_source_addresses'][sender]
    streams = [s for s in report['streams'] if (s['station'], s['direction']) == (station, 0)]
    if len(streams) != 1:
        raise ValueError('expected one sender UART TX stream')
    rows = []
    previous = None
    for packet in streams[0]['packets']:
        announce = packet.get('timesync_announce')
        if packet['source_address'] != source or announce is None:
            continue
        utc = announce['time_ms']
        relative = utc - epoch_ms
        wire_ms = packet['at_us'] / 1000
        if previous is not None and utc < previous:
            raise ValueError('advertised network UTC moved backwards')
        if relative < 0 or relative > wire_ms + 50:
            raise ValueError('advertised network UTC does not match the explicit simulator epoch')
        rows.append(dict(frame_complete_mono_us=packet['at_us'], packet_timestamp_ms=packet['timestamp_ms'],
                         announced_utc_ms=utc, epoch_ms=epoch_ms, relative_utc_ms=relative,
                         utc_minus_wire_ms=round(relative-wire_ms, 3), packet_id=packet['packet_id']))
        previous = utc
    if len(rows) < 2:
        raise ValueError('at least two actual source time-sync announces are required')
    return rows, dict(result='PASS', sender=sender, station=station, source_address=source,
                      epoch_ms=epoch_ms, announcements=len(rows), first_utc_ms=rows[0]['announced_utc_ms'],
                      last_utc_ms=rows[-1]['announced_utc_ms'],
                      utc_elapsed_ms=rows[-1]['announced_utc_ms']-rows[0]['announced_utc_ms'],
                      wire_elapsed_ms=(rows[-1]['frame_complete_mono_us']-rows[0]['frame_complete_mono_us'])/1000,
                      minimum_utc_minus_wire_ms=min(r['utc_minus_wire_ms'] for r in rows),
                      maximum_utc_minus_wire_ms=max(r['utc_minus_wire_ms'] for r in rows),
                      scope='Actual SDK time-sync payload decoder; queue delay is retained, not removed or sorted. Packet/UI wall timestamps are recorded separately.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('decoded', type=Path)
    parser.add_argument('--epoch-ms', type=int, required=True)
    parser.add_argument('--station', type=int, default=1)
    parser.add_argument('--sender', default='GS')
    parser.add_argument('--csv', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with args.decoded.open('rb') as source:
        data = source.read(64*1024*1024+1)
    if len(data) > 64*1024*1024:
        parser.error('decoded evidence exceeds64MiB bound')
    rows, result = analyze(json.loads(data), args.epoch_ms, args.station, args.sender)
    with args.csv.open('w', newline='') as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    args.output.write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
