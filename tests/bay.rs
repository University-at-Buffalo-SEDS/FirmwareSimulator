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
    std::env::remove_var("RENODE");
    std::env::remove_var("FIRMWARE_SIM_CONTAINER");
    fs::remove_dir_all(root).unwrap();
}
