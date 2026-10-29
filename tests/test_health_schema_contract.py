import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/health_schema_contract.py'
SPEC = importlib.util.spec_from_file_location('health_schema_contract', SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class HealthSchemaContractTests(unittest.TestCase):
    def check(self, change=None):
        schema = {'types': [{'name': name, 'reliable': 'Unordered',
            'element': {'kind': 'Static', 'data_type': 'Float32', 'count': 1}}
            for name in MODULE.HEALTH_TYPES]}
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for index in range(8):
                value = copy.deepcopy(schema)
                if index == 7 and change:
                    change(value)
                path = Path(directory) / f'peer-{index}.json'
                path.write_text(json.dumps(value))
                paths.append(path)
            return MODULE.validate_schemas(paths)

    def test_cohort_with_matching_delivery_contracts(self):
        self.assertEqual(self.check()['schemas'], 8)

    def test_old_peer_cannot_silently_revert_producer_delivery(self):
        with self.assertRaisesRegex(ValueError, 'peer-7.*BATTERY_VOLTAGE.*every peer'):
            self.check(lambda schema: schema['types'][0].update(reliable='Off'))

    def test_missing_or_incompatible_samples_fail(self):
        with self.assertRaisesRegex(ValueError, 'missing BATTERY_CURRENT'):
            self.check(lambda schema: schema['types'].pop())
        with self.assertRaisesRegex(ValueError, 'one Float32'):
            self.check(lambda schema: schema['types'][0]['element'].update(count=4))

    def test_peer_cannot_promote_health_reports_into_interactive_control_band(self):
        with self.assertRaisesRegex(ValueError, 'telemetry priority 0'):
            self.check(lambda schema: schema['types'][0].update(priority=254))
