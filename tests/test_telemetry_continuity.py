"""The continuity analyzer passes a steady AV bay and fails one that arrives in radio bursts with losses."""
import sqlite3, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/analyze-telemetry-continuity.py'


def run_dir(tmp, bursty):
    run = Path(tmp) / 'run'; (run / 'gs-data').mkdir(parents=True)
    con = sqlite3.connect(run / 'gs-data' / 'groundstation_recording_test.db')
    con.execute('create table telemetry (timestamp_ms integer, source_timestamp_ms integer, sender_id text, data_type text)')
    t0 = 1_767_225_600_000; rows = []
    for i in range(1500):  # 5 Hz for 300 s
        src = t0 + i * 200
        if bursty and (i % 10) in (3, 4, 5):
            continue  # lost on the air
        rx = (src // 2000 + 1) * 2000 if bursty else src + 50  # delivered in 2 s bursts
        rows += [(rx, src, 'FC', 'ACCEL_DATA'), (rx, src, 'FC', 'BAROMETER_DATA')]
    for i in range(300):  # 1 Hz
        src = t0 + i * 1000
        rows += [(src + 50, src, 'PB', 'BATTERY_VOLTAGE'), (src + 50, src, 'RF', 'GPS_STATUS')]
    con.executemany('insert into telemetry values (?,?,?,?)', rows); con.commit(); con.close()
    return run


class TelemetryContinuityTests(unittest.TestCase):
    def check(self, bursty):
        with tempfile.TemporaryDirectory() as tmp:
            return subprocess.run([sys.executable, str(SCRIPT), str(run_dir(tmp, bursty)), '--skip-ms', '10000',
                                   '--assert-targets'], capture_output=True, text=True)

    def test_steady_streams_meet_the_targets(self):
        result = self.check(False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('CONTINUITY TARGETS PASS', result.stdout)

    def test_bursty_lossy_streams_fail_the_targets(self):
        result = self.check(True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('FC/ACCEL_DATA', result.stdout.split('CONTINUITY TARGETS')[-1])


if __name__ == '__main__':
    unittest.main()
