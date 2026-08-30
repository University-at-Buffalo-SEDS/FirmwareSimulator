use crate::{
    core::{ArchitectureKind, McuKind},
    peripherals::PeripheralSpec,
    traffic::TrafficConfig,
};
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::{
    fs,
    path::{Path, PathBuf},
};

#[derive(Clone, Debug, Deserialize)]
pub struct BoardLayout {
    pub name: String,
    pub architecture: ArchitectureKind,
    pub mcu: McuKind,
    pub memory: MemoryLayout,
    pub artifacts: Artifacts,
    #[serde(default)]
    pub execution: ExecutionConfig,
    #[serde(default)]
    pub traffic: TrafficConfig,
    #[serde(default)]
    pub ota: OtaConfig,
    #[serde(default)]
    pub board: BoardConfig,
    #[serde(default)]
    pub peripherals: Vec<PeripheralSpec>,
}
impl BoardLayout {
    pub fn load(path: &Path) -> Result<Self> {
        let bytes = fs::read(path).with_context(|| format!("reading layout {}", path.display()))?;
        serde_json::from_slice(&bytes).with_context(|| format!("parsing layout {}", path.display()))
    }

    pub fn mcu(&self) -> McuKind {
        self.mcu
    }
}

#[derive(Clone, Debug, Deserialize)]
pub struct MemoryLayout {
    pub flash_base: u64,
    pub flash_size: u64,
    pub ram_regions: Vec<MemoryRegion>,
    pub bootloader_size: u64,
    pub slot_a_base: u64,
    pub slot_a_size: u64,
    #[serde(default)]
    pub slot_b_base: Option<u64>,
    #[serde(default)]
    pub slot_b_size: Option<u64>,
    #[serde(default)]
    pub delta_base: Option<u64>,
    #[serde(default)]
    pub delta_size: Option<u64>,
    pub erase_size: u64,
    pub write_alignment: u64,
    pub sedsnet_pool: usize,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct MemoryRegion {
    pub name: String,
    pub base: u64,
    pub size: u64,
}

#[derive(Clone, Debug, Deserialize)]
pub struct Artifacts {
    pub elf: PathBuf,
    pub bootloader_elf: PathBuf,
    pub firmware: PathBuf,
    pub bootloader: PathBuf,
    pub factory: PathBuf,
    #[serde(default)]
    pub updated_firmware: Option<PathBuf>,
    #[serde(default)]
    pub ota: Option<PathBuf>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct ExecutionConfig {
    #[serde(default = "default_virtual_time_ms")]
    pub virtual_time_ms: u64,
    #[serde(default)]
    pub trace: bool,
    #[serde(default = "default_boot_symbol")]
    pub boot_success_symbol: String,
    #[serde(default = "default_factory_boot_symbol")]
    pub factory_boot_success_symbol: String,
    #[serde(default = "default_sample_count")]
    pub sample_count: usize,
    #[serde(default)]
    pub memory_probe_warmup_samples: usize,
    #[serde(default)]
    pub hal_tick_address: Option<u64>,
    #[serde(default = "default_hal_tick_step")]
    pub hal_tick_step: u32,
    #[serde(default)]
    pub memory_probes: Vec<MemoryProbe>,
}
impl Default for ExecutionConfig {
    fn default() -> Self {
        Self {
            virtual_time_ms: default_virtual_time_ms(),
            trace: false,
            boot_success_symbol: default_boot_symbol(),
            factory_boot_success_symbol: default_factory_boot_symbol(),
            sample_count: default_sample_count(),
            memory_probe_warmup_samples: 0,
            hal_tick_address: None,
            hal_tick_step: default_hal_tick_step(),
            memory_probes: Vec::new(),
        }
    }
}
fn default_boot_symbol() -> String {
    "_tx_thread_schedule".into()
}
fn default_factory_boot_symbol() -> String {
    "main".into()
}
fn default_virtual_time_ms() -> u64 {
    250
}
fn default_sample_count() -> usize {
    1
}
fn default_hal_tick_step() -> u32 {
    1
}

#[derive(Clone, Debug, Deserialize)]
pub struct MemoryProbe {
    pub name: String,
    pub symbol: String,
    #[serde(default)]
    pub minimum: Option<u32>,
    #[serde(default)]
    pub maximum: Option<u32>,
    #[serde(default)]
    pub max_end_drop: Option<u32>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct OtaConfig {
    #[serde(default = "default_chunk")]
    pub chunk_size: usize,
    #[serde(default)]
    pub firmware_driven: bool,
    #[serde(default)]
    pub transport: Option<OtaTransport>,
    #[serde(default)]
    pub start_after_ms: u64,
    #[serde(default = "default_inter_byte_us")]
    pub inter_byte_us: u64,
    #[serde(default)]
    pub outcomes: Vec<BootOutcome>,
    #[serde(default)]
    pub power_cuts: PowerCutConfig,
}
impl Default for OtaConfig {
    fn default() -> Self {
        Self {
            chunk_size: default_chunk(),
            firmware_driven: false,
            transport: None,
            start_after_ms: 0,
            inter_byte_us: default_inter_byte_us(),
            outcomes: Vec::new(),
            power_cuts: PowerCutConfig::default(),
        }
    }
}
fn default_chunk() -> usize {
    512
}
fn default_inter_byte_us() -> u64 {
    100
}

#[derive(Clone, Debug, Deserialize)]
pub struct OtaTransport {
    pub kind: OtaTransportKind,
    pub peripheral: String,
    #[serde(default)]
    pub can_id: Option<u32>,
    #[serde(default)]
    pub mtu: Option<usize>,
    #[serde(default)]
    pub endpoint: Option<u8>,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum OtaTransportKind {
    Uart,
    Can,
    Usb,
    Sdmmc,
}

#[derive(Clone, Debug, Deserialize)]
pub struct BootOutcome {
    pub name: String,
    pub symbol: String,
    #[serde(default)]
    pub image: BootImage,
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum BootImage {
    Old,
    New,
    #[default]
    Recovery,
}

#[derive(Clone, Debug, Deserialize)]
pub struct PowerCutConfig {
    #[serde(default)]
    pub every_flash_operation: bool,
    #[serde(default)]
    pub events: Vec<FlashEventKind>,
    #[serde(default = "default_reboot_time_ms")]
    pub reboot_time_ms: u64,
}
impl Default for PowerCutConfig {
    fn default() -> Self {
        Self {
            every_flash_operation: false,
            events: Vec::new(),
            reboot_time_ms: default_reboot_time_ms(),
        }
    }
}
fn default_reboot_time_ms() -> u64 {
    500
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum FlashEventKind {
    EraseStart,
    EraseComplete,
    ProgramUnit,
    OptionCommit,
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct BoardConfig {
    #[serde(default)]
    pub strict_mmio: bool,
    #[serde(default)]
    pub clocks: Vec<ClockConfig>,
    #[serde(default)]
    pub connections: Vec<ConnectionConfig>,
    #[serde(default)]
    pub pins: Vec<PinConfig>,
    #[serde(default)]
    pub dma_routes: Vec<DmaRoute>,
    #[serde(default)]
    pub security: SecurityConfig,
}

#[derive(Clone, Debug, Deserialize)]
pub struct PinConfig {
    pub gpio: String,
    pub pin: u32,
    pub initial: PinState,
}

#[derive(Clone, Copy, Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PinState {
    Low,
    High,
    Floating,
}

#[derive(Clone, Debug, Deserialize)]
pub struct ClockConfig {
    pub peripheral: String,
    pub frequency_hz: u64,
}

#[derive(Clone, Debug, Deserialize)]
pub struct ConnectionConfig {
    pub from: String,
    pub to: String,
    #[serde(default)]
    pub active_low: bool,
}

#[derive(Clone, Debug, Deserialize)]
pub struct DmaRoute {
    pub request: String,
    pub controller: String,
    pub channel: u32,
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct SecurityConfig {
    #[serde(default)]
    pub trustzone: bool,
    #[serde(default)]
    pub secure_regions: Vec<MemoryRegion>,
}
