import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('radio_profile', ROOT / 'scripts/radio_profile.py')
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)

class RadioProfileTests(unittest.TestCase):
    def scenario(self):
        p = json.loads((ROOT / 'examples/radio/capacity-64k.json').read_text())
        t = {'links': [{'name': 'rocket_radio', 'kind': 'radio', 'endpoints': [
            {'node': 'rf', 'peripheral': 'usart1'}, {'node': 'groundstation', 'peripheral': 'radio'}]}],
            'host_nodes': [{'name': 'groundstation', 'binary': '/usr/local/bin/groundstation_backend',
                'cwd': '/opt/groundstation/backend', 'env': {'GS_SIM_VALIDATE_SOAK_COMMANDS': '1'},
                'serial_links': [{'link': 'rocket_radio', 'env': 'GS_AV_BAY_SERIAL_PORT'},
                                 {'link': 'fill_pico', 'env': 'GS_UMBILICAL_SERIAL_PORT'}]}]}
        return p, t
    def test_two_real_hosts_have_one_shared_radio_and_only_primary_fill_link(self):
        p, t = self.scenario()
        with tempfile.TemporaryDirectory() as tmp:
            profile.configure_radio_profile(Path(tmp), t, p, True)
            rr = t['host_nodes'][1]
            self.assertEqual(rr['env']['GS_RRLS_ROLE'], 'rrls')
            self.assertNotIn('GS_RRLS_ROLE', t['host_nodes'][0]['env'])
            self.assertEqual(rr['serial_links'], [{'link': 'rocket_radio', 'env': 'GS_AV_BAY_SERIAL_PORT'}])
            self.assertNotIn('GS_SIM_VALIDATE_SOAK_COMMANDS', rr['env'])
            self.assertNotIn('fill_box', json.loads((Path(tmp) / 'comms-rrls.json').read_text()))
            self.assertEqual(len(t['links'][0]['endpoints']), 3)
            self.assertEqual(t['links'][0]['radio'], p)
            self.assertEqual(t['host_nodes'][0]['env']['GS_SIM_VALIDATE_SOAK_COMMANDS'], '1')
            self.assertNotEqual(rr['cwd'], t['host_nodes'][0]['cwd'])
            self.assertEqual(rr['env']['GS_SIM_NETWORK_EPOCH_MS'],
                             t['host_nodes'][0]['env']['GS_SIM_NETWORK_EPOCH_MS'])
            self.assertEqual(rr['cwd'], '/tmp')
            for key in ('GS_USERS_FILE', 'GS_GSE_CONFIG', 'GS_GSE_QUALIFICATION',
                        'GS_LOADCELL_CALIBRATION_PATH', 'GS_STATION_POSITIONS_PATH',
                        'GS_CHECKLISTS_FILE', 'GS_MISSION_SETTINGS_PATH'):
                self.assertTrue(rr['env'][key].startswith('/tmp/simulator-rrls-'))
                self.assertNotEqual(rr['env'][key], t['host_nodes'][0]['env'].get(key))
    def test_rrls_scenario_rejects_a_disabled_or_receiver_primary_role(self):
        for role in ('off', 'rrls'):
            p, t = self.scenario()
            t['host_nodes'][0]['env']['GS_RRLS_ROLE'] = role
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(ValueError, 'primary station'):
                    profile.configure_radio_profile(Path(tmp), t, p, True)

    def test_finite_two_node_profile_keeps_primary_role_and_sources_unchanged(self):
        p, t = self.scenario()
        with tempfile.TemporaryDirectory() as tmp:
            profile.configure_radio_profile(Path(tmp), t, p)
            self.assertEqual(len(t['host_nodes']), 1)
            self.assertNotIn('GS_RRLS_ROLE', t['host_nodes'][0]['env'])
            self.assertEqual(t['host_nodes'][0]['env']['GS_SIM_NETWORK_EPOCH_MS'], '1767225600000')
            self.assertEqual(len(t['links'][0]['endpoints']), 2)
    def test_missing_assumptions_boolean_rates_and_oversize_buffers_are_rejected(self):
        p, _ = self.scenario()
        for key, value in [('uart_baud', True), ('tx_fifo_bytes', 2**32), ('max_burst_bytes', 0)]:
            with self.subTest(key=key):
                bad = dict(p); bad[key] = value
                with self.assertRaises(ValueError): profile.validate_profile(bad)
        p.pop('packet_idle_us')
        with self.assertRaises(ValueError): profile.validate_profile(p)
    def test_ambiguous_shared_working_directory_is_rejected(self):
        p, t = self.scenario()
        t['host_nodes'][0]['cwd'] = '/tmp'
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'working directory'):
                profile.configure_radio_profile(Path(tmp), t, p, True)
    def test_explicit_production_heartbeat_changes_only_host_traffic(self):
        p, t = self.scenario()
        with tempfile.TemporaryDirectory() as tmp:
            profile.configure_radio_profile(Path(tmp), t, p, True)
            links = json.loads(json.dumps(t['links']))
            profile.configure_host_heartbeat(t, 500)
            self.assertEqual([h['env']['GS_HEARTBEAT_PERIOD_MS'] for h in t['host_nodes']], ['500', '500'])
            self.assertEqual(t['links'], links)
            self.assertEqual(t['host_nodes'][0]['env']['GS_SIM_VALIDATE_SOAK_COMMANDS'], '1')
        for value in (True, 99, 60_001, '500'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                profile.configure_host_heartbeat(t, value)

if __name__ == '__main__': unittest.main()
