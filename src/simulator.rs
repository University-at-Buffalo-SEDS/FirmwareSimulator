use crate::{
    core::{Architecture, ArchitectureKind, CrashDiagnostic},
    execution::{self, ExecutionReport},
    layout::BoardLayout,
    peripherals::{self, DeviceReport},
    traffic::{self, TrafficReport},
    update::{self, UpdateReport},
};
use anyhow::{ensure, Context, Result};
use serde::Serialize;
use std::{fs, path::Path};

#[derive(Debug, Serialize)]
pub struct SimulationReport {
    pub board: String,
    pub architecture: ArchitectureKind,
    pub firmware_bytes: usize,
    pub bootloader_bytes: usize,
    pub factory_bytes: usize,
    pub ota_bytes: Option<usize>,
    pub physical_flash_bytes: u64,
    pub physical_ram_bytes: u64,
    pub physical_ram_regions: Vec<crate::layout::MemoryRegion>,
    pub devices: Vec<DeviceReport>,
    pub traffic: TrafficReport,
    pub update: UpdateReport,
    pub execution: ExecutionReport,
    pub fidelity: HardwareFidelityReport,
}

#[derive(Debug, Serialize)]
pub struct HardwareFidelityReport {
    pub exact_mcu: String,
    pub arm_instructions_executed: bool,
    pub factory_vectors_used_for_reset: bool,
    pub instruction_coupled_faults: bool,
    pub all_flash_operation_boundaries_modeled: bool,
    pub updater_and_reboot_executed_at_each_cut: bool,
    pub dynamic_rcc_clock_tree: bool,
    pub strict_unknown_mmio: bool,
    pub dma_data_path: bool,
    pub trustzone_attribution: bool,
    pub trustzone_core_enabled: bool,
    pub usb_sdmmc_ota_adapters: bool,
    pub gpio_pull_and_open_drain_behavior: bool,
    pub limitations: Vec<String>,
}
struct Images {
    firmware: Vec<u8>,
    bootloader: Vec<u8>,
    factory: Vec<u8>,
    ota: Option<Vec<u8>>,
    updated_firmware: Option<Vec<u8>>,
}

pub fn validate(layout: &BoardLayout, root: &Path) -> Result<()> {
    ensure!(!layout.name.is_empty(), "board name cannot be empty");
    Architecture::for_kind(layout.architecture).validate_mcu(layout.mcu(), &layout.memory)?;
    let images = load_images(layout, root)?;
    crate::elf::validate_elf(
        &root.join(&layout.artifacts.elf),
        &layout.memory,
        "firmware",
    )?;
    crate::elf::validate_elf(
        &root.join(&layout.artifacts.bootloader_elf),
        &layout.memory,
        "bootloader",
    )?;
    ensure!(
        images.bootloader.len() as u64 <= layout.memory.bootloader_size,
        "bootloader exceeds partition"
    );
    ensure!(!images.bootloader.is_empty(), "bootloader image is empty");
    ensure!(!images.firmware.is_empty(), "firmware image is empty");
    ensure!(
        images.firmware.len() as u64 <= layout.memory.slot_a_size,
        "firmware exceeds slot A"
    );
    ensure!(
        images.factory.len() as u64 <= layout.memory.flash_size,
        "factory image exceeds flash"
    );
    ensure!(
        images.factory.len() >= 8,
        "factory image has no reset vector"
    );
    let initial_sp = u32::from_le_bytes(images.factory[0..4].try_into().unwrap()) as u64;
    let reset_handler = u32::from_le_bytes(images.factory[4..8].try_into().unwrap()) as u64;
    ensure!(
        layout.memory.ram_regions.iter().any(|region| {
            initial_sp >= region.base && initial_sp <= region.base.saturating_add(region.size)
        }),
        "factory initial MSP 0x{initial_sp:08x} is outside physical RAM"
    );
    ensure!(
        reset_handler & 1 == 1
            && reset_handler >= layout.memory.flash_base
            && reset_handler < layout.memory.flash_base + layout.memory.flash_size,
        "factory reset handler 0x{reset_handler:08x} is not Thumb code in physical flash"
    );
    if let Some(ota) = &images.ota {
        ensure!(!ota.is_empty(), "OTA package is empty");
    }
    if let Some(updated) = &images.updated_firmware {
        ensure!(!updated.is_empty(), "updated firmware image is empty");
        ensure!(
            updated.len() as u64 <= layout.memory.slot_a_size,
            "updated firmware exceeds slot A"
        );
    }
    validate_machine_config(layout)?;
    crate::execution::validate_platform_config(layout)?;
    Ok(())
}

fn validate_machine_config(layout: &BoardLayout) -> Result<()> {
    ensure!(layout.ota.chunk_size > 0, "OTA chunk_size must be positive");
    ensure!(
        layout.ota.inter_byte_us > 0,
        "OTA inter_byte_us must be positive"
    );
    let valid_name = |value: &str| {
        !value.is_empty()
            && value
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '.' | '@'))
    };
    if layout.ota.firmware_driven {
        let transport = layout
            .ota
            .transport
            .as_ref()
            .context("firmware-driven OTA requires ota.transport")?;
        ensure!(
            layout.artifacts.ota.is_some(),
            "firmware-driven OTA requires artifacts.ota"
        );
        ensure!(
            layout.artifacts.updated_firmware.is_some(),
            "firmware-driven OTA requires artifacts.updated_firmware for real image classification"
        );
        ensure!(
            !layout.ota.outcomes.is_empty(),
            "firmware-driven OTA requires at least one observable outcome symbol"
        );
        match transport.kind {
            crate::layout::OtaTransportKind::Uart => {
                let supported = layout.mcu().descriptor().uart_ota;
                ensure!(
                    supported.contains(&transport.peripheral.as_str()),
                    "{} has no modeled UART named {}",
                    layout.mcu(),
                    transport.peripheral
                );
            }
            crate::layout::OtaTransportKind::Can => {
                let supported = layout.mcu().descriptor().can_ota;
                ensure!(
                    supported.contains(&transport.peripheral.as_str()),
                    "{} has no modeled CAN controller named {}",
                    layout.mcu(),
                    transport.peripheral
                );
                ensure!(
                    transport.can_id.is_some(),
                    "CAN OTA requires transport.can_id"
                );
                ensure!(
                    transport.mtu.unwrap_or(64) <= 64,
                    "CAN-FD payload cannot exceed 64 bytes"
                );
            }
            crate::layout::OtaTransportKind::Usb => {
                let supported = layout.mcu().descriptor().usb_ota;
                ensure!(
                    supported.contains(&transport.peripheral.as_str()),
                    "{} has no modeled USB controller named {}",
                    layout.mcu(),
                    transport.peripheral
                );
                ensure!(
                    transport.endpoint.unwrap_or(1) <= 7,
                    "USB endpoint must be in the range 0..=7"
                );
            }
            crate::layout::OtaTransportKind::Sdmmc => {
                let supported = layout.mcu().descriptor().sdmmc_ota;
                ensure!(
                    supported.contains(&transport.peripheral.as_str()),
                    "{} has no modeled SDMMC controller named {}",
                    layout.mcu(),
                    transport.peripheral
                );
            }
        }
    }
    ensure!(
        !layout.ota.power_cuts.every_flash_operation || layout.ota.power_cuts.events.is_empty(),
        "power_cuts must select either every_flash_operation or specific events"
    );
    ensure!(
        layout.ota.firmware_driven
            || (!layout.ota.power_cuts.every_flash_operation
                && layout.ota.power_cuts.events.is_empty()),
        "power-cut execution requires firmware_driven OTA"
    );
    for event in &layout.ota.power_cuts.events {
        ensure!(
            matches!(
                event,
                crate::layout::FlashEventKind::EraseStart
                    | crate::layout::FlashEventKind::EraseComplete
                    | crate::layout::FlashEventKind::ProgramUnit
            ),
            "power-cut event {:?} is not exposed by the flash model",
            event
        );
    }
    for (index, outcome) in layout.ota.outcomes.iter().enumerate() {
        ensure!(
            valid_name(&outcome.name) && valid_name(&outcome.symbol),
            "invalid OTA outcome name or symbol"
        );
        ensure!(
            !layout.ota.outcomes[..index]
                .iter()
                .any(|prior| { prior.name == outcome.name || prior.symbol == outcome.symbol }),
            "duplicate OTA outcome name or symbol {}",
            outcome.name
        );
    }
    for clock in &layout.board.clocks {
        ensure!(
            valid_name(&clock.peripheral) && clock.frequency_hz > 0,
            "invalid board clock declaration"
        );
    }
    for (index, clock) in layout.board.clocks.iter().enumerate() {
        ensure!(
            !layout.board.clocks[..index]
                .iter()
                .any(|prior| prior.peripheral == clock.peripheral),
            "duplicate clock target {}",
            clock.peripheral
        );
    }
    for connection in &layout.board.connections {
        ensure!(
            valid_name(&connection.from) && valid_name(&connection.to),
            "invalid board connection"
        );
    }
    for pin in &layout.board.pins {
        ensure!(valid_name(&pin.gpio), "invalid GPIO controller name");
        ensure!(
            pin.gpio == "gpio",
            "{} has no GPIO bank named {}",
            layout.mcu(),
            pin.gpio
        );
        let pin_count = match layout.architecture {
            ArchitectureKind::Stm32g4 => 7 * 16,
            ArchitectureKind::Stm32h5 => 8 * 16,
            ArchitectureKind::Stm32u5 => 9 * 16,
        };
        ensure!(
            pin.pin < pin_count,
            "GPIO pin {} is outside the MCU package model",
            pin.pin
        );
    }
    for route in &layout.board.dma_routes {
        ensure!(
            valid_name(&route.request) && valid_name(&route.controller),
            "invalid DMA route"
        );
        let expected = if layout.architecture == ArchitectureKind::Stm32g4 {
            ["dma1", "dma2"].as_slice()
        } else {
            ["gpdma"].as_slice()
        };
        ensure!(
            expected.contains(&route.controller.as_str()),
            "{} has no modeled DMA controller named {}",
            layout.mcu(),
            route.controller
        );
        ensure!(route.channel < 16, "DMA channel must be below 16");
    }
    for region in &layout.board.security.secure_regions {
        let end = region.base.checked_add(region.size);
        let inside_flash = region.base >= layout.memory.flash_base
            && end.is_some_and(|end| end <= layout.memory.flash_base + layout.memory.flash_size);
        let inside_ram = layout.memory.ram_regions.iter().any(|ram| {
            region.base >= ram.base && end.is_some_and(|end| end <= ram.base + ram.size)
        });
        ensure!(
            region.size > 0 && (inside_flash || inside_ram),
            "secure region {} is outside physical flash/RAM",
            region.name
        );
        ensure!(
            region.base % 32 == 0 && region.size % 32 == 0,
            "secure region {} must use the Cortex-M33 32-byte attribution granularity",
            region.name
        );
    }
    if layout.board.security.trustzone {
        ensure!(
            layout.mcu().descriptor().trustzone_capable,
            "{} has no Arm TrustZone core",
            layout.mcu()
        );
    }
    ensure!(
        layout.board.security.secure_regions.is_empty() || layout.board.security.trustzone,
        "secure_regions requires board.security.trustzone"
    );
    let mut secure = layout.board.security.secure_regions.clone();
    secure.sort_by_key(|region| region.base);
    for pair in secure.windows(2) {
        ensure!(
            pair[0].base + pair[0].size <= pair[1].base,
            "secure regions {} and {} overlap",
            pair[0].name,
            pair[1].name
        );
    }
    ensure!(
        crate::execution::non_secure_regions(layout).len() <= 8,
        "secure-region layout needs more than the Cortex-M33's eight SAU regions"
    );
    Ok(())
}

pub fn run(layout: &BoardLayout, root: &Path, seed: u64) -> Result<SimulationReport> {
    validate(layout, root).context("artifact_validation")?;
    let images = load_images(layout, root).context("artifact_mapping")?;
    let execution = execution::run(layout, root).context("instruction_execution")?;
    let devices = peripherals::exercise_all(&layout.peripherals, 1_000, seed)
        .context("peripheral_execution")?;
    let traffic = traffic::run(&layout.traffic, layout.memory.sedsnet_pool, seed)
        .context("sedsnet_traffic")?;
    let mut fallback_updated = images.firmware.clone();
    let middle = fallback_updated.len() / 2;
    fallback_updated[middle] ^= 1;
    let updated = images
        .updated_firmware
        .as_deref()
        .unwrap_or(&fallback_updated);
    let transfer = images.ota.as_deref().unwrap_or(updated);
    let mut update = update::interruption_matrix(
        &images.firmware,
        updated,
        transfer,
        &layout.memory,
        layout.ota.chunk_size,
    )
    .context("ota_recovery")?;
    update.updated_image_from_artifact = images.updated_firmware.is_some();
    if layout.ota.firmware_driven
        && (layout.ota.power_cuts.every_flash_operation || !layout.ota.power_cuts.events.is_empty())
    {
        let operation_count = execution
            .flash_operations_observed
            .context("firmware OTA did not report flash operations")?;
        if layout.ota.power_cuts.every_flash_operation {
            ensure!(
                execution.ota_power_cuts.len() as u64 == operation_count,
                "firmware OTA tested {} of {operation_count} flash cut points",
                execution.ota_power_cuts.len()
            );
        }
        update.interruption_points_tested = execution.ota_power_cuts.len() + 1;
        update.old_image_boot_points = 0;
        update.new_image_boot_points = 0;
        update.recovery_required_points = 0;
        for cut in &execution.ota_power_cuts {
            let configured = layout
                .ota
                .outcomes
                .iter()
                .find(|outcome| outcome.name == cut.outcome)
                .context("power-cut report references an unknown outcome")?;
            match configured.image {
                crate::layout::BootImage::Old => update.old_image_boot_points += 1,
                crate::layout::BootImage::New => update.new_image_boot_points += 1,
                crate::layout::BootImage::Recovery => update.recovery_required_points += 1,
            }
        }
        if let Some(completed) = layout
            .ota
            .outcomes
            .iter()
            .find(|outcome| Some(&outcome.name) == execution.ota_outcome.as_ref())
        {
            match completed.image {
                crate::layout::BootImage::Old => update.old_image_boot_points += 1,
                crate::layout::BootImage::New => update.new_image_boot_points += 1,
                crate::layout::BootImage::Recovery => update.recovery_required_points += 1,
            }
        }
        update.cpu_reboots_executed = execution
            .ota_power_cuts
            .iter()
            .all(|cut| cut.power_cut_triggered && cut.reboot_executed);
        update.all_flash_operation_boundaries_tested =
            layout.ota.power_cuts.every_flash_operation && update.cpu_reboots_executed;
    }
    let updater_and_reboot_executed_at_each_cut = layout.ota.firmware_driven
        && (layout.ota.power_cuts.every_flash_operation
            || !layout.ota.power_cuts.events.is_empty())
        && update.cpu_reboots_executed;
    Ok(SimulationReport {
        board: layout.name.clone(),
        architecture: layout.architecture,
        firmware_bytes: images.firmware.len(),
        bootloader_bytes: images.bootloader.len(),
        factory_bytes: images.factory.len(),
        ota_bytes: images.ota.as_ref().map(Vec::len),
        physical_flash_bytes: layout.memory.flash_size,
        physical_ram_bytes: layout
            .memory
            .ram_regions
            .iter()
            .map(|region| region.size)
            .sum(),
        physical_ram_regions: layout.memory.ram_regions.clone(),
        devices,
        traffic,
        update,
        execution,
        fidelity: HardwareFidelityReport {
            exact_mcu: layout.mcu().to_string(),
            arm_instructions_executed: true,
            factory_vectors_used_for_reset: true,
            instruction_coupled_faults: layout.peripherals.iter().any(|peripheral| {
                peripheral.model.is_some()
                    && (peripheral.failure_every.is_some() || peripheral.disconnect_after.is_some())
            }),
            all_flash_operation_boundaries_modeled: true,
            updater_and_reboot_executed_at_each_cut,
            dynamic_rcc_clock_tree: false,
            strict_unknown_mmio: layout.board.strict_mmio,
            dma_data_path: !layout.board.dma_routes.is_empty(),
            trustzone_attribution: layout.board.security.trustzone
                && !layout.board.security.secure_regions.is_empty(),
            trustzone_core_enabled: layout.board.security.trustzone,
            usb_sdmmc_ota_adapters: true,
            gpio_pull_and_open_drain_behavior: true,
            limitations: {
                let mut values = vec![
                    "RCC ready/status behavior is deterministic; runtime PLL changes do not propagate through the full clock tree".into(),
                    "GPIO pull/open-drain behavior and configured ADC samples are modeled digitally; voltage ramps, impedance, metastability, EMI, and propagation timing require hardware tests".into(),
                    "H5/U5 cache maintenance is firmware-visible but does not simulate cache timing or stale-line coherency".into(),
                    "Cortex-M33 SAU attribution protects configured flash/RAM regions; STM32 GTZC peripheral/MPC register programming is not a complete model".into(),
                    "strict MMIO rejects unmapped addresses; implemented peripheral models may still return reset values for unsupported register offsets".into(),
                ];
                if !updater_and_reboot_executed_at_each_cut {
                    values.push("the real updater and bootloader were not rerun at every flash operation; enable firmware_driven OTA and every_flash_operation to perform that matrix".into());
                }
                values
            },
        },
    })
}

pub fn diagnose(layout: &BoardLayout, seed: u64, error: &anyhow::Error) -> CrashDiagnostic {
    let reason = format!("{error:#}");
    let phase = [
        "artifact_validation",
        "artifact_mapping",
        "instruction_execution",
        "peripheral_execution",
        "sedsnet_traffic",
        "ota_recovery",
    ]
    .into_iter()
    .find(|candidate| reason.contains(candidate))
    .unwrap_or("simulation");
    CrashDiagnostic::capture(layout, seed, phase, reason)
}

pub fn self_test(kind: ArchitectureKind) -> Result<()> {
    let arch = Architecture::for_kind(kind);
    let memory = crate::layout::MemoryLayout {
        flash_base: 0x0800_0000,
        flash_size: arch.default_flash_size,
        ram_regions: vec![crate::layout::MemoryRegion {
            name: "sram".into(),
            base: 0x2000_0000,
            size: arch.default_ram_size,
        }],
        bootloader_size: 0x4000,
        slot_a_base: 0x0800_4000,
        slot_a_size: arch.default_flash_size - 0x6000,
        slot_b_base: None,
        slot_b_size: None,
        delta_base: Some(0x0800_0000 + arch.default_flash_size - 0x2000),
        delta_size: Some(0x2000),
        erase_size: 0x800,
        write_alignment: 8,
        sedsnet_pool: 4096,
    };
    arch.validate(&memory)?;
    ensure!(
        traffic::run(&Default::default(), memory.sedsnet_pool, 1)?
            .pool
            .bytes_in_use
            == 0,
        "self-test leaked memory"
    );
    update::interruption_matrix(&[0xaa; 2048], &[0x55; 2048], &[0x33; 1024], &memory, 256)?;
    Ok(())
}

fn load_images(layout: &BoardLayout, root: &Path) -> Result<Images> {
    let read = |relative: &Path| {
        fs::read(root.join(relative)).with_context(|| {
            format!(
                "reading firmware artifact {}",
                root.join(relative).display()
            )
        })
    };
    Ok(Images {
        firmware: read(&layout.artifacts.firmware)?,
        bootloader: read(&layout.artifacts.bootloader)?,
        factory: read(&layout.artifacts.factory)?,
        ota: layout.artifacts.ota.as_deref().map(read).transpose()?,
        updated_firmware: layout
            .artifacts
            .updated_firmware
            .as_deref()
            .map(read)
            .transpose()?,
    })
}
