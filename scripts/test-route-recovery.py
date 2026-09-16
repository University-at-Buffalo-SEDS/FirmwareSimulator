#!/usr/bin/env python3
"""Run seven real firmware images and GroundStation through CAN route loss."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time

def configure_fault(directory, node):
    path = directory / "topology.json"
    topology = json.loads(path.read_text())
    topology["can_link_events"] = [
        dict(node="gateway", peripheral="fdcan2", link="fill_can", after_sample=0, connected=False),
        dict(node="gateway", peripheral="fdcan2", link="fill_can", after_sample=3, connected=True),
    ]
    # Cumulative counters include the deliberate outage. Require them to stop
    # growing after rediscovery. BOTH output boards lose the upstream route,
    # regardless of which reporter is selected for the detailed retry probes.
    for affected in ("valve", "actuator"):
        affected_path = directory / f"{affected}.json"
        affected_layout = json.loads(affected_path.read_text())
        for probe in affected_layout["execution"]["memory_probes"]:
            if probe["name"] == "umbilical_status_fail":
                probe.pop("maximum", None)
                topology["assertions"].append(dict(
                    name=f"No new errors after route recovery: {affected}." + probe["name"],
                    node=affected, probe=probe["name"], maximum_gain=0,
                    from_sample=3, to_sample=topology["sample_count"]-1))
        affected_path.write_text(json.dumps(affected_layout, indent=2))
    layout_path = directory / f"{node}.json"
    layout = json.loads(layout_path.read_text())
    existing_probes = {probe["name"] for probe in layout["execution"]["memory_probes"]}
    for name in ("status_report_pending", "status_report_failures", "status_report_recovered"):
        if name not in existing_probes:
            layout["execution"]["memory_probes"].append(dict(name=name, symbol=name))
    layout_path.write_text(json.dumps(layout, indent=2))
    gateway_path = directory / "gateway.json"
    gateway = json.loads(gateway_path.read_text())
    for probe in gateway["execution"]["memory_probes"]:
        if probe["name"] == "fdcan_tx_fail":
            probe.pop("maximum", None)
            topology["assertions"].append(dict(
                name="Gateway send errors stop after reconnect", node="gateway",
                probe="fdcan_tx_fail", maximum_gain=0, from_sample=3,
                to_sample=topology["sample_count"]-1))
    gateway_path.write_text(json.dumps(gateway, indent=2))
    topology["assertions"].extend([
        dict(name="Output board still receives CAN while upstream is absent", node=node,
             probe="fdcan_rx", sample=2, minimum=1),
        dict(name="Missing route is not a local CAN send failure", node=node,
             probe="fdcan_tx_fail", maximum=0),
        dict(name="Missing route reports submission failure", node=node,
             probe="status_report_failures", sample=2, minimum=1),
        dict(name="Latest output state retained during route loss", node=node,
             probe="status_report_pending", sample=2, minimum=1),
        dict(name="Retained state retried after discovery", node=node,
             probe="status_report_recovered", minimum=1),
        dict(name="No state stranded after recovery", node=node,
             probe="status_report_pending", sample=topology["sample_count"]-1, maximum=0),
    ])
    # The planned 12-second outage is not discovery latency. All command and
    # state-return latency bounds remain unchanged after the route exists.
    for host in topology["host_nodes"]:
        host["env"]["GS_SIM_DISCOVERY_MAX_LATENCY_MS"] = "22000"
        host["env"]["GS_SIM_VALIDATE_SOAK_COMMANDS"] = "1"
        host["env"]["GS_SIM_SOAK_COMMAND_SAMPLES"] = "4,6,8"
    for board in ("VB", "AB"):
        for index, sample in enumerate((4, 6, 8)):
            state = str(index % 2 != 0).lower()
            topology["host_log_assertions"].append(dict(
                name=f"{board} fresh state response after reconnect round {index + 1}",
                node="groundstation", minimum_occurrences=1,
                contains=f"full-bay soak valve command acknowledged: board={board}, state={state}, round {index + 1}/3, sample {sample},"))
    path.write_text(json.dumps(topology, indent=2))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--report-node", "--disconnect-node", dest="disconnect_node",
                        choices=("valve", "actuator"),
                        help="output board to observe while Gateway is detached from CAN")
    parser.add_argument("--fault-elf", help="alternate ELF relative to the output board, for negative controls")
    parser.add_argument("--ultra-soak", action="store_true", help="run the normal short gate, then the complete ten-minute seven-board soak")
    parser.add_argument("--restart-regression", action="store_true", help="run the short gate, then a 120-second restart regression (not ten-minute qualification)")
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    if args.ultra_soak and args.restart_regression:
        parser.error("choose either --ultra-soak or --restart-regression")
    if (args.ultra_soak or args.restart_regression) and (args.disconnect_node or args.fault_elf):
        parser.error("--ultra-soak uses the full-system fault/reboot schedule, not the short route-loss scenario")
    if args.fault_elf and not args.disconnect_node:
        parser.error("--fault-elf requires --disconnect-node")
    root = args.workspace.resolve()
    spec = importlib.util.spec_from_file_location("board_suite", root / "ActuatorBoard26/sim/run_full.py")
    suite = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(suite)
    load_layout = suite.load_layout_for_build
    def load_built_layout(board, _subdir):
        subdir = "Release" if board.name == "FlightComputer26" else "Release_Script"
        layout = load_layout(board, subdir)
        fault_repo = {"valve": "ValveBoard26", "actuator": "ActuatorBoard26"}.get(args.disconnect_node)
        if args.fault_elf and board.name == fault_repo:
            layout["artifacts"]["elf"] = args.fault_elf
        return layout
    suite.load_layout_for_build = load_built_layout
    os.environ.update(SEDS_FIRMWARE_SIM_SUITE_ROOT=str(root),
                      SEDS_FIRMWARE_SIM_IMAGE=args.image, SEDS_FIRMWARE_SIM_SKIP_BUILD="1",
                      SEDS_FIRMWARE_SIM_NETWORK_TIME_MS="40000" if args.disconnect_node else "16000",
                      SEDS_FIRMWARE_SIM_NETWORK_SAMPLES="10" if args.disconnect_node else "6",
                      SEDS_FIRMWARE_SIM_DOCKER_NETWORK="host")
    os.environ.pop("SEDS_FIRMWARE_SIM_SOURCE", None)
    container = f"seds-route-recovery-{os.getpid()}"
    def run(command, label):
        if args.restart_regression:
            label = label.replace("ten-minute", "120-second restart regression")
        mount = next(value for value in command if value.endswith(":/simulation:ro"))
        directory = Path(mount.split(":", 1)[0])
        topology_path = directory / "topology.json"
        topology = json.loads(topology_path.read_text())
        for host in topology["host_nodes"]:
            host.setdefault("env", {})["GS_BIND_ADDRESS"] = "127.0.0.1:0"
        topology_path.write_text(json.dumps(topology, indent=2))
        if args.disconnect_node:
            configure_fault(directory, args.disconnect_node)
        run_kind = args.disconnect_node or "baseline"
        if "ten-minute" in label:
            run_kind = "ten-minute-soak"
        elif "120-second restart regression" in label:
            run_kind = "restart-regression"
        if args.fault_elf:
            run_kind += "-negative-control"
        evidence = root / f"route-recovery-{run_kind}.json"
        evidence.write_text((directory / "topology.json").read_text())
        command[2:2] = ["--name", container]
        started = time.monotonic()
        process = subprocess.Popen(command)
        try:
            while True:
                try:
                    result = process.wait(timeout=10)
                    if result:
                        raise subprocess.CalledProcessError(result, command)
                    break
                except subprocess.TimeoutExpired:
                    elapsed = time.monotonic() - started
                    print(f"[SIM] {label}: {elapsed:.0f}s elapsed", flush=True)
                    if elapsed >= args.timeout:
                        raise TimeoutError("Simulation deadline exceeded; NOT a pass")
        finally:
            if process.poll() is None:
                subprocess.run([command[0], "stop", "--time", "5", container], check=False)
                process.wait(timeout=20)
    suite.run_live = run
    class UI:
        @staticmethod
        def say(kind, message):
            print(f"[{kind}] {message}", flush=True)
    suite.run_network_simulation(UI(), root / "ActuatorBoard26", "stm32g4", "Release_Script")
    if args.ultra_soak or args.restart_regression:
        os.environ["SEDS_FIRMWARE_SIM_SOAK_MS"] = "120000" if args.restart_regression else "600000"
        suite.run_network_simulation(UI(), root / "ActuatorBoard26", "stm32g4", "Release_Script", ultra_soak=True)
    print("PASS: firmware network and route recovery", flush=True)

if __name__ == "__main__":
    main()
