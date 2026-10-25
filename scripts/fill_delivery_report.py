#!/usr/bin/env python3
"""Validate forwarding against actual DAQ publication, independently of ADC cadence."""
import argparse
import json
import re
from pathlib import Path

CHANNELS = ['KG1000', 'KG50', 'VB_BATTERY', 'VB_CURRENT', 'VB_PRESSURE']
SOURCES = ['daq.loadcell_network_rows', 'daq.kg50_network_rows']
MAX_GAPS = [500, 500, 3000, 3000, 3000]
REQUIRED_ZERO = ['gateway.allocation_failures', 'daq.loadcell_network_failures',
                 'daq.kg50_network_failures', 'daq.analog_report_failures', 'daq.sd_write_errors', 'daq.sd_raw_batch_drops']


def load_diagnostics(directory):
    directory = Path(directory)
    if not (directory / 'memory-probes.txt').is_file():
        found = [path for path in directory.glob('bay-*/memory-probes.txt')
                 if path.parent.name.removeprefix('bay-').isdigit()]
        if len(found) != 1:
            raise ValueError('Select exactly one completed bay capture')
        directory = found[0].parent
    indexed = {}
    for block in (directory / 'memory-probes.txt').read_text().split('SEDS_BAY_PROBE ')[1:]:
        lines = block.splitlines()
        fields = lines[0].split()
        if len(fields) != 3:
            raise ValueError('Malformed probe marker')
        node, name, sample = fields
        sample = int(sample)
        value = next((int(line.strip(), 16) for line in lines[1:]
                      if re.fullmatch(r'0x[0-9a-fA-F]+', line.strip())), None)
        key = node + '.' + name
        if value is None or sample in indexed.setdefault(key, {}):
            raise ValueError('Missing or duplicate probe value: ' + key)
        indexed[key][sample] = value
    samples = {}
    for key, values in indexed.items():
        if sorted(values) != list(range(len(values))):
            raise ValueError('Missing probe interval: ' + key)
        samples[key] = [values[i] for i in range(len(values))]
    windows = []
    for line in (directory / 'groundstation-host.log').read_text().splitlines():
        if ('sustained fill delivery failed {' in line
                or 'sustained fill delivery passed {' in line):
            windows.append(json.loads(line[line.index('{'):]))
    return {'samples_10s': samples, 'delivery_windows': windows}


def analyze(capture, expected_duration_ms=None):
    if expected_duration_ms is None:
        expected_duration_ms = capture.get('virtual_duration_ms')
    if expected_duration_ms is None:
        raise ValueError('Explicit virtual execution duration is required')
    samples = capture['samples_10s']
    windows = sorted(capture['delivery_windows'], key=lambda w: w['start_ms'])
    if not windows:
        raise ValueError('No completed delivery windows')
    source_arrays = [samples[key] for key in SOURCES]
    duration = len(source_arrays[0]) * 10000
    if duration < 60000 or any(len(a) * 10000 != duration for a in source_arrays):
        raise ValueError('Need matching producer probes for at least 60 seconds')
    if expected_duration_ms is not None and duration != expected_duration_ms:
        raise ValueError('Producer probe cadence does not match execution duration')
    required = max(1, duration // 10000 - 3)  # One final scheduling boundary allowed.
    if len(windows) < required:
        raise ValueError('Incomplete delivery window coverage')
    reports = []
    for index, window in enumerate(windows):
        start, elapsed = window['start_ms'], window['elapsed_ms']
        if start != 20000 + index * 10000 or elapsed != 10000:
            raise ValueError('Missing, duplicate or noncontiguous delivery window')
        if window['channels'] != CHANNELS:
            raise ValueError('Unexpected channel ordering')
        first, last = start // 10000 - 1, (start + elapsed) // 10000 - 1
        if last >= len(source_arrays[0]):
            raise ValueError('Delivery window has no matching producer probe')
        received, gaps = window['received'], window['max_gap_ms']
        if len(received) != 5 or len(gaps) != 5 or any(n < 0 for n in received + gaps):
            raise ValueError('Invalid delivery counters')
        produced = [(a[last] - a[first]) & 0xffffffff for a in source_arrays]
        valid_source = all(0 < n <= elapsed // 2 for n in produced)
        # Require >=99.5% of actual publications, not 95% of a configured cap.
        # A small upper bound also rejects inflated/duplicated receive counts.
        preserved = valid_source and all(
            received[i] * 1000 >= produced[i] * 995
            and received[i] * 1000 <= produced[i] * 1005 + 2000
            for i in range(2))
        fresh = all(gap <= maximum for gap, maximum in zip(gaps, MAX_GAPS))
        valve_rate = all(n * 1000000 >= 900 * elapsed for n in received[2:])
        reports.append({'start_ms': start, 'produced': produced,
                        'received': received, 'max_gap_ms': gaps,
                        'delivery_percent': [round(100 * received[i] / n, 4) if n else None
                                             for i, n in enumerate(produced)],
                        'source_hz': [n * 1000 / elapsed for n in produced],
                        'published_samples_preserved': preserved,
                        'freshness_passed': fresh, 'valve_rate_passed': valve_rate,
                        'passed': preserved and fresh and valve_rate,
                        'nominal_rate_diagnostic_passed': window['passed']})
    if windows[-1]['start_ms'] + 10000 < duration - 10000:
        raise ValueError('Delivery evidence ended before the final boundary')
    drops = samples['gateway.can_rx_ring_drops']
    if len(drops) * 10000 != duration:
        raise ValueError('CAN probe coverage does not match producer coverage')
    stable_can = all(value == drops[1] for value in drops[1:])
    zero_counters = {}
    for key in REQUIRED_ZERO:
        values = samples[key]
        if len(values) * 10000 != duration:
            raise ValueError('Incomplete counter coverage: ' + key)
        zero_counters[key] = all(value == 0 for value in values)
    for key, values in samples.items():
        if key.endswith(('.panics', '.tlsf_failures', '.can_fifo_overflow_fdcan1', '.can_fifo_overflow_fdcan2')):
            zero_counters[key] = len(values) * 10000 == duration and all(v == 0 for v in values)
    drop_free = all(value == 0 for value in drops)
    passed = drop_free and stable_can and all(zero_counters.values()) and all(w['passed'] for w in reports)
    return {'passed': passed, 'scope': 'network preservation and freshness; not ADC rate or physical SD qualification',
            'minimum_delivery_percent': 99.5, 'warmup_ms': 20000,
            'duration_ms': duration, 'windows': reports,
            'can_drops_stable_after_warmup': stable_can, 'can_drop_free_entire_run': drop_free,
            'startup_can_drops': drops[1], 'final_can_drops': drops[-1],
            'zero_counters': zero_counters}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--capture', type=Path, help='Previously extracted capture JSON')
    source.add_argument('--diagnostics', type=Path, help='Completed simulator diagnostics directory')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--duration-ms', type=int, help='Expected virtual execution duration; rejects mismatched probe cadence')
    args = parser.parse_args()
    capture = json.loads(args.capture.read_text()) if args.capture else load_diagnostics(args.diagnostics)
    report = analyze(capture, expected_duration_ms=args.duration_ms)
    text = json.dumps(report, indent=2) + '\n'
    if args.output:
        args.output.write_text(text)
    print(text, end='')
    raise SystemExit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()
