import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[0] / 'fill_delivery_report.py'
if not MODULE.exists():
    MODULE = Path(__file__).resolve().parents[1] / 'scripts/fill_delivery_report.py'
spec = importlib.util.spec_from_file_location('fill_delivery_report', MODULE)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def fixture():
    samples = {key: [0] * 12 for key in report.REQUIRED_ZERO}
    samples.update({'daq.loadcell_network_rows': [2400 * (i + 1) for i in range(12)],
                    'daq.kg50_network_rows': [2300 * (i + 1) for i in range(12)],
                    'gateway.can_rx_ring_drops': [0] * 12})
    windows = [{'start_ms': start, 'elapsed_ms': 10000, 'channels': report.CHANNELS,
                'received': [2400, 2300, 10, 10, 10], 'max_gap_ms': [50, 50, 1000, 1000, 1000],
                'passed': False} for start in range(20000, 120000, 10000)]
    return {'virtual_duration_ms': 120000, 'samples_10s': samples, 'delivery_windows': windows}


class FillDeliveryReportTests(unittest.TestCase):
    def test_preserves_every_actual_publication_without_assuming_cap_is_production(self):
        result = report.analyze(fixture())
        self.assertTrue(result['passed'])
        self.assertFalse(result['windows'][0]['nominal_rate_diagnostic_passed'])
        self.assertEqual(result['windows'][0]['source_hz'], [240.0, 230.0])

    def test_over_half_percent_loss_fails_any_window(self):
        capture = fixture()
        capture['delivery_windows'][5]['received'][0] -= 13
        self.assertFalse(report.analyze(capture)['passed'])

    def test_inflated_receives_cannot_hide_loss(self):
        capture = fixture()
        capture['delivery_windows'][0]['received'][0] += 15
        self.assertFalse(report.analyze(capture)['passed'])

    def test_equal_count_burst_cannot_hide_staleness(self):
        capture = fixture()
        capture['delivery_windows'][0]['max_gap_ms'][1] = 501
        self.assertFalse(report.analyze(capture)['passed'])

    def test_valve_freshness_and_cadence_are_separate_requirements(self):
        for field, value in [('max_gap_ms', 3001), ('received', 8)]:
            capture = fixture()
            capture['delivery_windows'][0][field][2] = value
            self.assertFalse(report.analyze(capture)['passed'])

    def test_source_death_and_counter_reset_fail(self):
        for values in [[0] * 12, [2400, 4800, 1] + list(range(4, 13))]:
            capture = fixture()
            capture['samples_10s']['daq.loadcell_network_rows'] = values
            self.assertFalse(report.analyze(capture)['passed'])

    def test_counter_wrap_preserves_real_delta(self):
        capture = fixture()
        for key in report.SOURCES:
            capture['samples_10s'][key] = [(n + (1 << 32) - 10000) & 0xffffffff
                                          for n in capture['samples_10s'][key]]
        self.assertTrue(report.analyze(capture)['passed'])

    def test_can_drop_growth_after_warmup_fails(self):
        capture = fixture()
        capture['samples_10s']['gateway.can_rx_ring_drops'][5:] = [101] * 7
        self.assertFalse(report.analyze(capture)['passed'])

    def test_startup_can_loss_is_still_a_failure(self):
        capture = fixture()
        capture['samples_10s']['gateway.can_rx_ring_drops'] = [50] + [100] * 11
        result = report.analyze(capture)
        self.assertTrue(result['can_drops_stable_after_warmup'])
        self.assertFalse(result['can_drop_free_entire_run'])
        self.assertFalse(result['passed'])

    def test_allocator_or_sd_errors_fail(self):
        for key in report.REQUIRED_ZERO:
            capture = fixture()
            capture['samples_10s'][key][0] = 1
            self.assertFalse(report.analyze(capture)['passed'], key)

    def test_startup_aux_refusals_cannot_hide_behind_good_sustained_delivery(self):
        capture = fixture()
        capture['samples_10s']['daq.analog_report_failures'] = [3] * 12
        result = report.analyze(capture)
        self.assertTrue(all(window['passed'] for window in result['windows']))
        self.assertFalse(result['passed'])
        self.assertFalse(result['zero_counters']['daq.analog_report_failures'])

    def test_missing_duplicate_windows_or_missing_source_probes_fail_closed(self):
        for mutation in ['missing', 'duplicate', 'source', 'tail']:
            capture = fixture()
            if mutation == 'missing': del capture['delivery_windows'][3]
            elif mutation == 'duplicate': capture['delivery_windows'].insert(3, copy.deepcopy(capture['delivery_windows'][3]))
            elif mutation == 'source': capture['samples_10s']['daq.loadcell_network_rows'].pop()
            else: del capture['delivery_windows'][-2:]
            with self.assertRaises(ValueError, msg=mutation): report.analyze(capture)

    def test_single_final_scheduler_boundary_is_explicitly_allowed(self):
        capture = fixture()
        capture['delivery_windows'].pop()
        self.assertTrue(report.analyze(capture)['passed'])

    def test_wrong_channel_order_is_rejected(self):
        capture = fixture()
        capture['delivery_windows'][0]['channels'] = list(reversed(report.CHANNELS))
        with self.assertRaises(ValueError): report.analyze(capture)

    def test_real_capture_format_and_probe_indices(self):
        capture = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            text = ''.join(f'SEDS_BAY_PROBE {key.split(".")[0]} {key.split(".")[1]} {i}\n0x{n:x}\n'
                           for key, values in capture['samples_10s'].items()
                           for i, n in enumerate(values))
            (directory / 'memory-probes.txt').write_text(text)
            (directory / 'groundstation-host.log').write_text('\n'.join(
                'INFO sustained fill delivery failed ' + json.dumps(w)
                for w in capture['delivery_windows']))
            self.assertTrue(report.analyze(report.load_diagnostics(directory), expected_duration_ms=120000)['passed'])
            (directory / 'memory-probes.txt').write_text(text.replace('SEDS_BAY_PROBE daq kg50_network_rows 5',
                                                                      'SEDS_BAY_PROBE daq kg50_network_rows 6'))
            with self.assertRaises(ValueError): report.load_diagnostics(directory)

    def test_qualification_rejects_unequal_execution_and_probe_duration(self):
        with self.assertRaisesRegex(ValueError, 'cadence'):
            report.analyze(fixture(), expected_duration_ms=110000)
        self.assertTrue(report.analyze(fixture(), expected_duration_ms=120000)['passed'])

    def test_unverified_sample_count_does_not_establish_execution_duration(self):
        capture=fixture();capture.pop('virtual_duration_ms')
        with self.assertRaisesRegex(ValueError,'Explicit virtual'):
            report.analyze(capture)


if __name__ == '__main__': unittest.main()
