#!/usr/bin/env python3
"""Measure source and received telemetry gaps in actual SDK-decoded wire evidence."""
import argparse
import json
from pathlib import Path

TYPES = {'BAROMETER_DATA', 'IMU_DATA', 'GPS_SATELLITE_NUMBER',
         'BATTERY_VOLTAGE', 'BATTERY_CURRENT', 'BOARD_HEALTH'}


def unique(packets):
    seen = set()
    result = []
    for packet in packets:
        key = packet['source_address'], packet['type'], packet['packet_id']
        if key not in seen:
            seen.add(key)
            result.append(packet)
    return result


def bounds(packets, start_us, end_us):
    rows = [p for p in packets if start_us <= p['at_us'] <= end_us]
    times = [p['at_us'] for p in rows]
    return dict(count=len(rows), first_ms=times[0]/1000 if times else None,
                last_ms=times[-1]/1000 if times else None,
                maximum_internal_gap_ms=max((b-a for a,b in zip(times,times[1:])), default=0)/1000 if len(times)>1 else None,
                leading_censored_interval_ms=((times[0] if times else end_us)-start_us)/1000,
                trailing_censored_interval_ms=(end_us-(times[-1] if times else start_us))/1000,
                timestamp_backsteps=sum(b<a for a,b in zip(times,times[1:])))


def analyze(report, start_ms, end_ms):
    if not 0 <= start_ms < end_ms:
        raise ValueError('invalid forwarding observation window')
    streams = {(s['station'],s['direction']):s['packets'] for s in report['streams']}
    emitted = unique(streams[0,0])
    received = unique([p for p in streams[1,1] if p.get('relay_kind') in (None,1)])
    relay = unique([p for p in streams[1,1] if p.get('relay_kind') == 1])
    rrls = unique([p for p in streams[2,1] if p.get('relay_kind') is None])
    start_us, end_us = start_ms*1000, end_ms*1000
    names = {v:k for k,v in report['known_source_addresses'].items()}
    keys = sorted({(p['source_address'],p['type']) for p in emitted if p['type'] in TYPES})
    rows = []
    for source,kind in keys:
        select = lambda packets: [p for p in packets if (p['source_address'],p['type']) == (source,kind)]
        app = select(received)
        intersecting = [(a,b) for a,b in zip(app,app[1:]) if a['at_us']<end_us and b['at_us']>start_us]
        longest = max(intersecting, key=lambda pair:pair[1]['at_us']-pair[0]['at_us'], default=None)
        row = dict(source=names.get(source, f'@addr:{source}'), source_address=source, type=kind,
                   source_rf_tx=bounds(select(emitted),start_us,end_us),
                   rrls_rf_arrival=bounds(select(rrls),start_us,end_us),
                   gs_down_data=bounds(select(relay),start_us,end_us),
                   gs_all_eligible_deduplicated=bounds(app,start_us,end_us))
        row['largest_app_gap_intersecting_window'] = None if longest is None else dict(
            gap_ms=(longest[1]['at_us']-longest[0]['at_us'])/1000,
            preceding_report_ms=longest[0]['at_us']/1000, following_report_ms=longest[1]['at_us']/1000,
            preceding_packet_id=longest[0]['packet_id'],following_packet_id=longest[1]['packet_id'])
        rows.append(row)
    return dict(start_ms=start_ms,end_ms=end_ms,rows=rows,
                scope='Original trace byte order and timestamps retained. Packet identities deduplicated per source/type. Gaps measure wire arrivals, not private UI state. Leading/trailing intervals are censored observation bounds, not inferred missing packets. GPS_SATELLITE_NUMBER is satellite-count telemetry only, not coordinate or lock evidence.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('decoded',type=Path)
    parser.add_argument('--start-ms',type=float,required=True)
    parser.add_argument('--end-ms',type=float,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    with args.decoded.open('rb') as source:
        data=source.read(64*1024*1024+1)
    if len(data)>64*1024*1024:
        parser.error('decoded evidence exceeds64MiB')
    result=analyze(json.loads(data),args.start_ms,args.end_ms)
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    for row in result['rows']:
        print(json.dumps(row,sort_keys=True))


if __name__=='__main__':
    main()
