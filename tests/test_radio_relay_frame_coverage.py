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

    def test_empty_scope_cannot_pass(self):
        with self.assertRaises(ValueError):
            COVERAGE.match([(100_000,7,b'proof')],[],100,150)
