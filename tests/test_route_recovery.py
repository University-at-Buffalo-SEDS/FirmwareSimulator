import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location("recovery", Path(__file__).parents[1] / "scripts/test-route-recovery.py")
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)

class RouteRecoveryTests(unittest.TestCase):
    def test_restart_regression_does_not_replace_ten_minute_qualification(self):
        for flag, duration in (("--restart-regression", "120000"), ("--ultra-soak", "600000")):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as tmp:
                runs = []
                suite = SimpleNamespace(load_layout_for_build=mock.Mock())
                suite.run_network_simulation = lambda *args, **kwargs: runs.append(
                    (kwargs.get("ultra_soak", False), recovery.os.environ.get("SEDS_FIRMWARE_SIM_SOAK_MS")))
                specification = SimpleNamespace(loader=mock.Mock())
                with mock.patch.object(recovery.importlib.util, "spec_from_file_location", return_value=specification), \
                     mock.patch.object(recovery.importlib.util, "module_from_spec", return_value=suite), \
                     mock.patch.dict(recovery.os.environ, {}, clear=True), \
                     mock.patch("sys.argv", ["test-route-recovery", "--workspace", tmp, "--image", "candidate", flag]):
                    recovery.main()
                self.assertEqual(runs, [(False, None), (True, duration)])

    def test_fault_preserves_end_to_end_checks_and_requires_failed_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = dict(sample_count=10, host_nodes=[dict(env={})], assertions=[dict(name="original command bound")],
                            host_log_assertions=[dict(contains="matching state received")])
            (root / "topology.json").write_text(json.dumps(original))
            (root / "valve.json").write_text(json.dumps(dict(execution=dict(memory_probes=[
                dict(name="umbilical_status_fail", symbol="g_sim_umbilical_status_fail", maximum=0)]))))
            (root / "gateway.json").write_text(json.dumps(dict(execution=dict(memory_probes=[]))))
            (root / "actuator.json").write_text((root / "valve.json").read_text())
            recovery.configure_fault(root, "valve")
            topology = json.loads((root / "topology.json").read_text())
            self.assertEqual(topology["assertions"][0], original["assertions"][0])
            self.assertEqual(topology["host_log_assertions"][0], original["host_log_assertions"][0])
            self.assertEqual(len(topology["host_log_assertions"]), 7)
            self.assertEqual(topology["host_nodes"][0]["env"]["GS_SIM_SOAK_COMMAND_SAMPLES"], "4,6,8")
            self.assertEqual(topology["host_nodes"][0]["env"]["GS_SIM_VALIDATE_SOAK_COMMANDS"], "1")
            self.assertFalse(topology["can_link_events"][0]["connected"])
            self.assertTrue(topology["can_link_events"][1]["connected"])
            self.assertEqual(topology["can_link_events"][0]["node"], "gateway")
            self.assertEqual(topology["assertions"][1]["maximum_gain"], 0)
            self.assertEqual(topology["assertions"][1]["from_sample"], 3)
            self.assertEqual(topology["assertions"][2]["node"], "actuator")
            self.assertEqual(topology["assertions"][2]["maximum_gain"], 0)
            missing_route = next(a for a in topology["assertions"] if a.get("probe") == "status_report_failures")
            self.assertEqual(missing_route["minimum"], 1)
            self.assertEqual(missing_route["sample"], 2)
            self.assertEqual(topology["assertions"][-1]["maximum"], 0)
            self.assertEqual(topology["assertions"][-1]["sample"], 9)

if __name__ == "__main__":
    unittest.main()
