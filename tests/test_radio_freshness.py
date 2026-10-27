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

    def test_required_stream_uses_mapped_source_and_exact_arrival_in_window(self):
        data=report()
        required=[('PB','BATTERY_VOLTAGE'),('FC','IMU_DATA')]
        coverage=FRESHNESS.required_stream_coverage(data,100,200,required)
        self.assertEqual(coverage['rows'][0]['result'],'PASS')
        self.assertEqual(coverage['rows'][1]['result'],'UNKNOWN_SOURCE')
        self.assertIsNone(coverage['rows'][1]['source_address'])
        # A different board's same type, an earlier arrival, or a late arrival
        # cannot satisfy a missing sensor's exact identity in this window.
        data['known_source_addresses']['FC']=7
        source=dict(packet(150,90),source_address=7,type='IMU_DATA')
        data['streams'][0]['packets'].append(source)
        for address,at_ms,identifier in [(3,160,90),(7,140,90),(7,210,90),(7,160,91)]:
            data['streams'][-1]['packets'].append(dict(source,source_address=address,
                at_us=at_ms*1000,packet_id=identifier))
        coverage=FRESHNESS.required_stream_coverage(data,100,200,required)
        self.assertEqual(coverage['missing'][0]['result'],'NO_MATCHING_GS_REPORTS')
        data['streams'][-1]['packets'].append(dict(source,at_us=170000))
        coverage=FRESHNESS.required_stream_coverage(data,100,200,required+required)
        self.assertEqual(coverage['result'],'PASS')
        self.assertEqual(len(coverage['rows']),2)

    def test_required_stream_without_any_sensor_reports_fails_closed(self):
        data=report()
        for stream in data['streams']:
            stream['packets']=[dict(p,type='HEARTBEAT') for p in stream['packets']]
        coverage=FRESHNESS.required_stream_coverage(data,100,600,[('PB','BATTERY_CURRENT')])
        self.assertEqual(coverage['result'],'MISSING_REQUIRED_STREAMS')
        self.assertEqual(coverage['missing'][0]['source_reports'],0)
        with self.assertRaises(ValueError):
            FRESHNESS.delivery_coverage(data,100,600)


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
    def test_required_sensor_streams_cannot_be_satisfied_by_other_sensors_or_heartbeats(self):
        data=report()
        data['known_source_addresses'].update(FC=7)
        for stream in data['streams']:
            stream['packets'] = [
                dict(packet,source_address=7,type='IMU_DATA') for packet in stream['packets']]
            stream['packets'] += [dict(packet(i*100,i+100),type=kind)
                                  for i in range(8) for kind in ('HEARTBEAT','BOARD_HEALTH')]
        data['streams'][-1]['packets'] = [dict(p,at_us=p['at_us']+20000,relay_kind=1)
                                         for p in data['streams'][0]['packets']]
        self.assertEqual(FRESHNESS.delivery_coverage(data,100,600)['result'],'PASS')
        with tempfile.TemporaryDirectory() as directory:
            directory=Path(directory)
            input_path=directory/'input.json';input_path.write_text(json.dumps(data))
            output_path=directory/'output.json'
            command=[sys.executable,str(Path(FRESHNESS.__file__)),str(input_path),
                     '--start-ms','100','--end-ms','600','--require-source-reports',
                     '--require-stream','FC:IMU_DATA','--require-stream','FC:BAROMETER_DATA',
                     '--require-stream','PB:BATTERY_VOLTAGE','--require-stream','PB:BATTERY_CURRENT',
                     '--output',str(output_path)]
            result=subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(result.returncode,1,result.stderr)
            evidence=json.loads(output_path.read_text())
            self.assertEqual(evidence['exact_source_delivery']['result'],'PASS')
            missing=evidence['required_streams']['missing']
            self.assertEqual([(row['source'],row['type']) for row in missing],
                             [('FC','BAROMETER_DATA'),('PB','BATTERY_VOLTAGE'),('PB','BATTERY_CURRENT')])
            self.assertTrue(all(row['source_reports']==0 for row in missing))
            # Keep a reviewable failure artifact even when every sensor is absent.
            for stream in data['streams']:
                stream['packets']=[dict(p,type='HEARTBEAT') for p in stream['packets']]
            input_path.write_text(json.dumps(data))
            result=subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(result.returncode,1,result.stderr)
            evidence=json.loads(output_path.read_text())
            self.assertEqual(len(evidence['required_streams']['missing']),4)

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

    def test_invalid_required_stream_configuration_fails(self):
        for stream in ['PB','PB:HEARTBEAT',':IMU_DATA','PB:IMU_DATA:extra']:
            result=subprocess.run([sys.executable,str(Path(FRESHNESS.__file__)),'unused',
                   '--start-ms','100','--end-ms','101','--output','unused',
                   '--require-stream',stream],capture_output=True)
            self.assertEqual(result.returncode,2)
