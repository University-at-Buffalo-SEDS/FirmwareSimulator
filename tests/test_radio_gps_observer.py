"""GPS qualification must reject satellite-only/stale/manual/wrong-position evidence."""
import importlib.util
from pathlib import Path
import unittest,json,tempfile
SPEC=importlib.util.spec_from_file_location('gps_observer',Path(__file__).resolve().parents[1]/'scripts/radio-gps-observer.py')
MODULE=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(MODULE)
class ActualGpsProofTests(unittest.TestCase):
    def test_frozen_layout_sets_oracle_and_old_fix_is_rejected(self):
        with tempfile.TemporaryDirectory()as tmp:
            path=Path(tmp)/'board.json';path.write_text(json.dumps({'peripherals':[{'model':'neo_m9n','position':{'latitude':31.758,'longitude':-102.046,'altitude_m':890.016}}]}))
            expected=MODULE.fixture_position(path)
        fix=dict(source='RF',manual=False,stale=False,latitude=31.758,longitude=-102.046,altitude_m=890.016,age_ms=750)
        self.assertFalse(MODULE.valid_fix(fix))
        rows=[{'virtual_ms':10000,'primary':fix},{'virtual_ms':20000,'primary':fix}]
        MODULE.verify_rows(rows,20000,['primary'],expected)
        with self.assertRaises(RuntimeError):MODULE.verify_rows([rows[0]],20000,['primary'],expected)
        skipped=[rows[0],dict(rows[1],virtual_ms=30000)]
        with self.assertRaises(RuntimeError):MODULE.verify_rows(skipped,30000,['primary'],expected)
        repeated=[rows[0],{'virtual_ms':20000,'primary':dict(fix,age_ms=10750)}]
        with self.assertRaises(RuntimeError):MODULE.verify_rows(repeated,20000,['primary'],expected)
    def test_only_planned_primary_restart_boundary_may_be_unavailable(self):
        fix=dict(source='RF',manual=False,stale=False,latitude=43.,longitude=-78.78,altitude_m=300.,age_ms=750)
        rows=[{'virtual_ms':10000,'primary':fix,'rrls':fix},{'virtual_ms':20000,'primary':None,'rrls':fix},{'virtual_ms':30000,'primary':fix,'rrls':fix}]
        MODULE.verify_rows(rows,30000,['primary','rrls'],{},20000)
        with self.assertRaises(RuntimeError):MODULE.verify_rows(rows,30000,['primary','rrls'],{})
        with self.assertRaises(RuntimeError):MODULE.verify_rows([dict(row,rrls=None)if row['virtual_ms']==20000 else row for row in rows],30000,['primary','rrls'],{},20000)
        with self.assertRaises(RuntimeError):MODULE.verify_rows(rows,30000,['primary','rrls'],{},10000)
    def test_primary_only_is_not_duplicated_as_rrls_proof(self):
        self.assertEqual(MODULE.station_ports(1234),[('primary',1234)])
        self.assertEqual(MODULE.station_ports(1234,5678),[('primary',1234),('rrls',5678)])
    def test_fresh_actual_rf_coordinates_required(self):
        fix=dict(source='RF',manual=False,stale=False,latitude=43.,longitude=-78.78,altitude_m=300.,age_ms=750)
        self.assertTrue(MODULE.valid_fix(fix))
        for bad in (None,{'satellites':12},dict(fix,source='FC'),dict(fix,manual=True),dict(fix,stale=True),dict(fix,latitude=float('nan')),dict(fix,longitude=0),dict(fix,altitude_m=None),dict(fix,age_ms=5001)):
            with self.subTest(fix=bad):self.assertFalse(MODULE.valid_fix(bad))
