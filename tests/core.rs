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
            ArchitectureKind::Stm32g4 | ArchitectureKind::Stm32h5 | ArchitectureKind::Stm32u5 => {
                "CAN.SedsFixedFdcan"
            }
            ArchitectureKind::Stm32 | ArchitectureKind::Stm32h7 => continue,
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
        if descriptor.architecture == ArchitectureKind::Stm32g4 {
            assert!(
                platform.contains(
                    "usart2: UART.SedsStm32Uart @ sysbus 0x40004400\n    frequency: 170000000\n    IRQ -> nvic@38"
                ),
                "{} must expose the G4 USART2 gateway interface",
                descriptor.name
            );
        }
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
        ArchitectureKind::Stm32h7,
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
        persistent_data_base: None,
        persistent_data_size: None,
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
        persistent_data_base: None,
        persistent_data_size: None,
        erase_size: 0x800,
        write_alignment: 8,
        sedsnet_pool: 4096,
    };
    assert!(Architecture::for_kind(ArchitectureKind::Stm32g4)
        .validate(&memory)
        .is_err());
}

#[test]
fn validates_reserved_persistent_flash_partition() {
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
        delta_base: Some(0x08078000),
        delta_size: Some(0x6000),
        persistent_data_base: Some(0x0807f000),
        persistent_data_size: Some(0x1000),
        erase_size: 0x800,
        write_alignment: 8,
        sedsnet_pool: 4096,
    };
    Architecture::for_kind(ArchitectureKind::Stm32g4)
        .validate(&memory)
        .unwrap();

    memory.persistent_data_base = Some(0x0807d800);
    assert!(Architecture::for_kind(ArchitectureKind::Stm32g4)
        .validate(&memory)
        .is_err());

    memory.persistent_data_base = Some(0x0807f000);
    memory.persistent_data_size = Some(0x800);
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
        persistent_data_base: None,
        persistent_data_size: None,
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
        persistent_data_base: None,
        persistent_data_size: None,
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
            persistent_data_base: None,
            persistent_data_size: None,
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

#[test]
fn h7_catalog_validates_physical_banks_and_flash_variants() {
    let expected = [
        "723", "725", "730", "733", "735", "742", "743", "745", "747", "750", "753", "755", "757",
        "7a3", "7b0", "7b3", "7r3", "7r7", "7s3", "7s7",
    ];
    assert_eq!(
        mcu_catalog()
            .iter()
            .filter(|d| d.architecture == ArchitectureKind::Stm32h7)
            .count(),
        expected.len()
    );
    for suffix in expected {
        let name = format!("stm32h{suffix}");
        let d = McuKind::new(&name).descriptor().unwrap();
        let mut memory = MemoryLayout {
            flash_base: d.flash_base,
            flash_size: d.flash_size,
            ram_regions: d.ram_regions.clone(),
            bootloader_size: 0x4000,
            slot_a_base: d.flash_base + 0x4000,
            slot_a_size: d.flash_size - 0x4000,
            slot_b_base: None,
            slot_b_size: None,
            delta_base: None,
            delta_size: None,
            persistent_data_base: None,
            persistent_data_size: None,
            erase_size: d.erase_size,
            write_alignment: d.write_alignment,
            sedsnet_pool: 4096,
        };
        let arch = Architecture::for_kind(ArchitectureKind::Stm32h7);
        arch.validate_mcu(d, &memory).unwrap();
        memory.ram_regions.push(MemoryRegion {
            name: "gap".into(),
            base: 0x21000000,
            size: 4096,
        });
        assert!(
            arch.validate_mcu(d, &memory).is_err(),
            "{name} accepted a RAM hole"
        );
        memory.ram_regions = d.ram_regions.clone();
        memory.write_alignment = 8;
        assert!(
            arch.validate_mcu(d, &memory).is_err(),
            "{name} accepted G4 flash geometry"
        );
        assert!(!d.trustzone_capable && !d.board_validated);
    }
    assert_eq!(
        McuKind::new("stm32h742").descriptor().unwrap().ram_size,
        692 * 1024
    );
    assert_eq!(
        McuKind::new("stm32h7s3").descriptor().unwrap().ram_size,
        620 * 1024
    );
    assert_eq!(
        McuKind::new("stm32h7s3").descriptor().unwrap().flash_size,
        64 * 1024
    );
}
