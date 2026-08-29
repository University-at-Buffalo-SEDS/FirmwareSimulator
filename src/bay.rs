use crate::{
    core::{Architecture, ArchitectureKind},
    layout::BoardLayout,
};
use anyhow::{bail, ensure, Context, Result};
use serde::{Deserialize, Serialize};
use std::{
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
}
#[derive(Debug, Serialize)]
pub struct BayReport {
    pub bay: String,
    pub nodes_executed: usize,
    pub links_connected: usize,
    pub virtual_time_ms: u64,
    pub register_dump: Vec<String>,
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
        let command = match link.kind {
            LinkKind::Can => "CreateCANHub",
            LinkKind::Uart => "CreateUARTHub",
        };
        script += &format!("emulation {} \"{}\"\n", command, safe(&link.name));
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
        Architecture::for_kind(layout.architecture).validate(&layout.memory)?;
        crate::elf::validate_elf(
            &elf,
            &layout.memory,
            &format!("node {} firmware", node.name),
        )?;
        let overlay = scratch.path().join(format!("{}.repl", safe(&node.name)));
        fs::write(
            &overlay,
            crate::execution::render_peripheral_overlay(&layout)?,
        )?;
        let marker = format!("SEDS_NODE_BOOT_{}", safe(&node.name));
        script += &format!(
            "mach create \"{}\"\nmachine LoadPlatformDescription @{}\nmachine LoadPlatformDescription @{}\nsysbus LoadELF @{}\ncpu AddHook `sysbus GetSymbolAddress \"{}\"` 'self.InfoLog(\"{}\")'\n",
            safe(&node.name),
            platform(layout.architecture).display(),
            overlay.display(),
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
    }
    script += &format!(
        "emulation SetGlobalQuantum \"{}\"\nemulation RunFor \"{}s\"\n",
        topology.quantum_seconds,
        topology.virtual_time_ms as f64 / 1000.0
    );
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
        || lower.contains("[error]")
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
    Ok(BayReport {
        bay: topology.name,
        nodes_executed: topology.nodes.len(),
        links_connected: topology.links.len(),
        virtual_time_ms: topology.virtual_time_ms,
        register_dump,
    })
}
fn platform(kind: ArchitectureKind) -> PathBuf {
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
