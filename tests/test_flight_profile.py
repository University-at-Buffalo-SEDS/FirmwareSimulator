"""Flight-profile generator and the FC sensor encodings used by SedsSpiSensors.cs."""
import importlib.util
import json
import math
import struct
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('flight_profile', ROOT / 'scripts/flight_profile.py')
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)


def f32(x):
    return struct.unpack('f', struct.pack('f', x))[0]


def fc_compensate(raw_p, raw_t, trims):
    """FlightComputer26 Core/Src/barometer.c read_trim_pars + compensation (float math)."""
    t1, t2, t3 = trims['t1'] * 2 ** 8, trims['t2'] / 2 ** 30, trims['t3'] / 2 ** 48
    p1 = (trims['p1'] - 2 ** 14) / 2 ** 20
    p2 = (trims['p2'] - 2 ** 14) / 2 ** 29
    p5 = trims['p5'] * 2 ** 3
    d1 = raw_t - t1
    t_lin = d1 * t2 + d1 * d1 * t3
    return f32(p5 + raw_p * (p1 + p2 * t_lin)), t_lin


# Register image written by SedsFlightSensorBus.Reset() when a profile is active.
PROFILE_TRIMS = {'t1': 0x61a8, 't2': 0x4000, 't3': 0, 'p1': 0x6000, 'p2': 0x4000, 'p5': 0}


class FlightProfileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows, cls.summary = profile.build(dict(profile.DEFAULTS))

    def test_deterministic(self):
        rows, summary = profile.build(dict(profile.DEFAULTS))
        self.assertEqual(json.dumps(summary, sort_keys=True), json.dumps(self.summary, sort_keys=True))
        self.assertEqual(rows[-1], self.rows[-1])

    def test_timeline_matches_the_scenario(self):
        e = self.summary['events_s']
        m = self.summary['expected_maxima']
        self.assertTrue(2500 <= m['max_altitude_agl_m'] <= 3000)
        self.assertLess(m['max_altitude_agl_m'], 3657.6)        # fcconfig.h VIGILANT_MAX_ALT
        self.assertTrue(8 <= m['max_acceleration_g'] <= 10)
        self.assertTrue(3 <= e['burnout'] <= 4)
        # FC still under canopy at T+8:00, on the ground by T+10:00.
        self.assertLess(e['main_s'], 480)
        self.assertTrue(480 < e['landing_s'] < 600)
        under_drogue = [r for r in self.rows if r['phase'] == 'drogue' and r['t_ms'] > (e['drogue_s'] + 10) * 1000]
        self.assertTrue(all(abs(r['vel_mps']) < 8.5 for r in under_drogue))
        self.assertTrue(all(abs(r['vel_mps']) > 4.25 for r in under_drogue))  # DESCENT_MIN_VEL
        self.assertGreater(self.summary['config']['main_alt_m'], 0)
        self.assertLess(self.summary['config']['main_alt_m'], profile.REEF_TARGET_ALT)

    def test_pressure_stays_inside_fc_validation(self):
        pressures = [r['pressure_pa'] for r in self.rows]
        self.assertGreater(min(pressures), 57200)   # MIN_PRS
        self.assertLess(max(pressures), 110000)     # MAX_PRS / BARO_MAX_PA
        self.assertGreater(pressures[0], 70000)     # BARO_MIN_PA baseline

    def test_barometer_encoding_round_trips_through_fc_compensation(self):
        p0 = self.rows[0]['pressure_pa']
        for row in self.rows[::500]:
            raw_p = round(row['pressure_pa'] * 128)
            raw_t = round(6400000 + row['temp_c'] * 65536)
            self.assertLess(raw_p, 1 << 24)
            pressure, t_lin = fc_compensate(raw_p, raw_t, PROFILE_TRIMS)
            self.assertAlmostEqual(t_lin, row['temp_c'], places=3)
            alt = 44330 * (1 - (pressure / p0) ** 0.1903)
            self.assertAlmostEqual(alt, row['alt_agl_m'], delta=0.05)

    def test_accelerometer_scale_and_saturation(self):
        lsb = 1.5 * 16 / 32768 * profile.G   # BMI088 range 0x03 (+/-24 g), datasheet scale
        self.assertAlmostEqual(round(profile.G / lsb) * lsb, profile.G, delta=lsb)
        full_scale = 32767 * lsb
        self.assertTrue(23.9 * profile.G < full_scale < 24.1 * profile.G)
        # The whole profile, boost included, is measurable without saturation.
        self.assertLess(self.summary['expected_maxima']['max_specific_force_mps2'], full_scale)

    def test_csv_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile.write(self.rows[:10], self.summary, Path(tmp))
            lines = (Path(tmp) / 'flight-profile.csv').read_text().splitlines()
            self.assertTrue(lines[0].startswith('#'))
            self.assertEqual(lines[1].split(','), profile.COLUMNS)
            self.assertEqual(len(lines), 12)


if __name__ == '__main__':
    unittest.main()
