use firmware_sim::{
    core::{Architecture, ArchitectureKind, FixedPool},
    layout::{MemoryLayout, MemoryRegion},
};

#[test]
fn validates_all_architectures() {
    for kind in [
        ArchitectureKind::Stm32g4,
        ArchitectureKind::Stm32h5,
        ArchitectureKind::Stm32u5,
    ] {
        firmware_sim::simulator::self_test(kind).unwrap();
    }
}

#[test]
fn rejects_overlapping_slot() {
    let memory = MemoryLayout {
        flash_base: 0x08000000,
        flash_size: 0x80000,
        ram_regions: vec![MemoryRegion {
            name: "sram".into(),
            base: 0x20000000,
            size: 0x1c000,
        }],
        bootloader_size: 0x4000,
        slot_a_base: 0x08002000,
        slot_a_size: 0x70000,
        slot_b_base: None,
        slot_b_size: None,
        delta_base: None,
        delta_size: None,
        erase_size: 0x800,
        write_alignment: 8,
        sedsnet_pool: 4096,
    };
    assert!(Architecture::for_kind(ArchitectureKind::Stm32g4)
        .validate(&memory)
        .is_err());
}

#[test]
fn fixed_pool_never_overcommits() {
    let mut pool = FixedPool::new(64);
    assert!(pool.allocate(64));
    assert!(!pool.allocate(1));
    assert!(pool.release(64));
    assert_eq!(pool.stats().bytes_in_use, 0);
}

#[test]
fn rejects_pool_larger_than_physical_ram() {
    let mut memory = MemoryLayout {
        flash_base: 0x08000000,
        flash_size: 0x80000,
        ram_regions: vec![MemoryRegion {
            name: "sram".into(),
            base: 0x20000000,
            size: 0x1000,
        }],
        bootloader_size: 0x4000,
        slot_a_base: 0x08004000,
        slot_a_size: 0x74000,
        slot_b_base: None,
        slot_b_size: None,
        delta_base: None,
        delta_size: None,
        erase_size: 0x800,
        write_alignment: 8,
        sedsnet_pool: 0x1001,
    };
    assert!(Architecture::for_kind(ArchitectureKind::Stm32g4)
        .validate(&memory)
        .is_err());
    memory.sedsnet_pool = 0x1000;
    Architecture::for_kind(ArchitectureKind::Stm32g4)
        .validate(&memory)
        .unwrap();
}

#[test]
fn rejects_overlapping_physical_ram_banks() {
    let mut memory = MemoryLayout {
        flash_base: 0x08000000,
        flash_size: 0x80000,
        ram_regions: vec![
            MemoryRegion {
                name: "sram1".into(),
                base: 0x20000000,
                size: 0x2000,
            },
            MemoryRegion {
                name: "sram2".into(),
                base: 0x20001000,
                size: 0x2000,
            },
        ],
        bootloader_size: 0x4000,
        slot_a_base: 0x08004000,
        slot_a_size: 0x74000,
        slot_b_base: None,
        slot_b_size: None,
        delta_base: None,
        delta_size: None,
        erase_size: 0x800,
        write_alignment: 8,
        sedsnet_pool: 1024,
    };
    assert!(Architecture::for_kind(ArchitectureKind::Stm32g4)
        .validate(&memory)
        .is_err());
    memory.ram_regions[1].base = 0x20002000;
    Architecture::for_kind(ArchitectureKind::Stm32g4)
        .validate(&memory)
        .unwrap();
}

#[test]
fn crash_snapshot_exposes_cortex_m_fault_state() {
    use firmware_sim::{
        core::CrashDiagnostic,
        layout::{Artifacts, BoardLayout, ExecutionConfig, OtaConfig},
        traffic::TrafficConfig,
    };
    let layout = BoardLayout {
        name: "debug-board".into(),
        architecture: ArchitectureKind::Stm32h5,
        memory: MemoryLayout {
            flash_base: 0x08000000,
            flash_size: 0x80000,
            ram_regions: vec![MemoryRegion {
                name: "sram".into(),
                base: 0x20000000,
                size: 0x40000,
            }],
            bootloader_size: 0x4000,
            slot_a_base: 0x08004000,
            slot_a_size: 0x74000,
            slot_b_base: None,
            slot_b_size: None,
            delta_base: None,
            delta_size: None,
            erase_size: 0x2000,
            write_alignment: 16,
            sedsnet_pool: 1024,
        },
        artifacts: Artifacts {
            elf: "firmware.elf".into(),
            bootloader_elf: "bootloader.elf".into(),
            firmware: "firmware.bin".into(),
            bootloader: "boot.bin".into(),
            factory: "factory.bin".into(),
            ota: None,
        },
        execution: ExecutionConfig::default(),
        traffic: TrafficConfig::default(),
        ota: OtaConfig::default(),
        peripherals: vec![],
    };
    let diagnostic =
        CrashDiagnostic::capture(&layout, 7, "peripheral_execution", "bus fault".into());
    assert!(diagnostic.registers.pc >= 0x08004000);
    assert_eq!(diagnostic.registers.xpsr & (1 << 24), 1 << 24);
    assert!(diagnostic.fault_registers.sfsr.is_some());
}
