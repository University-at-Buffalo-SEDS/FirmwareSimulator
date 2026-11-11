#!/usr/bin/env python3
"""Per-stream continuity at three stages: source (board timestamps), GS receipt (recording DB) and UI (WebSocket rows).

usage: analyze-telemetry-continuity.py RUN_DIR [WS.jsonl] [--skip-ms N] [--span-ms N] [--json OUT] [--assert-targets]
Rows come from the run's GS recording DB (start recording in the run), optional WebSocket rows from a JSONL capture
of TelemetryBatch rows. Times are GS clock ms (time-dilated in --faithful-time runs, so they read as virtual ms).
--assert-targets: 5 Hz AV-bay streams gap p95 <= 500 ms and >= 95 % delivered, 1 Hz streams <= 1.5 s and >= 98 %.
"""
import argparse, glob, json, shutil, sqlite3, tempfile
from collections import defaultdict
from pathlib import Path

STREAMS = [('FC', 'ACCEL_DATA'), ('FC', 'GYRO_DATA'), ('FC', 'BAROMETER_DATA'), ('PB', 'BATTERY_VOLTAGE'),
           ('PB', 'BATTERY_CURRENT'), ('RF', 'GPS_SATELLITE_NUMBER'), ('RF', 'GPS_STATUS'), ('RF', 'BATTERY_VOLTAGE'),
           ('DAQ', 'KG1000'), ('DAQ', 'DAQ_SAR_VOLTAGES'), ('VB', 'FUEL_TANK_PRESSURE'), ('GB', 'BATTERY_VOLTAGE')]


def pct(a, p):
    a = sorted(a)
    return a[min(len(a) - 1, int(p * len(a)))] if a else None


def gaps(ts):
    ts = sorted(ts)
    return [b - a for a, b in zip(ts, ts[1:])]


def load_db(run):
    tmp = Path(tempfile.mkdtemp()); rows = []
    for db in sorted(glob.glob(str(run / 'gs-data' / 'groundstation_recording_*.db'))):
        for suf in ('', '-wal', '-shm'):
            if Path(db + suf).exists(): shutil.copyfile(db + suf, tmp / (Path(db).name + suf))
        con = sqlite3.connect(str(tmp / Path(db).name))
        rows += con.execute('select timestamp_ms, source_timestamp_ms, sender_id, data_type from telemetry').fetchall()
        con.close()
    shutil.rmtree(tmp)
    return rows


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('run', type=Path); ap.add_argument('ws', nargs='?', type=Path)
    ap.add_argument('--skip-ms', type=int, default=30000, help='ignore the first ms of the recording (startup)')
    ap.add_argument('--span-ms', type=int, default=0); ap.add_argument('--json', type=Path)
    ap.add_argument('--assert-targets', action='store_true', help='exit 1 unless the continuity targets hold')
    a = ap.parse_args()
    rows = load_db(a.run)
    t0 = min(r[0] for r in rows) + a.skip_ms
    t1 = (t0 + a.span_ms) if a.span_ms else max(r[0] for r in rows)
    db = defaultdict(list)
    for ts, src, snd, ty in rows:
        if t0 <= ts <= t1: db[(snd, ty)].append((ts, src))
    ws = defaultdict(list)
    if a.ws and a.ws.exists():
        for line in a.ws.open():
            r = json.loads(line)
            if r['ts'] is not None and t0 <= r['ts'] <= t1: ws[(r['sender'], r['type'])].append(r)
    result = {}
    print(f'window {t1 - t0} ms')
    print(f"{'stream':30} {'src_per':>7} {'expect':>6} {'rx_n':>5} {'deliv%':>6} {'rx_p50':>6} {'rx_p95':>6} {'rx_max':>6} "
          f"{'lat_p50':>7} {'lat_p95':>7} {'ws_n':>5} {'ws_p95':>6}")
    for key in STREAMS:
        v = db.get(key, [])
        src = [s for _, s in v if s]
        sp = pct(gaps(src), .5) if len(src) > 2 else None
        expect = round((t1 - t0) / sp) if sp else None
        rxg = gaps([t for t, _ in v])
        lat = [t - s for t, s in v if s]
        w = ws.get(key, [])
        # WebSocket arrival on the virtual clock when captured (wall time runs --faithful-time slower).
        wg = gaps([r['v'] if r.get('v') is not None else r['wall'] * 1000 for r in w])
        row = dict(src_period=sp, expected=expect, rx=len(v), delivered=round(100 * len(v) / expect, 1) if expect else None,
                   rx_p50=pct(rxg, .5), rx_p95=pct(rxg, .95), rx_max=max(rxg) if rxg else None,
                   lat_p50=pct(lat, .5), lat_p95=pct(lat, .95), ws=len(w), ws_p95_wall=round(pct(wg, .95)) if wg else None)
        result['/'.join(key)] = row
        print(f"{'/'.join(key):30} {str(sp):>7} {str(expect):>6} {len(v):5} {str(row['delivered']):>6} {str(row['rx_p50']):>6} "
              f"{str(row['rx_p95']):>6} {str(row['rx_max']):>6} {str(row['lat_p50']):>7} {str(row['lat_p95']):>7} {len(w):5} {str(row['ws_p95_wall']):>6}")
    if a.json: a.json.write_text(json.dumps(result, indent=2))
    if a.assert_targets:
        # Targets: 5 Hz AV-bay streams gap p95 <= 2.5 periods (500 ms) and >= 95% delivered;
        # 1 Hz AV-bay streams gap p95 <= 1.5 s and >= 98% delivered.
        fails = []
        for key, (p95, deliv) in {'FC/ACCEL_DATA': (500, 95), 'FC/BAROMETER_DATA': (500, 95), 'PB/BATTERY_VOLTAGE': (1500, 98),
                                  'RF/GPS_STATUS': (1500, 98)}.items():
            r = result.get(key) or {}
            if r.get('rx_p95') is None or r['rx_p95'] > p95 or (r.get('delivered') or 0) < deliv:
                fails.append((key, r.get('rx_p95'), r.get('delivered')))
        print('CONTINUITY TARGETS', 'PASS' if not fails else f'FAIL {fails}')
        raise SystemExit(1 if fails else 0)


if __name__ == '__main__':
    main()
