#!/usr/bin/env python3
"""Run seven real firmware images and GroundStation through CAN route loss."""
import argparse
import importlib.util
import json
import os
import re
from pathlib import Path
import subprocess
import time

def built_subdir(board):
    return "Release" if board.name in {"FlightComputer26", "RFBoard26"} else "Release_Script"

def configured_pico_baud(cache):
    match = re.search(r"^GATEWAY_PICO_UART_BAUD_RATE:STRING=(\d+)$", cache.read_text(), re.MULTILINE)
    if not match or int(match.group(1)) == 0:
        raise ValueError("Gateway build must declare a positive Pico UART baud")
    return int(match.group(1))

def configure_hardware_fill_profile(directory, topology):
    """Match the field P6/Ethernet path and require every sustained window.

    No artificial loss, CPU speed-up, or reduced source rate is introduced.
    A separate captured baseline describes the observed fault, not a desired
    simulator output that we force by probabilistic dropping.
    """
    duration=topology['virtual_time_ms']
    if duration<70000 or topology.get('reboots') or topology.get('can_ack_events') or topology.get('can_link_events'):
        raise ValueError('hardware fill baseline requires >=70 seconds and no scheduled resets/link faults')
    if topology.get('sample_count', 0)*10000 != duration:
        raise ValueError('hardware fill probes must be spaced exactly ten seconds apart')
    stations=[host for host in topology.get('host_nodes',[]) if host['name']=='groundstation']
    links=[link for link in topology.get('links',[]) if link['name']=='fill_pico']
    if len(stations)!=1 or len(links)!=1:
        raise ValueError('hardware fill baseline requires one GroundStation and its Ethernet fill link')
    serial=[link for link in stations[0].get('serial_links',[]) if link['link']=='fill_pico']
    if len(serial)!=1 or serial[0].get('transport')!='pico_fi_discovery_to_uart' or serial[0].get('env')!='GS_FILL_PICO_DISCOVERY_PORT':
        raise ValueError('hardware fill baseline requires Pi Ethernet discovery/TCP, without a GS-side Pico or I2C')
    link=links[0]
    endpoints={(e['node'],e['peripheral']) for e in link.get('endpoints',[])}
    if link.get('kind')!='pico_fi' or link.get('baud_rate')!=1000000 or endpoints!={('groundstation','fill_box'),('gateway','usart2')}:
        raise ValueError('hardware fill baseline requires Ethernet to one gateway-side Pico-Fi and USART2 at 1 Mbaud')
    link['transport_path']=['GroundStation26 main Ethernet','UDP discovery + framed TCP','gateway-side Pico-Fi','Gateway USART2']
    windows=(duration-20000)//10000-1 # Permit one final scheduler boundary only.
    calibration=directory/'hardware-fill-calibration.json'
    calibration.write_text(json.dumps({'kg50_input':'p6_differential'}))
    for host in topology['host_nodes']:
        if host['name']=='groundstation':
            host.setdefault('env',{}).update(GS_SIM_VALIDATE_FILL_DELIVERY='1',
                GS_LOADCELL_CALIBRATION_PATH='/simulation/'+calibration.name)
    checks=topology.setdefault('host_log_assertions',[])
    # The host's nominal-rate diagnostic assumes the 250 Hz configured cap
    # is a guaranteed producer cadence. P6 gain/auxiliary settling makes that
    # assumption false. Preserve its raw windows, then require >=99.5% of the
    # actual firmware publications plus the same freshness and zero-CAN-loss
    # limits in mandatory source-aware evidence validation after execution.
    checks.append(dict(name='Every sustained delivery window is available for source-aware validation',
        node='groundstation',contains='sustained fill delivery ',minimum_occurrences=windows))
    # Probe the radio throughout the same run, including after discovery TTLs.
    # Schedule from simulator sample markers, never slow host wall-clock sleeps.
    rounds = topology['sample_count'] - 1
    for host in stations:
        host['env'].update(GS_SIM_VALIDATE_SOAK_COMMANDS='1',
            GS_SIM_VALIDATE_SOAK_AV_BAY='1',
            GS_SIM_SOAK_COMMAND_SAMPLES=','.join(str(i) for i in range(1, rounds + 1)),
            GS_SIM_MANAGED_VARIABLE_MAX_LATENCY_MS='500')
    checks.append(dict(name='Every sustained RF control round returns destination receipts',
        node='groundstation',contains='full-bay RF soak control acknowledged:',minimum_occurrences=rounds))
    applied = topology.setdefault('assertions', [])
    for sample in range(1, topology['sample_count']):
        enabled = (sample - 1) % 2
        for node in ['rf', 'power', 'flight']:
            applied.append(dict(name=f'{node} applied RF underglow round {sample}',
                node=node,probe='underglow_enabled',sample=sample,minimum=enabled,maximum=enabled))
            applied.append(dict(name=f'{node} advanced RF underglow round {sample}',
                node=node,probe='underglow_updates',from_sample=sample-1,to_sample=sample,minimum_gain=1))
        applied.append(dict(name=f'FC executed non-actuating RF command round {sample}',
            node='flight',probe='network_flight_commands_processed',
            from_sample=sample-1,to_sample=sample,minimum_gain=1,maximum_gain=1))


def configure_hardware_fill_probes(layout, board):
    if board == 'DAQ-Board':
        probes=layout.setdefault('execution',{}).setdefault('memory_probes',[])
        existing={p['name'] for p in probes}
        if 'publication_failure_snapshot_words' not in existing:
            probes.append(dict(name='publication_failure_snapshot_words',symbol='g_telemetry_alloc_failure_stack_words'))
        for index,name in [(1,'queue_budget'),(2,'shared_bytes'),(3,'rx_bytes'),(4,'tx_bytes'),(5,'queue_storage_bytes'),(7,'discovery_bytes'),(8,'schema_bytes'),(16,'result'),(17,'type'),(18,'tick'),(19,'element_count'),(20,'admission_rejections')]:
            if 'publication_failure_'+name not in existing:
                probes.append(dict(name='publication_failure_'+name,symbol='g_telemetry_alloc_failure_stack',byte_offset=4*index))
        return
    if board not in ('gateway-board26','gateway-board'):
        return
    probes=layout.setdefault('execution',{}).setdefault('memory_probes',[])
    existing={p['name'] for p in probes}
    for name in ['can_first_drop_tick','can_first_drop_phase']:
        if name not in existing:probes.append(dict(name=name,symbol='g_sim_'+name))
    counters={
        'can_admission_fail':'g_gateway_can_admission_fail',
        'can_handoff_fail':'g_gateway_can_rx_handoff_fail',
        'queue_service_fail':'g_gateway_queue_service_fail',
        'cached_transit_checks':'g_gateway_cached_checks',
        'cached_transit_denied':'g_gateway_cached_denied'}
    for name,symbol in counters.items():
        if name not in existing:probes.append(dict(name=name,symbol=symbol))
    for index,name in enumerate(('version','queue_budget','shared_bytes','rx_bytes','tx_bytes',
            'queue_storage_bytes','reliable_rx_bytes','discovery_bytes','schema_bytes',
            'managed_bytes','pending_bytes','template_count','template_bytes','chunk_payload_bytes',
            'type_stats_count','type_stats_bytes')):
        key='network_memory_'+name
        if key not in existing:
            probe=dict(name=key,symbol='g_telemetry_alloc_failure_stack',byte_offset=4*index)
            if index==0:probe.update(minimum=1,maximum=1)
            probes.append(probe)
    probes.extend([
        dict(name='network_gps_priority',symbol='g_telemetry_alloc_failure_stack',byte_offset=64),
        dict(name='network_gps_type_exists',symbol='g_telemetry_alloc_failure_stack',byte_offset=68,minimum=1)])
    for name,index in [('actuator',5),('valve',6),('daq',7)]:
        probes.append(dict(name=name+'_can_frames',symbol='g_can_rx_sender_frames',byte_offset=4*index))
        probes.append(dict(name=name+'_can_drops',symbol='g_can_rx_sender_dropped',byte_offset=4*index))
    for index,name in enumerate(('uart','can','network_queues','discovery','time','ota','wait')):
        probes.append(dict(name='service_'+name+'_ms',symbol='g_gateway_service_ms',byte_offset=4*index))

def configure_allocator_probes(layout, cache):
    """Use allocator-specific diagnostics; never turn a missing fault probe into zero."""
    if not cache.exists() or "TELEMETRY_USE_TLSF:BOOL=ON" not in cache.read_text():
        return
    threadx_only = {"small_pool_available", "large_pool_available", "emergency_pool_available",
                    "alloc_cross_pool_recoveries", "alloc_emergency_recoveries"}
    probes = layout.get("execution", {}).get("memory_probes", [])
    for probe in probes:
        if probe["name"] in threadx_only and any(key in probe for key in
                ("minimum", "maximum", "max_end_drop", "minimum_interval_gain")):
            raise ValueError(f"ThreadX-only probe has a qualification bound: {probe['name']}")
    probes[:] = [p for p in probes if p["name"] not in threadx_only]
    existing = {p["name"] for p in probes}
    for name, bounds in (("active", {"minimum": 1}), ("init_failed", {"maximum": 0}),
                         ("failures", {"maximum": 0}), ("largest_free", {}),
                         ("live_bytes", {}), ("peak_bytes", {}), ("snapshot_count", {"minimum": 1})):
        name = "tlsf_" + name
        if name not in existing:
            probes.append(dict(name=name, symbol="g_telemetry_" + name, **bounds))

def validate_fill_evidence(directory, duration_ms):
    # Import by absolute sibling path so both CLI and importlib-based tests use
    # the same validation module without altering interpreter search paths.
    spec=importlib.util.spec_from_file_location('fill_delivery_report',
        Path(__file__).with_name('fill_delivery_report.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    result=module.analyze(module.load_diagnostics(directory), expected_duration_ms=duration_ms)
    (Path(directory)/'source-aware-delivery.json').write_text(json.dumps(result,indent=2)+'\n')
    if not result['passed']:
        raise RuntimeError('Source-aware fill delivery failed; see source-aware-delivery.json')
    return result

def configure_policy_qualified_controls(topology):
    """Policy-correct controls; per-ID ALL-owner audit remains mandatory."""
    primary=[h for h in topology.get("host_nodes",[]) if h["name"]=="groundstation"]
    if len(primary)!=1:raise ValueError("policy qualification requires exactly one primary GroundStation")
    primary[0].setdefault("env",{}).update(GS_SIM_POLICY_CORRECT_CONTROLS="1",
        GS_SIM_TRACE_API_REQUESTS="1",SEDSNET_TRACE_RELIABLE="1",
        GS_FLIGHT_STATE_DEFAULT="0",GS_SIM_MANAGED_VARIABLE_MAX_LATENCY_MS="500")
    for check in topology.get("host_log_assertions",[]):
        if check.get("node")=="groundstation" and check.get("contains")=="full-bay managed-variable latency within bound":
            check["contains"]="full-bay qualified round "
            check["name"]="Controlled rounds globally settled; mandatory per-ID500ms ALL-owner audit follows"


def policy_capture_paths(directory):
    """Only the documented primary capture layouts, not arbitrary recursive logs."""
    directory=Path(directory)
    paths={path.resolve() for path in directory.glob("bay-*/groundstation-host.log")
           if re.fullmatch(r"bay-\d+",path.parent.name) and path.is_file()}
    direct=directory/"groundstation-host.log"
    if direct.is_file():paths.add(direct.resolve())
    return paths


def select_policy_capture(directory, previous_captures=None):
    candidates=policy_capture_paths(directory)
    if previous_captures is not None:candidates-=set(previous_captures)
    if len(candidates)!=1:
        raise ValueError(f"Expected exactly one current-stage primary capture, found {len(candidates)}: {sorted(map(str,candidates))}")
    return next(iter(candidates))


def audit_policy_evidence(directory, previous_captures=None):
    """Save each stage beside its exact capture; never overwrite prior audit evidence."""
    spec=importlib.util.spec_from_file_location("obligation_audit",Path(__file__).with_name("audit_obligations.py"))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    output=directory/"policy-all-owner-audit.json"
    try:
        capture=select_policy_capture(directory,previous_captures)
        output=capture.parent/"policy-all-owner-audit.json"
        if output.exists():raise ValueError(f"Current capture already has audit evidence: {output}")
        result=module.audit(capture.read_text().splitlines(),500,3)
        result["capture_path"]=str(capture)
    except Exception as error:
        result={"passed":False,"limit_ms":500,"errors":["Missing/invalid primary API+SDK trace: "+str(error)]}
        output=directory/"policy-all-owner-audit.json"
    if output.exists():output=directory/f"policy-all-owner-audit-failure-{time.time_ns()}.json"
    result["audit_path"]=str(output.resolve())
    with output.open("x") as file:file.write(json.dumps(result,indent=2)+"\n")
    return result


def policy_validation_failure(directory, original_failure, previous_captures=None):
    try:
        audit=audit_policy_evidence(directory,previous_captures)
        print("[SIM] managed policy audit: "+audit.get("audit_path","policy-all-owner-audit.json"),flush=True)
        if audit["passed"]:return original_failure
        print("FAIL: policy per-ID500ms ALL-owner audit; see policy-all-owner-audit.json",flush=True)
        return original_failure if original_failure is not None else RuntimeError(
            "Policy-qualified controls lack complete within500ms ALL-owner receipts")
    except Exception as error:
        # Never replace a failed/timeout simulation with an audit implementation error.
        return original_failure if original_failure is not None else error


RESTART_NODES = ("groundstation", "rf", "power", "flight", "gateway", "valve", "actuator", "daq")

def configure_evidence_capture(command, directory):
    """The output directory must be writable by the simulator container user."""
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if hasattr(os,'getgid'):
        # The container's unprivileged simulator UID differs from the host UID.
        # Grant the diagnostics directory's host group, not world write access.
        directory.chmod(directory.stat().st_mode | 0o070)
        command[2:2]=['--group-add',str(directory.stat().st_gid)]
    command[2:2] = ["-v", f"{directory}:/diagnostics", "-e", "FIRMWARE_SIM_DIAGNOSTICS_DIR=/diagnostics"]

def configure_soak_storage(layout, board):
    """Exercise prepared cards for recording; keep blank/removal fixtures opt-in elsewhere."""
    if board not in ("DAQ-Board", "FlightComputer26"):
        return
    storage = next((p for p in layout.get("peripherals", []) if p.get("model") == "sd_card"), None)
    if storage is None:
        raise ValueError(f"{board} needs its real SD model for the recording soak")
    storage["capacity_bytes"] = 64 * 1024**3
    storage["filesystem"] = "fat32:SEDSDAQ.ID" if board == "DAQ-Board" else "fat32"
    probes = layout.setdefault("execution", {}).setdefault("memory_probes", [])
    if board == "FlightComputer26":
        fields = [("sd_ready", "g_sd_ready", {"minimum": 1}),
                  ("sd_accepted_records", "g_sd_enqueued_records", {"minimum": 1}),
                  ("sd_processed_records", "g_sd_written_records", {"minimum": 1, "ignore_leading_zeroes": True}),
                  ("sd_flushed_records", "g_sd_flushed_records", {}),
                  ("sd_write_errors", "g_sd_write_errors", {"maximum": 0}),
                  ("sd_format_errors", "g_sd_format_errors", {"maximum": 0}),
                  ("sd_lock_contention_drops", "g_sd_lock_contention_drops", {}),
                  ("sd_unavailable_drops", "g_sd_unavailable_drops", {})]
    else:
        fields = [("sd_raw_adc_drained", "g_daq_raw_samples_drained_count", {}),
                  ("sd_raw_records_accepted", "g_sd_raw_records_enqueued_count", {"minimum": 1}),
                  ("sd_raw_records_flushed", "g_sd_raw_records_flushed_count", {}),
                  ("sd_raw_records_dropped", "g_sd_raw_records_dropped_count", {"maximum": 0}),
                  ("sd_raw_records_io_lost", "g_sd_raw_records_io_lost_count", {"maximum": 0})]
    existing = {p["name"] for p in probes}
    for name, symbol, bounds in fields:
        if name not in existing:
            probes.append(dict(name=name, symbol=symbol, **bounds))

def configure_pressure_sd_probes(layout, board):
    """Opt-in current-image gate: no missing-symbol fallback or weakened counters."""
    fields = {
        "ValveBoard26": [("pressure_sd_captured", "g_pressure_sd_captured_samples", {}),
            ("pressure_sd_capture_drops", "g_pressure_sd_capture_drops", {"maximum": 0}),
            ("pressure_sd_late_half_drops", "g_pressure_sd_late_half_drops", {"maximum": 0}),
            ("pressure_sd_published", "g_pressure_sd_published_samples", {}),
            ("pressure_sd_publish_drops", "g_pressure_sd_publish_drops", {"maximum": 0})],
        "DAQ-Board": [("pressure_sd_batches", "g_pressure_sd_batches_received", {}),
            ("pressure_sd_invalid", "g_pressure_sd_invalid_batches", {"maximum": 0}),
            ("pressure_sd_enqueued", "g_sd_pressure_records_enqueued", {}),
            ("pressure_sd_written", "g_sd_pressure_records_written", {}),
            ("pressure_sd_flushed", "g_sd_pressure_records_flushed", {}),
            ("pressure_sd_dropped", "g_sd_pressure_records_dropped", {"maximum": 0})]
    }.get(board, [])
    probes=layout.setdefault("execution",{}).setdefault("memory_probes",[])
    existing={p["name"] for p in probes}
    if board == "ValveBoard26":
        for name,address in [("adc3_cr",0x50000408),("adc3_cfgr",0x5000040c),("adc3_isr",0x50000400),
                ("tim3_cr1",0x40000400),("tim3_cr2",0x40000404),("tim3_psc",0x40000428),("tim3_arr",0x4000042c),
                ("dma1_ccr",0x40020008),("dma1_cndtr",0x4002000c),("dma1_cpar",0x40020010),("dmamux0",0x40020800)]:
            if name not in existing:probes.append(dict(name=name,address=address))
        for name in ["data_acq_pressure_init_fail_count","data_acq_pressure_read_fail_count"]:
            if name not in existing:probes.append(dict(name=name,symbol=name,maximum=0))
    if board in ("gateway-board","gateway-board26"):
        for index,name in enumerate(("uart","can","network_queues","discovery","time","ota","wait")):
            if "service_max_"+name not in existing:probes.append(dict(name="service_max_"+name,symbol="g_gateway_service_max_ms",byte_offset=4*index))
    for name,symbol,bounds in fields:
        if name not in existing:probes.append(dict(name=name,symbol=symbol,**bounds))

def configure_pressure_sd_assertions(topology):
    count=topology["sample_count"]
    if count<2:raise ValueError("pressure SD gate needs at least two observations")
    # Hardware-fill observations are ten seconds apart. After initial discovery,
    # require the requested250Hz capture and transport, not mere nonzero activity.
    if topology["virtual_time_ms"]//count != 10000:
        raise ValueError("pressure SD gate requires exact ten-second observations")
    assertions=topology.setdefault("assertions",[])
    for sample in range(1,count):
        for node,probe,gain in [("valve","pressure_sd_captured",2500),
                ("valve","pressure_sd_published",2500),("daq","pressure_sd_enqueued",2500),
                ("daq","pressure_sd_written",2500),
                ("daq","pressure_sd_batches",625)]:
            assertions.append(dict(name=f"250Hz PT SD {node}.{probe} round{sample}",node=node,probe=probe,
                from_sample=sample-1,to_sample=sample,minimum_gain=gain-8,maximum_gain=gain+8))

    assertions.append(dict(name="Pressure SD records reach a media flush",node="daq",probe="pressure_sd_flushed",sample=count-1,minimum=1))

def configure_storage_assertions(topology, require_flush):
    """Allow the first batching interval, then require actual writes in every boot."""
    count = topology["sample_count"]
    boundaries = sorted({0, count, *(r["after_sample"] for r in topology.get("reboots", []) if r["node"] == "flight")})
    assertions = topology.setdefault("assertions", [])
    for start, end in zip(boundaries, boundaries[1:]):
        common = dict(name=f"FC SD writer progresses in samples {start}..{end-1}", node="flight", probe="sd_processed_records")
        if end - start > 1:
            assertions.append(dict(**common, from_sample=start, to_sample=end-1, minimum_gain=1))
        else:
            assertions.append(dict(**common, sample=start, minimum=1))
    if require_flush:
        assertions.append(dict(name="FC SD records reach a media flush", node="flight", probe="sd_flushed_records", sample=count-1, minimum=1))
        assertions.append(dict(name="DAQ raw records reach a media flush", node="daq", probe="sd_raw_records_flushed", sample=count-1, minimum=1))

def configure_restarts(directory, nodes, samples=None):
    """Restart selected processes/MCUs without restarting their peers."""
    path = directory / "topology.json"
    topology = json.loads(path.read_text())
    count = topology["sample_count"]
    samples = samples if samples is not None else [(count * 2) // 3]
    if not nodes or any(node not in RESTART_NODES for node in nodes) or len(set(nodes)) != len(nodes):
        raise ValueError("Select unique, known restart nodes")
    if not samples or sorted(set(samples)) != samples or any(s <= 0 or s >= count for s in samples):
        raise ValueError(f"Restart samples must be unique, increasing, and within 1..{count - 1}")
    topology["reboots"] = [dict(node=node, after_sample=sample) for sample in samples for node in nodes]
    # A still-powered MCU cannot demonstrate boot-time flash restoration.
    # Keep those checks for nodes that actually reboot; retain all traffic,
    # command latency, allocator and persistence-error checks.
    topology["assertions"] = [
        assertion for assertion in topology.get("assertions", [])
        if assertion.get("node") in nodes
        or not any(word in assertion["name"].lower() for word in ("restored", "startup buzz"))
    ]
    # after_sample counts completed samples; probe indices are zero-based.
    # A reboot after 8 samples lies between probe indices 7 and 8. Require
    # fresh execution in the new boot, not growth from the old boot's counter;
    # never waive that interval or the independent host state ACK.
    for assertion in topology["assertions"]:
        if (assertion.get("node") in nodes
                and assertion.get("probe") == "valve_commands_executed"
                and assertion.get("minimum_gain") is not None
                and assertion.get("to_sample") in samples
                and assertion.get("from_sample") == assertion["to_sample"] - 1):
            assertion["minimum"] = assertion.pop("minimum_gain")
            assertion["sample"] = assertion.pop("to_sample")
            assertion.pop("from_sample")
    # Fault-scoped expiry guards follow the requested boot boundaries too.
    # A host-only restart cannot reset an RF counter; a real RF reset must.
    for assertion in topology["assertions"]:
        if " no CAN expiry outside injected ACK loss at sample " not in assertion["name"]:
            continue
        sample = assertion.get("sample", assertion.get("to_sample"))
        node = assertion["node"]
        for key in ("sample", "maximum", "from_sample", "to_sample", "maximum_gain"):
            assertion.pop(key, None)
        if sample == 0 or (node in nodes and sample in samples):
            assertion.update(sample=sample, maximum=0)
        else:
            assertion.update(from_sample=sample-1, to_sample=sample, maximum_gain=0)
    host_runs = 1 + (len(samples) if "groundstation" in nodes else 0)
    restart_checks = {
        "GroundStation preference survives discovery and process restart",
        "GroundStation discovered every board by autonomous name",
        "GroundStation attributed traffic to every board identity",
        "GroundStation network graph labelled every board with its own traffic",
    }
    for assertion in topology["host_log_assertions"]:
        if assertion["name"] in restart_checks:
            assertion["minimum_occurrences"] = host_runs
    if "gateway" in nodes:
        qualify_gateway_restart(directory, topology, samples)
    path.write_text(json.dumps(topology, indent=2))

def qualify_gateway_restart(directory, topology, samples):
    """A powered-off route can reject submissions, but must recover promptly."""
    path = directory / "valve.json"
    layout = json.loads(path.read_text())
    probes = layout["execution"]["memory_probes"]
    for probe in probes:
        if probe["name"] == "umbilical_status_fail":
            probe.pop("maximum", None)
    if not any(p["name"] == "status_report_pending" for p in probes):
        probes.append(dict(name="status_report_pending", symbol="status_report_pending"))
    assertions = topology.setdefault("assertions", [])
    assertions.append(dict(name="No Valve status errors before planned Gateway restart",
        node="valve", probe="umbilical_status_fail", sample=samples[0]-1, maximum=0))
    for index, sample in enumerate(samples):
        end = samples[index+1]-1 if index+1 < len(samples) else topology["sample_count"]-1
        if sample >= end:
            raise ValueError("Gateway restart requires at least two post-restart samples to prove recovery")
        assertions.extend([
            dict(name=f"Valve status errors stop after Gateway restart {index+1}",
                 node="valve", probe="umbilical_status_fail", maximum_gain=0,
                 from_sample=sample, to_sample=end),
            dict(name=f"No stranded Valve status after Gateway restart {index+1}",
                 node="valve", probe="status_report_pending", sample=end, maximum=0),
        ])
    path.write_text(json.dumps(layout, indent=2))

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
    parser.add_argument("--ultra-soak", action="store_true", help="run the short gate, then the complete seven-board soak (default: ten minutes)")
    parser.add_argument("--restart-regression", action="store_true", help="run the short gate, then a 120-second restart regression (not ten-minute qualification)")
    parser.add_argument("--soak-only", action="store_true", help="run only the requested long test; does not qualify the short gate")
    parser.add_argument("--soak-ms", type=int, default=600000,
                        help="ultra-soak simulated duration in milliseconds (default: 600000)")
    parser.add_argument("--soak-samples", type=int, default=12,
                        help="ultra-soak health/command observations (default: 12; use 360 for six hours)")
    parser.add_argument("--evidence-dir", type=Path,
                        help="retain full logs, memory probes and sparse SD images in this container-writable directory")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--restart-nodes", nargs="+", choices=RESTART_NODES,
                        help="override long-test reboot group; unselected peers stay powered")
    parser.add_argument("--restart-samples", nargs="+", type=int,
                        help="restart the selected group after these samples (default: two-thirds)")
    parser.add_argument('--hardware-fill-profile',action='store_true',
        help='Run the captured P6/1-Mbaud Ethernet fill baseline with continuous rate/freshness checks; no injected outage')
    parser.add_argument('--fill-duration-ms',type=int,default=120000,
        help='hardware fill simulated duration; >=70000 and a multiple of10000 (600000 for ten minutes)')
    parser.add_argument("--pressure-sd-logging",action="store_true",
        help="Require current250Hz valve-to-DAQ SD capture, transport, storage and loss counters in the hardware-fill profile")
    parser.add_argument("--pressure-sd-diagnostics",action="store_true",
        help="Capture pressure ADC/TIM/DMA registers in any profile; does not qualify250Hz logging")
    parser.add_argument("--radio-profile", type=Path,
        help="explicit finite shared-medium JSON profile; preserves all existing acceptance gates")
    parser.add_argument("--radio-rrls", action="store_true",
        help="add a second host on the same finite radio medium; requires --radio-profile")
    parser.add_argument("--radio-trace-until-ms", type=int,
        help="retain bounded passive UART traces through this virtual time; requires profile/evidence-dir")
    parser.add_argument("--host-heartbeat-period-ms", type=int,
        help="explicit hosted heartbeat period for finite-radio traffic qualification;100..60000ms")
    parser.add_argument("--policy-qualified-controls",action="store_true",
        help="policy-correct normal/HITL controls with mandatory per-ID500ms ALL-owner receipt audit; requires --evidence-dir")
    args = parser.parse_args()
    if args.policy_qualified_controls and args.evidence_dir is None:
        parser.error("--policy-qualified-controls requires --evidence-dir")
    if args.radio_rrls and not args.radio_profile:
        parser.error("--radio-rrls requires --radio-profile")
    if args.host_heartbeat_period_ms is not None and (
        not args.radio_profile or not 100 <= args.host_heartbeat_period_ms <= 60_000
    ):
        parser.error("--host-heartbeat-period-ms requires --radio-profile and100..60000ms")
    if args.radio_trace_until_ms is not None and (not args.radio_profile or args.evidence_dir is None or not 1<=args.radio_trace_until_ms<=3_600_000):
        parser.error("--radio-trace-until-ms requires --radio-profile, --evidence-dir and1..3600000ms")
    radio_profile = None
    if args.radio_profile:
        from radio_profile import validate_profile
        radio_profile = validate_profile(json.loads(args.radio_profile.read_text()))

    if args.pressure_sd_logging and not args.hardware_fill_profile:
        parser.error("--pressure-sd-logging requires --hardware-fill-profile")
    if args.fill_duration_ms<70000 or args.fill_duration_ms%10000:
        parser.error('--fill-duration-ms requires >=70000 and exact ten-second windows')
    if args.fill_duration_ms!=120000 and not args.hardware_fill_profile:
        parser.error('--fill-duration-ms requires --hardware-fill-profile')
    if args.hardware_fill_profile and (args.ultra_soak or args.restart_regression or args.disconnect_node or args.fault_elf or args.restart_nodes):
        parser.error('--hardware-fill-profile is a separate no-outage baseline')
    if args.soak_only and not (args.ultra_soak or args.restart_regression):
        parser.error("--soak-only requires --ultra-soak or --restart-regression")
    if args.restart_nodes and not (args.ultra_soak or args.restart_regression):
        parser.error("--restart-nodes requires --restart-regression or --ultra-soak")
    if args.restart_samples is not None and not args.restart_nodes:
        parser.error("--restart-samples requires --restart-nodes")
    if args.ultra_soak and args.restart_regression:
        parser.error("choose either --ultra-soak or --restart-regression")
    if (args.ultra_soak or args.restart_regression) and (args.disconnect_node or args.fault_elf):
        parser.error("--ultra-soak uses the full-system fault/reboot schedule, not the short route-loss scenario")
    if args.fault_elf and not args.disconnect_node:
        parser.error("--fault-elf requires --disconnect-node")
    if args.soak_ms != 600000 or args.soak_samples != 12:
        if not args.ultra_soak:
            parser.error("custom soak duration/samples require --ultra-soak")
    if args.soak_ms < 600000 or args.soak_samples < 12 or args.soak_ms // args.soak_samples < 1000:
        parser.error("soak requires at least ten minutes, twelve samples and one second per sample")
    root = args.workspace.resolve()
    if args.hardware_fill_profile and args.evidence_dir is None:
        args.evidence_dir=root/('hardware-fill-evidence-'+str(time.time_ns()))
    spec = importlib.util.spec_from_file_location("board_suite", root / "ActuatorBoard26/sim/run_full.py")
    suite = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(suite)
    load_layout = suite.load_layout_for_build
    def load_built_layout(board, _subdir):
        subdir = built_subdir(board)
        layout = load_layout(board, subdir)
        configure_allocator_probes(layout, board / "build" / subdir / "CMakeCache.txt")
        if args.hardware_fill_profile or board.name == 'DAQ-Board':
            configure_hardware_fill_probes(layout,board.name)
        if args.ultra_soak or args.pressure_sd_logging:
            configure_soak_storage(layout, board.name)
        if args.pressure_sd_logging or args.pressure_sd_diagnostics:
            configure_pressure_sd_probes(layout, board.name)
        fault_repo = {"valve": "ValveBoard26", "actuator": "ActuatorBoard26"}.get(args.disconnect_node)
        if args.fault_elf and board.name == fault_repo:
            layout["artifacts"]["elf"] = args.fault_elf
        return layout
    suite.load_layout_for_build = load_built_layout
    os.environ.update(SEDS_FIRMWARE_SIM_SUITE_ROOT=str(root),
                      SEDS_FIRMWARE_SIM_IMAGE=args.image, SEDS_FIRMWARE_SIM_SKIP_BUILD="1",
                      SEDS_FIRMWARE_SIM_NETWORK_TIME_MS=str(args.fill_duration_ms) if args.hardware_fill_profile else ("40000" if args.disconnect_node else "16000"),
                      SEDS_FIRMWARE_SIM_NETWORK_SAMPLES=str(args.fill_duration_ms//10000) if args.hardware_fill_profile else ("10" if args.disconnect_node else "6"),
                      SEDS_FIRMWARE_SIM_DOCKER_NETWORK="host")
    os.environ.pop("SEDS_FIRMWARE_SIM_SOURCE", None)
    container = f"seds-route-recovery-{os.getpid()}"
    def run(command, label):
        long_test = "ten-minute" in label
        if long_test and (args.soak_ms != 600000 or args.soak_samples != 12):
            label = label.replace("ten-minute", f"{args.soak_ms / 60000:g}-minute")
        if args.restart_regression:
            label = label.replace("ten-minute", "120-second restart regression")
        mount = next(value for value in command if value.endswith(":/simulation:ro"))
        directory = Path(mount.split(":", 1)[0])
        topology_path = directory / "topology.json"
        topology = json.loads(topology_path.read_text())
        for link in topology.get("links", []):
            if link["kind"] == "pico_fi":
                link["baud_rate"] = configured_pico_baud(root / "gateway-board26/build/Release_Script/CMakeCache.txt")
        for host in topology["host_nodes"]:
            host.setdefault("env", {}).update(GS_BIND_ADDRESS="127.0.0.1:0",GS_HTTPS_BIND_ADDRESS="off")
        if args.hardware_fill_profile:
            configure_hardware_fill_profile(directory,topology)
        if args.pressure_sd_logging:
            configure_pressure_sd_assertions(topology)
            configure_storage_assertions(topology,require_flush=True)
        if radio_profile is not None:
            from radio_profile import configure_radio_profile
            configure_radio_profile(directory, topology, radio_profile, args.radio_rrls)
        if args.host_heartbeat_period_ms is not None:
            from radio_profile import configure_host_heartbeat
            configure_host_heartbeat(topology, args.host_heartbeat_period_ms)
        if args.policy_qualified_controls:
            configure_policy_qualified_controls(topology)
        topology_path.write_text(json.dumps(topology, indent=2))
        if args.disconnect_node:
            configure_fault(directory, args.disconnect_node)
        run_kind = "hardware-fill-baseline" if args.hardware_fill_profile else (args.disconnect_node or "baseline")
        if long_test and not args.restart_regression:
            run_kind = "ten-minute-soak" if args.soak_ms == 600000 and args.soak_samples == 12 else f"soak-{args.soak_ms}ms-{args.soak_samples}samples"
        elif "120-second restart regression" in label:
            run_kind = "restart-regression"
        if args.restart_nodes and long_test:
            configure_restarts(directory, args.restart_nodes, args.restart_samples)
            run_kind += "-" + "-".join(args.restart_nodes)
            if args.restart_samples:
                run_kind += "-samples-" + "-".join(map(str, args.restart_samples))
        if args.ultra_soak:
            recording_topology = json.loads(topology_path.read_text())
            configure_storage_assertions(recording_topology, require_flush=long_test)
            topology_path.write_text(json.dumps(recording_topology, indent=2))
        if args.fault_elf:
            run_kind += "-negative-control"
        if radio_profile is not None:
            run_kind += "-finite-radio" + ("-rrls" if args.radio_rrls else "")
        evidence = root / f"route-recovery-{run_kind}.json"
        evidence.write_text((directory / "topology.json").read_text())
        if args.evidence_dir:
            configure_evidence_capture(command, args.evidence_dir)
        if args.radio_trace_until_ms is not None:
            command[2:2] = ["-e", "FIRMWARE_SIM_RADIO_TRACE_DIR=/diagnostics/radio-trace", "-e",
                "FIRMWARE_SIM_RADIO_TRACE_UNTIL_MS=" + str(args.radio_trace_until_ms)]
        command[2:2] = ["--name", container]
        started = time.monotonic()
        prior_policy_captures = policy_capture_paths(args.evidence_dir) if args.policy_qualified_controls else None
        process = subprocess.Popen(command)
        failure = None
        try:
            while True:
                try:
                    result = process.wait(timeout=10)
                    if result:
                        raise subprocess.CalledProcessError(result, command)
                    if args.hardware_fill_profile:
                        validate_fill_evidence(args.evidence_dir, args.fill_duration_ms)
                    break
                except subprocess.TimeoutExpired:
                    elapsed = time.monotonic() - started
                    print(f"[SIM] {label}: {elapsed:.0f}s elapsed", flush=True)
                    if elapsed >= args.timeout:
                        raise TimeoutError("Simulation deadline exceeded; NOT a pass")
        except BaseException as error:
            failure = error
        finally:
            if process.poll() is None:
                subprocess.run([command[0], "stop", "--time", "5", container], check=False)
                process.wait(timeout=20)
        if args.policy_qualified_controls:
            failure = policy_validation_failure(args.evidence_dir, failure, prior_policy_captures)
        if failure is not None:
            raise failure
    suite.run_live = run
    class UI:
        @staticmethod
        def say(kind, message):
            print(f"[{kind}] {message}", flush=True)
    if not args.soak_only:
        suite.run_network_simulation(UI(), root / "ActuatorBoard26", "stm32g4", "Release_Script")
    if args.ultra_soak or args.restart_regression:
        os.environ["SEDS_FIRMWARE_SIM_SOAK_MS"] = "120000" if args.restart_regression else str(args.soak_ms)
        if args.soak_samples != 12:
            original_sample_count = suite.network_sample_count
            suite.network_sample_count = lambda layouts, requested: original_sample_count(layouts, max(requested, args.soak_samples))
        suite.run_network_simulation(UI(), root / "ActuatorBoard26", "stm32g4", "Release_Script", ultra_soak=True)
    print("PASS: long test only; short gate not executed" if args.soak_only else "PASS: firmware network and route recovery", flush=True)

if __name__ == "__main__":
    main()
