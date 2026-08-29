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
}
struct Images {
    firmware: Vec<u8>,
    bootloader: Vec<u8>,
    factory: Vec<u8>,
    ota: Option<Vec<u8>>,
}

pub fn validate(layout: &BoardLayout, root: &Path) -> Result<()> {
    ensure!(!layout.name.is_empty(), "board name cannot be empty");
    Architecture::for_kind(layout.architecture).validate(&layout.memory)?;
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
    ensure!(
        images.firmware.len() as u64 <= layout.memory.slot_a_size,
        "firmware exceeds slot A"
    );
    ensure!(
        images.factory.len() as u64 <= layout.memory.flash_size,
        "factory image exceeds flash"
    );
    if let Some(ota) = &images.ota {
        ensure!(!ota.is_empty(), "OTA package is empty");
    }
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
    let mut updated = images.firmware.clone();
    let middle = updated.len() / 2;
    updated[middle] ^= 1;
    let transfer = images.ota.as_deref().unwrap_or(&updated);
    let update = update::interruption_matrix(
        &images.firmware,
        &updated,
        transfer,
        &layout.memory,
        layout.ota.chunk_size,
    )
    .context("ota_recovery")?;
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
    })
}
