#!/usr/bin/env python3
"""Station-managed native bay: the seven boards' real firmware (native builds) plus the physical plant, run for a
GroundStation that starts it and then connects to it like hardware (its "Firmware sim" mode).

Unlike scripts/native_engine.py (a scenario harness that owns the station and its clock), here the station owns the
bay: the bay runs at real time (pace 1) so the station's wall clock is the common time base, the station's radio is a
PTY and its fill-box link the modelled Pico-Fi on a loopback discovery port, both written to <out>/endpoints.json.

  python3 scripts/gs_bay.py --manifest <cache>/manifest.json --out DIR [--plant PLANT.json] [--nodes a,b,...]

<out>/control.json (written by the station, optional): {"plant": {"qd_connected": bool, "nitrogen_bottle_open": bool,
"nitrous_bottle_open": bool}, "faults": {"stuck_valves": {"<valve>": "open"|"closed"}, "dead_tank_pt": bool,
"dead_chamber_pt": bool, "radio_drop": bool}}. <out>/status.json (written once per virtual second): pace, nodes,
resets, firmware revisions and the plant (tank, valves, supply, umbilical, flight). Board logs: <out>/diagnostics.
SIGTERM stops the bay; board flash/SD state lives in a scratch directory that is removed on exit (each start is a
fresh power-on with blank or factory flash).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
NODES = ["rf", "power", "flight", "gateway", "actuator", "valve", "daq"]  # rf first: the bay clock file lives there
CAN = {"avionics_can": [("rf", "fdcan2"), ("power", "fdcan2"), ("flight", "fdcan1")],
       "fill_can": [("gateway", "fdcan2"), ("actuator", "fdcan2"), ("valve", "fdcan2"), ("daq", "fdcan1")]}
# RFD900x ASYNC 64 kbit/s air, 115200 UART (test-full-network.py RADIO['rfd'])
RADIO = dict(air_bits_per_second=64000, uart_baud=115200, tx_fifo_bytes=8192, rx_fifo_bytes=8192, max_burst_bytes=256,
             packet_idle_us=2000, overhead_bits=160, turn_us=2000)
RUN_FOREVER_MS = 30 * 24 * 3600 * 1000


def devices_text(layout: dict) -> str:
    sys.path.insert(0, str(HERE))
    from native_engine import devices_text as engine_devices  # same device lines as the fullnet engine
    return engine_devices(layout)


def topology(nodes: list[str]) -> dict:
    links = []
    for name, eps in CAN.items():
        eps = [e for e in eps if e[0] in nodes]
        if eps:
            links.append({"name": name, "kind": "can", "endpoints": [{"node": n, "peripheral": p} for n, p in eps]})
    if "rf" in nodes:
        links.append({"name": "rocket_radio", "kind": "radio", "radio": RADIO,
                      "endpoints": [{"node": "rf", "peripheral": "usart1"}, {"node": "groundstation", "peripheral": "av_bay"}]})
    if "gateway" in nodes:
        links.append({"name": "fill_pico", "kind": "pico_fi", "baud_rate": 1000000,
                      "endpoints": [{"node": "groundstation", "peripheral": "fill_box"}, {"node": "gateway", "peripheral": "usart2"}]})
    serial = [{"link": l["name"], "env": env} for l, env in
              ((l, {"rocket_radio": "GS_AV_BAY_SERIAL_PORT", "fill_pico": "GS_FILL_PICO_DISCOVERY_PORT"}.get(l["name"])) for l in links) if env]
    for s in serial:
        if s["link"] == "fill_pico":
            s["transport"] = "pico_fi_discovery_to_uart"
    return {"name": "gs-managed-firmware-sim", "quantum_seconds": 0.0001, "virtual_time_ms": RUN_FOREVER_MS, "sample_count": 1,
            "enforce_end_drop": False, "reboots": [], "can_ack_events": [], "can_link_events": [], "radio_link_events": [],
            "assertions": [], "host_log_assertions": [],
            "nodes": [{"name": n, "layout": f"{n}.json", "firmware_root": "."} for n in NODES if n in nodes],
            "links": links,
            "host_nodes": [{"name": "groundstation", "binary": "/bin/true", "serial_links": serial}]}


def die_with_parent(sig=signal.SIGTERM):
    """Linux: deliver `sig` to this process when its parent dies (a crashed or killed station never leaves a bay)."""
    try:
        import ctypes
        ctypes.CDLL(None, use_errno=True).prctl(1, int(sig), 0, 0, 0)  # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


SD_BYTES = 1024 << 20  # the shim's SIM_SD_MB default


def write_sd(image: Path, igt_dir: Path | None, dataset: str) -> dict:
    """A FAT32 card for the FC; with `dataset`, its IGT/ files at the card root (the IGT replay's layout)."""
    sys.path.insert(0, str(HERE))
    from fat32_image import build
    files = {}
    if dataset and igt_dir:
        src = igt_dir / dataset / "IGT"
        for f in sorted(src.glob("*.CSV")):
            files[f"IGT/{f.name.upper()}"] = f.read_bytes()
    image.parent.mkdir(parents=True, exist_ok=True)
    build(image, SD_BYTES, files)
    return {"dataset": dataset or None, "files": sorted(files), "bytes": sum(map(len, files.values()))}


def watch_sd(out: Path, image: Path, igt_dir: Path | None, stop) -> None:
    """Swap the card's contents when the operator picks another IGT dataset while the card is out (fc_sd_missing),
    as a person would swap cards; a change while the card is in waits for the next removal."""
    import time as _t
    status_path = out / "sdcard.json"
    loaded = json.loads(status_path.read_text()).get("dataset") if status_path.exists() else None
    while not stop.is_set():
        try:
            control = json.loads((out / "control.json").read_text())
        except (OSError, ValueError):
            control = {}
        want = ((control.get("igt") or {}).get("dataset")) or None
        out_of_slot = bool((control.get("faults") or {}).get("fc_sd_missing"))
        if want != loaded:
            if out_of_slot:
                info = write_sd(image, igt_dir, want or "")
                loaded = want
                status_path.write_text(json.dumps({**info, "pending": None}))
            else:
                status_path.write_text(json.dumps({"dataset": loaded, "pending": want,
                                                   "note": "remove the card (FC SD card missing) to load the selected dataset"}))
        _t.sleep(0.5)


def plant_from_flight_profile(directory: Path) -> tuple[dict, dict]:
    """Plant defaults from GroundStation26 tools/flight_profile (vehicle.json, the motor summary), so updates there
    (the apogee planner, the fire archive) flow into the sim. Returns (plant overrides, provenance)."""
    vehicle = json.loads((directory / "vehicle.json").read_text())
    v, m, rec, env = vehicle.get("vehicle", {}), vehicle.get("motor", {}), vehicle.get("recovery", {}), vehicle.get("environment", {})
    out = {"vehicle": {}, "motor": {}}
    veh = out["vehicle"]
    for key, src in (("dry_mass_kg", v.get("dry_mass_kg")), ("diameter_m", v.get("diameter_m")),
                     ("drogue_vt", rec.get("drogue_descent_mps")), ("main_vt", rec.get("main_descent_mps")),
                     ("launch_lat", env.get("site_lat_deg")), ("launch_lon", env.get("site_lon_deg")), ("pad_msl_m", env.get("site_msl_m"))):
        if isinstance(src, (int, float)):
            veh[key] = float(src)
    info = {"vehicle_json": str(directory / "vehicle.json")}
    curve = m.get("thrust_curve_csv")
    summary_path = (directory / curve).with_suffix(".json") if curve else None
    if summary_path and summary_path.exists():
        summary = json.loads(summary_path.read_text()).get("summary", {})
        impulse = summary.get("total_impulse_ns")
        breakdown = summary.get("propellant_mass_breakdown") or {}
        ox = breakdown.get("oxidizer_n2o_kg")
        fuel = breakdown.get("fuel_kg")
        of = (ox / fuel) if isinstance(ox, (int, float)) and isinstance(fuel, (int, float)) and fuel > 0 else 5.0
        if isinstance(impulse, (int, float)) and isinstance(ox, (int, float)) and ox > 0:
            # thrust = Isp g0 (1 + 1/OF) mdot_ox, so the measured impulse per kg of oxidiser fixes the delivered Isp
            out["motor"]["isp_s"] = impulse / (ox * 9.80665 * (1.0 + 1.0 / of))
            out["motor"]["of_ratio"] = of
        if isinstance(fuel, (int, float)):
            veh["fuel_kg"] = float(fuel)
            veh["tare_kg"] = veh.get("dry_mass_kg", 31.6) + float(fuel)
        info.update(motor_summary=str(summary_path), total_impulse_ns=impulse, oxidizer_kg=ox, fuel_kg=fuel,
                    isp_s=round(out["motor"].get("isp_s", 0.0), 1))
    if isinstance(m.get("total_impulse_ns"), (int, float)) and info.get("total_impulse_ns"):
        # vehicle.json rescales the measured curve to another impulse (the 10 kft sets)
        out["motor"]["impulse_scale"] = m["total_impulse_ns"] / info["total_impulse_ns"]
    return out, info


def host_can_run(exe: Path) -> str | None:
    """Why this host cannot run the 32-bit board executables (None: it can). On an aarch64 host they are armhf and
    need the kernel's 32-bit compat, which arm64 Linux offers only with 4 KB pages."""
    import errno, platform
    if platform.machine().lower() not in ("aarch64", "arm64"):
        return None
    os_id = ""
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("ID="):
                os_id = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    page = os.sysconf("SC_PAGE_SIZE")
    fix = ("Raspberry Pi OS: set kernel=kernel8.img in /boot/firmware/config.txt (4 KB pages, 32-bit compat) and reboot"
           if os_id in ("raspbian", "debian") else
           "use a kernel built with 4 KB pages and CONFIG_COMPAT (Ubuntu raspi kernels are)")
    if page != 4096:
        return f"this kernel uses {page // 1024} KB pages, so it cannot run 32-bit (armhf) programs; {fix}"
    try:
        probe = subprocess.run([str(exe)], env={"PATH": "/usr/bin:/bin"}, capture_output=True, timeout=3)
        del probe
    except OSError as e:
        if e.errno == errno.ENOEXEC:
            return f"this kernel cannot run 32-bit (armhf) programs (no CONFIG_COMPAT); {fix}"
        return f"cannot start the board executables: {e}"
    except subprocess.TimeoutExpired:
        pass
    return None


def std_pressure(h_msl: float) -> float:
    return 101325.0 * (1 - 2.25577e-5 * h_msl) ** 5.25588


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--plant", type=Path, help="plant configuration overrides (native_plant.rs PlantConfig)")
    p.add_argument("--nodes", default=",".join(NODES))
    p.add_argument("--flight-profile-dir", type=Path,
                   help="GroundStation26 tools/flight_profile: vehicle.json and the motor data seed the plant")
    p.add_argument("--igt-dir", type=Path, help="IGT datasets (GroundStation26 test-data/igt): <set>/IGT/*.CSV")
    p.add_argument("--mode", default="normal", help="station mode: test_fire keeps the rocket on the stand, hitl fires no igniter")
    p.add_argument("--pace", type=float, default=1.0, help="virtual s per wall s; the station needs 1")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--coordinator", type=Path, help="firmware-sim binary (default: the manifest's)")
    a = p.parse_args()
    die_with_parent()

    manifest = json.loads(a.manifest.read_text())
    nodes = [n for n in a.nodes.split(",") if n]
    missing = [n for n in nodes if n not in manifest.get("boards", {})]
    if missing:
        raise SystemExit(f"not built: {', '.join(missing)} (python3 tools/firmware_sim.py build)")
    out = a.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    blocked = host_can_run(Path(manifest["boards"][nodes[0]]["exe"]))
    if blocked:
        (out / "refused.txt").write_text(blocked + "\n")
        raise SystemExit(f"firmware simulation refused: {blocked}")
    config = out / "config"
    diag = out / "diagnostics"
    for d in (config, diag, diag / "temp"):
        d.mkdir(parents=True, exist_ok=True)
    for stale in ("endpoints.json", "status.json"):
        (out / stale).unlink(missing_ok=True)
    plant = json.loads(a.plant.read_text()) if a.plant else {}
    profile_info = None
    if a.flight_profile_dir and (a.flight_profile_dir / "vehicle.json").exists():
        seeded, profile_info = plant_from_flight_profile(a.flight_profile_dir)
        for section, values in seeded.items():
            for k, val in values.items():
                plant.setdefault(section, {}).setdefault(k, val)
    plant.setdefault("test_stand", a.mode in ("test_fire", "integrated_ground_test"))
    plant.setdefault("require_igniter", a.mode not in ("hitl", "testing"))
    plant.setdefault("seed", a.seed)
    # the DAQ's SystemCoreClock (its DAC trigger timers' clock) for the plant's DAC playback
    daq = manifest.get("boards", {}).get("daq")
    if daq:
        sym = Path(daq["exe"]).with_suffix(".symbols")
        try:
            for line in sym.read_text().splitlines():
                parts = line.split()
                if len(parts) >= 3 and parts[-1] == "SystemCoreClock":
                    plant.setdefault("dac", {}).setdefault("clock_addr", int(parts[0], 16))
                    break
        except OSError:
            pass
    pad_msl = (plant.get("vehicle") or {}).get("pad_msl_m", 890.016)
    shm = Path("/dev/shm") if Path("/dev/shm").is_dir() else None
    scratch = Path(tempfile.mkdtemp(prefix="seds-gs-bay-", dir=shm))
    # the FC's SD card (FAT32), with the selected IGT dataset if any
    control_doc = {}
    try:
        control_doc = json.loads((out / "control.json").read_text())
    except (OSError, ValueError):
        pass
    sd_image = scratch / "flight-state" / "sdcard.img"
    if "flight" in nodes:
        info = write_sd(sd_image, a.igt_dir, ((control_doc.get("igt") or {}).get("dataset")) or "")
        (out / "sdcard.json").write_text(json.dumps({**info, "pending": None}))
    native_nodes = {}
    firmware = {}
    for n in nodes:
        b = manifest["boards"][n]
        if n == "flight" and a.mode == "integrated_ground_test":
            # the FC ground-test image (CSV replay from the SD card, dry-run pyro gate)
            if "flight_igt" not in manifest["boards"]:
                raise SystemExit("the FC ground-test variant is not built (python3 tools/firmware_sim.py build)")
            b = manifest["boards"]["flight_igt"]
        board_dir = Path(b["exe"]).parent
        layout = json.loads((board_dir / "board.json").read_text())
        dev = config / f"{n}.devices"
        text = devices_text(layout)
        for per in layout.get("peripherals", []):
            if per.get("model") == "neo_m9n":  # the native NEO-M9N model (sim_gps.c)
                text += f"gps {per.get('bus', 'spi1')}\n"
        dev.write_text(text)
        env = {}
        if n == "flight":
            # the barometer's weather trim set (P = raw / 128 Pa) so the plant's flight model can drive it
            env["SIM_BARO_PA"] = f"{std_pressure(pad_msl):.1f}"
        native_nodes[n] = {"exe": b["exe"], "flash_init": b.get("factory_bin"), "flash_size": b.get("flash_size", 524288),
                           "devices": str(dev), "env": env, "drift_ppm": 0}
        firmware[n] = {k: b.get(k) for k in ("repo", "ref", "short", "describe", "subject", "committed")}
    firmware["_engine"] = {k: (manifest.get("firmware_simulator") or {}).get(k) for k in ("describe", "short", "source", "dirty")}
    firmware["_flight_profile"] = profile_info
    firmware["_sedsnet"] = {k: (manifest.get("sedsnet") or {}).get(k) for k in ("ref", "short", "describe")}
    topo = topology(nodes)
    (config / "topology.json").write_text(json.dumps(topo, indent=1))
    native = {"nodes": native_nodes, "pace": a.pace, "quantum_us": 1000, "max_skip_us": 20000, "scratch": str(scratch),
              "diagnostics": str(diag), "stimulus": [], "probes": [], "uart_gates": [], "seed": a.seed, "can_trace": False,
              "external_station": str(out / "endpoints.json"), "control": str(out / "control.json"),
              "status": str(out / "status.json"), "plant": plant, "firmware": firmware}
    (config / "native.json").write_text(json.dumps(native, indent=1))
    coordinator = a.coordinator or Path(manifest["coordinator"])
    cmd = [str(coordinator), "native-bay", "--topology", str(config / "topology.json"), "--native", str(config / "native.json")]
    env = {**os.environ, "FIRMWARE_SIM_NODE_NICE": os.environ.get("FIRMWARE_SIM_NODE_NICE", "10")}
    import threading
    stop = threading.Event()
    if "flight" in nodes:
        threading.Thread(target=watch_sd, args=(out, sd_image, a.igt_dir, stop), daemon=True).start()
    with (diag / "bay.log").open("a") as log:
        child = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env,
                                 preexec_fn=lambda: die_with_parent(signal.SIGTERM))

        def forward(sig, _frame):
            if child.poll() is None:
                child.send_signal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, forward)
        signal.signal(signal.SIGINT, forward)
        try:
            code = child.wait()
        finally:
            stop.set()
            if child.poll() is None:
                child.kill()
            shutil.rmtree(scratch, ignore_errors=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
