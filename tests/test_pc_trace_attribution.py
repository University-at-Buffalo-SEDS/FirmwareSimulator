import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('pc_attribution', ROOT / 'scripts/analyze-pc-trace.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PcAttributionTests(unittest.TestCase):
    def test_unsized_assembly_is_counted_and_same_address_aliases_do_not_hide_it(self):
        ranges = module.symbol_ranges([
            '08001000 00000010 T coded', '08001010 T assembly_wait',
            '08001010 T assembly_wait_alias', '08001020 00000004 T following'])
        profile = module.attribute(['0x08001001: nop', '0x08001004: mov',
                                    '0x08001011: wfe', '0x0800101e: b', '0x08001026: nop'], ranges)
        counts = {item['name']: item['instructions'] for item in profile['top_functions']}
        self.assertEqual(counts, {'coded': 2, 'assembly_wait': 2})
        self.assertEqual(profile['instructions'], 5)
        self.assertEqual(profile['unmapped'], 1)
        self.assertEqual(profile['inferred_assembly_ranges'][0]['aliases'], ['assembly_wait', 'assembly_wait_alias'])

    def test_unsized_final_label_does_not_absorb_unknown_addresses(self):
        ranges = module.symbol_ranges(['08001000 T final_label'])
        profile = module.attribute(['0x08001000', '0x08001004', '0x20000000'], ranges)
        self.assertEqual(profile['unmapped'], 2)

    def test_empty_symbols_or_undecoded_trace_fail_instead_of_reporting_zero_load(self):
        with self.assertRaises(ValueError):
            module.symbol_ranges(['00001000 D data'])
        with self.assertRaises(ValueError):
            module.attribute(['decoder error'], module.symbol_ranges(['08001000 T code']))


if __name__ == '__main__':
    unittest.main()
