import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('clock_analysis', ROOT/'scripts/analyze-radio-clock.py')
CLOCK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLOCK)


def report(times):
    return {'known_source_addresses': {'GS': 7}, 'streams': [{'station': 1, 'direction': 0, 'packets': [
        dict(source_address=7, at_us=mono*1000, timestamp_ms=999999, packet_id=index,
             timesync_announce=dict(time_ms=utc)) for index,(mono,utc) in enumerate(times)]}]}


class RadioClockTests(unittest.TestCase):
    def test_utc_epoch_and_queue_delay_are_preserved_in_csv_rows(self):
        rows, result = CLOCK.analyze(report([(100,1000),(1100,2000)]),epoch_ms=1000)
        self.assertEqual(result['result'], 'PASS')
        self.assertEqual(result['utc_elapsed_ms'], 1000)
        self.assertEqual(rows[0]['utc_minus_wire_ms'], -100)
        self.assertEqual(rows[0]['packet_timestamp_ms'], 999999)

    def test_wall_clock_jump_wrong_epoch_and_reverse_utc_fail(self):
        for times in [[(100,1000),(1100,999999)],[(100,999),(1100,2000)],[(100,1100),(1100,1050)]]:
            with self.assertRaises(ValueError):
                CLOCK.analyze(report(times),epoch_ms=1000)
        with self.assertRaisesRegex(ValueError, 'two'):
            CLOCK.analyze(report([(100,1000)]),epoch_ms=1000)

    def test_recovered_rf_authority_and_consumer_holdover_are_distinct(self):
        data = {'known_source_addresses': {'GS': 7, 'RF': 9, 'RRLS': 11}, 'streams': [
            {'station': 1, 'direction': 1, 'packets': [
                dict(source_address=13, at_us=25_000_000, timesync_announce=dict(priority=100)),
                dict(source_address=9, at_us=45_010_000, timesync_announce=dict(priority=1))]}]}
        result = CLOCK.analyze_authorities(data, 45_000)
        self.assertEqual(result['post_recovery_rf_announcements'], 1)
        data['streams'][0]['packets'][0]['timesync_announce']['priority'] = 0
        with self.assertRaisesRegex(ValueError, 'priority-zero'):
            CLOCK.analyze_authorities(data, 45_000)
        data['streams'][0]['packets'][0]['timesync_announce']['priority'] = 25
        with self.assertRaisesRegex(ValueError, 'holdover'):
            CLOCK.analyze_authorities(data, 45_000)
        data['streams'][0]['packets'][0]['timesync_announce']['priority'] = 100
        with self.assertRaisesRegex(ValueError, 'post-recovery'):
            CLOCK.analyze_authorities(data, 50_000)


if __name__ == '__main__':
    unittest.main()
