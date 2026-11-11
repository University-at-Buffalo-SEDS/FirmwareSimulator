#!/usr/bin/env python3
"""FC -> GS flight-state transport test on the full 7-board bay without the fill procedure.

The FC image must be built with scripts/fixtures/fc-sim-self-launch.patch, a
sim-only hook that is never part of firmware: it queues Postinit at
SIM_SELF_LAUNCH_MS-5 s and Launch at SIM_SELF_LAUNCH_MS like accepted network
commands. The flight profile is anchored 0.5 s after that.

PASS requires the station to end on the FC's own Landed/Recovery report (not
the comms-loss fallback) and to have raised both deployment notifications. The harness records, per FC state change, what the GS
reports (flightstate, FC messages, FLIGHT_STATE rows in its recording DB) and
the RF radio TX drop counters, so a lost report can be attributed to a hop.

usage: fc-transport-sim.py EVIDENCE BUILD_DIR [launch_ms] [duration_ms] [extra profile args...]
"""
import importlib.util, json, subprocess, sys, time, argparse
from pathlib import Path
HERE = Path(__file__).parent
SIM = HERE / 'test-operational-flight-sim.py'
spec = importlib.util.spec_from_file_location('fs', SIM); fs = importlib.util.module_from_spec(spec)
sys.argv, saved = [str(SIM)], sys.argv; spec.loader.exec_module(fs); sys.argv = saved
ev, build = Path(saved[1]), Path(saved[2])
launch_ms = int(saved[3]) if len(saved) > 3 else 90000
duration = int(saved[4]) if len(saved) > 4 else 330000
profile_args = saved[5:] or ['--drogue-vt', '30', '--main-vt', '12', '--tail-s', '60']
args = argparse.Namespace(evidence=ev, cohort=build / 'cohort', image=(build / 'IMAGE-ID').read_text().strip(),
                          duration_ms=duration, tiles=None, cpus='12', memory='16g', gps_utc_offset_ms=-5000,
                          profile_arg=profile_args, wall_freshness_scale=30)
out, diag, key, command, network, profile, missing = fs.prepare(args)
config = out / 'config'
RF = [('rf_tx_drops', 'g_tx_drops'), ('rf_tx_drop_oldest', 'g_tx_drop_oldest'), ('rf_tx_drop_stale', 'g_tx_drop_stale'),
      ('rf_tx_drop_control', 'g_tx_drop_control'), ('rf_tx_drop_network_control', 'g_tx_drop_network_control'),
      ('rf_tx_drop_application', 'g_tx_drop_application'), ('rf_tx_drop_heartbeat', 'g_tx_drop_heartbeat'),
      ('rf_tx_drop_discovery', 'g_tx_drop_discovery'), ('rf_radio_tx_ok', 'g_radio_tx_ok'),
      ('rf_tx_queue_high_water', 'g_tx_queue_high_water'), ('rf_rx_isr_drops', 'g_rx_isr_drops'),
      ('rf_radio_rx_frames_ok', 'g_radio_rx_frames_ok'), ('rf_fsc_ignored', 'g_flight_state_cache_reports_ignored'),
      ('rf_fsc_errors', 'g_flight_state_cache_errors')]
FC = [('fc_fsc_ignored', 'g_flight_state_cache_reports_ignored'), ('fc_fsc_errors', 'g_flight_state_cache_errors'), ('fc_queue_errors', 'g_telemetry_queue_errors'), ('fc_last_queue_result', 'g_telemetry_last_queue_result'), ('fc_link_backpressure', 'g_telemetry_link_backpressure')]
for name, repo, probes in (('rf', 'RFBoard26', RF), ('flight', 'FlightComputer26', FC)):
    layout = json.loads((config / f'{name}.json').read_text())
    symbols = fs.nm_symbols(args.cohort / repo, layout)
    miss = []
    fs.add_probes(layout, symbols, [(n, s, 0) for n, s in probes], miss)
    print(name, 'missing probes', miss, flush=True)
    (config / f'{name}.json').write_text(json.dumps(layout))
fs.atomic(diag / 'gps.fix', '1')
fs.atomic(diag / 'flight' / 'launch-anchor.txt', str(launch_ms + 500))
subprocess.run(['docker', 'network', 'create', '--internal', network], check=True, capture_output=True)
proc = subprocess.Popen(command, stdout=(out / 'simulator.log').open('w'), stderr=subprocess.STDOUT)
tail = fs.ProbeTail(diag)
timeline = (out / 'transport-timeline.jsonl').open('a')
token = origin = fwd = None
fc_state = gs_state = None
messages_seen = set()
result = {'fc_states': [], 'gs_states': [], 'fc_messages': []}
try:
    while proc.poll() is None:
        time.sleep(2)
        clocks = list((diag / 'temp').glob('*/virtual-clock.txt')); logs = list((diag / 'temp').glob('*/groundstation-*log'))
        if not clocks or not logs: continue
        try: now = int(clocks[0].read_text())
        except ValueError: continue
        if origin is None:
            if 'HTTP listening on' not in '\n'.join(p.read_text(errors='replace') for p in logs): continue
            info = json.loads(subprocess.check_output(['docker', 'inspect', command[command.index('--name') + 1]], text=True))[0]
            fwd = fs.Forwarder(info['NetworkSettings']['Networks'][network]['IPAddress']); origin = fwd.origin
            for _ in range(50):
                try: token = fs.login(origin, key); break
                except Exception: time.sleep(.5)
        tail.poll()
        sample, row = tail.latest('flight')
        _, rf = tail.latest('rf')
        if row and 'fc_state' in row:
            s = fs.FC_LOCAL[row['fc_state'] & 0xff] if (row['fc_state'] & 0xff) < len(fs.FC_LOCAL) else row['fc_state']
            if s != fc_state:
                fc_state = s
                rec = {'virtual_ms': now, 'probe_sample': sample, 'fc_state': s, 'rf': rf, 'fc': {k: row.get(k) for k in row if k.startswith(('fc_fsc','fc_queue','fc_last_queue','fc_link'))}}
                result['fc_states'].append(rec); timeline.write(json.dumps({'event': 'fc_state', **rec}) + '\n'); timeline.flush()
                print(f'[{now/1000:7.1f}s] FC {s} rf_drops={rf.get("rf_tx_drops") if rf else None} netctl={rf.get("rf_tx_drop_network_control") if rf else None}', flush=True)
        try:
            c, fsv = fs.request(origin, '/flightstate', None, token)
            c2, msgs = fs.request(origin, '/api/messages', None, token)
        except Exception:
            continue
        if fsv != gs_state:
            gs_state = fsv
            rec = {'virtual_ms': now, 'gs_flightstate': fsv, 'rf': rf}
            result['gs_states'].append(rec); timeline.write(json.dumps({'event': 'gs_state', **rec}) + '\n'); timeline.flush()
            print(f'[{now/1000:7.1f}s] GS {fsv}', flush=True)
        for m in msgs if isinstance(msgs, list) else []:
            if m.get('id') in messages_seen: continue
            messages_seen.add(m.get('id'))
            if str(m.get('message', '')).startswith('FC:') and 'delayed GPS' not in m.get('message', ''):
                rec = {'virtual_ms': now, 'message': m.get('message')}
                result['fc_messages'].append(rec); timeline.write(json.dumps({'event': 'fc_message', **rec}) + '\n'); timeline.flush()
        if fc_state == 'Recovery' and result['fc_states'][-1]['virtual_ms'] + 30000 < now:
            break
        if now > duration - 3000: break
finally:
    name = command[command.index('--name') + 1]
    try:
        files = subprocess.check_output(['docker', 'exec', name, 'sh', '-c', 'ls /opt/groundstation/data/groundstation_recording_*'], text=True).split()
        (out / 'gsdb').mkdir(exist_ok=True)
        for f in files: subprocess.run(['docker', 'cp', f'{name}:{f}', str(out / 'gsdb')], capture_output=True)
    except Exception as e:
        result['db_copy_error'] = str(e)
    if proc.poll() is None:
        subprocess.run(['docker', 'kill', name], capture_output=True); proc.wait(60)
    subprocess.run(['docker', 'network', 'rm', network], capture_output=True)
    for p in (diag / 'temp').glob('*/groundstation-*log'):
        (out / ('gs-' + p.name)).write_bytes(p.read_bytes())
    tail.poll(); s, r = tail.latest('rf'); result['rf_final'] = r
    s, r = tail.latest('flight'); result['fc_final'] = r
    try:
        import sqlite3
        db = next((out / 'gsdb').glob('*.db'))
        c = sqlite3.connect(str(db))
        result['gs_db_flight_state'] = [list(x) for x in c.execute("select timestamp_ms, source_timestamp_ms, sender_id, values_json from telemetry where data_type='FLIGHT_STATE' order by rowid")]
        result['gs_db_messages'] = [x[0] for x in c.execute("select message from messages order by rowid") if str(x[0]).startswith('FC:') and 'delayed GPS' not in str(x[0])]
        result['gs_db_deployments'] = [x[0] for x in c.execute("select message from messages order by rowid") if 'parachute deployed' in str(x[0])]
        result['gs_db_accel_rows'] = c.execute("select count(*) from telemetry where data_type like '%ACCEL%' or data_type like '%IMU%'").fetchone()[0]
    except Exception as e:
        result['db_error'] = str(e)
    final_gs = result['gs_states'][-1]['gs_flightstate'] if result['gs_states'] else None
    if isinstance(final_gs, str):
        try: final_gs = json.loads(final_gs)
        except ValueError: pass
    # /flightstate is {"state": <set-(a) name>, "fc_phase": {...}} since the state split.
    if isinstance(final_gs, dict): final_gs = final_gs.get('state')
    # The station logs every deployment notification; the recording DB exists
    # only once a recording starts, so the log is the authoritative record here.
    gs_log = '\n'.join(p.read_text(errors='replace') for p in out.glob('gs-groundstation-*.log'))
    result['gs_log_deployments'] = [l for l in gs_log.splitlines() if 'parachute deployed' in l]
    deploy = ' '.join(result.get('gs_db_deployments', []) + result['gs_log_deployments'])
    result['checks'] = {
        'gs_followed_fc_to_landed': final_gs in ('Landed', 'Recovery'),
        'gs_drogue_notification': 'Drogue parachute deployed' in deploy,
        'gs_main_notification': 'Main parachute deployed' in deploy,
    }
    spec = importlib.util.spec_from_file_location('wire', HERE / 'flight_state_wire_check.py'); wire = importlib.util.module_from_spec(spec); spec.loader.exec_module(wire)
    result['wire'] = wire.check(out)
    result['checks']['flight_state_wire_set_a'] = result['wire']['pass']
    result['passed'] = all(result['checks'].values())
    (out / 'transport-result.json').write_text(json.dumps(result, indent=1, default=str))
    if fwd: fwd.close()
    print(json.dumps({k: result[k] for k in ('checks', 'gs_states', 'gs_db_flight_state', 'gs_db_messages') if k in result}, default=str)[:4000])
    sys.exit(0 if result['passed'] else 1)
