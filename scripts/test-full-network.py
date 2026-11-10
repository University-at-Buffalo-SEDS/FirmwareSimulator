#!/usr/bin/env python3
"""Full seven-board linked simulation (see docs/hardware-faithful-radio.md for --radio rfd-faithful
--faithful-time and the faithful, mode_switch, rrls_faithful, gps_time, vb_stale, stale_auto_abort and gse_flows scenarios) (RF, PB, FC on the AV bay radio; GB, AB, VB, DAQ on the
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
    F.atomic(diag / 'flight/radio-gate.txt', '0'); F.atomic(diag / 'flight/gb-pico-gate.txt', '0')
    key, users = F.fixture_user(); (config / 'users.json').write_text(json.dumps(users))
    gse = {'nitrogen_target_psi': 500, 'pressure_step_psi': 50, 'pressure_ceiling_psi': 900, 'maximum_zero_offset_psi': 20,
           'dry_self_test_confirmed': False, 'grouped_panel': True, 'turbocharge_max_added_mass_kg': 0.5, 'turbocharge_pressure_deadband_psi': 12}
    targets = {'version': 1, 'fill_source': 'kg50', 'nitrogen': {'target_mass_kg': 0.5, 'target_pressure_psi': 500},
               'nitrous': {'target_mass_kg': 8, 'target_pressure_psi': 750}}
    if a.scenario == 'gse_flows':
        # The operational flight sim's bench plant (F.plant_pressure, 130 psi regulator): a nitrogen test,
        # cold flow and Turbocharge that the valve-driven plant can actually reach.
        gse.update(nitrogen_target_psi=50, pressure_ceiling_psi=150, cold_flow_target_psi=40, turbocharge_max_added_mass_kg=0.3)
        targets.update(nitrogen={'target_mass_kg': 10, 'target_pressure_psi': 50}, nitrous={'target_mass_kg': 5, 'target_pressure_psi': 100})
    gse.update(a.gse_config)
    (config / 'gse.json').write_text(json.dumps(gse))
    (config / 'fill-targets.json').write_text(json.dumps(targets))
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
        # Start the dilated station clock at the simulation network epoch (GS_SIM_NETWORK_EPOCH_MS,
        # 2026-01-01T00:00:00Z) so host wall time and the boards' network time share one base.
        env['FAKETIME'] = '@2026-01-01 00:00:00 x%.6f' % (1.0 / a.faithful_time)
        env['FAKETIME_DONT_FAKE_MONOTONIC'] = '0'
    for kv in a.gs_env:
        k, v = kv.split('=', 1); env[k] = v
    # A station whose modem the medium models runs its startup modem check against that model
    # (GS 5eda140), so its pacing follows the modem's read-back S5 as on the bench.
    modelled = {int(e.split('=', 1)[0]) for e in a.rfd_modem.split(';') if e.strip()} if a.rfd_modem else set()
    if 1 in modelled:
        env.setdefault('GS_SIMULATED_RFD_MODEM', '1')
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
        for k_ in ('GS_LAYOUT_PATH', 'GS_SIM_ROUTER_TIME_DIVISOR', 'GS_SIM_NETWORK_EPOCH_MS', 'GS_HEARTBEAT_PERIOD_MS', 'GS_SIM_WALL_FRESHNESS_SCALE',
                   'LD_PRELOAD', 'FAKETIME', 'FAKETIME_DONT_FAKE_MONOTONIC'):
            if k_ in env: rr['env'][k_] = env[k_]
        if 2 in modelled:
            rr['env']['GS_SIMULATED_RFD_MODEM'] = '1'
        topo['host_nodes'].append(rr)
        radio_link['endpoints'].append({'node': 'rrls', 'peripheral': 'radio'})
    # scheduled events (samples are virtual seconds)
    for spec_ in a.gs_reboot:
        topo['reboots'].append({'node': 'groundstation', 'after_sample': int(spec_)})
    for spec_ in a.board_reboot:
        node, s = spec_.split(':'); topo['reboots'].append({'node': NODE_OF.get(node, node), 'after_sample': int(s)})
    for spec_ in a.radio_cut:
        s0_, s1_ = (int(x) for x in spec_.split(':'))
        topo.setdefault('radio_link_events', [])
        for snd, rcv in (('groundstation', 'rf'), ('rf', 'groundstation')):
            topo['radio_link_events'] += [dict(link='rocket_radio', sender=snd, receiver=rcv, after_sample=s0_, connected=False),
                                          dict(link='rocket_radio', sender=snd, receiver=rcv, after_sample=s1_, connected=True)]
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
           '-e', 'SEDS_UART_GATE=rf:usart1:/diagnostics/flight/radio-gate.txt;gateway:usart2:/diagnostics/flight/gb-pico-gate.txt',
           '-v', f'{config}:/simulation:ro', '-v', f'{diag}:/diagnostics',
           '-v', f'{out / "gs-data"}:/opt/groundstation/data', '-v', f'{out / "gs-backend-data"}:/opt/groundstation/backend/data']
    if a.radio_trace:
        cmd += ['-e', 'FIRMWARE_SIM_RADIO_TRACE_DIR=/diagnostics/radio-trace', '-e', f'FIRMWARE_SIM_RADIO_TRACE_UNTIL_MS={a.duration_ms - 1000}']
        cmd += ['-e', f'FIRMWARE_SIM_RADIO_TRACE_MAX_MB={a.radio_trace_max_mb}']
    if a.faithful_time:
        cmd += ['-e', f'FIRMWARE_SIM_WALL_PACE={a.faithful_time}']
    if a.radio == 'rfd-faithful':
        cmd += ['-e', f'FIRMWARE_SIM_RFD_GLOBAL_RETRIES={a.rfd_global_retries}']
    if a.rfd_modem:
        cmd += ['-e', 'FIRMWARE_SIM_RFD_MODEM=' + a.rfd_modem, '-e', 'FIRMWARE_SIM_RFD_MODEM_LOG=/diagnostics/rfd-modem.log']
    if a.rfd_broadcast_retry:
        cmd += ['-e', 'FIRMWARE_SIM_RFD_BROADCAST_RETRY=' + a.rfd_broadcast_retry]
    if a.rfd_carrier_sense:
        cmd += ['-e', 'FIRMWARE_SIM_RFD_CARRIER_SENSE=' + a.rfd_carrier_sense]
    if a.rfd_air_loss_ppm:
        cmd += ['-e', f'FIRMWARE_SIM_RFD_AIR_LOSS_PPM={a.rfd_air_loss_ppm}']
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
        if getattr(self, 'gse_plant', None) is not None:
            # Valve-driven tank (operational flight sim model) from the station's actual valve readbacks.
            # Supplies are connected only once the scenario says so (isolated during the dry self-test).
            p = self.gse_plant; valves = (self.state.get('gse') or {}).get('valves')
            if isinstance(valves, list) and len(valves) == 5 and all(type(v) is bool for v in valves):
                fed = list(valves)
                if not p['nitrogen']: fed[3] = False
                while dt > 0:
                    step = min(dt, 200); dt -= step
                    self.pressure = F.plant_pressure(self.pressure, step, fed)
                    self.mass = F.plant_mass(self.mass, step, valves, p['nitrous'], p['nitrogen'])
            F.atomic(self.diag / 'pressure.raw', str(F.raw_pressure(self.pressure)))
            F.atomic(self.diag / 'loadcell.raw', F.mass_samples(self.mass))
            return
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
                if not hasattr(self, 'last_progress'): self.last_progress = time.monotonic()
                try:
                    self.snapshot()
                except Exception as e:  # GS restarting
                    time.sleep(.3); continue
                if self.now != prev_v: self.last_progress = time.monotonic()
                elif time.monotonic() - getattr(self, 'last_progress', time.monotonic()) > 900:
                    raise TimeoutError('virtual clock stalled for 900 s of wall time')
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


def station_aborted(s):
    """The station holds an Abort: an operator Abort puts it in Aborted (flight_state 15); an automatic
    (safety task) Abort leaves the flight state alone and shows as gse.automatic_abort_claimed."""
    return (s.get('rec') or {}).get('flight_state') == 15 or (s.get('gse') or {}).get('automatic_abort_claimed') is True


def clear_gse(sim, label):
    """Operator 'Clear GSE abort' when the station holds an operator or automatic Abort."""
    if station_aborted(sim.st()):
        code, body = sim.api('/api/gse/abort/clear', {})
        sim.log_action('clear_gse_abort', label=label, http=code, body=body)
        sim.check(f'{label}: Clear GSE abort accepted', code == 200, http=code, body=body)
        sim.wait_until(lambda s: not station_aborted(s), 10000, 'station left Aborted')


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
    KEEP = ('StationModeChanged', 'ActionPolicy', 'Notification', 'NetworkTime', 'Warning', 'Error', 'AlertAckState')

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
                    if ty in ('StationModeChanged', 'Notification', 'Warning', 'Error', 'AlertAckState'): row['data'] = json.loads(text).get('data')
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
    # The settings page reviews the change only if it can decode this challenge (GS f061992).
    live = sim.out / 'contract-live' / label; live.mkdir(parents=True, exist_ok=True)
    (live / 'snapshot.json').write_text(json.dumps(st, indent=1)); (live / 'prepare.json').write_text(json.dumps(ch, indent=1))
    problems = ui_challenge_problems(ch, binding)
    sim.check(f'{label}: the UI decodes the mode challenge', not problems, problems=problems, version=ch.get('version'))
    v0 = sim.vclock()
    code, rc = sim.api('/api/station-modes/confirm', {**binding, 'ticket': ch['ticket'], 'transition_id': ch['transition_id'], 'confirmation': ch['required_confirmation']})
    sim.log_action('station_mode_confirm', label=label, http=code, body=rc)
    if code != 200: sim.check(f'{label}: confirm accepted', False, http=code, body=rc); return False
    (live / 'confirm.json').write_text(json.dumps(rc, indent=1))
    problems = ui_receipt_problems(rc, binding, ch)
    sim.check(f'{label}: the UI accepts the confirmation receipt', not problems, problems=problems)
    want = target.split('-')[0]; t_end = time.time() + 60; st2 = None
    while time.time() < t_end:
        c2, st2 = sim.api('/api/station-modes')
        if c2 == 200 and st2['current']['mode_id'] == want and not st2.get('transition', {}) or (c2 == 200 and st2['current']['mode_id'] == want and (st2.get('transition') or {}).get('state') != 'pending'): break
        time.sleep(.2)
    sim.check(f'{label}: station reports {want} after switch', st2 and st2['current']['mode_id'] == want, current=(st2 or {}).get('current'), transition=(st2 or {}).get('transition'),
              layout_revision=(st2 or {}).get('layout_revision'), switched_v_ms=sim.vclock() - v0)
    if st2: (live / 'after.json').write_text(json.dumps(st2, indent=1))
    code_l1, layout1 = sim.api('/api/layout')
    sim.check(f'{label}: /api/layout changes with the mode', code_l1 == 200 and json.dumps(layout1, sort_keys=True) != json.dumps(layout0, sort_keys=True),
              before_keys=sorted(layout0)[:12] if isinstance(layout0, dict) else str(layout0)[:200], after_keys=sorted(layout1)[:12] if isinstance(layout1, dict) else str(layout1)[:200])
    (sim.out / f'layout-{label}-before.json').write_text(json.dumps(layout0, indent=1)); (sim.out / f'layout-{label}-after.json').write_text(json.dumps(layout1, indent=1))
    if ws:
        t_end = time.time() + 20
        while time.time() < t_end and not any(e['ty'] == 'StationModeChanged' for e in ws.events[n0:]): time.sleep(.2)
        smc = next((e for e in ws.events[n0:] if e['ty'] == 'StationModeChanged'), None)
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
    # mode switch checks live in the mode_switch scenario (GS 0be3424 rule)
    # (2)/(i) abort at Idle with a backlogged radio, then Clear GSE abort within a bounded time
    code, body = sim.command('Abort'); va = sim.vclock()
    sim.wait_until(lambda s: (s.get('rec') or {}).get('flight_state') == 15, 30000, 'station Aborted')
    sim.wait_v(15000)
    gs_view(sim)
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
    a = sim.a
    if a.sched.get('vbrb'):  # VB power cycle (scheduled reboot); the outage is the reboot plus about 5 s of boot
        s0 = a.sched['vbrb']; s1 = s0 + 5
    else:
        s0, s1 = a.sched['vbc0'], a.sched['vbc1']
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
    code, body = sim.api('/api/command', 'StartWritingNow'); sim.log_action('start_recording', http=code, body=body)
    sim.faithful = (sim.out / 'faithful.jsonl').open('a')
    v0 = sim.vclock(); w0 = None
    while sim.vclock() - v0 < a.phase_a_ms and not sim.stop.is_set():
        r = gs_view(sim); w0 = w0 or r['wall'] * 1000; sim.wait_v(5000)
    # Window bounds on the station's own wall clock (the recording's timestamp_ms): under the time-dilated
    # station that is the network epoch plus virtual time, otherwise host wall time.
    gs_wall = (lambda: 1767225600000 + sim.vclock()) if a.faithful_time else (lambda: time.time() * 1000)
    sim.log_action('gps_fix_on'); F.atomic(sim.diag / 'gps.fix', '1'); w_fix = gs_wall()
    v1 = sim.vclock()
    while sim.vclock() - v1 < a.gps_fix_ms and not sim.stop.is_set():
        gs_view(sim); sim.wait_v(5000)
    w_end = gs_wall() + 60000; sim.log_action('gps_phase_end')
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


class Rrls:
    def __init__(self, sim):
        self.sim = sim; self.fwd = F.Forwarder(sim.addr); self.fwd.destination = (sim.addr, 3001); sim.rrls_fwd = self.fwd; self.tok = None
    def login(self, tries=600):
        for _ in range(tries):
            try: self.tok = F.login(self.fwd.origin, self.sim.key); return True
            except (AssertionError, OSError, KeyError, TypeError): time.sleep(.5)
        return False
    def api(self, path, body=None):
        for attempt in range(2):
            try: code, data = F.request(self.fwd.origin, path, body, self.tok, 30)
            except OSError as e: code, data = 0, {'os_error': str(e)}
            if code in (0, 401) and attempt == 0: self.login(tries=120); continue
            return code, data


def sc_rrls_faithful(sim):
    """RRLS on the faithful radio: (1) idle RRLS seen alive at the primary, (2) RRLS-issued exchanges/commands
    within ~1 s, (3) no relay traffic on a healthy direct path; relay engaged and within its share when broken."""
    a = sim.a; S = a.sched; boards = present_boards(sim); out = (sim.out / 'rrls.jsonl').open('a')
    sim.wait_ready(boards, timeout_ms=60000)
    r = Rrls(sim); sim.check('RRLS login', r.login())
    def both(tag):
        row = {'v': sim.vclock(), 'tag': tag}
        for k, (fn, path) in {'p_rrls': (sim.api, '/api/rrls/status'), 'r_rrls': (r.api, '/api/rrls/status'), 'p_topo': (sim.api, '/api/network_topology'),
                              'p_pair': (sim.api, '/api/station/pairing'), 'r_pair': (r.api, '/api/station/pairing'), 'r_ctl': (r.api, '/api/rrls/control'),
                              'r_boards': (r.api, '/api/boards')}.items():
            c, d = fn(path); row[k] = d if c == 200 else {'http': c, 'body': str(d)[:200]}
        # (5) network time on both stations, read back to back
        c1, t1 = sim.api('/api/network_time'); c2, t2 = r.api('/api/network_time'); c3, t3 = sim.api('/api/network_time')
        try: row['clock_offset_ms'] = t2['timestamp_ms'] - (t1['timestamp_ms'] + t3['timestamp_ms']) / 2; row['clock_bracket_ms'] = t3['timestamp_ms'] - t1['timestamp_ms']
        except (TypeError, KeyError): row['clock_offset_ms'] = None; row['clock_raw'] = [str(t1)[:100], str(t2)[:100]]
        if isinstance(row['p_topo'], dict):
            row['p_topo'] = {'nodes': [n for n in row['p_topo'].get('nodes', []) if 'rrls' in json.dumps(n).lower()],
                             'links': [l for l in row['p_topo'].get('links', []) if 'rrls' in json.dumps(l).lower()]}
        out.write(json.dumps(row, default=str) + '\n'); out.flush(); return row
    # (2a) pairing exchange over the radio
    both('initial'); v0 = sim.vclock()
    sim.api('/api/station/pairing/start', {}); r.api('/api/station/pairing/start', {})
    code = None
    while sim.vclock() - v0 < 60000:
        x = both('pair'); cp = ((x['p_pair'] or {}).get('exchange') or {}).get('code'); cr = ((x['r_pair'] or {}).get('exchange') or {}).get('code')
        if cp and cp == cr: code = cp; break
        sim.wait_v(200)
    v_code = sim.vclock()
    if code:
        sim.api('/api/station/pairing/confirm', {'code': code}); r.api('/api/station/pairing/confirm', {'code': code})
    v1 = sim.vclock(); saved = None
    while code and sim.vclock() - v1 < 60000:
        x = both('pair-saved')
        if (x['p_pair'] or {}).get('paired') and (x['r_pair'] or {}).get('paired'): saved = sim.vclock(); break
        sim.wait_v(200)
    sim.check('(2) pairing exchange over the radio: code within 1 s and saved within 1 s of confirm', code is not None and v_code - v0 <= 1500 and saved is not None and saved - v1 <= 1500,
              code_after_v_ms=v_code - v0 if code else None, saved_after_v_ms=(saved - v1) if saved else None)
    # restart both stations to activate the pairing (scheduled host reboots)
    wait_until_v(sim, (S['hostrb'] + 3) * 1000, 'stations restarted')
    sim.wait_until(lambda s: isinstance(s.get('rec'), dict) and s['rec'].get('boards'), 120000, 'primary back')
    r.login(tries=240); sim.wait_v(10000)
    # (1)+(3a) healthy direct path
    rows = []; v_idle = sim.vclock()
    while sim.vclock() < S['cut0'] * 1000 - 2000:
        rows.append(both('idle')); sim.wait_v(2000)
    def rel(x): return ((x.get('r_rrls') or {}).get('relayed_frames') or 0, (x.get('r_rrls') or {}).get('relay_envelope_bytes') or 0, bool((x.get('r_rrls') or {}).get('forwarding')))
    seen = []
    for x in rows:
        t = json.dumps(x.get('p_topo')); m = re.findall(r'"(?:age_ms|peer_age_ms|last_seen_age_ms)": ?(\d+)', t)
        st = re.findall(r'"status": ?"(\w+)"', t)
        seen.append(bool(x['p_topo'].get('nodes')) and (not m or min(int(v) for v in m) <= 5000) and not any(s_ in ('offline', 'unknown', 'stale') for s_ in st))
    sim.check('(1) idle RRLS seen alive at the primary continuously (RRLS topology node present and <= 5 s old in >= 95% of samples)',
              rows and sum(seen) >= 0.95 * len(rows), fraction=round(sum(seen) / max(1, len(rows)), 3), last_nodes=(rows[-1]['p_topo'] if rows else None))
    if rows:
        f0, f1 = rel(rows[0]), rel(rows[-1])
        sim.check('(3) no relay forwarding while the direct path is healthy', f1[0] - f0[0] == 0 and not any(rel(x)[2] for x in rows),
                  relayed_frames_delta=f1[0] - f0[0], forwarding_samples=sum(rel(x)[2] for x in rows), p_relay_requested=[(x.get('p_rrls') or {}).get('relay_requested') for x in rows][-3:])
    # (4) RRLS shows live data from every board it hears
    def live(x):
        bl = (x.get('r_boards') or {}).get('boards') if isinstance(x.get('r_boards'), dict) else None
        rowsb = {y.get('sender_id'): y for y in (bl or [])}
        return {b: (rowsb.get(b) or {}).get('age_ms') for b in boards}
    lv = [live(x) for x in rows]
    frac = {b: round(sum(1 for d in lv if d.get(b) is not None and d[b] <= 5000) / max(1, len(lv)), 3) for b in boards}
    sim.check('(4) idle RRLS shows live data from every board (age <= 5 s in >= 95% of samples, RRLS /api/boards)', all(f >= 0.95 for f in frac.values()),
              fraction=frac, last=lv[-1] if lv else None)
    offs = [x.get('clock_offset_ms') for x in rows]
    sim.check('(5) idle RRLS network clock within 1 s of the primary', bool(offs) and all(o is not None and abs(o) <= 1000 for o in offs),
              max_abs=max((abs(o) for o in offs if o is not None), default=None), missing=sum(o is None for o in offs), samples=len(offs))
    # (2b) RRLS-issued recovery query reaches the primary
    v2 = sim.vclock(); c, b = r.api('/api/board-recovery/query', {'board': 'RF'}); got = None
    while sim.vclock() - v2 < 15000:
        rf = sim.rows().get('RF') or {}; op = rf.get('operation') or {}
        if op.get('action') == 'query' and (op.get('age_ms') or 10**9) <= (sim.vclock() - v2) * a.faithful_time + 2000: got = sim.vclock(); break
        time.sleep(.2)
    sim.check('(2) RRLS-issued recovery query is seen at the primary within 1 s', c == 200 and got is not None and got - v2 <= 1000, http=c, body=str(b)[:300], after_v_ms=(got - v2) if got else None)
    # (3b) broken direct path
    wait_until_v(sim, S['cut0'] * 1000, 'direct path broken'); cut = []
    while sim.vclock() < S['cut1'] * 1000:
        cut.append(both('cut')); sim.wait_v(2000)
    if len(cut) >= 3:
        f0, f1 = rel(cut[2]), rel(cut[-1]); dt = (cut[-1]['v'] - cut[2]['v']) / 1000
        share = (f1[1] - f0[1]) * 8 * (1 + a.rfd_global_retries) / dt / 64000 if dt > 0 else None
        sim.check('(3) relay engages when the direct path is broken', any(rel(x)[2] for x in cut) and f1[0] > f0[0], relayed_frames_delta=f1[0] - f0[0], forwarding_samples=sum(rel(x)[2] for x in cut))
        co = [x.get('clock_offset_ms') for x in cut[2:]]
        sim.check('(5) relaying RRLS network clock within 1 s of the primary', bool(co) and all(o is not None and abs(o) <= 1000 for o in co),
                  max_abs=max((abs(o) for o in co if o is not None), default=None), missing=sum(o is None for o in co))
        sim.check('(3) relay air share stays <= 15 %', share is not None and share <= 0.15, share=round(share, 3) if share is not None else None, relay_bytes=f1[1] - f0[1], window_s=dt)
    wait_until_v(sim, S['cut1'] * 1000 + 20000, 'direct restored'); after = [both('restored')]
    sim.wait_v(20000); after.append(both('restored2'))
    sim.check('(3) relay stops after the direct path returns', not rel(after[-1])[2], last=after[-1].get('r_rrls'))


SCENARIOS['rrls_faithful'] = sc_rrls_faithful


def sc_pyro_paths(sim):
    """HITL operator parachute commands and Abort over the direct GS-RF path, over the RRLS relay with the direct
    path cut, and after the direct path returns, under the normal background load (telemetry, heartbeats, time sync)
    plus a managed-variable query every 3 s. Records the POST time of every command (pyro.jsonl); with --radio-trace
    scripts/analyze-pyro-latency.py derives per-hop latency to the FC's acknowledgement. Checks relay engagement,
    handover, RRLS presence and RRLS-issued commands. Schedule (virtual s): hostrb (pairing restart), cut0/cut1."""
    a = sim.a; S = a.sched; boards = present_boards(sim); out = (sim.out / 'pyro.jsonl').open('a')
    def rec(**kw):
        out.write(json.dumps({'v': sim.vclock(), **kw}, default=str) + '\n'); out.flush()
    sim.check('startup: boards ready', sim.wait_ready(boards, timeout_ms=a.ready_timeout_ms) is not None)
    r = Rrls(sim) if a.rrls else None
    if r:
        sim.check('RRLS login', r.login())
    if r and S.get('hostrb'):
        sim.api('/api/station/pairing/start', {}); r.api('/api/station/pairing/start', {}); v0 = sim.vclock(); code = None
        while sim.vclock() - v0 < 60000 and not code:
            cp = ((sim.api('/api/station/pairing')[1] or {}).get('exchange') or {}).get('code')
            cr = ((r.api('/api/station/pairing')[1] or {}).get('exchange') or {}).get('code')
            if cp and cp == cr: code = cp
            else: sim.wait_v(200)
        if code:
            sim.api('/api/station/pairing/confirm', {'code': code}); r.api('/api/station/pairing/confirm', {'code': code})
        sim.check('RRLS pairing code exchanged over the radio', code is not None, after_v_ms=sim.vclock() - v0)
        wait_until_v(sim, (S['hostrb'] + 3) * 1000, 'stations restarted')
        sim.wait_until(lambda s: isinstance(s.get('rec'), dict) and s['rec'].get('boards'), 120000, 'primary back')
        r.login(tries=240)
        sim.check('boards ready after the pairing restart', sim.wait_ready(boards, timeout_ms=120000) is not None)
    stop_bg = threading.Event()
    def background():
        while not stop_bg.is_set() and not sim.stop.is_set():
            query_rtt_loop(sim, 30000, boards=boards, period_ms=3000 * len(boards), tag='bg-queries')
    bg = threading.Thread(target=background, daemon=True); bg.start()
    def rrls_status():
        if not r: return {}
        c, d = r.api('/api/rrls/status'); return d if c == 200 and isinstance(d, dict) else {}
    seq = [0]; refused = []
    def pyro(path, until_v):
        while sim.vclock() < until_v and not sim.stop.is_set():
            cmd = ('DeployParachute', 'ExpandParachute')[seq[0] % 2]; seq[0] += 1
            v0 = sim.vclock(); code, body = sim.command(cmd); v1 = sim.vclock()
            rs = rrls_status()
            rec(kind='pyro', path=path, cmd=cmd, v_post=v0, v_reply=v1, http=code, body=str(body)[:200],
                rrls_forwarding=rs.get('forwarding'), relayed_frames=rs.get('relayed_frames'))
            if code != 200: refused.append((path, cmd, code, str(body)[:200]))
            # FLIGHT_COMMAND is Ordered: space commands past one full reliable window (4.5 s) so each command
            # measures its own path, not the predecessor's; the jitter spreads them over the telemetry phase.
            sim.wait_v(5000 + (seq[0] * 977) % 1500)
    def abort_on(path):
        v0 = sim.vclock(); code, body = sim.command('Abort'); got = {}
        while sim.vclock() - v0 < 30000 and len(got) < len(boards) and not sim.stop.is_set():
            snap = sim.st()
            if snap.get('v', 0) >= v0:
                for b in boards:
                    row = sim.rows(snap).get(b) or {}
                    if b not in got and (row.get('abort_latched') or row.get('state_label') == 'Aborted'): got[b] = snap['v'] - v0
            time.sleep(.02)
        rec(kind='abort', path=path, v_post=v0, http=code, latency_ms=got, missing=[b for b in boards if b not in got])
        sim.check(f'Abort over the {path} path reaches every board', code in (200, 202) and len(got) == len(boards), latency_ms=got, http=code)
        av = [got.get(b) for b in ('RF', 'FC', 'PB') if b in boards]
        sim.check(f'Abort over the {path} path reaches the AV bay within {a.abort_bound_ms} ms', all(x is not None and x <= a.abort_bound_ms for x in av), latency_ms=got)
        res = sim.recovery('reset_startup', boards=boards, label=f'reset all after {path} Abort', timeout_ms=120000)
        sim.check(f'Reset all after the {path} Abort succeeds', res.get('success') is True, duration_ms=res.get('duration_ms'), body=str(res.get('body'))[:300])
        if res.get('success'):
            verify_clear(sim, f'after {path} Abort', boards=boards, timeout_ms=60000)
        return bool(res.get('success'))
    def rrls_query(path):
        if not r: return
        v0 = sim.vclock(); c, b = r.api('/api/board-recovery/query', {'board': 'RF'}); got = None
        while sim.vclock() - v0 < 15000:
            op = (sim.rows().get('RF') or {}).get('operation') or {}
            if op.get('action') == 'query' and (op.get('age_ms') or 10**9) <= (sim.vclock() - v0) * a.faithful_time + 2000: got = sim.vclock(); break
            time.sleep(.1)
        rec(kind='rrls_query', path=path, http=c, after_v_ms=(got - v0) if got else None)
        sim.check(f'RRLS-issued query reaches the primary within 1 s ({path})', c == 200 and got is not None and got - v0 <= 1000, http=c, after_v_ms=(got - v0) if got else None)
    # 1. direct path
    rs0 = rrls_status()
    pyro('direct', S['cut0'] * 1000 - 8000)
    rrls_query('direct')
    rs1 = rrls_status()
    if r:
        sim.check('no relay forwarding while the direct path is healthy', not rs1.get('forwarding') and (rs1.get('relayed_frames') or 0) == (rs0.get('relayed_frames') or 0),
                  relayed=[rs0.get('relayed_frames'), rs1.get('relayed_frames')])
        topo = json.dumps(sim.api('/api/network_topology')[1]).lower()
        sim.check('RRLS present at the primary on the direct path', 'rrls' in topo)
    # 2. direct path cut: the relay must engage
    wait_until_v(sim, S['cut0'] * 1000, 'direct path cut'); v_cut = sim.vclock(); v_eng = None
    while r and sim.vclock() - v_cut < 20000:
        if rrls_status().get('forwarding'): v_eng = sim.vclock(); break
        sim.wait_v(250)
    if r:
        sim.check('relay engages within 6 s of the direct path loss (3 s loss detection + lease)', v_eng is not None and v_eng - v_cut <= 6000,
                  engage_v_ms=(v_eng - v_cut) if v_eng else None)
        rec(kind='relay_engage', after_v_ms=(v_eng - v_cut) if v_eng else None)
    pyro('relay', S['cut1'] * 1000 - 70000)
    rrls_query('relay')
    relay_reset = abort_on('relay')
    # 3. direct path returns: handover back to direct
    wait_until_v(sim, S['cut1'] * 1000, 'direct path restored'); v_res = sim.vclock(); v_off = None
    while r and sim.vclock() - v_res < 40000:
        if not rrls_status().get('forwarding'): v_off = sim.vclock(); break
        sim.wait_v(250)
    if r:
        sim.check('relay hands back to the direct path within 20 s (5 s continuous direct, 10 s minimum dwell)', v_off is not None and v_off - v_res <= 20000,
                  handover_v_ms=(v_off - v_res) if v_off else None)
        rec(kind='relay_handover', after_v_ms=(v_off - v_res) if v_off else None)
    if not relay_reset:
        # The reset after the relayed Abort needs fresh board queries; retry it on the direct path.
        res = sim.recovery('reset_startup', boards=boards, label='reset all on the restored direct path', timeout_ms=120000)
        sim.check('Reset all succeeds once the direct path is back', res.get('success') is True, duration_ms=res.get('duration_ms'), body=str(res.get('body'))[:300])
        verify_clear(sim, 'restored direct path', boards=boards, timeout_ms=60000)
    pyro('restored', a.duration_ms - 75000)
    abort_on('direct')
    stop_bg.set()
    sim.check('every parachute command admitted (HTTP 200)', seq[0] > 0 and not refused, commands=seq[0], refused=refused[:10])


SCENARIOS['pyro_paths'] = sc_pyro_paths


def switch_round_trip(sim, label):
    """normal -> hitl -> normal in process; returns True when both switches complete."""
    ok = station_mode_switch(sim, 'hitl', label + '-to-hitl')
    sim.wait_v(2000)
    back = station_mode_switch(sim, 'normal', label + '-to-normal') if ok else False
    sim.wait_v(2000)
    return ok and back


def switch_refused(sim, label):
    c, st = sim.api('/api/station-modes')
    opt = next((o for o in (st or {}).get('options', []) if o.get('id') == 'hitl'), None) if c == 200 else None
    binding = {k: st[k] for k in ('backend_instance', 'principal_context', 'authority_id', 'revision', 'policy_epoch')} if c == 200 else {}
    binding.update(current_id=(st or {}).get('current', {}).get('id'), target_id='hitl')
    c2, b2 = sim.api('/api/station-modes/prepare', binding)
    sim.check(f'{label}: mode switch refused', not (opt or {}).get('available', False) and c2 != 200, option_reason=(opt or {}).get('reason'), prepare_http=c2, prepare=str(b2)[:200])


def sc_mode_switch(sim):
    """GS 0be3424 rule on the faithful radio: the mode switch never locks on board/abort/delivery/recovery state;
    only a countdown, turbocharge, manual control or cold flow refuse it. Schedule (virtual s): DAQ CAN cut
    sched dq0..dq1, GS<->RF direct radio cut cut0..cut1."""
    a = sim.a; S = a.sched
    sim.ws = None
    try: sim.ws = WsWatch(sim, sim.origin, sim.token, tag='ws-modes')
    except Exception as e: sim.log_action('ws_failed', error=repr(e))
    sim.faithful = (sim.out / 'faithful.jsonl').open('a')
    # Refused while a sequence is actively moving valves: the startup valve sequence right after boot.
    seen_active = None
    while sim.vclock() < 25000 and not sim.stop.is_set():
        c, g = sim.api('/api/gse/status')
        ph = (g or {}).get('phase') if c == 200 else None
        if ph and ph not in ('idle', 'cancelled', 'passed', 'paused', 'fault') and seen_active is None:
            seen_active = ph; switch_refused(sim, f'sequence actively moving valves (phase {ph})')
        time.sleep(.2)
    if seen_active is None: sim.log_action('no_active_sequence_observed_at_startup')
    wait_until_v(sim, 30000, 'startup sequence finished'); gs_view(sim)
    sim.check('switch at Idle right after the startup sequence finished', switch_round_trip(sim, 'idle-after-startup-sequence'))
    wait_until_v(sim, (S['dq0'] + 8) * 1000, 'DAQ missing')
    sim.check('switch at Idle with a board missing (DAQ CAN cut)', switch_round_trip(sim, 'daq-missing'), daq=sim.rows().get('DAQ', {}).get('readback_status'))
    c, b = sim.api('/api/board-recovery/query', {'board': 'DAQ'}); sim.log_action('daq_query_pending', http=c, body=b)
    sim.check('switch while a board recovery query is pending', switch_round_trip(sim, 'recovery-pending'), query_http=c)
    wait_until_v(sim, (S['cut0'] + 3) * 1000, 'direct radio broken')
    sim.command('Abort'); sim.wait_v(8000); gs_view(sim)
    ab = sim.aborted_set(sim.st())
    sim.check('switch while Aborted, a board aborted and the AV-bay ABORT undeliverable', switch_round_trip(sim, 'aborted-undeliverable'), aborted_boards=ab,
              radio_budget=((sim.st().get('netdiag') or {}).get('radio_tx_budget') or {}).get('pending_control_bytes'))
    c, b = sim.api('/api/gse/abort/clear', {}); sim.log_action('clear_abort', http=c, body=b)
    sim.wait_v(3000)
    sim.check('switch at Idle after the abort is cleared', switch_round_trip(sim, 'after-abort-clear'), clear_http=c)
    wait_until_v(sim, (S['cut1'] + 5) * 1000, 'direct radio restored')
    for cmd in ('Dump',):
        sim.command(cmd); sim.wait_v(3000); sim.command(cmd); sim.wait_v(3000)
        gs = sim.api('/api/gse/status')[1]
        sim.log_action('manual_state', manual=(gs or {}).get('manual_control'), valves=(gs or {}).get('valves'))
        sim.check(f'switch at Idle after a manual valve command ({cmd}) completed', switch_round_trip(sim, f'after-manual-{cmd}'))
    gs_view(sim)
    if sim.ws: (sim.out / 'ws-counts.json').write_text(json.dumps(sim.ws.counts, indent=1))


SCENARIOS['mode_switch'] = sc_mode_switch


AV_BAY = ['RF', 'FC', 'PB']


def sc_stale_auto_abort(sim):
    """Normal mode at Idle on the faithful RFD radio. A GB CAN cut (sched gbc0..gbc1) raises the automatic ABORT
    while the radio is saturated. GS 0715aec retires an automatic Abort by age only once its cause has cleared.
    - restored (gbc1 - gbc0 < 30): GB is back before the 30 s mark; the abort is retired with its cause cleared and
      the AV bay never aborts afterwards;
    - offline (gbc1 - gbc0 >= 60): the abort keeps being delivered past 30 s (no retirement by age); an operator
      Clear still retires it, and the AV bay never aborts afterwards."""
    a = sim.a; s0, s1 = a.sched['gbc0'], a.sched['gbc1']; offline = (s1 - s0) >= 60
    def notes():
        code, n = sim.api('/api/notifications')
        lst = n if isinstance(n, list) else ((n or {}).get('notifications') or []) if code == 200 else []
        return [x.get('message', '') if isinstance(x, dict) else str(x) for x in lst]
    def retired(): return [t for t in notes() if 'Stale automatic Abort retired' in t]
    sim.wait_ready(timeout_ms=a.ready_timeout_ms)  # may time out on the faithful radio; the scenario proceeds
    wait_until_v(sim, s0 * 1000, 'GB offline')
    # GB fully offline: its Pico-Fi UART to the station is gated (SEDS_UART_GATE) and its CAN is cut.
    F.atomic(sim.diag / 'flight/gb-pico-gate.txt', '1'); sim.log_action('gb_pico_gate', cut=True)
    def gb_back():
        wait_until_v(sim, s1 * 1000, 'GB back'); F.atomic(sim.diag / 'flight/gb-pico-gate.txt', '0'); sim.log_action('gb_pico_gate', cut=False)
    threading.Thread(target=gb_back, daemon=True).start()
    snap = sim.wait_until(station_aborted, 60000, 'automatic abort raised')
    v_abort = sim.vclock()
    sim.check('GB offline raises the automatic abort', snap is not None)
    if snap is None: return
    clean = [b for b in AV_BAY if b not in sim.aborted_set(sim.st())]
    sim.log_action('av_bay_clean_at_abort', boards=clean, offline_case=offline)
    if not offline:
        wait_until_v(sim, s1 * 1000 + 2000, 'GB back')
        wait_until_v(sim, v_abort + 40000, 'abort older than 30 s')
        r = retired()
        sim.check('restored GB: the automatic Abort is retired by age with its cause cleared', bool(r) and any('clear' in t.lower() for t in r), retired=r[-3:])
    else:
        wait_until_v(sim, v_abort + 40000, 'abort older than 30 s, GB still offline')
        r = retired()
        code, diag = sim.api('/api/network_diagnostics')
        sim.check('GB still offline: the automatic Abort is not retired by age past 30 s', not r, retired=r[-3:],
                  pending_control_bytes=((diag or {}).get('radio_tx_budget') or {}).get('pending_control_bytes') if code == 200 else None)
    v_clear = None
    for _ in range(30):
        code, body = sim.api('/api/gse/abort/clear', {})
        sim.log_action('clear_gse_abort', http=code, body=body)
        if code == 200:
            v_clear = sim.vclock(); break
        sim.wait_v(2000)
    sim.check('Clear GSE abort accepted', v_clear is not None)
    if v_clear is None: return
    if offline:
        sim.wait_v(5000); r = retired()
        sim.check('GB still offline: operator Clear retires the automatic Abort', bool(r), retired=r[-3:])
    clean = [b for b in AV_BAY if b not in sim.aborted_set(sim.st())]
    late = set()
    while sim.vclock() - v_clear < 60000 and not sim.stop.is_set():
        late |= set(sim.aborted_set(sim.st())) & set(clean)
        sim.wait_v(1000)
    sim.check('no AV bay board aborts after the abort is retired/cleared', not late, late=sorted(late), clean_at_clear=clean)
    code, diag = sim.api('/api/network_diagnostics')
    sim.result['reliable_after'] = (diag or {}).get('reliable') if code == 200 else diag
    sim.result['notifications_tail'] = notes()[-10:]


SCENARIOS['stale_auto_abort'] = sc_stale_auto_abort


# ------------------------------------------------------------------ operator GSE flows (gse_flows)
# Each flow drives the GS API exactly as the operator UI does and checks what the station and the
# boards did. They reproduce GS/UI faults found on hardware on 2026-11-09; see
# docs/hardware-faithful-radio.md "Operator GSE flows".
def ui_challenge_problems(ch, binding):
    """Mirror of the UI's station_mode_contract::Challenge decode and matches() (Seds-Ground-Station-Frontend):
    the review binding is flattened into the challenge together with its contract version."""
    if not isinstance(ch, dict): return ['challenge is not a JSON object']
    p = []
    if ch.get('version') != 2:
        p.append(f"version is {ch.get('version')!r}, not 2: the UI rejects it as 'Unsupported mode challenge'")
    for k in ('backend_instance', 'principal_context', 'authority_id', 'revision', 'policy_epoch', 'current_id', 'target_id', 'ticket', 'transition_id', 'summary'):
        if not isinstance(ch.get(k), str) or not ch.get(k): p.append(f'{k} missing')
    for k in ('current', 'target'):
        if not isinstance(ch.get(k), dict): p.append(f'{k} missing')
    if 'required_confirmation' not in ch: p.append('required_confirmation missing')
    if not isinstance(ch.get('expires_in_ms'), int) or not 1 <= ch['expires_in_ms'] <= 15000: p.append('expires_in_ms outside 1..15000')
    for k, v in binding.items():
        if ch.get(k) != v: p.append(f'{k} {ch.get(k)!r} differs from the reviewed {v!r}')
    if (ch.get('current') or {}).get('id') != binding.get('current_id'): p.append('current.id differs from current_id')
    if (ch.get('target') or {}).get('id') != binding.get('target_id'): p.append('target.id differs from target_id')
    return p


def ui_receipt_problems(rc, binding, ch):
    """Mirror of the UI's station_mode_contract::Accepted::matches() for the confirmation receipt."""
    if not isinstance(rc, dict): return ['receipt is not a JSON object']
    p = []
    if rc.get('version') != 2: p.append(f"version is {rc.get('version')!r}, not 2")
    if rc.get('accepted') is not True: p.append('accepted is not true')
    for k in ('backend_instance', 'principal_context', 'authority_id', 'policy_epoch', 'current_id', 'target_id'):
        if rc.get(k) != binding.get(k): p.append(f'{k} differs from the request')
    t = rc.get('transition') or {}
    if t.get('id') != ch.get('transition_id'): p.append('transition.id differs from the challenge')
    if t.get('target_id') != binding.get('target_id'): p.append('transition.target_id differs')
    if t.get('state') != 'pending': p.append(f"transition.state is {t.get('state')!r}, not pending")
    if t.get('error') is not None: p.append('transition.error is set')
    return p


GSE_REST = ('idle', 'cancelled', 'passed', 'paused', 'fault', None)


def gse_now(sim):
    code, g = sim.api('/api/gse/status')
    return g if code == 200 and isinstance(g, dict) else {}


def policy_control(sim, cmd, snap=None):
    pol = (snap or {}).get('policy') if snap else None
    if pol is None:
        code, pol = sim.api('/api/action_policy')
        if code != 200: return None
    return next((c for c in (pol or {}).get('controls', []) if c.get('cmd') == cmd), None) if isinstance(pol, dict) else None


def current_mode(sim):
    code, st = sim.api('/api/station-modes')
    return ((st or {}).get('current') or {}).get('mode_id') if code == 200 else None


def other_mode(home):
    return 'normal' if home == 'hitl' else 'hitl'


def switch_round_trip_any(sim, label):
    """home -> other -> home in process (UI review contract checked on every leg)."""
    home = current_mode(sim) or sim.a.station_mode
    def settle():
        # Entering Normal or test fire runs the startup valve check about 2 s later; a switch that
        # meets it is (correctly) refused, so let it finish first.
        sim.wait_v(4000)
        sim.wait_until(lambda s: (s.get('gse') or {}).get('phase') in GSE_REST, 30000, 'GSE at rest after the switch', quiet=True)
    ok = station_mode_switch(sim, other_mode(home), f'{label}-to-{other_mode(home)}')
    settle()
    back = station_mode_switch(sim, home, f'{label}-to-{home}') if ok else False
    settle()
    return ok and back


def mode_switch_refused(sim, label, words):
    """The station refuses a mode change now: the target is offered as unavailable with a reason naming
    one of `words`, and a review (prepare) is refused. Returns the reason."""
    c, st = sim.api('/api/station-modes')
    home = ((st or {}).get('current') or {}).get('mode_id') if c == 200 else None
    target = other_mode(home)
    opt = next((o for o in (st or {}).get('options', []) if o.get('id') == target), None) if c == 200 else None
    binding = {k: st.get(k) for k in ('backend_instance', 'principal_context', 'authority_id', 'revision', 'policy_epoch')} if c == 200 else {}
    binding.update(current_id=((st or {}).get('current') or {}).get('id'), target_id=target)
    c2, b2 = sim.api('/api/station-modes/prepare', binding)
    reason = ' '.join(str(x) for x in ((opt or {}).get('reason'), (st or {}).get('reason'), b2 if c2 != 200 else '') if x)
    ok = bool(opt) and not opt.get('available') and c2 != 200 and any(w in reason.lower() for w in words)
    sim.check(f'{label}: mode change refused', ok, want=words, available=(opt or {}).get('available'), reason=reason[:400], prepare_http=c2)
    return reason


def sequence_ran(sim, label, start_cmd, timeout_ms, block_words=('sequence',), on_snap=None):
    """Operator command that starts a GSE sequence; waits until the engine leaves its active phases.
    While it runs, a mode change must be refused (GS 0be3424/322cfd8). Returns (accepted, final gse, valves seen)."""
    v0 = sim.vclock(); code, body = sim.command(start_cmd)
    sim.check(f'{label}: {start_cmd} accepted', code == 200, http=code, body=body)
    if code != 200: return False, gse_now(sim), []
    seen, active, blocked = [], False, False
    while sim.vclock() - v0 < timeout_ms and not sim.stop.is_set():
        s = sim.st(); g = s.get('gse') or {}; ph = g.get('phase')
        if s.get('v', 0) < v0: time.sleep(.05); continue
        if isinstance(g.get('valves'), list) and (not seen or seen[-1] != g['valves']): seen.append(list(g['valves']))
        if ph not in GSE_REST:
            active = True
            if not blocked:
                blocked = True; mode_switch_refused(sim, f'{label} running ({ph})', block_words)
        verdict = on_snap(g) if on_snap else False
        if verdict == 'done': break
        if verdict: continue
        if active and ph in GSE_REST and ph != 'paused': break
        time.sleep(.05)
    g = gse_now(sim)
    sim.log_action('sequence_end', label=label, phase=g.get('phase'), message=g.get('message'), valves_seen=seen[:40])
    if not blocked: sim.check(f'{label}: mode change refused while it runs (sequence never observed active)', False)
    return True, g, seen


def flow_isolation(sim):
    """UI 790627f: the isolation checkbox must reach the station and unlock the valve self-test."""
    # Only the isolation checkbox may be what holds the self-test (not the Startup flight state).
    s = sim.wait_until(lambda s: ((s.get('gse') or {}).get('request_gate') or {}).get('prelaunch') is True, 60000, 'prelaunch')
    g = gse_now(sim); c = policy_control(sim, 'ValveSelfTest') or {}
    sim.check('isolation: self-test held only by the isolation confirmation', s is not None and g.get('dry_self_test_confirmed') is False and
              not c.get('enabled') and any(w in str(c.get('disabled_reason')).lower() for w in ('isolat', 'confirm')),
              confirmed=g.get('dry_self_test_confirmed'), control=c, request_gate=g.get('request_gate'))
    code, body = sim.command('ValveSelfTest')
    sim.check('isolation: ValveSelfTest refused before isolation is confirmed', code != 200, http=code, body=body)
    sim.wait_v(1500)  # past the station's 500 ms software-command de-duplication
    # Exactly what the Actions tab posts for the checkbox (actions_tab.rs, POST /api/gse/self-test-confirmation).
    code, body = sim.api('/api/gse/self-test-confirmation', {'confirmed': True})
    sim.log_action('isolation_confirm', http=code, body=body)
    sim.check('isolation: station accepted the confirmation', code == 200 and body == {'confirmed': True}, http=code, body=body)
    s = sim.wait_until(lambda s: (s.get('gse') or {}).get('dry_self_test_confirmed') is True and (policy_control(sim, 'ValveSelfTest', s) or {}).get('enabled'),
                       10000, 'isolation confirmed and ValveSelfTest enabled')
    sim.check('isolation: station reports it confirmed and enables ValveSelfTest', s is not None,
              confirmed=(sim.st().get('gse') or {}).get('dry_self_test_confirmed'), control=policy_control(sim, 'ValveSelfTest', sim.st()))


def flow_self_test(sim):
    ok, g, seen = sequence_ran(sim, 'self-test', 'ValveSelfTest', 120000)
    # HITL keeps no qualification record and returns to idle with a completion message.
    passed = g.get('phase') == 'passed' or bool((g.get('qualification') or {}).get('self_test')) or \
        (g.get('phase') == 'idle' and 'self-test complete' in str(g.get('message')).lower())
    sim.check('self-test: passes', ok and passed, phase=g.get('phase'), message=g.get('message'), qualification=g.get('qualification'))
    moved = [i for i in range(5) if any(v[i] for v in seen) and any(not v[i] for v in seen)]
    sim.check('self-test: every valve readback (AB/VB) opened and closed', moved == list(range(5)), moved=moved, seen=seen[:30])
    sim.check('self-test: the isolation confirmation is consumed', g.get('dry_self_test_confirmed') is False, confirmed=g.get('dry_self_test_confirmed'))


def cold_flow_at_hold(sim, g):
    """Cold flow from the nitrogen-test hold: a mode change is refused while it runs; then cancel it."""
    c, st = sim.api('/api/station-modes')
    opt = next((o for o in (st or {}).get('options', []) if o.get('id') == 'hitl'), {}) if c == 200 else {}
    sim.log_action('mode_change_at_hold_before_cold_flow', available=opt.get('available'), reason=opt.get('reason'))
    code, body = sim.api('/api/gse/cold-flow/start', {}); sim.log_action('cold_flow_start', http=code, body=body)
    sim.check('cold flow: starts from the nitrogen-test hold', code == 200, http=code, body=body, cold_flow=g.get('cold_flow'))
    if code != 200: return
    s = sim.wait_until(lambda s: ((s.get('gse') or {}).get('cold_flow') or {}).get('active') is True, 10000, 'cold flow active')
    # The engine counts cold flow as a running sequence, so the station names either.
    if s: mode_switch_refused(sim, 'cold flow running', ('cold flow', 'sequence'))
    key = ((s or sim.st()).get('gse') or {}).get('cold_flow', {}).get('key')
    code, body = sim.api('/api/gse/cold-flow/cancel', key); sim.log_action('cold_flow_cancel', http=code, body=body)
    s = sim.wait_until(lambda s: not ((s.get('gse') or {}).get('cold_flow') or {}).get('active'), 60000, 'cold flow cancelled')
    sim.check('cold flow: cancels', s is not None, cancel_http=code, cold_flow=(sim.st().get('gse') or {}).get('cold_flow'))


def nitrogen_test(sim, label, at_hold):
    """The stepped nitrogen test on the valve-driven plant; at its hold either dump (fill follows) or hand off to
    cold flow."""
    sim.gse_plant['nitrogen'] = True
    st = {'held': False}
    def hold(g):
        live = [c for c in (g.get('sequence_progress') or {}).get('controls', []) if c.get('hold_choice')]
        if st['held'] or not (live and g.get('phase') == 'paused'): return False
        st['held'] = True
        if at_hold == 'cold_flow':
            cold_flow_at_hold(sim, g); return 'done'
        if (live[0]['hold_choice'].get('dump') or {}).get('allowed'):
            code, body = sim.api('/api/gse/sequences/dump', live[0]['key']); sim.log_action('nitrogen_hold_dump', http=code, body=body)
            sim.wait_v(1000); return True
        return False
    ok, g, seen = sequence_ran(sim, label, 'NitrogenTest', 240000, on_snap=hold)
    return ok, g, st['held']


def flow_nitrogen(sim):
    """Normal/test fire: the nitrogen test, dumped at its hold so the fill can follow."""
    ok, g, held = nitrogen_test(sim, 'nitrogen test', 'dump')
    passed = bool((g.get('qualification') or {}).get('nitrogen_test')) or g.get('nitrogen_passed') is True
    sim.check('nitrogen test: passes', ok and passed and g.get('phase') == 'passed', phase=g.get('phase'), message=g.get('message'),
              qualification=g.get('qualification'), held=held)


def flow_cold_flow(sim):
    """A further nitrogen test handed to cold flow at its hold (GS 0be3424: cold flow holds a mode change)."""
    ok, g, held = nitrogen_test(sim, 'nitrogen test for cold flow', 'cold_flow')
    sim.check('cold flow: the nitrogen test reached its hold', held, phase=g.get('phase'), message=g.get('message'))


def flow_n2_override(sim):
    """GS c56bd8c / UI acbc3a2: the HITL nitrogen-test override, then the gated fill is admitted. Normal and test
    fire have the operational cold-flow override on the same route (needs the self-test's baseline)."""
    sim.gse_plant['nitrogen'] = True
    hitl = sim.a.station_mode == 'hitl'
    g = gse_now(sim); no = g.get('nitrogen_override')
    if hitl: sim.check('n2 override: /api/gse/status reports nitrogen_override', isinstance(no, dict), nitrogen_override=no)
    code, body = sim.command('StartFill')
    sim.check('n2 override: StartFill refused before the nitrogen test or its override', code != 200, http=code, body=body)
    if hitl:
        s = sim.wait_until(lambda s: ((s.get('gse') or {}).get('nitrogen_override') or {}).get('available') is True, 15000, 'override available')
        sim.check('n2 override: available at rest', s is not None, nitrogen_override=(sim.st().get('gse') or {}).get('nitrogen_override'))
    # Exactly what the override dialog posts (gse_panel.rs HitlNitrogenOverride / the cold-flow override).
    code, body = sim.api('/api/gse/cold-flow/override', {'reason': 'fullnet gse_flows: known-good tank'})
    sim.log_action('nitrogen_override', http=code, body=body)
    sim.check('n2 override: station accepted the override', code == 200, http=code, body=body)
    if hitl:
        pred = lambda g: (g.get('nitrogen_override') or {}).get('active') is True and g.get('nitrogen_overridden') is True
    else:
        pred = lambda g: g.get('nitrogen_overridden') is True and bool((g.get('qualification') or {}).get('nitrogen_override'))
    s = sim.wait_until(lambda s: pred(s.get('gse') or {}), 10000, 'override active')
    g = (s or sim.st()).get('gse') or {}
    sim.check('n2 override: /api/gse/status reports the override active', s is not None, nitrogen_override=g.get('nitrogen_override'),
              nitrogen_overridden=g.get('nitrogen_overridden'), qualification=g.get('qualification'), phase=g.get('phase'))


def flow_fill(sim):
    """StartFill after self-test and nitrogen test (or its override): the nitrous valve opens on the boards
    and the fill reaches its mass target on the loadcell plant."""
    sim.gse_plant['nitrous'] = True
    st = {'open': False, 'resumed': 0}
    def watch(g):
        if isinstance(g.get('valves'), list) and g['valves'][4]: st['open'] = True
        if g.get('phase') == 'paused' and 'below target' in str(g.get('message')) and st['resumed'] < 3:
            st['resumed'] += 1; sim.command('StartFill'); sim.wait_v(2000); return True
        if (g.get('qualification') or {}).get('fill_target') and g.get('phase') in GSE_REST: return 'done'
        # HITL keeps no qualification record: the settled-mass verdict ends the fill.
        if g.get('phase') == 'paused' and 'settled within tolerance' in str(g.get('message')): return 'done'
        return False
    # As an operator would: a fresh fill starts from the tank's zero band, so wait for a relief (a nitrogen
    # dump or a cold-flow cancel) to finish venting before pressing Fill.
    def vented(s):
        g = s.get('gse') or {}; b = g.get('baseline') or {}; p = g.get('pressure_psi')
        return isinstance(p, (int, float)) and 'average_psi' in b and abs(p - b['average_psi']) <= max(b.get('noise_psi') or 0, 0.01)
    if (gse_now(sim).get('baseline') or {}):
        sim.wait_until(vented, 30000, 'tank vented to its zero band before Fill')
    ok, g, seen = sequence_ran(sim, 'fill', 'StartFill', 240000, on_snap=watch)
    sim.check('fill: nitrous valve readback opened', st['open'], valves_seen=seen[:30])
    done = bool((g.get('qualification') or {}).get('fill_target')) or g.get('phase') == 'passed' or 'settled within tolerance' in str(g.get('message'))
    sim.check('fill: reaches its mass target', ok and done, phase=g.get('phase'), message=g.get('message'), mass=sim.mass,
              mission_stage=g.get('mission_stage'), qualification=g.get('qualification'))
    # The station holds the settled fill: no valve fault or acknowledgement timeout afterwards.
    v0 = sim.vclock(); bad = None
    while sim.vclock() - v0 < 8000 and not sim.stop.is_set() and bad is None:
        m = str((sim.st().get('gse') or {}).get('message'))
        if any(w in m.lower() for w in ('timed out', 'fault', 'opposite state')): bad = m
        time.sleep(.1)
    sim.check('fill: settled fill holds without a valve fault', bad is None, message=bad or gse_now(sim).get('message'))


def flow_turbocharge(sim):
    c = policy_control(sim, 'Turbocharge')
    if not (c or {}).get('enabled'):
        sim.result.setdefault('not_reached', []).append({'flow': 'turbocharge', 'control': c}); return
    code, body = sim.command('Turbocharge')
    s = sim.wait_until(lambda s: ((s.get('gse') or {}).get('turbocharge') or {}).get('active') is True, 10000, 'turbocharge active')
    if s is None:
        sim.result.setdefault('not_reached', []).append({'flow': 'turbocharge', 'http': code, 'body': body, 'turbocharge': (sim.st().get('gse') or {}).get('turbocharge')}); return
    mode_switch_refused(sim, 'turbocharge running', ('turbocharge',))
    sim.command('CancelFill')
    s = sim.wait_until(lambda s: not ((s.get('gse') or {}).get('turbocharge') or {}).get('active'), 60000, 'turbocharge ended')
    sim.check('turbocharge: ends on Cancel fill', s is not None, turbocharge=(sim.st().get('gse') or {}).get('turbocharge'))


def flow_manual_switch(sim):
    """GS 322cfd8: after a completed manual valve command the manual-control latch no longer holds a mode
    change; the round trip also checks the UI review contract (GS f061992) for this mode."""
    for _ in range(2):
        code, body = sim.command('Dump'); sim.wait_v(3000)
    sim.wait_until(lambda s: (s.get('gse') or {}).get('phase') in GSE_REST, 30000, 'GSE at rest after manual Dump')
    sim.check('mode change allowed after a completed manual valve command', switch_round_trip_any(sim, 'after-manual-dump'),
              manual=gse_now(sim).get('manual_control'))


def flow_challenge(sim):
    sim.check('mode change allowed at rest (UI review contract)', switch_round_trip_any(sim, 'at-rest'))


def flow_rules_hitl(sim):
    """GS 0be3424/322cfd8 in HITL: allowed in a fill phase and while Aborted; refused by a pending Launch and
    the countdown. The Abort comes last: its recovery is not needed by the other steps."""
    ok = phase_to(sim, 4, 'to NitrogenFill')
    sim.check('rules: HITL reaches NitrogenFill', ok, flight_state=(sim.st().get('rec') or {}).get('flight_state'))
    if ok: sim.check('mode change allowed in the NitrogenFill phase', switch_round_trip_any(sim, 'nitrogen-fill-phase'))
    ok = phase_to(sim, 6, 'to Armed')
    sim.check('rules: HITL reaches Armed', ok, flight_state=(sim.st().get('rec') or {}).get('flight_state'))
    if ok: launch_gate(sim)
    latch_all(sim, 'rules abort', expect=BOARDS)
    sim.check('mode change allowed while Aborted', switch_round_trip_any(sim, 'aborted'))
    r = sim.recovery('reset_startup', boards=BOARDS, label='rules reset all')
    if not r.get('success'): recover_all_needing(sim, 'rules leftovers')
    verify_clear(sim, 'rules after abort', timeout_ms=45000)


def launch_gate(sim):
    """Launch in Armed: the mode change is refused for the pending Launch or the countdown
    (launch-mode-gate.jsonl), then the launch is reset and the station returns to Idle."""
    rows, launched = (sim.out / 'launch-mode-gate.jsonl').open('a'), False
    for attempt in range(5):
        sim.api('/api/board_abort/refresh', {}); sim.wait_v(1500)
        v0 = sim.vclock(); code, body = sim.command('Launch')
        if code == 200: launched = True; break
    sim.check('rules: Launch accepted in Armed', launched, http=code, body=body)
    if not launched: return
    seen = []
    while sim.vclock() - v0 < 25000 and not sim.stop.is_set():
        c, st = sim.api('/api/station-modes'); c2, clk = sim.api('/api/launch_clock')
        opt = next((o for o in (st or {}).get('options', []) if o.get('id') == 'normal'), {}) if c == 200 else {}
        row = {'v': sim.vclock() - v0, 'available': opt.get('available'), 'reason': opt.get('reason'), 'clock': (clk or {}).get('kind') if c2 == 200 else None}
        rows.write(json.dumps(row) + '\n'); rows.flush()
        if not seen or (seen[-1]['available'], seen[-1]['reason'], seen[-1]['clock']) != (row['available'], row['reason'], row['clock']): seen.append(row)
        time.sleep(.05)
    pending = any('launch command is pending' in str(r['reason']).lower() for r in seen)
    countdown = any('countdown' in str(r['reason']).lower() for r in seen)
    leak = [r for r in seen if r['available'] and r['clock'] in ('t_minus', 'TMinus')]
    sim.check('Launch pending or countdown refuses the mode change', (pending or countdown) and not leak, pending=pending, countdown=countdown, leak=leak, transitions=seen[:20])
    sim.command('ResetTClock'); sim.wait_v(5000)
    phase_to(sim, 1, 'rewind to Idle after Launch')
    s = sim.wait_until(lambda s: not (s.get('rec') or {}).get('automatic_actions_inhibited') and 'locked after' not in lock_reason(sim, s)[1], 40000, 'launch lock released')
    sim.check('rules: launch lock released after the reset', s is not None, reason=lock_reason(sim)[1])


def abort_keys(sim):
    code, ab = sim.api('/api/board_abort')
    errs = (ab or {}).get('active_errors', []) if code == 200 else []
    return {e.get('board'): e.get('timestamp_ms') for e in errs}, errs


def flow_dismissals(sim):
    """GS 8215ce7 / UI 5090cf4, 69165ff, c280bb7: board abort errors are dismissible (HITL, every board), the UI
    keys a dismissal by (board, timestamp_ms), so that key must survive a websocket reconnect, an AV-bay radio
    drop and a station restart; alert acknowledgements must survive a reconnect. Schedule: sched cut0..cut1
    (AV-bay radio cut) and gsrb (station power cycle), virtual seconds."""
    a = sim.a; S = a.sched; mode = a.station_mode
    shown = [b for b in BOARDS if not (mode == 'test_fire' and b in AV_BAY)]
    latch_all(sim, 'dismissals', expect=shown)
    sim.wait_v(3000)
    keys0, errs = abort_keys(sim)
    sim.check('dismissals: every aborted board has an active error', sorted(keys0) == sorted(shown), boards=sorted(keys0), want=shown)
    want = mode == 'hitl'
    sim.check(f"dismissals: every board's abort error {'is' if want else 'is not'} dismissible in {mode}",
              bool(errs) and all(e.get('dismissible', False) is want for e in errs), dismissible={e.get('board'): e.get('dismissible') for e in errs})
    code, alerts = sim.api('/api/alerts?minutes=60')
    lst = alerts if isinstance(alerts, list) else []
    newest = lambda sev: max((x.get('timestamp_ms', 0) for x in lst if x.get('severity') == sev), default=0)
    ack = {'warning_timestamp_ms': newest('warning'), 'error_timestamp_ms': newest('error')}
    code, reply = sim.api('/api/alerts/ack', ack); sim.log_action('alerts_ack', request=ack, http=code, reply=reply, alerts=len(lst))
    sim.check('dismissals: alert acknowledgement accepted', code == 200 and (reply or {}).get('warning_ack_timestamp_ms') == ack['warning_timestamp_ms']
              and (reply or {}).get('error_ack_timestamp_ms') == ack['error_timestamp_ms'], http=code, reply=reply, request=ack)
    def reconnect(tag):
        try: ws = WsWatch(sim, sim.origin, sim.token, tag=tag)
        except Exception as e: sim.log_action('ws_failed', error=repr(e)); return None
        t = time.time() + 30
        while time.time() < t and not any(e['ty'] == 'AlertAckState' for e in ws.events): time.sleep(.2)
        return next((e.get('data') for e in ws.events if e['ty'] == 'AlertAckState'), None)
    def same(tag):
        keys, _ = abort_keys(sim)
        moved = {b: (keys0[b], keys.get(b)) for b in keys0 if b in keys and keys[b] != keys0[b]}
        sim.check(f'dismissals: abort error keys (board, timestamp_ms) unchanged after {tag}', not moved and set(keys) >= set(keys0), moved=moved, missing=sorted(set(keys0) - set(keys)))
    st = reconnect('ws-reconnect')
    sim.check('dismissals: alert acknowledgement restored on a websocket reconnect', (st or {}).get('warning_ack_timestamp_ms') == ack['warning_timestamp_ms']
              and (st or {}).get('error_ack_timestamp_ms') == ack['error_timestamp_ms'], ack_state=st, ack=ack)
    same('a websocket reconnect')
    if S.get('cut0'):
        wait_until_v(sim, (S['cut1'] + 10) * 1000, 'AV-bay radio back')
        sim.wait_until(lambda s: all(b in sim.aborted_set(s) for b in shown), 30000, 'abort errors back after the radio cut')
        same('an AV-bay radio drop and return')
    if S.get('gsrb'):
        wait_until_v(sim, S['gsrb'] * 1000 + 2000, 'station power cycled')
        back = sim.wait_until(lambda s: isinstance(s.get('rec'), dict) and s['rec'].get('boards') and all(b in sim.aborted_set(s) for b in shown), 120000, 'station back with abort errors')
        sim.check('dismissals: station back after the power cycle', back is not None)
        same('a station restart')
        st = reconnect('ws-after-restart')
        sim.result['alert_ack_after_restart'] = {'ack_state': st, 'acked': ack}
        sim.log_action('alert_ack_after_restart', ack_state=st, acked=ack)
    sim.save_snapshot('dismissals')
    r = sim.recovery('reset_startup', boards=BOARDS, label='dismissals reset all')
    if not r.get('success'): recover_all_needing(sim, 'dismissals leftovers')
    verify_clear(sim, 'after dismissals', timeout_ms=45000)


FLOWS = {'isolation': flow_isolation, 'self_test': flow_self_test, 'nitrogen': flow_nitrogen, 'cold_flow': flow_cold_flow, 'n2_override': flow_n2_override,
         'fill': flow_fill, 'turbocharge': flow_turbocharge, 'manual_switch': flow_manual_switch, 'challenge': flow_challenge,
         'rules_hitl': flow_rules_hitl, 'dismissals': flow_dismissals}


# Default --flows per station mode: the full operator path with the real nitrogen test.
DEFAULT_FLOWS = {'normal': 'isolation,self_test,nitrogen,cold_flow,fill,turbocharge,manual_switch',
                 'test_fire': 'isolation,self_test,nitrogen,cold_flow,fill,turbocharge,manual_switch',
                 'hitl': 'isolation,self_test,nitrogen,cold_flow,fill,manual_switch'}


def gse_flow_order(names):
    """--flows in order, except that cold_flow runs before fill: its further nitrogen test starts by opening dump
    and vent for the empty-tank baseline, which belongs before the tank holds a fill."""
    names = list(names)
    if 'cold_flow' in names and 'fill' in names and names.index('cold_flow') > names.index('fill'):
        names.remove('cold_flow'); names.insert(names.index('fill'), 'cold_flow')
    return names


def sc_gse_flows(sim):
    """Operator GSE flows on the faithful radio, all seven boards, in the station mode under test (--flows,
    in order with cold_flow before fill; default DEFAULT_FLOWS; see FLOWS). Each flow is a step: a fault is
    recorded and the next flow still runs."""
    global BOARDS
    BOARDS = present_boards(sim)
    if sim.a.station_mode == 'test_fire':
        # A test fire covers the stand only: the station leaves the (still simulated) AV bay unqueried.
        BOARDS = [b for b in BOARDS if b not in AV_BAY]
    sim.gse_plant = {'nitrogen': False, 'nitrous': False}
    snap = sim.wait_ready(BOARDS, timeout_ms=sim.a.ready_timeout_ms)
    sim.check('startup: all boards healthy', snap is not None, rows={b: (sim.rows().get(b) or {}).get('readback_status') for b in BOARDS})
    # HITL does not report topology_stable.
    g = sim.wait_until(lambda s: (s.get('gse') or {}).get('phase') in ('idle', 'cancelled', 'passed') and (s.get('gse') or {}).get('topology_stable') is not False
                       and isinstance((s.get('gse') or {}).get('valves'), list) and None not in s['gse']['valves'], 120000, 'GSE at rest with valve readbacks')
    sim.check('startup: GSE at rest with AB/VB valve readbacks', g is not None, gse={k: (sim.st().get('gse') or {}).get(k) for k in ('phase', 'valves', 'message')})
    names = gse_flow_order(f for f in sim.a.flows.split(',') if f)
    sim.log_action('gse_flows_order', flows=names)
    for name in names:
        step(sim, name, lambda: FLOWS[name](sim))
    sim.save_snapshot('end')


SCENARIOS['gse_flows'] = sc_gse_flows


def ui_contract_check(sim):
    """Decode each live station mode review captured by station_mode_switch (contract-live/<label>/) with the
    UI's own types: Seds-Ground-Station-Frontend live_station_mode_switch_decodes_with_the_ui_contract."""
    for d in sorted(p for p in (sim.out / 'contract-live').glob('*') if (p / 'after.json').exists() or (p / 'prepare.json').exists()):
        r = subprocess.run(['cargo', 'test', '--quiet', 'live_station_mode', '--', '--test-threads=1'], cwd=sim.a.ui_contract,
                           env={**os.environ, 'GS_LIVE_STATION_MODE_DIR': str(d)}, capture_output=True, text=True)
        (d / 'ui-decode.log').write_text(r.stdout + r.stderr)
        sim.result['checks'].append({'v': None, 'name': f'{d.name}: the UI decodes the live review, receipt and switch', 'pass': r.returncode == 0,
                                     'log': str(d / 'ui-decode.log')})
    sim.result['passed'] = all(c['pass'] for c in sim.result['checks']); sim.write_result()


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
    p.add_argument('--abort-bound-ms', type=int, default=3000, help='pyro_paths: AV-bay Abort latency bound')
    p.add_argument('--radio', choices=['hub', 'rfd', 'rfd-faithful', 'backlog'], default='rfd')
    p.add_argument('--rfd-global-retries', type=int, default=3, help='extra air copies per packet; 3 is calibrated against the bench uplink (see docs)')
    p.add_argument('--faithful-time', type=float, default=0, help='wall seconds per virtual second (Renode pace + time-dilated station)')
    p.add_argument('--phase-a-ms', type=int, default=180000)
    p.add_argument('--gps-initial', type=int, choices=[0, 1, 2], default=0, help='RF GPS fixture at power-up: 0 no fix, 1 fix, 2 u-blox placeholder date, no fix')
    p.add_argument('--rfd-modem', default='')
    p.add_argument('--rfd-profile', choices=['', 'drain', 'field'], default='',
                   help='model every radio station\'s modem with these registers (docs/rfd-radio-model-audit.md): '
                        'drain = deployed rfd-async-64k-drain (S5=1, S6=10), field = 2026-10-28 readback (S5=10, S6=50)')
    p.add_argument('--rfd-broadcast-retry', choices=['', 'blind', 'on_loss'], default='', help='GLOBAL_RETRIES reading (open assumption A1)')
    p.add_argument('--rfd-carrier-sense', choices=['', 'register', 'always', 'never'], default='', help='LBT reading (open assumption A2)')
    p.add_argument('--rfd-air-loss-ppm', type=int, default=0, help='independent per-copy air loss')
    p.add_argument('--radio-trace', action='store_true')
    p.add_argument('--radio-trace-max-mb', type=int, default=4, help='per-stream radio trace bound (4..256)')
    p.add_argument('--gs-reboot', action='append', default=[])
    p.add_argument('--board-reboot', action='append', default=[])
    p.add_argument('--can-cut', action='append', default=[])
    p.add_argument('--radio-cut', action='append', default=[], help='s0:s1 virtual seconds: GS<->RF direct radio path broken')
    p.add_argument('--wall-freshness-scale', type=int, default=30)
    p.add_argument('--rust-log', default='info')
    p.add_argument('--cpus', type=int, default=6)
    p.add_argument('--poll', type=float, default=.05)
    p.add_argument('--timeout', type=int, default=4 * 3600)
    p.add_argument('--gs-env', action='append', default=[])
    p.add_argument('--nodes', default='', help='subset of rf,power,flight,gateway,actuator,valve,daq')
    p.add_argument('--no-query', action='store_true', help='passive comms measurement (no operator Query load)')
    p.add_argument('--sched', type=json.loads, default={}, help='{"can0":s,"can1":s,"gsrb":s,"abrb":s} virtual seconds')
    p.add_argument('--flows', default='', help='gse_flows: comma list of ' + ','.join(FLOWS) + ' (default: the full operator path for --mode)')
    p.add_argument('--gse-config', type=json.loads, default={}, help='JSON merged into the station gse.json')
    p.add_argument('--ui-contract', type=Path, help='Seds-Ground-Station-Frontend checkout: after the run, decode every captured live '
                   'station mode review with the UI (cargo test live_station_mode)')
    a = p.parse_args()
    a.station_mode = {'hitl_mode': 'hitl', 'test_fire_mode': 'test_fire'}.get(a.mode, a.mode)
    a.mode = {'hitl': 'hitl_mode', 'test_fire': 'test_fire_mode'}.get(a.station_mode, 'normal')
    if a.rfd_profile and not a.rfd_modem:
        regs = {'drain': '5:GLOBAL_RETRIES=1,6:SER_BRK_DETMS=10', 'field': '5:GLOBAL_RETRIES=10,6:SER_BRK_DETMS=50'}[a.rfd_profile]
        # Station indexes are the rocket_radio endpoint order: rf, groundstation, then rrls.
        a.rfd_modem = ';'.join(f'{i}={regs}' for i in range(3 if a.rrls else 2))
    if a.sched.get('can0'): a.can_cut.append(f"DAQ:{a.sched['can0']}:{a.sched['can1']}")
    if a.sched.get('gsrb'): a.gs_reboot.append(str(a.sched['gsrb']))
    if a.sched.get('abrb'): a.board_reboot.append(f"AB:{a.sched['abrb']}")
    if a.sched.get('vbrb'): a.board_reboot.append(f"VB:{a.sched['vbrb']}")
    if a.sched.get('vbc0'): a.can_cut.append(f"VB:{a.sched['vbc0']}:{a.sched['vbc1']}")
    if a.sched.get('cut0'): a.radio_cut.append(f"{a.sched['cut0']}:{a.sched['cut1']}")
    if a.sched.get('dq0'): a.can_cut.append(f"DAQ:{a.sched['dq0']}:{a.sched['dq1']}")
    if a.sched.get('gbc0'): a.can_cut.append(f"GB:{a.sched['gbc0']}:{a.sched['gbc1']}")
    if a.sched.get('hostrb'): a.gs_reboot.append(str(a.sched['hostrb'])); a.board_reboot.append(f"rrls:{a.sched['hostrb']}")
    import signal
    def _term(*_): raise KeyboardInterrupt('SIGTERM')
    signal.signal(signal.SIGTERM, _term)
    if a.scenario == 'gse_flows' and not a.flows:
        a.flows = DEFAULT_FLOWS[a.station_mode]
    unknown = [f for f in a.flows.split(',') if f and f not in FLOWS]
    assert not unknown, ('unknown flows', unknown)
    s = Sim(a); s.run(SCENARIOS[a.scenario])
    if a.ui_contract: ui_contract_check(s)
    print(json.dumps({k: s.result.get(k) for k in ('phase', 'passed', 'error', 'virtual_ms', 'wall_s')}))
