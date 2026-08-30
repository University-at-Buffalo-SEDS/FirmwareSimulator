use std::{fs, os::unix::fs::PermissionsExt, path::PathBuf, process};

use firmware_sim::{layout::BoardLayout, report, simulator};
use serde_json::json;

#[test]
fn file_defined_board_and_firmware_run_end_to_end() {
    let root = std::env::temp_dir().join(format!(
        "firmware-sim-integration-{}-{}",
        process::id(),
        0x5ed5_u64
    ));
    fs::create_dir_all(root.join("build")).unwrap();
    fs::write(root.join("build/firmware.img"), vec![0x11; 4096]).unwrap();
    fs::write(root.join("build/bootloader.bin"), vec![0x22; 1024]).unwrap();
    let mut factory = vec![0x33; 8192];
    factory[0..4].copy_from_slice(&0x2001_c000_u32.to_le_bytes());
    factory[4..8].copy_from_slice(&0x0800_4001_u32.to_le_bytes());
    fs::write(root.join("build/factory.bin"), factory).unwrap();
    fs::write(root.join("build/update.seds"), vec![0x44; 2048]).unwrap();
    let mut elf = vec![0_u8; 88];
    elf[0..6].copy_from_slice(b"\x7fELF\x01\x01");
    elf[24..28].copy_from_slice(&0x0800_4001_u32.to_le_bytes());
    elf[28..32].copy_from_slice(&52_u32.to_le_bytes());
    elf[42..44].copy_from_slice(&32_u16.to_le_bytes());
    elf[44..46].copy_from_slice(&1_u16.to_le_bytes());
    elf[52..56].copy_from_slice(&1_u32.to_le_bytes());
    elf[56..60].copy_from_slice(&84_u32.to_le_bytes());
    elf[60..64].copy_from_slice(&0x0800_4000_u32.to_le_bytes());
    elf[64..68].copy_from_slice(&0x0800_4000_u32.to_le_bytes());
    elf[68..72].copy_from_slice(&4_u32.to_le_bytes());
    elf[72..76].copy_from_slice(&4_u32.to_le_bytes());
    fs::write(root.join("build/firmware.elf"), &elf).unwrap();
    fs::write(root.join("build/bootloader.elf"), &elf).unwrap();
    let renode = root.join("renode-mock");
    fs::write(&renode, "#!/bin/sh\nprintf 'SEDS_FIRMWARE_BOOT_REACHED\\nSEDS_FACTORY_BOOT_REACHED\\nSEDS_REG FIRMWARE_PC\\n0x08004101\\nSEDS_REG FACTORY_PC\\n0x08004101\\nSEDS_EXECUTION_COMPLETE\\n'\n").unwrap();
    fs::set_permissions(&renode, fs::Permissions::from_mode(0o755)).unwrap();
    std::env::set_var("RENODE", &renode);
    std::env::set_var("FIRMWARE_SIM_CONTAINER", "1");
    let layout_path: PathBuf = root.join("board.json");
    fs::write(
        &layout_path,
        serde_json::to_vec_pretty(&json!({
            "name": "integration-board",
            "architecture": "stm32g4",
            "mcu": "stm32g491",
            "memory": {
                "flash_base": 134217728,
                "flash_size": 524288,
                "ram_regions": [{"name":"sram","base":536870912,"size":114688}],
                "bootloader_size": 16384,
                "slot_a_base": 134234112,
                "slot_a_size": 475136,
                "delta_base": 134709248,
                "delta_size": 24576,
                "erase_size": 2048,
                "write_alignment": 8,
                "sedsnet_pool": 4096
            },
            "artifacts": {
                "elf": "build/firmware.elf",
                "bootloader_elf": "build/bootloader.elf",
                "firmware": "build/firmware.img",
                "bootloader": "build/bootloader.bin",
                "factory": "build/factory.bin",
                "ota": "build/update.seds"
            },
            "traffic": {"iterations": 1000, "max_payload": 128, "immediate_dispatch": true},
            "peripherals": [
                {"type": "imu", "name": "imu", "failure_every": 17},
                {"type": "barometer", "name": "barometer"},
                {"type": "gps", "name": "gps", "disconnect_after": 900},
                {"type": "adc", "name": "adc", "bits": 12, "channels": 8},
                {"type": "pressure_transducer", "name": "pressure", "max_psi": 5000}
            ]
        }))
        .unwrap(),
    )
    .unwrap();

    let layout = BoardLayout::load(&layout_path).unwrap();
    let report = simulator::run(&layout, &root, 42).unwrap();
    assert_eq!(report.devices.len(), 5);
    assert_eq!(report.traffic.pool.bytes_in_use, 0);
    assert_ne!(report.update.original_sha256, report.update.updated_sha256);
    assert_eq!(report.ota_bytes, Some(2048));
    assert!(report.execution.instruction_execution_observed);
    let rendered = report::simulation(&report);
    assert!(rendered.contains("Fault test"));
    assert!(rendered.contains("Injected / Expected"));
    assert!(rendered.contains("Disconnected / Expected"));
    assert!(rendered.contains("58 / 58"));
    assert!(rendered.contains("100 / 100"));
    assert!(rendered.contains("2 fault-injection schedules passed"));
    std::env::remove_var("RENODE");
    std::env::remove_var("FIRMWARE_SIM_CONTAINER");
    fs::remove_dir_all(root).unwrap();
}
