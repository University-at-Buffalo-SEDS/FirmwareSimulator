import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/flight_state_wire_check.py'
SPEC = importlib.util.spec_from_file_location('flight_state_wire_check', SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def run_dir(root, recorded, logged):
    data = Path(root) / 'gs-backend-data'
    data.mkdir()
    db = sqlite3.connect(data / 'groundstation_recording_1.db')
    db.execute('CREATE TABLE telemetry (id INTEGER PRIMARY KEY, timestamp_ms INTEGER, data_type TEXT, sender_id TEXT, values_json TEXT)')
    for at, (sender, value) in enumerate(recorded):
        db.execute('INSERT INTO telemetry (timestamp_ms, data_type, sender_id, values_json) VALUES (?, ?, ?, ?)',
                   (at, 'FLIGHT_STATE', sender, f'[{value}]'))
    db.execute("INSERT INTO telemetry (timestamp_ms, data_type, sender_id, values_json) VALUES (9, 'ROLL', 'FC', '[8]')")
    db.commit()
    db.close()
    (Path(root) / 'simulator.log').write_text(''.join(
        f'INFO groundstation_backend::network_variables: FLIGHT_STATE wire value={v} (x)\n' for v in logged))
    return Path(root)


class FlightStateWireCheckTests(unittest.TestCase):
    def test_full_flight_on_set_a_passes(self):
        with tempfile.TemporaryDirectory() as root:
            result = MODULE.check(run_dir(root, [('FC', 0), ('FC', 6), ('FC', 7), ('FC', 16), ('FC', 13), ('VB', 7)],
                                          [0, 1, 2, 2, 6, 7, 15]))
        self.assertTrue(result['pass'], result)
        self.assertEqual(result['station_writes'], 7)
        self.assertEqual(result['values'], [0, 1, 2, 6, 7, 13, 15, 16])

    def test_an_fc_detailed_phase_on_the_wire_fails(self):
        with tempfile.TemporaryDirectory() as root:
            result = MODULE.check(run_dir(root, [('FC', 7), ('FC', 8), ('FC', 11), ('FC', 14)], [7]))
        self.assertFalse(result['pass'])
        self.assertEqual([v['value'] for v in result['violations']], [8, 11, 14])

    def test_a_station_fill_sub_phase_write_fails(self):
        with tempfile.TemporaryDirectory() as root:
            result = MODULE.check(run_dir(root, [('FC', 0)], [2, 5]))
        self.assertFalse(result['pass'])
        self.assertEqual(result['violations'][0]['value'], 5)

    def test_a_run_without_frames_fails(self):
        with tempfile.TemporaryDirectory() as root:
            result = MODULE.check(Path(root))
        self.assertFalse(result['pass'])


if __name__ == '__main__':
    unittest.main()
