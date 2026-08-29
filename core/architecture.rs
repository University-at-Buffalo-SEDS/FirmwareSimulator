use crate::layout::MemoryLayout;
use anyhow::{ensure, Result};
use clap::ValueEnum;
use serde::{Deserialize, Serialize};
use std::fmt;

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
}
