#!/usr/bin/env python3
"""Decode a native bay CAN trace (diagnostics/native-can-trace.txt: `<virtual ns> <node> <id hex> <data hex>` per
frame, written with `firmware-sim native-bay` can_trace or `gs_bay.py --can-trace`) into SEDSnet 7 packets.

Link format v2 fragments are reassembled per sender and identifier (scripts/analyze-can-trace.py link_payload);
canonical frames are decoded up to the payload: type, source address, epoch, endpoints, contract, timestamp,
nonce, reliable header. Side-transport frames (first byte with both 0x60 bits set: template, chunk, bundle)
carry no visible type and are only counted. No CRC or application validity claim.

  python3 scripts/decode-native-can-trace.py TRACE [--types 256715704,1] [--nodes gateway,valve] [--from-s A]
      [--to-s B] [--jsonl]

Example: every FLIGHT_STATE write the gateway relayed and the receipts (ReliableAck, type 1) the fill boards sent:
  decode-native-can-trace.py native-can-trace.txt --types <FLIGHT_STATE id>,1 --nodes gateway,valve,daq
"""
from __future__ import annotations

import argparse
import collections
import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location('analyze_can_trace', Path(__file__).resolve().parent / 'analyze-can-trace.py')
_READER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_READER)
link_payload = _READER.link_payload


def uleb(data: bytes, offset: int) -> tuple[int, int]:
    value = shift = 0
    for _ in range(10):
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        shift += 7
        if not byte & 128:
            return value, offset
    raise ValueError('overlong LEB128')


def decode(frame: bytes) -> dict | None:
    """A SEDSnet 7 canonical frame up to its payload (CRC excluded), or None for a side-transport frame."""
    flags = frame[0]
    if flags & 0x60 == 0x60:
        return None
    ty, o = uleb(frame, 1)
    src, o = uleb(frame, o)
    epoch = None
    if flags & 0x80:
        epoch, o = uleb(frame, o)
    endpoints = None
    if flags & 0x60 == 0x20:
        endpoint, o = uleb(frame, o)
        endpoints = [endpoint]
    elif flags & 0x60 == 0x40:
        count, o = uleb(frame, o)
        endpoints = []
        for _ in range(count):
            endpoint, o = uleb(frame, o)
            endpoints.append(endpoint)
    contract = None
    if flags & 0x04:
        size, o = uleb(frame, o)
        contract = frame[o:o + size]
        o += size
    timestamp, o = uleb(frame, o)
    if flags & 0x11:
        _, o = uleb(frame, o)
    nonce = None
    if flags & 0x08:
        nonce, o = uleb(frame, o)
    reliable = None
    if flags & 0x02 or (contract and contract[0] & 0x04):
        rel_flags = frame[o]
        o += 1
        seq = ack = 0
        if rel_flags & 4:
            seq, o = uleb(frame, o)
        if rel_flags & 8:
            ack, o = uleb(frame, o)
        reliable = {'flags': rel_flags, 'seq': seq, 'ack': ack}
    if o > len(frame) - 4:
        raise ValueError('header runs into the CRC')
    return {'flags': flags, 'type': ty, 'source': src, 'epoch': epoch, 'endpoints': endpoints,
            'contract': contract.hex() if contract is not None else None, 'timestamp': timestamp,
            'nonce': nonce, 'reliable': reliable, 'payload': frame[o:-4].hex()}


def packets(lines):
    """(virtual ns, node, identifier, packet bytes) for every complete link v2 packet in trace lines."""
    active = {}
    for line in lines:
        parts = line.split()
        if len(parts) != 4:
            continue
        now, node, identifier, data = int(parts[0]), parts[1], int(parts[2], 16), bytes.fromhex(parts[3])
        body = link_payload(identifier, data)
        if body is None:
            continue
        kind, key = identifier & 7, (node, identifier & 0x1F8)
        if kind <= 1:
            yield now, node, identifier, body
        elif kind == 2:
            active[key] = [body]
        elif key in active:
            active[key].append(body)
            if kind >= 4:
                yield now, node, identifier, b''.join(active.pop(key))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('trace', type=Path)
    p.add_argument('--types', help='comma-separated SEDSnet type ids (default: every canonical packet)')
    p.add_argument('--nodes', help='comma-separated sender nodes')
    p.add_argument('--from-s', type=float, default=0.0)
    p.add_argument('--to-s', type=float, default=float('inf'))
    p.add_argument('--jsonl', action='store_true')
    a = p.parse_args()
    types = {int(t) for t in a.types.split(',')} if a.types else None
    nodes = set(a.nodes.split(',')) if a.nodes else None
    skipped = collections.Counter()
    with a.trace.open() as lines:
        for now, node, identifier, packet in packets(lines):
            seconds = now / 1e9
            if not a.from_s <= seconds <= a.to_s or (nodes and node not in nodes):
                continue
            try:
                row = decode(packet)
            except (IndexError, ValueError):
                skipped['malformed'] += 1
                continue
            if row is None:
                skipped['side-transport'] += 1
                continue
            if types and row['type'] not in types:
                continue
            row.update(time_s=round(seconds, 6), node=node, can_id=identifier)
            if a.jsonl:
                print(json.dumps(row, separators=(',', ':')))
            else:
                print(f"{seconds:11.6f} {node:9s} id={identifier:03x} type={row['type']} src={row['source']} "
                      f"ts={row['timestamp']} nonce={row['nonce']} rel={row['reliable']} payload={row['payload']}")
    if skipped and not a.jsonl:
        print('# not decoded (in range): ' + ', '.join(f'{k} {v}' for k, v in sorted(skipped.items())))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
