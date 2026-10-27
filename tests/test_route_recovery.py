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
    def test_policy_qualified_primary_only_keeps_original_bounds(self):
        topology={"host_nodes":[{"name":"groundstation","env":{"GS_SIM_FLIGHT_STATE_SEQUENCE":"1,0,1","GS_SIM_UNDERGLOW_SEQUENCE":"1,0,1","GS_SIM_FLIGHT_BUZZER_SEQUENCE":"1,0,1"}},{"name":"rrls","env":{"unchanged":"yes"}}],"host_log_assertions":[{"node":"groundstation","contains":"full-bay managed-variable latency within bound","minimum_occurrences":3},{"node":"groundstation","contains":"other gate","minimum_occurrences":1}]}
        recovery.configure_policy_qualified_controls(topology)
        env=topology["host_nodes"][0]["env"]
        for name in ["GS_SIM_POLICY_CORRECT_CONTROLS","GS_SIM_TRACE_API_REQUESTS","SEDSNET_TRACE_RELIABLE"]:self.assertEqual(env[name],"1")
        self.assertEqual(env["GS_FLIGHT_STATE_DEFAULT"],"0")
        self.assertEqual(env["GS_SIM_MANAGED_VARIABLE_MAX_LATENCY_MS"],"500")
        self.assertEqual(env["GS_SIM_FLIGHT_STATE_SEQUENCE"],"1,0,1")
        self.assertEqual(topology["host_nodes"][1]["env"],{"unchanged":"yes"})
        self.assertEqual(topology["host_log_assertions"][0]["minimum_occurrences"],3)
        self.assertEqual(topology["host_log_assertions"][1]["contains"],"other gate")
        with self.assertRaises(ValueError):recovery.configure_policy_qualified_controls({})

    def test_policy_failed_missing_trace_still_writes_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            result=recovery.audit_policy_evidence(directory)
            self.assertFalse(result["passed"])
            self.assertEqual(json.loads((Path(directory)/"policy-all-owner-audit.json").read_text())["passed"],False)

    def test_policy_audit_required_before_pass_preserves_original_failure(self):
        import subprocess
        failed=subprocess.CalledProcessError(7,["simulation"])
        for result in [{"passed":True},{"passed":False}]:
            with mock.patch.object(recovery,"audit_policy_evidence",return_value=result):
                self.assertIs(recovery.policy_validation_failure("unused",failed),failed)
                self.assertEqual(recovery.policy_validation_failure("unused",None) is None,result["passed"])
        with mock.patch.object(recovery,"audit_policy_evidence",side_effect=OSError("trace unreadable")):
            self.assertIs(recovery.policy_validation_failure("unused",failed),failed)
            self.assertIsInstance(recovery.policy_validation_failure("unused",None),OSError)

    def test_pressure_sd_gate_requires_full_rate_and_no_drops(self):
        for board,probe in [("ValveBoard26","pressure_sd_publish_drops"),("DAQ-Board","pressure_sd_dropped")]:
            layout={}
            recovery.configure_pressure_sd_probes(layout,board)
            probes=layout["execution"]["memory_probes"]
            self.assertEqual(next(p for p in probes if p["name"]==probe)["maximum"],0)
            recovery.configure_pressure_sd_probes(layout,board)
            self.assertEqual(len(layout["execution"]["memory_probes"]),len(probes))
        topology=dict(sample_count=12,virtual_time_ms=120000)
        recovery.configure_pressure_sd_assertions(topology)
        self.assertEqual(len(topology["assertions"]),56)
        self.assertEqual(len([a for a in topology["assertions"] if a["probe"]=="pressure_sd_written"]),11)
        self.assertEqual(topology["assertions"][-1]["probe"],"pressure_sd_flushed")
        self.assertEqual(topology["assertions"][-1]["minimum"],1)
        self.assertEqual(topology["assertions"][0]["minimum_gain"],2492)
        for invalid in [dict(sample_count=1,virtual_time_ms=10000),dict(sample_count=12,virtual_time_ms=600000)]:
            with self.assertRaises(ValueError):recovery.configure_pressure_sd_assertions(invalid)

    def test_pico_baud_follows_actual_gateway_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "CMakeCache.txt"
            for baud in (115200, 1000000):
                cache.write_text(f"GATEWAY_PICO_UART_BAUD_RATE:STRING={baud}\n")
                self.assertEqual(recovery.configured_pico_baud(cache), baud)
            for value in ("0", "", "abc"):
                cache.write_text(f"GATEWAY_PICO_UART_BAUD_RATE:STRING={value}\n")
                with self.assertRaises(ValueError):
                    recovery.configured_pico_baud(cache)

    def test_tlsf_keeps_generic_fault_and_memory_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "CMakeCache.txt"
            original = [dict(name="pool_low_water", symbol="g_telemetry_pool_low_water", minimum=1024),
                        dict(name="alloc_failures", symbol="g_telemetry_alloc_fail", maximum=0),
                        dict(name="panics", symbol="g_telemetry_panic_count", maximum=0),
                        dict(name="small_pool_available", symbol="g_telemetry_small_pool_available")]
            layout = dict(execution=dict(memory_probes=list(original)))
            recovery.configure_allocator_probes(layout, cache)
            self.assertEqual(layout["execution"]["memory_probes"], original)
            cache.write_text("TELEMETRY_USE_TLSF:BOOL=ON\n")
            recovery.configure_allocator_probes(layout, cache)
            probes = layout["execution"]["memory_probes"]
            self.assertEqual(probes[:3], original[:3])
            self.assertEqual(next(p for p in probes if p["name"] == "tlsf_failures")["maximum"], 0)
            self.assertEqual(next(p for p in probes if p["name"] == "tlsf_init_failed")["maximum"], 0)
            self.assertNotIn(original[3], probes)
            count = len(probes)
            recovery.configure_allocator_probes(layout, cache)
            self.assertEqual(len(probes), count)
            probes.append(dict(name="emergency_pool_available", minimum=1024))
            with self.assertRaisesRegex(ValueError, "qualification bound"):
                recovery.configure_allocator_probes(layout, cache)

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

    def test_soak_only_keeps_full_duration_and_does_not_run_short_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = []
            suite = SimpleNamespace(load_layout_for_build=mock.Mock())
            suite.run_network_simulation = lambda *args, **kwargs: runs.append(
                (kwargs.get("ultra_soak", False), recovery.os.environ.get("SEDS_FIRMWARE_SIM_SOAK_MS")))
            specification = SimpleNamespace(loader=mock.Mock())
            with mock.patch.object(recovery.importlib.util, "spec_from_file_location", return_value=specification), \
                 mock.patch.object(recovery.importlib.util, "module_from_spec", return_value=suite), \
                 mock.patch.dict(recovery.os.environ, {}, clear=True), \
                 mock.patch("sys.argv", ["test-route-recovery", "--workspace", tmp, "--image", "candidate", "--ultra-soak", "--soak-only"]):
                recovery.main()
            self.assertEqual(runs, [(True, "600000")])

    def test_six_hour_profile_keeps_short_gate_and_continuous_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = []
            suite = SimpleNamespace(load_layout_for_build=mock.Mock(), network_sample_count=lambda layouts, count: count)
            suite.run_network_simulation = lambda *args, **kwargs: runs.append((
                kwargs.get("ultra_soak", False), recovery.os.environ.get("SEDS_FIRMWARE_SIM_SOAK_MS"),
                suite.network_sample_count({}, 12)))
            specification = SimpleNamespace(loader=mock.Mock())
            with mock.patch.object(recovery.importlib.util, "spec_from_file_location", return_value=specification), \
                 mock.patch.object(recovery.importlib.util, "module_from_spec", return_value=suite), \
                 mock.patch.dict(recovery.os.environ, {}, clear=True), \
                 mock.patch("sys.argv", ["test-route-recovery", "--workspace", tmp, "--image", "candidate",
                     "--ultra-soak", "--soak-ms", "21600000", "--soak-samples", "360"]):
                recovery.main()
            self.assertEqual(runs, [(False, None, 12), (True, "21600000", 360)])

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

    def test_evidence_capture_preserves_paths_and_does_not_invoke_a_shell(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "saved evidence"
            command = ["docker", "run", "--rm", "image", "bay"]
            recovery.configure_evidence_capture(command, directory)
            self.assertTrue(directory.is_dir())
            self.assertEqual(command[:2], ["docker", "run"])
            self.assertEqual(command[2:6], ["-v", f"{directory.resolve()}:/diagnostics", "-e", "FIRMWARE_SIM_DIAGNOSTICS_DIR=/diagnostics"])
            self.assertEqual(command[-3:], ["--rm", "image", "bay"])

    def test_recording_soak_uses_large_prepared_cards_and_observes_fc_writer(self):
        for board, filesystem in [("DAQ-Board", "fat32:SEDSDAQ.ID"), ("FlightComputer26", "fat32")]:
            card = {"model": "sd_card", "capacity_bytes": 4*1024*1024}
            layout = {"peripherals": [card], "execution": {"memory_probes": []}}
            recovery.configure_soak_storage(layout, board)
            self.assertEqual(card["capacity_bytes"], 64*1024**3)
            self.assertEqual(card["filesystem"], filesystem)
            count = len(layout["execution"]["memory_probes"])
            recovery.configure_soak_storage(layout, board)
            self.assertEqual(len(layout["execution"]["memory_probes"]), count)
            if board == "DAQ-Board":
                probes = {p["name"]: p for p in layout["execution"]["memory_probes"]}
                self.assertEqual(probes["sd_raw_records_dropped"]["maximum"], 0)
                self.assertEqual(probes["sd_raw_records_io_lost"]["maximum"], 0)
                self.assertIn("sd_raw_adc_drained", probes)
                self.assertIn("sd_raw_records_flushed", probes)
            if board == "FlightComputer26":
                probes = {p["name"]: p for p in layout["execution"]["memory_probes"]}
                self.assertEqual(probes["sd_ready"]["minimum"], 1)
                self.assertEqual(probes["sd_processed_records"]["minimum"], 1)
                self.assertTrue(probes["sd_processed_records"]["ignore_leading_zeroes"])
                self.assertEqual(probes["sd_write_errors"]["maximum"], 0)
        with self.assertRaises(ValueError):
            recovery.configure_soak_storage({}, "FlightComputer26")

    def test_sd_progress_spans_resets_and_flush_is_required_only_after_its_period(self):
        short = {"sample_count": 7, "assertions": []}
        recovery.configure_storage_assertions(short, require_flush=False)
        self.assertEqual(short["assertions"][0]["minimum_gain"], 1)
        self.assertEqual(short["assertions"][0]["to_sample"], 6)
        self.assertFalse(any(a["probe"] == "sd_flushed_records" for a in short["assertions"]))
        long = {"sample_count": 360, "assertions": [], "reboots": [{"node":"groundstation","after_sample":120},{"node":"flight","after_sample":240}]}
        recovery.configure_storage_assertions(long, require_flush=True)
        progress = [a for a in long["assertions"] if a["probe"] == "sd_processed_records"]
        self.assertEqual([(a["from_sample"], a["to_sample"]) for a in progress], [(0,239),(240,359)])
        self.assertTrue(all(a["minimum_gain"] == 1 for a in progress))
        self.assertEqual(long["assertions"][-1]["sample"], 359)
        self.assertEqual(long["assertions"][-1]["minimum"], 1)

    def test_ack_fault_expiry_checks_follow_actual_restarts(self):
        for nodes in (["groundstation"], ["rf"]):
            with self.subTest(nodes=nodes), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                checks = [dict(name=f"rf no CAN expiry outside injected ACK loss at sample {n}",
                               node="rf", probe="can_tx_expired", sample=n, maximum=0)
                          for n in (0, 6, 8, 10)]
                (root / "topology.json").write_text(json.dumps(dict(
                    sample_count=12, assertions=checks, host_log_assertions=[])))
                recovery.configure_restarts(root, nodes, [6, 10])
                actual = json.loads((root / "topology.json").read_text())["assertions"]
                self.assertEqual(actual[0]["maximum"], 0)
                for check, index in zip(actual[1:], (6, 8, 10)):
                    if "rf" in nodes and index in (6, 10):
                        self.assertEqual(check["sample"], index)
                        self.assertEqual(check["maximum"], 0)
                    else:
                        self.assertEqual((check["from_sample"], check["to_sample"], check["maximum_gain"]), (index-1, index, 0))

if __name__ == "__main__":
    unittest.main()
