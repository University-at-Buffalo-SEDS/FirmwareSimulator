#!/usr/bin/env python3
"""Add a deterministic direct-radio outage to the unchanged startup/control gates.

Only the two directed GS/RF edges are disabled. No firmware, hardware modem,
physical output or unrelated service is accessed. All commands run in Renode.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
DURATION_MS = 60000
LOSS_MS = 20000
RESTORE_MS = 40000
TRACE_UNTIL_MS = 56000
CONTROL_SAMPLES = [2, 3, 6, 7, 10, 11]
OBSERVER_TOLERANCE_MS = 250


def clock_stream_script():
    # Renode updates its tiny clock file without a newline. Explicit framing
    # is mandatory; require one actual file rather than silently mixing clocks.
    return r'''while :; do
radio_clock_count=0
for radio_clock_file in /tmp/.tmp*/virtual-clock.txt /tmp/*/virtual-clock.txt; do
    if [ -f "$radio_clock_file" ]; then
        radio_clock_count=$((radio_clock_count + 1))
        radio_selected_clock="$radio_clock_file"
    fi
done
if [ "$radio_clock_count" -gt 1 ]; then
    echo "ambiguous simulator clock files" >&2
    exit 2
fi
if [ "$radio_clock_count" -eq 1 ]; then
    cat "$radio_selected_clock"
    printf '\n'
fi
sleep 0.05
done'''


def configure_fallback(topology, ports, heartbeat_period_ms=None):
    if topology.get('reboots') or topology.get('can_ack_events') or topology.get('can_link_events') or topology.get('radio_link_events'):
        raise ValueError('fallback scenario must isolate its radio outage from other faults')
    if topology['sample_count'] < 6 or topology['virtual_time_ms'] < 16000:
        raise ValueError('fallback scenario requires the complete startup sampling baseline')
    topology['sample_count'] = 12
    topology['virtual_time_ms'] = DURATION_MS
    hosts = {host['name']: host for host in topology['host_nodes']}
    if set(hosts) != {'groundstation', 'rrls'}:
        raise ValueError('fallback scenario requires the primary and its single paired RRLS')
    for name, port in zip(('groundstation', 'rrls'), ports):
        hosts[name]['env']['GS_BIND_ADDRESS'] = f'127.0.0.1:{port}'
    if heartbeat_period_ms is not None:
        if not 100 <= heartbeat_period_ms <= 60000:
            raise ValueError('heartbeat override must be100..60000milliseconds')
        for host in hosts.values():
            host['env']['GS_HEARTBEAT_INTERVAL_MS'] = str(heartbeat_period_ms)
    primary = hosts['groundstation']['env']
    if primary.get('GS_SIM_MANAGED_VARIABLE_MAX_LATENCY_MS') != '500':
        raise ValueError('the existing 500ms managed-variable gate must be retained')
    primary.update(GS_SIM_VALIDATE_SOAK_COMMANDS='1', GS_SIM_VALIDATE_SOAK_AV_BAY='1',
                   GS_SIM_SOAK_COMMAND_SAMPLES=','.join(map(str, CONTROL_SAMPLES)))
    topology['radio_link_events'] = [dict(link='rocket_radio', sender=sender, receiver=receiver,
        after_sample=sample, connected=connected) for sample, connected in [(4, False), (8, True)]
        for sender, receiver in [('groundstation', 'rf'), ('rf', 'groundstation')]]
    topology['enforce_end_drop'] = True
    # Keep every existing startup, sensor, CAN, persistence and memory assertion.
    checks = topology.setdefault('host_log_assertions', [])
    checks.append(dict(name='Every direct/fallback/restored RF control round returned all receipts',
        node='groundstation', contains='full-bay RF soak control acknowledged:',
        minimum_occurrences=len(CONTROL_SAMPLES), maximum_occurrences=len(CONTROL_SAMPLES)))
    for word in ['missed latency deadline', 'exceeded latency bound', 'failed to queue']:
        checks.append(dict(name='No control failure: ' + word, node='groundstation', contains=word,
                           minimum_occurrences=0, maximum_occurrences=0))
    return topology


def validate_status(rows):
    valid = [row for row in rows if isinstance(row.get('virtual_ms'), int)
             and isinstance(row.get('primary'), dict) and isinstance(row.get('rrls'), dict)]
    if not valid:
        raise ValueError('no complete paired status observations')
    for row in valid:
        if row['primary'].get('role') != 'primary' or row['rrls'].get('role') != 'rrls':
            raise ValueError('status identities do not match the paired stations')
    def window(start, end):
        found = [row for row in valid if start <= row['virtual_ms'] <= end]
        if len(found) < 3:
            raise ValueError(f'insufficient phase coverage {start}..{end}')
        return found
    for row in window(10000, 19000) + window(45000, 55000):
        if row['primary']['relay_requested'] or row['primary']['forwarding'] or row['rrls']['forwarding']:
            raise ValueError('healthy direct link must keep the relay quiescent')
    requested = next((row for row in valid if row['virtual_ms'] >= LOSS_MS and row['primary']['relay_requested']), None)
    if requested is None or not 3000 <= requested['primary']['direct_age_ms'] <= 3000 + OBSERVER_TOLERANCE_MS:
        raise ValueError('fallback did not respect the three-second direct-loss threshold')
    forwarding = next((row for row in valid if row['virtual_ms'] >= requested['virtual_ms']
                       and row['primary']['forwarding'] and row['rrls']['forwarding']), None)
    if forwarding is None or forwarding['virtual_ms'] - requested['virtual_ms'] > 2000:
        raise ValueError('live relay did not establish forwarding within its two-second proof lease')
    for row in window(26000, 39000):
        if not row['primary']['relay_requested'] or not row['primary']['forwarding'] or not row['rrls']['forwarding']:
            raise ValueError('relay must sustain the real direct-link outage')
        if row['primary']['direct_age_ms'] < 3000:
            raise ValueError('relayed data falsely renewed the direct link')
    fresh = next((row for row in valid if row['virtual_ms'] >= RESTORE_MS
                  and row['primary'].get('direct_age_ms') is not None
                  and row['primary']['direct_age_ms'] <= OBSERVER_TOLERANCE_MS), None)
    cleared = next((row for row in valid if row['virtual_ms'] >= RESTORE_MS and not row['primary']['relay_requested']), None)
    if fresh is None or cleared is None:
        raise ValueError('restored direct link did not recover')
    # The first status read can follow several raw arrivals. Bound that sampling
    # uncertainty explicitly; the separate command receipt limit remains500ms.
    if not RESTORE_MS + 2000 <= cleared['virtual_ms'] <= fresh['virtual_ms'] + 2000 + OBSERVER_TOLERANCE_MS:
        raise ValueError('direct restoration did not respect the two-second stable-link hysteresis')
    return dict(result='PASS', observations=len(valid), loss_requested_ms=requested['virtual_ms'],
                forwarding_ms=forwarding['virtual_ms'], restored_first_observation_ms=fresh['virtual_ms'],
                direct_restored_ms=cleared['virtual_ms'], observer_tolerance_ms=OBSERVER_TOLERANCE_MS,
                command_ack_limit_ms=500)


def validate_wire(report, heartbeat_period_ms=500):
    streams = {(s['station'], s['direction']): s for s in report['streams']}
    def packets(station, direction, start, end):
        return [p for p in streams[station, direction]['packets'] if start * 1000 <= p['at_us'] <= end * 1000]
    for start, end, kind in [(10000, 19000, 5), (26000, 39000, 2), (45000, 56000, 5)]:
        data = packets(1, 0, start, end)
        if not data or any(p.get('relay_kind') != kind for p in data):
            raise ValueError(f'primary wire envelopes do not match phase {start}..{end}')
    for start, end in [(10000, 19000), (45000, 56000)]:
        if packets(2, 0, start, end):
            raise ValueError('standby RRLS forwarded application packets on a healthy direct link')
    # The SDK supplies its actual source-address mapping: no guessed hashes.
    gs_address = report['known_source_addresses']['GS']
    heartbeats = [p for p in streams[1, 0]['packets']
                  if p['type'] == 'HEARTBEAT' and p['source_address'] == gs_address]
    maximum = TRACE_UNTIL_MS // heartbeat_period_ms + 2  # startup boundary and one in-flight frame.
    if not heartbeats:
        raise ValueError('no actual GS-source heartbeat frames to validate cadence')
    if len(heartbeats) > maximum:
        raise ValueError(f'GS heartbeat production follows wall time: {len(heartbeats)} > {maximum}')
    kinds = {p.get('relay_kind') for p in packets(2, 0, 26000, 39000)}
    if not {1, 5} <= kinds:
        raise ValueError('RRLS did not carry both return data and avionics uplink during the outage')
    return dict(result='PASS', direct_kind=5, fallback_kind=2, rrls_downlink_kind=1,
                gs_heartbeats=len(heartbeats), heartbeat_maximum=maximum,
                configured_heartbeat_ms=heartbeat_period_ms)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--radio-profile', type=Path, required=True)
    parser.add_argument('--evidence-dir', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=1800)
    parser.add_argument('--heartbeat-period-ms', type=int, help='Optional100..60000ms cadence override on both hosted stations; omitted preserves the inherited profile')
    args = parser.parse_args()
    if args.heartbeat_period_ms is not None and not 100 <= args.heartbeat_period_ms <= 60000:
        parser.error('--heartbeat-period-ms must be100..60000')
    evidence = args.evidence_dir.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    reserved = [socket.socket() for _ in range(2)]
    for sock in reserved:
        sock.bind(('127.0.0.1', 0))
    ports = [sock.getsockname()[1] for sock in reserved]
    import radio_profile
    original_configure = radio_profile.configure_radio_profile
    scenario_config = {}
    def configure(directory, topology, profile, with_rrls=False):
        original_configure(directory, topology, profile, with_rrls)
        configure_fallback(topology, ports, args.heartbeat_period_ms)
        primary = next(host for host in topology['host_nodes'] if host['name'] == 'groundstation')
        scenario_config['heartbeat_period_ms'] = int(primary['env'].get('GS_HEARTBEAT_INTERVAL_MS', '500'))
    radio_profile.configure_radio_profile = configure
    original_popen = subprocess.Popen
    observers = []
    observer_results = []
    def observe(container, process):
        log = evidence / 'status-observer.log'
        with log.open('wb') as output:
            for _ in range(20):
                clock = original_popen(['docker', 'exec', container, '/bin/sh', '-c',
                    clock_stream_script()],
                    stdout=subprocess.PIPE, stderr=output)
                command = [sys.executable, str(ROOT / 'scripts/radio-fallback-observer.py'), '--clock-stdin',
                           '--primary-port', str(ports[0]), '--rrls-port', str(ports[1]),
                           '--output', str(evidence / 'rrls-status.jsonl'), '--until-ms', str(TRACE_UNTIL_MS),
                           '--timeout', str(args.timeout)]
                # Retry only container creation, before starting the bounded
                # observer. All subsequent observer errors fail the scenario.
                time.sleep(0.1)
                if clock.poll() is None:
                    child = original_popen(command, stdin=clock.stdout, stdout=output, stderr=output)
                    result = child.wait()
                    clock.terminate()
                    clock.wait(timeout=5)
                    observer_results.append(result)
                    return
                clock.wait()
                if process.poll() is not None:
                    observer_results.append(1)
                    return
                time.sleep(0.1)
            observer_results.append(1)
    def popen(command, *positional, **kwargs):
        if isinstance(command, list) and '--name' in command and 'bay' in command:
            for sock in reserved:
                sock.close()
            process = original_popen(command, *positional, **kwargs)
            container = command[command.index('--name') + 1]
            thread = threading.Thread(target=observe, args=(container, process), daemon=True)
            observers.append(thread)
            thread.start()
            return process
        return original_popen(command, *positional, **kwargs)
    subprocess.Popen = popen
    specification = importlib.util.spec_from_file_location('fallback_base_controller', ROOT / 'scripts/test-route-recovery.py')
    controller = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(controller)
    sys.argv = [str(ROOT / 'scripts/test-route-recovery.py'), '--workspace', str(args.workspace.resolve()),
                '--image', args.image, '--radio-profile', str(args.radio_profile.resolve()), '--radio-rrls',
                '--radio-trace-until-ms', str(TRACE_UNTIL_MS), '--evidence-dir', str(evidence), '--timeout', str(args.timeout)]
    controller.main()
    for thread in observers:
        thread.join(timeout=10)
    if observer_results != [0]:
        raise RuntimeError(f'status observer failed or incomplete: {observer_results}')
    rows = [json.loads(line) for line in (evidence / 'rrls-status.jsonl').read_text().splitlines()]
    result = validate_status(rows)
    decoded = evidence / 'sdk-decoded.json'
    subprocess.run([sys.executable, str(ROOT / 'scripts/decode-radio-trace.py'), str(evidence / 'radio-trace'),
                    '--output', str(decoded), '--max-clock-backstep-us', '100'], check=True)
    result['wire'] = validate_wire(json.loads(decoded.read_text()), scenario_config['heartbeat_period_ms'])
    # A live heartbeat does not prove that FC/PB acquisition is reaching GS.
    # Keep exact source delivery and canonical forwarding as separate gates.
    freshness = evidence / 'source-freshness.json'
    subprocess.run([sys.executable, str(ROOT / 'scripts/analyze-radio-freshness.py'),
                    str(decoded), '--start-ms', '26000', '--end-ms', '39000',
                    '--output', str(freshness), '--require-source-reports',
                    '--require-canonical-metadata', '--max-source-latency-ms', '1500',
                    '--max-canonical-latency-ms', '3000',
                    '--require-stream', 'FC:IMU_DATA',
                    '--require-stream', 'FC:BAROMETER_DATA',
                    '--require-stream', 'PB:BATTERY_VOLTAGE',
                    '--require-stream', 'PB:BATTERY_CURRENT'], check=True)
    result['source_freshness'] = str(freshness)
    (evidence / 'fallback-validation.json').write_text(json.dumps(result, indent=2) + '\n')
    print('PASS: actual radio outage, paired relay fallback and direct restoration; '
          'steady commands <=500ms; exact FC/PB source delivery <=1500ms')


if __name__ == '__main__':
    main()
