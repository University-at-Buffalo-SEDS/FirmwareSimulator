use crate::layout::MemoryLayout;
use anyhow::{ensure, Result};
use clap::ValueEnum;
use serde::{Deserialize, Serialize};
use std::{fmt, sync::OnceLock};

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(transparent)]
pub struct McuKind(String);

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct McuDescriptor {
    pub name: String,
    pub architecture: ArchitectureKind,
    pub core_model: String,
    pub platform_file: String,
    #[serde(default)]
    pub platform_from_firmware: bool,
    pub flash_profile: String,
    pub flash_base: u64,
    pub flash_size: u64,
    pub ram_base: u64,
    pub ram_size: u64,
    pub erase_size: u64,
    pub write_alignment: u64,
    pub trustzone_capable: bool,
    #[serde(default)]
    pub uart_ota: Vec<String>,
    #[serde(default)]
    pub can_ota: Vec<String>,
    #[serde(default)]
    pub usb_ota: Vec<String>,
    #[serde(default)]
    pub sdmmc_ota: Vec<String>,
    #[serde(default)]
    pub board_validated: bool,
}

impl McuKind {
    pub fn new(name: impl Into<String>) -> Self {
        Self(name.into().to_ascii_lowercase())
    }

    pub fn descriptor(&self) -> Option<&'static McuDescriptor> {
        mcu_catalog()
            .iter()
            .find(|descriptor| descriptor.name == self.0)
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

pub fn mcu_catalog() -> &'static [McuDescriptor] {
    static CATALOG: OnceLock<Vec<McuDescriptor>> = OnceLock::new();
    CATALOG.get_or_init(|| {
        serde_json::from_str(include_str!("../mcu/catalog.json"))
            .expect("the built-in MCU catalog must be valid")
    })
}

impl McuDescriptor {
    pub fn validate_definition(&self) -> Result<()> {
        ensure!(!self.name.is_empty(), "MCU descriptor name cannot be empty");
        ensure!(
            self.core_model.starts_with("cortex-m"),
            "MCU {} has an invalid Cortex-M core model",
            self.name
        );
        let platform = std::path::Path::new(&self.platform_file);
        ensure!(
            !self.platform_file.is_empty()
                && !platform.is_absolute()
                && !platform
                    .components()
                    .any(|part| matches!(part, std::path::Component::ParentDir)),
            "MCU {} platform_file must be a nonempty relative path",
            self.name
        );
        ensure!(
            matches!(
                self.flash_profile.as_str(),
                "stm32g4" | "stm32h5" | "stm32u5"
            ),
            "MCU {} uses unsupported flash_profile {}",
            self.name,
            self.flash_profile
        );
        ensure!(
            self.flash_size > 0
                && self.ram_size > 0
                && self.erase_size.is_power_of_two()
                && self.write_alignment.is_power_of_two(),
            "MCU {} has invalid memory or flash geometry",
            self.name
        );
        Ok(())
    }
}

impl fmt::Display for McuKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize, ValueEnum)]
#[serde(rename_all = "lowercase")]
pub enum ArchitectureKind {
    /// Runtime-supplied exact STM32 platform (any supported Cortex-M model).
    Stm32,
    Stm32g4,
    Stm32h5,
    Stm32u5,
}

impl fmt::Display for ArchitectureKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Stm32 => "stm32",
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
            ArchitectureKind::Stm32 => Self {
                kind,
                vector_alignment: 0x200,
                default_flash_size: 512 * 1024,
                default_ram_size: 128 * 1024,
            },
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
        ensure!(
            memory.persistent_data_base.is_some() == memory.persistent_data_size.is_some(),
            "persistent_data_base and persistent_data_size must be provided together"
        );
        if let (Some(base), Some(size)) = (memory.persistent_data_base, memory.persistent_data_size)
        {
            let end = base
                .checked_add(size)
                .ok_or_else(|| anyhow::anyhow!("persistent-data range overflow"))?;
            ensure!(
                size >= 2 * memory.erase_size
                    && base % memory.erase_size == 0
                    && size % memory.erase_size == 0
                    && base >= slot_end
                    && end <= flash_end,
                "invalid persistent-data area"
            );
            let overlaps = |other_base: u64, other_size: u64| {
                base < other_base.saturating_add(other_size) && other_base < end
            };
            ensure!(
                !overlaps(memory.flash_base, memory.bootloader_size)
                    && !overlaps(memory.slot_a_base, memory.slot_a_size)
                    && !memory
                        .delta_base
                        .zip(memory.delta_size)
                        .is_some_and(|(other_base, other_size)| overlaps(other_base, other_size))
                    && !memory
                        .slot_b_base
                        .zip(memory.slot_b_size)
                        .is_some_and(|(other_base, other_size)| overlaps(other_base, other_size)),
                "persistent-data area overlaps a firmware/update partition"
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

    pub fn validate_mcu(&self, mcu: &McuDescriptor, memory: &MemoryLayout) -> Result<()> {
        mcu.validate_definition()?;
        ensure!(
            mcu.architecture == self.kind,
            "MCU {} does not belong to architecture {}",
            mcu.name,
            self.kind
        );
        self.validate(memory)?;
        ensure!(
            memory.flash_size <= mcu.flash_size,
            "configured flash exceeds {} physical flash",
            mcu.name
        );
        ensure!(
            memory.flash_base == mcu.flash_base,
            "{} physical flash must start at 0x{:08x}",
            mcu.name,
            mcu.flash_base
        );
        let physical_ram_base = mcu.ram_base;
        let physical_ram_end = physical_ram_base + mcu.ram_size;
        for region in &memory.ram_regions {
            let end = region.base + region.size;
            ensure!(
                region.base >= physical_ram_base && end <= physical_ram_end,
                "RAM region {} is outside {} physical SRAM",
                region.name,
                mcu.name
            );
        }
        let total_ram: u64 = memory.ram_regions.iter().map(|region| region.size).sum();
        ensure!(
            total_ram <= mcu.ram_size,
            "configured RAM exceeds {} physical RAM",
            mcu.name
        );
        let (erase_size, write_alignment) = (mcu.erase_size, mcu.write_alignment);
        ensure!(
            memory.erase_size == erase_size && memory.write_alignment == write_alignment,
            "{} flash geometry requires erase_size {erase_size} and write_alignment {write_alignment}",
            mcu.name
        );
        Ok(())
    }
}
