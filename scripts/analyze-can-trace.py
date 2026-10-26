#!/usr/bin/env python3
"""Read passive SEDSCAN1 captures; reassemble CAN fragments without decoding SDT compact/chunk frames."""
import argparse
import collections
import json
from pathlib import Path
import struct

HEADER = struct.Struct('<8sIIQQ')
RECORD = struct.Struct('<QIBBH64s')
FRAGMENT = struct.Struct('<2sBBBBBH')
MAX_BYTES = 64 * 1024 * 1024
MAX_ASSEMBLIES = 256


def uleb(data, offset):
    value = 0
    for n in range(5):
        if offset >= len(data):
            raise ValueError('truncated u32 LEB128')
        byte = data[offset]
        offset += 1
        if n == 4 and byte & 0xf0:
            raise ValueError('overflowing u32 LEB128')
        value |= (byte & 127) << (7 * n)
        if not byte & 128:
            return value, offset
    raise ValueError('unterminated u32 LEB128')


def taxonomy(data):
    offset = 0
    if data.startswith(b'SDT'):
        if len(data) < 4:
            return 'malformed', None
        if data[3] != 1:
            return 'sdt-kind-' + str(data[3]) + '-undecoded', None
        try:
            _, offset = uleb(data, 4)
        except ValueError:
            return 'malformed', None
        kind = 'sdt-full'
    else:
        kind = 'canonical'
    try:
        if len(data) - offset < 3:
            raise ValueError('short canonical header')
        ty, _ = uleb(data, offset + 2)
        return kind, ty
    except ValueError:
        return 'malformed', None


def read_capture(path, emit=None):
    path = Path(path)
    size = path.stat().st_size
    if not HEADER.size <= size <= MAX_BYTES or (size - HEADER.size) % RECORD.size:
        raise ValueError('capture size invalid or over bound')
    status_path = Path(str(path) + '.status.json')
    if status_path.stat().st_size > 4096:
        raise ValueError('oversized status')
    status = json.loads(status_path.read_text())
    with path.open('rb') as source:
        magic, version, record_size, until, limit = HEADER.unpack(source.read(HEADER.size))
        count = (size - HEADER.size) // RECORD.size
        if magic != b'SEDSCAN1' or version != 1 or record_size != RECORD.size:
            raise ValueError('capture header unsupported')
        if not HEADER.size + RECORD.size <= limit <= MAX_BYTES or size > limit:
            raise ValueError('capture limit invalid')
        if not (status.get('finished') is True and status.get('capped') is False
                and status.get('faulted') is False and status.get('version') == 1
                and status.get('record_bytes') == RECORD.size and status.get('records') == count
                and status.get('until_us') == until and status.get('max_bytes') == limit):
            raise ValueError('capture not finished, complete, or status/header mismatch')
        result = {'path': str(path), 'records': count, 'logical_packets': 0, 'boundary': 'CAN fragment reassembly only; each undecoded SDT chunk is a separate payload, not a complete SDK packet',
                  'fragment_frames': 0, 'fragment_duplicates': 0, 'fragment_rejected': 0,
                  'fragment_expired': 0, 'kinds': collections.Counter(),
                  'data_types': collections.Counter(), 'per_second': {}}
        active = collections.OrderedDict()
        last_time = 0
        def packet(now, identifier, data, first):
            kind, ty = taxonomy(data)
            result['logical_packets'] += 1
            result['kinds'][kind] += 1
            if ty is not None:
                key = str(ty)
                result['data_types'][key] += 1
                second = result['per_second'].setdefault(str(now // 1000000), {})
                second[key] = second.get(key, 0) + 1
            if emit:
                emit({'capture': path.name, 'time_us': now, 'first_us': first, 'can_id': identifier,
                      'kind': kind, 'data_type': ty, 'bytes_hex': data.hex()})
        for _ in range(count):
            now, identifier, flags, length, reserved, padded = RECORD.unpack(source.read(RECORD.size))
            if now < last_time or now > until or flags & 0xf0 or length > 64 or reserved:
                raise ValueError('invalid trace record')
            last_time = now
            data = padded[:length]
            for key in list(active):
                if now - active[key]['last'] > 1000000:
                    del active[key]
                    result['fragment_expired'] += 1
            if not data.startswith(b'SD') or data.startswith(b'SDT'):
                packet(now, identifier, data, now)
                continue
            result['fragment_frames'] += 1
            if len(data) < FRAGMENT.size:
                result['fragment_rejected'] += 1
                continue
            _, sender, seq, index, total, frag_flags, total_length = FRAGMENT.unpack_from(data)
            if (not total or index >= total or not total_length or total_length > 55 * total
                    or total != (total_length + 54) // 55 or len(data) < FRAGMENT.size + min(55, total_length - 55 * index)):
                result['fragment_rejected'] += 1
                continue
            key = (identifier, sender, seq)
            item = active.get(key)
            if item and (item['total'], item['length']) != (total, total_length):
                del active[key]
                result['fragment_rejected'] += 1
                continue
            if item is None:
                if len(active) >= MAX_ASSEMBLIES:
                    active.popitem(last=False)
                    result['fragment_expired'] += 1
                item = {'first': now, 'last': now, 'total': total, 'length': total_length,
                        'flags': frag_flags, 'parts': {}}
                active[key] = item
            payload = data[FRAGMENT.size:FRAGMENT.size + min(55, total_length - 55 * index)]
            if index in item['parts']:
                if item['parts'][index] != payload:
                    del active[key]
                    result['fragment_rejected'] += 1
                    continue
                result['fragment_duplicates'] += 1
            item['parts'][index] = payload
            item['last'] = now
            active.move_to_end(key)
            if len(item['parts']) == total:
                packet(now, identifier, b''.join(item['parts'][i] for i in range(total)), item['first'])
                del active[key]
        if status.get('last_record_us') != last_time:
            raise ValueError('last timestamp/status mismatch')
        result['fragment_incomplete_at_end'] = len(active)
        result['last_record_us'] = last_time
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('captures', type=Path, nargs='+')
    parser.add_argument('--packets-jsonl', type=Path)
    args = parser.parse_args()
    output = args.packets_jsonl.open('x') if args.packets_jsonl else None
    written = 0
    try:
        def emit(row):
            nonlocal written
            line = json.dumps(row, separators=(',', ':')) + '\n'
            written += len(line)
            if written > MAX_BYTES:
                raise ValueError('logical packet output exceeds 64MiB bound')
            output.write(line)
        summaries = [read_capture(path, emit if output else None) for path in args.captures]
        print(json.dumps({'scope': 'Passive CAN taxonomy; compact/chunk SDT remains undecoded; no CRC/application validity claim', 'captures': summaries}, indent=2))
    finally:
        if output:
            output.close()


if __name__ == '__main__':
    main()
