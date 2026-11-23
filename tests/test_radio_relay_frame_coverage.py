import importlib.util
from pathlib import Path
import unittest

SPEC=importlib.util.spec_from_file_location('frame_coverage',Path(__file__).resolve().parents[1]/'scripts/diagnostics/relay_frame_coverage.py')
COVERAGE=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COVERAGE)


class FrameCoverageTests(unittest.TestCase):
    def test_exact_bytes_direction_and_occurrences_are_required(self):
        incoming=[(100_000,None,b'abc'),(110_000,None,b'abc'),(120_000,2,b'xyz')]
        outgoing=[(90_000,1,b'abc'),(105_000,1,b'abc'),(125_000,1,b'xyz'),(130_000,5,b'abc')]
        result=COVERAGE.match(incoming,outgoing,100,150)
        self.assertEqual(result['delivered_inputs'],1)
        self.assertEqual(len(result['missing']),2)
        outgoing += [(140_000,1,b'abc'),(150_000,5,b'xyz')]
        result=COVERAGE.match(incoming,outgoing,100,150)
        self.assertEqual(result['result'],'PASS')
        self.assertEqual(result['by_output_kind'],{'1':2,'5':1})
        self.assertEqual(result['maximum_forwarding_age_ms'],30)

    def test_rr_v2_envelopes_unwrap_in_both_forms(self):
        import zlib
        crc=lambda data: zlib.crc32(data).to_bytes(4,'little')
        sdk=b'\x42frame'+crc(b'\x42frame')
        for kind in range(17):
            encoded=bytes([0xE0+kind])+b'body'
            self.assertEqual(COVERAGE.unwrap(encoded+crc(encoded)),(kind,b'body'))
            self.assertIsNone(COVERAGE.unwrap(encoded+bytes(4)))
        for marker,kind in COVERAGE.SELF_CHECKED.items():
            self.assertEqual(COVERAGE.unwrap(bytes([marker])+sdk),(kind,sdk))
            self.assertIsNone(COVERAGE.unwrap(bytes([marker])+sdk[:-1]+b'\x00'))
            for bit in range(8):
                flipped=bytes([marker^(1<<bit)])+sdk
                self.assertNotIn(flipped[0],COVERAGE.SELF_CHECKED)
                self.assertIsNone(COVERAGE.unwrap(flipped))
        for marker in (0xF1,0xF2,0xF4,0xFF,0x42):
            self.assertIsNone(COVERAGE.unwrap(bytes([marker])+sdk))
        self.assertIsNone(COVERAGE.unwrap(b'RR\x01\x05'+sdk+crc(b'RR\x01\x05'+sdk)))

    def test_empty_scope_cannot_pass(self):
        with self.assertRaises(ValueError):
            COVERAGE.match([(100_000,7,b'proof')],[],100,150)
