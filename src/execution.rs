use crate::{
    core::{ArchitectureKind, McuKind},
    layout::BoardLayout,
};
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
    pub firmware_driven_ota: bool,
    pub ota_outcome: Option<String>,
    pub flash_operations_observed: Option<u64>,
    pub flash_event_trace: Vec<String>,
    pub ota_power_cuts: Vec<OtaPowerCutReport>,
}

#[derive(Debug, Serialize)]
pub struct OtaPowerCutReport {
    pub after_flash_operation: u64,
    pub power_cut_triggered: bool,
    pub reboot_executed: bool,
    pub outcome: String,
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
    let ota_bytes = layout
        .artifacts
        .ota
        .as_ref()
        .map(|path| fs::read(root.join(path)))
        .transpose()
        .context("reading OTA transport artifact")?;
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
    let factory_bytes = fs::read(&factory).context("reading factory reset vector")?;
    ensure!(
        factory_bytes.len() >= 8,
        "factory image has no reset vector"
    );
    let factory_msp = u32::from_le_bytes(factory_bytes[0..4].try_into().unwrap());
    let factory_pc = u32::from_le_bytes(factory_bytes[4..8].try_into().unwrap());
    let firmware_reset = crate::elf::reset_vector(&elf, &layout.memory, "firmware")?;
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
    let scratch = tempfile::tempdir().context("creating Renode scratch directory")?;
    let firmware_image = scratch.path().join("firmware-flash.bin");
    fs::write(
        &firmware_image,
        crate::elf::flash_image(&elf, &layout.memory, "firmware")?,
    )
    .context("writing firmware flash image")?;
    let platform = materialize_platform(layout, scratch.path())?;
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
            ExecutionArtifacts {
                elf: &elf,
                bootloader_elf: &bootloader_elf,
                factory: &factory,
                firmware_image: &firmware_image,
            },
            ExecutionScenario {
                firmware_reset,
                factory_reset: (factory_msp, factory_pc),
                trace: &trace,
                ota: ota_bytes.as_deref(),
                power_cut_after: None,
            },
        ),
    )?;
    let combined = run_renode_script(&renode, &script)?;
    let registers = marker_lines(&combined);
    let memory_profile = parse_memory_profile(layout, &combined)?;
    ensure!(
        observed_marker(&combined, "SEDS_FIRMWARE_BOOT_REACHED"),
        "firmware never reached {}; final registers: {}\n{}",
        layout.execution.boot_success_symbol,
        registers.join(", "),
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
    let post_transfer_output = combined
        .split_once("SEDS_OTA_TRANSFER_BEGIN")
        .map(|(_, output)| output)
        .unwrap_or(&combined);
    let ota_outcome = unique_ota_outcome(layout, post_transfer_output)?;
    let flash_operations_observed = marker_u64(&combined, "SEDS_FLASH_OPERATIONS");
    let flash_event_trace = marker_csv(&combined, "SEDS_FLASH_EVENT_TRACE");
    if layout.ota.firmware_driven {
        ensure!(
            ota_outcome.is_some(),
            "firmware-driven OTA completed without a configured boot outcome:\n{}",
            tail(&combined, 80)
        );
        ensure!(
            flash_operations_observed.unwrap_or(0) > 0,
            "firmware-driven OTA did not perform an emulated flash operation"
        );
    }
    let mut ota_power_cuts = Vec::new();
    if layout.ota.firmware_driven
        && (layout.ota.power_cuts.every_flash_operation || !layout.ota.power_cuts.events.is_empty())
    {
        let operations =
            flash_operations_observed.context("missing baseline flash operation count")?;
        ensure!(
            flash_event_trace.len() as u64 == operations,
            "flash event trace has {} entries for {operations} operations",
            flash_event_trace.len()
        );
        let cut_operations: Vec<u64> = flash_event_trace
            .iter()
            .enumerate()
            .filter(|(_, event)| {
                layout.ota.power_cuts.every_flash_operation
                    || layout.ota.power_cuts.events.iter().any(|selected| {
                        matches!(
                            (selected, event.as_str()),
                            (crate::layout::FlashEventKind::EraseStart, "erase_start")
                                | (
                                    crate::layout::FlashEventKind::EraseComplete,
                                    "erase_complete"
                                )
                                | (crate::layout::FlashEventKind::ProgramUnit, "program_unit")
                        )
                    })
            })
            .map(|(index, _)| index as u64 + 1)
            .collect();
        ensure!(
            !cut_operations.is_empty(),
            "none of the selected power-cut events occurred during firmware OTA"
        );
        for cut in cut_operations {
            let cut_script = scratch.path().join(format!("power-cut-{cut}.resc"));
            let cut_trace = scratch.path().join(format!("power-cut-{cut}.trace"));
            fs::write(
                &cut_script,
                render_script(
                    layout,
                    &platform,
                    &peripheral_overlay,
                    ExecutionArtifacts {
                        elf: &elf,
                        bootloader_elf: &bootloader_elf,
                        factory: &factory,
                        firmware_image: &firmware_image,
                    },
                    ExecutionScenario {
                        firmware_reset,
                        factory_reset: (factory_msp, factory_pc),
                        trace: &cut_trace,
                        ota: ota_bytes.as_deref(),
                        power_cut_after: Some(cut),
                    },
                ),
            )?;
            let cut_output = run_renode_script(&renode, &cut_script)
                .with_context(|| format!("firmware OTA power cut after flash operation {cut}"))?;
            let post_reboot = cut_output
                .split_once("SEDS_POWER_CUT_REBOOT")
                .map(|(_, output)| output)
                .context("power-cut run did not execute a cold reboot")?;
            let outcome = unique_ota_outcome(layout, post_reboot)?
                .with_context(|| format!("no boot outcome after flash operation {cut}"))?;
            let power_cut_triggered = marker_bool(&cut_output, "SEDS_POWER_CUT_TRIGGERED")
                .context("flash model did not report its power-cut latch")?;
            ensure!(
                power_cut_triggered,
                "flash cut after operation {cut} was not triggered"
            );
            ota_power_cuts.push(OtaPowerCutReport {
                after_flash_operation: cut,
                power_cut_triggered,
                reboot_executed: true,
                outcome,
            });
        }
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
        firmware_driven_ota: layout.ota.firmware_driven,
        ota_outcome,
        flash_operations_observed,
        flash_event_trace,
        ota_power_cuts,
    })
}

fn unique_ota_outcome(layout: &BoardLayout, output: &str) -> Result<Option<String>> {
    let matches: Vec<_> = layout
        .ota
        .outcomes
        .iter()
        .filter(|outcome| observed_marker(output, &format!("SEDS_OTA_OUTCOME_{}", outcome.name)))
        .map(|outcome| outcome.name.clone())
        .collect();
    ensure!(
        matches.len() <= 1,
        "OTA run reached conflicting boot outcomes: {}",
        matches.join(", ")
    );
    Ok(matches.into_iter().next())
}

fn run_renode_script(renode: &Path, script: &Path) -> Result<String> {
    let output = Command::new(renode)
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
    Ok(combined)
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
fn platform_path(kind: McuKind) -> PathBuf {
    simulator_root()
        .join("renode/platforms")
        .join(kind.descriptor().platform_file)
}

pub(crate) fn materialize_platform(layout: &BoardLayout, directory: &Path) -> Result<PathBuf> {
    let source = platform_path(layout.mcu());
    let mut text = fs::read_to_string(&source)
        .with_context(|| format!("reading platform {}", source.display()))?;
    ensure!(
        text.contains(&format!(
            "cpuType: \"{}\"",
            layout.mcu().descriptor().core_model
        )),
        "MCU catalog core {} does not match platform {}",
        layout.mcu().descriptor().core_model,
        source.display()
    );
    if layout.board.clocks.is_empty() && !layout.board.security.trustzone {
        return Ok(source);
    }
    for clock in &layout.board.clocks {
        let header = format!("{}:", clock.peripheral);
        let start = text
            .lines()
            .enumerate()
            .find(|(_, line)| !line.starts_with(char::is_whitespace) && line.starts_with(&header))
            .map(|(index, _)| index)
            .with_context(|| {
                format!(
                    "clock target {} is not in the MCU platform",
                    clock.peripheral
                )
            })?;
        let mut lines: Vec<String> = text.lines().map(str::to_owned).collect();
        let frequency = lines[start + 1..]
            .iter()
            .position(|line| !line.is_empty() && !line.starts_with(char::is_whitespace))
            .map(|relative| start + 1 + relative)
            .unwrap_or(lines.len());
        let line = lines[start + 1..frequency]
            .iter()
            .position(|line| line.trim_start().starts_with("frequency:"))
            .map(|relative| start + 1 + relative)
            .with_context(|| {
                format!(
                    "clock target {} has no configurable frequency",
                    clock.peripheral
                )
            })?;
        let indentation = lines[line]
            .chars()
            .take_while(|c| c.is_whitespace())
            .collect::<String>();
        lines[line] = format!("{indentation}frequency: {}", clock.frequency_hz);
        text = format!("{}\n", lines.join("\n"));
    }
    if layout.board.security.trustzone {
        let mut lines: Vec<String> = text.lines().map(str::to_owned).collect();
        let cpu = lines
            .iter()
            .position(|line| line.starts_with("cpu:"))
            .context("MCU platform has no Cortex-M CPU declaration")?;
        let cpu_end = lines[cpu + 1..]
            .iter()
            .position(|line| !line.is_empty() && !line.starts_with(char::is_whitespace))
            .map(|relative| cpu + 1 + relative)
            .unwrap_or(lines.len());
        ensure!(
            lines[cpu + 1..cpu_end]
                .iter()
                .any(|line| line.contains("cortex-m33")),
            "TrustZone requires a Cortex-M33 platform"
        );
        if !lines[cpu + 1..cpu_end]
            .iter()
            .any(|line| line.trim_start().starts_with("enableTrustZone:"))
        {
            lines.insert(cpu + 2, "    enableTrustZone: true".into());
        }
        text = format!("{}\n", lines.join("\n"));
    }
    let output = directory.join("configured-platform.repl");
    fs::write(&output, text).context("writing configured MCU platform")?;
    Ok(output)
}

pub(crate) fn validate_platform_config(layout: &BoardLayout) -> Result<()> {
    let scratch = tempfile::tempdir().context("validating configured MCU platform")?;
    materialize_platform(layout, scratch.path()).map(|_| ())
}

fn simulator_root() -> PathBuf {
    env::var_os("FIRMWARE_SIM_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")))
}

pub(crate) fn render_peripheral_overlay(layout: &BoardLayout) -> Result<String> {
    let source_root = simulator_root().join("renode/peripherals");
    let flash_model = source_root.join("SedsStm32Flash.cs");
    let mut overlay = format!(
        "flashBacking: Memory.MappedMemory @ sysbus 0x{:08x}\n    size: 0x{:x}\nphysicalFlash: MTD.SedsStm32FlashController @ sysbus 0x40022000\n    flash: flashBacking\n    mcu: \"{}\"\n    eraseSize: {}\n    writeAlignment: {}\n    flashBase: 0x{:08x}\n    preinit:\n        include @{}\n",
        layout.memory.flash_base,
        layout.memory.flash_size,
        layout.mcu(),
        layout.memory.erase_size,
        layout.memory.write_alignment,
        layout.memory.flash_base,
        flash_model.display(),
    );
    for (index, region) in layout.memory.ram_regions.iter().enumerate() {
        overlay.push_str(&format!(
            "physicalRam{index}: Memory.MappedMemory @ sysbus 0x{:08x}\n    size: 0x{:x}\n",
            region.base, region.size
        ));
    }
    for (index, connection) in layout.board.connections.iter().enumerate() {
        if connection.active_low {
            overlay.push_str(&format!(
                "layoutSignalInverter{index}: Miscellaneous.SedsSignalInverter\n    preinit:\n        include @{}\n    Output -> {}\n{}",
                source_root.join("SedsSignalInverter.cs").display(),
                connection.to,
                render_wire(&connection.from, &format!("layoutSignalInverter{index}@0"))
            ));
        } else {
            overlay.push_str(&render_wire(&connection.from, &connection.to));
        }
    }
    for route in &layout.board.dma_routes {
        overlay.push_str(&render_wire(
            &route.request,
            &format!("{}@{}", route.controller, route.channel),
        ));
    }
    let mut flight_sensor_bus_added = false;
    let mut flight_sensor_faults = None;
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
                    "layoutDevice{index}: Sensors.SedsNeoM9N @ spi1\n    failureEvery: {}\n    disconnectAfter: {}\n    preinit:\n        include @{}\n",
                    peripheral.failure_every.unwrap_or(0),
                    peripheral.disconnect_after.unwrap_or(u64::MAX),
                    source_root.join("SedsSpiSensors.cs").display(),
                ));
            }
            (ArchitectureKind::Stm32g4, "ltc2990", address)
                if matches!(address, "i2c2@0x4c" | "i2c2@0x4d") =>
            {
                let address = address.rsplit_once('@').unwrap().1;
                overlay.push_str(&format!(
                    "layoutDevice{index}: Sensors.SedsLtc2990 @ i2c2 {address}\n    failureEvery: {}\n    disconnectAfter: {}\n    preinit:\n        include @{}\n",
                    peripheral.failure_every.unwrap_or(0),
                    peripheral.disconnect_after.unwrap_or(u64::MAX),
                    source_root.join("SedsLtc2990.cs").display()
                ));
            }
            (ArchitectureKind::Stm32h5, "bmi088" | "bmp390", "spi1") => {
                let faults = (peripheral.failure_every, peripheral.disconnect_after);
                ensure!(
                    flight_sensor_faults.is_none() || flight_sensor_faults == Some(faults),
                    "H5 SPI1 flight sensors share one physical bus model and must use identical fault schedules"
                );
                flight_sensor_faults = Some(faults);
                if !flight_sensor_bus_added {
                    overlay.push_str(&format!(
                        "layoutFlightSensors: Sensors.SedsFlightSensorBus @ spi1\n    failureEvery: {}\n    disconnectAfter: {}\n    preinit:\n        include @{}\n",
                        peripheral.failure_every.unwrap_or(0),
                        peripheral.disconnect_after.unwrap_or(u64::MAX),
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
                    "layoutAdc{index}: Sensors.SedsStm32Adc @ sysbus 0x{address:08x}\n    bits: {}\n    channels: {}\n    samples: \"{}\"\n    noiseLsb: {}\n    failureEvery: {}\n    disconnectAfter: {}\n    preinit:\n        include @{}\n",
                    peripheral.bits.unwrap_or(12), peripheral.channels.unwrap_or(1),
                    peripheral.channel_samples.iter().map(u32::to_string).collect::<Vec<_>>().join(","), peripheral.noise_lsb.unwrap_or(0),
                    peripheral.failure_every.unwrap_or(0), peripheral.disconnect_after.unwrap_or(u64::MAX),
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
                    "layoutAdc{index}: Sensors.SedsStm32Adc @ sysbus 0x42228000\n    bits: {}\n    channels: {}\n    samples: \"{}\"\n    noiseLsb: {}\n    failureEvery: {}\n    disconnectAfter: {}\n    preinit:\n        include @{}\n",
                    peripheral.bits.unwrap_or(12), peripheral.channels.unwrap_or(1),
                    peripheral.channel_samples.iter().map(u32::to_string).collect::<Vec<_>>().join(","), peripheral.noise_lsb.unwrap_or(0),
                    peripheral.failure_every.unwrap_or(0), peripheral.disconnect_after.unwrap_or(u64::MAX),
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
                    "layoutAdc{index}: Sensors.SedsStm32Adc @ sysbus 0x{address:08x}\n    bits: {}\n    channels: {}\n    samples: \"{}\"\n    noiseLsb: {}\n    failureEvery: {}\n    disconnectAfter: {}\n    preinit:\n        include @{}\n",
                    peripheral.bits.unwrap_or(12), peripheral.channels.unwrap_or(1),
                    peripheral.channel_samples.iter().map(u32::to_string).collect::<Vec<_>>().join(","), peripheral.noise_lsb.unwrap_or(0),
                    peripheral.failure_every.unwrap_or(0), peripheral.disconnect_after.unwrap_or(u64::MAX),
                    source_root.join("SedsStm32Adc.cs").display()
                ));
            }
            (ArchitectureKind::Stm32h5 | ArchitectureKind::Stm32u5, "sd_card", "sdmmc1") => {
                let controller = if layout.architecture == ArchitectureKind::Stm32h5 {
                    "sdmmc"
                } else {
                    "sdmmc1"
                };
                overlay.push_str(&format!(
                    "{controller}:\n    CardCapacityBytes: {}\n    FailureEvery: {}\n    DisconnectAfter: {}\n",
                    peripheral.capacity_bytes.unwrap_or(4 * 1024 * 1024),
                    peripheral.failure_every.unwrap_or(0),
                    peripheral.disconnect_after.unwrap_or(u64::MAX)
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

fn render_wire(source: &str, target: &str) -> String {
    match source.split_once('.') {
        Some((peripheral, output)) => {
            format!("{peripheral}:\n    {output} -> {target}\n")
        }
        None => format!("{source}:\n    -> {target}\n"),
    }
}

#[derive(Clone, Copy)]
struct ExecutionArtifacts<'a> {
    elf: &'a Path,
    bootloader_elf: &'a Path,
    factory: &'a Path,
    firmware_image: &'a Path,
}

#[derive(Clone, Copy)]
struct ExecutionScenario<'a> {
    firmware_reset: (u32, u32, u32),
    factory_reset: (u32, u32),
    trace: &'a Path,
    ota: Option<&'a [u8]>,
    power_cut_after: Option<u64>,
}

fn render_script(
    layout: &BoardLayout,
    platform: &Path,
    peripheral_overlay: &Path,
    artifacts: ExecutionArtifacts<'_>,
    scenario: ExecutionScenario<'_>,
) -> String {
    let ExecutionArtifacts {
        elf,
        bootloader_elf,
        factory,
        firmware_image,
    } = artifacts;
    let ExecutionScenario {
        firmware_reset,
        factory_reset,
        trace,
        ota,
        power_cut_after,
    } = scenario;
    let (firmware_msp, firmware_pc, firmware_vtor) = firmware_reset;
    let (factory_msp, factory_pc) = factory_reset;
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
    let ota_script = render_ota_script(layout, ota, power_cut_after);
    let outcome_hooks = layout
        .ota
        .outcomes
        .iter()
        .map(|outcome| {
            format!(
        "cpu AddHook `sysbus GetSymbolAddress \"{}\" 0` 'self.InfoLog(\"SEDS_OTA_OUTCOME_{}\")'\n",
        outcome.symbol, outcome.name
    )
        })
        .collect::<String>();
    let strict_mmio = if layout.board.strict_mmio {
        "sysbus UnhandledAccessBehaviour ThrowException\n"
    } else {
        ""
    };
    let security = render_security_script(layout);
    let board_initialization = render_board_initialization_script(layout);
    format!("mach create \"{}_firmware\"\nmachine LoadPlatformDescription @{}\nmachine LoadPlatformDescription @{}\n{}{}{}sysbus LoadSymbolsFrom @{}\nsysbus LoadBinary @{} {}\nphysicalFlash EndHostLoading\ncpu SetRegister 13 0x{:08x}\ncpu PC 0x{:08x}\ncpu VectorTableOffset 0x{:08x}\ncpu AddHook `sysbus GetSymbolAddress \"{}\"` 'self.InfoLog(\"SEDS_FIRMWARE_BOOT_REACHED\")'\n{}{}{}echo \"SEDS_REG FIRMWARE_PC\"\ncpu PC\necho \"SEDS_REG FIRMWARE_SP\"\ncpu GetRegister 13\necho \"SEDS_REG FIRMWARE_LR\"\ncpu GetRegister 14\ncpu IsHalted true\nmach create \"{}_factory\"\nmachine LoadPlatformDescription @{}\nmachine LoadPlatformDescription @{}\n{}{}{}sysbus LoadSymbolsFrom @{}\nsysbus LoadSymbolsFrom @{}\nsysbus LoadBinary @{} {}\nphysicalFlash EndHostLoading\ncpu SetRegister 13 0x{:08x}\ncpu PC 0x{:08x}\ncpu AddHook `sysbus GetSymbolAddress \"{}\" 0` 'self.InfoLog(\"SEDS_FACTORY_BOOT_REACHED\")'\n{}{}emulation RunFor \"{}s\"\n{}echo \"SEDS_FLASH_OPERATIONS\"\nphysicalFlash GetOperationCount\necho \"SEDS_FLASH_EVENT_TRACE\"\nphysicalFlash GetOperationTrace\necho \"SEDS_REG FACTORY_PC\"\ncpu PC\necho \"SEDS_REG FACTORY_SP\"\ncpu GetRegister 13\necho \"SEDS_REG FACTORY_LR\"\ncpu GetRegister 14\necho \"SEDS_EXECUTION_COMPLETE\"\n", name, platform.display(), peripheral_overlay.display(), strict_mmio, security, board_initialization, elf.display(), firmware_image.display(), layout.memory.flash_base, firmware_msp, firmware_pc, firmware_vtor, symbol, tick_hook, tracing, profile_script, name, platform.display(), peripheral_overlay.display(), strict_mmio, security, board_initialization, elf.display(), bootloader_elf.display(), factory.display(), layout.memory.flash_base, factory_msp, factory_pc, factory_symbol, tick_hook, outcome_hooks, factory_seconds, ota_script)
}

fn render_board_initialization_script(layout: &BoardLayout) -> String {
    layout
        .board
        .pins
        .iter()
        .map(|pin| match pin.initial {
            crate::layout::PinState::Low => format!("{} DrivePin {} false\n", pin.gpio, pin.pin),
            crate::layout::PinState::High => format!("{} DrivePin {} true\n", pin.gpio, pin.pin),
            crate::layout::PinState::Floating => format!("{} ReleasePin {}\n", pin.gpio, pin.pin),
        })
        .collect()
}

pub(crate) fn non_secure_regions(layout: &BoardLayout) -> Vec<(u64, u64)> {
    let mut physical = vec![(
        layout.memory.flash_base,
        layout.memory.flash_base + layout.memory.flash_size,
    )];
    physical.extend(
        layout
            .memory
            .ram_regions
            .iter()
            .map(|region| (region.base, region.base + region.size)),
    );
    let secure = &layout.board.security.secure_regions;
    let mut result = Vec::new();
    for (start, end) in physical {
        let mut cursor = start;
        let mut inside = secure
            .iter()
            .filter(|region| region.base >= start && region.base < end)
            .collect::<Vec<_>>();
        inside.sort_by_key(|region| region.base);
        for region in inside {
            if cursor < region.base {
                result.push((cursor, region.base));
            }
            cursor = cursor.max(region.base + region.size);
        }
        if cursor < end {
            result.push((cursor, end));
        }
    }
    result
}

fn render_security_script(layout: &BoardLayout) -> String {
    if layout.board.security.secure_regions.is_empty() {
        return String::new();
    }
    let mut script = String::new();
    for (index, (base, end)) in non_secure_regions(layout).iter().enumerate() {
        script.push_str(&format!(
            "cpu SAURegionNumber {index}\ncpu SAURegionBaseAddress 0x{base:08x}\ncpu SAURegionLimitAddress 0x{:08x}\n",
            (end - 1) | 1
        ));
    }
    script.push_str("cpu SAUControl 1\n");
    script
}

fn render_ota_script(
    layout: &BoardLayout,
    ota: Option<&[u8]>,
    power_cut_after: Option<u64>,
) -> String {
    if !layout.ota.firmware_driven {
        return String::new();
    }
    let transport = layout
        .ota
        .transport
        .as_ref()
        .expect("validated OTA transport");
    let payload = ota.expect("validated OTA artifact");
    let mut script = format!(
        "emulation RunFor \"{}s\"\necho \"SEDS_OTA_TRANSFER_BEGIN\"\n",
        layout.ota.start_after_ms as f64 / 1000.0
    );
    if let Some(operation) = power_cut_after {
        script.push_str(&format!("physicalFlash ArmPowerCut {operation}\n"));
    }
    match transport.kind {
        crate::layout::OtaTransportKind::Uart => {
            for chunk in payload.chunks(layout.ota.chunk_size) {
                for byte in chunk {
                    script.push_str(&format!(
                        "{} WriteChar 0x{:02x}\n",
                        transport.peripheral, byte
                    ));
                }
                script.push_str(&format!(
                    "emulation RunFor \"{}s\"\n",
                    (layout.ota.inter_byte_us * chunk.len() as u64) as f64 / 1_000_000.0
                ));
            }
        }
        crate::layout::OtaTransportKind::Can => {
            let id = transport.can_id.expect("validated CAN identifier");
            let mtu = transport.mtu.unwrap_or(64);
            for frame in payload.chunks(mtu) {
                let bytes = frame
                    .iter()
                    .map(u8::to_string)
                    .collect::<Vec<_>>()
                    .join(",");
                script.push_str(&format!(
                    "python \"from Antmicro.Renode.Core.CAN import CANMessageFrame; monitor.Machine['sysbus.{}'].OnFrameReceived(CANMessageFrame(0x{id:x}, System.Array[System.Byte]([{bytes}])))\"\n",
                    transport.peripheral
                ));
                script.push_str(&format!(
                    "emulation RunFor \"{}s\"\n",
                    (layout.ota.inter_byte_us * frame.len() as u64) as f64 / 1_000_000.0
                ));
            }
        }
        crate::layout::OtaTransportKind::Usb => {
            let endpoint = transport.endpoint.unwrap_or(1);
            for packet in payload.chunks(layout.ota.chunk_size) {
                let bytes = packet
                    .iter()
                    .map(u8::to_string)
                    .collect::<Vec<_>>()
                    .join(",");
                script.push_str(&format!(
                    "python \"monitor.Machine['sysbus.{}'].InjectPacket(System.Array[System.Byte]([{bytes}]), {endpoint})\"\n",
                    transport.peripheral
                ));
                script.push_str(&format!(
                    "emulation RunFor \"{}s\"\n",
                    (layout.ota.inter_byte_us * packet.len() as u64) as f64 / 1_000_000.0
                ));
            }
        }
        crate::layout::OtaTransportKind::Sdmmc => {
            script.push_str(&format!(
                "python \"monitor.Machine['sysbus.{}'].BeginCardImage()\"\n",
                transport.peripheral
            ));
            for chunk in payload.chunks(layout.ota.chunk_size) {
                let bytes = chunk
                    .iter()
                    .map(u8::to_string)
                    .collect::<Vec<_>>()
                    .join(",");
                script.push_str(&format!(
                    "python \"monitor.Machine['sysbus.{}'].AppendCardBytes(System.Array[System.Byte]([{bytes}]))\"\n",
                    transport.peripheral
                ));
            }
            script.push_str(&format!(
                "python \"monitor.Machine['sysbus.{}'].MountCardImage()\"\n",
                transport.peripheral
            ));
        }
    }
    script.push_str(&format!(
        "emulation RunFor \"{}s\"\n",
        layout.ota.power_cuts.reboot_time_ms as f64 / 1000.0
    ));
    if power_cut_after.is_some() {
        script.push_str(&format!(
            "echo \"SEDS_POWER_CUT_TRIGGERED\"\nphysicalFlash GetPowerCutTriggered\necho \"SEDS_POWER_CUT_REBOOT\"\npython \"[(e.Peripheral.Reset() if hasattr(e.Peripheral, 'Reset') else None) for e in monitor.Machine.GetRegisteredPeripherals() if e.Name not in ('sysbus', 'flashBacking')]\"\nphysicalFlash DisarmPowerCut\ncpu SetRegister 13 `sysbus ReadDoubleWord 0x{:08x}`\ncpu PC `sysbus ReadDoubleWord 0x{:08x}`\nemulation RunFor \"{}s\"\n",
            layout.memory.flash_base,
            layout.memory.flash_base + 4,
            layout.ota.power_cuts.reboot_time_ms as f64 / 1000.0
        ));
    }
    script
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
fn marker_u64(output: &str, marker: &str) -> Option<u64> {
    let lines: Vec<_> = output.lines().collect();
    let index = lines.iter().position(|line| line.trim() == marker)?;
    lines[index + 1..].iter().find_map(|line| {
        let value = line.trim();
        value
            .strip_prefix("0x")
            .and_then(|hex| u64::from_str_radix(hex, 16).ok())
            .or_else(|| value.parse().ok())
    })
}
fn marker_bool(output: &str, marker: &str) -> Option<bool> {
    let lines: Vec<_> = output.lines().collect();
    let index = lines.iter().position(|line| line.trim() == marker)?;
    lines[index + 1..]
        .iter()
        .find_map(|line| match line.trim().to_ascii_lowercase().as_str() {
            "true" => Some(true),
            "false" => Some(false),
            _ => None,
        })
}
fn marker_csv(output: &str, marker: &str) -> Vec<String> {
    let lines: Vec<_> = output.lines().collect();
    let Some(index) = lines.iter().position(|line| line.trim() == marker) else {
        return Vec::new();
    };
    lines[index + 1..]
        .iter()
        .map(|line| line.trim().trim_matches('"'))
        .find(|line| {
            !line.is_empty()
                && line
                    .chars()
                    .all(|c| c.is_ascii_lowercase() || c == '_' || c == ',')
        })
        .map(|line| line.split(',').map(str::to_owned).collect())
        .unwrap_or_default()
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
    use super::{
        marker_csv, marker_u64, materialize_platform, parse_memory_profile, render_ota_script,
        render_peripheral_overlay, render_script, render_security_script, ExecutionArtifacts,
        ExecutionScenario,
    };
    use crate::layout::{BoardLayout, MemoryProbe};

    fn layout(architecture: &str, peripherals: &str) -> BoardLayout {
        let mcu = match architecture {
            "stm32g4" => "stm32g491",
            "stm32h5" => "stm32h523",
            "stm32u5" => "stm32u585",
            _ => unreachable!(),
        };
        serde_json::from_str(&format!(
            r#"{{
                "name":"test",
                "architecture":"{architecture}",
                "mcu":"{mcu}",
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
        assert!(overlay.contains("flashBacking: Memory.MappedMemory @ sysbus 0x08000000"));
        assert!(overlay.contains("physicalFlash: MTD.SedsStm32FlashController"));
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
    fn shared_h5_sensor_bus_rejects_conflicting_fault_schedules() {
        let board = layout(
            "stm32h5",
            r#"[
                {"type":"imu","name":"imu","model":"bmi088","bus":"spi1","failure_every":3},
                {"type":"barometer","name":"baro","model":"bmp390","bus":"spi1","failure_every":4}
            ]"#,
        );
        assert!(render_peripheral_overlay(&board).is_err());
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
        assert!(overlay.contains("sdmmc1:"));
    }

    #[test]
    fn h5_sd_card_maps_logical_bus_to_platform_controller() {
        let board = layout(
            "stm32h5",
            r#"[{"type":"storage","name":"sd","model":"sd_card","bus":"sdmmc1"}]"#,
        );
        let overlay = render_peripheral_overlay(&board).unwrap();
        assert!(overlay.contains("sdmmc:"));
        assert!(!overlay.contains("sdmmc1:"));
        assert!(overlay.contains("FailureEvery: 0"));
    }

    #[test]
    fn factory_boot_uses_binary_vectors_and_symbol_only_elf_loading() {
        let board = layout("stm32g4", "[]");
        let script = render_script(
            &board,
            std::path::Path::new("platform.repl"),
            std::path::Path::new("overlay.repl"),
            ExecutionArtifacts {
                elf: std::path::Path::new("firmware.elf"),
                bootloader_elf: std::path::Path::new("bootloader.elf"),
                factory: std::path::Path::new("factory.bin"),
                firmware_image: std::path::Path::new("firmware-flash.bin"),
            },
            ExecutionScenario {
                firmware_reset: (0x2001_bff0, 0x0800_4001, 0x0800_4200),
                factory_reset: (0x2001_c000, 0x0800_0001),
                trace: std::path::Path::new("trace.bin"),
                ota: None,
                power_cut_after: None,
            },
        );
        let firmware = script.split("_firmware\"").nth(1).unwrap();
        assert!(firmware.contains("cpu SetRegister 13 0x2001bff0"));
        assert!(firmware.contains("cpu PC 0x08004001"));
        assert!(firmware.contains("cpu VectorTableOffset 0x08004200"));
        let factory = script.split("_factory\"").nth(1).unwrap();
        assert!(factory.contains("LoadSymbolsFrom @firmware.elf"));
        assert!(factory.contains("LoadSymbolsFrom @bootloader.elf"));
        assert!(!factory.contains("LoadELF"));
        assert!(factory.contains("cpu SetRegister 13 0x2001c000"));
        assert!(factory.contains("cpu PC 0x08000001"));
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

    #[test]
    fn firmware_ota_uses_real_uart_receive_method_and_virtual_time() {
        let mut board = layout("stm32g4", "[]");
        board.ota.firmware_driven = true;
        board.ota.chunk_size = 2;
        board.ota.transport = Some(crate::layout::OtaTransport {
            kind: crate::layout::OtaTransportKind::Uart,
            peripheral: "uart4".into(),
            can_id: None,
            mtu: None,
            endpoint: None,
        });
        let script = render_ota_script(&board, Some(&[0x12, 0x34, 0x56]), None);
        assert!(script.contains("uart4 WriteChar 0x12"));
        assert!(script.contains("uart4 WriteChar 0x56"));
        assert!(script.contains("SEDS_OTA_TRANSFER_BEGIN"));
    }

    #[test]
    fn flash_counter_parser_accepts_renode_hex_output() {
        assert_eq!(
            marker_u64(
                "SEDS_FLASH_OPERATIONS\n0x000000000000002a\n",
                "SEDS_FLASH_OPERATIONS"
            ),
            Some(42)
        );
        assert_eq!(
            marker_csv(
                "SEDS_FLASH_EVENT_TRACE\nprogram_unit,erase_complete\n",
                "SEDS_FLASH_EVENT_TRACE"
            ),
            vec!["program_unit", "erase_complete"]
        );
    }

    #[test]
    fn firmware_ota_can_frames_enter_the_real_controller() {
        let mut board = layout("stm32g4", "[]");
        board.ota.firmware_driven = true;
        board.ota.transport = Some(crate::layout::OtaTransport {
            kind: crate::layout::OtaTransportKind::Can,
            peripheral: "fdcan1".into(),
            can_id: Some(0x321),
            mtu: Some(2),
            endpoint: None,
        });
        let script = render_ota_script(&board, Some(&[1, 2, 3]), None);
        assert_eq!(script.matches("OnFrameReceived").count(), 2);
        assert!(script.contains("CANMessageFrame(0x321"));
        assert!(script.contains("System.Array[System.Byte]([1,2])"));
    }

    #[test]
    fn firmware_ota_usb_packets_enter_the_selected_endpoint() {
        let mut board = layout("stm32u5", "[]");
        board.ota.firmware_driven = true;
        board.ota.chunk_size = 2;
        board.ota.transport = Some(crate::layout::OtaTransport {
            kind: crate::layout::OtaTransportKind::Usb,
            peripheral: "usb".into(),
            can_id: None,
            mtu: None,
            endpoint: Some(3),
        });
        let script = render_ota_script(&board, Some(&[1, 2, 3]), None);
        assert_eq!(script.matches("InjectPacket").count(), 2);
        assert!(script.contains("System.Array[System.Byte]([1,2]), 3"));
    }

    #[test]
    fn firmware_ota_sdmmc_mounts_a_chunked_card_image() {
        let mut board = layout("stm32h5", "[]");
        board.ota.firmware_driven = true;
        board.ota.chunk_size = 2;
        board.ota.transport = Some(crate::layout::OtaTransport {
            kind: crate::layout::OtaTransportKind::Sdmmc,
            peripheral: "sdmmc".into(),
            can_id: None,
            mtu: None,
            endpoint: None,
        });
        let script = render_ota_script(&board, Some(&[1, 2, 3]), None);
        assert!(script.contains("BeginCardImage"));
        assert_eq!(script.matches("AppendCardBytes").count(), 2);
        assert!(script.contains("MountCardImage"));
    }

    #[test]
    fn board_clock_rewrites_the_real_platform_constructor() {
        let mut board = layout("stm32g4", "[]");
        board.board.clocks.push(crate::layout::ClockConfig {
            peripheral: "uart4".into(),
            frequency_hz: 80_000_000,
        });
        let scratch = tempfile::tempdir().unwrap();
        let configured = materialize_platform(&board, scratch.path()).unwrap();
        let platform = std::fs::read_to_string(configured).unwrap();
        let uart = platform
            .split("uart4:")
            .nth(1)
            .unwrap()
            .split("usart1:")
            .next()
            .unwrap();
        assert!(uart.starts_with(" UART.STM32F7_USART"));
        assert!(uart.contains("frequency: 80000000"));
        assert!(!uart.contains("frequency: 170000000"));
    }

    #[test]
    fn power_cut_script_reboots_from_persistent_factory_vectors() {
        let mut board = layout("stm32g4", "[]");
        board.ota.firmware_driven = true;
        board.ota.transport = Some(crate::layout::OtaTransport {
            kind: crate::layout::OtaTransportKind::Uart,
            peripheral: "uart4".into(),
            can_id: None,
            mtu: None,
            endpoint: None,
        });
        let script = render_ota_script(&board, Some(&[1, 2]), Some(7));
        assert!(script.contains("physicalFlash ArmPowerCut 7"));
        assert!(script.contains("GetRegisteredPeripherals"));
        assert!(script.contains("e.Name not in ('sysbus', 'flashBacking')"));
        assert!(script.contains("physicalFlash DisarmPowerCut"));
        assert!(script.contains("cpu SetRegister 13 `sysbus ReadDoubleWord 0x08000000`"));
        assert!(script.contains("cpu PC `sysbus ReadDoubleWord 0x08000004`"));
    }

    #[test]
    fn m33_platform_enables_trustzone_when_board_requests_it() {
        let mut board = layout("stm32u5", "[]");
        board.board.security.trustzone = true;
        let scratch = tempfile::tempdir().unwrap();
        let configured = materialize_platform(&board, scratch.path()).unwrap();
        let platform = std::fs::read_to_string(configured).unwrap();
        let cpu = platform.split("cpu:").nth(1).unwrap();
        assert!(cpu.contains("cpuType: \"cortex-m33\""));
        assert!(cpu.contains("enableTrustZone: true"));
    }

    #[test]
    fn secure_regions_generate_real_sau_non_secure_complements() {
        let mut board = layout("stm32u5", "[]");
        board.board.security.trustzone = true;
        board
            .board
            .security
            .secure_regions
            .push(crate::layout::MemoryRegion {
                name: "secure_boot".into(),
                base: 0x0800_0000,
                size: 0x2000,
            });
        let script = render_security_script(&board);
        assert!(!script.contains("SAURegionBaseAddress 0x08000000"));
        assert!(script.contains("SAURegionBaseAddress 0x08002000"));
        assert!(script.contains("cpu SAUControl 1"));
    }

    #[test]
    fn board_wires_support_numbered_outputs_and_active_low_routes() {
        let mut board = layout("stm32g4", "[]");
        board
            .board
            .connections
            .push(crate::layout::ConnectionConfig {
                from: "gpio.0".into(),
                to: "gpio@1".into(),
                active_low: true,
            });
        let overlay = render_peripheral_overlay(&board).unwrap();
        assert!(overlay.contains("SedsSignalInverter"));
        assert!(overlay.contains("Output -> gpio@1"));
        assert!(overlay.contains("gpio:\n    0 -> layoutSignalInverter0@0"));
    }
}
