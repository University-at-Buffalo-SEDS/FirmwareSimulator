use crate::{
    core::{ArchitectureKind, McuDescriptor, McuKind},
    peripherals::PeripheralSpec,
    traffic::TrafficConfig,
};
use anyhow::{bail, Context, Result};
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
    /// Optional exact silicon descriptor. This lets a firmware repository add
    /// a part and its Renode platform without rebuilding the simulator image.
    #[serde(default)]
    pub mcu_descriptor: Option<McuDescriptor>,
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
        let mut value: serde_json::Value = serde_json::from_slice(&bytes)
            .with_context(|| format!("parsing layout {}", path.display()))?;
        normalize_legacy_layout(&mut value)
            .with_context(|| format!("normalizing layout {}", path.display()))?;
        let layout: Self = serde_json::from_value(value)
            .with_context(|| format!("parsing layout {}", path.display()))?;
        layout.resolve_mcu_descriptor()?;
        Ok(layout)
    }

    pub fn mcu(&self) -> &McuKind {
        &self.mcu
    }

    pub fn resolve_mcu_descriptor(&self) -> Result<&McuDescriptor> {
        let descriptor = match &self.mcu_descriptor {
            Some(descriptor) => {
                if descriptor.name != self.mcu.as_str() {
                    bail!(
                        "mcu_descriptor name {} does not match mcu {}",
                        descriptor.name,
                        self.mcu
                    );
                }
                descriptor
            }
            None => self.mcu.descriptor().with_context(|| {
                format!(
                    "MCU {} is not built in; add an exact mcu_descriptor to the board layout",
                    self.mcu
                )
            })?,
        };
        descriptor.validate_definition()?;
        Ok(descriptor)
    }
}

// v0.1 layouts selected the only MCU in each architecture implicitly and used a
// single contiguous `ram_size`. Keep those packages runnable while v0.2's
// explicit MCU/RAM-bank schema remains the canonical representation.
fn normalize_legacy_layout(value: &mut serde_json::Value) -> Result<()> {
    let root = value
        .as_object_mut()
        .context("board layout must be a JSON object")?;
    let architecture = root
        .get("architecture")
        .and_then(serde_json::Value::as_str)
        .context("missing string field `architecture`")?
        .to_owned();

    if !root.contains_key("mcu") {
        let mcu = match architecture.as_str() {
            "stm32g4" => "stm32g491",
            "stm32h5" => "stm32h523",
            "stm32u5" => "stm32u585",
            other => bail!("cannot infer an MCU for legacy architecture {other}"),
        };
        root.insert("mcu".into(), serde_json::Value::String(mcu.into()));
    }

    let memory = root
        .get_mut("memory")
        .and_then(serde_json::Value::as_object_mut)
        .context("missing object field `memory`")?;
    if !memory.contains_key("ram_regions") {
        let size = memory
            .get("ram_size")
            .and_then(serde_json::Value::as_u64)
            .context("missing `ram_regions` (legacy layouts may provide `ram_size`)")?;
        if size == 0 {
            bail!("legacy ram_size must be positive");
        }
        memory.insert(
            "ram_regions".into(),
            serde_json::json!([{"name": "sram", "base": 0x2000_0000_u64, "size": size}]),
        );
    }

    let artifacts = root
        .get_mut("artifacts")
        .and_then(serde_json::Value::as_object_mut)
        .context("missing object field `artifacts`")?;
    if !artifacts.contains_key("elf") {
        let firmware = artifacts
            .get("firmware")
            .and_then(serde_json::Value::as_str)
            .context("missing string field `artifacts.firmware`")?;
        let stem = firmware
            .strip_suffix(".launchcore.img")
            .or_else(|| firmware.strip_suffix(".img"))
            .context("cannot infer artifacts.elf from legacy firmware path")?;
        artifacts.insert(
            "elf".into(),
            serde_json::Value::String(format!("{stem}.elf")),
        );
    }
    if !artifacts.contains_key("bootloader_elf") {
        let bootloader = artifacts
            .get("bootloader")
            .and_then(serde_json::Value::as_str)
            .context("missing string field `artifacts.bootloader`")?;
        let stem = bootloader
            .strip_suffix(".bin")
            .context("cannot infer artifacts.bootloader_elf from legacy bootloader path")?;
        artifacts.insert(
            "bootloader_elf".into(),
            serde_json::Value::String(format!("{stem}.elf")),
        );
    }

    if let Some(peripherals) = root
        .get_mut("peripherals")
        .and_then(serde_json::Value::as_array_mut)
    {
        for peripheral in peripherals {
            let Some(spec) = peripheral.as_object_mut() else {
                continue;
            };
            if spec.get("type").and_then(serde_json::Value::as_str) != Some("adc")
                || spec.contains_key("model")
                || spec.contains_key("bus")
            {
                continue;
            }
            let Some(name) = spec
                .get("name")
                .and_then(serde_json::Value::as_str)
                .map(str::to_owned)
            else {
                continue;
            };
            let is_physical_adc = match architecture.as_str() {
                "stm32g4" => matches!(name.as_str(), "adc1" | "adc2" | "adc3"),
                "stm32h5" => name == "adc1",
                "stm32u5" => matches!(name.as_str(), "adc1" | "adc4"),
                _ => false,
            };
            if is_physical_adc {
                spec.insert(
                    "model".into(),
                    serde_json::Value::String("stm32_adc".into()),
                );
                spec.insert("bus".into(), serde_json::Value::String(name));
            }
        }
    }
    Ok(())
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
    #[serde(default)]
    pub persistent_data_base: Option<u64>,
    #[serde(default)]
    pub persistent_data_size: Option<u64>,
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
    pub require_stack_probe: bool,
    #[serde(default)]
    pub hal_tick_address: Option<u64>,
    #[serde(default = "default_hal_tick_step")]
    pub hal_tick_step: u32,
    #[serde(default)]
    pub memory_probes: Vec<MemoryProbe>,
    /// Whether CAN transmissions receive a physical-layer ACK.
    #[serde(default = "default_true")]
    pub can_acknowledged: bool,
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
            require_stack_probe: false,
            hal_tick_address: None,
            hal_tick_step: default_hal_tick_step(),
            memory_probes: Vec::new(),
            can_acknowledged: true,
        }
    }
}
impl ExecutionConfig {
    pub(crate) fn satisfies_stack_probe_requirement(&self) -> bool {
        !self.require_stack_probe
            || self
                .memory_probes
                .iter()
                .any(|probe| probe.name.contains("stack") && probe.minimum.unwrap_or(0) > 0)
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
fn default_true() -> bool {
    true
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

#[cfg(test)]
mod tests {
    use super::{BoardLayout, ExecutionConfig, MemoryProbe};
    use crate::core::McuKind;
    use std::fs;

    #[test]
    fn loads_legacy_single_ram_layout() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("board.json");
        fs::write(
            &path,
            r#"{
                "name":"legacy-h5", "architecture":"stm32h5",
                "memory":{"flash_base":134217728,"flash_size":524288,
                    "ram_size":278528,"bootloader_size":16384,
                    "slot_a_base":134234112,"slot_a_size":475136,
                    "erase_size":8192,"write_alignment":16,"sedsnet_pool":1024},
                "artifacts":{"firmware":"build/Firmware.launchcore.img",
                    "bootloader":"build/FirmwareBootloader.bin",
                    "factory":"build/Firmware.factory.bin"},
                "peripherals":[{"type":"adc","name":"adc1","bits":12,"channels":4}]
            }"#,
        )
        .unwrap();

        let layout = BoardLayout::load(&path).unwrap();
        assert_eq!(layout.mcu, McuKind::new("stm32h523"));
        assert_eq!(layout.memory.ram_regions[0].base, 0x2000_0000);
        assert_eq!(layout.memory.ram_regions[0].size, 278528);
        assert_eq!(layout.artifacts.elf.to_str(), Some("build/Firmware.elf"));
        assert_eq!(
            layout.artifacts.bootloader_elf.to_str(),
            Some("build/FirmwareBootloader.elf")
        );
        assert_eq!(layout.peripherals[0].model.as_deref(), Some("stm32_adc"));
        assert_eq!(layout.peripherals[0].bus.as_deref(), Some("adc1"));
    }

    #[test]
    fn required_stack_probe_needs_a_positive_remaining_margin() {
        let mut execution = ExecutionConfig {
            require_stack_probe: true,
            ..ExecutionConfig::default()
        };
        assert!(!execution.satisfies_stack_probe_requirement());
        execution.memory_probes.push(MemoryProbe {
            name: "telemetry_stack_remaining".into(),
            symbol: "g_telemetry_stack_remaining".into(),
            minimum: Some(8192),
            maximum: None,
            max_end_drop: None,
        });
        assert!(execution.satisfies_stack_probe_requirement());
        execution.memory_probes[0].minimum = Some(0);
        assert!(!execution.satisfies_stack_probe_requirement());
    }
}
