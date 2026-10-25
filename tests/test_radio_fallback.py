import importlib.util
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('radio_fallback', ROOT / 'scripts/test-radio-fallback.py')
FALLBACK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FALLBACK)
OBS_SPEC = importlib.util.spec_from_file_location('radio_observer', ROOT / 'scripts/radio-fallback-observer.py')
OBSERVER = importlib.util.module_from_spec(OBS_SPEC)
OBS_SPEC.loader.exec_module(OBSERVER)


def statuses():
    rows = []
    for now in range(10000, 56001, 50):
        requested = 23000 <= now < 42000
        forwarding = 23500 <= now < 42000
        direct_age = now - 19950 if 20000 <= now < 40000 else 0
        rows.append(dict(virtual_ms=now,
            primary=dict(role='primary', relay_requested=requested, forwarding=forwarding, direct_age_ms=direct_age),
            rrls=dict(role='rrls', forwarding=23500 <= now < 42500)))
    return rows


class RadioFallbackTests(unittest.TestCase):
    def test_direct_edges_only_and_existing_gates_preserved(self):
        existing = dict(name='Existing500msACKgate', node='groundstation', contains='latency within bound')
        topology = dict(sample_count=12, virtual_time_ms=60000, assertions=[{'name': 'sensor'}],
                        host_log_assertions=[existing.copy()], host_nodes=[
                            dict(name='groundstation', env={'GS_SIM_MANAGED_VARIABLE_MAX_LATENCY_MS': '500'}),
                            dict(name='rrls', env={})])
        FALLBACK.configure_fallback(topology, [18001, 18002])
        self.assertEqual(topology['host_log_assertions'][0], existing)
        self.assertEqual(topology['assertions'], [{'name': 'sensor'}])
        self.assertEqual(len(topology['radio_link_events']), 4)
        self.assertEqual({(v['sender'], v['receiver']) for v in topology['radio_link_events']},
                         {('rf', 'groundstation'), ('groundstation', 'rf')})
        primary = topology['host_nodes'][0]['env']
        self.assertEqual(primary['GS_SIM_MANAGED_VARIABLE_MAX_LATENCY_MS'], '500')
        self.assertEqual(primary['GS_SIM_SOAK_COMMAND_SAMPLES'], '2,3,6,7,10,11')
        topology['can_ack_events'] = [{'node': 'rf'}]
        with self.assertRaisesRegex(ValueError, 'isolate'):
            FALLBACK.configure_fallback(topology, [18001, 18002])

    def test_optional_production_cadence_override_updates_both_hosts_only(self):
        def topology():
            return dict(sample_count=12, virtual_time_ms=60000, host_nodes=[
                dict(name='groundstation', env={'GS_SIM_MANAGED_VARIABLE_MAX_LATENCY_MS': '500', 'GS_HEARTBEAT_INTERVAL_MS': '1000'}),
                dict(name='rrls', env={})])
        original = topology()
        FALLBACK.configure_fallback(original, [18001,18002])
        self.assertEqual(original['host_nodes'][0]['env']['GS_HEARTBEAT_INTERVAL_MS'], '1000')
        self.assertNotIn('GS_HEARTBEAT_INTERVAL_MS', original['host_nodes'][1]['env'])
        production = topology()
        FALLBACK.configure_fallback(production, [18001,18002], heartbeat_period_ms=500)
        self.assertTrue(all(h['env']['GS_HEARTBEAT_INTERVAL_MS']=='500' for h in production['host_nodes']))
        self.assertEqual(production['host_nodes'][0]['env']['GS_SIM_MANAGED_VARIABLE_MAX_LATENCY_MS'], '500')
        for invalid in [99,60001]:
            with self.assertRaisesRegex(ValueError,'heartbeat'):
                FALLBACK.configure_fallback(topology(), [18001,18002], heartbeat_period_ms=invalid)

    def test_transition_and_steady_phase_evidence(self):
        result = FALLBACK.validate_status(statuses())
        self.assertEqual(result['result'], 'PASS')
        self.assertEqual(result['command_ack_limit_ms'], 500)
        self.assertEqual(result['direct_restored_ms'], 42000)

    def test_false_fallback_early_restore_relay_liveness_and_missing_phase_fail(self):
        rows = statuses()
        rows[0]['primary']['relay_requested'] = True
        with self.assertRaisesRegex(ValueError, 'quiescent'):
            FALLBACK.validate_status(rows)
        rows = statuses()
        for row in rows:
            if row['virtual_ms'] == 30000:
                row['primary']['direct_age_ms'] = 100
        with self.assertRaisesRegex(ValueError, 'falsely'):
            FALLBACK.validate_status(rows)
        rows = statuses()
        for row in rows:
            if row['virtual_ms'] == 41000:
                row['primary']['relay_requested'] = False
        with self.assertRaisesRegex(ValueError, 'hysteresis'):
            FALLBACK.validate_status(rows)
        with self.assertRaisesRegex(ValueError, 'coverage'):
            FALLBACK.validate_status([r for r in statuses() if r['virtual_ms'] < 45000])

    def test_actual_clock_producer_frames_files_without_newlines_and_rejects_ambiguity(self):
        pattern = '/tmp/.tmp*/virtual-clock.txt /tmp/*/virtual-clock.txt'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'clock'
            for contents in ['12345', '12345\n']:
                path.write_text(contents)
                script = FALLBACK.clock_stream_script().replace('while :;', 'for radio_test_iteration in 1 2;').replace(pattern, shlex.quote(str(path)))
                result = subprocess.run(['/bin/sh', '-c', script], capture_output=True, timeout=2, check=True)
                self.assertEqual([line for line in result.stdout.splitlines() if line], [b'12345', b'12345'])
            other = Path(directory) / 'other-clock'
            other.write_text('0')
            script = FALLBACK.clock_stream_script().replace(pattern, shlex.quote(str(path)) + ' ' + shlex.quote(str(other)))
            result = subprocess.run(['/bin/sh', '-c', script], capture_output=True, timeout=2)
            self.assertEqual(result.returncode, 2)
            self.assertIn(b'ambiguous', result.stderr)

    def test_clock_stream_framing_fragmentation_and_bound(self):
        read, write = os.pipe()
        try:
            clock = OBSERVER.ClockInput(read)
            os.write(write, b'123')
            self.assertEqual(clock.poll(), (None, False))
            os.write(write, b'45\n\n12360\n')
            self.assertEqual(clock.poll(), (12360, False))
            os.write(write, b'1' * 33)
            with self.assertRaisesRegex(RuntimeError, 'bound'):
                clock.poll()
        finally:
            os.close(read)
            os.close(write)

    def test_wire_namespaces_prove_each_actual_path(self):
        def p(ms, kind):
            return dict(at_us=ms * 1000, relay_kind=kind, type='DATA', source_address=7)
        report = {'known_source_addresses': {'GS': 7}, 'streams': [dict(station=1, direction=0, packets=[p(10000,5),p(30000,2),p(50000,5)]),
                              dict(station=2, direction=0, packets=[p(30000,5),p(31000,1)])]}
        report['streams'][0]['packets'].append(dict(at_us=10000000, relay_kind=5, type='HEARTBEAT', source_address=7))
        self.assertEqual(FALLBACK.validate_wire(report)['result'], 'PASS')
        self.assertEqual(FALLBACK.validate_wire(report, heartbeat_period_ms=1000)['heartbeat_maximum'], 58)
        heartbeat = dict(at_us=10000000, relay_kind=5, type='HEARTBEAT', source_address=7)
        report['streams'][0]['packets'].extend([heartbeat] * 115)
        with self.assertRaisesRegex(ValueError, 'wall time'):
            FALLBACK.validate_wire(report)
        report['streams'][0]['packets'] = report['streams'][0]['packets'][:4]
        report['streams'][1]['packets'].append(p(51000,1))
        with self.assertRaisesRegex(ValueError, 'standby'):
            FALLBACK.validate_wire(report)


if __name__ == '__main__':
    unittest.main()
