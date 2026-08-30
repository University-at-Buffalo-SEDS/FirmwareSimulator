use crate::{core::Architecture, execution::MemoryProbeReport, layout::BoardLayout};
use anyhow::{bail, ensure, Context, Result};
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    env, fs,
    path::{Path, PathBuf},
    process::Command,
};

#[derive(Debug, Deserialize)]
pub struct BayTopology {
    pub name: String,
    #[serde(default = "default_quantum")]
    pub quantum_seconds: f64,
    #[serde(default = "default_duration")]
    pub virtual_time_ms: u64,
    #[serde(default = "default_sample_count")]
    pub sample_count: usize,
    pub nodes: Vec<Node>,
    #[serde(default)]
    pub links: Vec<Link>,
}
#[derive(Debug, Deserialize)]
pub struct Node {
    pub name: String,
    pub layout: PathBuf,
    pub firmware_root: PathBuf,
}
#[derive(Debug, Deserialize)]
pub struct Link {
    pub name: String,
    pub kind: LinkKind,
    pub endpoints: Vec<Endpoint>,
}
#[derive(Clone, Copy, Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LinkKind {
    Can,
    Uart,
}
#[derive(Debug, Deserialize)]
pub struct Endpoint {
    pub node: String,
    pub peripheral: String,
    #[serde(default)]
    pub activity_probe: Option<String>,
    #[serde(default = "default_minimum_activity")]
    pub minimum_activity: u32,
}
#[derive(Debug, Serialize)]
pub struct BayReport {
    pub bay: String,
    pub nodes_executed: usize,
    pub links_connected: usize,
    pub virtual_time_ms: u64,
    pub register_dump: Vec<String>,
    pub memory_profiles: BTreeMap<String, Vec<MemoryProbeReport>>,
}

pub fn run(topology_path: &Path) -> Result<BayReport> {
    if !Path::new("/.dockerenv").exists()
        && env::var("FIRMWARE_SIM_CONTAINER").as_deref() != Ok("1")
    {
        eprintln!(
            "warning: native avionics-bay simulation is unsupported and runs at your own risk; \
             use the published Docker image for validated results"
        );
    }
    let bytes =
        fs::read(topology_path).with_context(|| format!("reading {}", topology_path.display()))?;
    let topology: BayTopology = serde_json::from_slice(&bytes)?;
    ensure!(
        !topology.nodes.is_empty(),
        "avionics bay must contain at least one node"
    );
    ensure!(
        topology.sample_count > 1,
        "bay sample_count must exceed one"
    );
    ensure!(
        topology.sample_count as u64 <= topology.virtual_time_ms,
        "bay sample_count cannot exceed virtual_time_ms"
    );
    for (index, node) in topology.nodes.iter().enumerate() {
        ensure!(
            !topology.nodes[..index]
                .iter()
                .any(|other| other.name == node.name),
            "duplicate bay node {}",
            node.name
        );
    }
    let base = topology_path.parent().unwrap_or_else(|| Path::new("."));
    let scratch = tempfile::tempdir()?;
    let mut script = String::new();
    let mut node_layouts = Vec::new();
    for link in &topology.links {
        ensure!(
            link.endpoints.len() >= 2,
            "link {} needs at least two endpoints",
            link.name
        );
        for endpoint in &link.endpoints {
            ensure!(
                topology.nodes.iter().any(|node| node.name == endpoint.node),
                "link {} references unknown node {}",
                link.name,
                endpoint.node
            );
        }
        match link.kind {
            // Physical normal-mode CAN controllers do not receive their own
            // frames. Renode's CAN hub defaults to loopback, so disable it for
            // linked-machine traffic or relay nodes amplify a synthetic echo.
            LinkKind::Can => {
                script += &format!("emulation CreateCANHub \"{}\" false\n", safe(&link.name))
            }
            LinkKind::Uart => {
                script += &format!("emulation CreateUARTHub \"{}\"\n", safe(&link.name))
            }
        }
    }
    for node in &topology.nodes {
        let layout_path = base.join(&node.layout);
        let layout = BoardLayout::load(&layout_path)?;
        let root = base.join(&node.firmware_root);
        let elf = root.join(&layout.artifacts.elf);
        ensure!(
            elf.is_file(),
            "node {} ELF is missing: {}",
            node.name,
            elf.display()
        );
        Architecture::for_kind(layout.architecture).validate_mcu(layout.mcu(), &layout.memory)?;
        crate::elf::validate_elf(
            &elf,
            &layout.memory,
            &format!("node {} firmware", node.name),
        )?;
        let node_scratch = scratch.path().join(safe(&node.name));
        fs::create_dir_all(&node_scratch)?;
        let overlay = node_scratch.join("peripherals.repl");
        fs::write(
            &overlay,
            crate::execution::render_peripheral_overlay(&layout)?,
        )?;
        let configured_platform = crate::execution::materialize_platform(&layout, &node_scratch)?;
        let marker = format!("SEDS_NODE_BOOT_{}", safe(&node.name));
        script += &format!(
            "mach create \"{}\"\nmachine LoadPlatformDescription @{}\nmachine LoadPlatformDescription @{}\n{}sysbus LoadELF @{}\nphysicalFlash EndHostLoading\ncpu AddHook `sysbus GetSymbolAddress \"{}\"` 'self.InfoLog(\"{}\")'\n",
            safe(&node.name),
            configured_platform.display(),
            overlay.display(),
            if layout.board.strict_mmio { "sysbus UnhandledAccessBehaviour ThrowException\n" } else { "" },
            elf.display(),
            layout.execution.boot_success_symbol,
            marker
        );
        for link in &topology.links {
            for endpoint in link
                .endpoints
                .iter()
                .filter(|endpoint| endpoint.node == node.name)
            {
                script += &format!(
                    "connector Connect sysbus.{} {}\n",
                    safe(&endpoint.peripheral),
                    safe(&link.name)
                );
            }
        }
        node_layouts.push((node.name.clone(), layout));
    }
    script += &format!(
        "emulation SetGlobalQuantum \"{}\"\nemulation SetGlobalSerialExecution True\n",
        topology.quantum_seconds
    );
    let base_ms = topology.virtual_time_ms / topology.sample_count as u64;
    let remainder_ms = topology.virtual_time_ms % topology.sample_count as u64;
    for sample in 0..topology.sample_count {
        let duration_ms = base_ms + u64::from((sample as u64) < remainder_ms);
        script += &format!("emulation RunFor \"{}s\"\n", duration_ms as f64 / 1000.0);
        for (node_name, layout) in &node_layouts {
            script += &format!("mach set \"{}\"\n", safe(node_name));
            for probe in &layout.execution.memory_probes {
                script += &format!(
                    "echo \"SEDS_BAY_PROBE {} {} {}\"\nsysbus ReadDoubleWord `sysbus GetSymbolAddress \"{}\"`\n",
                    safe(node_name), probe.name, sample, probe.symbol
                );
            }
        }
    }
    for node in &topology.nodes {
        script += &format!(
            "mach set \"{}\"\necho \"SEDS_NODE {} PC\"\ncpu PC\n",
            safe(&node.name),
            safe(&node.name)
        );
    }
    script += "echo \"SEDS_BAY_COMPLETE\"\n";
    let script_path = scratch.path().join("bay.resc");
    fs::write(&script_path, script)?;
    let renode = env::var_os("RENODE")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/usr/bin/renode"));
    let output = Command::new(&renode)
        .args(["--disable-xwt", "--console", "--execute"])
        .arg(format!("include @{}; quit", script_path.display()))
        .output()?;
    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let lower = combined.to_ascii_lowercase();
    if !output.status.success()
        || !combined
            .lines()
            .any(|line| line.trim() == "SEDS_BAY_COMPLETE")
        || lower.contains("there was an error executing command")
        || lower.contains("parameters did not match")
    {
        bail!(
            "linked bay execution failed:\n{}",
            combined
                .lines()
                .rev()
                .take(100)
                .collect::<Vec<_>>()
                .into_iter()
                .rev()
                .collect::<Vec<_>>()
                .join("\n")
        );
    }
    for node in &topology.nodes {
        let marker = format!("SEDS_NODE_BOOT_{}", safe(&node.name));
        ensure!(
            observed_marker(&combined, &marker),
            "node {} did not reach its boot-success symbol",
            node.name
        );
    }
    let lines: Vec<_> = combined.lines().collect();
    let mut register_dump = Vec::new();
    for (index, line) in lines.iter().enumerate() {
        if line.trim().starts_with("SEDS_NODE ") {
            register_dump.push(line.trim().to_string());
            if let Some(value) = lines.get(index + 1) {
                register_dump.push(value.trim().to_string());
            }
        }
    }
    let mut memory_profiles = BTreeMap::new();
    for (node_name, layout) in &node_layouts {
        memory_profiles.insert(
            node_name.clone(),
            parse_memory_profiles(node_name, layout, &combined, topology.sample_count)?,
        );
    }
    for link in &topology.links {
        for endpoint in &link.endpoints {
            let Some(probe_name) = endpoint.activity_probe.as_deref() else {
                continue;
            };
            let reports = memory_profiles
                .get(&endpoint.node)
                .with_context(|| format!("missing profiles for node {}", endpoint.node))?;
            let report = reports
                .iter()
                .find(|report| report.name == probe_name)
                .with_context(|| {
                    format!(
                        "link {} endpoint {} requires unknown activity probe {}",
                        link.name, endpoint.node, probe_name
                    )
                })?;
            ensure!(
                report.maximum_observed >= endpoint.minimum_activity,
                "link {} endpoint {} observed no real firmware activity: probe {} maximum {} is below {}",
                link.name,
                endpoint.node,
                probe_name,
                report.maximum_observed,
                endpoint.minimum_activity
            );
        }
    }
    Ok(BayReport {
        bay: topology.name,
        nodes_executed: topology.nodes.len(),
        links_connected: topology.links.len(),
        virtual_time_ms: topology.virtual_time_ms,
        register_dump,
        memory_profiles,
    })
}

fn parse_memory_profiles(
    node_name: &str,
    layout: &BoardLayout,
    output: &str,
    sample_count: usize,
) -> Result<Vec<MemoryProbeReport>> {
    let mut reports = Vec::new();
    for probe in &layout.execution.memory_probes {
        let prefix = format!("SEDS_BAY_PROBE {} {} ", safe(node_name), probe.name);
        let mut indexed = Vec::new();
        let mut cursor = 0;
        while let Some(relative) = output[cursor..].find(&prefix) {
            let marker_start = cursor + relative;
            let sample_start = marker_start + prefix.len();
            let sample_end = output[sample_start..]
                .find(|c: char| !c.is_ascii_digit())
                .map(|offset| sample_start + offset)
                .unwrap_or(output.len());
            let sample: usize = output[sample_start..sample_end].parse()?;
            let next_marker = output[sample_end..]
                .find("SEDS_BAY_PROBE ")
                .map(|offset| sample_end + offset)
                .unwrap_or(output.len());
            let value = output[sample_end..next_marker]
                .split(['\r', '\n'])
                .map(str::trim)
                .find_map(|line| {
                    let hex = line.strip_prefix("0x")?;
                    (!hex.is_empty() && hex.chars().all(|c| c.is_ascii_hexdigit())).then_some(hex)
                })
                .context("bay memory probe value is missing")?;
            indexed.push((sample, u32::from_str_radix(value, 16)?));
            cursor = sample_end;
        }
        indexed.sort_unstable_by_key(|(sample, _)| *sample);
        ensure!(
            indexed.len() == sample_count,
            "node {node_name} probe {} returned {} of {sample_count} samples",
            probe.name,
            indexed.len()
        );
        let samples: Vec<u32> = indexed.into_iter().map(|(_, value)| value).collect();
        ensure!(
            layout.execution.memory_probe_warmup_samples < samples.len(),
            "node {node_name} memory_probe_warmup_samples must be less than sample_count"
        );
        let minimum_observed = *samples.iter().min().context("empty bay probe")?;
        let maximum_observed = *samples.iter().max().context("empty bay probe")?;
        let end_drop = i64::from(samples[layout.execution.memory_probe_warmup_samples])
            - i64::from(*samples.last().unwrap());
        if let Some(minimum) = probe.minimum {
            ensure!(
                minimum_observed >= minimum,
                "node {node_name} probe {} fell below {minimum}: {:?}",
                probe.name,
                samples
            );
        }
        if let Some(maximum) = probe.maximum {
            ensure!(
                maximum_observed <= maximum,
                "node {node_name} probe {} exceeded {maximum}: {:?}",
                probe.name,
                samples
            );
        }
        if let Some(max_end_drop) = probe.max_end_drop {
            ensure!(
                end_drop <= i64::from(max_end_drop),
                "node {node_name} probe {} lost {end_drop} bytes: {:?}",
                probe.name,
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
fn safe(value: &str) -> String {
    value
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '_' || c == '-' {
                c
            } else {
                '_'
            }
        })
        .collect()
}
fn observed_marker(output: &str, marker: &str) -> bool {
    output.lines().any(|line| {
        let line = line.trim();
        line == marker || (line.ends_with(marker) && line.contains("[INFO]"))
    })
}
fn default_quantum() -> f64 {
    0.0001
}
fn default_duration() -> u64 {
    1000
}
fn default_sample_count() -> usize {
    10
}
fn default_minimum_activity() -> u32 {
    1
}
