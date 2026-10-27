#!/usr/bin/env python3
"""Measure source and received telemetry gaps in actual SDK-decoded wire evidence."""
import argparse
from bisect import bisect_left
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


def delivery_coverage(report, start_ms, end_ms, allow_empty=False):
    """Match exact source packet identities to eligible primary DOWN_DATA arrivals."""
    if not 0 <= start_ms < end_ms:
        raise ValueError('invalid source coverage window')
    streams = {(stream['station'],stream['direction']):stream['packets'] for stream in report['streams']}
    source = unique([packet for packet in streams[0,0] if packet['type'] in TYPES
                     and start_ms*1000 <= packet['at_us'] <= end_ms*1000])
    arrivals = {(packet['source_address'],packet['type'],packet['packet_id']):packet
                for packet in reversed(streams[1,1]) if packet.get('relay_kind') == 1}
    missing = []
    delivered = []
    for packet in source:
        key = packet['source_address'],packet['type'],packet['packet_id']
        arrival = arrivals.get(key)
        item = dict(source_address=key[0], type=key[1], packet_id=key[2],
                    source_rf_tx_ms=packet['at_us']/1000)
        if arrival is None:
            missing.append(item)
        else:
            item.update(gs_down_data_ms=arrival['at_us']/1000,
                        source_to_gs_ms=(arrival['at_us']-packet['at_us'])/1000)
            delivered.append(item)
    if not source and not allow_empty:
        raise ValueError('no source reports in coverage window')
    return dict(result='PASS' if not missing else 'MISSING_SOURCE_REPORTS',
                start_ms=start_ms,end_ms=end_ms,source_reports=len(source),
                delivered_reports=len(delivered),missing=missing,
                maximum_source_to_gs_ms=max((row['source_to_gs_ms'] for row in delivered),default=None),
                delivered=delivered,
                scope='Exact SDK source/type/packet-ID match to primary DOWN_DATA; no direct-arrival substitution, loss imputation or threshold relaxation.')


def required_stream_coverage(report, start_ms, end_ms, required):
    """Require actual sensor emissions and matching arrivals for each named stream."""
    if not 0 <= start_ms < end_ms:
        raise ValueError('invalid required-stream window')
    streams = {(s['station'],s['direction']):s['packets'] for s in report['streams']}
    emitted = unique([p for p in streams[0,0]
                      if start_ms*1000 <= p['at_us'] <= end_ms*1000])
    arrivals = {}
    for p in streams[1,1]:
        if p.get('relay_kind') in (None,1) and p['at_us'] <= end_ms*1000:
            arrivals.setdefault((p['source_address'],p['type'],p['packet_id']),[]).append(p['at_us'])
    for times in arrivals.values():
        times.sort()
    rows = []
    for name, kind in dict.fromkeys(required):
        address = report['known_source_addresses'].get(name)
        source = [p for p in emitted if address is not None
                  and (p['source_address'],p['type']) == (address,kind)]
        delivered = []
        for p in source:
            times = arrivals.get((address,kind,p['packet_id']),[])
            if bisect_left(times,p['at_us']) < len(times):
                delivered.append(p)
        status = ('UNKNOWN_SOURCE' if address is None else
                  'NO_SOURCE_REPORTS' if not source else
                  'NO_MATCHING_GS_REPORTS' if not delivered else 'PASS')
        rows.append(dict(source=name,source_address=address,type=kind,
                         source_reports=len(source),delivered_reports=len(delivered),result=status))
    missing = [row for row in rows if row['result'] != 'PASS']
    return dict(result='MISSING_REQUIRED_STREAMS' if missing else 'PASS',
                start_ms=start_ms,end_ms=end_ms,rows=rows,missing=missing,
                scope='Each requested source/type must emit and reach GS within the observation window, matched by SDK source address and exact packet identity. Heartbeats and other sensor types cannot substitute. This presence gate does not assert sampling cadence; exact DOWN_DATA delivery and latency remain separate gates.')


def stream_spec(value):
    parts = value.split(':')
    if len(parts) != 2 or not parts[0] or parts[1] not in TYPES:
        raise argparse.ArgumentTypeError('expected SOURCE:TYPE with a supported sensor/report type')
    return tuple(parts)



def metadata_coverage(report, start_ms, end_ms):
    """Match every canonical discovery occurrence through its correct relay direction.

    Do not deduplicate repeated advertisements: each received canonical packet is
    separate queued work. A later matching output can satisfy only one input.
    SDT metadata is deliberately excluded because its chunk/template dependency
    checks belong to the actual SDK decoder and ordered-stream fixture.
    """
    if not 0 <= start_ms < end_ms:
        raise ValueError('invalid metadata coverage window')
    streams = {(stream['station'],stream['direction']):stream for stream in report['streams']}
    canonical = lambda stream: [stream['packets'][frame['packet_index']]
                                for frame in stream['frames']
                                if 'packet_index' in frame and frame.get('side_kind') is None]
    outgoing = canonical(streams[2,0])
    used = set()
    rows = []
    for packet in canonical(streams[2,1]):
        if (packet['type'] != 'SedsnetDiscoveryAddress'
                or not start_ms*1000 <= packet['at_us'] <= end_ms*1000
                or packet.get('relay_kind') not in (None,2)):
            continue
        expected = 5 if packet.get('relay_kind') == 2 else 1
        identity = (packet['source_address'], packet['type'], packet['packet_id'])
        match = next(((index, candidate) for index,candidate in enumerate(outgoing)
                      if index not in used and candidate['at_us'] >= packet['at_us']
                      and candidate.get('relay_kind') == expected
                      and (candidate['source_address'], candidate['type'], candidate['packet_id']) == identity), None)
        row = dict(source_address=identity[0], packet_id=identity[2], input_us=packet['at_us'],
                   expected_output_kind=expected, output_us=None, age_ms=None)
        if match is not None:
            index,candidate = match
            used.add(index)
            row.update(output_us=candidate['at_us'], age_ms=(candidate['at_us']-packet['at_us'])/1000)
        rows.append(row)
    if not rows:
        raise ValueError('no canonical discovery inputs in coverage window')
    missing = [row for row in rows if row['output_us'] is None]
    return dict(result='PASS' if not missing else 'MISSING_CANONICAL_METADATA',
                start_ms=start_ms,end_ms=end_ms, received_occurrences=len(rows),
                delivered_occurrences=len(rows)-len(missing),missing=missing,rows=rows,
                maximum_forwarding_age_ms=max((row['age_ms'] for row in rows if row['age_ms'] is not None),default=None),
                scope='Canonical DiscoveryAddress occurrences at RRLS UART RX matched one-to-one by source/type/packet ID to the corresponding RRLS UART TX direction; no deduplication, SDT imputation or direct-arrival substitution.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('decoded',type=Path)
    parser.add_argument('--start-ms',type=float,required=True)
    parser.add_argument('--end-ms',type=float,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--require-source-reports',action='store_true',help='Require every observed selected source report in this window to arrive through DOWN_DATA')
    parser.add_argument('--require-stream',action='append',type=stream_spec,default=[],metavar='SOURCE:TYPE',help='Require this SDK-named source/type to emit and reach GS in the window (repeatable)')
    parser.add_argument('--require-canonical-metadata',action='store_true',help='Require every received canonical DiscoveryAddress occurrence to be forwarded')
    parser.add_argument('--max-source-latency-ms',type=float,help='Fail if any matched source report exceeds this measured wire-latency bound')
    parser.add_argument('--max-canonical-latency-ms',type=float,help='Fail if any matched canonical discovery exceeds this forwarding bound')
    args=parser.parse_args()
    if args.max_source_latency_ms is not None and (not args.require_source_reports or not 0 < args.max_source_latency_ms < float('inf')):
        parser.error('positive --max-source-latency-ms requires --require-source-reports')
    if args.max_canonical_latency_ms is not None and (not args.require_canonical_metadata or not 0 < args.max_canonical_latency_ms < float('inf')):
        parser.error('positive --max-canonical-latency-ms requires --require-canonical-metadata')
    with args.decoded.open('rb') as source:
        data=source.read(64*1024*1024+1)
    if len(data)>64*1024*1024:
        parser.error('decoded evidence exceeds64MiB')
    report=json.loads(data)
    result=analyze(report,args.start_ms,args.end_ms)
    if args.require_stream:
        result['required_streams']=required_stream_coverage(report,args.start_ms,args.end_ms,args.require_stream)
    if args.require_source_reports:
        result['exact_source_delivery']=delivery_coverage(report,args.start_ms,args.end_ms,allow_empty=bool(args.require_stream))
    if args.require_canonical_metadata:
        result['exact_canonical_metadata']=metadata_coverage(report,args.start_ms,args.end_ms)
    if args.max_source_latency_ms is not None:
        result['source_latency_gate']=dict(bound_ms=args.max_source_latency_ms, passed=result['exact_source_delivery']['maximum_source_to_gs_ms'] is not None and result['exact_source_delivery']['maximum_source_to_gs_ms'] <= args.max_source_latency_ms)
    if args.max_canonical_latency_ms is not None:
        result['canonical_latency_gate']=dict(bound_ms=args.max_canonical_latency_ms, passed=result['exact_canonical_metadata']['maximum_forwarding_age_ms'] is not None and result['exact_canonical_metadata']['maximum_forwarding_age_ms'] <= args.max_canonical_latency_ms)
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    for row in result['rows']:
        print(json.dumps(row,sort_keys=True))
    if args.require_stream and result['required_streams']['missing']:
        parser.exit(1,'Required source/type streams are absent from source emission or GS arrival evidence.\n')
    if any(not result[key]['passed'] for key in ('source_latency_gate','canonical_latency_gate') if key in result):
        parser.exit(1,'Observed relay forwarding exceeded the requested latency bound.\n')
    if args.require_canonical_metadata and result['exact_canonical_metadata']['missing']:
        parser.exit(1,'Observed canonical discovery packets are missing from the relay.\n')
    if args.require_source_reports and result['exact_source_delivery']['missing']:
        parser.exit(1,'Observed source reports are missing from the relay return path.\n')


if __name__=='__main__':
    main()
