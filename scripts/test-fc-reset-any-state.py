#!/usr/bin/env python3
"""FC reset to Startup from Descent, without an Abort, on the full 7-board bay.

State-split contract, "Reset protocol" (2026-11-11). Same harness as
test-fc-state-transport.py: the FC image must carry the sim-only
scripts/fixtures/fc-sim-self-launch.patch hook, so it launches on its own and
flies the deterministic profile.

Scenario:
  1. the FC flies; when its own `sm` reads Descent the operator presses
     Deploy (DeployParachute) through the ordinary GS HTTP API;
  2. after the deploy settles the station queries the FC and resets it with
     `reset_fc_startup` (prepare + confirm, no Abort first);
  3. the FC must be back in Startup with its deployment history kept
     (g_conf Parachute_Deployed still set), answer a fresh query healthy
     (result 0, state 0, no Abort latch), and keep reporting normally;
  4. every FLIGHT_STATE frame of the run must be in set (a).

PASS needs FC firmware that implements operation 3 and advertises readback
bit 7 (the FC side of the contract). Older FC firmware fails at step 2: the
station refuses the reset ("does not support reset from any state").

usage: test-fc-reset-any-state.py EVIDENCE BUILD_DIR [launch_ms] [duration_ms] [extra profile args...]
"""
import argparse
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
SIM = HERE / 'test-operational-flight-sim.py'
spec = importlib.util.spec_from_file_location('fs', SIM); fs = importlib.util.module_from_spec(spec)
sys.argv, saved = [str(SIM)], sys.argv; spec.loader.exec_module(fs); sys.argv = saved
spec = importlib.util.spec_from_file_location('wire', HERE / 'flight_state_wire_check.py'); wire = importlib.util.module_from_spec(spec); spec.loader.exec_module(wire)

ev, build = Path(saved[1]), Path(saved[2])
launch_ms = int(saved[3]) if len(saved) > 3 else 90000
duration = int(saved[4]) if len(saved) > 4 else 330000
profile_args = saved[5:] or ['--drogue-vt', '30', '--main-vt', '12', '--tail-s', '60']
args = argparse.Namespace(evidence=ev, cohort=build / 'cohort', image=(build / 'IMAGE-ID').read_text().strip(),
                          duration_ms=duration, tiles=None, cpus='12', memory='16g', gps_utc_offset_ms=-5000,
                          profile_arg=profile_args, wall_freshness_scale=30)
out, diag, key, command, network, profile, missing = fs.prepare(args)
fs.atomic(diag / 'gps.fix', '1')
fs.atomic(diag / 'flight' / 'launch-anchor.txt', str(launch_ms + 500))
subprocess.run(['docker', 'network', 'create', '--internal', network], check=True, capture_output=True)
proc = subprocess.Popen(command, stdout=(out / 'simulator.log').open('w'), stderr=subprocess.STDOUT)
tail = fs.ProbeTail(diag)
timeline = (out / 'reset-timeline.jsonl').open('a')
token = origin = fwd = None
stage, deadline, ticket = 'flying', None, None
result = {'events': []}


def event(name, now, **extra):
    row = {'event': name, 'virtual_ms': now, **extra}
    result['events'].append(row)
    timeline.write(json.dumps(row, default=str) + '\n'); timeline.flush()
    print(f'[{now/1000:7.1f}s] {name} {json.dumps(extra, default=str)[:300]}', flush=True)


def fc_row(status):
    return next((row for row in status.get('boards', []) if row.get('id') == 'FC'), {})


try:
    while proc.poll() is None:
        time.sleep(1)
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
        _, row = tail.latest('flight')
        if not row or 'fc_state' not in row: continue
        raw = row['fc_state'] & 0xff
        fc_state = fs.FC_LOCAL[raw] if raw < len(fs.FC_LOCAL) else raw
        flags = row.get('fc_config_flags', 0)
        if stage == 'flying' and fc_state == 'Descent':
            code, reply = fs.request(origin, '/api/command', 'DeployParachute', token)
            event('operator_deploy', now, code=code, reply=reply, fc_state=fc_state)
            stage, deadline = 'settling', now + 5000
        elif stage == 'settling' and now >= deadline:
            code, reply = fs.request(origin, '/api/board-recovery/query', {'board': 'FC'}, token)
            event('fc_query', now, code=code)
            stage, deadline = 'prepare', now + 3000
        elif stage == 'prepare' and now >= deadline:
            code, status = fs.request(origin, '/api/board-recovery/status', None, token)
            row_fc = fc_row(status) if isinstance(status, dict) else {}
            result['drogue_before_reset'] = bool(flags & fs.OPT_DROGUE)
            event('status_before_reset', now, fc_state=fc_state, flight_state=status.get('flight_state') if isinstance(status, dict) else None,
                  capability=row_fc.get('capabilities', {}).get('reset_startup_any_state'),
                  reason=row_fc.get('reset_startup_any_state_blocked_reason'), warning=row_fc.get('reset_fc_startup_warning'))
            body = {'action': 'reset_fc_startup', 'board': 'FC', 'mode': None,
                    'backend_instance': status.get('backend_instance'), 'reset_epoch': status.get('reset_epoch')}
            code, ticket = fs.request(origin, '/api/board-recovery/prepare', body, token)
            event('prepare', now, code=code, ticket=ticket)
            if code != 200:
                result['failure'] = f'prepare refused: {ticket}'; break
            confirm = {k: ticket[k] for k in ('ticket', 'backend_instance', 'principal_context', 'reset_epoch')}
            confirm['confirmation'] = ticket['confirmation']
            code, reply = fs.request(origin, '/api/board-recovery/confirm', confirm, token)
            event('confirm', now, code=code, reply=reply)
            if code != 200:
                result['failure'] = f'confirm refused: {reply}'; break
            stage, deadline = 'resetting', now + 30000
        elif stage == 'resetting':
            code, status = fs.request(origin, '/api/board-recovery/status', None, token)
            op = status.get('operation') if isinstance(status, dict) else None
            if op and op.get('completed'):
                event('reset_terminal', now, operation=op, fc_state=fc_state)
                if not op.get('success'):
                    result['failure'] = f'reset failed: {op}'; break
                stage, deadline = 'after', now + 20000
            elif now >= deadline:
                result['failure'] = 'reset did not complete'; break
        elif stage == 'after' and now >= deadline:
            fs.request(origin, '/api/board-recovery/query', {'board': 'FC'}, token)
            time.sleep(3)
            code, status = fs.request(origin, '/api/board-recovery/status', None, token)
            row_fc = fc_row(status) if isinstance(status, dict) else {}
            code, flightstate = fs.request(origin, '/flightstate', None, token)
            result['after'] = {'fc_probe_state': fc_state, 'drogue_flag': bool(flags & fs.OPT_DROGUE),
                               'readback_state': row_fc.get('state'), 'readback_result': row_fc.get('result'),
                               'abort_latched': row_fc.get('abort_latched'), 'gs_flightstate': flightstate}
            event('after_reset', now, **result['after'])
            break
        if now > duration - 3000:
            result['failure'] = result.get('failure') or f'timed out in stage {stage}'; break
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
    after = result.get('after', {})
    result['wire'] = wire.check(out)
    result['checks'] = {
        'operator_deploy_in_descent': any(e['event'] == 'operator_deploy' and e.get('code') == 200 for e in result['events']),
        'reset_without_abort_succeeded': any(e['event'] == 'reset_terminal' and (e.get('operation') or {}).get('success') for e in result['events']),
        'fc_back_in_startup': after.get('fc_probe_state') == 'Startup' and after.get('readback_state') == 0,
        'deployment_history_kept': bool(result.get('drogue_before_reset')) and after.get('drogue_flag') is True,
        'fc_healthy_after_reset': after.get('readback_result') == 0 and after.get('abort_latched') is False,
        'flight_state_wire_set_a': result['wire']['pass'],
    }
    result['passed'] = all(result['checks'].values())
    (out / 'reset-result.json').write_text(json.dumps(result, indent=1, default=str))
    if fwd: fwd.close()
    print(json.dumps({'checks': result['checks'], 'failure': result.get('failure')}, default=str))
    sys.exit(0 if result['passed'] else 1)
