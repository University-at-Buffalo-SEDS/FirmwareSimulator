#!/usr/bin/env python3
"""Per-hop latency of operator parachute commands in a pyro_paths run (scripts/test-full-network.py --scenario
pyro_paths --radio-trace).

For every command in pyro.jsonl (virtual POST time) it finds, in the SDK-decoded radio traces:
  gs_uart   first GS UART write of that FLIGHT_COMMAND (station 1 TX)
  rf_uart   first arrival of that packet id at the RF board's UART (station 0 RX), direct or relayed
  fc_ack    the FC's end-to-end acknowledgement leaving the RF board's UART (station 0 TX)
  gs_ack    that acknowledgement arriving at the GS UART (station 1 RX)
The FC acknowledges from dispatch_flight_cmd once the command is queued to the priority-0 recovery thread, which
preempts at once, so the FC handler has run when the acknowledgement is built. FC execution therefore lies in
[rf_uart, fc_ack]; `exec_upper_ms` (POST -> fc_ack at the RF UART) is the conservative end-to-end figure.

Exits 1 if any path's exec_upper p95 or max exceeds --p95-bound-ms / --max-bound-ms, or a command never reached
the FC. Usage: analyze-pyro-latency.py RUN [--decoded out.json | --sdk-source DIR --groundstation-source DIR
--schema telemetry_config.json]
"""
import argparse, json, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIONS = {0: 'RF', 1: 'GS', 2: 'RRLS'}


def pct(xs, p):
    xs = sorted(x for x in xs if x is not None)
    return xs[min(len(xs) - 1, int(round((len(xs) - 1) * p / 100)))] if xs else None


def stats(xs):
    xs = [x for x in xs if x is not None]
    return {'n': len(xs), 'p50': pct(xs, 50), 'p95': pct(xs, 95), 'max': max(xs) if xs else None}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('run', type=Path)
    ap.add_argument('--decoded', type=Path)
    ap.add_argument('--sdk-source', type=Path)
    ap.add_argument('--groundstation-source', type=Path)
    ap.add_argument('--schema', type=Path)
    ap.add_argument('--p95-bound-ms', type=float, default=1000)
    ap.add_argument('--max-bound-ms', type=float, default=2000)
    ap.add_argument('--output', type=Path)
    a = ap.parse_args()
    decoded = a.decoded or a.run / 'radio-decoded.json'
    if not decoded.exists():
        cmd = [sys.executable, str(ROOT / 'scripts/decode-radio-trace.py'), str(a.run / 'diagnostics/radio-trace'),
               '--output', str(decoded), '--max-clock-backstep-us', '100']
        if a.sdk_source: cmd += ['--sdk-source', str(a.sdk_source)]
        if a.groundstation_source: cmd += ['--groundstation-source', str(a.groundstation_source)]
        if a.schema: cmd += ['--schema', str(a.schema)]
        subprocess.run(cmd, check=True)
    report = json.loads(decoded.read_text())
    streams = {}
    for s in report['streams']:
        name = Path(s['file']).name  # rocket_radio-station-<n>-<rx|tx>.bin
        st = int(name.split('-station-')[1].split('-')[0]); d = 'rx' if name.endswith('-rx.bin') else 'tx'
        streams[(st, d)] = sorted(s['packets'], key=lambda p: p['at_us'])
    rows = [json.loads(l) for l in (a.run / 'pyro.jsonl').read_text().splitlines() if l.strip()]
    cmds = [r for r in rows if r.get('kind') == 'pyro' and r.get('http') == 200]
    byte = {'DeployParachute': 12, 'ExpandParachute': 13}
    gs_tx, rf_rx, rf_tx, gs_rx = (streams.get(k, []) for k in ((1, 'tx'), (0, 'rx'), (0, 'tx'), (1, 'rx')))
    out = []
    for i, c in enumerate(cmds):
        t0 = c['v_post'] * 1000; t_next = cmds[i + 1]['v_post'] * 1000 if i + 1 < len(cmds) else float('inf')
        first = next((p for p in gs_tx if p['type'] == 'FLIGHT_COMMAND' and t0 <= p['at_us'] < t_next
                      and (p.get('managed_value') or [None])[0] == byte[c['cmd']]), None)
        row = {'path': c['path'], 'cmd': c['cmd'], 'v_post': c['v_post']}
        if first:
            pid = first['packet_id']
            row['gs_copies'] = sum(1 for p in gs_tx if p['packet_id'] == pid)
            rf = next((p for p in rf_rx if p['packet_id'] == pid and p['at_us'] >= first['at_us'] - 1000), None)
            ack = next((p for p in rf_tx if p.get('ack_for') == pid), None)
            gack = next((p for p in gs_rx if p.get('ack_for') == pid), None)
            ms = lambda p: round((p['at_us'] - t0) / 1000, 1) if p else None
            row.update(api_to_gs_uart_ms=ms(first), rf_uart_ms=ms(rf), exec_upper_ms=ms(ack), gs_ack_ms=ms(gack),
                       rf_copies=sum(1 for p in rf_rx if p['packet_id'] == pid))
        out.append(row)
    summary = {}
    for path in sorted({r['path'] for r in out}):
        rs = [r for r in out if r['path'] == path]
        summary[path] = {k: stats([r.get(k) for r in rs]) for k in ('api_to_gs_uart_ms', 'rf_uart_ms', 'exec_upper_ms', 'gs_ack_ms')}
        summary[path]['commands'] = len(rs)
        summary[path]['never_reached_fc'] = sum(1 for r in rs if r.get('exec_upper_ms') is None and r.get('rf_uart_ms') is None)
        summary[path]['mean_gs_copies'] = round(sum(r.get('gs_copies') or 0 for r in rs) / max(1, len(rs)), 2)
    failures = []
    for path, s in summary.items():
        e = s['exec_upper_ms']
        if s['never_reached_fc']: failures.append(f'{path}: {s["never_reached_fc"]} commands never reached the FC')
        if e['p95'] is None or e['p95'] > a.p95_bound_ms: failures.append(f'{path}: exec_upper p95 {e["p95"]} > {a.p95_bound_ms}')
        if e['max'] is None or e['max'] > a.max_bound_ms: failures.append(f'{path}: exec_upper max {e["max"]} > {a.max_bound_ms}')
    result = {'bounds_ms': {'p95': a.p95_bound_ms, 'max': a.max_bound_ms}, 'summary': summary, 'failures': failures, 'commands': out}
    text = json.dumps(result, indent=1)
    (a.output or a.run / 'pyro-latency.json').write_text(text + '\n')
    print(json.dumps({'summary': summary, 'failures': failures}, indent=1))
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
