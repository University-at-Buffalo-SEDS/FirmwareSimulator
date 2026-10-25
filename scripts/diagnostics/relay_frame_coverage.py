#!/usr/bin/env python3
"""Match exact inner frame bytes across a completed RRLS UART trace.

Frame boundaries and relay-kind decisions come from the actual SDK decoder.
The tiny RR envelope adapter below is checked against fixtures executed from
GroundStation's production encode/decode functions, recorded in its manifest.
"""
import argparse
from collections import defaultdict, deque
import hashlib
import importlib.util
import json
from pathlib import Path
import zlib

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('trace_validator', ROOT/'scripts/inspect-radio-trace.py')
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)


def unwrap(encoded):
    if len(encoded)<8 or encoded[:3]!=b'RR\x01':
        return None
    if zlib.crc32(encoded[:-4]) != int.from_bytes(encoded[-4:],'little'):
        return None
    return encoded[3],encoded[4:-4]


def frames(stream):
    _,raw=VALIDATOR.inspect(stream['file'],max_clock_backstep_us=100)
    result=[]
    for frame in stream['frames']:
        offset=frame['uart_offset']
        body=raw[offset+4:offset+4+frame['length']]
        if frame.get('relay_kind') is not None:
            decoded=unwrap(body)
            if decoded is None or decoded[0]!=frame['relay_kind']:
                raise ValueError('actual SDK relay frame disagrees with production golden adapter')
            body=decoded[1]
        result.append((frame['at_us'],frame.get('relay_kind'),body))
    return result


def match(incoming, outgoing, start_ms, end_ms):
    available=defaultdict(deque)
    for timestamp,kind,body in outgoing:
        if kind in (1,5):
            available[kind,body].append(timestamp)
    rows=[]
    for timestamp,kind,body in incoming:
        if not start_ms*1000<=timestamp<=end_ms*1000 or kind not in (None,2):
            continue
        expected=1 if kind is None else 5
        candidates=available[expected,body]
        while candidates and candidates[0]<timestamp:
            candidates.popleft()
        delivered=candidates.popleft() if candidates else None
        rows.append(dict(input_us=timestamp,output_kind=expected,body_bytes=len(body),
                         body_sha256=hashlib.sha256(body).hexdigest(),output_us=delivered,
                         forwarding_age_ms=(delivered-timestamp)/1000 if delivered is not None else None))
    if not rows:
        raise ValueError('no eligible relay input work observed')
    missing=[row for row in rows if row['output_us'] is None]
    return dict(result='PASS' if not missing else 'MISSING_INNER_FRAME',
                start_ms=start_ms,end_ms=end_ms,observed_inputs=len(rows),
                delivered_inputs=len(rows)-len(missing),missing=missing,
                by_output_kind={str(kind):sum(row['output_kind']==kind for row in rows) for kind in (1,5)},
                maximum_forwarding_age_ms=max((row['forwarding_age_ms'] for row in rows if row['output_us'] is not None),default=None),rows=rows)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('decoded',type=Path)
    parser.add_argument('--start-ms',type=float,required=True)
    parser.add_argument('--end-ms',type=float,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if not 0<=args.start_ms<args.end_ms:
        parser.error('invalid observation window')
    report=json.loads(args.decoded.read_text())
    manifest=json.loads(args.decoded.with_suffix('.manifest.json').read_text())
    for fixture in manifest['relay_fixtures']:
        decoded=unwrap(bytes(fixture['bytes']))
        if bool(decoded is not None)!=fixture['valid'] or (decoded is not None and decoded!=(fixture['kind'],bytes([0,1,2,128,255]))):
            raise ValueError('adapter failed actual production codec fixtures')
    streams={(stream['station'],stream['direction']):stream for stream in report['streams']}
    result=match(frames(streams[2,1]),frames(streams[2,0]),args.start_ms,args.end_ms)
    result['relay_contract_sha256']=report['relay_contract_sha256']
    result['scope']='Exact inner bytes, one input occurrence per output, correct RR direction, original observed order/timestamps; no latest-value substitution. CRC-valid actual SDK frame boundaries and production RR golden fixtures. Window is guaranteed active relay; intentional direct-link outages are separate.'
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({key:value for key,value in result.items() if key not in ('rows','missing')}))
    if result['missing']:
        parser.exit(1,'Observed inner frame bytes are missing from RRLS egress.\n')


if __name__=='__main__':
    main()
