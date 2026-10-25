import importlib.util
from pathlib import Path
import unittest

SPEC=importlib.util.spec_from_file_location('freshness',Path(__file__).resolve().parents[1]/'scripts/analyze-radio-freshness.py')
FRESHNESS=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FRESHNESS)


def packet(at_ms, identifier, relay=None):
    return dict(at_us=at_ms*1000,packet_id=identifier,source_address=3,
                type='BATTERY_VOLTAGE',relay_kind=relay)


def report():
    return dict(known_source_addresses={'PB':3},streams=[
        dict(station=0,direction=0,packets=[packet(i*100,i) for i in range(8)]),
        dict(station=2,direction=1,packets=[packet(i*100+5,i) for i in range(8)]),
        dict(station=1,direction=1,packets=[packet(10,0),packet(120,1,1),
             packet(620,6,1),packet(630,6),packet(720,7)])])


class FreshnessTests(unittest.TestCase):
    def test_missing_reports_and_direct_relay_duplicates_remain_distinct(self):
        row=FRESHNESS.analyze(report(),100,650)['rows'][0]
        self.assertEqual(row['source_rf_tx']['maximum_internal_gap_ms'],100)
        self.assertEqual(row['gs_down_data']['maximum_internal_gap_ms'],500)
        self.assertEqual(row['gs_all_eligible_deduplicated']['count'],2)
        self.assertEqual(row['largest_app_gap_intersecting_window']['gap_ms'],500)
        self.assertEqual(row['gs_down_data']['first_ms'],120)
        self.assertEqual(row['gs_down_data']['last_ms'],620)

    def test_empty_window_is_censored_without_inventing_report_gap(self):
        row=FRESHNESS.analyze(report(),200,400)['rows'][0]
        self.assertEqual(row['gs_down_data']['count'],0)
        self.assertIsNone(row['gs_down_data']['maximum_internal_gap_ms'])
        self.assertEqual(row['gs_down_data']['leading_censored_interval_ms'],200)
        self.assertEqual(row['largest_app_gap_intersecting_window']['gap_ms'],500)

    def test_invalid_window_is_rejected(self):
        for start,end in [(-1,0),(1,1),(2,1)]:
            with self.assertRaises(ValueError):FRESHNESS.analyze(report(),start,end)
