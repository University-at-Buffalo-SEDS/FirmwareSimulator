use firmware_sim::{
    core::{mcu_catalog, Architecture, ArchitectureKind, FixedPool, McuKind},
    layout::{MemoryLayout, MemoryRegion},
};

#[test]
fn every_built_in_mcu_descriptor_is_valid_and_has_a_matching_platform() {
    for descriptor in mcu_catalog() {
        descriptor.validate_definition().unwrap();
        let platform = std::fs::read_to_string(
            std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("renode/platforms")
                .join(&descriptor.platform_file),
        )
        .unwrap();
        assert!(platform.contains(&format!("cpuType: \"{}\"", descriptor.core_model)));
        let expected_fdcan = match descriptor.architecture {
            ArchitectureKind::Stm32g4 => "CAN.STM32_FDCAN",
            ArchitectureKind::Stm32h5 => "CAN.SedsStm32H5Fdcan",
            ArchitectureKind::Stm32u5 => "CAN.SedsStm32U5Fdcan",
            ArchitectureKind::Stm32 => continue,
        };
        assert!(
            platform.contains(expected_fdcan),
            "{} must use its fixed-layout FDCAN model",
            descriptor.name
        );
        assert!(
            !platform.contains("CAN.MCAN"),
            "{} must not use configurable generic M_CAN",
            descriptor.name
        );
    }
    assert!(mcu_catalog().len() >= 22);
    assert_eq!(
        mcu_catalog()
            .iter()
            .filter(|mcu| mcu.board_validated)
            .count(),
        3
    );
}

#[test]
fn validates_all_architectures() {
    for kind in [
        ArchitectureKind::Stm32,
        ArchitectureKind::Stm32g4,
        ArchitectureKind::Stm32h5,
        ArchitectureKind::Stm32u5,
    ] {
        firmware_sim::simulator::self_test(kind).unwrap();
    }
}

#[test]
fn exact_mcu_must_match_architecture_and_capacity() {
    let mut memory = MemoryLayout {
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
        delta_base: None,
        delta_size: None,
        erase_size: 0x800,
        write_alignment: 8,
        sedsnet_pool: 4096,
    };
    let architecture = Architecture::for_kind(ArchitectureKind::Stm32g4);
    architecture
        .validate_mcu(McuKind::new("stm32g491").descriptor().unwrap(), &memory)
        .unwrap();
    assert!(architecture
        .validate_mcu(McuKind::new("stm32h523").descriptor().unwrap(), &memory)
        .is_err());
    memory.flash_size += 1;
    assert!(architecture
        .validate_mcu(McuKind::new("stm32g491").descriptor().unwrap(), &memory)
        .is_err());
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
        layout::{Artifacts, BoardConfig, BoardLayout, ExecutionConfig, OtaConfig},
        traffic::TrafficConfig,
    };
    let layout = BoardLayout {
        name: "debug-board".into(),
        architecture: ArchitectureKind::Stm32h5,
        mcu: McuKind::new("stm32h523"),
        mcu_descriptor: None,
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
            updated_firmware: None,
            ota: None,
        },
        execution: ExecutionConfig::default(),
        traffic: TrafficConfig::default(),
        ota: OtaConfig::default(),
        board: BoardConfig::default(),
        peripherals: vec![],
    };
    let diagnostic =
        CrashDiagnostic::capture(&layout, 7, "peripheral_execution", "bus fault".into());
    assert!(diagnostic.registers.is_none());
    assert!(diagnostic.fault_registers.is_none());
    assert!(diagnostic.note.contains("never emitted"));
}
