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
    pub trace: Option<String>,
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
    let renode = find_renode()?;
    let platform = platform_path(layout.architecture);
    let scratch = tempfile::tempdir().context("creating Renode scratch directory")?;
    let script = scratch.path().join("run.resc");
    let trace = scratch.path().join("execution.trace");
    fs::write(
        &script,
        render_script(layout, &platform, &elf, &bootloader_elf, &factory, &trace),
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
    ensure!(
        observed_marker(&combined, "SEDS_FIRMWARE_BOOT_REACHED"),
        "firmware never reached {}:\n{}",
        layout.execution.boot_success_symbol,
        tail(&combined, 80)
    );
    ensure!(
        observed_marker(&combined, "SEDS_FACTORY_BOOT_REACHED"),
        "factory boot flow never reached {}:\n{}",
        layout.execution.boot_success_symbol,
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
    let root = env::var_os("FIRMWARE_SIM_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")));
    root.join("renode/platforms").join(file)
}
fn render_script(
    layout: &BoardLayout,
    platform: &Path,
    elf: &Path,
    bootloader_elf: &Path,
    factory: &Path,
    trace: &Path,
) -> String {
    let seconds = layout.execution.virtual_time_ms as f64 / 1000.0;
    let tracing = if layout.execution.trace {
        format!(
            "cpu CreateExecutionTracing \"{}\" BinaryPC\n",
            trace.display()
        )
    } else {
        String::new()
    };
    let name = layout.name.replace('"', "_");
    let symbol = &layout.execution.boot_success_symbol;
    format!("mach create \"{}_firmware\"\nmachine LoadPlatformDescription @{}\nsysbus LoadELF @{}\ncpu AddHook `sysbus GetSymbolAddress \"{}\"` 'self.InfoLog(\"SEDS_FIRMWARE_BOOT_REACHED\")'\n{}emulation RunFor \"{}s\"\necho \"SEDS_REG FIRMWARE_PC\"\ncpu PC\necho \"SEDS_REG FIRMWARE_SP\"\ncpu GetRegister 13\ncpu IsHalted true\nmach create \"{}_factory\"\nmachine LoadPlatformDescription @{}\nsysbus LoadELF @{}\nsysbus LoadELF @{}\nsysbus LoadBinary @{} {}\ncpu AddHook `sysbus GetSymbolAddress \"{}\"` 'self.InfoLog(\"SEDS_FACTORY_BOOT_REACHED\")'\nemulation RunFor \"{}s\"\necho \"SEDS_REG FACTORY_PC\"\ncpu PC\necho \"SEDS_REG FACTORY_SP\"\ncpu GetRegister 13\necho \"SEDS_EXECUTION_COMPLETE\"\n", name, platform.display(), elf.display(), symbol, tracing, seconds, name, platform.display(), elf.display(), bootloader_elf.display(), factory.display(), layout.memory.flash_base, symbol, seconds)
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
