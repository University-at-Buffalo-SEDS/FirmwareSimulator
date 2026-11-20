#!/usr/bin/env python3
"""A small FAT16 SD-card image (no partition table) for the native engine.

The native SD shim (native/shim/src/hal_storage.c) serves <state>/sdcard.img
block for block, and FileX mounts it like the FC's card. This writes a fresh
FAT16 volume holding the given files (8.3 names, one directory level), so a
scenario can hand the FC its integrated-ground-test CSVs (/IGT/*.CSV) before
it boots. Standard library only; no mkfs/mtools needed.

    python3 scripts/fat_image.py out.img IGT/IMU0.CSV=path/IMU0.CSV ...
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

SECTOR = 512
SPC = 4                  # sectors per cluster (2 KiB)
RESERVED = 1
FATS = 2
ROOT_ENTRIES = 512
DATE = ((2026 - 1980) << 9) | (1 << 5) | 1   # 2026-01-01
TIME = 0


def short_name(name: str) -> bytes:
    base, _, ext = name.upper().partition('.')
    if not base or len(base) > 8 or len(ext) > 3 or not (base + ext).replace('_', '').isalnum():
        raise ValueError(f'not an 8.3 name: {name}')
    return base.ljust(8).encode() + ext.ljust(3).encode()


def entry(name: bytes, attr: int, cluster: int, size: int) -> bytes:
    return struct.pack('<11sBBBHHHHHHHL', name, attr, 0, 0, TIME, DATE, DATE, 0, TIME, DATE, cluster, size)


def build(path: Path, files: dict[str, bytes], size_mb: int = 64) -> None:
    total = size_mb * 1024 * 1024 // SECTOR
    root_sectors = ROOT_ENTRIES * 32 // SECTOR
    fat_sectors = 1
    while True:  # smallest FAT covering every data cluster
        clusters = (total - RESERVED - root_sectors - FATS * fat_sectors) // SPC
        if (clusters + 2) * 2 <= fat_sectors * SECTOR:
            break
        fat_sectors += 1
    if not 4085 <= clusters < 65525:
        raise ValueError(f'{size_mb} MiB gives {clusters} clusters: not FAT16')
    data_start = RESERVED + FATS * fat_sectors + root_sectors
    fat = [0xFFF8, 0xFFFF] + [0] * clusters
    image = bytearray(data_start * SECTOR)
    data = bytearray()
    next_cluster = 2

    def alloc(payload: bytes) -> int:
        nonlocal next_cluster
        if not payload:
            return 0
        count = -(-len(payload) // (SPC * SECTOR))
        first = next_cluster
        for i in range(count):
            c = first + i
            fat[c] = c + 1 if i + 1 < count else 0xFFFF
        next_cluster += count
        data.extend(payload.ljust(count * SPC * SECTOR, b'\0'))
        return first

    dirs: dict[str, list[tuple[str, bytes]]] = {}
    root_files: list[tuple[str, bytes]] = []
    for name, payload in sorted(files.items()):
        parts = name.strip('/').split('/')
        if len(parts) == 1:
            root_files.append((parts[0], payload))
        elif len(parts) == 2:
            dirs.setdefault(parts[0], []).append((parts[1], payload))
        else:
            raise ValueError(f'one directory level only: {name}')

    root = bytearray()
    root += entry(b'SEDS SD    ', 0x08, 0, 0)  # volume label
    for name, payload in root_files:
        root += entry(short_name(name), 0x20, alloc(payload), len(payload))
    for dname, items in sorted(dirs.items()):
        # The directory's own cluster(s) come first so '.' can name them.
        size = (2 + len(items)) * 32
        count = -(-size // (SPC * SECTOR))
        first = next_cluster
        listing = bytearray(entry(b'.          ', 0x10, first, 0) + entry(b'..         ', 0x10, 0, 0))
        placeholder = len(data)
        alloc(b'\0' * count * SPC * SECTOR)
        for fname, payload in items:
            listing += entry(short_name(fname), 0x20, alloc(payload), len(payload))
        data[placeholder:placeholder + len(listing)] = listing
        root += entry(short_name(dname), 0x10, first, 0)
    if len(root) > ROOT_ENTRIES * 32:
        raise ValueError('root directory full')
    if next_cluster - 2 > clusters:
        raise ValueError('image too small')

    boot = bytearray(SECTOR)
    boot[0:3] = b'\xEB\x3C\x90'
    boot[3:11] = b'MSDOS5.0'
    struct.pack_into('<HBHBHHBHHHLL', boot, 11, SECTOR, SPC, RESERVED, FATS, ROOT_ENTRIES,
                     0 if total > 0xFFFF else total, 0xF8, fat_sectors, 63, 255, 0,
                     total if total > 0xFFFF else 0)
    struct.pack_into('<BBBL11s8s', boot, 36, 0x80, 0, 0x29, 0x5345_4453, b'SEDS SD    ', b'FAT16   ')
    boot[510:512] = b'\x55\xAA'
    image[0:SECTOR] = boot
    fat_bytes = struct.pack(f'<{len(fat)}H', *fat).ljust(fat_sectors * SECTOR, b'\0')
    for i in range(FATS):
        start = (RESERVED + i * fat_sectors) * SECTOR
        image[start:start + len(fat_bytes)] = fat_bytes
    root_start = (RESERVED + FATS * fat_sectors) * SECTOR
    image[root_start:root_start + len(root)] = root
    with path.open('wb') as handle:
        handle.write(image)
        handle.write(data)
        handle.truncate(total * SECTOR)  # sparse tail


def read_files(path: Path) -> dict[str, bytes]:
    """Inverse of build(), for tests: every file one directory level deep."""
    raw = path.read_bytes()
    (bps, spc, reserved, fats, root_entries, total16, _media, fat_sz) = struct.unpack_from('<HBHBHHBH', raw, 11)
    fat = struct.unpack_from(f'<{fat_sz * bps // 2}H', raw, reserved * bps)
    root_start = (reserved + fats * fat_sz) * bps
    data_start = root_start + root_entries * 32

    def chain(cluster: int) -> bytes:
        out = bytearray()
        while 2 <= cluster < 0xFFF8:
            off = data_start + (cluster - 2) * spc * bps
            out += raw[off:off + spc * bps]
            cluster = fat[cluster]
        return bytes(out)

    def listing(blob: bytes, prefix: str, out: dict[str, bytes]) -> None:
        for i in range(0, len(blob), 32):
            e = blob[i:i + 32]
            if e[0] in (0, 0xE5) or e[11] & 0x08:
                if e[0] == 0:
                    break
                continue
            name = e[0:8].decode().rstrip() + ('.' + e[8:11].decode().rstrip() if e[8:11].strip() else '')
            if name in ('.', '..'):
                continue
            cluster, size = struct.unpack_from('<HL', e, 26)
            if e[11] & 0x10:
                listing(chain(cluster), prefix + name + '/', out)
            else:
                out[prefix + name] = chain(cluster)[:size]

    files: dict[str, bytes] = {}
    listing(raw[root_start:data_start], '', files)
    return files


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    files = {}
    for spec in argv[1:]:
        name, _, src = spec.partition('=')
        files[name] = Path(src).read_bytes()
    build(Path(argv[0]), files)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
