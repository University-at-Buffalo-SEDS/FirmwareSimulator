#!/usr/bin/env python3
"""Parity between the native SITL engine and Renode for one fullnet scenario (docs/native-sitl.md, Parity).

  # compare two finished runs
  python3 scripts/native_parity.py --renode RUN_DIR --native RUN_DIR [--out parity.json]
  # re-run a finished Renode run natively with the same controller arguments, then compare
  python3 scripts/native_parity.py --renode RUN_DIR --run-native EVIDENCE_DIR [native_engine options...]

Compared: every check's pass/fail (by name), and the numeric metrics the checks record (durations, reach and
confirmation times, clock-spread medians, vent/countdown times) within tolerances. Metrics listed in
EXPECTED_DIFFERENCES are reported but never fail parity (see the docs for why they legitimately differ).
"""
from __future__ import annotations
import argparse, json, re, shlex, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# metric key (last path element, or a regex on the full path) -> (absolute tolerance, relative tolerance)
TOLERANCES = [
    (r'(^|/)median_ms$', (60, 0.0)),          # board stamp offset vs station, typical: a few ms both engines
    (r'(^|/)max_ms$', (60, 0.0)),
    (r'(^|/)reached_ms$', (2000, 0.0)),       # station reads the operator UTC (bound 5000 ms)
    (r'(^|/)duration_ms$', (3000, 1.0)),      # recovery operations: station poll cadence dominates
    (r'(^|/)confirm_ms$', (3000, 1.0)),
    (r'(^|/)worst$', (250, 0.0)),             # network gap through a window
    (r'(^|/)vent_v_s$', (3, 0.0)),
    (r'(^|/)launch_v_ms$', (15000, 0.0)),
    (r'first_counting/0/0$', (3, 0.0)),       # countdown start second
    (r'first_counting/0/1$', (2048, 0.0)),    # countdown word: remaining seconds in bits 8.. (+-8 s)
]
# Never fail parity: transport tails, sample counts, allocator/stack figures, wall-clock-stamped fields.
ONE_SIDED = [r"(^|/)max_ms$"]  # most-ahead board stamp: native may be tighter than Renode, never worse by > tolerance
EXPECTED_DIFFERENCES = [
    r'(^|/)min_ms$', r'(^|/)max_abs_ms$', r'(^|/)packets$', r'(^|/)samples$', r'(^|/)age_ms$', r'(^|/)v$',
    r'received_age_ms', r'(^|/)window_wall', r'(^|/)utc_ms$', r'(^|/)network_utc_ms$', r'timestamp_ms', r'_age_ms$',
    r'(^|/)id$', r'stack', r'heap', r'tlsf', r'pool', r'(^|/)n$', r'(^|/)gaps/', r'(^|/)net_gap_ms/', r'attempts/',
    r'last_step/', r'refused_reports', r'last_admitted', r'last_received', r'stale_for_ms', r'daq_clock_offset_ms',
]


def numeric_leaves(obj, path=''):
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        yield path, float(obj); return
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from numeric_leaves(v, f'{path}/{k}' if path else str(k))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj[:64]):
            yield from numeric_leaves(v, f'{path}/{i}' if path else str(i))


def checks(run: Path):
    r = json.loads((run / 'result.json').read_text())
    out = {}
    for c in r.get('checks', []):
        name = c['name']; k = name; n = 2
        while k in out: k = f'{name} #{n}'; n += 1
        out[k] = c
    return r, out


def tolerance(path):
    for pat in EXPECTED_DIFFERENCES:
        if re.search(pat, path): return None
    for pat, tol in TOLERANCES:
        if re.search(pat, path): return tol
    return 'untracked'


def compare(renode: Path, native: Path):
    rr, rc = checks(renode); nr, nc = checks(native)
    rows = []; ok = True
    for name in list(rc) + [n for n in nc if n not in rc]:
        a, b = rc.get(name), nc.get(name)
        row = {'check': name, 'renode': a['pass'] if a else None, 'native': b['pass'] if b else None, 'metrics': []}
        row['pass_match'] = row['renode'] == row['native']
        if a and b:
            am = dict(numeric_leaves({k: v for k, v in a.items() if k not in ('name', 'pass', 'v')}))
            bm = dict(numeric_leaves({k: v for k, v in b.items() if k not in ('name', 'pass', 'v')}))
            for key in sorted(set(am) & set(bm)):
                tol = tolerance(key)
                if tol == 'untracked' or tol is None:
                    if tol is None and abs(am[key] - bm[key]) > 0:
                        row['metrics'].append({'metric': key, 'renode': am[key], 'native': bm[key], 'expected_difference': True})
                    continue
                absol, rel = tol
                limit = max(absol, rel * abs(am[key]))
                within = (bm[key] - am[key] <= limit) if any(re.search(p, key) for p in ONE_SIDED) else abs(am[key] - bm[key]) <= limit
                row['metrics'].append({'metric': key, 'renode': am[key], 'native': bm[key], 'within': within, 'tolerance': [absol, rel]})
                if not within: row.setdefault('metric_mismatch', []).append(key)
        if not row['pass_match'] or row.get('metric_mismatch'):
            ok = False
        rows.append(row)
    summary = {'renode': {'run': str(renode), 'passed': rr.get('passed'), 'checks': len(rc), 'virtual_ms': rr.get('virtual_ms'), 'wall_s': rr.get('wall_s')},
               'native': {'run': str(native), 'passed': nr.get('passed'), 'checks': len(nc), 'virtual_ms': nr.get('virtual_ms'), 'wall_s': nr.get('wall_s')},
               'pass_fail_matches': sum(1 for r in rows if r['pass_match']), 'checks_compared': len(rows),
               'metrics_compared': sum(1 for r in rows for m in r['metrics'] if 'within' in m),
               'metrics_outside_tolerance': [(r['check'], k) for r in rows for k in r.get('metric_mismatch', [])],
               'parity': ok}
    nrep = native / 'diagnostics/native-report.json'
    if nrep.exists():
        summary['native']['engine'] = json.loads(nrep.read_text())
    if rr.get('wall_s') and nr.get('wall_s') and rr.get('virtual_ms') and nr.get('virtual_ms'):
        summary['speedup_wall_per_virtual'] = round((rr['wall_s'] / rr['virtual_ms']) / (nr['wall_s'] / nr['virtual_ms']), 1)
    return summary, rows


def renode_command(renode: Path):
    """The controller arguments of a finished Renode run (its notes' command line, else args.json)."""
    notes = renode.parent / (renode.name.rsplit('-a', 1)[0] + '-notes.txt')
    if notes.exists():
        for line in notes.read_text().splitlines():
            if 'test-full-network.py' in line:
                argv = shlex.split(line)
                argv = argv[argv.index(next(x for x in argv if x.endswith('test-full-network.py'))) + 1:]
                return [x for x in argv]
    a = json.loads((renode / 'args.json').read_text())
    raise SystemExit(f'no notes command line for {renode}; pass the controller arguments explicitly ({sorted(a)})')


def replace_arg(argv, name, value):
    out = []; skip = False
    for i, x in enumerate(argv):
        if skip: skip = False; continue
        if x == name: skip = True; continue
        if x.startswith(name + '='): continue
        out.append(x)
    return out + [name, value]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--renode', type=Path, required=True)
    ap.add_argument('--native', type=Path)
    ap.add_argument('--run-native', type=Path, help='evidence dir for a native re-run with the Renode run\'s arguments')
    ap.add_argument('--out', type=Path)
    a, engine_opts = ap.parse_known_args()
    native = a.native
    if a.run_native:
        argv = replace_arg(renode_command(a.renode), '--evidence', str(a.run_native))
        cmd = [sys.executable, str(HERE / 'native_engine.py'), *engine_opts, *argv]
        print('[parity] native run:', shlex.join(cmd), flush=True)
        subprocess.run(cmd, check=False)
        native = a.run_native
    summary, rows = compare(a.renode, native)
    out = a.out or (native / 'parity.json')
    out.write_text(json.dumps({'summary': summary, 'checks': rows}, indent=1))
    print(json.dumps(summary, indent=1))
    for r in rows:
        flag = 'OK ' if r['pass_match'] and not r.get('metric_mismatch') else 'DIFF'
        print(f"{flag} renode={r['renode']!s:5} native={r['native']!s:5} {r['check'][:110]}" + (f"  metrics: {r['metric_mismatch']}" if r.get('metric_mismatch') else ''))
    sys.exit(0 if summary['parity'] else 1)


if __name__ == '__main__':
    main()
