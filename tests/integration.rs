use std::{fs, path::PathBuf, process};

use firmware_sim::{layout::BoardLayout, simulator};
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
    fs::write(root.join("build/factory.bin"), vec![0x33; 8192]).unwrap();
    fs::write(root.join("build/update.seds"), vec![0x44; 2048]).unwrap();
    let layout_path: PathBuf = root.join("board.json");
    fs::write(
        &layout_path,
        serde_json::to_vec_pretty(&json!({
            "name": "integration-board",
            "architecture": "stm32g4",
            "memory": {
                "flash_base": 134217728,
                "flash_size": 524288,
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
    fs::remove_dir_all(root).unwrap();
}
