import importlib.util
from pathlib import Path
import unittest
p=Path(__file__).resolve().parents[1]/'scripts/test-operational-gse.py'
spec=importlib.util.spec_from_file_location('fixture',p);fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
class Contracts(unittest.TestCase):
 def test_builder_requires_ordinary_policy_features(self):
  recipe=(p.parents[1]/'Dockerfile.operational-sequences').read_text();fixture.ordinary_recipe(recipe)
  for features in ['hitl_mode','test_fire_mode,firmware_sim,testing','test_fire_mode','test_fire_mode,firmware_sim,hitl_mode']:
   with self.assertRaises(ValueError):fixture.ordinary_recipe(recipe.replace('--release --features test_fire_mode,firmware_sim','--release --features '+features))
 def test_assumed_plant_uses_only_actual_valve_inputs(self):
  self.assertEqual(fixture.plant_pressure(0,200,[False,False,False,True,False]),7)
  self.assertEqual(fixture.plant_pressure(40,200,[False,True,False,True,False]),0)
  self.assertEqual(fixture.plant_pressure(50,100,[False]*5),50)
  self.assertEqual(fixture.plant_pressure(50,0,[False,False,False,True,False]),50)
  self.assertEqual(fixture.plant_pressure(50,200,[None]*5),50)
 def test_adc_fixture_units_and_bounds(self):
  self.assertEqual(fixture.raw_pressure(0),0);self.assertEqual(fixture.raw_pressure(2000),3103)
  self.assertEqual(fixture.raw_pressure(-1),0);self.assertEqual(fixture.raw_pressure(9999),4095)
 def test_mass_fixture_matches_actual_daq_historical_units(self):
  code=fixture.raw_loadcell_kg(5)
  calibrated=code*2.2104/16777216*2/16*100
  self.assertAlmostEqual(calibrated,5,places=5)
  samples=list(map(int,fixture.mass_samples(5).split(',')))
  self.assertEqual(len(samples),16);self.assertEqual(samples[1],code);self.assertEqual(samples[12],305652)
if __name__=='__main__':unittest.main()
