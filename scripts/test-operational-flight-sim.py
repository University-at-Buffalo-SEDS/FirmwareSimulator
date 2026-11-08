#!/usr/bin/env python3
"""Setup-to-landing operational flight simulation on real firmware.

Runs the seven real board images (FC, RF, PB, gateway, actuator, valve, DAQ)
in Renode with the flight GroundStation build (features firmware_sim only)
and its served web UI, then:

  1. drives Startup -> Idle -> valve self-test -> nitrogen test -> fill to the
     mass target (Armed) -> RF GPS lock -> QD retract -> Launch through the
     ordinary GS HTTP API (synthetic fixture account, no state injection);
  2. at the first observed T-plus writes the launch anchor, so the FC
     barometer/IMU and RF GPS models (renode/peripherals/SedsSpiSensors.cs)
     replay a deterministic flight profile (scripts/flight_profile.py) keyed on
     Renode virtual time;
  3. after T+--comms-loss-after-s cuts the RF radio UART (RF usart1 <-> GS
     av_bay PTY, both directions) for --comms-loss-s virtual seconds via the
     SEDS_UART_GATE file of renode/peripherals/SedsStm32Uart.cs, while every
     board keeps running; then restores it;
  4. records an append-only timeline.jsonl (FC state from the real `sm`
     variable, GS /flightstate, /api/mission_phase, /api/launch_clock,
     notifications, comms events), status.json for UI drivers, and a
     result.json with per-step pass/fail.

Optional --ui-driver CMD starts once the GS origin is known with GS_ORIGIN,
GS_USER, GS_PASSWORD, EVIDENCE_DIR, STATUS_FILE and TIMELINE_FILE set.

All plant inputs (pressure, mass, GPS fix, flight profile, comms cut) are
explicit simulator stimuli; no GS or firmware state is written directly.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.client
import importlib.util
import ipaddress
import json
import os
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
WP = Path('/home/rylan/Documents/Gitkraken/SEDS/.workspace-progress/states-fc-sim-20261108')
BUILD = WP / 'flight-sim-build'
USER, PASSWORD = 'sequence-fixture', 'fixture-only-password'
FC_LOCAL = ['Startup', 'Postinit', 'Armed', 'Launch', 'Ascent', 'Coast', 'Apogee', 'Descent', 'Reefing', 'Landed', 'Recovery']
FC_GLOBAL = ['Startup', 'Idle', 'PreFill', 'FillTest', 'NitrogenFill', 'NitrousFill', 'Armed', 'Launch', 'Ascent', 'Coast',
             'Apogee', 'Descent', 'Reefing', 'Landed', 'Recovery', 'Aborted']
FLIGHT_STATES = ['Launch', 'Ascent', 'Coast', 'Apogee', 'Descent', 'Reefing', 'Landed']
OPT_LAUNCH_REQUESTED, OPT_DROGUE, OPT_MAIN = 1 << 9, 1 << 10, 1 << 11
ACCEL_FULL_SCALE = 32767 * 1.5 * 16 / 32768 * 9.80665  # m/s^2, BMI088 +/-24 g (SedsSpiSensors.cs)


# ---------------------------------------------------------------- utilities
def atomic(path: Path, text: str):
    tmp = path.with_name(path.name + '.next')
    tmp.write_text(text)
    tmp.replace(path)


def request(origin, path, body=None, token=None, timeout=10):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(origin + path, None if body is None else json.dumps(body).encode(), headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = response.read(8 * 1024 * 1024)
            return response.status, json.loads(data) if data and data[:1] in (b'{', b'[') else (data.decode(errors='replace') if data else None)
    except urllib.error.HTTPError as error:
        body = error.read(4097)
        return error.code, {'public_error': body.decode('utf-8', errors='replace')[:4096]}


def fixture_user():
    salt = b'isolated-gse-model'
    key = hashlib.pbkdf2_hmac('sha256', PASSWORD.encode(), salt, 120000)
    return key, {'version': 1, 'anonymous': {'view_data': False, 'send_commands': False}, 'users': [
        {'username': USER, 'roles': ['admin'], 'password': {'algorithm': 'pbkdf2_sha256', 'iterations': 120000,
         'salt_b64': base64.b64encode(salt).decode(), 'hash_b64': base64.b64encode(key).decode()},
         'permissions': {'view_data': True, 'send_commands': True}, 'disabled': False}]}


def login(origin, key):
    code, c = request(origin, '/api/auth/challenge', {'username': USER})
    assert code == 200, ('challenge', code, c)
    nonce = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip('=')
    message = {'username': USER, 'challenge_id': c['challenge_id'], 'server_nonce_b64': c['server_nonce_b64'],
               'client_nonce_b64': nonce, 'remember_me': False}
    proof = base64.urlsafe_b64encode(hmac.new(key, json.dumps(message, separators=(',', ':')).encode(), hashlib.sha256).digest()).decode().rstrip('=')
    code, result = request(origin, '/api/auth/login', {**message, 'proof_b64': proof})
    assert code == 200, ('login', code, result)
    return result['token']


def raw_pressure(psi):
    return max(0, min(4095, round(psi * 3103 / 2000)))


def plant_pressure(previous, dt_ms, valves, regulator_psi=130):
    """Assumption: relief dominates supply; 35 psi/s rise, 200 psi/s relief, no leak."""
    dt = max(0, min(dt_ms, 200)) / 1000
    if len(valves) != 5 or any(v is None for v in valves):
        return previous
    if valves[1] or valves[2]:
        return max(0, previous - 200 * dt)
    if valves[3]:
        return min(regulator_psi, previous + 35 * dt)
    return previous


def plant_mass(previous, dt_ms, valves, nitrous_connected, nitrogen_connected):
    """Assumed tank mass from actual valve reports (same model as the fill controller)."""
    if len(valves) != 5 or any(type(v) is not bool for v in valves):
        return previous
    dt = max(0, min(dt_ms, 200)) / 1000
    gain = 2 * dt if nitrous_connected and valves[4] else 0
    if nitrogen_connected and valves[3] and not valves[1] and not valves[2]:
        gain += 0.01 * dt
    loss = 0.2 * dt if valves[1] and not valves[4] else 0
    return max(0, previous + gain - loss)


def mass_samples(mass):
    raw = max(0, min(8388607, round(mass / 100 * 16777216 / (2.2104 * 2 / 16))))
    values = [((i + 1) * 0x100000) & 0xffffff for i in range(16)]
    for i in range(8, 12):
        values[i] = (values[2 * (i - 8)] - values[2 * (i - 8) + 1]) & 0xffffff
    values[12] = 305652
    values[0] = 0
    values[1] = raw
    return ','.join(str(v) for v in values)


def nm_symbols(board: Path, layout: dict) -> dict[str, int]:
    elf = board / layout['artifacts']['elf']
    cache = (elf.parent / 'CMakeCache.txt').read_text()
    nm = re.search(r'^CMAKE_NM:FILEPATH=(.*)$', cache, re.M)[1]
    text = subprocess.check_output([nm, '-n', str(elf)], text=True)
    return {parts[-1]: int(parts[0], 16) for line in text.splitlines() if len(parts := line.split()) >= 3}


def load_layout(board: Path) -> dict:
    """sim/board.json with artifacts pointed at the build subdirectory that exists."""
    layout = json.loads((board / 'sim/board.json').read_text())
    subdir = 'Release_Script' if (board / 'build/Release_Script/CMakeCache.txt').exists() else 'Release'
    for key, value in layout.get('artifacts', {}).items():
        parts = Path(value).parts
        if len(parts) >= 3 and parts[0] == 'build':
            layout['artifacts'][key] = str(Path('build', subdir, *parts[2:]))
    return layout


def add_probes(layout, symbols, definitions, missing):
    for name, symbol, offset in definitions:
        matches = [s for s in symbols if s == symbol or s.startswith(symbol + '.')]
        resolved = symbol if symbol in symbols else (matches[0] if len(matches) == 1 else None)
        if resolved is None:
            missing.append(symbol)
            continue
        layout['execution']['memory_probes'].append({'name': name, 'symbol': resolved, 'byte_offset': offset})


class ProbeTail:
    """Incremental reader for bay-live-*/memory-probes.txt (it grows all run)."""
    PATTERN = re.compile(r'SEDS_BAY_PROBE (\w+) ([\w_]+) (\d+)\n(0x[0-9a-fA-F]+)\n')

    def __init__(self, diagnostics: Path):
        self.diagnostics = diagnostics
        self.path = None
        self.offset = 0
        self.buffer = ''
        self.rows: dict[str, dict[int, dict[str, int]]] = {}

    def poll(self):
        if self.path is None:
            found = list(self.diagnostics.glob('bay-live-*/memory-probes.txt'))
            if not found:
                return
            self.path = found[0]
        with self.path.open('r', errors='replace') as handle:
            handle.seek(self.offset)
            chunk = handle.read(16 * 1024 * 1024)
            self.offset = handle.tell()
        self.buffer += chunk
        last = 0
        for match in self.PATTERN.finditer(self.buffer):
            node, name, sample, value = match.groups()
            self.rows.setdefault(node, {}).setdefault(int(sample), {})[name] = int(value, 16)
            last = match.end()
        self.buffer = self.buffer[last:][-4096:]

    def latest(self, node):
        samples = self.rows.get(node)
        if not samples:
            return None, {}
        sample = max(samples)
        return sample, samples[sample]


class Forwarder:
    """127.0.0.1 ephemeral port -> the owned container's internal :3000."""

    def __init__(self, address):
        self.destination = (address, 3000)
        self.stop = threading.Event()
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(64)
        self.listener.settimeout(.2)
        self.origin = 'http://127.0.0.1:%d' % self.listener.getsockname()[1]
        self.errors = 0
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self):
        while not self.stop.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._relay, args=(client,), daemon=True).start()

    def _relay(self, client):
        upstream = None
        try:
            upstream = socket.create_connection(self.destination, timeout=2)
            while not self.stop.is_set():
                ready, _, _ = select.select([client, upstream], [], [], .5)
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    (upstream if source is client else client).sendall(data)
        except OSError:
            self.errors += 1
        finally:
            for s in (client, upstream):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass

    def close(self):
        self.stop.set()
        self.listener.close()
        self.thread.join(timeout=2)


# ---------------------------------------------------------------- topology
def topology(duration_ms, wall_freshness_scale=30):
    def node(name):
        return {'name': name, 'layout': f'/simulation/{name}.json', 'firmware_root': f'/nodes/{name}'}
    return {
        'name': 'operational-flight-setup-to-landing', 'quantum_seconds': 0.0001,
        'virtual_time_ms': duration_ms, 'sample_count': duration_ms // 1000, 'enforce_end_drop': False,
        'reboots': [], 'can_ack_events': [], 'can_link_events': [], 'radio_link_events': [],
        'assertions': [], 'host_log_assertions': [],
        # rf first: the simulation clock (virtual-clock.txt) lives on the first node.
        'nodes': [node(n) for n in ('rf', 'power', 'flight', 'gateway', 'actuator', 'valve', 'daq')],
        'links': [
            {'name': 'avionics_can', 'kind': 'can', 'endpoints': [
                {'node': 'rf', 'peripheral': 'fdcan2', 'tx_probe': 'fdcan_tx_ok', 'rx_probe': 'fdcan_rx'},
                {'node': 'power', 'peripheral': 'fdcan2', 'tx_probe': 'fdcan_tx_ok', 'rx_probe': 'fdcan_rx'},
                {'node': 'flight', 'peripheral': 'fdcan1', 'tx_probe': 'fdcan_tx_ok', 'rx_probe': 'fdcan_rx'}]},
            {'name': 'rocket_radio', 'kind': 'radio', 'endpoints': [
                {'node': 'rf', 'peripheral': 'usart1', 'tx_probe': 'radio_tx_frames', 'rx_probe': 'radio_rx_frames'},
                {'node': 'groundstation', 'peripheral': 'av_bay'}]},
            {'name': 'fill_pico', 'kind': 'pico_fi', 'baud_rate': 1000000, 'endpoints': [
                {'node': 'groundstation', 'peripheral': 'fill_box'},
                {'node': 'gateway', 'peripheral': 'usart2', 'tx_probe': 'uart_tx_frames', 'rx_probe': 'uart_rx_frames'}]},
            {'name': 'fill_can', 'kind': 'can', 'endpoints': [
                {'node': 'gateway', 'peripheral': 'fdcan2', 'tx_probe': 'fdcan_tx_ok', 'rx_probe': 'fdcan_rx'},
                {'node': 'actuator', 'peripheral': 'fdcan2', 'tx_probe': 'fdcan_tx_ok', 'rx_probe': 'fdcan_rx'},
                {'node': 'valve', 'peripheral': 'fdcan2', 'tx_probe': 'fdcan_tx_ok', 'rx_probe': 'fdcan_rx'},
                {'node': 'daq', 'peripheral': 'fdcan1', 'tx_probe': 'fdcan_tx_ok', 'rx_probe': 'fdcan_rx'}]},
        ],
        'host_nodes': [{
            'name': 'groundstation', 'binary': '/usr/local/bin/groundstation_backend', 'cwd': '/opt/groundstation',
            'serial_links': [{'link': 'rocket_radio', 'env': 'GS_AV_BAY_SERIAL_PORT'},
                             {'link': 'fill_pico', 'env': 'GS_FILL_PICO_DISCOVERY_PORT', 'transport': 'pico_fi_discovery_to_uart'}],
            'network_variable_cache': '/diagnostics/state/network-variables.json',
            'env': {'RUST_LOG': 'info', 'GS_DEBUG_PRINTS': '0', 'GS_SIMULATED_SERIAL_PTY': '1', 'GS_BIND_ADDRESS': '0.0.0.0:3000',
                    'GS_HTTPS_BIND_ADDRESS': 'off', 'GS_VIDEO_MANAGED': '0', 'GS_VIDEO_DISCOVERY': '0',
                    'GS_STATION_DISCOVERY_BIND': '127.0.0.1:0', 'GS_VIDEO_DISCOVERY_BIND': '127.0.0.1:0', 'GS_SIM_MODE': '0',
                    'GS_COMMS_LINK_CONFIG': '/simulation/comms.json', 'GS_LAYOUT_PATH': '/opt/groundstation/backend/layout/layout.json',
                    'GS_HEARTBEAT_PERIOD_MS': '500', 'GS_SIM_NETWORK_EPOCH_MS': '1767225600000',
                    # firmware_sim build only: stretch the wall-clock board/retraction
                    # launch proofs for a bay that runs 10-20x slower than real time.
                    'GS_SIM_WALL_FRESHNESS_SCALE': str(wall_freshness_scale),
                    'GS_NETWORK_VARIABLE_CACHE': '/diagnostics/state/network-variables.json', 'GS_USERS_FILE': '/simulation/users.json',
                    'GS_GSE_CONFIG': '/diagnostics/state/gse.json', 'GS_GSE_QUALIFICATION': '/diagnostics/state/qualification.json',
                    'GS_FILL_TARGETS_CONFIG': '/simulation/fill-targets.json', 'GS_CHECKLISTS_FILE': '/diagnostics/state/checklists.json',
                    'GS_LOADCELL_CALIBRATION_PATH': '/diagnostics/state/calibration.json',
                    'GS_CALIBRATION_CONFIG_PATH': '/diagnostics/state/calibration-config.json'}}],
    }


REPOS = {'flight': 'FlightComputer26', 'rf': 'RFBoard26', 'power': 'PowerBoard26', 'gateway': 'gateway-board26',
         'actuator': 'ActuatorBoard26', 'valve': 'ValveBoard26', 'daq': 'DAQ-Board'}
BASE_PROBES = ('panics', 'tlsf_failures', 'can_rx_frames', 'uart_tx_frames', 'uart_rx_frames', 'fdcan_tx_ok', 'fdcan_rx',
               'radio_tx_frames', 'radio_rx_frames')


def prepare(args):
    out = args.evidence.resolve()
    assert not out.exists(), 'Use a new evidence directory'
    out.mkdir(parents=True)
    config = out / 'config'
    config.mkdir()
    diagnostics = out / 'diagnostics'
    for sub in ('', 'temp', 'state', 'flight'):
        (diagnostics / sub).mkdir(mode=0o770, exist_ok=True)
        (diagnostics / sub).chmod(0o770)
    flight = diagnostics / 'flight'
    # Deterministic profile, stored with the evidence and read by Renode.
    subprocess.run([sys.executable, str(HERE / 'flight_profile.py'), '--out', str(flight)] + args.profile_arg, check=True,
                   stdout=(out / 'profile-generator.log').open('w'))
    profile = json.loads((flight / 'flight-profile.json').read_text())
    atomic(flight / 'radio-gate.txt', '0')
    atomic(diagnostics / 'pressure.raw', '0')
    atomic(diagnostics / 'loadcell.raw', mass_samples(0))
    atomic(diagnostics / 'gps.fix', '0')
    shutil.copyfile(Path(__file__), out / 'controller.py')
    key, users = fixture_user()
    (config / 'users.json').write_text(json.dumps(users))
    gse = {'nitrogen_target_psi': 50, 'pressure_step_psi': 50, 'pressure_ceiling_psi': 150, 'maximum_zero_offset_psi': 20,
           'dry_self_test_confirmed': False, 'grouped_panel': True, 'turbocharge_max_added_mass_kg': 0.3,
           'turbocharge_pressure_deadband_psi': 12}
    (config / 'gse.json').write_text(json.dumps(gse))
    (config / 'fill-targets.json').write_text(json.dumps({'version': 1, 'fill_source': 'kg50',
        'nitrogen': {'target_mass_kg': 10, 'target_pressure_psi': 50}, 'nitrous': {'target_mass_kg': 5, 'target_pressure_psi': 100}}))
    items = [{'id': n, 'text': n, 'checked': False, 'complete_on': k, 'completed_by': None, 'completed_at_ms': None}
             for n, k in [('self-test', 'valve_self_test'), ('nitrogen', 'nitrogen_test')]]
    (config / 'checklists.json').write_text(json.dumps({'revision': 0, 'enforce_before_launch': True, 'checklists': [
        {'id': 'checkout', 'title': 'Checkout', 'required_before_launch': True, 'items': items}]}))
    calibration = {'version': 1, 'ch1': {'m': 100, 'b': 0}, 'iadc': {'m': 1, 'b': 0}, 'kg50_input': 'amp2',
                   'extra_channels': {'kg50': {'linear': {'m': 100, 'b': 0}, 'zero_raw': None, 'points': [], 'fit': None}}}
    (config / 'calibration.json').write_text(json.dumps(calibration))
    for name in ('checklists', 'calibration', 'gse'):
        shutil.copyfile(config / f'{name}.json', diagnostics / 'state' / f'{name}.json')
        (diagnostics / 'state' / f'{name}.json').chmod(0o660)
    topo = topology(args.duration_ms, args.wall_freshness_scale)
    missing = {}
    for node in topo['nodes']:
        name = node['name']
        board = args.cohort / REPOS[name]
        layout = load_layout(board)
        symbols = nm_symbols(board, layout)
        layout['execution']['memory_probe_warmup_samples'] = 3
        # Keep only the small transport probe set the Release images retain.
        layout['execution']['memory_probes'] = [p for p in layout['execution'].get('memory_probes', [])
                                                if p['name'] in BASE_PROBES and p.get('symbol') in symbols]
        if 'hal_tick_address' in layout['execution'] and 'uwTick' in symbols:
            layout['execution']['hal_tick_address'] = symbols['uwTick']
        miss = missing.setdefault(name, [])
        if name == 'valve':
            sensor = next(p for p in layout['peripherals'] if p.get('bus') == 'adc3')
            sensor.update(channel_samples=[0], noise_lsb=0, stimulus_path='/diagnostics/pressure.raw')
        if name == 'daq':
            next(p for p in layout['peripherals'] if p.get('model') == 'mcp3564r')['stimulus_path'] = '/diagnostics/loadcell.raw'
        if name == 'rf':
            gps = next(p for p in layout['peripherals'] if p.get('model') == 'neo_m9n')
            gps['stimulus_path'] = '/diagnostics/gps.fix'
            gps['position'] = {'latitude': profile['launch_site'][0], 'longitude': profile['launch_site'][1], 'altitude_m': profile['pad_msl_m']}
            add_probes(layout, symbols, [('rf_gps_has_fix_word', 'g_neom9n_has_fix', 0)], miss)
        if name == 'actuator':
            # QD retraction progress: one step per TIM1 update interrupt (30 us nominal).
            add_probes(layout, symbols, [('ab_retract_steps_remaining', 'timer_steps_remaining', 0),
                                         ('ab_retract_active_word', 'g_plumbing_retract_active', 0),
                                         ('ab_local_ticks', '_tx_timer_system_clock', 0)], miss)
        if name == 'flight':
            add_probes(layout, symbols, [('fc_state', 'sm', 0), ('fc_config_flags', 'g_conf', 0),
                                         ('fc_deployment_pulses', 'g_deployment_pulse_completions', 0),
                                         ('fc_watchdog_resume_rejected', 'g_fc_watchdog_resume_rejected', 0),
                                         ('fc_watchdog_resumed', 'g_fc_watchdog_resumed', 0),
                                         ('fc_commands_received', 'g_network_flight_commands_received', 0),
                                         ('fc_last_command_id', 'g_last_network_flight_command_id', 0),
                                         # ThreadX stack-check / startup fault evidence (Core/Src/app_threadx.c)
                                         ('fc_startup_fault_stage', 'g_startup_fault_stage', 0),
                                         ('fc_stack_error_count', 'g_thread_stack_error_count', 0),
                                         ('fc_stack_error_thread', 'g_thread_stack_error_thread', 0),
                                         ('fc_stack_error_start', 'g_thread_stack_error_start', 0),
                                         ('fc_stack_error_end', 'g_thread_stack_error_end', 0),
                                         ('fc_hardfault_count', 'g_hardfault_count', 0),
                                         ('fc_commands_processed', 'g_network_flight_commands_processed', 0),
                                         # Per-thread stack low-water marks (bytes free / remaining).
                                         ('fc_tlmt_stack_free_min', 'g_telemetry_stack_free_min', 0),
                                         ('fc_eval_stack_free_min', 'g_evaluation_stack_free_min', 0),
                                         ('fc_dist_stack_free_min', 'g_distribution_stack_free_min', 0),
                                         ('fc_recv_stack_remaining', 'g_recovery_stack_remaining', 0)], miss)
        (config / f'{name}.json').write_text(json.dumps(layout))
    (config / 'comms.json').write_text(json.dumps({'version': 1,
        'av_bay': {'interface': 'serial', 'port': '/dev/ttyAMA0', 'baud_rate': 115200, 'protocol': 'raw_uart'},
        'fill_box': {'interface': 'pico_fi_tcp', 'mode': 'server', 'address': '0.0.0.0:0', 'discovery': True}}, indent=2))
    (config / 'topology.json').write_text(json.dumps(topo, indent=2))
    for name in ('gs-data', 'gs-backend-data'):
        (out / name).mkdir(mode=0o777)
        (out / name).chmod(0o777)
    if args.tiles and args.tiles.exists():
        shutil.copytree(args.tiles, out / 'gs-backend-data/maps/north_america/tiles')
        subprocess.run(['chmod', '-R', 'a+rwX', str(out / 'gs-backend-data')], check=True)
    pid = os.getpid()
    name = f'seds-flight-sim-{pid}'
    network = f'seds-flight-sim-net-{pid}'
    command = ['docker', 'run', '--rm', '--name', name, '--cidfile', str(out / 'container.cid'),
               '--cpus', str(args.cpus), '--memory', args.memory, '--memory-swap', args.memory, '--pids-limit', '512',
               '--ulimit', 'core=0', '--network', network, '--group-add', str(os.getgid()),
               '-e', 'TMPDIR=/diagnostics/temp', '-e', 'FIRMWARE_SIM_DIAGNOSTICS_DIR=/diagnostics',
               '-e', 'SEDS_FLIGHT_PROFILE=/diagnostics/flight/flight-profile.csv',
               '-e', 'SEDS_FLIGHT_LAUNCH=/diagnostics/flight/launch-anchor.txt',
               '-e', 'SEDS_UART_GATE=rf:usart1:/diagnostics/flight/radio-gate.txt',
               '-e', f'SEDS_GPS_UTC_OFFSET_MS={args.gps_utc_offset_ms}',
               '-v', f'{config}:/simulation:ro', '-v', f'{diagnostics}:/diagnostics',
               '-v', f'{out / "gs-data"}:/opt/groundstation/data', '-v', f'{out / "gs-backend-data"}:/opt/groundstation/backend/data']
    for node, repo in REPOS.items():
        command += ['-v', f'{(args.cohort / repo).resolve()}:/nodes/{node}:ro']
    command += [args.image, 'bay', '--topology', '/simulation/topology.json']
    (out / 'command.json').write_text(json.dumps(command, indent=2))
    return out, diagnostics, key, command, network, profile, missing


# ---------------------------------------------------------------- run
class Run:
    def __init__(self, args):
        self.args = args
        self.out, self.diag, self.key, self.command, self.network, self.profile, missing = prepare(args)
        self.flight = self.diag / 'flight'
        self.timeline_path = self.out / 'timeline.jsonl'
        self.status_path = self.out / 'status.json'
        self.probes = ProbeTail(self.diag)
        self.origin = self.token = None
        self.forwarder = None
        self.ui = None
        self.stage = 'boot'
        self.now = 0
        self.t0 = None
        self.fc_state = None
        self.fc_word = {}
        self.gs = {}
        self.cut = {'state': 'nominal', 'start_ms': None, 'end_ms': None}
        self.qd_checked_ms = -10**9
        self.qd_mode_set = False
        self.qd_query_wall = self.qd_policy_wall = self.qd_cycle_wall = self.qd_plumbing_wall = 0.0
        self.seen_notifications = set()
        self.seen_messages = set()
        self.result = {
            'phase': 'running', 'image': args.image, 'evidence': str(self.out), 'missing_optional_probes': missing,
            'profile': {'csv': str(self.flight / 'flight-profile.csv'), 'summary': self.profile},
            'expected_maxima': self.profile['expected_maxima'],
            'expected_sensed_accelerometer_max_mps2': min(self.profile['expected_maxima']['max_specific_force_mps2'], ACCEL_FULL_SCALE),
            'comms_loss_method': 'RF usart1 (RFD900x radio UART) <-> GS av_bay PTY cut in both directions by '
                                 'SedsStm32Uart SEDS_UART_GATE (file diagnostics/flight/radio-gate.txt); all MCUs keep running',
            'milestones': [], 'fc_states_seen': {}, 'gs_flight_states_seen': {}, 'mission_phases_seen': [],
            'deployment_messages': [], 'deployment_notifications': [], 'checks': {},
        }
        self.write_result()

    # ---- evidence helpers
    def write_result(self):
        atomic(self.out / 'result.json', json.dumps(self.result, indent=2, default=str) + '\n')

    def tplus_ms(self):
        return None if self.t0 is None else self.now - self.t0

    def status(self):
        atomic(self.status_path, json.dumps({
            'step': self.stage, 'virtual_ms': self.now, 'wall_ms': int(time.time() * 1000), 't_plus_ms': self.tplus_ms(),
            'fc_state': self.fc_state, 'gs_flight_state': self.gs.get('flightstate'), 'mission_phase': self.gs.get('mission_phase'),
            'comms': self.cut['state'], 'origin': self.origin, 'timeline': str(self.timeline_path),
            'done': self.stage in ('finished', 'failed')}, default=str) + '\n')

    def event(self, name, **extra):
        row = {'virtual_ms': self.now, 'wall_ms': int(time.time() * 1000), 't_plus_ms': self.tplus_ms(), 'event': name,
               'step': self.stage, 'fc_state': self.fc_state, 'fc_probe_sample': self.fc_word.get('sample'),
               'gs_flightstate': self.gs.get('flightstate_raw'), 'mission_phase': self.gs.get('mission_phase_raw'),
               'launch_clock': self.gs.get('launch_clock'), 'comms': self.cut['state'], **extra}
        with self.timeline_path.open('a') as handle:
            handle.write(json.dumps(row, default=str) + '\n')
        print(f"[{self.now/1000:9.1f}s T{'' if self.t0 is None else '%+.1f' % (self.tplus_ms()/1000)}] {name} fc={self.fc_state} "
              f"gs={self.gs.get('flightstate')} phase={self.gs.get('mission_phase')} {json.dumps(extra, default=str)[:300]}", flush=True)
        self.status()

    def milestone(self, name, **extra):
        self.result['milestones'].append({'virtual_ms': self.now, 't_plus_ms': self.tplus_ms(), 'name': name, **extra})
        self.write_result()
        self.event(name, **extra)

    def api(self, path, body=None):
        return request(self.origin, path, body, self.token)

    # ---- startup
    def connect(self, logs):
        text = '\n'.join(p.read_text(errors='replace')[:262144] for p in logs)
        if not re.search(r'HTTP listening on (?:0\.0\.0\.0|127\.0\.0\.1):3000', text):
            return False
        assert 'features testing=false hitl_mode=false test_fire_mode=false' in text, 'GS is not the ordinary flight build'
        info = json.loads(subprocess.check_output(['docker', 'inspect', self.command[self.command.index('--name') + 1]], text=True))[0]
        address = info['NetworkSettings']['Networks'][self.network]['IPAddress']
        assert ipaddress.ip_address(address).is_private
        self.forwarder = Forwarder(address)
        self.origin = self.forwarder.origin
        for _ in range(50):
            try:
                self.token = login(self.origin, self.key)
                break
            except (AssertionError, OSError):
                time.sleep(.2)
        assert self.token, 'login failed'
        code, page = request(self.origin, '/', None, None)
        self.result['ui'] = {'origin': self.origin, 'index_status': code, 'index_has_html': isinstance(page, str) and '<html' in page.lower()}
        tile = self.profile['launch_site']
        self.result['tiles'] = self.tile_probe(tile)
        self.write_result()
        self.milestone('gs_origin_ready', origin=self.origin)
        if self.args.ui_driver:
            shells = sorted(Path.home().glob('.cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell'))
            env = {'PLAYWRIGHT_MODULE': str(WP.parent / 'fill-ui-readiness-20261105/browser-tools/node_modules/playwright/index.mjs'),
                   **({'CHROME_BINARY': str(shells[-1])} if shells else {}),
                   'UI_TIMEOUT_S': str(int(self.args.timeout)), **os.environ, 'GS_ORIGIN': self.origin, 'GS_USER': USER, 'GS_PASSWORD': PASSWORD, 'EVIDENCE_DIR': str(self.out),
                   'STATUS_FILE': str(self.status_path), 'TIMELINE_FILE': str(self.timeline_path)}
            self.ui = subprocess.Popen(self.args.ui_driver, shell=True, env=env, stdout=(self.out / 'ui-driver.log').open('w'),
                                       stderr=subprocess.STDOUT, start_new_session=True)
            self.milestone('ui_driver_started', pid=self.ui.pid)
        return True

    def tile_probe(self, site):
        import math
        z = 14
        n = 2 ** z
        x = int((site[1] + 180) / 360 * n)
        y = int((1 - math.asinh(math.tan(math.radians(site[0]))) / math.pi) / 2 * n)
        req = urllib.request.Request(f'{self.origin}/tiles/{z}/{x}/{y}.jpg')
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                body = response.read()
                return {'url': f'/tiles/{z}/{x}/{y}.jpg', 'status': response.status, 'bytes': len(body),
                        'source': response.headers.get('x-gs26-tile-source'), 'jpeg': body[:2] == b'\xff\xd8'}
        except urllib.error.HTTPError as error:
            return {'url': f'/tiles/{z}/{x}/{y}.jpg', 'status': error.code}
        except OSError as error:
            return {'url': f'/tiles/{z}/{x}/{y}.jpg', 'error': str(error)}

    # ---- observation
    def observe_fc_probe(self):
        self.probes.poll()
        sample, row = self.probes.latest('flight')
        if not row or 'fc_state' not in row or sample == self.fc_word.get('sample'):
            return
        if row.get('fc_startup_fault_stage') and not self.result.get('fc_fault'):
            self.result['fc_fault'] = {'virtual_ms': self.now, 'probe_sample': sample, **{k: row.get(k) for k in row if k.startswith(('fc_startup', 'fc_stack'))}}
            self.event('fc_fault', fault=self.result['fc_fault'])
            self.write_result()
        low = self.result.setdefault('fc_stack_low_water', {})
        for key in ('fc_tlmt_stack_free_min', 'fc_eval_stack_free_min', 'fc_dist_stack_free_min', 'fc_recv_stack_remaining'):
            value = row.get(key)
            if value and (key not in low or value < low[key]['bytes']):
                low[key] = {'bytes': value, 'virtual_ms': self.now, 'probe_sample': sample, 'fc_state': self.fc_state}
        for key in ('fc_stack_error_count', 'fc_hardfault_count'):
            if row.get(key) and not self.result.get('fc_fault_counter_' + key):
                self.result['fc_fault_counter_' + key] = {'virtual_ms': self.now, 'probe_sample': sample, **row}
                self.event('fc_fault_counter', counter=key, row=row)
        word = row['fc_state']
        local = word & 0xff
        state = FC_LOCAL[local] if local < len(FC_LOCAL) else f'invalid:{local}'
        flags = row.get('fc_config_flags', 0)
        self.fc_word = {'sample': sample, 'word': word, 'flags': flags, 'pulses': row.get('fc_deployment_pulses'),
                        'global': FC_GLOBAL[(word >> 8) & 0xff] if ((word >> 8) & 0xff) < len(FC_GLOBAL) else (word >> 8) & 0xff}
        if state != self.fc_state:
            self.fc_state = state
            self.result['fc_states_seen'].setdefault(state, {'virtual_ms': self.now, 't_plus_ms': self.tplus_ms(), 'probe_sample': sample})
            self.event('fc_state', fc_probe=self.fc_word, launch_requested=bool(flags & OPT_LAUNCH_REQUESTED),
                       drogue_flag=bool(flags & OPT_DROGUE), main_flag=bool(flags & OPT_MAIN))
            self.write_result()

    def observe_actuator(self):
        sample, row = self.probes.latest('actuator')
        if not row or 'ab_retract_steps_remaining' not in row or sample == getattr(self, 'ab_sample', None):
            return
        self.ab_sample = sample
        if self.stage in ('retract', 'qd') and sample % 10 == 0:
            with (self.out / 'qd-retraction-probe.jsonl').open('a') as handle:
                handle.write(json.dumps({'virtual_ms': self.now, 'probe_sample': sample, **{k: row.get(k) for k in
                             ('ab_retract_steps_remaining', 'ab_retract_active_word', 'ab_local_ticks')}}) + '\n')

    def observe_gs(self, full=True):
        changed = []
        code, flightstate = self.api('/flightstate')
        if code == 200:
            name = flightstate if isinstance(flightstate, str) else (flightstate.get('state') or flightstate.get('flight_state') or flightstate.get('name')) if isinstance(flightstate, dict) else flightstate
            if isinstance(name, int) and name < len(FC_GLOBAL):
                name = FC_GLOBAL[name]
            if flightstate != self.gs.get('flightstate_raw'):
                self.gs['flightstate_raw'] = flightstate
                if name != self.gs.get('flightstate'):
                    self.gs['flightstate'] = name
                    self.result['gs_flight_states_seen'].setdefault(str(name), {'virtual_ms': self.now, 't_plus_ms': self.tplus_ms()})
                    changed.append(('gs_flight_state', {'value': flightstate}))
        code, phase = self.api('/api/mission_phase')
        phase_value = phase if code == 200 else {'http_status': code}
        if phase_value != self.gs.get('mission_phase_raw'):
            self.gs['mission_phase_raw'] = phase_value
            name = None
            if code == 200:
                name = phase if isinstance(phase, str) else (phase.get('phase') or phase.get('mission_phase') or phase.get('label')) if isinstance(phase, dict) else phase
            if name != self.gs.get('mission_phase'):
                self.gs['mission_phase'] = name
                self.result['mission_phases_seen'].append({'virtual_ms': self.now, 't_plus_ms': self.tplus_ms(), 'phase': name, 'fc_state': self.fc_state, 'comms': self.cut['state']})
                changed.append(('mission_phase', {'value': phase_value}))
        if self.t0 is not None or self.stage in ('qd', 'fire', 'flight'):
            code, clock = self.api('/api/launch_clock')
            if code == 200:
                self.gs['launch_clock'] = clock
        if full:
            code, notes = self.api('/api/notifications')
            if code == 200:
                for note in notes if isinstance(notes, list) else notes.get('notifications', []) if isinstance(notes, dict) else []:
                    ident = json.dumps(note, sort_keys=True, default=str)
                    if ident in self.seen_notifications:
                        continue
                    self.seen_notifications.add(ident)
                    text = json.dumps(note, default=str)
                    with (self.out / 'notifications.jsonl').open('a') as handle:
                        handle.write(json.dumps({'virtual_ms': self.now, 't_plus_ms': self.tplus_ms(), 'notification': note}, default=str) + '\n')
                    lowered = text.lower()
                    if any(k in lowered for k in ('drogue', 'main parachute', 'reef', 'deploy', 'parachute', 'landed', 'in flight', 'apogee', 'contact')):
                        self.result['deployment_notifications'].append({'virtual_ms': self.now, 't_plus_ms': self.tplus_ms(), 'notification': note})
                        changed.append(('gs_notification', {'notification': note}))
            if self.t0 is not None:
                code, messages = self.api('/api/messages')
                if code == 200:
                    for message in messages if isinstance(messages, list) else messages.get('messages', []) if isinstance(messages, dict) else []:
                        text = json.dumps(message, default=str)
                        if 'approx altitude' in text and text not in self.seen_messages:
                            self.seen_messages.add(text)
                            self.result['deployment_messages'].append({'virtual_ms': self.now, 't_plus_ms': self.tplus_ms(), 'message': message})
                            changed.append(('fc_deployment_message', {'message': message}))
        for name, extra in changed:
            self.event(name, **extra)
        if changed:
            self.write_result()

    # ---- comms loss
    def comms(self):
        t = self.tplus_ms()
        if t is None:
            return
        if self.cut['state'] == 'nominal' and t >= self.args.comms_loss_after_s * 1000:
            atomic(self.flight / 'radio-gate.txt', '1')
            self.cut.update(state='cut', start_ms=self.now)
            self.milestone('comms_cut', method='SEDS_UART_GATE rf:usart1 both directions', at_t_plus_ms=t)
        elif self.cut['state'] == 'cut' and self.now - self.cut['start_ms'] >= self.args.comms_loss_s * 1000:
            atomic(self.flight / 'radio-gate.txt', '0')
            self.cut.update(state='restored', end_ms=self.now)
            status = (self.flight / 'radio-gate.txt.status')
            self.milestone('comms_restored', cut_virtual_ms=self.now - self.cut['start_ms'],
                           gate_status=status.read_text().strip() if status.exists() else None)

    # ---- the ordinary GSE sequence (Startup -> Launch)
    def gse_step(self, status, nitro):
        phase = status['phase']
        stage = self.stage
        if status['qualification'].get('fault') or phase == 'fault':
            raise AssertionError('Procedure fault: ' + str(status.get('message')))
        if stage == 'boot' and self.args.qd_mode != 'stepper' and not self.qd_mode_set:
            # Operator drive selection (only allowed in Startup/Idle).
            if not (status['topology_stable'] and phase in ('idle', 'cancelled')):
                return
            if self.now - self.qd_checked_ms < 2000:
                return
            self.qd_checked_ms = self.now
            self.api('/api/actuator/plumbing/query', {})
            code, plumbing = self.api('/api/actuator/plumbing/status')
            board = (plumbing or {}).get('board') or {}
            if code != 200 or not board.get('fresh'):
                return
            if board.get('mode') == self.args.qd_mode:
                self.qd_mode_set = True
                self.milestone('qd_drive_mode_confirmed', mode=board.get('mode'), board=board)
                return
            body = {'mode': self.args.qd_mode, 'expected_generation': board.get('generation'), 'expected_epoch': board.get('epoch'),
                    'backend_instance': plumbing.get('backend_instance'), 'expected_revision': (plumbing.get('desired') or {}).get('revision'),
                    'expected_reset_epoch': plumbing.get('reset_epoch'),
                    # Stepper -> auxiliary must explicitly release the idle stepper holding torque.
                    'release_stepper': board.get('mode') == 'stepper' and self.args.qd_mode == 'auxiliary'}
            code, reply = self.api('/api/actuator/plumbing/settings', body)
            with (self.out / 'qd-mode-attempts.jsonl').open('a') as handle:
                handle.write(json.dumps({'virtual_ms': self.now, 'request': body, 'status': code, 'reply': reply}, default=str) + '\n')
            return
        if stage == 'boot':
            if status['topology_stable'] and status['valves'] == [False, True, True, False, False] and phase in ('idle', 'cancelled'):
                assert self.api('/api/command', 'StartFill')[0] == 403, 'Fill admitted before tests'
                assert self.api('/api/gse/self-test-confirmation', {'confirmed': True})[0] == 200
                assert self.api('/api/command', 'ValveSelfTest')[0] == 200
                self.stage = 'self-test'
                self.milestone('valve_self_test_sent')
        elif stage == 'self-test' and status['qualification']['self_test']:
            assert self.api('/api/command', 'NitrogenTest')[0] == 200
            nitro['n2'] = True
            self.stage = 'nitrogen'
            self.milestone('nitrogen_test_sent')
        elif stage == 'nitrogen':
            live = [c for c in (status.get('sequence_progress') or {}).get('controls', []) if c.get('hold_choice')]
            if live and phase == 'paused':
                choice = live[0]['hold_choice']
                if choice.get('dump', {}).get('allowed'):
                    code, reply = self.api('/api/gse/sequences/dump', live[0]['key'])
                    assert code == 200, ('nitrogen dump refused', reply)
                    self.stage = 'nitrogen-dumping'
                    self.milestone('nitrogen_hold_dump_sent', hold=choice)
            elif phase == 'passed' and status['qualification']['nitrogen_test']:
                self.stage = 'nitrogen-dumping'
        elif stage == 'nitrogen-dumping' and phase == 'passed' and status['qualification']['nitrogen_test']:
            assert self.api('/api/command', 'StartFill')[0] == 200
            nitro['n2o'] = True
            self.stage = 'fill'
            self.milestone('fill_started')
        elif stage == 'fill' and status['qualification']['fill_target']:
            assert status['mission_stage'] == 'armed', status['mission_stage']
            self.milestone('armed_fill_target', valves=status['valves'])
            doc = self.api('/api/checklists')[1]
            code, reply = self.api('/api/checklists/sign', {'checklist_id': 'checkout', 'revision': doc['revision']})
            self.milestone('checklist_signoff', status=code)
            code, _ = self.api('/api/command', 'RetractPlumbing')
            self.result['qd_before_gps_status'] = code
            atomic(self.diag / 'gps.fix', '1')
            self.stage = 'gps'
        elif stage == 'gps':
            code, antenna = self.api('/api/antenna/status')
            fix = antenna.get('rocket') if code == 200 and isinstance(antenna, dict) else None
            if fix and fix.get('source') == 'RF' and not fix.get('stale') and fix.get('altitude_m') is not None:
                self.milestone('rf_gps_lock', fix=fix)
                self.stage = 'retract'
        elif stage == 'retract':
            # RetractPlumbing can be refused while the actuator readback is
            # refreshing; refresh it and retry until the GS reports QD motion.
            if status.get('qd') not in (None, 'NotStarted'):
                self.stage = 'qd'
                self.milestone('qd_retract_dispatched', qd=status.get('qd'))
                return
            if self.now - self.qd_checked_ms < 2000:
                return
            self.qd_checked_ms = self.now
            self.api('/api/actuator/plumbing/query', {})
            code, reply = self.api('/api/command', 'RetractPlumbing')
            with (self.out / 'retract-attempts.jsonl').open('a') as handle:
                handle.write(json.dumps({'virtual_ms': self.now, 'status': code, 'response': reply, 'qd': status.get('qd')}, default=str) + '\n')
        elif stage == 'qd':
            # GS launch admission needs every required board's state proof
            # (board_abort.rs PROOF_TTL) and the actuator retraction proof
            # (actuator_plumbing FRESH_MS) younger than 5 s of WALL time, while
            # the bay runs ~10-20x slower than real time. Each 15 s wall cycle:
            # one board-state refresh, then watch /api/board_abort at 5 Hz;
            # query the actuator only once board proofs are clean, and press
            # Launch only when both gates hold together.
            wall = time.monotonic()
            if self.qd_cycle_wall == 0.0 or wall - self.qd_cycle_wall >= 15.0:
                self.qd_cycle_wall = wall
                self.qd_plumbing_wall = 0.0
                self.api('/api/board_abort/refresh', {})
            if wall - self.qd_policy_wall < 0.2 or wall - self.qd_cycle_wall > 12.0:
                return
            self.qd_policy_wall = wall
            code, snapshot = self.api('/api/board_abort')
            if code != 200 or not isinstance(snapshot, dict):
                return
            missing = snapshot.get('missing_required_state') or []
            retraction = snapshot.get('retraction') or {}
            record = {'virtual_ms': self.now, 'wall_ms': int(time.time() * 1000), 'missing': missing, 'retraction': retraction.get('phase')}
            if not missing and not retraction.get('allowed') and wall - self.qd_plumbing_wall >= 2.0:
                self.qd_plumbing_wall = wall
                self.api('/api/actuator/plumbing/query', {})
            if not missing and retraction.get('allowed'):
                code, reply = self.api('/api/command', 'Launch')
                record.update(status=code, response=reply)
                if code == 200:
                    self.stage = 'fire'
                    self.milestone('launch_sent', qd=status.get('qd'))
            if 'status' in record or self.now - self.qd_checked_ms >= 2000:
                self.qd_checked_ms = self.now
                with (self.out / 'launch-attempts.jsonl').open('a') as handle:
                    handle.write(json.dumps(record, default=str) + '\n')
        if self.stage in ('qd', 'fire'):
            clock = self.gs.get('launch_clock') or {}
            if clock.get('kind') == 't_plus':
                if self.stage == 'qd':
                    self.milestone('launch_observed_from_gs_clock', note='Launch was not sent by this controller')
                self.t0 = self.now
                atomic(self.flight / 'launch-anchor.txt', str(self.t0))
                self.stage = 'flight'
                self.milestone('t0_launch_anchor_written', launch_clock=clock, anchor_virtual_ms=self.t0)

    # ---- checks
    def evaluate(self):
        c = {}
        seen = self.result['fc_states_seen']
        gs_seen = {k.lower(): v for k, v in self.result['gs_flight_states_seen'].items()}
        for state in FLIGHT_STATES + ['Recovery']:
            c[f'fc_state_{state.lower()}'] = {'pass': state in seen or state.lower() in gs_seen,
                                               'fc_probe': seen.get(state), 'gs_flightstate': gs_seen.get(state.lower())}
        msgs = [json.dumps(m['message']) for m in self.result['deployment_messages']]
        c['fc_drogue_message'] = {'pass': any('PD approx altitude' in m for m in msgs)}
        c['fc_main_message'] = {'pass': any('PR approx altitude' in m for m in msgs)}
        pulses = self.fc_word.get('pulses')
        flags = self.fc_word.get('flags', 0)
        c['fc_drogue_pulse'] = {'pass': bool(flags & OPT_DROGUE), 'pulse_completions': pulses}
        c['fc_main_pulse'] = {'pass': bool(flags & OPT_MAIN), 'pulse_completions': pulses}
        notes = [json.dumps(n['notification']).lower() for n in self.result['deployment_notifications']]
        alt = re.compile(r'\d+(\.\d+)?\s*(m\b|ft\b|meters|metres|altitude)|altitude[^0-9]{0,24}\d')
        c['gs_notification_drogue_altitude'] = {'pass': any('drogue' in n and alt.search(n) for n in notes)}
        c['gs_notification_main_altitude'] = {'pass': any(('main' in n or 'reef' in n) and alt.search(n) for n in notes)}
        phases = self.result['mission_phases_seen']
        start, end = self.cut['start_ms'], self.cut['end_ms']
        landed = [p for p in phases if start and str(p['phase']).lower().replace(' ', '_') == 'landed' and p['virtual_ms'] >= start and (end is None or p['virtual_ms'] <= end + 5000)]
        reverted = [p for p in phases if end and p['virtual_ms'] >= end and 'flight' in str(p['phase']).lower()]
        c['comms_loss_window'] = {'pass': bool(start and end and end - start >= self.args.comms_loss_s * 1000 - 1000
                                               and self.t0 is not None and start - self.t0 >= self.args.comms_loss_after_s * 1000),
                                  'cut_t_plus_ms': None if start is None or self.t0 is None else start - self.t0,
                                  'restore_t_plus_ms': None if end is None or self.t0 is None else end - self.t0}
        c['gs_landed_during_comms_loss'] = {'pass': bool(landed), 'first': landed[0] if landed else None,
                                            'silence_ms_at_switch': landed[0]['virtual_ms'] - start if landed else None}
        restore_state = next((e for e in self.timeline_rows() if e['event'] == 'comms_restored'), None)
        c['gs_reverted_to_in_flight'] = {'pass': bool(reverted) and bool(landed), 'first': reverted[0] if reverted else None,
                                         'fc_state_at_restore': restore_state and restore_state.get('fc_state')}
        c['fc_in_descent_or_reefing_at_restore'] = {'pass': bool(restore_state) and restore_state.get('fc_state') in ('Descent', 'Reefing')}
        c['fc_no_stack_or_hard_fault'] = {'pass': not any(k.startswith('fc_fault_counter_') for k in self.result) and not self.result.get('fc_fault'),
                                          'low_water': self.result.get('fc_stack_low_water')}
        self.result['checks'] = c
        return c

    def timeline_rows(self):
        if not self.timeline_path.exists():
            return []
        return [json.loads(line) for line in self.timeline_path.read_text().splitlines() if line.strip()]

    # ---- main loop
    def run(self):
        args = self.args
        subprocess.run(['docker', 'network', 'create', '--internal', '--label', 'seds.task=flight-sim', self.network],
                       check=True, capture_output=True)
        log = (self.out / 'simulator.log').open('w')
        process = subprocess.Popen(self.command, stdout=log, stderr=subprocess.STDOUT)
        start = time.monotonic()
        previous = 0
        pressure = mass = 0.0
        nitro = {'n2': False, 'n2o': False}
        last_full = 0.0
        last_heartbeat = 0
        finished_at = None
        try:
            while process.poll() is None:
                if time.monotonic() - start > args.timeout:
                    raise TimeoutError('wall-clock deadline reached; not a pass')
                clocks = list((self.diag / 'temp').glob('*/virtual-clock.txt'))
                logs = list((self.diag / 'temp').glob('*/groundstation-*log'))
                if not clocks or not logs:
                    time.sleep(.2)
                    continue
                try:
                    self.now = int(clocks[0].read_text().strip())
                except ValueError:
                    continue
                if self.origin is None:
                    if not self.connect(logs):
                        time.sleep(.2)
                        continue
                if self.now == previous:
                    time.sleep(.05)
                    continue
                # Stimuli that must not depend on GS responsiveness come first.
                self.observe_fc_probe()
                self.observe_actuator()
                self.comms()
                try:
                    code, status = self.api('/api/gse/status')
                    if code != 200:
                        time.sleep(.2)
                        continue
                    delta = max(0, min(self.now - previous, 200))
                    previous = self.now
                    pressure = plant_pressure(pressure, delta, status['valves'])
                    atomic(self.diag / 'pressure.raw', str(raw_pressure(pressure)))
                    mass = plant_mass(mass, delta, status['valves'], nitro['n2o'], nitro['n2'])
                    atomic(self.diag / 'loadcell.raw', mass_samples(mass))
                    if status.get('phase') != self.gs.get('gse_phase'):
                        self.gs['gse_phase'] = status.get('phase')
                        self.event('gse_phase', gse_phase=status.get('phase'), mission_stage=status.get('mission_stage'), message=status.get('message'))
                    full = time.monotonic() - last_full > (1.0 if self.t0 is not None else 3.0)
                    if full:
                        last_full = time.monotonic()
                    self.observe_gs(full=full)
                    if self.stage != 'flight':
                        self.gse_step(status, nitro)
                    if self.now - last_heartbeat >= 10000:
                        last_heartbeat = self.now
                        self.status()
                        if self.t0 is not None:
                            code, maxima = self.api('/api/flight_maxima')
                            self.result['gs_flight_maxima_latest'] = {'virtual_ms': self.now, 'status': code, 'body': maxima}
                            self.write_result()
                except (OSError, http.client.HTTPException, ValueError) as error:
                    # The GS can be slow under full-bay load; a slow reply is not a failure.
                    self.result['api_errors'] = self.result.get('api_errors', 0) + 1
                    self.result['last_api_error'] = f'{type(error).__name__}: {error}'
                    time.sleep(.2)
                    continue
                done_state = args.stop_after
                if finished_at is None and done_state and self.fc_state and done_state in FC_LOCAL and self.fc_state in FC_LOCAL \
                        and FC_LOCAL.index(self.fc_state) >= FC_LOCAL.index(done_state) and (self.cut['state'] != 'cut'):
                    finished_at = self.now
                    self.milestone('stop_condition_reached', stop_after=done_state)
                if finished_at is not None and self.now - finished_at >= args.linger_s * 1000:
                    break
                time.sleep(.05)
            self.result['simulator_exit'] = process.poll()
        except BaseException as error:
            self.result['error'] = f'{type(error).__name__}: {error}'
            raise
        finally:
            try:
                if self.origin and self.token:
                    for path, name in (('/api/flight_maxima', 'gs-flight-maxima.json'), ('/api/notifications', 'gs-notifications.json'),
                                       ('/api/messages', 'gs-messages.json'), ('/api/mission_phase', 'gs-mission-phase.json'),
                                       ('/flightstate', 'gs-flightstate.json'), ('/api/launch_clock', 'gs-launch-clock.json')):
                        try:
                            code, body = self.api(path)
                            (self.out / name).write_text(json.dumps({'virtual_ms': self.now, 'status': code, 'body': body}, indent=2, default=str) + '\n')
                        except OSError:
                            pass
            finally:
                if process.poll() is None:
                    subprocess.run(['docker', 'stop', '--time', '10', self.command[self.command.index('--name') + 1]], capture_output=True)
                    process.wait(timeout=60)
                log.close()
                # Tell the UI driver the run is over before giving it its grace period.
                self.stage = 'finished'
                self.status()
                if self.ui is not None:
                    try:
                        self.ui.wait(timeout=args.ui_grace_s)
                    except subprocess.TimeoutExpired:
                        os.killpg(self.ui.pid, signal.SIGTERM)
                        self.ui.wait(timeout=30)
                    self.result['ui_driver_exit'] = self.ui.returncode
                if self.forwarder:
                    self.forwarder.close()
                subprocess.run(['docker', 'network', 'rm', self.network], capture_output=True)
                for p in list((self.diag / 'temp').glob('*/groundstation-*log')):
                    shutil.copyfile(p, self.out / ('gs-' + p.name))
                checks = self.evaluate()
                required = [k for k in checks if not k.startswith('fc_state_recovery')]
                self.result['passed'] = [k for k in required if checks[k]['pass']]
                self.result['failed'] = [k for k in required if not checks[k]['pass']]
                if 'error' not in self.result:
                    self.result['phase'] = 'passed' if not self.result['failed'] else 'completed-with-failures'
                else:
                    self.result['phase'] = 'failed'
                self.result['wall_seconds'] = time.monotonic() - start
                self.write_result()
                self.stage = 'finished'
                self.status()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--evidence', type=Path, default=None, help='NEW evidence directory (default: evidence/flight-sim-<time>)')
    p.add_argument('--cohort', type=Path, default=BUILD / 'cohort')
    p.add_argument('--image', default=None, help='image id (default: flight-sim-build/IMAGE-ID)')
    p.add_argument('--tiles', type=Path, default=BUILD / 'tiles/north_america/tiles')
    p.add_argument('--duration-ms', type=int, default=2_400_000, help='topology cap; the real AB QD retraction alone takes ~600 s virtual')
    p.add_argument('--timeout', type=int, default=8 * 3600, help='wall-clock seconds')
    p.add_argument('--cpus', default='3')
    p.add_argument('--memory', default='16g')
    p.add_argument('--comms-loss-after-s', type=float, default=302.0)
    p.add_argument('--comms-loss-s', type=float, default=170.0)
    p.add_argument('--stop-after', default='Recovery', choices=FC_LOCAL, help='end once the FC reaches this state')
    p.add_argument('--linger-s', type=float, default=20.0, help='virtual seconds to keep running after the stop state')
    p.add_argument('--ui-driver', default=None, help='shell command started when the GS origin is known')
    p.add_argument('--ui-grace-s', type=float, default=120.0)
    p.add_argument('--profile-arg', action='append', default=[], help='extra flight_profile.py argument (repeatable)')
    p.add_argument('--qd-mode', choices=['stepper', 'auxiliary'], default='stepper',
                   help='actuator QD drive selected in Idle; auxiliary avoids the ~1500 s modelled stepper retraction')
    p.add_argument('--gps-utc-offset-ms', type=int, default=-5000,
                   help='NEO-M9N NMEA UTC offset; runs 3/4 measured RF GPS publications ~+4.95 s ahead of GS network UTC')
    p.add_argument('--wall-freshness-scale', type=int, default=30,
                   help='GS_SIM_WALL_FRESHNESS_SCALE for the firmware_sim GS (1 = operational windows)')
    p.add_argument('--prepare-only', action='store_true')
    args = p.parse_args()
    if args.evidence is None:
        args.evidence = WP / 'evidence' / time.strftime('flight-sim-%Y%m%d-%H%M%S')
    if args.image is None:
        args.image = (BUILD / 'IMAGE-ID').read_text().strip()
    assert args.duration_ms % 1000 == 0
    if args.prepare_only:
        prepare(args)
        return
    run = Run(args)
    run.run()
    print(json.dumps({'phase': run.result['phase'], 'failed': run.result.get('failed'), 'evidence': str(run.out)}, indent=2))
    sys.exit(0 if run.result['phase'] == 'passed' else 1)


if __name__ == '__main__':
    main()
