#!/usr/bin/env python3
"""RF + Power + Flight Computer + GroundStation linked run for the OTA storage fault.

usage: avbay.py <evidence-dir> <rf-root>:<subdir> <pb-root>:<subdir> [--virtual-ms N]
"""
import argparse, base64, hashlib, hmac, json, os, re, shutil, subprocess, sys, threading, time, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ('panics', 'tlsf_failures', 'can_rx_frames', 'fdcan_tx_ok', 'fdcan_rx', 'radio_tx_frames', 'radio_rx_frames')
EXTRA = {'rf': [('service_faults', 'g_telemetry_service_faults', 0), ('persistent_store_errors', 'g_persistent_store_errors', None),
                ('telemetry_loop_completions', 'g_telemetry_loop_completions', None)],
         'power': [('service_faults', 'g_telemetry_service_faults', 0), ('persistent_store_errors', 'g_persistent_store_errors', None),
                   ('watchdog_missing', 'g_watchdog_missing_mask', None)]}


USER, PASSWORD = 'sequence-fixture', 'fixture-only-password'


def fixture_user():
    salt = b'isolated-gse-model'
    key = hashlib.pbkdf2_hmac('sha256', PASSWORD.encode(), salt, 120000)
    return key, {'version': 1, 'anonymous': {'view_data': False, 'send_commands': False}, 'users': [
        {'username': USER, 'roles': ['admin'], 'password': {'algorithm': 'pbkdf2_sha256', 'iterations': 120000,
         'salt_b64': base64.b64encode(salt).decode(), 'hash_b64': base64.b64encode(key).decode()},
         'permissions': {'view_data': True, 'send_commands': True}, 'disabled': False}]}


def request(origin, path, body=None, token=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(origin + path, None if body is None else json.dumps(body).encode(), headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
            return r.status, json.loads(data) if data[:1] in (b'{', b'[') else data.decode(errors='replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read(2000).decode(errors='replace')


def start_recording(name, key, log):
    """Log in to the GroundStation and start its packet recording database."""
    deadline = time.time() + 1800
    while time.time() < deadline:
        try:
            info = json.loads(subprocess.check_output(['docker', 'inspect', name], text=True, stderr=subprocess.DEVNULL))[0]
            ip = next(iter(info['NetworkSettings']['Networks'].values()))['IPAddress']
            origin = f'http://{ip}:3000'
            code, c = request(origin, '/api/auth/challenge', {'username': USER})
            if code != 200:
                raise RuntimeError(code)
            nonce = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip('=')
            message = {'username': USER, 'challenge_id': c['challenge_id'], 'server_nonce_b64': c['server_nonce_b64'],
                       'client_nonce_b64': nonce, 'remember_me': False}
            proof = base64.urlsafe_b64encode(hmac.new(key, json.dumps(message, separators=(',', ':')).encode(),
                                                      hashlib.sha256).digest()).decode().rstrip('=')
            code, result = request(origin, '/api/auth/login', {**message, 'proof_b64': proof})
            token = result['token']
            code, reply = request(origin, '/api/command', 'StartWritingNow', token)
            log.write(f'{time.time():.0f} recording start {code} {reply}\n'); log.flush()
            if code == 200:
                return
        except Exception as error:  # GS not up yet
            log.write(f'{time.time():.0f} waiting {error!r}\n'); log.flush()
        time.sleep(5)


def symbols(elf):
    out = subprocess.check_output(['arm-none-eabi-nm', '-n', str(elf)], text=True)
    return {p[-1]: int(p[0], 16) for line in out.splitlines() if len(p := line.split()) >= 3}


def layout_for(name, root, subdir):
    layout = json.loads((root / 'sim/board.json').read_text())
    for key, value in layout['artifacts'].items():
        parts = Path(value).parts
        layout['artifacts'][key] = str(Path('build', subdir, *parts[2:]))
    layout['artifacts'].pop('ota', None)
    syms = symbols(root / layout['artifacts']['elf'])
    probes = [p for p in layout['execution'].get('memory_probes', []) if p['name'] in BASE and p.get('symbol') in syms]
    for probe_name, symbol, maximum in EXTRA.get(name, []):
        if symbol in syms:
            probe = {'name': probe_name, 'symbol': symbol}
            if maximum is not None:
                probe['maximum'] = maximum
            probes.append(probe)
    layout['execution']['memory_probes'] = probes
    layout['execution']['memory_probe_warmup_samples'] = 2
    layout['execution']['require_stack_probe'] = False
    if 'hal_tick_address' in layout['execution'] and 'uwTick' in syms:
        layout['execution']['hal_tick_address'] = syms['uwTick']
    return layout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('evidence', type=Path)
    ap.add_argument('--rf', required=True, help='RFBoard26 checkout:build subdirectory')
    ap.add_argument('--pb', required=True, help='PowerBoard26 checkout:build subdirectory')
    ap.add_argument('--flight', required=True, help='FlightComputer26 checkout:build subdirectory')
    ap.add_argument('--image', required=True, help='simulator image that contains groundstation_backend')
    ap.add_argument('--simulator-root', type=Path,
                    help='FirmwareSimulator checkout whose target/release/firmware-sim and renode/ replace the image copies')
    ap.add_argument('--virtual-ms', type=int, default=75000)
    ap.add_argument('--samples', type=int, default=15)
    ap.add_argument('--no-dfu', action='store_true', help='blank part instead of a DFU factory flash')
    args = ap.parse_args()
    out = args.evidence.resolve(); out.mkdir(parents=True)
    config = out / 'config'; config.mkdir()
    diagnostics = out / 'diagnostics'
    for sub in ('', 'temp', 'state'):
        (diagnostics / sub).mkdir(mode=0o777, exist_ok=True); (diagnostics / sub).chmod(0o777)
    roots = {}
    for name, spec in (('rf', args.rf), ('power', args.pb), ('flight', args.flight)):
        root, subdir = spec.rsplit(':', 1)
        roots[name] = (Path(root).resolve(), subdir)
    for name, (root, subdir) in roots.items():
        layout = layout_for(name, root, subdir)
        if args.no_dfu:
            layout.pop('dfu_factory_flash', None)
        (config / f'{name}.json').write_text(json.dumps(layout, indent=1))
    (config / 'comms.json').write_text(json.dumps({'version': 1,
        'av_bay': {'interface': 'serial', 'port': '/dev/ttyAMA0', 'baud_rate': 115200, 'protocol': 'raw_uart'},
        'fill_box': {'interface': 'pico_fi_tcp', 'mode': 'server', 'address': '0.0.0.0:0', 'discovery': True}}))
    key, users = fixture_user()
    (config / 'users.json').write_text(json.dumps(users))
    n = args.samples
    assertions = [
        {'name': 'RF reports no failed router service', 'node': 'rf', 'probe': 'service_faults', 'maximum': 0},
        {'name': 'Power Board reports no failed router service', 'node': 'power', 'probe': 'service_faults', 'maximum': 0},
    ]
    # Every 5 s window after startup: the RF keeps receiving CAN and keeps transmitting radio.
    for i in range(2, n - 1):
        assertions.append({'name': f'RF radio egress continued in window {i}', 'node': 'rf', 'probe': 'radio_tx_frames',
                           'minimum_gain': 1, 'from_sample': i, 'to_sample': i + 1})
        assertions.append({'name': f'RF CAN ingress continued in window {i}', 'node': 'rf', 'probe': 'fdcan_rx',
                           'minimum_gain': 1, 'from_sample': i, 'to_sample': i + 1})
    topology = {
        'name': 'av-bay-ota-storage', 'quantum_seconds': 0.0001, 'virtual_time_ms': args.virtual_ms, 'sample_count': n,
        'enforce_end_drop': False, 'assertions': assertions,
        'host_log_assertions': [
            {'name': 'GroundStation discovered RF, PB and FC', 'node': 'groundstation', 'contains': 'FC,PB,RF'},
        ],
        'nodes': [{'name': k, 'layout': f'/simulation/{k}.json', 'firmware_root': f'/nodes/{k}'} for k in ('rf', 'power', 'flight')],
        'links': [
            {'name': 'avionics_can', 'kind': 'can', 'endpoints': [
                {'node': 'rf', 'peripheral': 'fdcan2', 'tx_probe': 'fdcan_tx_ok', 'rx_probe': 'fdcan_rx'},
                {'node': 'power', 'peripheral': 'fdcan2', 'tx_probe': 'fdcan_tx_ok', 'rx_probe': 'fdcan_rx'},
                {'node': 'flight', 'peripheral': 'fdcan1', 'tx_probe': 'fdcan_tx_ok', 'rx_probe': 'fdcan_rx'}]},
            {'name': 'rocket_radio', 'kind': 'radio', 'endpoints': [
                {'node': 'rf', 'peripheral': 'usart1', 'tx_probe': 'radio_tx_frames', 'rx_probe': 'radio_rx_frames'},
                {'node': 'groundstation', 'peripheral': 'av_bay'}]},
        ],
        'host_nodes': [{
            'name': 'groundstation', 'binary': '/usr/local/bin/groundstation_backend', 'cwd': '/opt/groundstation',
            'serial_links': [{'link': 'rocket_radio', 'env': 'GS_AV_BAY_SERIAL_PORT'}],
            'env': {'RUST_LOG': 'info', 'GS_DEBUG_PRINTS': '0', 'GS_SIMULATED_SERIAL_PTY': '1', 'GS_BIND_ADDRESS': '0.0.0.0:3000',
                    'GS_HTTPS_BIND_ADDRESS': 'off', 'GS_VIDEO_MANAGED': '0', 'GS_VIDEO_DISCOVERY': '0',
                    'GS_STATION_DISCOVERY_BIND': '127.0.0.1:0', 'GS_VIDEO_DISCOVERY_BIND': '127.0.0.1:0', 'GS_SIM_MODE': '0',
                    'GS_COMMS_LINK_CONFIG': '/simulation/comms.json', 'GS_LAYOUT_PATH': '/opt/groundstation/backend/layout/layout.json',
                    'GS_HEARTBEAT_PERIOD_MS': '500', 'GS_SIM_NETWORK_EPOCH_MS': '1767225600000', 'GS_SIM_WALL_FRESHNESS_SCALE': '30',
                    'GS_SIM_EXPECT_DISCOVERY_NODES': 'FC,PB,RF', 'GS_SIM_VALIDATE_TELEMETRY_RETURN': '1',
                    'GS_USERS_FILE': '/simulation/users.json'}}],
    }
    (config / 'topology.json').write_text(json.dumps(topology, indent=1))
    for name in ('gs-data', 'gs-backend-data'):
        (out / name).mkdir(mode=0o777); (out / name).chmod(0o777)
    name = f'seds-avbay-{os.getpid()}'
    network = name + '-net'
    subprocess.run(['docker', 'network', 'create', '--internal', network], check=True, capture_output=True)
    command = ['docker', 'run', '--rm', '--name', name, '--cpus', '4', '--memory', '12g', '--pids-limit', '512', '--network', network,
               '--group-add', str(os.getgid()), '-e', 'TMPDIR=/diagnostics/temp', '-e', 'FIRMWARE_SIM_DIAGNOSTICS_DIR=/diagnostics',
               '-v', f'{config}:/simulation:ro', '-v', f'{diagnostics}:/diagnostics',
               '-v', f'{out / "gs-data"}:/opt/groundstation/data', '-v', f'{out / "gs-backend-data"}:/opt/groundstation/backend/data']
    if args.simulator_root:
        sim = args.simulator_root.resolve()
        command += ['-v', f'{sim / "target/release/firmware-sim"}:/usr/local/bin/firmware-sim:ro',
                    '-v', f'{sim / "renode"}:/opt/firmware-sim/renode:ro']
    for node, (root, _) in roots.items():
        command += ['-v', f'{root}:/nodes/{node}:ro']
    command += [args.image, 'bay', '--topology', '/simulation/topology.json']
    (out / 'command.json').write_text(json.dumps(command, indent=1))
    recorder = threading.Thread(target=start_recording, args=(name, key, (out / 'recording.log').open('w')), daemon=True)
    recorder.start()
    try:
        with (out / 'simulator.log').open('wb') as log:
            rc = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT).returncode
    finally:
        subprocess.run(['docker', 'network', 'rm', network], capture_output=True)
    print(f'bay rc={rc}')
    subprocess.run(['docker', 'run', '--rm', '-v', f'{out}:/e', 'debian:bookworm-slim', 'chmod', '-R', 'a+rX', '/e'], check=False)
    boards = {'RFBoard': 'RF', 'PowerBoard': 'PB', 'FlightComputer': 'FC'}
    aliases = sorted({f'{a}={boards[b]}' for a, b in re.findall(r'sender=(@addr:\d+) resolved=Some\((\w+)\)',
                      (out / 'simulator.log').read_text(errors='replace')) if b in boards})
    check = subprocess.run([sys.executable, str(ROOT / 'scripts/check-av-bay-flow.py'), str(out / 'gs-data')]
                           + [f'--alias={a}' for a in aliases],
                           capture_output=True, text=True)
    (out / 'av-bay-flow.txt').write_text(check.stdout + check.stderr)
    print(check.stdout + check.stderr)
    return 1 if rc or check.returncode else 0


if __name__ == '__main__':
    sys.exit(main())
