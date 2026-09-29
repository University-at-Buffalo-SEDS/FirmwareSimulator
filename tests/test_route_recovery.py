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
    def test_command_counter_reset_still_requires_new_execution_and_host_ack(self):
        for nodes, reset in ((["valve"], True), (["groundstation"], False)):
            with self.subTest(nodes=nodes), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                checks = [dict(name=f"command interval {sample}", node="valve",
                               probe="valve_commands_executed", minimum_gain=1,
                               from_sample=sample-1, to_sample=sample)
                          for sample in (7, 8, 9)]
                host = [dict(name="fresh state return", contains="round 7/10, sample 8,",
                             minimum_occurrences=1)]
                (root / "topology.json").write_text(json.dumps(dict(
                    sample_count=12, assertions=checks, host_log_assertions=host)))
                recovery.configure_restarts(root, nodes, [8])
                actual = json.loads((root / "topology.json").read_text())
                self.assertEqual(actual["host_log_assertions"], host)
                self.assertEqual(actual["assertions"][0], checks[0])
                self.assertEqual(actual["assertions"][2], checks[2])
                if reset:
                    boundary = actual["assertions"][1]
                    self.assertEqual(boundary["minimum"], 1)
                    self.assertEqual(boundary["sample"], 8)
                    self.assertNotIn("minimum_gain", boundary)
                    self.assertNotIn("from_sample", boundary)
                    # A healthy 9 -> 3 reset passes, a stuck 9 -> 0 does not.
                    self.assertGreaterEqual(3, boundary["minimum"])
                    self.assertLess(0, boundary["minimum"])
                else:
                    self.assertEqual(actual["assertions"], checks)

    def test_host_only_restarts_keep_boards_powered_and_require_each_rediscovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = dict(sample_count=12, reboots=[dict(node="rf", after_sample=8)],
                can_ack_events=[dict(node="rf", after_sample=3, acknowledged=False)],
                assertions=[dict(name="command latency unchanged", maximum=2500),
                            dict(name="flight restored flash at boot", node="flight", minimum=1)],
                host_log_assertions=[dict(name="GroundStation discovered every board by autonomous name",
                                          contains="AB,DAQ,FC,GB,PB,RF,VB", minimum_occurrences=2),
                                     dict(name="Every command returns state", minimum_occurrences=20)])
            (root / "topology.json").write_text(json.dumps(original))
            recovery.configure_restarts(root, ["groundstation"], [4, 8])
            actual = json.loads((root / "topology.json").read_text())
            self.assertEqual(actual["reboots"], [dict(node="groundstation", after_sample=s) for s in (4, 8)])
            self.assertEqual(actual["host_log_assertions"][0]["minimum_occurrences"], 3)
            self.assertEqual(actual["host_log_assertions"][1], original["host_log_assertions"][1])
            self.assertEqual(actual["assertions"], original["assertions"][:1])
            self.assertEqual(actual["can_ack_events"], original["can_ack_events"])

    def test_firmware_group_restart_does_not_restart_groundstation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "valve.json").write_text(json.dumps(dict(execution=dict(memory_probes=[
                dict(name="umbilical_status_fail", maximum=0),
                dict(name="allocation_failures", maximum=0)]))))
            (root / "topology.json").write_text(json.dumps(dict(sample_count=12,
                host_log_assertions=[dict(name="GroundStation network graph labelled every board with its own traffic",
                                          minimum_occurrences=2)])))
            recovery.configure_restarts(root, ["gateway", "valve", "actuator", "daq"])
            actual = json.loads((root / "topology.json").read_text())
            self.assertEqual({e["node"] for e in actual["reboots"]}, {"gateway", "valve", "actuator", "daq"})
            self.assertTrue(all(e["after_sample"] == 8 for e in actual["reboots"]))
            self.assertEqual(actual["host_log_assertions"][0]["minimum_occurrences"], 1)
            probes = json.loads((root / "valve.json").read_text())["execution"]["memory_probes"]
            self.assertNotIn("maximum", probes[0])
            self.assertEqual(probes[1]["maximum"], 0)
            self.assertEqual(actual["assertions"][0]["sample"], 7)
            self.assertEqual(actual["assertions"][1]["from_sample"], 8)
            self.assertEqual(actual["assertions"][1]["to_sample"], 11)
            self.assertEqual(actual["assertions"][1]["maximum_gain"], 0)
            self.assertEqual(actual["assertions"][2]["probe"], "status_report_pending")
            with self.assertRaises(ValueError):
                recovery.configure_restarts(root, ["gateway"], [11])
            (root / "topology.json").write_text(json.dumps(dict(sample_count=12,
                host_log_assertions=[], assertions=[
                    dict(name="flight restored flash at boot", node="flight", minimum=1),
                    dict(name="flight persistence errors", node="flight", maximum=0)])))
            recovery.configure_restarts(root, ["flight"])
            self.assertEqual(len(json.loads((root / "topology.json").read_text())["assertions"]), 2)
            for samples in ([], [0], [12], [8, 4], [4, 4]):
                with self.subTest(samples=samples), self.assertRaises(ValueError):
                    recovery.configure_restarts(root, ["groundstation"], samples)

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
