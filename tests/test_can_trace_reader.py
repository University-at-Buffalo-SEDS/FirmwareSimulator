import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('can_reader', Path(__file__).resolve().parents[1] / 'scripts/analyze-can-trace.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class CanTraceReaderTests(unittest.TestCase):
    def capture(self, directory, frames, **changes):
        path = Path(directory) / 'wire.bin'
        until = 10000000
        content = M.HEADER.pack(b'SEDSCAN1', 1, 80, until, M.MAX_BYTES)
        for now, data in frames:
            content += M.RECORD.pack(now, 6, 1, len(data), 0, data.ljust(64, b'\0'))
        path.write_bytes(content)
        status = dict(version=1, record_bytes=80, records=len(frames), until_us=until,
                      max_bytes=M.MAX_BYTES, last_record_us=frames[-1][0] if frames else 0,
                      finished=True, capped=False, faulted=False)
        status.update(changes)
        Path(str(path) + '.status.json').write_text(json.dumps(status))
        return path

    def test_raw_full_and_compact_have_distinct_taxonomy(self):
        for data, expected in [(b'\1\0\x7fdata', ('canonical', 127)),
                               (b'SDT\1\x81\1\1\0\x7fdata', ('sdt-full', 127)),
                               (b'SDT\2anything', ('sdt-kind-2-undecoded', None)),
                               (b'SDT\1\x80', ('malformed', None))]:
            self.assertEqual(M.taxonomy(data), expected)

    def test_reorders_fragments_preserves_duplicates_and_exact_bytes(self):
        payload = b'\1\0\x7f' + bytes(range(80))
        def frag(i):
            return M.FRAGMENT.pack(b'SD', 2, 9, i, 2, 1 if i == 0 else 2, len(payload)) + payload[i*55:(i+1)*55]
        with tempfile.TemporaryDirectory() as tmp:
            rows = []
            path = self.capture(tmp, [(1, frag(1)), (2, frag(1)), (3, frag(0))])
            result = M.read_capture(path, rows.append)
        self.assertEqual(result['logical_packets'], 1)
        self.assertEqual(result['fragment_duplicates'], 1)
        self.assertEqual(rows[0]['bytes_hex'], payload.hex())
        self.assertEqual(result['data_types'], {'127': 1})

    def test_rejects_incomplete_faulted_or_mismatched_status(self):
        for changes in [dict(finished=False), dict(capped=True), dict(faulted=True), dict(records=3)]:
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError):
                    M.read_capture(self.capture(tmp, [(1, b'\1\0\1')], **changes))

    def test_truncated_capture_and_fragment_conflict_are_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.capture(tmp, [(1, b'\1\0\1')])
            path.write_bytes(path.read_bytes()[:-1])
            with self.assertRaises(ValueError): M.read_capture(path)
        fragment = M.FRAGMENT.pack(b'SD', 2, 1, 0, 2, 0, 60)
        with tempfile.TemporaryDirectory() as tmp:
            result = M.read_capture(self.capture(tmp, [(1, fragment + b'A'*55), (2, fragment + b'B'*55)]))
        self.assertEqual(result['fragment_rejected'], 1)
        self.assertEqual(result['logical_packets'], 0)


if __name__ == '__main__':
    unittest.main()
