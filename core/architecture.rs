use crate::layout::MemoryLayout;
use anyhow::{ensure, Result};
use clap::ValueEnum;
use serde::{Deserialize, Serialize};
use std::fmt;

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize, ValueEnum)]
#[serde(rename_all = "lowercase")]
pub enum McuKind {
    Stm32g491,
    Stm32h523,
    Stm32u585,
}

#[derive(Clone, Copy, Debug, Serialize)]
pub struct McuDescriptor {
    pub name: &'static str,
    pub architecture: ArchitectureKind,
    pub core_model: &'static str,
    pub platform_file: &'static str,
    pub flash_base: u64,
    pub flash_size: u64,
    pub ram_base: u64,
    pub ram_size: u64,
    pub erase_size: u64,
    pub write_alignment: u64,
    pub trustzone_capable: bool,
    pub uart_ota: &'static [&'static str],
    pub can_ota: &'static [&'static str],
    pub usb_ota: &'static [&'static str],
    pub sdmmc_ota: &'static [&'static str],
}

impl McuKind {
    pub const ALL: [Self; 3] = [Self::Stm32g491, Self::Stm32h523, Self::Stm32u585];

    pub fn descriptor(self) -> McuDescriptor {
        match self {
            Self::Stm32g491 => McuDescriptor {
                name: "stm32g491",
                architecture: ArchitectureKind::Stm32g4,
                core_model: "cortex-m4f",
                platform_file: "stm32g491.repl",
                flash_base: 0x0800_0000,
                flash_size: 0x80000,
                ram_base: 0x2000_0000,
                ram_size: 0x1c000,
                erase_size: 0x800,
                write_alignment: 8,
                trustzone_capable: false,
                uart_ota: &["uart4", "usart1"],
                can_ota: &["fdcan1", "fdcan2"],
                usb_ota: &["usb"],
                sdmmc_ota: &[],
            },
            Self::Stm32h523 => McuDescriptor {
                name: "stm32h523",
                architecture: ArchitectureKind::Stm32h5,
                core_model: "cortex-m33",
                platform_file: "stm32h523.repl",
                flash_base: 0x0800_0000,
                flash_size: 0x80000,
                ram_base: 0x2000_0000,
                ram_size: 0x44000,
                erase_size: 0x2000,
                write_alignment: 16,
                trustzone_capable: true,
                uart_ota: &[],
                can_ota: &["fdcan1"],
                usb_ota: &["usb"],
                sdmmc_ota: &["sdmmc"],
            },
            Self::Stm32u585 => McuDescriptor {
                name: "stm32u585",
                architecture: ArchitectureKind::Stm32u5,
                core_model: "cortex-m33",
                platform_file: "stm32u585.repl",
                flash_base: 0x0800_0000,
                flash_size: 0x200000,
                ram_base: 0x2000_0000,
                ram_size: 0xc0000,
                erase_size: 0x2000,
                write_alignment: 16,
                trustzone_capable: true,
                uart_ota: &["usart1"],
                can_ota: &["fdcan1"],
                usb_ota: &["usb"],
                sdmmc_ota: &["sdmmc1"],
            },
        }
    }

    pub fn architecture(self) -> ArchitectureKind {
        self.descriptor().architecture
    }

    pub fn for_architecture(kind: ArchitectureKind) -> Self {
        match kind {
            ArchitectureKind::Stm32g4 => Self::Stm32g491,
            ArchitectureKind::Stm32h5 => Self::Stm32h523,
            ArchitectureKind::Stm32u5 => Self::Stm32u585,
        }
    }

    fn flash_program_geometry(self) -> (u64, u64) {
        let descriptor = self.descriptor();
        (descriptor.erase_size, descriptor.write_alignment)
    }
}

impl fmt::Display for McuKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.descriptor().name)
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize, ValueEnum)]
#[serde(rename_all = "lowercase")]
pub enum ArchitectureKind {
    Stm32g4,
    Stm32h5,
    Stm32u5,
}

impl fmt::Display for ArchitectureKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Stm32g4 => "stm32g4",
            Self::Stm32h5 => "stm32h5",
            Self::Stm32u5 => "stm32u5",
        })
    }
}

#[derive(Clone, Copy, Debug)]
pub struct Architecture {
    pub kind: ArchitectureKind,
    pub vector_alignment: u64,
    pub default_flash_size: u64,
    pub default_ram_size: u64,
}

impl Architecture {
    pub fn for_kind(kind: ArchitectureKind) -> Self {
        match kind {
            ArchitectureKind::Stm32g4 => Self {
                kind,
                vector_alignment: super::stm32g4::VECTOR_ALIGNMENT,
                default_flash_size: super::stm32g4::DEFAULT_FLASH_SIZE,
                default_ram_size: super::stm32g4::DEFAULT_RAM_SIZE,
            },
            ArchitectureKind::Stm32h5 => Self {
                kind,
                vector_alignment: super::stm32h5::VECTOR_ALIGNMENT,
                default_flash_size: super::stm32h5::DEFAULT_FLASH_SIZE,
                default_ram_size: super::stm32h5::DEFAULT_RAM_SIZE,
            },
            ArchitectureKind::Stm32u5 => Self {
                kind,
                vector_alignment: super::stm32u5::VECTOR_ALIGNMENT,
                default_flash_size: super::stm32u5::DEFAULT_FLASH_SIZE,
                default_ram_size: super::stm32u5::DEFAULT_RAM_SIZE,
            },
        }
    }

    pub fn validate(&self, memory: &MemoryLayout) -> Result<()> {
        ensure!(
            memory.flash_size > 0 && memory.bootloader_size > 0 && memory.slot_a_size > 0,
            "flash partitions must be positive"
        );
        ensure!(
            memory.erase_size.is_power_of_two(),
            "erase_size must be a power of two"
        );
        ensure!(
            memory.write_alignment.is_power_of_two(),
            "write_alignment must be a power of two"
        );
        ensure!(
            memory.slot_a_base & (self.vector_alignment - 1) == 0,
            "slot A is not vector aligned"
        );
        ensure!(
            memory.slot_a_base >= memory.flash_base + memory.bootloader_size,
            "slot A overlaps bootloader"
        );
        let flash_end = memory
            .flash_base
            .checked_add(memory.flash_size)
            .ok_or_else(|| anyhow::anyhow!("flash range overflow"))?;
        let slot_end = memory
            .slot_a_base
            .checked_add(memory.slot_a_size)
            .ok_or_else(|| anyhow::anyhow!("slot range overflow"))?;
        ensure!(slot_end <= flash_end, "slot A exceeds flash");
        if let (Some(base), Some(size)) = (memory.delta_base, memory.delta_size) {
            ensure!(
                size > 0 && base >= slot_end && base + size <= flash_end,
                "invalid delta area"
            );
        }
        if let (Some(base), Some(size)) = (memory.slot_b_base, memory.slot_b_size) {
            ensure!(
                size > 0 && base >= slot_end && base + size <= flash_end,
                "invalid slot B"
            );
        }
        ensure!(memory.sedsnet_pool > 0, "sedsnet_pool must be positive");
        ensure!(
            !memory.ram_regions.is_empty(),
            "ram_regions must describe the physical MCU RAM banks"
        );
        let mut ranges = Vec::new();
        let mut total_ram = 0_u64;
        for region in &memory.ram_regions {
            ensure!(
                !region.name.is_empty()
                    && region
                        .name
                        .chars()
                        .all(|c| c.is_ascii_alphanumeric() || c == '_'),
                "invalid RAM region name {}",
                region.name
            );
            ensure!(
                region.size > 0,
                "RAM region {} must be positive",
                region.name
            );
            let end = region
                .base
                .checked_add(region.size)
                .ok_or_else(|| anyhow::anyhow!("RAM region {} overflows", region.name))?;
            ensure!(
                !ranges
                    .iter()
                    .any(|(start, prior_end)| region.base < *prior_end && end > *start),
                "RAM region {} overlaps another physical region",
                region.name
            );
            ranges.push((region.base, end));
            total_ram = total_ram
                .checked_add(region.size)
                .ok_or_else(|| anyhow::anyhow!("total RAM size overflows"))?;
        }
        ensure!(
            memory.sedsnet_pool as u64 <= total_ram,
            "sedsnet_pool exceeds total physical RAM"
        );
        Ok(())
    }

    pub fn validate_mcu(&self, mcu: McuKind, memory: &MemoryLayout) -> Result<()> {
        ensure!(
            mcu.architecture() == self.kind,
            "MCU {mcu} does not belong to architecture {}",
            self.kind
        );
        self.validate(memory)?;
        let descriptor = mcu.descriptor();
        ensure!(
            memory.flash_size <= descriptor.flash_size,
            "configured flash exceeds {mcu} physical flash"
        );
        ensure!(
            memory.flash_base == descriptor.flash_base,
            "{mcu} physical flash must start at 0x08000000"
        );
        let physical_ram_base = descriptor.ram_base;
        let physical_ram_end = physical_ram_base + descriptor.ram_size;
        for region in &memory.ram_regions {
            let end = region.base + region.size;
            ensure!(
                region.base >= physical_ram_base && end <= physical_ram_end,
                "RAM region {} is outside {mcu} physical SRAM",
                region.name
            );
        }
        let total_ram: u64 = memory.ram_regions.iter().map(|region| region.size).sum();
        ensure!(
            total_ram <= descriptor.ram_size,
            "configured RAM exceeds {mcu} physical RAM"
        );
        let (erase_size, write_alignment) = mcu.flash_program_geometry();
        ensure!(
            memory.erase_size == erase_size && memory.write_alignment == write_alignment,
            "{mcu} flash geometry requires erase_size {erase_size} and write_alignment {write_alignment}"
        );
        Ok(())
    }
}
