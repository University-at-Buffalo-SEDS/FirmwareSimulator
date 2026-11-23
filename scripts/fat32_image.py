#!/usr/bin/env python3
"""Minimal FAT32 image writer for simulated SD cards (no mkfs/mtools needed, so it runs in a station container).

  build(path, size_bytes, {"IGT/BARO0.CSV": b"...", ...}, label="SIMSD")

8.3 upper-case names only (no long-name entries), directories one level deep, files written contiguously. The image is
a sparse file of `size_bytes` with an MBR-less volume (a "superfloppy", as most SD cards formatted by hosts and as
FileX opens at sector 0). `read(path)` lists the files back (used by the tests).
"""
from __future__ import annotations

import math
import os
import struct
from pathlib import Path

SECTOR = 512


def _83(name: str) -> bytes:
    base, _, ext = name.upper().partition(".")
    if not (1 <= len(base) <= 8 and len(ext) <= 3) or any(c in base + ext for c in ' "*+,/:;<=>?[\\]|'):
        raise ValueError(f"not an 8.3 name: {name}")
    return base.ljust(8).encode("ascii") + ext.ljust(3).encode("ascii")


def _entry(name83: bytes, attr: int, cluster: int, size: int) -> bytes:
    # 2026-01-01 00:00 timestamps
    date = ((2026 - 1980) << 9) | (1 << 5) | 1
    return struct.pack("<11sBBBHHHHHHHI", name83, attr, 0, 0, 0, date, date, cluster >> 16, 0, date, cluster & 0xFFFF, size)


def build(path: Path, size_bytes: int, files: dict[str, bytes], label: str = "SIMSD") -> None:
    sectors = size_bytes // SECTOR
    spc = 8 if size_bytes <= 8 << 30 else 64  # 4 KB clusters up to 8 GB
    reserved, nfats = 32, 2
    clusters_est = sectors // spc
    fat_sectors = math.ceil((clusters_est + 2) * 4 / SECTOR)
    data_start = reserved + nfats * fat_sectors
    clusters = (sectors - data_start) // spc
    if clusters < 65525:
        raise ValueError("too small for FAT32 (use at least 260 MB)")
    cbytes = spc * SECTOR
    # layout: cluster 2 = root; then directories; then files
    dirs = sorted({p.split("/")[0] for p in files if "/" in p})
    fat = [0x0FFFFFF8, 0x0FFFFFFF]
    data: dict[int, bytes] = {}

    def alloc(nbytes: int) -> int:
        n = max(1, math.ceil(nbytes / cbytes))
        first = len(fat)
        for i in range(n):
            fat.append(first + i + 1 if i < n - 1 else 0x0FFFFFFF)
        return first

    root = alloc(cbytes)
    dir_clusters = {d: alloc(cbytes) for d in dirs}
    entries: dict[str | None, list[bytes]] = {None: [_entry(label.upper().ljust(11)[:11].encode("ascii"), 0x08, 0, 0)]}
    for d in dirs:
        entries[None].append(_entry(_83(d), 0x10, dir_clusters[d], 0))
        entries[d] = [_entry(b".          ", 0x10, dir_clusters[d], 0), _entry(b"..         ", 0x10, 0, 0)]
    for p, content in sorted(files.items()):
        d, _, name = p.rpartition("/")
        cl = alloc(len(content)) if content else 0
        if content:
            data[cl] = content
        entries[d or None].append(_entry(_83(name), 0x20, cl, len(content)))
    for key, ents in entries.items():
        blob = b"".join(ents)
        if len(blob) > cbytes:
            raise ValueError("directory too large for one cluster")
        data[root if key is None else dir_clusters[key]] = blob
    if len(fat) > clusters + 2:
        raise ValueError("files do not fit")
    vol_id = 0x5EED5D01
    bpb = struct.pack("<3s8sHBHBHHBHHHII", b"\xEB\x58\x90", b"SEDSSIM ", SECTOR, spc, reserved, nfats, 0, 0, 0xF8, 0, 63, 255, 0, sectors)
    bpb += struct.pack("<IHHIHH12sBBBI11s8s", fat_sectors, 0, 0, root, 1, 6, b"\0" * 12, 0x80, 0, 0x29, vol_id,
                       label.upper().ljust(11)[:11].encode("ascii"), b"FAT32   ")
    boot = bpb.ljust(510, b"\0") + b"\x55\xAA"
    free = clusters + 2 - len(fat)
    fsinfo = struct.pack("<I480sI", 0x41615252, b"\0" * 480, 0x61417272) + struct.pack("<II12sI", free, len(fat), b"\0" * 12, 0xAA550000)
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "wb") as f:
        f.truncate(sectors * SECTOR)
        for base in (0, 6):  # boot sector and its backup
            f.seek((base) * SECTOR); f.write(boot)
            f.seek((base + 1) * SECTOR); f.write(fsinfo)
            f.seek((base + 2) * SECTOR); f.write(b"\0" * 510 + b"\x55\xAA")
        table = struct.pack(f"<{len(fat)}I", *fat)
        for i in range(nfats):
            f.seek((reserved + i * fat_sectors) * SECTOR); f.write(table)
        for cl, blob in data.items():
            f.seek((data_start + (cl - 2) * spc) * SECTOR); f.write(blob)
    os.replace(tmp, path)


def read(path: Path) -> dict[str, bytes]:
    """Files in the image (root and one directory level), for checks."""
    with open(path, "rb") as f:
        b = f.read(SECTOR)
        spc, reserved, nfats = b[13], struct.unpack_from("<H", b, 14)[0], b[16]
        fat_sectors, root = struct.unpack_from("<I", b, 36)[0], struct.unpack_from("<I", b, 44)[0]
        f.seek(reserved * SECTOR)
        fat = f.read(fat_sectors * SECTOR)
        data_start = reserved + nfats * fat_sectors

        def chain(cl: int) -> bytes:
            out = b""
            while 2 <= cl < 0x0FFFFFF8:
                f.seek((data_start + (cl - 2) * spc) * SECTOR)
                out += f.read(spc * SECTOR)
                cl = struct.unpack_from("<I", fat, cl * 4)[0] & 0x0FFFFFFF
            return out

        def entries(cl: int):
            blob = chain(cl)
            for i in range(0, len(blob), 32):
                e = blob[i:i + 32]
                if e[0] == 0:
                    break
                name = e[:8].decode().strip() + ("." + e[8:11].decode().strip() if e[8:11].strip() else "")
                yield name, e[11], (struct.unpack_from("<H", e, 20)[0] << 16) | struct.unpack_from("<H", e, 26)[0], struct.unpack_from("<I", e, 28)[0]

        out = {}
        for name, attr, cl, size in entries(root):
            if attr & 0x08 or name in (".", ".."):
                continue
            if attr & 0x10:
                for n2, a2, c2, s2 in entries(cl):
                    if not (a2 & 0x10):
                        out[f"{name}/{n2}"] = chain(c2)[:s2]
            else:
                out[name] = chain(cl)[:size]
        return out


if __name__ == "__main__":
    import sys, tempfile
    with tempfile.TemporaryDirectory() as d:
        img = Path(d) / "sd.img"
        files = {"IGT/BARO0.CSV": b"t,p\n0,101325\n" * 1000, "IGT/IMU0.CSV": b"", "README.TXT": b"sim"}
        build(img, 1 << 30, files)
        back = read(img)
        assert back == files, (sorted(back), sorted(files))
        print("ok", os.stat(img).st_blocks * 512 // 1024, "KB on disk")
        sys.exit(0)
