#!/usr/bin/env python3
"""Long pad sit with a weather pressure ramp on the FC's production firmware (native SITL, FC alone).

The FC is built for the host with native/build_board.py and run by `firmware-sim native-bay` as fast as it goes.
Its barometer reads SIM_BARO_PA plus SIM_BARO_RAMP_PA_PER_H from boot (native/shim/src/sim_flight_sensors.c) and
its IMU rests at 1 g, so the vehicle is still on the pad the whole time. Once per virtual second the coordinator
samples the FC's own globals: the barometric altitude it reports (`meas.baro.alt`, what the estimator and the
flight state machine use), the pressure, and the ground reference (`baro_ref`, FlightComputer26 baro_reference.h,
when the build has it). A fixed reference (the boot zero, no compensation) is computed from the same pressures.

  python3 scripts/native_baro_drift.py --build build/native/fc/flight --hours 2 --ramp-hpa-h -3 --out evidence/x
  # a second build (for example dev's) to compare:
  python3 scripts/native_baro_drift.py --build A --compare B ...

Passes when |AGL| stays under --max-agl-m after the first minute; the fixed reference's error is reported.
Launch and flight need the station (fullnet scenarios); this covers the pad sit, where weather drift builds up.
"""
from __future__ import annotations

import argparse
import json
import math
import struct
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from native_engine import devices_text  # noqa: E402


def symbols(build: Path) -> dict[str, int]:
    out = {}
    for line in (build / f'{build.name}.symbols').read_text().splitlines():
        parts = line.split()
        if len(parts) == 3:
            out[parts[2]] = int(parts[0], 16)
    return out


def run(build: Path, out: Path, hours: float, pa: float, ramp: float, firmware_sim: Path, extra=()) -> list[dict]:
    man = json.loads((build / f'{build.name}.native.json').read_text())
    src = Path(man['src'])
    layout = json.loads((src / 'sim/board.json').read_text())
    out.mkdir(parents=True, exist_ok=True)
    (out / 'diag').mkdir(exist_ok=True)
    (out / 'scratch').mkdir(exist_ok=True)
    dev = out / 'flight.devices'
    dev.write_text(devices_text(layout))
    syms = symbols(build)
    probes = [('alt', 'meas', 12), ('prs', 'meas', 16)]
    if 'baro_ref' in syms:
        probes += [('ref', 'baro_ref', 0), ('boot', 'baro_ref', 4), ('frozen', 'baro_ref', 12)]
    for spec in extra:  # name=symbol+offset
        name, _, target = spec.partition('=')
        sym, _, off = target.partition('+')
        probes.append((name, sym, int(off or 0, 0)))
    topology = {'name': 'fc-pad-sit', 'quantum_seconds': 0.001, 'virtual_time_ms': int(hours * 3600_000) + 5_000,
                'nodes': [{'name': 'flight', 'layout': str(src / 'sim/board.json'), 'firmware_root': str(src)}],
                'links': []}
    native = {'nodes': {'flight': {'exe': man['exe'], 'flash_init': man.get('factory_bin'),
                                   'flash_size': (layout.get('memory') or {}).get('flash_size', 524288),
                                   'devices': str(dev), 'env': {'SIM_BARO_PA': str(pa), 'SIM_BARO_RAMP_PA_PER_H': str(ramp)}}},
              'pace': 0, 'scratch': str(out / 'scratch'), 'diagnostics': str(out / 'diag'),
              'probes': [{'node': 'flight', 'name': n, 'address': syms[s] + off} for n, s, off in probes]}
    (out / 'topology.json').write_text(json.dumps(topology, indent=1))
    (out / 'native.json').write_text(json.dumps(native, indent=1))
    with open(out / 'native-bay.log', 'w') as log:
        subprocess.run([str(firmware_sim), 'native-bay', '--topology', str(out / 'topology.json'),
                        '--native', str(out / 'native.json')], stdout=log, stderr=subprocess.STDOUT, check=False)
    probe_file = next((out / 'diag').rglob('memory-probes.txt'))
    # "SEDS_BAY_PROBE <node> <name> <second>" then the value, per probe and virtual second.
    by_second: dict[int, dict] = {}
    lines = probe_file.read_text().splitlines()
    for head, value in zip(lines[::2], lines[1::2]):
        parts = head.split()
        if len(parts) != 4 or parts[0] != 'SEDS_BAY_PROBE':
            continue
        word = int(value, 16) & 0xFFFFFFFF
        by_second.setdefault(int(parts[3]), {})[parts[2]] = struct.unpack('<f', struct.pack('<I', word))[0]
    names = [n for n, _, _ in probes]
    rows = [row for _, row in sorted(by_second.items()) if all(n in row for n in names)]
    return rows


def agl(p: float, p0: float) -> float:
    return 44330.0 * (1.0 - (p / p0) ** 0.1903)


def summarize(label: str, rows: list[dict], max_agl: float) -> dict:
    live = [r for r in rows if 30_000 < r['prs'] < 125_000]
    if len(live) < 120:
        raise SystemExit(f'{label}: only {len(live)} barometer samples probed')
    zero = live[0]['prs']
    settled = live[60:]
    worst = max(abs(r['alt']) for r in settled)
    fixed = agl(live[-1]['prs'], zero)
    out = {'label': label, 'samples': len(live), 'worst_abs_agl_m': round(worst, 3),
           'final_agl_m': round(live[-1]['alt'], 3), 'fixed_reference_final_agl_m': round(fixed, 2),
           'pressure_change_pa': round(live[-1]['prs'] - zero, 1)}
    if 'ref' in live[-1]:
        out['reference_shift_pa'] = round(live[-1]['ref'] - live[-1]['boot'], 1)
    out['pass'] = worst <= max_agl
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--build', type=Path, required=True, help='native/build_board.py output dir of the FC (…/flight)')
    ap.add_argument('--compare', type=Path, help='a second FC build to run the same way')
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--hours', type=float, default=2.0)
    ap.add_argument('--pa', type=float, default=100_000.0)
    ap.add_argument('--ramp-hpa-h', type=float, default=-3.0)
    ap.add_argument('--max-agl-m', type=float, default=0.5)
    ap.add_argument('--probe', action='append', default=[], help='extra float probe name=symbol+offset')
    ap.add_argument('--firmware-sim', type=Path, default=ROOT / 'target/release/firmware-sim')
    a = ap.parse_args()
    results = []
    for label, build in (('build', a.build), ('compare', a.compare)):
        if build is None:
            continue
        rows = run(build.resolve(), a.out / label, a.hours, a.pa, a.ramp_hpa_h * 100.0, a.firmware_sim, a.probe)
        results.append(summarize(f'{label} ({build})', rows, a.max_agl_m))
    (a.out / 'result.json').write_text(json.dumps(results, indent=1))
    for r in results:
        print(json.dumps(r))
    return 0 if results[0]['pass'] else 1


if __name__ == '__main__':
    sys.exit(main())
