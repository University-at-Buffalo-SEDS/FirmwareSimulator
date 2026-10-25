#!/usr/bin/env python3
"""Bounded read-only observer executed inside one simulator container."""
import argparse
import glob
import json
import os
import sys
from pathlib import Path
import time
import urllib.request

MAX_BYTES = 4 * 1024 * 1024


class ClockInput:
    """Bounded, fragmented newline framing for a source file without a newline."""
    def __init__(self, fd):
        self.fd = fd
        self.pending = b''
        self.latest = None
        os.set_blocking(fd, False)

    def poll(self):
        closed = False
        for _ in range(16):
            try:
                chunk = os.read(self.fd, 4096)
            except BlockingIOError:
                break
            if not chunk:
                closed = True
                break
            self.pending += chunk
            lines = self.pending.split(b'\n')
            self.pending = lines.pop()
            if len(self.pending) > 32 or any(len(line) > 32 for line in lines):
                raise RuntimeError('clock line exceeds bound')
            for line in lines:
                if line.strip():
                    self.latest = int(line)
        return self.latest, closed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--primary-port', type=int, required=True)
    parser.add_argument('--rrls-port', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--until-ms', type=int, default=56000)
    parser.add_argument('--timeout', type=int, default=1800)
    parser.add_argument('--clock-stdin', action='store_true')
    args = parser.parse_args()
    deadline = time.monotonic() + args.timeout
    total = 0
    clock = ClockInput(sys.stdin.fileno()) if args.clock_stdin else None
    with args.output.open('xb') as output:
        while time.monotonic() < deadline:
            now = None
            if clock:
                now, closed = clock.poll()
                if closed and (now is None or now < args.until_ms):
                    raise RuntimeError('clock stream ended before complete observation')
            else:
                paths = set(glob.glob('/tmp/.tmp*/virtual-clock.txt') + glob.glob('/tmp/*/virtual-clock.txt'))
                if len(paths) > 1:
                    raise RuntimeError('ambiguous simulator clock files')
                if paths:
                    try:
                        now = int(Path(next(iter(paths))).read_text().strip())
                    except (ValueError, OSError):
                        pass  # Explicit null records preserve truncate/write races.
            row = {'virtual_ms': now}
            for name, port in [('primary', args.primary_port), ('rrls', args.rrls_port)]:
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/rrls/status', timeout=0.5) as response:
                        raw = response.read(4097)
                    if len(raw) > 4096:
                        raise RuntimeError('status response exceeds bound')
                    row[name] = json.loads(raw)
                except (OSError, ValueError) as error:
                    row[name] = None
                    row[name + '_error'] = type(error).__name__
            encoded = (json.dumps(row, separators=(',', ':')) + '\n').encode()
            if total + len(encoded) > MAX_BYTES:
                raise RuntimeError('observer evidence cap reached; NOT a complete capture')
            output.write(encoded)
            output.flush()
            total += len(encoded)
            if now is not None and now >= args.until_ms:
                return
            time.sleep(0.5)
    raise TimeoutError('observer did not reach its virtual-time endpoint')


if __name__ == '__main__':
    main()
