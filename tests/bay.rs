use firmware_sim::bay;
use serde_json::json;
use std::{fs, os::unix::fs::PermissionsExt, process};

#[test]
fn linked_bay_runs_two_real_elf_nodes_on_a_can_hub() {
    let root = std::env::temp_dir().join(format!("firmware-sim-bay-{}", process::id()));
    fs::create_dir_all(root.join("a/build")).unwrap();
    fs::create_dir_all(root.join("b/build")).unwrap();
    let board = |name: &str| {
        json!({
            "name": name, "architecture": "stm32g4", "mcu": "stm32g491",
            "memory": {"flash_base": 134217728, "flash_size": 524288, "ram_regions": [{"name":"sram","base":536870912,"size":114688}], "bootloader_size": 16384, "slot_a_base": 134234112, "slot_a_size": 475136, "erase_size": 2048, "write_alignment": 8, "sedsnet_pool": 4096},
            "artifacts": {"elf": "build/fw.elf", "bootloader_elf": "build/boot.elf", "firmware": "build/fw.img", "bootloader": "build/boot.bin", "factory": "build/factory.bin"},
            "execution": {"memory_probes": [
                {"name": "pool_available", "symbol": "pool_available", "minimum": 2048},
                {"name": "can_tx", "symbol": "can_tx"},
                {"name": "can_rx", "symbol": "can_rx"},
                {"name": "topology_mask", "symbol": "topology_mask"}
            ]}
        })
    };
    let mut elf = vec![0_u8; 96];
    elf[0..6].copy_from_slice(b"\x7fELF\x01\x01");
    elf[24..28].copy_from_slice(&0x0800_4001_u32.to_le_bytes());
    elf[28..32].copy_from_slice(&52_u32.to_le_bytes());
    elf[42..44].copy_from_slice(&32_u16.to_le_bytes());
    elf[44..46].copy_from_slice(&1_u16.to_le_bytes());
    elf[52..56].copy_from_slice(&1_u32.to_le_bytes());
    elf[56..60].copy_from_slice(&84_u32.to_le_bytes());
    elf[60..64].copy_from_slice(&0x0800_4000_u32.to_le_bytes());
    elf[64..68].copy_from_slice(&0x0800_4000_u32.to_le_bytes());
    elf[68..72].copy_from_slice(&12_u32.to_le_bytes());
    elf[72..76].copy_from_slice(&12_u32.to_le_bytes());
    elf[84..88].copy_from_slice(&0x2001_c000_u32.to_le_bytes());
    elf[88..92].copy_from_slice(&0x0800_4001_u32.to_le_bytes());
    for name in ["a", "b"] {
        fs::write(
            root.join(name).join("board.json"),
            serde_json::to_vec(&board(name)).unwrap(),
        )
        .unwrap();
        fs::write(root.join(name).join("build/fw.elf"), &elf).unwrap();
        let mut factory = vec![0xff; 0x400c];
        factory[..8].copy_from_slice(&elf[84..92]);
        factory[0x4000..].copy_from_slice(&elf[84..]);
        fs::write(root.join(name).join("build/factory.bin"), factory).unwrap();
    }
    let topology = root.join("bay.json");
    fs::write(
        &topology,
        serde_json::to_vec(&json!({
            "name": "test-bay", "nodes": [
                {"name": "a", "layout": "a/board.json", "firmware_root": "a"},
                {"name": "b", "layout": "b/board.json", "firmware_root": "b"}
            ], "links": [{"name": "can", "kind": "can", "endpoints": [
                {"node": "a", "peripheral": "fdcan2", "tx_probe": "can_tx", "rx_probe": "can_rx"},
                {"node": "b", "peripheral": "fdcan2", "tx_probe": "can_tx", "rx_probe": "can_rx"}
            ]}],
            "assertions": [
                {"name": "a discovered b", "node": "a", "probe": "topology_mask", "required_bits": 2},
                {"name": "b discovered a", "node": "b", "probe": "topology_mask", "required_bits": 1}
            ]
        }))
        .unwrap(),
    )
    .unwrap();
    let renode = root.join("renode-mock");
    fs::write(&renode, "#!/bin/sh\necho SEDS_NODE_BOOT_a\necho SEDS_NODE_BOOT_b\ni=0\nwhile [ $i -lt 10 ]; do\n  echo SEDS_BAY_PROBE a pool_available $i\n  echo 0x1000\n  echo SEDS_BAY_PROBE b pool_available $i\n  echo 0x1000\n  echo SEDS_BAY_PROBE a can_tx $i\n  echo 0x5\n  echo SEDS_BAY_PROBE a can_rx $i\n  echo 0x6\n  echo SEDS_BAY_PROBE b can_tx $i\n  echo 0x6\n  echo SEDS_BAY_PROBE b can_rx $i\n  echo 0x5\n  echo SEDS_BAY_PROBE a topology_mask $i\n  echo 0x2\n  echo SEDS_BAY_PROBE b topology_mask $i\n  echo 0x1\n  i=$((i + 1))\ndone\necho SEDS_NODE a PC\necho 0x08004101\necho SEDS_NODE b PC\necho 0x08004101\necho SEDS_BAY_COMPLETE\n").unwrap();
    // The fake executor obeys the same dedicated capture channel as Renode.
    let mock = fs::read_to_string(&renode)
        .unwrap()
        .replace("done\n", "done > \"$SEDS_SIM_PROBE_CAPTURE\"\n");
    let mock = mock.replace(
        "  i=$((i + 1))",
        "echo SEDS_BAY_PROBE a can_fifo_overflow_fdcan1 $i\necho 0x0\necho SEDS_BAY_PROBE a can_fifo_overflow_fdcan2 $i\necho 0x0\necho SEDS_BAY_PROBE b can_fifo_overflow_fdcan1 $i\necho 0x0\necho SEDS_BAY_PROBE b can_fifo_overflow_fdcan2 $i\necho 0x0\n  i=$((i + 1))",
    );
    // A plausible but false console reading must not influence memory checks.
    let mock = mock.replace(
        "echo SEDS_BAY_COMPLETE",
        "echo SEDS_BAY_PROBE a pool_available 0\necho 0x0\necho SEDS_BAY_COMPLETE",
    );
    fs::write(&renode, &mock).unwrap();
    fs::set_permissions(&renode, fs::Permissions::from_mode(0o755)).unwrap();
    std::env::set_var("RENODE", &renode);
    std::env::set_var("FIRMWARE_SIM_CONTAINER", "1");
    let report = bay::run(&topology).unwrap();
    assert_eq!(report.nodes_executed, 2);
    assert_eq!(report.links_connected, 1);
    assert_eq!(report.register_dump.len(), 4);
    assert_eq!(report.memory_profiles["a"][0].samples.len(), 10);
    assert_eq!(report.memory_profiles["b"][0].minimum_observed, 4096);
    assert_eq!(report.link_reports[0].endpoints[0].tx_observed, Some(5));
    assert_eq!(report.assertion_reports.len(), 2);
    // Boot evidence has a dedicated file channel, independent of Renode's
    // asynchronous INFO logger. Missing/corrupt records must never imply boot.
    let file_boot = mock
        .replace(
            "echo SEDS_NODE_BOOT_a",
            "echo SEDS_NODE_BOOT_a > \"$SEDS_SIM_BOOT_CAPTURE_DIR/boot-a.marker\"",
        )
        .replace(
            "echo SEDS_NODE_BOOT_b",
            "echo SEDS_NODE_BOOT_b > \"$SEDS_SIM_BOOT_CAPTURE_DIR/boot-b.marker\"",
        );
    fs::write(&renode, &file_boot).unwrap();
    assert_eq!(bay::run(&topology).unwrap().nodes_executed, 2);
    fs::write(&renode, mock.replace("echo SEDS_NODE_BOOT_a", ":")).unwrap();
    assert!(bay::run(&topology)
        .unwrap_err()
        .to_string()
        .contains("did not reach its boot-success symbol"));
    fs::write(
        &renode,
        file_boot.replace("echo SEDS_NODE_BOOT_a >", "echo WRONG >"),
    )
    .unwrap();
    assert!(bay::run(&topology)
        .unwrap_err()
        .to_string()
        .contains("invalid boot capture"));
    // Hardware FIFO loss must fail even when ordinary firmware probes pass.
    fs::write(
        &renode,
        mock.replace(
            "echo SEDS_BAY_PROBE a can_fifo_overflow_fdcan1 $i\necho 0x0",
            "echo SEDS_BAY_PROBE a can_fifo_overflow_fdcan1 $i\necho 0x1",
        ),
    )
    .unwrap();
    assert!(bay::run(&topology)
        .unwrap_err()
        .to_string()
        .contains("can_fifo_overflow_fdcan1"));
    // Console-only output is not accepted if the dedicated capture is lost.
    fs::write(
        &renode,
        mock.replace("done > \"$SEDS_SIM_PROBE_CAPTURE\"", "done"),
    )
    .unwrap();
    let error = format!("{:#}", bay::run(&topology).unwrap_err());
    assert!(error.contains("returned 0 of 10 samples"), "{error}");
    // Renode stays at its prompt after an invalid platform include. Holding
    // stdin open must not hide that error indefinitely from the caller.
    fs::write(&renode, "#!/bin/sh\necho \"There was an error executing command 'include @bad.repl'\"\nread ignored\n").unwrap();
    let start = std::time::Instant::now();
    let error = bay::run(&topology).unwrap_err().to_string();
    assert!(error.contains("include @bad.repl"), "{error}");
    assert!(start.elapsed() < std::time::Duration::from_secs(5));
    // A watchdog failure must exit without hanging on the runtime child and
    // retain the evidence before the temporary simulation directory disappears.
    let diagnostics = root.join("diagnostics");
    std::env::set_var("FIRMWARE_SIM_DIAGNOSTICS_DIR", &diagnostics);
    fs::write(
        &renode,
        "#!/bin/sh\necho SEDS_IWDG_RESET count=1\nsleep 60 &\nwait\n",
    )
    .unwrap();
    let start = std::time::Instant::now();
    let error = bay::run(&topology).unwrap_err().to_string();
    assert!(error.contains("SEDS_IWDG_RESET"), "{error}");
    assert!(start.elapsed() < std::time::Duration::from_secs(5));
    let directories: Vec<_> = fs::read_dir(&diagnostics)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .collect();
    let retained = directories
        .iter()
        .find(|path| path.join("renode.log").is_file())
        .unwrap();
    let live = directories
        .iter()
        .find(|path| path.join("renode.stdout.log").is_file())
        .unwrap();
    assert!(fs::read_to_string(live.join("renode.stdout.log"))
        .unwrap()
        .contains("SEDS_IWDG_RESET"));
    assert!(live.join("memory-probes.txt").is_file());
    assert!(live.join("bay.resc").is_file());
    assert!(fs::read_to_string(retained.join("renode.log"))
        .unwrap()
        .contains("SEDS_IWDG_RESET"));
    assert!(retained.join("bay.resc").is_file());
    // Evidence must already exist while execution is still in progress: an
    // external SIGKILL cannot run the normal final artifact-writing path.
    let release = root.join("release-live-probe");
    fs::write(&renode, format!(
        "#!/bin/sh\necho 'SEDS_BAY_PROBE a pool_available 0' > \"$SEDS_SIM_PROBE_CAPTURE\"\necho 0x1000 >> \"$SEDS_SIM_PROBE_CAPTURE\"\necho 'SEDS_BAY_SAMPLE_DONE 1'\nwhile [ ! -f '{}' ]; do sleep 0.02; done\necho \"There was an error executing command 'include @interrupted.repl'\"\nread ignored\n", release.display()
    )).unwrap();
    let live_topology = topology.clone();
    let running = std::thread::spawn(move || bay::run(&live_topology));
    let start = std::time::Instant::now();
    let mut observed_during_execution = false;
    while start.elapsed() < std::time::Duration::from_secs(5) {
        observed_during_execution = fs::read_dir(&diagnostics)
            .unwrap()
            .filter_map(Result::ok)
            .any(|entry| {
                fs::read_to_string(entry.path().join("renode.stdout.log"))
                    .unwrap_or_default()
                    .contains("SEDS_BAY_SAMPLE_DONE 1")
                    && fs::read_to_string(entry.path().join("memory-probes.txt"))
                        .unwrap_or_default()
                        .contains("0x1000")
            });
        if observed_during_execution {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(20));
    }
    fs::write(&release, "release").unwrap();
    let error = running.join().unwrap().unwrap_err().to_string();
    assert!(error.contains("interrupted.repl"), "{error}");
    assert!(
        observed_during_execution,
        "partial probes and checkpoint log must be readable before finalization"
    );
    // Launcher/runtime failures before the UART exists must retain their
    // actual stderr instead of reporting only a generic missing-PTY timeout.
    let mut startup_topology: serde_json::Value =
        serde_json::from_slice(&fs::read(&topology).unwrap()).unwrap();
    startup_topology["host_nodes"] = json!([{"name":"host", "binary":renode,
        "serial_links":[{"link":"radio", "env":"RADIO_PORT"}]}]);
    startup_topology["links"]
        .as_array_mut()
        .unwrap()
        .push(json!({
        "name":"radio", "kind":"uart", "endpoints":[
            {"node":"a", "peripheral":"usart2"},
            {"node":"host", "peripheral":"radio"}]}));
    fs::write(&topology, serde_json::to_vec(&startup_topology).unwrap()).unwrap();
    fs::write(
        &renode,
        "#!/bin/sh\necho 'diagnostic launcher unavailable' >&2\nexit 7\n",
    )
    .unwrap();
    let error = bay::run(&topology).unwrap_err().to_string();
    assert!(error.contains("did not create host UART PTY"), "{error}");
    assert!(error.contains("diagnostic launcher unavailable"), "{error}");
    std::env::remove_var("FIRMWARE_SIM_DIAGNOSTICS_DIR");
    std::env::remove_var("RENODE");
    std::env::remove_var("FIRMWARE_SIM_CONTAINER");
    fs::remove_dir_all(root).unwrap();
}
