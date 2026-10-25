import importlib.util
import json
import subprocess
import sys
import tempfile
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

    def test_exact_source_delivery_does_not_substitute_direct_or_duplicate_packets(self):
        original=report()
        original['streams'][-1]['packets'].append(packet(330,3))
        result=FRESHNESS.delivery_coverage(original,100,600)
        self.assertEqual(result['source_reports'],6)
        self.assertEqual(result['delivered_reports'],2)
        self.assertEqual([row['packet_id'] for row in result['missing']],[2,3,4,5])
        self.assertEqual(result['maximum_source_to_gs_ms'],20)
        data=report()
        data['streams'][-1]['packets'] += [packet(i*100+20,i,1) for i in range(2,6)]
        result=FRESHNESS.delivery_coverage(data,100,600)
        self.assertEqual(result['result'],'PASS')
        self.assertFalse(result['missing'])

    def test_invalid_window_is_rejected(self):
        for start,end in [(-1,0),(1,1),(2,1)]:
            with self.assertRaises(ValueError):FRESHNESS.analyze(report(),start,end)


class MetadataCoverageTests(unittest.TestCase):
    @staticmethod
    def packet(at_ms, identifier, relay):
        return dict(at_us=at_ms*1000,packet_id=identifier,source_address=3,
                    type='SedsnetDiscoveryAddress',relay_kind=relay)

    def test_direction_and_occurrences_cannot_be_substituted(self):
        data = dict(streams=[
            dict(station=2,direction=1,packets=[self.packet(100,1,None),
                 self.packet(110,1,None),self.packet(120,2,2)]),
            dict(station=2,direction=0,packets=[self.packet(105,1,1),
                 self.packet(125,2,1),self.packet(130,1,5)])])
        for stream in data['streams']:
            stream['frames']=[dict(packet_index=i,side_kind=None) for i in range(len(stream['packets']))]
        coverage=FRESHNESS.metadata_coverage(data,100,150)
        self.assertEqual(coverage['received_occurrences'],3)
        self.assertEqual(coverage['delivered_occurrences'],1)
        self.assertEqual([row['packet_id'] for row in coverage['missing']],[1,2])
        data['streams'][1]['packets'] += [self.packet(140,1,1),self.packet(150,2,5)]
        for stream in data['streams']:
            stream['frames']=[dict(packet_index=i,side_kind=None) for i in range(len(stream['packets']))]
        coverage=FRESHNESS.metadata_coverage(data,100,150)
        self.assertEqual(coverage['result'],'PASS')
        self.assertEqual(coverage['maximum_forwarding_age_ms'],30)

    def test_sdt_wrapped_metadata_is_not_independent_canonical_work(self):
        data=dict(streams=[
            dict(station=2,direction=1,packets=[self.packet(100,1,None),self.packet(110,2,None)],
                 frames=[dict(packet_index=0,side_kind=None),dict(packet_index=1,side_kind=1)]),
            dict(station=2,direction=0,packets=[self.packet(120,1,1)],
                 frames=[dict(packet_index=0,side_kind=None)])])
        coverage=FRESHNESS.metadata_coverage(data,100,150)
        self.assertEqual(coverage['received_occurrences'],1)
        self.assertEqual(coverage['result'],'PASS')

    def test_no_observed_work_is_rejected(self):
        with self.assertRaises(ValueError):
            FRESHNESS.metadata_coverage(dict(streams=[dict(station=2,direction=0,packets=[],frames=[]),
                 dict(station=2,direction=1,packets=[],frames=[])]),100,150)


class FreshnessCommandTests(unittest.TestCase):
    def test_latency_bound_is_enforced_and_written_before_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            directory=Path(directory)
            input_path=directory/'input.json'
            input_path.write_text(json.dumps(report()))
            output_path=directory/'output.json'
            command=[sys.executable,str(Path(FRESHNESS.__file__)),str(input_path),
                     '--start-ms','100','--end-ms','101','--require-source-reports',
                     '--output',str(output_path),'--max-source-latency-ms']
            result=subprocess.run(command+['19'],capture_output=True,text=True)
            self.assertEqual(result.returncode,1)
            evidence=json.loads(output_path.read_text())
            self.assertEqual(evidence['exact_source_delivery']['result'],'PASS')
            self.assertFalse(evidence['source_latency_gate']['passed'])
            self.assertEqual(subprocess.run(command+['20'],capture_output=True).returncode,0)

    def test_invalid_latency_configuration_fails(self):
        for bound in ['nan','inf','0','-1']:
            result=subprocess.run([sys.executable,str(Path(FRESHNESS.__file__)),'unused',
                   '--start-ms','100','--end-ms','101','--output','unused',
                   '--require-source-reports','--max-source-latency-ms',bound],capture_output=True)
            self.assertEqual(result.returncode,2)
