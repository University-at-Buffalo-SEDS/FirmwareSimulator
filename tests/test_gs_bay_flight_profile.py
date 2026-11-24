"""gs_bay.py seeds the plant from GroundStation26 tools/flight_profile/vehicle.json, the run tank included."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('gs_bay', ROOT / 'scripts/gs_bay.py')
gs_bay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gs_bay)


class PlantFromFlightProfile(unittest.TestCase):
    def test_run_tank_volume_and_mass_come_from_vehicle_json(self):
        with tempfile.TemporaryDirectory() as d:
            vehicle = {"vehicle": {"dry_mass_kg": 33.2, "run_tank_volume_l": 15.8537,
                                   "mass_breakdown_kg": {"nose": 1.8, "n2o_run_tank_al6061_15p85L_with_bulkheads": 8.6}}}
            (Path(d) / "vehicle.json").write_text(json.dumps(vehicle))
            plant, info = gs_bay.plant_from_flight_profile(Path(d))
        self.assertAlmostEqual(plant["fluids"]["tank_volume_m3"], 0.0158537)
        self.assertAlmostEqual(plant["fluids"]["tank_wall_kg"], 8.6)
        self.assertAlmostEqual(plant["vehicle"]["dry_mass_kg"], 33.2)
        self.assertAlmostEqual(info["run_tank_volume_l"], 15.8537)

    def test_without_a_tank_volume_the_plant_keeps_its_default(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "vehicle.json").write_text(json.dumps({"vehicle": {"dry_mass_kg": 30.0}}))
            plant, info = gs_bay.plant_from_flight_profile(Path(d))
        self.assertEqual(plant["fluids"], {})
        self.assertNotIn("run_tank_volume_l", info)

    def test_the_hybrid_grain_comes_from_vehicle_json_when_named(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "vehicle.json").write_text(json.dumps({"motor": {"hybrid_grain": "cylindrical_preset"}}))
            plant, info = gs_bay.plant_from_flight_profile(Path(d))
        self.assertEqual(plant["motor"]["hybrid_grain"], "cylindrical_preset")
        self.assertEqual(info["hybrid_grain"], "cylindrical_preset")
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "vehicle.json").write_text(json.dumps({"motor": {}}))
            plant, _ = gs_bay.plant_from_flight_profile(Path(d))
        self.assertNotIn("hybrid_grain", plant["motor"])

    def test_attitude_and_launcher_come_from_vehicle_json_for_weathercocking(self):
        with tempfile.TemporaryDirectory() as d:
            vehicle = {"vehicle": {"length_m": 4.38658, "cn_alpha_per_rad": 9.28, "static_margin_cal": 3.49,
                                   "pitch_inertia_factor": 0.85},
                       "launch": {"rail_length_m": 5.18, "tilt_deg": 5.0, "azimuth_deg": 60.0, "rail_friction": 0.03},
                       "attitude": {"pitch_damping_ratio": 0.08}}
            (Path(d) / "vehicle.json").write_text(json.dumps(vehicle))
            plant, _ = gs_bay.plant_from_flight_profile(Path(d))
        self.assertEqual(plant["vehicle"], {"length_m": 4.38658, "cn_alpha_per_rad": 9.28, "static_margin_cal": 3.49,
                                            "pitch_inertia_factor": 0.85, "pitch_damping_ratio": 0.08,
                                            "rail_length_m": 5.18, "rail_tilt_deg": 5.0, "rail_azimuth_deg": 60.0,
                                            "rail_friction": 0.03})

    def test_the_groundstation_vehicle_json_carries_the_measured_tank(self):
        gs = ROOT.parent / "GroundStation26" / "tools" / "flight_profile"
        if not (gs / "vehicle.json").exists():
            self.skipTest("no GroundStation26 checkout beside this one")
        plant, _ = gs_bay.plant_from_flight_profile(gs)
        if "tank_volume_m3" not in plant["fluids"]:
            self.skipTest("this GroundStation26 predates run_tank_volume_l")
        # 967.45 in^3, and native_physics.rs RUN_TANK_VOLUME_M3 is the same number
        self.assertAlmostEqual(plant["fluids"]["tank_volume_m3"], 967.45 * 0.016387064e-3, places=7)


if __name__ == "__main__":
    unittest.main()
