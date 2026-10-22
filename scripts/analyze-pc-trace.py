#!/usr/bin/env python3
"""Attribute a bounded Renode Cortex-M PC trace to its exact ELF, including assembly labels."""
import argparse
import bisect
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

MAX_TRACE_BYTES = 64 * 1024 * 1024


def symbol_ranges(lines):
    labels = {}
    for line in lines:
        match = re.match(r'^([0-9a-fA-F]+)\s+(?:([0-9a-fA-F]+)\s+)?[tT]\s+(.+)$', line.strip())
        if match:
            start = int(match[1], 16) & ~1
            labels.setdefault(start, []).append((int(match[2], 16) if match[2] else 0, match[3]))
    if not labels:
        raise ValueError('ELF contains no text symbols; refusing an empty profile')
    starts = sorted(labels)
    result = []
    for index, start in enumerate(starts):
        entries = sorted(labels[start], key=lambda item: (-item[0], item[1]))
        size, name = entries[0]
        # Assembly frequently has no ELF size. Use the next distinct text label;
        # never extend an unsized final label over arbitrary memory.
        end = start + size if size else (starts[index + 1] if index + 1 < len(starts) else start + 2)
        result.append((start, end, name, sorted({name for _, name in entries}), size == 0))
    return result


def attribute(lines, symbols):
    starts = [item[0] for item in symbols]
    counts = Counter()
    total = unknown = 0
    for line in lines:
        match = re.match(r'^\s*(0x[0-9a-fA-F]+)(?:\s|$|:)', line)
        if not match:
            continue
        pc = int(match[1], 16) & ~1
        total += 1
        index = bisect.bisect_right(starts, pc) - 1
        if index >= 0 and pc < symbols[index][1]:
            counts[symbols[index][2]] += 1
        else:
            unknown += 1
    if not total:
        raise ValueError('No PC records decoded; refusing an empty performance result')
    return {'scope': '20 ms guest instruction sample; not cycle-accurate CPU utilization or delivery qualification',
            'instructions': total, 'unmapped': unknown,
            'top_functions': [{'name': name, 'instructions': count, 'percent': round(100 * count / total, 3)}
                              for name, count in counts.most_common(40)],
            'inferred_assembly_ranges': [{'name': name, 'start': start, 'end': end, 'aliases': aliases}
                                        for start, end, name, aliases, inferred in symbols if inferred]}


def digest(path):
    with path.open('rb') as source:
        value = hashlib.sha256()
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            value.update(chunk)
        return value.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trace', type=Path, required=True, help='Bounded Renode binary PC trace (at most 64 MiB).')
    parser.add_argument('--elf', type=Path, required=True, help='Exact ELF used by the traced Cortex-M node.')
    parser.add_argument('--reader', type=Path, required=True, help='Renode execution_tracer_reader.py path.')
    parser.add_argument('--python', default=sys.executable, help='Python interpreter with the trace reader dependencies.')
    parser.add_argument('--nm', default=shutil.which('arm-none-eabi-nm'), help='Matching arm-none-eabi-nm executable.')
    parser.add_argument('--output', type=Path, required=True, help='Write JSON attribution and source hashes here.')
    args = parser.parse_args()
    if not args.nm:
        parser.error('arm-none-eabi-nm is unavailable; provide --nm')
    if not 0 < args.trace.stat().st_size <= MAX_TRACE_BYTES:
        parser.error('trace is empty or exceeds the 64 MiB artifact limit')
    symbols = symbol_ranges(subprocess.check_output(
        [args.nm, '-S', '-n', '--defined-only', '--demangle', str(args.elf)], text=True).splitlines())
    with tempfile.TemporaryFile(mode='w+') as errors:
        process = subprocess.Popen([args.python, str(args.reader), 'inspect', str(args.trace)],
                                   stdout=subprocess.PIPE, stderr=errors, text=True)
        try:
            result = attribute(process.stdout, symbols)
        except BaseException:
            process.kill()
            process.wait()
            raise
        code = process.wait()
        if code:
            errors.seek(0)
            raise ValueError('PC trace decoder failed: ' + errors.read(8192))
    result.update(elf_sha256=digest(args.elf), trace_sha256=digest(args.trace))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({key: result[key] for key in ['instructions', 'unmapped', 'top_functions']}, indent=2))


if __name__ == '__main__':
    main()
