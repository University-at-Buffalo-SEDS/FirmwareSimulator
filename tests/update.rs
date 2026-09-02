use firmware_sim::{
    layout::{MemoryLayout, MemoryRegion},
    update::{interruption_matrix, Flash, UpdateStrategy},
};
fn memory() -> MemoryLayout {
    MemoryLayout {
        flash_base: 0x08000000,
        flash_size: 0x80000,
        ram_regions: vec![MemoryRegion {
            name: "sram".into(),
            base: 0x20000000,
            size: 0x1c000,
        }],
        bootloader_size: 0x4000,
        slot_a_base: 0x08004000,
        slot_a_size: 0x74000,
        slot_b_base: None,
        slot_b_size: None,
        delta_base: Some(0x08078000),
        delta_size: Some(0x6000),
        persistent_data_base: None,
        persistent_data_size: None,
        erase_size: 0x800,
        write_alignment: 8,
        sedsnet_pool: 4096,
    }
}

fn delta_transfer(size: usize) -> Vec<u8> {
    let mut transfer = vec![0x44; size];
    transfer[..4].copy_from_slice(&0x4c43_4450u32.to_le_bytes());
    transfer
}

#[test]
fn tests_delta_power_loss_matrix() {
    let report = interruption_matrix(
        &vec![0x11; 4096],
        &vec![0x22; 4096],
        &delta_transfer(1024),
        &memory(),
        128,
    )
    .unwrap();
    assert_eq!(report.strategy, UpdateStrategy::DeltaOnly);
    assert!(report.interruption_points_tested > report.chunks);
    assert!(report.recovery_required_points > 0);
    assert!(report.all_flash_operation_boundaries_tested);
    assert!(!report.cpu_reboots_executed);
}

#[test]
fn full_image_ota_uses_recovery_even_when_delta_staging_exists() {
    let report = interruption_matrix(
        &vec![0x11; 4096],
        &vec![0x22; 4096],
        &vec![0x55; 0x7000],
        &memory(),
        512,
    )
    .unwrap();
    assert_eq!(report.strategy, UpdateStrategy::RecoveryTransport);
}

#[test]
fn detects_dual_slot_and_recovery_layouts() {
    let mut dual = memory();
    dual.delta_base = None;
    dual.delta_size = None;
    dual.slot_a_size = 0x30000;
    dual.slot_b_base = Some(0x08034000);
    dual.slot_b_size = Some(0x30000);
    let dual_report =
        interruption_matrix(&[0x11; 4096], &[0x22; 4096], &[0x44; 2048], &dual, 128).unwrap();
    assert_eq!(dual_report.strategy, UpdateStrategy::DualSlot);
    assert_eq!(dual_report.recovery_required_points, 0);
    assert!(dual_report.old_image_boot_points > 0);
    assert!(dual_report.new_image_boot_points > 0);

    dual.slot_b_base = None;
    dual.slot_b_size = None;
    let recovery_report =
        interruption_matrix(&[0x11; 4096], &[0x22; 4096], &[0x44; 2048], &dual, 128).unwrap();
    assert_eq!(recovery_report.strategy, UpdateStrategy::RecoveryTransport);
}

#[test]
fn flash_enforces_real_programming_rules() {
    let mut flash = Flash::new(4096, 2048, 8);
    flash.erase(0, 2048).unwrap();
    flash.program(0, &[0x00; 8]).unwrap();
    assert!(flash.program(0, &[0xff; 8]).is_err());
    assert!(flash.erase(1, 2048).is_err());
}
