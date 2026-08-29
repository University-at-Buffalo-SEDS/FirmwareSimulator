use crate::{core::ArchitectureKind, layout::BoardLayout};
use anyhow::{bail, ensure, Context, Result};
use serde::Serialize;
use std::{
    env, fs,
    path::{Path, PathBuf},
    process::Command,
};

#[derive(Debug, Serialize)]
pub struct ExecutionReport {
    pub backend: &'static str,
    pub elf: String,
    pub virtual_time_ms: u64,
    pub instruction_execution_observed: bool,
    pub firmware_boot_reached: bool,
    pub factory_boot_reached: bool,
    pub register_dump: Vec<String>,
    pub memory_profile: Vec<MemoryProbeReport>,
    pub trace: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct MemoryProbeReport {
    pub name: String,
    pub symbol: String,
    pub samples: Vec<u32>,
    pub minimum_observed: u32,
    pub maximum_observed: u32,
    pub end_drop: i64,
}

pub fn run(layout: &BoardLayout, root: &Path) -> Result<ExecutionReport> {
    warn_if_native_executor();
    let elf = root.join(&layout.artifacts.elf);
    let bootloader_elf = root.join(&layout.artifacts.bootloader_elf);
    let factory = root.join(&layout.artifacts.factory);
    ensure!(elf.is_file(), "firmware ELF is missing: {}", elf.display());
    ensure!(
        bootloader_elf.is_file(),
        "bootloader ELF is missing: {}",
        bootloader_elf.display()
    );
    ensure!(
        factory.is_file(),
        "factory image is missing: {}",
        factory.display()
    );
    ensure!(
        layout
            .execution
            .boot_success_symbol
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_'),
        "invalid execution success symbol"
    );
    ensure!(
        layout
            .execution
            .factory_boot_success_symbol
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_'),
        "invalid factory execution success symbol"
    );
    ensure!(
        layout.execution.sample_count > 0,
        "execution sample_count must be positive"
    );
    ensure!(
        layout.execution.sample_count as u64 <= layout.execution.virtual_time_ms,
        "execution sample_count cannot exceed virtual_time_ms"
    );
    ensure!(
        layout.execution.memory_probe_warmup_samples < layout.execution.sample_count,
        "execution memory_probe_warmup_samples must be less than sample_count"
    );
    ensure!(
        layout.execution.hal_tick_step > 0,
        "execution hal_tick_step must be positive"
    );
    if let Some(address) = layout.execution.hal_tick_address {
        ensure!(
            layout.memory.ram_regions.iter().any(|region| {
                address >= region.base
                    && address
                        .checked_add(4)
                        .is_some_and(|end| end <= region.base + region.size)
            }),
            "execution hal_tick_address 0x{address:08x} is outside physical RAM"
        );
    }
    for probe in &layout.execution.memory_probes {
        ensure!(
            !probe.name.is_empty()
                && probe
                    .name
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-'),
            "invalid memory probe name {}",
            probe.name
        );
        ensure!(
            !probe.symbol.is_empty()
                && probe
                    .symbol
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || c == '_'),
            "invalid memory probe symbol {}",
            probe.symbol
        );
    }
    let renode = find_renode()?;
    let platform = platform_path(layout.architecture);
    let scratch = tempfile::tempdir().context("creating Renode scratch directory")?;
    let peripheral_overlay = scratch.path().join("peripherals.repl");
    fs::write(&peripheral_overlay, render_peripheral_overlay(layout)?)
        .context("writing Renode peripheral overlay")?;
    let script = scratch.path().join("run.resc");
    let trace = scratch.path().join("execution.trace");
    fs::write(
        &script,
        render_script(
            layout,
            &platform,
            &peripheral_overlay,
            &elf,
            &bootloader_elf,
            &factory,
            &trace,
        ),
    )?;
    let output = Command::new(&renode)
        .args(["--disable-xwt", "--console", "--execute"])
        .arg(format!("include @{}; quit", script.display()))
        .output()
        .with_context(|| format!("starting Renode at {}", renode.display()))?;
    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    if !output.status.success() {
        bail!(
            "Renode failed ({}):\n{}",
            output.status,
            tail(&combined, 80)
        );
    }
    let lower = combined.to_ascii_lowercase();
    ensure!(
        !lower.contains("fatal error")
            && !lower.contains("cpu abort")
            && !lower.contains("[error]")
            && !lower.contains("there was an error executing command")
            && !lower.contains("parameters did not match"),
        "firmware execution faulted:\n{}",
        tail(&combined, 80)
    );
    let registers = marker_lines(&combined);
    let memory_profile = parse_memory_profile(layout, &combined)?;
    ensure!(
        observed_marker(&combined, "SEDS_FIRMWARE_BOOT_REACHED"),
        "firmware never reached {}:\n{}",
        layout.execution.boot_success_symbol,
        tail(&combined, 80)
    );
    ensure!(
        observed_marker(&combined, "SEDS_FACTORY_BOOT_REACHED"),
        "factory boot flow never reached {}:\n{}",
        layout.execution.factory_boot_success_symbol,
        tail(&combined, 80)
    );
    ensure!(
        registers.iter().any(|line| line.contains("PC")),
        "Renode did not return registers:\n{}",
        tail(&combined, 80)
    );
    let trace_path = layout.execution.trace.then(|| trace.display().to_string());
    if layout.execution.trace {
        ensure!(
            trace.is_file(),
            "Renode did not produce the requested instruction trace"
        );
    }
    Ok(ExecutionReport {
        backend: "renode",
        elf: elf.display().to_string(),
        virtual_time_ms: layout.execution.virtual_time_ms,
        instruction_execution_observed: true,
        firmware_boot_reached: true,
        factory_boot_reached: true,
        register_dump: registers,
        memory_profile,
        trace: trace_path,
    })
}

fn warn_if_native_executor() {
    if !Path::new("/.dockerenv").exists()
        && env::var("FIRMWARE_SIM_CONTAINER").as_deref() != Ok("1")
    {
        eprintln!(
            "warning: native firmware simulation is unsupported and runs at your own risk; \
             use the published Docker image or build.py test --full for validated results"
        );
    }
}

fn find_renode() -> Result<PathBuf> {
    if let Some(path) = env::var_os("RENODE") {
        return Ok(path.into());
    }
    for path in ["/usr/bin/renode", "/opt/renode/renode"] {
        let candidate = PathBuf::from(path);
        if candidate.is_file() {
            return Ok(candidate);
        }
    }
    bail!("Renode is required for instruction-level execution; set RENODE or use the simulator Docker image")
}
fn platform_path(kind: ArchitectureKind) -> PathBuf {
    let file = match kind {
        ArchitectureKind::Stm32g4 => "stm32g491.repl",
        ArchitectureKind::Stm32h5 => "stm32h523.repl",
        ArchitectureKind::Stm32u5 => "stm32u585.repl",
    };
    simulator_root().join("renode/platforms").join(file)
}

fn simulator_root() -> PathBuf {
    env::var_os("FIRMWARE_SIM_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")))
}

pub(crate) fn render_peripheral_overlay(layout: &BoardLayout) -> Result<String> {
    let source_root = simulator_root().join("renode/peripherals");
    let mut overlay = format!(
        "physicalFlash: Memory.MappedMemory @ sysbus 0x{:08x}\n    size: 0x{:x}\n",
        layout.memory.flash_base, layout.memory.flash_size
    );
    for (index, region) in layout.memory.ram_regions.iter().enumerate() {
        overlay.push_str(&format!(
            "physicalRam{index}: Memory.MappedMemory @ sysbus 0x{:08x}\n    size: 0x{:x}\n",
            region.base, region.size
        ));
    }
    let mut flight_sensor_bus_added = false;
    let mut g4_adc12_common_added = false;
    let mut g4_adc345_common_added = false;
    for (index, peripheral) in layout.peripherals.iter().enumerate() {
        let (model, bus) = match (peripheral.model.as_deref(), peripheral.bus.as_deref()) {
            (None, None) => continue,
            (Some(model), Some(bus)) => (model, bus),
            _ => bail!(
                "peripheral {} must specify model and bus together",
                peripheral.name
            ),
        };
        match (layout.architecture, model, bus) {
            (ArchitectureKind::Stm32g4, "neo_m9n", "spi1") => {
                overlay.push_str(&format!(
                    "layoutDevice{index}: Sensors.SedsNeoM9N @ spi1\n    preinit:\n        include @{}\n",
                    source_root.join("SedsSpiSensors.cs").display()
                ));
            }
            (ArchitectureKind::Stm32g4, "ltc2990", address)
                if matches!(address, "i2c2@0x4c" | "i2c2@0x4d") =>
            {
                let address = address.rsplit_once('@').unwrap().1;
                overlay.push_str(&format!(
                    "layoutDevice{index}: Sensors.SedsLtc2990 @ i2c2 {address}\n    preinit:\n        include @{}\n",
                    source_root.join("SedsLtc2990.cs").display()
                ));
            }
            (ArchitectureKind::Stm32h5, "bmi088" | "bmp390", "spi1") => {
                if !flight_sensor_bus_added {
                    overlay.push_str(&format!(
                        "layoutFlightSensors: Sensors.SedsFlightSensorBus @ spi1\n    preinit:\n        include @{}\n",
                        source_root.join("SedsSpiSensors.cs").display()
                    ));
                    flight_sensor_bus_added = true;
                }
            }
            (ArchitectureKind::Stm32g4, "stm32_adc", "adc1" | "adc2" | "adc3") => {
                let address = match bus {
                    "adc1" => 0x5000_0000_u64,
                    "adc2" => 0x5000_0100_u64,
                    "adc3" => 0x5000_0400_u64,
                    _ => unreachable!(),
                };
                overlay.push_str(&format!(
                    "layoutAdc{index}: Sensors.SedsStm32Adc @ sysbus 0x{address:08x}\n    preinit:\n        include @{}\n",
                    source_root.join("SedsStm32Adc.cs").display()
                ));
                if matches!(bus, "adc1" | "adc2") && !g4_adc12_common_added {
                    overlay.push_str(&format!(
                        "layoutAdc12Common: Sensors.SedsStm32Adc @ sysbus 0x50000300\n    preinit:\n        include @{}\n",
                        source_root.join("SedsStm32Adc.cs").display()
                    ));
                    g4_adc12_common_added = true;
                }
                if bus == "adc3" && !g4_adc345_common_added {
                    overlay.push_str(&format!(
                        "layoutAdc345Common: Sensors.SedsStm32Adc @ sysbus 0x50000700\n    preinit:\n        include @{}\n",
                        source_root.join("SedsStm32Adc.cs").display()
                    ));
                    g4_adc345_common_added = true;
                }
            }
            (ArchitectureKind::Stm32h5, "stm32_adc", "adc1") => {
                overlay.push_str(&format!(
                    "layoutAdc{index}: Sensors.SedsStm32Adc @ sysbus 0x42228000\n    preinit:\n        include @{}\n",
                    source_root.join("SedsStm32Adc.cs").display()
                ));
            }
            (ArchitectureKind::Stm32u5, "stm32_adc", "adc1" | "adc4") => {
                let address = if bus == "adc1" {
                    0x4202_8000_u64
                } else {
                    0x4602_1000_u64
                };
                overlay.push_str(&format!(
                    "layoutAdc{index}: Sensors.SedsStm32Adc @ sysbus 0x{address:08x}\n    preinit:\n        include @{}\n",
                    source_root.join("SedsStm32Adc.cs").display()
                ));
            }
            (ArchitectureKind::Stm32u5, "sd_card", "sdmmc1") => {
                overlay.push_str(&format!(
                    "layoutSdmmc{index}: Storage.SedsStm32Sdmmc @ sysbus 0x420c8000\n    preinit:\n        include @{}\n",
                    source_root.join("SedsStm32Sdmmc.cs").display()
                ));
            }
            _ => bail!(
                "peripheral {} selects unsupported model/bus {model}/{bus} on {:?}",
                peripheral.name,
                layout.architecture
            ),
        }
    }
    Ok(overlay)
}
fn render_script(
    layout: &BoardLayout,
    platform: &Path,
    peripheral_overlay: &Path,
    elf: &Path,
    bootloader_elf: &Path,
    factory: &Path,
    trace: &Path,
) -> String {
    let sample_count = layout.execution.sample_count;
    let base_ms = layout.execution.virtual_time_ms / sample_count as u64;
    let remainder_ms = layout.execution.virtual_time_ms % sample_count as u64;
    let mut profile_script = String::new();
    for sample in 0..sample_count {
        let duration_ms = base_ms + u64::from((sample as u64) < remainder_ms);
        profile_script.push_str(&format!(
            "emulation RunFor \"{}s\"\n",
            duration_ms as f64 / 1000.0
        ));
        for probe in &layout.execution.memory_probes {
            profile_script.push_str(&format!(
                "echo \"SEDS_PROBE {} {}\"\nsysbus ReadDoubleWord `sysbus GetSymbolAddress \"{}\"`\n",
                probe.name, sample, probe.symbol
            ));
        }
    }
    // H5 bootloader + application startup includes clock, SD-card absence, and
    // TIM6 timebase initialization. Give the factory flow enough virtual time
    // to reach the same scheduler hook without lengthening the allocator soak.
    let minimum_factory_ms = match layout.architecture {
        ArchitectureKind::Stm32h5 => 1_000,
        _ => 50,
    };
    let factory_ms = minimum_factory_ms;
    let factory_seconds = factory_ms as f64 / 1000.0;
    let tracing = if layout.execution.trace {
        format!(
            "cpu CreateExecutionTracing \"{}\" BinaryPC\n",
            trace.display()
        )
    } else {
        String::new()
    };
    let tick_hook = layout
        .execution
        .hal_tick_address
        .map(|address| {
            let step = layout.execution.hal_tick_step;
            format!(
                "set sedsTickHook\n\"\"\"\nbus = machine[\"sysbus\"]\nbus.WriteDoubleWord(0x{address:08x}, (bus.ReadDoubleWord(0x{address:08x}) + {step}) & 0xffffffff)\n\"\"\"\ncpu AddHook `sysbus GetSymbolAddress \"HAL_GetTick\" 0` $sedsTickHook\n"
            )
        })
        .unwrap_or_default();
    let name = layout.name.replace('"', "_");
    let symbol = &layout.execution.boot_success_symbol;
    let factory_symbol = &layout.execution.factory_boot_success_symbol;
    format!("mach create \"{}_firmware\"\nmachine LoadPlatformDescription @{}\nmachine LoadPlatformDescription @{}\nsysbus LoadELF @{}\ncpu AddHook `sysbus GetSymbolAddress \"{}\"` 'self.InfoLog(\"SEDS_FIRMWARE_BOOT_REACHED\")'\n{}{}{}echo \"SEDS_REG FIRMWARE_PC\"\ncpu PC\necho \"SEDS_REG FIRMWARE_SP\"\ncpu GetRegister 13\necho \"SEDS_REG FIRMWARE_LR\"\ncpu GetRegister 14\ncpu IsHalted true\nmach create \"{}_factory\"\nmachine LoadPlatformDescription @{}\nmachine LoadPlatformDescription @{}\nsysbus LoadELF @{}\nsysbus LoadELF @{}\nsysbus LoadBinary @{} {}\ncpu AddHook `sysbus GetSymbolAddress \"{}\" 0` 'self.InfoLog(\"SEDS_FACTORY_BOOT_REACHED\")'\n{}emulation RunFor \"{}s\"\necho \"SEDS_REG FACTORY_PC\"\ncpu PC\necho \"SEDS_REG FACTORY_SP\"\ncpu GetRegister 13\necho \"SEDS_REG FACTORY_LR\"\ncpu GetRegister 14\necho \"SEDS_EXECUTION_COMPLETE\"\n", name, platform.display(), peripheral_overlay.display(), elf.display(), symbol, tick_hook, tracing, profile_script, name, platform.display(), peripheral_overlay.display(), elf.display(), bootloader_elf.display(), factory.display(), layout.memory.flash_base, factory_symbol, tick_hook, factory_seconds)
}

fn parse_memory_profile(layout: &BoardLayout, output: &str) -> Result<Vec<MemoryProbeReport>> {
    let lines: Vec<_> = output.lines().collect();
    let mut reports = Vec::new();
    for probe in &layout.execution.memory_probes {
        let prefix = format!("SEDS_PROBE {} ", probe.name);
        let mut indexed = Vec::new();
        for (index, line) in lines.iter().enumerate() {
            let Some(marker_start) = line.find(&prefix) else {
                continue;
            };
            let marker = &line[marker_start..];
            let sample: usize = marker[prefix.len()..]
                .trim()
                .parse()
                .with_context(|| format!("parsing probe marker {marker}"))?;
            let value = lines[index + 1..]
                .iter()
                .take_while(|line| !line.contains("SEDS_PROBE "))
                .map(|line| line.trim())
                .find_map(|line| {
                    let hex = line.strip_prefix("0x")?;
                    (!hex.is_empty() && hex.chars().all(|c| c.is_ascii_hexdigit())).then_some(hex)
                })
                .context("memory probe value is missing")?;
            indexed.push((sample, u32::from_str_radix(value, 16)?));
        }
        indexed.sort_unstable_by_key(|(sample, _)| *sample);
        ensure!(
            indexed.len() == layout.execution.sample_count,
            "probe {} returned {} of {} samples",
            probe.name,
            indexed.len(),
            layout.execution.sample_count
        );
        let samples: Vec<u32> = indexed.into_iter().map(|(_, value)| value).collect();
        let minimum_observed = *samples.iter().min().context("empty probe samples")?;
        let maximum_observed = *samples.iter().max().context("empty probe samples")?;
        let end_drop = i64::from(samples[layout.execution.memory_probe_warmup_samples])
            - i64::from(*samples.last().unwrap());
        if let Some(minimum) = probe.minimum {
            ensure!(
                minimum_observed >= minimum,
                "probe {} fell below {}: {:?}",
                probe.name,
                minimum,
                samples
            );
        }
        if let Some(maximum) = probe.maximum {
            ensure!(
                maximum_observed <= maximum,
                "probe {} exceeded {}: {:?}",
                probe.name,
                maximum,
                samples
            );
        }
        if let Some(max_end_drop) = probe.max_end_drop {
            ensure!(
                end_drop <= i64::from(max_end_drop),
                "probe {} lost {} bytes between first and last sample: {:?}",
                probe.name,
                end_drop,
                samples
            );
        }
        reports.push(MemoryProbeReport {
            name: probe.name.clone(),
            symbol: probe.symbol.clone(),
            samples,
            minimum_observed,
            maximum_observed,
            end_drop,
        });
    }
    Ok(reports)
}
fn marker_lines(output: &str) -> Vec<String> {
    let lines: Vec<_> = output.lines().collect();
    let mut result = Vec::new();
    for (index, line) in lines.iter().enumerate() {
        if line.trim().starts_with("SEDS_REG ") {
            result.push(line.trim().to_string());
            if let Some(value) = lines.get(index + 1) {
                result.push(value.trim().to_string());
            }
        }
    }
    result
}
fn observed_marker(output: &str, marker: &str) -> bool {
    output.lines().any(|line| {
        let line = line.trim();
        line == marker || (line.ends_with(marker) && line.contains("[INFO]"))
    })
}
fn tail(value: &str, count: usize) -> String {
    value
        .lines()
        .rev()
        .take(count)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect::<Vec<_>>()
        .join("\n")
}

#[cfg(test)]
mod tests {
    use super::{parse_memory_profile, render_peripheral_overlay};
    use crate::layout::{BoardLayout, MemoryProbe};

    fn layout(architecture: &str, peripherals: &str) -> BoardLayout {
        serde_json::from_str(&format!(
            r#"{{
                "name":"test",
                "architecture":"{architecture}",
                "memory":{{
                    "flash_base":134217728,"flash_size":524288,
                    "ram_regions":[{{"name":"sram","base":536870912,"size":114688}}],
                    "bootloader_size":16384,"slot_a_base":134234112,
                    "slot_a_size":475136,"erase_size":2048,
                    "write_alignment":8,"sedsnet_pool":4096
                }},
                "artifacts":{{
                    "elf":"fw.elf","bootloader_elf":"boot.elf",
                    "firmware":"fw.bin","bootloader":"boot.bin",
                    "factory":"factory.bin"
                }},
                "peripherals":{peripherals}
            }}"#
        ))
        .unwrap()
    }

    #[test]
    fn overlay_contains_only_layout_selected_g4_devices() {
        let board = layout(
            "stm32g4",
            r#"[
                {"type":"gps","name":"gps","model":"neo_m9n","bus":"spi1"},
                {"type":"adc","name":"rail","model":"ltc2990","bus":"i2c2@0x4c"}
            ]"#,
        );
        let overlay = render_peripheral_overlay(&board).unwrap();
        assert!(overlay.contains("physicalFlash: Memory.MappedMemory @ sysbus 0x08000000"));
        assert!(overlay.contains("physicalRam0: Memory.MappedMemory @ sysbus 0x20000000"));
        assert!(overlay.contains("size: 0x1c000"));
        assert!(overlay.contains("SedsNeoM9N @ spi1"));
        assert!(overlay.contains("SedsLtc2990 @ i2c2 0x4c"));
        assert!(!overlay.contains("SedsFlightSensorBus"));
        assert!(!overlay.contains("SedsStm32Adc"));
    }

    #[test]
    fn h5_sensor_declarations_share_one_physical_spi_model() {
        let board = layout(
            "stm32h5",
            r#"[
                {"type":"imu","name":"imu","model":"bmi088","bus":"spi1"},
                {"type":"barometer","name":"baro","model":"bmp390","bus":"spi1"}
            ]"#,
        );
        let overlay = render_peripheral_overlay(&board).unwrap();
        assert_eq!(overlay.matches("SedsFlightSensorBus @ spi1").count(), 1);
    }

    #[test]
    fn invalid_architecture_bus_combination_is_rejected() {
        let board = layout(
            "stm32h5",
            r#"[{"type":"gps","name":"gps","model":"neo_m9n","bus":"spi1"}]"#,
        );
        assert!(render_peripheral_overlay(&board).is_err());
    }

    #[test]
    fn g4_adc_instances_have_independent_register_maps() {
        let board = layout(
            "stm32g4",
            r#"[
                {"type":"adc","name":"adc1","model":"stm32_adc","bus":"adc1"},
                {"type":"adc","name":"adc2","model":"stm32_adc","bus":"adc2"},
                {"type":"adc","name":"adc3","model":"stm32_adc","bus":"adc3"}
            ]"#,
        );
        let overlay = render_peripheral_overlay(&board).unwrap();
        assert!(overlay.contains("sysbus 0x50000000"));
        assert!(overlay.contains("sysbus 0x50000100"));
        assert!(overlay.contains("sysbus 0x50000400"));
        assert_eq!(overlay.matches("layoutAdc12Common").count(), 1);
        assert_eq!(overlay.matches("layoutAdc345Common").count(), 1);
    }

    #[test]
    fn u5_adc_instances_are_selected_by_layout() {
        let board = layout(
            "stm32u5",
            r#"[
                {"type":"adc","name":"adc1","model":"stm32_adc","bus":"adc1"},
                {"type":"adc","name":"adc4","model":"stm32_adc","bus":"adc4"}
            ]"#,
        );
        let overlay = render_peripheral_overlay(&board).unwrap();
        assert!(overlay.contains("sysbus 0x42028000"));
        assert!(overlay.contains("sysbus 0x46021000"));
    }

    #[test]
    fn u5_sd_card_is_selected_by_layout() {
        let board = layout(
            "stm32u5",
            r#"[{"type":"storage","name":"sd","model":"sd_card","bus":"sdmmc1"}]"#,
        );
        let overlay = render_peripheral_overlay(&board).unwrap();
        assert!(overlay.contains("Storage.SedsStm32Sdmmc @ sysbus 0x420c8000"));
        assert!(overlay.contains("SedsStm32Sdmmc.cs"));
    }

    #[test]
    fn memory_probe_parser_tolerates_asynchronous_renode_logs() {
        let mut board = layout("stm32g4", "[]");
        board.execution.memory_probes.push(MemoryProbe {
            name: "pool".into(),
            symbol: "pool_available".into(),
            minimum: None,
            maximum: None,
            max_end_drop: None,
        });
        let report = parse_memory_profile(
            &board,
            "SEDS_PROBE pool 0\n[INFO] asynchronous peripheral message\n0x1234\n",
        )
        .unwrap();
        assert_eq!(report[0].samples, vec![0x1234]);
    }
}
