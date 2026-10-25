import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('radio_trace', ROOT / 'scripts/inspect-radio-trace.py')
TRACE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TRACE)


class RadioTraceTests(unittest.TestCase):
    def fixture(self, root, records=((0, 42), (100, 43)), capped=False, finished=True):
        path = Path(root) / 'air-station-1-tx.bin'
        path.write_bytes(TRACE.HEADER.pack(b'SEDSRBT1', 1, 9, 1, 0, 1000)
                         + b''.join(TRACE.RECORD.pack(at, value) for at, value in records))
        Path(str(path) + '.status.json').write_text(json.dumps({
            'version': 1, 'max_bytes': TRACE.MAX_BYTES, 'header_bytes': 32, 'record_bytes': 9,
            'records': len(records), 'until_us': 1000, 'last_record_us': records[-1][0] if records else 0,
            'capped': capped, 'finished': finished}))
        return path

    def test_raw_bytes_timestamps_and_reconciliation(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.fixture(root)
            summary, raw = TRACE.inspect(path)
            self.assertEqual(raw, b'\x2a\x2b')
            self.assertEqual((summary['station'], summary['direction'], summary['first_us'], summary['last_us']),
                             (1, 'tx', 0, 100))
            meta = Path(str(path) + '.status.json')
            status = json.loads(meta.read_text())
            status['records'] = 3
            meta.write_text(json.dumps(status))
            with self.assertRaisesRegex(ValueError, 'reconcile'):
                TRACE.inspect(path)

    def test_cap_or_unfinished_trace_never_silently_passes(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.fixture(root, capped=True)
            with self.assertRaisesRegex(ValueError, 'cap'):
                TRACE.inspect(path)
            self.assertTrue(TRACE.inspect(path, allow_capped=True)[0]['capped'])
            path = self.fixture(root, finished=False)
            with self.assertRaisesRegex(ValueError, 'finish'):
                TRACE.inspect(path)
            self.assertFalse(TRACE.inspect(path, allow_incomplete=True)[0]['finished'])

    def test_truncated_reversed_and_out_of_scope_records_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.fixture(root)
            path.write_bytes(path.read_bytes()[:-1])
            with self.assertRaisesRegex(ValueError, 'truncated'):
                TRACE.inspect(path)
            for records in [((100, 1), (50, 2)), ((1001, 1),)]:
                path = self.fixture(root, records)
                with self.assertRaisesRegex(ValueError, 'timestamp'):
                    TRACE.inspect(path)

    def test_explicit_bounded_clock_backstep_preserves_bytes_and_is_reported(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.fixture(root, records=((100,1),(90,2),(200,3)))
            with self.assertRaisesRegex(ValueError,'reversed'):
                TRACE.inspect(path)
            result,raw = TRACE.inspect(path,max_clock_backstep_us=100)
            self.assertEqual(raw,b'\x01\x02\x03')
            self.assertFalse(result['fully_monotonic'])
            self.assertEqual(result['backward_timestamp_count'],1)
            self.assertEqual(result['max_clock_backstep_us'],10)
            with self.assertRaises(ValueError):
                TRACE.inspect(path,max_clock_backstep_us=101)
            path = self.fixture(root,records=((200,1),(99,2)))
            with self.assertRaisesRegex(ValueError,'reversed'):
                TRACE.inspect(path,max_clock_backstep_us=100)

    def test_missing_metadata_or_invalid_identity_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.fixture(root)
            blob = bytearray(path.read_bytes())
            struct.pack_into('<I', blob, 16, 16)
            path.write_bytes(blob)
            with self.assertRaisesRegex(ValueError, 'header'):
                TRACE.inspect(path)
            path = self.fixture(root)
            Path(str(path) + '.status.json').unlink()
            with self.assertRaises(OSError):
                TRACE.inspect(path)


if __name__ == '__main__':
    unittest.main()
