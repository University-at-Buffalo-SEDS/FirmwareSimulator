#!/usr/bin/env python3
"""Full seven-board linked simulation (see docs/hardware-faithful-radio.md for --radio rfd-faithful
--faithful-time and the faithful, gps_time and vb_stale scenarios) (RF, PB, FC on the AV bay radio; GB, AB, VB, DAQ on the
umbilical) with the one-binary GS (+firmware_sim) started in a runtime station mode
(GS_STATION_MODE=hitl|test_fire|normal; legacy --mode hitl_mode/test_fire_mode map onto it), a generic
recorder (virtual-time stamped API snapshots) and scripted scenarios.

All stimuli are explicit simulator inputs (pressure/loadcell raw files, scheduled CAN-link,
board-reboot and GS power-cycle events) or ordinary authenticated GS API calls.
Evidence: <evidence>/ (record.jsonl, actions.jsonl, result.json, simulator.log, diagnostics/).
"""
from __future__ import annotations
import argparse, importlib.util, json, os, re, subprocess, sys, threading, time, traceback
from pathlib import Path

W = Path(__file__).resolve().parent
_fsim = W / 'test-operational-flight-sim.py'
if not _fsim.exists(): _fsim = W / 'src/FirmwareSimulator/scripts/test-operational-flight-sim.py'
spec = importlib.util.spec_from_file_location('fsim', _fsim)
F = importlib.util.module_from_spec(spec); spec.loader.exec_module(F)
BOARDS = ['RF', 'FC', 'PB', 'AB', 'VB', 'GB', 'DAQ']
NODE_OF = {'RF': 'rf', 'FC': 'flight', 'PB': 'power', 'AB': 'actuator', 'VB': 'valve', 'GB': 'gateway', 'DAQ': 'daq'}
CAN_EP = {'rf': ('avionics_can', 'fdcan2'), 'power': ('avionics_can', 'fdcan2'), 'flight': ('avionics_can', 'fdcan1'),
          'gateway': ('fill_can', 'fdcan2'), 'actuator': ('fill_can', 'fdcan2'), 'valve': ('fill_can', 'fdcan2'), 'daq': ('fill_can', 'fdcan1')}
RADIO = {
    # RFD900x ASYNC 64 kbit/s air, 115200 UART (deployed rfd-async-64k-compatible), finite FIFOs.
    'rfd': dict(air_bits_per_second=64000, uart_baud=115200, tx_fifo_bytes=8192, rx_fifo_bytes=8192, max_burst_bytes=256,
                packet_idle_us=2000, overhead_bits=160, turn_us=2000),
    # Same modem with a quarter of the air capacity: the RF->GS and GS->RF directions back up.
    # Hardware-faithful RFD900x2 ASYNC 4.03 (backend/comms/rfd-profile.json "rfd-async-64k-compatible"):
    # AIR_SPEED 64k, SERIAL_SPEED 115200 without RTS/CTS (a full modem buffer drops bytes), packets close
    # after SER_BRK_DETMS=50 ms of UART idle or at MAX_DATA, every packet to the broadcast DESTID is sent
    # 1 + GLOBAL_RETRIES(10) times (FIRMWARE_SIM_RFD_GLOBAL_RETRIES), about 20 B of air framing per copy.
    'rfd-faithful': dict(air_bits_per_second=64000, uart_baud=115200, tx_fifo_bytes=4096, rx_fifo_bytes=4096, max_burst_bytes=3968,
                         packet_idle_us=50000, overhead_bits=160, turn_us=2000),
    'backlog': dict(air_bits_per_second=16000, uart_baud=115200, tx_fifo_bytes=8192, rx_fifo_bytes=8192, max_burst_bytes=256,
                    packet_idle_us=2000, overhead_bits=160, turn_us=4000),
}


def build_config(a):
    out = a.evidence.resolve(); assert not out.exists(), 'new evidence dir required'
    out.mkdir(parents=True); config = out / 'config'; config.mkdir(); diag = out / 'diagnostics'
    for sub in ('', 'temp', 'state', 'flight'):
        (diag / sub).mkdir(mode=0o770, exist_ok=True); (diag / sub).chmod(0o770)
    F.atomic(diag / 'pressure.raw', '0'); F.atomic(diag / 'loadcell.raw', F.mass_samples(0)); F.atomic(diag / 'gps.fix', str(a.gps_initial))
    F.atomic(diag / 'flight/radio-gate.txt', '0')
    key, users = F.fixture_user(); (config / 'users.json').write_text(json.dumps(users))
    gse = {'nitrogen_target_psi': 500, 'pressure_step_psi': 50, 'pressure_ceiling_psi': 900, 'maximum_zero_offset_psi': 20,
           'dry_self_test_confirmed': False, 'grouped_panel': True, 'turbocharge_max_added_mass_kg': 0.5, 'turbocharge_pressure_deadband_psi': 12}
    (config / 'gse.json').write_text(json.dumps(gse))
    (config / 'fill-targets.json').write_text(json.dumps({'version': 1, 'fill_source': 'kg50', 'nitrogen': {'target_mass_kg': 0.5, 'target_pressure_psi': 500},
                                                          'nitrous': {'target_mass_kg': 8, 'target_pressure_psi': 750}}))
    items = [{'id': n, 'text': n, 'checked': False, 'complete_on': k, 'completed_by': None, 'completed_at_ms': None}
             for n, k in [('self-test', 'valve_self_test'), ('nitrogen', 'nitrogen_test')]]
    (config / 'checklists.json').write_text(json.dumps({'revision': 0, 'enforce_before_launch': True, 'checklists': [
        {'id': 'checkout', 'title': 'Checkout', 'required_before_launch': True, 'items': items}]}))
    (config / 'calibration.json').write_text(json.dumps({'version': 1, 'ch1': {'m': 100, 'b': 0}, 'iadc': {'m': 1, 'b': 0}, 'kg50_input': 'amp2',
        'extra_channels': {'kg50': {'linear': {'m': 100, 'b': 0}, 'zero_raw': None, 'points': [], 'fit': None}}}))
    for n in ('checklists', 'calibration', 'gse'):
        p = diag / 'state' / f'{n}.json'; p.write_text((config / f'{n}.json').read_text()); p.chmod(0o660)
    topo = F.topology(a.duration_ms, a.wall_freshness_scale)
    topo['name'] = 'fullnet-' + a.station_mode
    if a.nodes:
        keep = set(a.nodes.split(','))
        topo['nodes'] = [n for n in topo['nodes'] if n['name'] in keep]
        topo['links'] = [l for l in topo['links'] if all(e['node'] in keep | {'groundstation'} for e in l['endpoints'])]
        names = {l['name'] for l in topo['links']}
        topo['host_nodes'][0]['serial_links'] = [x for x in topo['host_nodes'][0]['serial_links'] if x['link'] in names]
    host = topo['host_nodes'][0]
    host['binary'] = '/usr/local/bin/groundstation_backend'
    env = host['env']
    env['GS_STATION_MODE'] = a.station_mode
    # No GS_LAYOUT_PATH: the backend picks layout.json / layout_hitl.json / layout_test_fire.json from the
    # station mode (as deployed), so an in-process mode switch can change the layout.
    env.pop('GS_LAYOUT_PATH', None)
    if a.mode == 'test_fire_mode':
        env.pop('GS_SIM_WALL_FRESHNESS_SCALE', None)
    env['RUST_LOG'] = a.rust_log
    if a.faithful_time:
        # One time base: Renode is held to wall/pace and the station runs time-dilated by the same
        # factor, so its wall-clock timers (heartbeat, discovery, reliable retries, query refresh,
        # freshness TTLs) age at the boards' rate. No freshness stretching.
        env.pop('GS_SIM_WALL_FRESHNESS_SCALE', None)
        env['LD_PRELOAD'] = '/usr/lib/x86_64-linux-gnu/faketime/libfaketimeMT.so.1'
        env['FAKETIME'] = '+0 x%.6f' % (1.0 / a.faithful_time)
        env['FAKETIME_DONT_FAKE_MONOTONIC'] = '0'
    for kv in a.gs_env:
        k, v = kv.split('=', 1); env[k] = v
    env['GS_LAUNCH_CLOCK_STATE'] = '/diagnostics/state/launch-clock.json'
    env['GS_BOARD_ABORT_HISTORY_DIR'] = '/diagnostics/state/board_abort_history'
    radio_link = next((l for l in topo['links'] if l['name'] == 'rocket_radio'), None)
    if a.radio != 'hub' and radio_link is not None:
        radio_link['radio'] = RADIO[a.radio]
    if a.rrls:
        # Second hosted station on the same radio medium (FirmwareSimulator radio_profile.py layout),
        # same binary and station mode, private cwd/config, HTTP on :3001.
        assert radio_link is not None and a.radio != 'hub'
        (config / 'comms-rrls.json').write_text(json.dumps({'version': 1, 'av_bay': {'interface': 'serial', 'port': '/dev/simulator-radio',
                                                            'baud_rate': RADIO[a.radio]['uart_baud'], 'protocol': 'raw_uart'}}, indent=2))
        rr = {'name': 'rrls', 'binary': host['binary'], 'cwd': '/tmp',
              'env': {'GS_RRLS_ROLE': 'rrls', 'GS_COMMS_LINK_CONFIG': '/simulation/comms-rrls.json', 'GS_SIMULATED_SERIAL_PTY': '1',
                      'GS_BIND_ADDRESS': '0.0.0.0:3001', 'GS_HTTPS_BIND_ADDRESS': 'off', 'GS_VIDEO_MANAGED': '0', 'GS_VIDEO_DISCOVERY': '0',
                      'GS_STATION_DISCOVERY_BIND': '127.0.0.1:0', 'GS_VIDEO_DISCOVERY_BIND': '127.0.0.1:0',
                      'GS_DEBUG_PRINTS': '0', 'RUST_LOG': a.rust_log, 'GS_STATION_MODE': a.station_mode, 'GS_USERS_FILE': '/simulation/users.json'},
              'serial_links': [{'link': 'rocket_radio', 'env': 'GS_AV_BAY_SERIAL_PORT'}]}
        for k_, filename in {'GS_FILL_TARGETS_CONFIG': 'fill-targets.json', 'GS_FLIGHT_SETUP_CONFIG': 'flight-setup.json', 'GS_GSE_CONFIG': 'gse.json',
                              'GS_GSE_QUALIFICATION': 'gse-qualification.json', 'GS_AUTO_ZERO_CONFIG': 'auto-zero.json', 'GS_CALIBRATION_CONFIG_PATH': 'calibration.json',
                              'GS_LOADCELL_CALIBRATION_PATH': 'loadcells.json', 'GS_STATION_POSITIONS_PATH': 'station-positions.json',
                              'GS_CHECKLISTS_FILE': 'checklists.json', 'GS_MISSION_SETTINGS_PATH': 'mission-settings.json'}.items():
            rr['env'][k_] = '/tmp/simulator-rrls-' + filename
        for k_ in ('GS_LAYOUT_PATH', 'GS_SIM_ROUTER_TIME_DIVISOR', 'GS_SIM_NETWORK_EPOCH_MS', 'GS_HEARTBEAT_PERIOD_MS', 'GS_SIM_WALL_FRESHNESS_SCALE'):
            if k_ in env: rr['env'][k_] = env[k_]
        topo['host_nodes'].append(rr)
        radio_link['endpoints'].append({'node': 'rrls', 'peripheral': 'radio'})
    # scheduled events (samples are virtual seconds)
    for spec_ in a.gs_reboot:
        topo['reboots'].append({'node': 'groundstation', 'after_sample': int(spec_)})
    for spec_ in a.board_reboot:
        node, s = spec_.split(':'); topo['reboots'].append({'node': NODE_OF.get(node, node), 'after_sample': int(s)})
    for spec_ in a.can_cut:
        node, s0, s1 = spec_.split(':'); node = NODE_OF.get(node, node); link, per = CAN_EP[node]
        topo['can_link_events'] += [{'link': link, 'node': node, 'peripheral': per, 'after_sample': int(s0), 'connected': False},
                                    {'link': link, 'node': node, 'peripheral': per, 'after_sample': int(s1), 'connected': True}]
    missing = {}
    for node in topo['nodes']:
        name = node['name']; board = a.cohort / F.REPOS[name]
        layout = F.load_layout(board); symbols = F.nm_symbols(board, layout)
        layout['execution']['memory_probe_warmup_samples'] = 3
        layout['execution']['memory_probes'] = [p for p in layout['execution'].get('memory_probes', [])
                                                if p['name'] in F.BASE_PROBES and p.get('symbol') in symbols]
        if 'hal_tick_address' in layout['execution'] and 'uwTick' in symbols:
            layout['execution']['hal_tick_address'] = symbols['uwTick']
        miss = missing.setdefault(name, [])
        if name == 'valve':
            s = next(p for p in layout['peripherals'] if p.get('bus') == 'adc3'); s.update(channel_samples=[0], noise_lsb=0, stimulus_path='/diagnostics/pressure.raw')
            F.add_probes(layout, symbols, [('valve_abort', 'g_sim_valve_aborted', 0), ('vb_op_stage_word', 'g_overpressure', 32), ('vb_op_trips_word', 'g_overpressure', 28)], miss)
        if name == 'daq':
            next(p for p in layout['peripherals'] if p.get('model') == 'mcp3564r')['stimulus_path'] = '/diagnostics/loadcell.raw'
        if name == 'rf':
            g = next(p for p in layout['peripherals'] if p.get('model') == 'neo_m9n'); g['stimulus_path'] = '/diagnostics/gps.fix'
        if name == 'actuator':
            F.add_probes(layout, symbols, [('ab_abort_word', 'g_aborted', 0)], miss)
        if name == 'flight':
            F.add_probes(layout, symbols, [('fc_state', 'sm', 0), ('fc_config_flags', 'g_conf', 0), ('fc_commands_received', 'g_network_flight_commands_received', 0)], miss)
            if 'owner' in symbols:  # fc_recovery_owner.c static mailbox: epoch @0, phase/sensor_restart @20, manual_stop.. @22
                layout['execution']['memory_probes'] += [{'name': f'fc_owner_w{i}', 'address': symbols['owner'] + 4 * i} for i in range(8)]
        dbg = sorted(x for x in symbols if '.' not in x and x.startswith(('g_dbg_', 'g_telemetry_timesync_', 'g_telemetry_service_faults')))
        layout['execution']['memory_probes'] += [{'name': x[2:], 'symbol': x} for x in dbg]
        (config / f'{name}.json').write_text(json.dumps(layout))
    (config / 'comms.json').write_text(json.dumps({'version': 1,
        'av_bay': {'interface': 'serial', 'port': '/dev/ttyAMA0', 'baud_rate': 115200, 'protocol': 'raw_uart'},
        'fill_box': {'interface': 'pico_fi_tcp', 'mode': 'server', 'address': '0.0.0.0:0', 'discovery': True}}, indent=2))
    (config / 'topology.json').write_text(json.dumps(topo, indent=2))
    for n in ('gs-data', 'gs-backend-data'):
        (out / n).mkdir(mode=0o777); (out / n).chmod(0o777)
    pid = os.getpid(); name = f'seds-fullnet-{pid}'; net = f'seds-fullnet-net-{pid}'
    cmd = ['docker', 'run', '--rm', '--name', name, '--cpus', str(a.cpus), '--memory', '20g', '--memory-swap', '20g', '--pids-limit', '768',
           '--ulimit', 'core=0', '--network', net, '--group-add', str(os.getgid()),
           '-e', 'TMPDIR=/diagnostics/temp', '-e', 'FIRMWARE_SIM_DIAGNOSTICS_DIR=/diagnostics',
           '-e', 'SEDS_UART_GATE=rf:usart1:/diagnostics/flight/radio-gate.txt',
           '-v', f'{config}:/simulation:ro', '-v', f'{diag}:/diagnostics',
           '-v', f'{out / "gs-data"}:/opt/groundstation/data', '-v', f'{out / "gs-backend-data"}:/opt/groundstation/backend/data']
    if a.radio_trace:
        cmd += ['-e', 'FIRMWARE_SIM_RADIO_TRACE_DIR=/diagnostics/radio-trace', '-e', f'FIRMWARE_SIM_RADIO_TRACE_UNTIL_MS={a.duration_ms - 1000}']
    if a.faithful_time:
        cmd += ['-e', f'FIRMWARE_SIM_WALL_PACE={a.faithful_time}']
    if a.radio == 'rfd-faithful':
        cmd += ['-e', f'FIRMWARE_SIM_RFD_GLOBAL_RETRIES={a.rfd_global_retries}']
    if a.rfd_modem:
        cmd += ['-e', 'FIRMWARE_SIM_RFD_MODEM=' + a.rfd_modem, '-e', 'FIRMWARE_SIM_RFD_MODEM_LOG=/diagnostics/rfd-modem.log']
    for node, repo in F.REPOS.items():
        cmd += ['-v', f'{(a.cohort / repo).resolve()}:/nodes/{node}:ro']
    cmd += [a.image, 'bay', '--topology', '/simulation/topology.json']
    (out / 'command.json').write_text(json.dumps(cmd, indent=2)); (out / 'args.json').write_text(json.dumps(vars(a), default=str, indent=2))
    import shutil; shutil.copyfile(__file__, out / 'controller.py')
    return out, diag, key, cmd, name, net, missing


class Sim:
    def __init__(self, a):
        self.a = a
        self.out, self.diag, self.key, self.cmd, self.cname, self.net, missing = build_config(a)
        self.lock = threading.RLock(); self.state = {}; self.now = 0; self.origin = None; self.token = None
        self.fwd = None; self.proc = None; self.stop = threading.Event(); self.result = {'phase': 'running', 'checks': [], 'missing_probes': missing}
        self.rec = (self.out / 'record.jsonl').open('a'); self.act = (self.out / 'actions.jsonl').open('a')
        self.gs_epoch = 0; self.pressure = 0.0; self.pressure_target = None; self.pressure_rate = 0.0
        self.mass = 0.0
        self.last_diag_v = -10**9; self.slow = []

    # ---- low level
    def vclock(self):
        for p in (self.diag / 'temp').glob('*/virtual-clock.txt'):
            try: return int(p.read_text().strip())
            except ValueError: return self.now
        return self.now

    def api(self, path, body=None, timeout=10):
        for attempt in range(3):
            tok = self.token
            try:
                code, data = F.request(self.origin, path, body, tok, timeout)
            except OSError as e:
                code, data = 0, {'os_error': str(e)}
            if code == 401 and attempt < 2:
                self.relogin(); continue
            return code, data
        return code, data

    def relogin(self):
        for _ in range(600):
            try:
                self.token = F.login(self.origin, self.key); return True
            except (AssertionError, OSError, KeyError, TypeError) as e:
                self.result['last_login_error'] = repr(e)[:500]; time.sleep(.5)
        return False

    def log_action(self, name, **kw):
        v = self.vclock(); row = {'v': v, 'wall': round(time.time(), 3), 'action': name, **kw}
        with self.lock:
            self.act.write(json.dumps(row, default=str) + '\n'); self.act.flush()
        print(f'[{v/1000:8.1f}s] {name} {json.dumps(kw, default=str)[:400]}', flush=True)
        return row

    def check(self, name, ok, **kw):
        row = {'v': self.vclock(), 'name': name, 'pass': bool(ok), **kw}
        self.result['checks'].append(row); self.write_result()
        print(f'[{row["v"]/1000:8.1f}s] CHECK {"PASS" if ok else "FAIL"} {name} {json.dumps(kw, default=str)[:600]}', flush=True)
        return ok

    def write_result(self):
        F.atomic(self.out / 'result.json', json.dumps(self.result, indent=2, default=str) + '\n')

    # ---- recorder (main thread)
    def connect(self):
        logs = list((self.diag / 'temp').glob('*/groundstation-*log'))
        if not logs: return False
        text = '\n'.join(p.read_text(errors='replace')[:262144] for p in logs)
        if not re.search(r'HTTP listening on (?:0\.0\.0\.0|127\.0\.0\.1):3000', text): return False
        feats = re.search(r'features testing=(\w+) hitl_mode=(\w+) test_fire_mode=(\w+)(?: mode=([\w ]+?) firmware_sim=(\w+))?', text)
        self.result['gs_features'] = feats.group(0) if feats else None
        want = {'hitl': 'HITL', 'test_fire': 'Test fire', 'normal': 'Normal'}[self.a.station_mode]
        assert feats and feats.group(4) == want and feats.group(5) == 'true', ('station mode at start', feats and feats.group(0), want)
        info = json.loads(subprocess.check_output(['docker', 'inspect', self.cname], text=True))[0]
        addr = info['NetworkSettings']['Networks'][self.net]['IPAddress']
        self.fwd = F.Forwarder(addr); self.origin = self.fwd.origin
        self.addr = addr
        assert self.relogin(), 'login failed'
        self.log_action('gs_connected', origin=self.origin, features=self.result['gs_features']); return True

    def snapshot(self):
        v = self.vclock(); snap = {'v': v, 'wall': round(time.time(), 3)}
        for key, path in (('rec', '/api/board-recovery/status'), ('abort', '/api/board_abort'), ('policy', '/api/action_policy'),
                          ('gse', '/api/gse/status'), ('boards', '/api/boards')):
            code, data = self.api(path, timeout=5); snap[key] = data if code == 200 else {'http': code, 'body': data}
        if v - self.last_diag_v >= 5000:
            code, data = self.api('/api/network_diagnostics', timeout=5); snap['netdiag'] = data if code == 200 else {'http': code}
            self.last_diag_v = v
        snap['v_end'] = self.vclock()
        with self.lock:
            self.state = snap; self.now = v
            self.rec.write(json.dumps(snap, default=str, separators=(',', ':')) + '\n')
        return snap

    def plant(self, dt):
        if self.pressure_target is not None:
            step = self.pressure_rate * dt / 1000
            self.pressure = min(self.pressure_target, self.pressure + step) if self.pressure < self.pressure_target else max(self.pressure_target, self.pressure - step)
            F.atomic(self.diag / 'pressure.raw', str(F.raw_pressure(self.pressure)))

    def run(self, scenario):
        subprocess.run(['docker', 'network', 'create', '--internal', '--label', 'seds.task=fullnet', self.net], check=True, capture_output=True)
        log = (self.out / 'simulator.log').open('w'); self.proc = subprocess.Popen(self.cmd, stdout=log, stderr=subprocess.STDOUT)
        start = time.monotonic(); worker = None; prev_v = 0; err = None
        try:
            while self.proc.poll() is None:
                if time.monotonic() - start > self.a.timeout: raise TimeoutError('wall timeout')
                if self.origin is None:
                    if not self.connect(): time.sleep(.3); continue
                    worker = threading.Thread(target=self._scenario, args=(scenario,), daemon=True); worker.start()
                try:
                    self.snapshot()
                except Exception as e:  # GS restarting
                    time.sleep(.3); continue
                self.plant(max(0, min(self.now - prev_v, 500))); prev_v = self.now
                if worker is not None and not worker.is_alive() and self.result['phase'] != 'running':
                    break
                time.sleep(self.a.poll)
            if self.proc.poll() is not None and self.result['phase'] == 'running':
                self.result['phase'] = 'sim_exited'; self.result['sim_exit'] = self.proc.returncode
        except BaseException as e:
            err = e; self.result['phase'] = 'controller_error'; self.result['error'] = repr(e)
        finally:
            self.stop.set(); self.result['wall_s'] = round(time.monotonic() - start); self.result['virtual_ms'] = self.vclock()
            self.result['sim_exit'] = self.proc.poll()
            self.write_result()
            if self.proc.poll() is None:
                subprocess.run(['docker', 'stop', '--time', '5', self.cname], capture_output=True); self.proc.wait(timeout=60)
            if self.fwd: self.fwd.close()
            if getattr(self, 'rrls_fwd', None): self.rrls_fwd.close()
            subprocess.run(['docker', 'network', 'rm', self.net], capture_output=True)
            log.close(); self.rec.close(); self.act.close()
        if err: raise err

    def _scenario(self, scenario):
        try:
            scenario(self)
            if self.result['phase'] == 'running':
                self.result['phase'] = 'completed'
        except Exception as e:
            self.result['phase'] = 'scenario_error'; self.result['error'] = repr(e); self.result['traceback'] = traceback.format_exc()
            print(traceback.format_exc(), flush=True)
        self.result['passed'] = all(c['pass'] for c in self.result['checks'])
        self.write_result()

    # ---- scenario helpers (worker thread)
    def st(self):
        with self.lock: return self.state

    def rows(self, snap=None):
        snap = snap or self.st(); rec = snap.get('rec') or {}
        return {r['id']: r for r in rec.get('boards', [])} if isinstance(rec, dict) else {}

    def wait_v(self, ms):
        t = self.vclock() + ms
        while self.vclock() < t and not self.stop.is_set(): time.sleep(.05)

    def wait_until(self, pred, timeout_ms, label, quiet=False):
        t0 = self.vclock(); last = None
        while not self.stop.is_set():
            snap = self.st()
            if snap.get('v', -1) >= t0:
                try:
                    if pred(snap):
                        if not quiet: self.log_action('wait_ok', label=label, waited_ms=snap['v'] - t0)
                        return snap
                except (KeyError, TypeError, AttributeError):
                    pass
            if self.vclock() - t0 > timeout_ms:
                self.log_action('wait_timeout', label=label, waited_ms=self.vclock() - t0); return None
            time.sleep(.05)
        return None

    def launch_control(self, snap=None):
        snap = snap or self.st(); pol = snap.get('policy') or {}
        return next((c for c in pol.get('controls', []) if c.get('cmd') == 'Launch'), None) if isinstance(pol, dict) else None

    def command(self, name):
        code, body = self.api('/api/command', name); self.log_action('command', cmd=name, http=code, body=body); return code, body

    def healthy(self, row):
        return (row.get('readback_status') == 'readback_current' and not row.get('recovery_required') and row.get('abort_latched') in (None, False))

    def all_present_healthy(self, snap, boards=None):
        boards = boards or BOARDS; r = self.rows(snap); return all(b in r and self.healthy(r[b]) for b in boards)

    def aborted_set(self, snap):
        ab = snap.get('abort') or {}; return sorted({e.get('board') for e in ab.get('active_errors', [])} if isinstance(ab, dict) else [])

    def recovery(self, action, board=None, boards=None, expect_ok=True, label=None, wait=True, timeout_ms=60000):
        rec = self.st().get('rec') or {}
        req = {'action': action, 'board': board, 'boards': boards or [], 'mode': None, 'backend_instance': rec.get('backend_instance'), 'reset_epoch': rec.get('reset_epoch')}
        # refresh identity directly (status in snapshot may be a few hundred ms old)
        code, live = self.api('/api/board-recovery/status')
        if code == 200: req.update(backend_instance=live['backend_instance'], reset_epoch=live['reset_epoch'])
        v0 = self.vclock(); code, ticket = self.api("/api/board-recovery/prepare", req, timeout=900)
        self.log_action('recovery_prepare', label=label, request=req, http=code, ticket=ticket if code != 200 else {k: ticket.get(k) for k in ('action', 'targets', 'confirmation', 'expires_in_ms', 'summary', 'required_proof_boards')})
        if code != 200: return {'prepared': False, 'http': code, 'body': ticket, 'v0': v0}
        conf = {k: ticket[k] for k in ('ticket', 'backend_instance', 'principal_context', 'reset_epoch', 'confirmation')}
        code, resp = self.api("/api/board-recovery/confirm", conf, timeout=900)
        self.log_action('recovery_confirm', label=label, http=code, resp=resp)
        if code != 200 or not resp.get('accepted'): return {'prepared': True, 'confirmed': False, 'http': code, 'body': resp, 'v0': v0}
        op_id = resp['operation']['id']
        if not wait: return {'prepared': True, 'confirmed': True, 'op': resp['operation'], 'v0': v0}
        snap = self.wait_until(lambda s: (s['rec'].get('operation') or {}).get('id') == op_id and s['rec']['operation'].get('completed'), timeout_ms, f'op {op_id} {label}')
        op = (snap or self.st())['rec'].get('operation') if snap else None
        res = {'prepared': True, 'confirmed': True, 'op': op, 'v0': v0, 'v_done': snap['v'] if snap else None,
               'duration_ms': (snap['v'] - v0) if snap else None, 'success': bool(op and op.get('success'))}
        self.log_action('recovery_done', label=label, **{k: res[k] for k in ('duration_ms', 'success')}, op=op)
        return res

    def wait_ready(self, boards=None, timeout_ms=240000):
        boards = boards or BOARDS
        return self.wait_until(lambda s: self.all_present_healthy(s, boards), timeout_ms, 'boards ready ' + ','.join(boards))

    def save_snapshot(self, label):
        F.atomic(self.out / f'snap-{label}.json', json.dumps(self.st(), indent=2, default=str))


# ------------------------------------------------------------------ scenarios
def query_rtt_loop(sim, duration_ms, boards=BOARDS, period_ms=2000, tag='rtt'):
    """Operator Query per board round robin; RTT in virtual ms from POST to completed readback."""
    out = (sim.out / f'{tag}.jsonl').open('a'); end = sim.vclock() + duration_ms; i = 0
    if not boards:
        sim.wait_v(duration_ms); out.close(); return
    while sim.vclock() < end and not sim.stop.is_set():
        b = boards[i % len(boards)]; i += 1
        v0 = sim.vclock(); w0 = time.time(); code, body = sim.api('/api/board-recovery/query', {'board': b}, timeout=60)
        row = {'board': b, 'v0': v0, 'http': code, 'accepted': isinstance(body, dict) and body.get('accepted')}
        if code == 200 and row['accepted']:
            # completion: board row operation completed with a newer id or readback age reset
            def done(s):
                r = sim.rows(s).get(b) or {}; op = r.get('operation') or {}
                return op.get('action') == 'query' and op.get('completed') and s['v'] > v0
            t_end = v0 + 15000; res = None
            while sim.vclock() < t_end and not sim.stop.is_set():
                code2, live = sim.api('/api/board-recovery/status', timeout=5)
                if code2 == 200:
                    r = next((x for x in live['boards'] if x['id'] == b), {}); op = r.get('operation') or {}
                    if op.get('action') == 'query' and op.get('completed') and (op.get('age_ms') or 0) <= (time.time() - w0) * 1000 + 50:
                        res = {'v1': sim.vclock(), 'success': op.get('success'), 'op_age_ms_wall': op.get('age_ms'), 'readback_status': r.get('readback_status'),
                               'age_ms': r.get('age_ms'), 'receipt_age_ms': r.get('receipt_age_ms'), 'msg': op.get('message')}
                        break
                time.sleep(.03)
            row.update(res or {'timeout': True}); row['rtt_v_ms'] = (row['v1'] - v0) if res else None; row['rtt_wall_ms'] = round((time.time() - w0) * 1000)
        else:
            row['body'] = body
        out.write(json.dumps(row, default=str) + '\n'); out.flush()
        nxt = v0 + period_ms // len(boards)
        while sim.vclock() < nxt and not sim.stop.is_set(): time.sleep(.03)
    out.close()


def present_boards(sim):
    if not sim.a.nodes: return BOARDS
    keep = set(sim.a.nodes.split(',')); return [b for b in BOARDS if NODE_OF[b] in keep]


def sc_comms(sim):
    code, body = sim.api('/api/command', 'StartWritingNow'); sim.log_action('start_recording', http=code, body=body)
    boards = present_boards(sim)
    snap = sim.wait_ready(boards, timeout_ms=sim.a.ready_timeout_ms)
    sim.check('all present boards readback_current at startup', snap is not None, missing=[b for b in boards if not sim.healthy(sim.rows().get(b, {}))],
              rows={b: (sim.rows().get(b) or {}).get('readback_status') for b in boards})
    sim.save_snapshot('ready')
    sim.log_action('comms_measure_start')
    query_rtt_loop(sim, sim.a.measure_ms, boards=boards if not sim.a.no_query else [])
    sim.log_action('comms_measure_end'); sim.save_snapshot('end')


def latch_all(sim, label, expect=None):
    expect = expect or BOARDS
    sim.command('Abort')
    snap = sim.wait_until(lambda s: set(expect) <= set(sim.aborted_set(s)) or all(sim.rows(s).get(b, {}).get('recovery_required') for b in expect), 30000, 'abort latched ' + label)
    got = sim.aborted_set(snap or sim.st()); rr = {b: (sim.rows().get(b) or {}).get('recovery_reason') for b in BOARDS}
    sim.check(f'{label}: Abort latched on every board', snap is not None, active_errors=got, recovery_reason=rr)
    return snap


def clear_gse(sim, label):
    """Operator 'Clear GSE abort' when the station itself is in Aborted (flight_state 15)."""
    rec = sim.st().get('rec') or {}
    if rec.get('flight_state') == 15:
        code, body = sim.api('/api/gse/abort/clear', {})
        sim.log_action('clear_gse_abort', label=label, http=code, body=body)
        sim.check(f'{label}: Clear GSE abort accepted', code == 200, http=code, body=body)
        sim.wait_until(lambda s: (s.get('rec') or {}).get('flight_state') != 15, 10000, 'station left Aborted')


def verify_clear(sim, label, boards=None, timeout_ms=30000):
    boards = boards or BOARDS
    clear_gse(sim, label)
    snap =sim.wait_until(lambda s: sim.all_present_healthy(s, boards) and not sim.aborted_set(s), timeout_ms, 'cleared ' + label)
    lc = sim.launch_control(snap or sim.st()); r = sim.rows(snap or sim.st())
    reason = (lc or {}).get('disabled_reason') or ''
    lock_free = not any(x in reason for x in ('locked after the last attempt', 'report Abort', 'missed the last reset', 'unfinished board recovery'))
    sim.check(f'{label}: all boards read back non-aborted', snap is not None, rows={b: {k: (r.get(b) or {}).get(k) for k in ('readback_status', 'recovery_required', 'recovery_reason', 'abort_latched', 'state_label')} for b in boards}, active_errors=sim.aborted_set(sim.st()))
    sim.check(f'{label}: Launch not blocked by abort/lock', lock_free, launch_control=lc)
    return snap


def sc_recovery(sim):
    a = sim.a
    snap = sim.wait_ready(timeout_ms=a.ready_timeout_ms)
    sim.check('startup: all seven boards healthy', snap is not None, rows={b: (sim.rows().get(b) or {}).get('readback_status') for b in BOARDS})
    sim.save_snapshot('ready')
    # 1. single-board Review recovery for every board after a global Abort (all latched at once)
    latch_all(sim, 'cycle0')
    sim.save_snapshot('latched0')
    for b in BOARDS:
        r = sim.recovery('recover_abort', board=b, label='recover ' + b)
        rows = sim.rows();
        sim.check(f'single-board recovery {b}', r.get('success') is True, duration_ms=r.get('duration_ms'), op=r.get('op') or r.get('body'),
                  row={k: (rows.get(b) or {}).get(k) for k in ('readback_status', 'recovery_required', 'recovery_reason', 'abort_latched', 'state_label')})
    verify_clear(sim, 'after per-board recovery')
    # 2. Reset all boards after Abort, five cycles alternating with per-board recovery
    for n in range(1, a.cycles + 1):
        latch_all(sim, f'cycle{n}')
        if n % 2:
            r = sim.recovery('reset_startup', boards=BOARDS, label=f'reset all cycle{n}')
            sim.check(f'cycle{n}: Reset all boards succeeds within 15 s', r.get('success') is True and (r.get('duration_ms') or 10**9) <= 15000, duration_ms=r.get('duration_ms'), op=r.get('op') or r.get('body'))
        else:
            for b in BOARDS:
                r = sim.recovery('recover_abort', board=b, label=f'cycle{n} recover {b}')
                sim.check(f'cycle{n}: recover {b}', r.get('success') is True, duration_ms=r.get('duration_ms'), op=r.get('op') or r.get('body'))
        verify_clear(sim, f'cycle{n}')
    sim.save_snapshot('end')


def step(sim, name, fn):
    sim.log_action('step_begin', step=name)
    try:
        fn()
    except Exception as e:
        sim.check(f'step {name} raised', False, error=repr(e), tb=traceback.format_exc()[-1500:])
    sim.save_snapshot('after-' + name.replace(' ', '_'))
    sim.log_action('step_end', step=name)


def wait_until_v(sim, v_ms, label):
    if sim.vclock() > v_ms:
        sim.log_action('schedule_late', label=label, target_v=v_ms)
    while sim.vclock() < v_ms and not sim.stop.is_set(): time.sleep(.05)


def lock_reason(sim, snap=None):
    lc = sim.launch_control(snap) or {}; return lc.get('enabled'), lc.get('disabled_reason') or ''


def phase_to(sim, target, label, limit=10):
    """HITL State +1/-1 until rec.flight_state == target (local order 0..6)."""
    for _ in range(limit):
        code, live = sim.api('/api/board-recovery/status', timeout=60)
        fs = live.get('flight_state') if code == 200 else (sim.st().get('rec') or {}).get('flight_state')
        if fs == target: return True
        cmd = 'AdvanceFlightState' if (fs or 0) < target else 'RewindFlightState'
        code, body = sim.command(cmd)
        if code not in (200, 202):
            sim.check(f'{label}: {cmd} refusal names a reason', isinstance(body, (dict, str)) and len(json.dumps(body)) > 20, http=code, body=body)
            return False
        sim.wait_until(lambda s: (s.get('rec') or {}).get('flight_state') != fs, 8000, f'{label} {cmd} from {fs}')
    return (sim.st().get('rec') or {}).get('flight_state') == target


def recover_all_needing(sim, label):
    """Single-board Review recovery for every board that still requires it (query first if needed)."""
    for b in BOARDS:
        r = sim.rows().get(b) or {}
        if r.get('recovery_required') or r.get('abort_latched'):
            res = sim.recovery('recover_abort', board=b, label=f'{label} recover {b}')
            if not res.get('prepared') and 'fresh' in json.dumps(res.get('body')):
                sim.api('/api/board-recovery/query', {'board': b}); sim.wait_v(1500)
                res = sim.recovery('recover_abort', board=b, label=f'{label} recover {b} (after query)')
            sim.check(f'{label}: single-board recovery {b}', res.get('success') is True, duration_ms=res.get('duration_ms'), op=res.get('op') or res.get('body'))


def sc_hitl_full(sim):
    a = sim.a; S = a.sched; hitl = a.mode == 'hitl_mode'
    global BOARDS
    BOARDS = present_boards(sim)
    snap = sim.wait_ready(BOARDS, timeout_ms=a.ready_timeout_ms)
    sim.check('startup: all seven boards healthy', snap is not None, rows={b: (sim.rows().get(b) or {}).get('readback_status') for b in BOARDS})
    en, why = lock_reason(sim); sim.check('startup: Launch not blocked by abort/lock', not any(x in why for x in ('locked', 'Abort', 'missed', 'unfinished')), enabled=en, reason=why)

    def g_prepare_replaces_query():
        # (g): operator Query outstanding on every board, then prepare Reset all immediately.
        for b in BOARDS: sim.api('/api/board-recovery/query', {'board': b})
        r = sim.recovery('reset_startup', boards=BOARDS, label='reset all with outstanding queries')
        sim.check('(g) prepare accepted beside outstanding queries', r.get('prepared') is True, body=r.get('body'))
        sim.check('(f) Reset all boards from Idle completes <=15 s', r.get('success') is True and (r.get('duration_ms') or 10**9) <= 15000, duration_ms=r.get('duration_ms'), op=r.get('op'))
        verify_clear(sim, 'after (g)/(f) reset')
    step(sim, 'g_prepare_replaces_query', g_prepare_replaces_query)

    def phase_controls_and_resync():
        ok = phase_to(sim, 5, 'advance to NitrousFill')
        sim.check('HITL State +1 reaches NitrousFill', ok, flight_state=(sim.st().get('rec') or {}).get('flight_state'))
        for b in ('AB', 'VB'):
            r = sim.recovery('resync', board=b, label=f'resync {b} in NitrousFill')
            if not r.get('prepared'):
                sim.check(f'Resync {b} in fill: refusal names next action', len(json.dumps(r.get('body'))) > 30, body=r.get('body'))
            else:
                sim.check(f'Resync {b} in fill phase', r.get('success') is True, duration_ms=r.get('duration_ms'), op=r.get('op'))
        ok = phase_to(sim, 1, 'rewind to Idle')
        sim.check('HITL State -1 returns to Idle', ok, flight_state=(sim.st().get('rec') or {}).get('flight_state'))
    if hitl: step(sim, 'phase_controls_and_resync', phase_controls_and_resync)

    def launch_lock():
        ok = phase_to(sim, 6, 'advance to Armed')
        sim.check('HITL reaches Armed', ok, flight_state=(sim.st().get('rec') or {}).get('flight_state'))
        if not ok:
            sim.log_action('launch_lock_skipped', reason='Armed not reached'); return
        en, why = lock_reason(sim); sim.log_action('pre_launch_policy', enabled=en, reason=why)
        sim.check('(c) no launch lock before any attempt', 'locked after the last attempt' not in why and not (sim.st().get('rec') or {}).get('automatic_actions_inhibited'), reason=why)
        code, body = sim.command('Launch')
        s2 = sim.wait_until(lambda s: 'locked after the last attempt' in lock_reason(sim, s)[1] or (s.get('rec') or {}).get('automatic_actions_inhibited'), 15000, 'lock set after Launch')
        sim.check('(c) lock set after a real Launch attempt', s2 is not None, launch_http=code, launch_body=body, reason=lock_reason(sim)[1])
        sim.wait_v(3000)
        sim.command('ResetTClock'); sim.wait_v(12000)
        en, why = lock_reason(sim)
        sim.check('(c) T-clock reset alone does not release the lock', (sim.st().get('rec') or {}).get('automatic_actions_inhibited') or 'locked' in why, reason=why, rec_inhibited=(sim.st().get('rec') or {}).get('automatic_actions_inhibited'))
        ok = phase_to(sim, 1, 'rewind to Idle after Launch')
        sim.log_action('after_rewind', ok=ok, flight_state=(sim.st().get('rec') or {}).get('flight_state'), rows={b: (sim.rows().get(b) or {}).get('state_label') for b in BOARDS})
        s3 = sim.wait_until(lambda s: not (s.get('rec') or {}).get('automatic_actions_inhibited') and 'locked after' not in lock_reason(sim, s)[1], 40000, 'lock released by healthy readbacks')
        released_by = 'healthy_readbacks' if s3 else None
        if not s3:
            sim.save_snapshot('lock-not-released-by-health')
            r = sim.recovery('reset_startup', boards=BOARDS, label='reset all to release launch lock')
            s3 = sim.wait_until(lambda s: not (s.get('rec') or {}).get('automatic_actions_inhibited') and 'locked after' not in lock_reason(sim, s)[1], 20000, 'lock released by Reset all')
            released_by = 'reset_all' if s3 else None
        sim.check('(c) launch lock releases (healthy readbacks or Reset all boards)', s3 is not None, released_by=released_by, reason=lock_reason(sim)[1])
        recover_all_needing(sim, 'post-launch'); verify_clear(sim, 'after launch-lock test')
    if hitl: step(sim, 'launch_lock', launch_lock)

    def abort_idle():
        if hitl: phase_to(sim, 1, 'to Idle')
        latch_all(sim, 'abort from Idle')
        code, body = sim.command('AdvanceFlightState')
        sim.log_action('advance_while_aborted', http=code, body=body, flight_state=(sim.st().get('rec') or {}).get('flight_state'))
        en, why = lock_reason(sim); sim.check('(a) aborted: Launch disabled with a reason', not en and len(why) > 10, reason=why)
        r = sim.recovery('reset_startup', boards=BOARDS, label='reset all after Idle abort')
        sim.check('(a)(f) Reset all after Idle abort <=15 s', r.get('success') is True and (r.get('duration_ms') or 10**9) <= 15000, duration_ms=r.get('duration_ms'), op=r.get('op') or r.get('body'))
        recover_all_needing(sim, 'idle-abort leftovers'); verify_clear(sim, 'after Idle abort')
    step(sim, 'abort_idle', abort_idle)

    def abort_fill():
        phase_to(sim, 4, 'to NitrogenFill')
        latch_all(sim, 'abort from fill')
        r = sim.recovery('resync', board='AB', label='resync while aborted')
        sim.check('resync while aborted: refused or recovers with a clear result', (not r.get('prepared') and len(json.dumps(r.get('body'))) > 30) or r.get('success') is not None, r=r.get('body') or r.get('op'))
        recover_all_needing(sim, 'fill abort'); verify_clear(sim, 'after fill abort (per-board)')
        phase_to(sim, 1, 'to Idle after fill abort')
    if hitl: step(sim, 'abort_fill', abort_fill)
    if a.cycles:
        def cycles():
            for n in range(1, a.cycles + 1):
                latch_all(sim, f'cycle{n}')
                if n % 2:
                    r = sim.recovery('reset_startup', boards=BOARDS, label=f'reset all cycle{n}')
                    sim.check(f'cycle{n}: Reset all boards succeeds within 15 s', r.get('success') is True and (r.get('duration_ms') or 10**9) <= 15000, duration_ms=r.get('duration_ms'), op=r.get('op') or r.get('body'))
                recover_all_needing(sim, f'cycle{n}')
                verify_clear(sim, f'cycle{n}')
        step(sim, 'abort_clear_cycles', cycles)

    if S.get('can0'):
        def failed_reset():
            wait_until_v(sim, (S['can0'] + 6) * 1000, 'DAQ CAN cut')
            latch_all(sim, 'abort with DAQ unreachable', expect=[b for b in BOARDS if b != 'DAQ'])
            r = sim.recovery('reset_startup', boards=BOARDS, label='reset all with DAQ unreachable', timeout_ms=40000)
            sim.save_snapshot('failed-reset')
            sim.log_action('failed_reset_result', r={k: r.get(k) for k in ('prepared', 'success', 'duration_ms', 'op', 'body')})
            others = [b for b in BOARDS if b != 'DAQ']
            sim.check('(b) reset with one board silent: others recover', sim.all_present_healthy(sim.st(), others) or not r.get('prepared'), rows={b: (sim.rows().get(b) or {}).get('readback_status') for b in BOARDS}, prepare=r.get('body'), op=r.get('op'))
            if not r.get('prepared') or not r.get('success'):
                recover_all_needing(sim, 'DAQ-cut others')
            wait_until_v(sim, (S['can1'] + 6) * 1000, 'DAQ CAN restored')
            sim.wait_until(lambda s: (sim.rows(s).get('DAQ') or {}).get('capabilities', {}).get('endpoint_verified'), 30000, 'DAQ endpoint back')
            recover_all_needing(sim, 'DAQ back')
            en, why = lock_reason(sim)
            sim.check('(b) healthy boards do not block the remaining single-board recovery', sim.all_present_healthy(sim.st()), reason=why, rows={b: {k: (sim.rows().get(b) or {}).get(k) for k in ('readback_status', 'recovery_required', 'recovery_reason')} for b in BOARDS})
            verify_clear(sim, 'after failed-reset recovery')
        step(sim, 'failed_reset', failed_reset)

    if S.get('gsrb'):
        def gs_restart():
            wait_until_v(sim, (S['gsrb'] - 8) * 1000, 'pre GS restart')
            latch_all(sim, 'abort before GS restart')
            wait_until_v(sim, int((S['gsrb'] - 0.6) * 1000), 'reset just before GS restart')
            r = sim.recovery('reset_startup', boards=BOARDS, label='reset all interrupted by GS restart', wait=False)
            sim.log_action('reset_sent_before_restart', r=r)
            wait_until_v(sim, (S['gsrb'] + 2) * 1000, 'GS restarted')
            back = sim.wait_until(lambda s: isinstance(s.get('rec'), dict) and s['rec'].get('backend_instance') and s['rec'].get('backend_instance') != (r.get('op') or {}).get('backend_instance', 'x') and s['rec'].get('boards'), 120000, 'GS back after restart')
            sim.save_snapshot('after-gs-restart')
            rec = sim.st().get('rec') or {}
            sim.log_action('post_restart_state', inhibited=rec.get('automatic_actions_inhibited'), host_recovery=rec.get('host_recovery'), aborted=sim.aborted_set(sim.st()), reason=lock_reason(sim)[1], flight_state=rec.get('flight_state'),
                           rows={b: {k: (sim.rows().get(b) or {}).get(k) for k in ('readback_status', 'recovery_required', 'recovery_reason', 'abort_latched', 'state_label')} for b in BOARDS})
            sim.wait_ready(timeout_ms=60000)
            clear_gse(sim, 'post-restart')
            rr = sim.recovery('reset_startup', boards=BOARDS, label='reset all after GS restart')
            sim.check('restart mid-recovery: Reset all boards accepted and succeeds', rr.get('success') is True, prepare=rr.get('body'), op=rr.get('op'), duration_ms=rr.get('duration_ms'))
            recover_all_needing(sim, 'post-restart')
            verify_clear(sim, 'after GS restart mid-recovery', timeout_ms=45000)
            s = sim.wait_until(lambda s: not (s.get('rec') or {}).get('automatic_actions_inhibited') and not ((s.get('rec') or {}).get('host_recovery') or {}).get('pending'), 30000, 'journal clean after restart')
            sim.check('restart mid-recovery: no inhibited/pending journal left', s is not None, rec_host_recovery=(sim.st().get('rec') or {}).get('host_recovery'), inhibited=(sim.st().get('rec') or {}).get('automatic_actions_inhibited'))
        step(sim, 'gs_restart_mid_recovery', gs_restart)

    if S.get('abrb'):
        def ab_reboot_latch():
            wait_until_v(sim, (S['abrb'] - 8) * 1000, 'pre AB reboot')
            latch_all(sim, 'abort before AB reboot')
            # Start AB Review recovery so its marker is PENDING when the AB loses power.
            wait_until_v(sim, int((S['abrb'] - 1.5) * 1000), 'AB recovery just before reboot')
            r0 = sim.recovery('recover_abort', board='AB', label='AB recovery interrupted by AB power cut', wait=False)
            sim.log_action('ab_recovery_before_reboot', r=r0)
            wait_until_v(sim, (S['abrb'] + 3) * 1000, 'AB rebooted')
            s = sim.wait_until(lambda s: (sim.rows(s).get('AB') or {}).get('readback_status') == 'readback_current' and (sim.rows(s).get('AB') or {}).get('uptime_ms', 10**9) < 60000, 60000, 'AB back after reboot')
            row = sim.rows().get('AB') or {}
            sim.check('AB abort latch survives MCU reset (backup register)', row.get('abort_latched') is True or row.get('recovery_reason') == 'abort_latched', row={k: row.get(k) for k in ('abort_latched', 'recovery_reason', 'state_label', 'uptime_ms', 'boot')})
            recover_all_needing(sim, 'after AB reboot')
            verify_clear(sim, 'after AB reboot latch')
        step(sim, 'ab_reboot_latch', ab_reboot_latch)

    if S.get('vbc0'):
        def vb_stale_reset():
            wait_until_v(sim, int((S['vbc0'] - 1.0) * 1000), 'reset just before VB cut')
            r = sim.recovery('reset_startup', boards=BOARDS, label='reset all whose VB job goes stale (VB CAN cut)', wait=False)
            sim.log_action('vb_stale_reset_sent', r=r)
            wait_until_v(sim, (S['vbc1'] + 8) * 1000, 'VB CAN restored')
            sim.wait_until(lambda s: (sim.rows(s).get('VB') or {}).get('readback_status') == 'readback_current', 40000, 'VB readback after restore')
            row = sim.rows().get('VB') or {}
            sim.save_snapshot('vb-after-stale-reset')
            sim.check('stale VB reset to Idle does not latch Abort', row.get('abort_latched') is not True and row.get('recovery_reason') != 'abort_latched' and 'VB' not in sim.aborted_set(sim.st()),
                      row={k: row.get(k) for k in ('abort_latched', 'recovery_reason', 'state_label', 'readback_status', 'result')}, op=(sim.st().get('rec') or {}).get('operation'))
            recover_all_needing(sim, 'after stale VB reset'); verify_clear(sim, 'after stale VB reset', timeout_ms=45000)
        step(sim, 'vb_stale_reset', vb_stale_reset)

    def overpressure():
        # (e) floating / implausible pressure: single-sample jumps to full scale and to an impossible value.
        base = 20.0; sim.pressure = base; sim.pressure_target = base; sim.pressure_rate = 1000
        for spike in (2500, 0, 2400, 3000, 2600):
            F.atomic(sim.diag / 'pressure.raw', str(F.raw_pressure(spike))); time.sleep(.4); F.atomic(sim.diag / 'pressure.raw', str(F.raw_pressure(base))); sim.wait_v(1500)
        sim.wait_v(3000)
        gse = sim.st().get('gse') or {}
        sim.check('(e) floating/implausible pressure spikes do not trip overpressure', not gse.get('overpressure_latched') and not sim.aborted_set(sim.st()), overpressure=gse.get('overpressure'), aborted=sim.aborted_set(sim.st()))
        sim.pressure_rate = 60; sim.pressure_target = 1200
        s = sim.wait_until(lambda s: (s.get('gse') or {}).get('overpressure_latched') or 'VB' in sim.aborted_set(s) or ((s.get('gse') or {}).get('overpressure') or {}).get('valve_board'), 40000, 'overpressure trips on real ramp')
        gse = sim.st().get('gse') or {}
        sim.check('(e) real pressure ramp trips overpressure', s is not None, pressure=sim.pressure, overpressure=gse.get('overpressure'), aborted=sim.aborted_set(sim.st()))
        sim.pressure_rate = 400; sim.pressure_target = 0
        sim.wait_until(lambda s: ((s.get('gse') or {}).get('pressure_psi') or 999) < 50, 20000, 'pressure vented')
        code, body = sim.api('/api/gse/overpressure/acknowledge', {})
        sim.log_action('overpressure_ack', http=code, body=body)
        sim.check('overpressure acknowledge accepted or refused with a reason', code == 200 or (code == 409 and len(json.dumps(body)) > 20), http=code, body=body)
        recover_all_needing(sim, 'after overpressure'); clear_gse(sim, 'after overpressure')
        if not sim.all_present_healthy(sim.st()):
            sim.recovery('reset_startup', boards=BOARDS, label='reset all after overpressure')
        code, body = sim.api('/api/gse/overpressure/acknowledge', {}); sim.log_action('overpressure_ack2', http=code, body=body)
        verify_clear(sim, 'after overpressure', timeout_ms=45000)
        gse = sim.st().get('gse') or {}
        sim.check('overpressure latch cleared afterwards', not gse.get('overpressure_latched'), overpressure=gse.get('overpressure'))
    step(sim, 'overpressure', overpressure)
    sim.save_snapshot('end')


def sc_abort_probe(sim):
    boards = present_boards(sim)
    snap = sim.wait_ready(boards, timeout_ms=sim.a.ready_timeout_ms)
    sim.check('startup: present boards healthy', snap is not None, rows={b: (sim.rows().get(b) or {}).get('readback_status') for b in boards})
    for n in range(sim.a.cycles):
        latch_all(sim, f'probe cycle{n}', expect=boards)
        sim.save_snapshot(f'latched{n}')
        for b in boards:
            r = sim.recovery('recover_abort', board=b, label=f'cycle{n} recover {b}')
            sim.check(f'cycle{n}: recover {b}', r.get('success') is True, duration_ms=r.get('duration_ms'), op=r.get('op') or r.get('body'))
        verify_clear(sim, f'probe cycle{n}', boards=boards)


# ------------------------------------------------------------------ websocket watcher (stdlib)
class WsWatch:
    """Reads /ws on a forwarded origin and logs selected message types (virtual-time stamped)."""
    KEEP = ('StationModeChanged', 'ActionPolicy', 'Notification', 'NetworkTime', 'Warning', 'Error')

    def __init__(self, sim, origin, token, tag='ws'):
        import socket as _s, base64 as _b
        self.sim = sim; self.out = (sim.out / f'{tag}.jsonl').open('a'); self.counts = {}; self.events = []
        host, port = origin.split('//')[1].split(':')
        self.sock = _s.create_connection((host, int(port)), timeout=10)
        key = _b.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f'GET /ws?token={token} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                           f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
        buf = b''
        while b'\r\n\r\n' not in buf: buf += self.sock.recv(4096)
        head, self.buf = buf.split(b'\r\n\r\n', 1)
        assert b' 101 ' in head.split(b'\r\n')[0], head[:200]
        self.sock.settimeout(1.0); self.alive = True
        threading.Thread(target=self._run, daemon=True).start()

    def _read(self, n):
        while len(self.buf) < n:
            try: d = self.sock.recv(1 << 16)
            except TimeoutError: 
                if self.sim.stop.is_set(): raise EOFError
                continue
            if not d: raise EOFError
            self.buf += d
        out, self.buf = self.buf[:n], self.buf[n:]; return out

    def _run(self):
        frag = b''
        try:
            while not self.sim.stop.is_set():
                h = self._read(2); fin = h[0] & 0x80; op = h[0] & 0x0f; ln = h[1] & 0x7f
                if ln == 126: ln = int.from_bytes(self._read(2), 'big')
                elif ln == 127: ln = int.from_bytes(self._read(8), 'big')
                data = self._read(ln)
                if op == 8: break
                if op == 9:  # ping -> pong (masked)
                    m = os.urandom(4); self.sock.sendall(bytes([0x8a, 0x80 | len(data)]) + m + bytes(b ^ m[i % 4] for i, b in enumerate(data))); continue
                if op in (0, 1, 2): frag += data
                if not fin or op not in (0, 1): continue
                text, frag = frag, b''
                m = re.match(rb'\{"ty":"(\w+)"', text); ty = m.group(1).decode() if m else '?'
                self.counts[ty] = self.counts.get(ty, 0) + 1
                if ty in self.KEEP:
                    row = {'v': self.sim.vclock(), 'ty': ty, 'n': self.counts[ty]}
                    if ty in ('StationModeChanged', 'Notification', 'Warning', 'Error'): row['data'] = json.loads(text).get('data')
                    if ty == 'ActionPolicy':
                        try:
                            d = json.loads(text).get('data') or {}; row['launch'] = next((c for c in d.get('controls', []) if c.get('cmd') == 'Launch'), None); row['n_controls'] = len(d.get('controls', []))
                        except Exception: pass
                    if ty == 'NetworkTime': row['data'] = json.loads(text).get('data')
                    self.events.append(row)
                    if ty != 'NetworkTime' or self.counts[ty] % 20 == 1:
                        self.out.write(json.dumps(row, default=str) + '\n'); self.out.flush()
        except (EOFError, OSError):
            pass
        self.alive = False


def station_mode_switch(sim, target, label):
    """In-process switch via /api/station-modes prepare+confirm; checks API, layout and WS announce."""
    ws = getattr(sim, 'ws', None); n0 = len(ws.events) if ws else 0
    code, st = sim.api('/api/station-modes'); sim.log_action('station_modes_status', label=label, http=code, current=(st or {}).get('current') if code == 200 else st,
                                                             reason=(st or {}).get('reason') if code == 200 else None)
    code_l0, layout0 = sim.api('/api/layout')
    opt = next((o for o in st.get('options', []) if o['id'] == target), None) if code == 200 else None
    sim.check(f'{label}: target {target} offered in process', bool(opt and opt.get('in_process') and opt.get('available')), option=opt, reason=st.get('reason') if code == 200 else st)
    if not opt or not opt.get('available'): return False
    binding = {k: st[k] for k in ('backend_instance', 'principal_context', 'authority_id', 'revision', 'policy_epoch')}
    binding.update(current_id=st['current']['id'], target_id=target)
    code, ch = sim.api('/api/station-modes/prepare', binding); sim.log_action('station_mode_prepare', label=label, http=code, body=ch if code != 200 else {k: ch.get(k) for k in ('summary', 'required_confirmation')})
    if code != 200: sim.check(f'{label}: prepare accepted', False, http=code, body=ch); return False
    v0 = sim.vclock()
    code, rc = sim.api('/api/station-modes/confirm', {**binding, 'ticket': ch['ticket'], 'transition_id': ch['transition_id'], 'confirmation': ch['required_confirmation']})
    sim.log_action('station_mode_confirm', label=label, http=code, body=rc)
    if code != 200: sim.check(f'{label}: confirm accepted', False, http=code, body=rc); return False
    want = target.split('-')[0]; t_end = time.time() + 60; st2 = None
    while time.time() < t_end:
        c2, st2 = sim.api('/api/station-modes')
        if c2 == 200 and st2['current']['mode_id'] == want and not st2.get('transition', {}) or (c2 == 200 and st2['current']['mode_id'] == want and (st2.get('transition') or {}).get('state') != 'pending'): break
        time.sleep(.2)
    sim.check(f'{label}: station reports {want} after switch', st2 and st2['current']['mode_id'] == want, current=(st2 or {}).get('current'), transition=(st2 or {}).get('transition'),
              layout_revision=(st2 or {}).get('layout_revision'), switched_v_ms=sim.vclock() - v0)
    code_l1, layout1 = sim.api('/api/layout')
    sim.check(f'{label}: /api/layout changes with the mode', code_l1 == 200 and json.dumps(layout1, sort_keys=True) != json.dumps(layout0, sort_keys=True),
              before_keys=sorted(layout0)[:12] if isinstance(layout0, dict) else str(layout0)[:200], after_keys=sorted(layout1)[:12] if isinstance(layout1, dict) else str(layout1)[:200])
    (sim.out / f'layout-{label}-before.json').write_text(json.dumps(layout0, indent=1)); (sim.out / f'layout-{label}-after.json').write_text(json.dumps(layout1, indent=1))
    if ws:
        t_end = time.time() + 20
        while time.time() < t_end and not any(e['ty'] == 'StationModeChanged' for e in ws.events[n0:]): time.sleep(.2)
        evs = [e for e in ws.events[n0:] if e['ty'] in ('StationModeChanged', 'ActionPolicy')][:4]
        smc = next((e for e in evs if e['ty'] == 'StationModeChanged'), None)
        pol_after = next((e for e in ws.events[n0:] if e['ty'] == 'ActionPolicy' and smc and e['n'] and ws.events.index(e) > ws.events.index(smc)), None)
        sim.check(f'{label}: websocket announces StationModeChanged then a fresh ActionPolicy', smc is not None and pol_after is not None and (smc.get('data') or {}).get('layout_revision') == (st2 or {}).get('layout_revision'),
                  station_mode_changed=smc, ws_alive=ws.alive, policy_after=bool(pol_after))
    c3, pol = sim.api('/api/action_policy'); sim.log_action('policy_after_switch', label=label, http=c3, launch=sim.launch_control({'policy': pol}) if c3 == 200 else pol)
    return True


def gps_phases(sim):
    """RF GPS fix (RF should lead UTC) then GPS lost (30 s holdover, then a consumer)."""
    a = sim.a
    if not a.gps_fix_ms: return
    sim.log_action('gps_fix_on'); F.atomic(sim.diag / 'gps.fix', '1'); sim.save_snapshot('gps-fix-start')
    sim.wait_v(a.gps_fix_ms); sim.save_snapshot('gps-fix-end')
    sim.log_action('gps_fix_off'); F.atomic(sim.diag / 'gps.fix', '0')
    sim.wait_v(a.gps_lost_ms); sim.save_snapshot('gps-lost-end'); sim.log_action('gps_phases_end')


def sc_timesync_comms(sim):
    """Comms measurement (as sc_comms), then GPS phases, then in-process station mode switches while idle."""
    sim.ws = None
    try:
        sim.ws = WsWatch(sim, sim.origin, sim.token)
    except Exception as e:
        sim.log_action('ws_failed', error=repr(e))
    sc_comms(sim)
    gps_phases(sim)
    for i, tgt in enumerate([t for t in sim.a.mode_switch.split(',') if t]):
        station_mode_switch(sim, tgt, f'switch{i}-{tgt}')
        sim.wait_v(5000)
        # The new mode still serves Query on an umbilical and an AV-bay board.
        for b in ('AB', 'RF'):
            code, body = sim.api('/api/board-recovery/query', {'board': b}); sim.log_action('query_after_switch', board=b, http=code, body=body)
        sim.wait_v(5000)
    if sim.ws: (sim.out / 'ws-counts.json').write_text(json.dumps(sim.ws.counts, indent=1))
    sim.save_snapshot('end-all')


def sc_pairing(sim):
    """Primary <-> RRLS pairing over the shared rocket radio, driven through both stations' admin APIs."""
    snap = sim.wait_ready(present_boards(sim), timeout_ms=sim.a.ready_timeout_ms)
    sim.check('startup: boards ready (primary)', snap is not None)
    rf = F.Forwarder(sim.addr); rf.destination = (sim.addr, 3001); sim.rrls_fwd = rf
    rtok = None
    for _ in range(600):
        try: rtok = F.login(rf.origin, sim.key); break
        except (AssertionError, OSError, KeyError, TypeError): time.sleep(.5)
    sim.check('RRLS HTTP up and login works', rtok is not None, origin=rf.origin)
    if not rtok: return
    def rapi(path, body=None):
        try: return F.request(rf.origin, path, body, rtok, 30)
        except OSError as e: return 0, {'os_error': str(e)}
    rows = (sim.out / 'pairing.jsonl').open('a')
    def both(tag):
        c1, p = sim.api('/api/station/pairing'); c2, r = rapi('/api/station/pairing')
        row = {'v': sim.vclock(), 'tag': tag, 'primary': p if c1 == 200 else {'http': c1, 'body': p}, 'rrls': r if c2 == 200 else {'http': c2, 'body': r}}
        rows.write(json.dumps(row, default=str) + '\n'); rows.flush(); return row
    r0 = both('initial')
    sim.check('both stations offer radio pairing', (r0['primary'].get('radio_available') and r0['rrls'].get('radio_available')) is True,
              primary={k: r0['primary'].get(k) for k in ('role', 'paired', 'radio_available', 'error')}, rrls={k: r0['rrls'].get(k) for k in ('role', 'paired', 'radio_available', 'error', 'primary_link')})
    v0 = sim.vclock()
    c1, b1 = sim.api('/api/station/pairing/start', {}); sim.log_action('pair_start_primary', http=c1, body=b1 if c1 != 200 else (b1 or {}).get('exchange'))
    c2, b2 = rapi('/api/station/pairing/start', {}); sim.log_action('pair_start_rrls', http=c2, body=b2 if c2 != 200 else (b2 or {}).get('exchange'))
    ph = lambda r, side: ((r.get(side) or {}).get('exchange') or {}).get('phase')
    code = None; v_compare = None; t_end = time.time() + 3600
    while time.time() < t_end and not sim.stop.is_set():
        r = both('wait-compare')
        if ph(r, 'primary') == 'failed' or ph(r, 'rrls') == 'failed': break
        cp = ((r['primary'].get('exchange') or {}).get('code')); cr = ((r['rrls'].get('exchange') or {}).get('code'))
        if ph(r, 'primary') == 'compare' and ph(r, 'rrls') == 'compare' and cp and cp == cr:
            code = cp; v_compare = sim.vclock(); break
        if sim.vclock() - v0 > 300000: break
        time.sleep(.5)
    sim.check('pairing reaches the same six-digit code on both stations', code is not None, compare_after_v_ms=(v_compare - v0) if v_compare else None, last=both('after-compare'))
    if not code: return
    c1, b1 = sim.api('/api/station/pairing/confirm', {'code': code}); sim.log_action('pair_confirm_primary', http=c1, body=b1 if c1 != 200 else (b1 or {}).get('exchange'))
    c2, b2 = rapi('/api/station/pairing/confirm', {'code': code}); sim.log_action('pair_confirm_rrls', http=c2, body=b2 if c2 != 200 else (b2 or {}).get('exchange'))
    v1 = sim.vclock(); done = None
    while time.time() < t_end and not sim.stop.is_set():
        r = both('wait-saved')
        if ph(r, 'primary') == 'failed' or ph(r, 'rrls') == 'failed': break
        if r['primary'].get('paired') and r['rrls'].get('paired'): done = sim.vclock(); break
        if sim.vclock() - v1 > 300000: break
        time.sleep(.5)
    last = both('final')
    sim.check('pairing saved on both stations (both status pages report paired)', done is not None, saved_after_confirm_v_ms=(done - v1) if done else None, total_v_ms=(done - v0) if done else None,
              primary={k: last['primary'].get(k) for k in ('paired', 'station_id', 'restart_required', 'exchange')}, rrls={k: last['rrls'].get(k) for k in ('paired', 'station_id', 'restart_required', 'exchange', 'primary_link')})
    sim.wait_v(20000); both('settled')


SCENARIOS = {'abort_probe': sc_abort_probe, 'comms': sc_comms, 'recovery': sc_recovery, 'hitl_full': sc_hitl_full,
             'timesync_comms': sc_timesync_comms, 'pairing': sc_pairing}

# ------------------------------------------------------------------ hardware-faithful scenario
def gs_view(sim):
    """Pi-equivalent live counters, virtual-time stamped (faithful.jsonl)."""
    v = sim.vclock(); row = {'v': v, 'wall': round(time.time(), 3)}
    for key, path in (('netdiag', '/api/network_diagnostics'), ('topology', '/api/network_topology'), ('modes', '/api/station-modes'),
                      ('recording', '/api/recording_status')):
        code, data = sim.api(path, timeout=20); row[key] = data if code == 200 else {'http': code, 'body': str(data)[:300]}
    st = sim.st(); row['rec'] = st.get('rec'); row['policy_launch'] = sim.launch_control(st); row['abort'] = st.get('abort')
    if isinstance(row['topology'], dict):
        row['topology'] = {'health': row['topology'].get('health'), 'simulated': row['topology'].get('simulated')}
    pace = list((sim.diag / 'temp').glob('*/virtual-clock.txt.pace'))
    if pace:
        try: row['pace'] = pace[0].read_text().split()
        except OSError: pass
    with sim.lock:
        sim.faithful.write(json.dumps(row, default=str, separators=(',', ':')) + '\n'); sim.faithful.flush()
    return row


def side(nd, name):
    return next((x for x in (nd or {}).get('sides', []) if x.get('name') == name), None) if isinstance(nd, dict) else None


def type_deltas(a, z, name):
    s0, s1 = side(a, name), side(z, name)
    if not s0 or not s1: return {}
    t0 = {t['data_type']: t for t in s0['data_types']}
    return {t['data_type']: (t['tx_packets'] - t0.get(t['data_type'], {}).get('tx_packets', 0), t['tx_retries'] - t0.get(t['data_type'], {}).get('tx_retries', 0))
            for t in s1['data_types']}


FRESH_PROOF_MS = 5000  # board_abort.rs PROOF_TTL


def board_fresh(row):
    return row.get('readback_status') == 'readback_current' and (row.get('age_ms') is not None and row['age_ms'] <= FRESH_PROOF_MS)


def sc_faithful(sim):
    """Normal mode, all 7 boards, Idle, hardware-faithful RFD link (fault list 1,2,3,6,8,10)."""
    a = sim.a; sim.faithful = (sim.out / 'faithful.jsonl').open('a'); rows = []
    boards = present_boards(sim)
    t_end = sim.vclock() + a.phase_a_ms; last = -10**9; warm = 30000
    sim.log_action('phase_a_start', boards=boards)
    while sim.vclock() < t_end and not sim.stop.is_set():
        if sim.vclock() - last >= 5000:
            last = sim.vclock(); r = gs_view(sim); rows.append(r)
        time.sleep(.1)
    sim.log_action('phase_a_end')
    win = [r for r in rows if r['v'] >= warm and isinstance(r.get('netdiag'), dict) and r['netdiag'].get('sides')]
    if len(win) < 3:
        sim.check('faithful: enough netdiag samples', False, n=len(win)); return
    a0, z = win[0], win[-1]; dt = (z['v'] - a0['v']) / 1000
    d = type_deltas(a0['netdiag'], z['netdiag'], 'rocket_comms')
    tx = sum(x[0] for x in d.values()); spins = sum(x[1] for x in d.values())
    names = {1: 'RELIABLE_ACK', 4: 'TIME_SYNC_ANNOUNCE', 7: 'DISCOVERY_ANNOUNCE', 9: 'DISCOVERY_TOPOLOGY', 10: 'DISCOVERY_SCHEMA', 11: 'DISCOVERY_SCHEMA_REQUEST',
             13: 'MANAGED_VARIABLE_REQUEST', 17: 'DISCOVERY_ADDRESS', 106: 'HEARTBEAT', 200: 'BOARD_RECOVERY_REQUEST'}
    per = {names.get(k, str(k)): {'tx': v[0], 'refused_retries': v[1]} for k, v in sorted(d.items()) if v != (0, 0)}
    q0, q1 = a0['netdiag']['queues']['tx_len'], z['netdiag']['queues']['tx_len']
    rb = lambda r: ((r['netdiag'].get('radio_tx_budget') or {}).get('refusals') or {})
    ref = {k: rb(z).get(k, 0) - rb(a0).get(k, 0) for k in rb(z)}
    summary = {'window_virtual_s': dt, 'rocket_tx_frames_per_s': round(tx / dt, 2), 'rocket_refusal_spins_per_s': round(spins / dt, 1),
               'tx_len': [q0, q1], 'tx_len_growth_per_s': round((q1 - q0) / dt, 2), 'reliable_end': z['netdiag'].get('reliable'),
               'refusals_delta': ref, 'per_type': per, 'discovery_interval_ms': (z['netdiag'].get('discovery') or {}).get('interval_ms')}
    sim.result['faithful_summary'] = summary; sim.log_action('faithful_summary', **summary)
    # (1) radio writer keeps up with the station's own demand
    sim.check('(1) rocket_comms router tx queue does not grow (<= 0.5 frames/s, ends <= 200)', (q1 - q0) / dt <= 0.5 and q1 <= 200, tx_len=[q0, q1], growth_per_s=summary['tx_len_growth_per_s'])
    sim.check('(1) refusal spins stay bounded (<= 20 per on-air frame)', spins <= 20 * max(tx, 1), tx=tx, spins=spins, per_type=per)
    sim.check('(1) reliable pending stays bounded (<= 8)', (z['netdiag'].get('reliable') or {}).get('pending', 99) <= 8, reliable=z['netdiag'].get('reliable'))
    for k, nm, need in ((106, 'HEARTBEAT', 6), (4, 'TIME_SYNC_ANNOUNCE', 2), (7, 'DISCOVERY_ANNOUNCE', 2)):
        n = d.get(k, (0, 0))[0] * 60 / dt
        sim.check(f'(1) {nm} reaches the air (>= {need}/min on rocket_comms)', n >= need, per_min=round(n, 2), refused_retries=d.get(k, (0, 0))[1])
    # (10) every board continuously fresh for the launch proof
    fr = {}; miss_reason = 0
    for r in win:
        rec = {x['id']: x for x in ((r.get('rec') or {}).get('boards') or [])} if isinstance(r.get('rec'), dict) else {}
        for b in boards:
            fr.setdefault(b, []).append(board_fresh(rec.get(b) or {}))
        if 'fresh required board state is missing' in json.dumps(r.get('policy_launch') or {}): miss_reason += 1
    frac = {b: round(sum(v) / len(v), 3) for b, v in fr.items()}
    ages = {b: max(((x.get('age_ms') or 0) for r in win for x in ((r.get('rec') or {}).get('boards') or []) if x.get('id') == b), default=None) for b in boards}
    sim.check('(10) all boards continuously fresh for the launch proof (readback_current, age <= 5 s, >= 98% of samples)', all(f >= 0.98 for f in frac.values()),
              fraction_fresh=frac, max_age_ms=ages)
    sim.check('(10) Launch never reports "fresh required board state is missing"', miss_reason == 0, samples_with_reason=miss_reason, last=z.get('policy_launch'))
    recst = z.get('rec') or {}
    sim.check('(10) recovery status shows no rejected query replies', not recst.get('rejected_statuses'), rejected_statuses=recst.get('rejected_statuses'), last_rejection=recst.get('last_rejection'))
    # (6) DAQ details
    def daq(r):
        h = next((x for x in ((r.get('topology') or {}).get('health') or []) if x.get('sender_id') == 'DAQ'), {}) if isinstance(r.get('topology'), dict) else {}
        return h
    dq = [daq(r) for r in win]
    sd_ok = [bool((h.get('diagnostics') or {}).get('sd_flags') is not None and ((h.get('diagnostics') or {}).get('sd_status_received_age_ms') or 10**9) <= 10000) for h in dq]
    sim.check('(6) DAQ recording state is shown (sd status <= 10 s old in >= 95% of samples)', sum(sd_ok) >= 0.95 * len(sd_ok), fraction=round(sum(sd_ok) / max(1, len(sd_ok)), 3), last=(dq[-1].get('diagnostics') or {}) if dq else None)
    dqrec = [next((x for x in ((r.get('rec') or {}).get('boards') or []) if x.get('id') == 'DAQ'), {}) for r in win]
    pend = max(((x.get('operation') or {}).get('age_ms') or 0) for x in dqrec if (x.get('operation') or {}).get('action') == 'query' and not (x.get('operation') or {}).get('completed')) if any((x.get('operation') or {}).get('action') == 'query' and not (x.get('operation') or {}).get('completed') for x in dqrec) else 0
    sim.check('(6) DAQ board state confirmed (not Startup/unconfirmed/stale) and no query pending > 10 s', all(board_fresh(x) and x.get('state_label') not in (None, 'Unknown') for x in dqrec[-5:]) and pend <= 10000,
              last_rows=[{k: x.get(k) for k in ('state_label', 'readback_status', 'age_ms')} for x in dqrec[-3:]], longest_pending_query_ms=pend)
    # (8) clock offsets
    h = (z.get('topology') or {}).get('health') or []
    offs = {x.get('sender_id'): x.get('clock_offset_ms') for x in h}
    sim.check('(8) network health clock_offset_ms present for every board', all(offs.get(b) is not None for b in boards), clock_offset_ms=offs)
    # (3)/(ii) mode switch availability at Idle and the switch itself
    m = z.get('modes') or {}
    opt = next((o for o in m.get('options', []) if o.get('id') == 'hitl'), None) if isinstance(m, dict) else None
    sim.check('(3) HITL switch offered while Idle', bool(opt and opt.get('available')), option=opt, reason=m.get('reason') if isinstance(m, dict) else m)
    ok = station_mode_switch(sim, 'hitl', 'idle-normal-to-hitl')
    if ok: station_mode_switch(sim, 'normal', 'idle-hitl-to-normal')
    # (2)/(i) abort at Idle with a backlogged radio, then Clear GSE abort within a bounded time
    code, body = sim.command('Abort'); va = sim.vclock()
    # (iii) immediately after: an Abort still genuinely in delivery must hold the mode switch
    c3, st3 = sim.api('/api/station-modes'); o3 = next((o for o in (st3 or {}).get('options', []) if o.get('id') == 'hitl'), None) if c3 == 200 else None
    sim.check('(iii) mode switch refused while the Abort is still being delivered', not (o3 or {}).get('available', False), option=o3, reason=(st3 or {}).get('reason') if c3 == 200 else st3)
    sim.wait_until(lambda s: (s.get('rec') or {}).get('flight_state') == 15, 30000, 'station Aborted')
    sim.wait_v(15000)
    r = gs_view(sim)
    m = r.get('modes') or {}
    opt = next((o for o in m.get('options', []) if o.get('id') == 'hitl'), None) if isinstance(m, dict) else None
    sim.check('(ii) mode switch offered while Aborted at Idle with nothing moving (15 s after Abort)', bool(opt and opt.get('available')), option=opt, reason=m.get('reason') if isinstance(m, dict) else m)
    v0 = sim.vclock(); cleared = None; tries = []
    while sim.vclock() - v0 < 120000 and not sim.stop.is_set():
        c, b = sim.api('/api/gse/abort/clear', {}); tries.append({'v': sim.vclock(), 'http': c, 'body': str(b)[:200]})
        if c == 200: cleared = sim.vclock(); break
        sim.wait_v(5000)
    sim.check('(2)(i) Clear GSE abort succeeds within 60 s of trying (radio backlogged)', cleared is not None and cleared - v0 <= 60000, after_v_ms=(cleared - v0) if cleared else None, attempts=len(tries), last=tries[-1] if tries else None)
    gs_view(sim); sim.save_snapshot('faithful-end')


SCENARIOS['faithful'] = sc_faithful


def sc_vb_stale(sim):
    """VB offline ~30 s then back (scheduled --can-cut VB:s0:s1): no [S] stale-sensor warning during the outage or
    within 10 s of reconnecting; an injected F (invalid, raw 4095) fault inside the grace still warns."""
    a = sim.a; s0, s1 = a.sched['vbc0'], a.sched['vbc1']
    sim.ws = WsWatch(sim, sim.origin, sim.token, tag='ws-vb')
    def sensor_events(lo, hi, needle):
        return [e for e in sim.ws.events if e['ty'] in ('Warning', 'Notification', 'Error') and lo <= e['v'] <= hi and needle in json.dumps(e.get('data'))]
    wait_until_v(sim, (s0 - 5) * 1000, 'VB steady before cut')
    pre = sensor_events(0, sim.vclock(), 'overpressure sensor')
    sim.log_action('vb_pre_cut', sensor_events=pre[-3:])
    wait_until_v(sim, s1 * 1000, 'VB CAN restored')
    back = sim.wait_until(lambda s: (sim.rows(s).get('VB') or {}).get('readback_status') == 'readback_current' or any(
        x.get('sender_id') == 'VB' for x in []), 60000, 'VB seen again')
    wait_until_v(sim, s1 * 1000 + 3000, 'inject F inside the grace')
    sim.log_action('inject_F'); v_f = sim.vclock()
    F.atomic(sim.diag / 'pressure.raw', '4095'); sim.wait_v(3000); F.atomic(sim.diag / 'pressure.raw', str(F.raw_pressure(0)))
    wait_until_v(sim, s1 * 1000 + 25000, 'after grace')
    stale = sensor_events(s0 * 1000, s1 * 1000 + 10000, 'stale')
    fault = sensor_events(v_f, sim.vclock(), '[F')
    fault = fault or sensor_events(v_f, sim.vclock(), 'invalid')
    code, gse = sim.api('/api/gse/status')
    sim.check('VB outage and 10 s reconnect grace raise no stale-sensor [S] warning', not stale, stale_events=stale[:4], vb_cut_v_s=[s0, s1])
    sim.check('injected F sensor fault inside the reconnect grace still warns', bool(fault), fault_events=fault[:3], gse_sensor=((gse or {}).get('overpressure') or {}).get('sensor') if code == 200 else gse)
    all_s = sensor_events(0, sim.vclock(), 'overpressure sensor')
    sim.result['vb_sensor_events'] = all_s[-20:]
    (sim.out / 'ws-counts.json').write_text(json.dumps(sim.ws.counts, indent=1))


SCENARIOS['vb_stale'] = sc_vb_stale


def board_clock_rows(sim):
    """(receive_wall_ms, source_ms, sender) from the station recording, copied while live."""
    import sqlite3, glob, shutil, tempfile
    tmp = Path(tempfile.mkdtemp()); rows = []
    for db in sorted(glob.glob(str(sim.out / 'gs-data' / 'groundstation_recording_*.db'))):
        for suf in ('', '-wal', '-shm'):
            if Path(db + suf).exists(): shutil.copyfile(db + suf, tmp / (Path(db).name + suf))
        con = sqlite3.connect(str(tmp / Path(db).name))
        rows += con.execute('select timestamp_ms, source_timestamp_ms, sender_id from telemetry where source_timestamp_ms is not null').fetchall(); con.close()
    shutil.rmtree(tmp, ignore_errors=True); return rows


def clock_report(rows, w0, w1, boards):
    import bisect
    gs = sorted((w, src) for w, src, snd in rows if snd == 'GS' and src and src > 1.5e12); gw = [g[0] for g in gs]
    def net(w):
        i = bisect.bisect_left(gw, w)
        if not gs or i <= 0 or i >= len(gs): return None
        (a0, s0), (a1, s1) = gs[i - 1], gs[i]; return s0 + (s1 - s0) * ((w - a0) / (a1 - a0) if a1 > a0 else 0)
    out = {}
    for b in boards:
        xs = sorted((w, src) for w, src, snd in rows if snd == b and w0 <= w <= w1 and src)
        if not xs: out[b] = None; continue
        non_utc = sum(1 for _, src in xs if src < 1.5e12)
        ahead = [src - net(w) for w, src in xs if src >= 1.5e12 and net(w) is not None]
        back = 0; worst = 0; prev = None
        for _, src in xs:
            if prev is not None and src < prev - 1000: back += 1; worst = max(worst, prev - src)
            prev = src if prev is None else max(prev, src)
        out[b] = {'packets': len(xs), 'non_utc_packets': non_utc, 'max_ahead_of_station_ms': max(ahead) if ahead else None,
                  'backward_steps_over_1s': back, 'worst_backward_ms': worst}
    return out


def sc_gps_time(sim):
    """RF GPS cold start (placeholder date, no fix) then a valid fix; every board must follow station/GPS UTC
    without a backward step (fixture: --gps-initial 2)."""
    a = sim.a; boards = present_boards(sim)
    sim.faithful = (sim.out / 'faithful.jsonl').open('a')
    v0 = sim.vclock(); w0 = None
    while sim.vclock() - v0 < a.phase_a_ms and not sim.stop.is_set():
        r = gs_view(sim); w0 = w0 or r['wall'] * 1000; sim.wait_v(5000)
    sim.log_action('gps_fix_on'); F.atomic(sim.diag / 'gps.fix', '1'); w_fix = time.time() * 1000
    v1 = sim.vclock()
    while sim.vclock() - v1 < a.gps_fix_ms and not sim.stop.is_set():
        gs_view(sim); sim.wait_v(5000)
    w_end = time.time() * 1000; sim.log_action('gps_phase_end')
    rows = board_clock_rows(sim)
    cold = clock_report(rows, 0, w_fix, boards); fix = clock_report(rows, w_fix, w_end, boards)
    sim.result['clock_cold'] = cold; sim.result['clock_fix'] = fix
    sim.check('cold GPS (placeholder date, no fix): every board stamps network UTC, never the receiver/uptime time',
              all(v and v['non_utc_packets'] == 0 for v in cold.values()), boards={b: v and v['non_utc_packets'] for b, v in cold.items()})
    sim.check('cold GPS: no board clock runs ahead of the station by more than 2 s',
              all(v and (v['max_ahead_of_station_ms'] or 0) <= 2000 for v in cold.values()), ahead={b: v and v['max_ahead_of_station_ms'] for b, v in cold.items()})
    sim.check('cold GPS: no board clock steps backwards by more than 1 s',
              all(v and v['backward_steps_over_1s'] == 0 for v in cold.values()), backward={b: v and (v['backward_steps_over_1s'], v['worst_backward_ms']) for b, v in cold.items()})
    sim.check('valid fix: boards keep UTC and follow without a backward step over 1 s',
              all(v and v['non_utc_packets'] == 0 and v['backward_steps_over_1s'] == 0 for v in fix.values()),
              boards={b: v and (v['non_utc_packets'], v['backward_steps_over_1s'], v['worst_backward_ms'], v['max_ahead_of_station_ms']) for b, v in fix.items()})


SCENARIOS['gps_time'] = sc_gps_time


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--mode', choices=['hitl', 'test_fire', 'normal', 'hitl_mode', 'test_fire_mode'], required=True,
                   help='GS_STATION_MODE for the one-binary backend (legacy *_mode names accepted)')
    p.add_argument('--rrls', action='store_true', help='add an RRLS hosted station on the rocket radio medium')
    p.add_argument('--gps-fix-ms', type=int, default=0, help='timesync_comms: RF GPS fix phase length (virtual ms)')
    p.add_argument('--gps-lost-ms', type=int, default=60000, help='timesync_comms: GPS lost phase length (virtual ms)')
    p.add_argument('--mode-switch', default='', help='timesync_comms: comma list of in-process station mode targets, e.g. test_fire,hitl')
    p.add_argument('--scenario', choices=sorted(SCENARIOS), required=True)
    p.add_argument('--evidence', type=Path, required=True)
    p.add_argument('--image', required=True)
    p.add_argument('--cohort', type=Path, required=True, help='directory holding the seven built firmware repositories')
    p.add_argument('--duration-ms', type=int, default=900000)
    p.add_argument('--measure-ms', type=int, default=300000)
    p.add_argument('--ready-timeout-ms', type=int, default=240000)
    p.add_argument('--cycles', type=int, default=5)
    p.add_argument('--radio', choices=['hub', 'rfd', 'rfd-faithful', 'backlog'], default='rfd')
    p.add_argument('--rfd-global-retries', type=int, default=10)
    p.add_argument('--faithful-time', type=float, default=0, help='wall seconds per virtual second (Renode pace + time-dilated station)')
    p.add_argument('--phase-a-ms', type=int, default=180000)
    p.add_argument('--gps-initial', type=int, choices=[0, 1, 2], default=0, help='RF GPS fixture at power-up: 0 no fix, 1 fix, 2 u-blox placeholder date, no fix')
    p.add_argument('--rfd-modem', default='')
    p.add_argument('--radio-trace', action='store_true')
    p.add_argument('--gs-reboot', action='append', default=[])
    p.add_argument('--board-reboot', action='append', default=[])
    p.add_argument('--can-cut', action='append', default=[])
    p.add_argument('--wall-freshness-scale', type=int, default=30)
    p.add_argument('--rust-log', default='info')
    p.add_argument('--cpus', type=int, default=6)
    p.add_argument('--poll', type=float, default=.05)
    p.add_argument('--timeout', type=int, default=4 * 3600)
    p.add_argument('--gs-env', action='append', default=[])
    p.add_argument('--nodes', default='', help='subset of rf,power,flight,gateway,actuator,valve,daq')
    p.add_argument('--no-query', action='store_true', help='passive comms measurement (no operator Query load)')
    p.add_argument('--sched', type=json.loads, default={}, help='{"can0":s,"can1":s,"gsrb":s,"abrb":s} virtual seconds')
    a = p.parse_args()
    a.station_mode = {'hitl_mode': 'hitl', 'test_fire_mode': 'test_fire'}.get(a.mode, a.mode)
    a.mode = {'hitl': 'hitl_mode', 'test_fire': 'test_fire_mode'}.get(a.station_mode, 'normal')
    if a.sched.get('can0'): a.can_cut.append(f"DAQ:{a.sched['can0']}:{a.sched['can1']}")
    if a.sched.get('gsrb'): a.gs_reboot.append(str(a.sched['gsrb']))
    if a.sched.get('abrb'): a.board_reboot.append(f"AB:{a.sched['abrb']}")
    if a.sched.get('vbc0'): a.can_cut.append(f"VB:{a.sched['vbc0']}:{a.sched['vbc1']}")
    import signal
    def _term(*_): raise KeyboardInterrupt('SIGTERM')
    signal.signal(signal.SIGTERM, _term)
    s = Sim(a); s.run(SCENARIOS[a.scenario]); print(json.dumps({k: s.result.get(k) for k in ('phase', 'passed', 'error', 'virtual_ms', 'wall_s')}))
