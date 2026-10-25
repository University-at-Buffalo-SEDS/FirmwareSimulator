import importlib.util
from pathlib import Path
import unittest
import tempfile
p=Path(__file__).resolve().parents[1]/'scripts/test-operational-gse.py'
spec=importlib.util.spec_from_file_location('fixture',p);fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
class Contracts(unittest.TestCase):
 def test_builder_requires_ordinary_policy_features(self):
  recipe=(p.parents[1]/'Dockerfile.operational-sequences').read_text();fixture.ordinary_recipe(recipe)
  self.assertIn('COPY sim/renode/peripherals/SedsSpiSensors.cs /opt/firmware-sim/renode/peripherals/SedsSpiSensors.cs',recipe)
  for features in ['hitl_mode','test_fire_mode,firmware_sim,testing','test_fire_mode','test_fire_mode,firmware_sim,hitl_mode']:
   with self.assertRaises(ValueError):fixture.ordinary_recipe(recipe.replace('--release --features test_fire_mode,firmware_sim','--release --features '+features))
 def test_flight_builder_retains_ordinary_policy_and_requires_full_radio_cohort(self):
  recipe=(p.parents[1]/'Dockerfile.operational-flight').read_text()
  fixture.ordinary_recipe(recipe,flight=True)
  for model in ['SedsStm32System.cs','SedsStm32H5Rtc.cs']:
   self.assertIn('COPY sim/renode/peripherals/'+model+' /opt/firmware-sim/renode/peripherals/'+model,recipe)
  self.assertIn('COPY sim/renode/platforms/stm32h523.repl /opt/firmware-sim/renode/platforms/stm32h523.repl',recipe)
  with self.assertRaises(ValueError):fixture.ordinary_recipe(recipe,flight=False)
  for features in ['testing,firmware_sim','hitl_mode,firmware_sim','test_fire_mode,firmware_sim']:
   with self.assertRaises(ValueError):fixture.ordinary_recipe(recipe.replace('--release --features firmware_sim','--release --features '+features),flight=True)
 def test_flight_proof_requires_actual_evaluation_not_just_command_acceptance(self):
  with tempfile.TemporaryDirectory()as root:
   d=Path(root)/'bay-live-test';d.mkdir();p=d/'memory-probes.txt'
   fields={'fc_commands_received':1,'fc_commands_accepted':1,'fc_commands_processed':1,'fc_last_command_id':1,'fc_config_flags':0,'fc_state':0,'fc_watchdog_resume_rejected':0}
   def write():p.write_text(''.join('SEDS_BAY_PROBE flight '+k+' 1\n0x%08x\n'%v for k,v in fields.items()))
   write();self.assertIsNone(fixture.flight_launch_evidence(Path(root)))
   fields['fc_config_flags']=0x200;fields['fc_state']=3;write()
   self.assertEqual(fixture.flight_launch_evidence(Path(root))['fc_state'],3)
   fields['fc_watchdog_resume_rejected']=1;write();self.assertIsNone(fixture.flight_launch_evidence(Path(root)))
   fields['fc_watchdog_resume_rejected']=2;write();self.assertIsNone(fixture.flight_launch_evidence(Path(root)))
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
