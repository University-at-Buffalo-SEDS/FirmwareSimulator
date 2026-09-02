use crate::{core::Architecture, execution::MemoryProbeReport, layout::BoardLayout};
use anyhow::{bail, ensure, Context, Result};
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    env, fs,
    io::{BufRead, BufReader},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    thread,
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
    /// Enforce each board layout's end-to-end memory-drop limit. Short linked
    /// functional runs can disable this because allocator availability is
    /// intentionally bursty; minimum and maximum bounds remain enforced.
    #[serde(default = "default_true")]
    pub enforce_end_drop: bool,
    pub nodes: Vec<Node>,
    #[serde(default)]
    pub host_nodes: Vec<HostNode>,
    #[serde(default)]
    pub links: Vec<Link>,
    #[serde(default)]
    pub assertions: Vec<NetworkAssertion>,
}
#[derive(Debug, Deserialize)]
pub struct Node {
    pub name: String,
    pub layout: PathBuf,
    pub firmware_root: PathBuf,
}
#[derive(Debug, Deserialize)]
pub struct HostNode {
    pub name: String,
    pub binary: PathBuf,
    #[serde(default)]
    pub cwd: Option<PathBuf>,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    pub serial_links: Vec<HostSerialLink>,
}
#[derive(Debug, Deserialize)]
pub struct HostSerialLink {
    pub link: String,
    pub env: String,
}
#[derive(Debug, Deserialize)]
pub struct Link {
    pub name: String,
    pub kind: LinkKind,
    pub endpoints: Vec<Endpoint>,
    /// Human-readable physical stages represented by this deterministic link.
    /// For example: RF radio, ground-station router, and the Pico-Fi tunnel.
    #[serde(default)]
    pub transport_path: Vec<String>,
}
#[derive(Clone, Copy, Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LinkKind {
    Can,
    Uart,
    Radio,
    PicoFi,
    RoutedSerial,
}
#[derive(Debug, Deserialize)]
pub struct Endpoint {
    pub node: String,
    pub peripheral: String,
    #[serde(default)]
    pub activity_probe: Option<String>,
    #[serde(default = "default_minimum_activity")]
    pub minimum_activity: u32,
    #[serde(default)]
    pub tx_probe: Option<String>,
    #[serde(default)]
    pub rx_probe: Option<String>,
    #[serde(default = "default_minimum_activity")]
    pub minimum_tx: u32,
    #[serde(default = "default_minimum_activity")]
    pub minimum_rx: u32,
}
#[derive(Debug, Deserialize)]
pub struct NetworkAssertion {
    pub name: String,
    pub node: String,
    pub probe: String,
    #[serde(default)]
    pub minimum: Option<u32>,
    #[serde(default)]
    pub maximum: Option<u32>,
    #[serde(default)]
    pub required_bits: Option<u32>,
}
#[derive(Debug, Serialize)]
pub struct LinkReport {
    pub name: String,
    pub kind: String,
    pub transport_path: Vec<String>,
    pub endpoints: Vec<EndpointReport>,
}
#[derive(Debug, Serialize)]
pub struct EndpointReport {
    pub node: String,
    pub tx_probe: Option<String>,
    pub tx_observed: Option<u32>,
    pub rx_probe: Option<String>,
    pub rx_observed: Option<u32>,
}
#[derive(Debug, Serialize)]
pub struct AssertionReport {
    pub name: String,
    pub node: String,
    pub probe: String,
    pub observed: u32,
}
#[derive(Debug, Serialize)]
pub struct BayReport {
    pub bay: String,
    pub nodes_executed: usize,
    pub host_nodes_executed: usize,
    pub links_connected: usize,
    pub virtual_time_ms: u64,
    pub register_dump: Vec<String>,
    pub memory_profiles: BTreeMap<String, Vec<MemoryProbeReport>>,
    pub link_reports: Vec<LinkReport>,
    pub assertion_reports: Vec<AssertionReport>,
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
        topology.sample_count > 0,
        "bay sample_count must be nonzero"
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
    for host in &topology.host_nodes {
        ensure!(
            !topology.nodes.iter().any(|node| node.name == host.name),
            "host node {} duplicates a firmware node",
            host.name
        );
        ensure!(
            host.binary.is_file(),
            "host node {} binary is missing: {}",
            host.name,
            host.binary.display()
        );
    }
    let base = topology_path.parent().unwrap_or_else(|| Path::new("."));
    let scratch = tempfile::tempdir()?;
    let mut script = String::new();
    let mut node_layouts = Vec::new();
    let mut host_serial_paths = BTreeMap::new();
    for link in &topology.links {
        ensure!(
            link.endpoints.len() >= 2,
            "link {} needs at least two endpoints",
            link.name
        );
        for endpoint in &link.endpoints {
            ensure!(
                topology.nodes.iter().any(|node| node.name == endpoint.node)
                    || topology
                        .host_nodes
                        .iter()
                        .any(|node| node.name == endpoint.node),
                "link {} references unknown node {}",
                link.name,
                endpoint.node
            );
        }
        let host_endpoints: Vec<_> = link
            .endpoints
            .iter()
            .filter(|endpoint| {
                topology
                    .host_nodes
                    .iter()
                    .any(|host| host.name == endpoint.node)
            })
            .collect();
        ensure!(
            host_endpoints.len() <= 1,
            "link {} has more than one host endpoint",
            link.name
        );
        if let Some(host_endpoint) = host_endpoints.first() {
            ensure!(
                !matches!(link.kind, LinkKind::Can),
                "host endpoint {} on {} must use a serial link",
                host_endpoint.node,
                link.name
            );
            let path = scratch.path().join(format!("{}.pty", safe(&link.name)));
            script += &format!(
                "emulation CreateUartPtyTerminal \"{}\" \"{}\"\n",
                safe(&link.name),
                path.display()
            );
            host_serial_paths.insert(link.name.clone(), path);
            continue;
        }
        match link.kind {
            // Physical normal-mode CAN controllers do not receive their own
            // frames. Renode's CAN hub defaults to loopback, so disable it for
            // linked-machine traffic or relay nodes amplify a synthetic echo.
            LinkKind::Can => {
                script += &format!("emulation CreateCANHub \"{}\" false\n", safe(&link.name))
            }
            LinkKind::Uart | LinkKind::Radio | LinkKind::PicoFi | LinkKind::RoutedSerial => {
                script += &format!("emulation CreateUARTHub \"{}\"\n", safe(&link.name))
            }
        }
    }
    for host in &topology.host_nodes {
        for serial in &host.serial_links {
            ensure!(
                host_serial_paths.contains_key(&serial.link),
                "host node {} references unknown serial link {}",
                host.name,
                serial.link
            );
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
        Architecture::for_kind(layout.architecture)
            .validate_mcu(layout.resolve_mcu_descriptor()?, &layout.memory)?;
        crate::elf::validate_elf(
            &elf,
            &layout.memory,
            &format!("node {} firmware", node.name),
        )?;
        let (firmware_msp, firmware_pc, firmware_vtor) = crate::elf::reset_vector(
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
        let configured_platform =
            crate::execution::materialize_platform(&layout, &root, &node_scratch)?;
        let marker = format!("SEDS_NODE_BOOT_{}", safe(&node.name));
        script += &format!(
            "mach create \"{}\"\nmachine LoadPlatformDescription @{}\nmachine LoadPlatformDescription @{}\n{}sysbus LoadELF @{}\nphysicalFlash EndHostLoading\ncpu SetRegister 13 0x{:08x}\ncpu PC 0x{:08x}\ncpu VectorTableOffset 0x{:08x}\ncpu AddHook `sysbus GetSymbolAddress \"{}\"` 'self.InfoLog(\"{}\")'\n",
            safe(&node.name),
            configured_platform.display(),
            overlay.display(),
            if layout.board.strict_mmio { "sysbus UnhandledAccessBehaviour ThrowException\n" } else { "" },
            elf.display(),
            firmware_msp,
            firmware_pc,
            firmware_vtor,
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
    if !topology.host_nodes.is_empty() {
        script += "echo \"SEDS_HOST_ENDPOINTS_READY\"\nsleep 5\n";
    }
    /* Firmware nodes are independent MCUs and must advance concurrently.
     * Globally serial execution can deadlock a sender inside a bounded HAL
     * FIFO wait because the receiving machine/CAN hub never gets a scheduling
     * turn. A fixed global quantum keeps the run repeatable while matching the
     * physical bay's concurrent execution. */
    script += &format!(
        "emulation SetGlobalQuantum \"{}\"\nemulation SetGlobalSerialExecution False\n",
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
        script += &format!(
            "echo \"SEDS_BAY_SAMPLE_DONE {} {}\"\n",
            sample + 1,
            topology.sample_count
        );
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
    println!(
        "[SIM] starting {} nodes across {} physical links for {} ms",
        topology.nodes.len(),
        topology.links.len(),
        topology.virtual_time_ms
    );
    let mut child = Command::new(&renode)
        .args(["--disable-xwt", "--console", "--execute"])
        .arg(format!("include @{}; quit", script_path.display()))
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .context("starting Renode")?;
    let mut host_children = Vec::new();
    if !topology.host_nodes.is_empty() {
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(4);
        while host_serial_paths.values().any(|path| !path.exists())
            && std::time::Instant::now() < deadline
        {
            std::thread::sleep(std::time::Duration::from_millis(25));
        }
        for path in host_serial_paths.values() {
            ensure!(
                path.exists(),
                "Renode did not create host UART PTY {}",
                path.display()
            );
            // Renode can create the PTY endpoint with a restrictive mode even
            // when the host process runs as the same unprivileged container
            // user. Make the simulated cable explicitly readable/writable.
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                fs::set_permissions(path, fs::Permissions::from_mode(0o660))
                    .with_context(|| format!("making host UART PTY usable: {}", path.display()))?;
            }
        }
        for host in &topology.host_nodes {
            let mut command = Command::new(&host.binary);
            command.args(&host.args).envs(&host.env);
            if let Some(cwd) = &host.cwd {
                command.current_dir(cwd);
            }
            for serial in &host.serial_links {
                command.env(&serial.env, &host_serial_paths[&serial.link]);
            }
            command.env(
                "GS_NETWORK_VARIABLE_CACHE",
                scratch
                    .path()
                    .join(format!("{}-network-variables.json", safe(&host.name))),
            );
            let stdout_path = scratch
                .path()
                .join(format!("{}-stdout.log", safe(&host.name)));
            let stderr_path = scratch
                .path()
                .join(format!("{}-stderr.log", safe(&host.name)));
            let child = command
                .stdout(Stdio::from(fs::File::create(&stdout_path)?))
                .stderr(Stdio::from(fs::File::create(&stderr_path)?))
                .spawn()
                .with_context(|| format!("starting host node {}", host.name))?;
            println!("[SIM] host node {} started", host.name);
            host_children.push((host.name.clone(), child, stdout_path, stderr_path));
        }
    }
    let stdout = child.stdout.take().context("capturing Renode stdout")?;
    let stderr = child.stderr.take().context("capturing Renode stderr")?;
    let stdout_reader = thread::spawn(move || -> Result<String> {
        let mut captured = String::new();
        for line in BufReader::new(stdout).lines() {
            let line = line?;
            if let Some(progress) = line.trim().strip_prefix("SEDS_BAY_SAMPLE_DONE ") {
                println!("[SIM] network sample {progress} complete");
            }
            captured.push_str(&line);
            captured.push('\n');
        }
        Ok(captured)
    });
    let stderr_reader = thread::spawn(move || -> Result<String> {
        let mut captured = String::new();
        for line in BufReader::new(stderr).lines() {
            let line = line?;
            captured.push_str(&line);
            captured.push('\n');
        }
        Ok(captured)
    });
    let status = child.wait()?;
    let mut host_failures = Vec::new();
    let mut host_diagnostics = BTreeMap::new();
    for (name, mut host, stdout_path, stderr_path) in host_children {
        if let Some(status) = host.try_wait()? {
            let stdout = fs::read_to_string(&stdout_path).unwrap_or_default();
            let stderr = fs::read_to_string(&stderr_path).unwrap_or_default();
            host_failures.push(format!(
                "host node {name} exited early with {status}; verify its serial-port and layout configuration\n{}",
                [stdout, stderr]
                    .concat()
                    .lines()
                    .rev()
                    .take(30)
                    .collect::<Vec<_>>()
                    .into_iter()
                    .rev()
                    .collect::<Vec<_>>()
                    .join("\n")
            ));
        } else {
            let _ = host.kill();
            let _ = host.wait();
        }
        let stdout = fs::read_to_string(&stdout_path).unwrap_or_default();
        let stderr = fs::read_to_string(&stderr_path).unwrap_or_default();
        let combined_host = [stdout, stderr].concat();
        let lines = combined_host.lines().collect::<Vec<_>>();
        let diagnostic = if lines.len() <= 120 {
            lines.join("\n")
        } else {
            format!(
                "{}\n... {} host log lines omitted ...\n{}",
                lines[..60].join("\n"),
                lines.len() - 120,
                lines[lines.len() - 60..].join("\n")
            )
        };
        host_diagnostics.insert(name, diagnostic);
    }
    ensure!(host_failures.is_empty(), "{}", host_failures.join("; "));
    let stdout = stdout_reader
        .join()
        .map_err(|_| anyhow::anyhow!("Renode stdout reader panicked"))??;
    let stderr = stderr_reader
        .join()
        .map_err(|_| anyhow::anyhow!("Renode stderr reader panicked"))??;
    let combined = format!("{stdout}{stderr}");
    let lower = combined.to_ascii_lowercase();
    if !status.success()
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
            parse_memory_profiles(
                node_name,
                layout,
                &combined,
                topology.sample_count,
                topology.enforce_end_drop,
            )?,
        );
    }
    let mut link_reports = Vec::new();
    let mut link_failures = Vec::new();
    for link in &topology.links {
        let mut endpoint_reports = Vec::new();
        for endpoint in &link.endpoints {
            if topology
                .host_nodes
                .iter()
                .any(|host| host.name == endpoint.node)
            {
                endpoint_reports.push(EndpointReport {
                    node: endpoint.node.clone(),
                    tx_probe: None,
                    tx_observed: None,
                    rx_probe: None,
                    rx_observed: None,
                });
                continue;
            }
            let reports = memory_profiles
                .get(&endpoint.node)
                .with_context(|| format!("missing profiles for node {}", endpoint.node))?;
            let tx_observed = observe_endpoint_probe(
                reports,
                endpoint.tx_probe.as_deref(),
                &link.name,
                &endpoint.node,
                "TX",
            )?;
            let rx_observed = observe_endpoint_probe(
                reports,
                endpoint.rx_probe.as_deref(),
                &link.name,
                &endpoint.node,
                "RX",
            )?;
            if let (Some(probe), Some(observed)) = (&endpoint.tx_probe, tx_observed) {
                if observed < endpoint.minimum_tx {
                    link_failures.push(format!(
                        "{} / {} TX: {}={} (required >= {})",
                        link.name, endpoint.node, probe, observed, endpoint.minimum_tx
                    ));
                }
            }
            if let (Some(probe), Some(observed)) = (&endpoint.rx_probe, rx_observed) {
                if observed < endpoint.minimum_rx {
                    link_failures.push(format!(
                        "{} / {} RX: {}={} (required >= {})",
                        link.name, endpoint.node, probe, observed, endpoint.minimum_rx
                    ));
                }
            }
            if endpoint.tx_probe.is_none() && endpoint.rx_probe.is_none() {
                let activity_observed = observe_endpoint_probe(
                    reports,
                    endpoint.activity_probe.as_deref(),
                    &link.name,
                    &endpoint.node,
                    "activity",
                )?;
                if let (Some(probe), Some(observed)) = (&endpoint.activity_probe, activity_observed)
                {
                    if observed < endpoint.minimum_activity {
                        link_failures.push(format!(
                            "{} / {} activity: {}={} (required >= {})",
                            link.name, endpoint.node, probe, observed, endpoint.minimum_activity
                        ));
                    }
                }
            }
            ensure!(
                endpoint.tx_probe.is_some() && endpoint.rx_probe.is_some()
                    || endpoint.activity_probe.is_some(),
                "link {} endpoint {} has no TX/RX probes; bidirectional communication cannot be certified",
                link.name,
                endpoint.node
            );
            endpoint_reports.push(EndpointReport {
                node: endpoint.node.clone(),
                tx_probe: endpoint.tx_probe.clone(),
                tx_observed,
                rx_probe: endpoint.rx_probe.clone(),
                rx_observed,
            });
        }
        link_reports.push(LinkReport {
            name: link.name.clone(),
            kind: format!("{:?}", link.kind),
            transport_path: link.transport_path.clone(),
            endpoints: endpoint_reports,
        });
    }
    if !link_failures.is_empty() {
        let diagnostic_probe_names = [
            "heartbeat_attempts",
            "heartbeat_ok",
            "heartbeat_fail",
            "network_ready",
            "peer_mask",
            "fdcan_tx_ok",
            "fdcan_tx_fail",
            "fdcan_rx",
            "fdcan_last_error",
            "fdcan_last_state",
            "queue_errors",
            "discovery_poll_errors",
            "timesync_poll_errors",
            "startup_fault_stage",
            "telemetry_thread_entered",
            "telemetry_service_stage",
            "alloc_failures",
            "panics",
        ];
        let mut diagnostics = Vec::new();
        for (node, reports) in &memory_profiles {
            let values: Vec<String> = diagnostic_probe_names
                .iter()
                .filter_map(|name| {
                    reports
                        .iter()
                        .find(|report| report.name == *name)
                        .map(|report| format!("{name}={}", report.maximum_observed))
                })
                .collect();
            diagnostics.push(format!("{node}: {}", values.join(", ")));
        }
        bail!(
            "{} network endpoint check(s) failed:\n- {}\n\nEndpoint diagnostics:\n{}",
            link_failures.len(),
            link_failures.join("\n- "),
            diagnostics.join("\n")
        );
    }
    let mut assertion_reports = Vec::new();
    let mut assertion_failures = Vec::new();
    for assertion in &topology.assertions {
        let reports = memory_profiles.get(&assertion.node).with_context(|| {
            format!(
                "assertion {} references unknown node {}",
                assertion.name, assertion.node
            )
        })?;
        let report = reports
            .iter()
            .find(|report| report.name == assertion.probe)
            .with_context(|| {
                format!(
                    "assertion {} references unknown probe {} on {}",
                    assertion.name, assertion.probe, assertion.node
                )
            })?;
        let observed = report.maximum_observed;
        if let Some(minimum) = assertion.minimum {
            if observed < minimum {
                assertion_failures.push(format!(
                    "{}: {}.{} maximum {} is below {}",
                    assertion.name, assertion.node, assertion.probe, observed, minimum
                ));
            }
        }
        if let Some(maximum) = assertion.maximum {
            if observed > maximum {
                assertion_failures.push(format!(
                    "{}: {}.{} maximum {} exceeds {}",
                    assertion.name, assertion.node, assertion.probe, observed, maximum
                ));
            }
        }
        if let Some(required_bits) = assertion.required_bits {
            if observed & required_bits != required_bits {
                assertion_failures.push(format!(
                    "{}: {}.{} observed 0x{observed:08x}, requires 0x{required_bits:08x}",
                    assertion.name, assertion.node, assertion.probe
                ));
            }
        }
        assertion_reports.push(AssertionReport {
            name: assertion.name.clone(),
            node: assertion.node.clone(),
            probe: assertion.probe.clone(),
            observed,
        });
    }
    if !assertion_failures.is_empty() {
        let host_detail = host_diagnostics
            .iter()
            .map(|(name, output)| format!("{name}:\n{output}"))
            .collect::<Vec<_>>()
            .join("\n");
        bail!(
            "{} network assertion(s) failed:\n- {}\n\nHost diagnostics:\n{}",
            assertion_failures.len(),
            assertion_failures.join("\n- "),
            host_detail
        );
    }
    Ok(BayReport {
        bay: topology.name,
        nodes_executed: topology.nodes.len(),
        host_nodes_executed: topology.host_nodes.len(),
        links_connected: topology.links.len(),
        virtual_time_ms: topology.virtual_time_ms,
        register_dump,
        memory_profiles,
        link_reports,
        assertion_reports,
    })
}

fn observe_endpoint_probe(
    reports: &[MemoryProbeReport],
    probe_name: Option<&str>,
    link_name: &str,
    node_name: &str,
    direction: &str,
) -> Result<Option<u32>> {
    let Some(probe_name) = probe_name else {
        return Ok(None);
    };
    let report = reports
        .iter()
        .find(|report| report.name == probe_name)
        .with_context(|| format!("link {link_name} endpoint {node_name} requires unknown {direction} probe {probe_name}"))?;
    Ok(Some(report.maximum_observed))
}

fn parse_memory_profiles(
    node_name: &str,
    layout: &BoardLayout,
    output: &str,
    sample_count: usize,
    enforce_end_drop: bool,
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
        if enforce_end_drop {
            if let Some(max_end_drop) = probe.max_end_drop {
                ensure!(
                    end_drop <= i64::from(max_end_drop),
                    "node {node_name} probe {} lost {end_drop} bytes: {:?}",
                    probe.name,
                    samples
                );
            }
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

fn default_true() -> bool {
    true
}
fn default_minimum_activity() -> u32 {
    1
}
