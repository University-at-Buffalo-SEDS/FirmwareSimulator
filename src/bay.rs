use crate::{core::Architecture, execution::MemoryProbeReport, layout::BoardLayout};
use anyhow::{bail, ensure, Context, Result};
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, VecDeque},
    env, fs,
    io::{BufRead, BufReader, Read, Write},
    os::fd::AsRawFd,
    os::unix::net::UnixListener,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    thread,
    time::Duration,
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
    #[serde(default)]
    pub host_log_assertions: Vec<HostLogAssertion>,
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
    /// Optional persistent cache path shared across separate bay invocations.
    /// Relative paths resolve beside the topology; absolute paths are useful
    /// for a writable Docker volume such as /state.
    #[serde(default)]
    pub network_variable_cache: Option<PathBuf>,
    pub serial_links: Vec<HostSerialLink>,
}
#[derive(Debug, Deserialize)]
pub struct HostSerialLink {
    pub link: String,
    pub env: String,
    #[serde(default)]
    pub transport: HostLinkTransport,
}
#[derive(Clone, Copy, Debug, Default, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum HostLinkTransport {
    #[default]
    UartPty,
    /// Linux I2C transactions on the host side, translated by a Pico-Fi pair
    /// to framed UART bytes on the firmware side.
    PicoFiI2cToUart,
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
    /// Check one zero-based sample rather than the maximum across the run.
    #[serde(default)]
    pub sample: Option<usize>,
}
#[derive(Debug, Deserialize)]
pub struct HostLogAssertion {
    pub name: String,
    pub node: String,
    pub contains: String,
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

const PICO_I2C_SLOT_SIZE: usize = 32;
const PICO_I2C_HEADER_SIZE: usize = 18;
const PICO_I2C_PAYLOAD_SIZE: usize = PICO_I2C_SLOT_SIZE - PICO_I2C_HEADER_SIZE;
const PICO_I2C_MAGIC: [u8; 2] = [0x49, 0x32];
const PICO_I2C_VERSION: u8 = 1;
const PICO_I2C_DATA: u8 = 1;
const PICO_I2C_START: u8 = 1;
const PICO_I2C_END: u8 = 2;
const PICO_I2C_PACKET_MAX: usize = 4_096;
const PICO_UART_MAX_FRAME: usize = PICO_I2C_PACKET_MAX + 4;
const PICO_PACKET_QUEUE_DEPTH: usize = 8;
const PICO_PACKET_QUEUE_BYTES: usize = 8_192;
const PICO_UART_BAUD: u64 = 115_200;
const PICO_UART_BITS_PER_BYTE: u64 = 10;
const PICO_UART_EMULATION_PACING_SCALE: u64 = 12;

#[derive(Default)]
struct PicoBridgeState {
    host_assembly: Option<(u16, usize, Vec<u8>)>,
    uart_rx: Vec<u8>,
    uart_tx_packets: VecDeque<Vec<u8>>,
    uart_tx_offset: usize,
    uart_tx_bytes: usize,
    host_reads: VecDeque<[u8; PICO_I2C_SLOT_SIZE]>,
    next_transfer_id: u16,
    i2c_to_uart_frames: u32,
    uart_to_i2c_frames: u32,
}

fn run_pico_fi_bridge(listener: UnixListener, uart_path: &Path) -> Result<()> {
    let mut uart = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(uart_path)
        .with_context(|| format!("opening Pico-Fi Gateway UART {}", uart_path.display()))?;
    configure_raw_nonblocking(uart.as_raw_fd())?;
    let (mut host, _) = listener
        .accept()
        .context("accepting GroundStation I2C endpoint")?;
    let mut state = PicoBridgeState {
        next_transfer_id: 1,
        ..Default::default()
    };
    loop {
        drain_gateway_uart_tx(&mut state, &mut uart)?;
        let mut operation = [0u8; 1];
        match host.read_exact(&mut operation) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::UnexpectedEof => return Ok(()),
            Err(error) => return Err(error).context("reading simulated I2C operation"),
        }
        match operation[0] {
            b'W' => {
                let mut slot = [0u8; PICO_I2C_SLOT_SIZE];
                host.read_exact(&mut slot)?;
                match ingest_host_i2c_slot(&mut state, &slot) {
                    Ok(()) => host.write_all(&[0])?,
                    Err(error) => {
                        eprintln!("[SIM] Pico-Fi rejected malformed I2C slot: {error:#}");
                        host.write_all(&[1])?;
                    }
                }
            }
            b'R' => {
                read_gateway_uart(&mut state, &mut uart)?;
                let slot = state
                    .host_reads
                    .pop_front()
                    .unwrap_or([0; PICO_I2C_SLOT_SIZE]);
                host.write_all(&slot)?;
            }
            other => bail!("unknown simulated I2C operation 0x{other:02x}"),
        }
        drain_gateway_uart_tx(&mut state, &mut uart)?;
        host.flush()?;
    }
}

fn configure_raw_nonblocking(fd: std::os::fd::RawFd) -> Result<()> {
    let mut termios = std::mem::MaybeUninit::<libc::termios>::uninit();
    ensure!(
        unsafe { libc::tcgetattr(fd, termios.as_mut_ptr()) } == 0,
        "tcgetattr failed: {}",
        std::io::Error::last_os_error()
    );
    let mut termios = unsafe { termios.assume_init() };
    unsafe { libc::cfmakeraw(&mut termios) };
    ensure!(
        unsafe { libc::tcsetattr(fd, libc::TCSANOW, &termios) } == 0,
        "tcsetattr failed: {}",
        std::io::Error::last_os_error()
    );
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    ensure!(
        flags >= 0,
        "fcntl(F_GETFL) failed: {}",
        std::io::Error::last_os_error()
    );
    ensure!(
        unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } == 0,
        "fcntl(F_SETFL) failed: {}",
        std::io::Error::last_os_error()
    );
    Ok(())
}

fn ingest_host_i2c_slot(
    state: &mut PicoBridgeState,
    slot: &[u8; PICO_I2C_SLOT_SIZE],
) -> Result<()> {
    ensure!(
        slot[..2] == PICO_I2C_MAGIC,
        "invalid Pico-Fi I2C slot magic"
    );
    ensure!(
        slot[2] == PICO_I2C_VERSION,
        "unsupported Pico-Fi I2C slot version"
    );
    ensure!(
        slot[3] == PICO_I2C_DATA,
        "unsupported Pico-Fi I2C slot kind"
    );
    let flags = slot[4];
    let offset = u32::from_le_bytes(slot[6..10].try_into().unwrap()) as usize;
    let total = u32::from_le_bytes(slot[10..14].try_into().unwrap()) as usize;
    let length = u16::from_le_bytes(slot[14..16].try_into().unwrap()) as usize;
    let transfer_id = u16::from_le_bytes(slot[16..18].try_into().unwrap());
    ensure!(
        length <= PICO_I2C_PAYLOAD_SIZE,
        "oversized Pico-Fi I2C slot"
    );
    ensure!(total <= PICO_I2C_PACKET_MAX, "oversized Pico-Fi transfer");
    if flags & PICO_I2C_START != 0 {
        ensure!(offset == 0, "Pico-Fi transfer starts at a nonzero offset");
        state.host_assembly = Some((transfer_id, total, Vec::with_capacity(total)));
    }
    let (active_id, active_total, payload) = state
        .host_assembly
        .as_mut()
        .context("Pico-Fi continuation without START")?;
    ensure!(*active_id == transfer_id, "Pico-Fi transfer id changed");
    ensure!(*active_total == total, "Pico-Fi transfer length changed");
    ensure!(
        payload.len() == offset,
        "Pico-Fi transfer offset is discontinuous"
    );
    payload.extend_from_slice(&slot[PICO_I2C_HEADER_SIZE..PICO_I2C_HEADER_SIZE + length]);
    ensure!(
        payload.len() <= total,
        "Pico-Fi transfer exceeded declared length"
    );
    if flags & PICO_I2C_END != 0 {
        ensure!(payload.len() == total, "Pico-Fi transfer ended early");
        let (_, _, frame) = state.host_assembly.take().unwrap();
        // The mailbox logical packet contains its own A5/5A envelope. The
        // I2C Pico removes it before forwarding raw bytes over the network;
        // the UART Pico adds a new, independent envelope for the Gateway.
        let gateway_frame = i2c_request_to_gateway_uart(&frame)?;
        let gateway_payload_len = gateway_frame.len().saturating_sub(4);
        enqueue_gateway_uart_packet(state, gateway_frame);
        state.i2c_to_uart_frames += 1;
        if state.i2c_to_uart_frames <= 5 || state.i2c_to_uart_frames % 25 == 0 {
            eprintln!(
                "[SIM] Pico-Fi I2C->UART frame {} translated ({} payload bytes, head {:02x?})",
                state.i2c_to_uart_frames,
                gateway_payload_len,
                &frame[4..frame.len().min(16)]
            );
        }
        // The physical write acknowledges queue admission. Pico-Fi does not
        // add an empty mailbox packet, because that packet would compete with
        // real inbound telemetry in its bounded overwrite queue.
    }
    Ok(())
}

fn read_gateway_uart(state: &mut PicoBridgeState, uart: &mut fs::File) -> Result<()> {
    let mut scratch = [0u8; 1024];
    loop {
        match uart.read(&mut scratch) {
            Ok(0) => break,
            Ok(length) => state.uart_rx.extend_from_slice(&scratch[..length]),
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                ) =>
            {
                break
            }
            Err(error) => return Err(error).context("reading Gateway UART in Pico-Fi bridge"),
        }
    }
    loop {
        let Some(sync) = state.uart_rx.windows(2).position(|bytes| {
            matches!(
                bytes,
                [0xA5, 0x5A] | [0x5A, 0xA5] | [0xA6, 0x5B] | [0x5B, 0xA6] | [0xA7, 0x7A]
            )
        }) else {
            state.uart_rx.clear();
            break;
        };
        if sync > 0 {
            state.uart_rx.drain(..sync);
        }
        let Some(frame_length) = complete_uart_frame_length(&state.uart_rx) else {
            break;
        };
        ensure!(
            frame_length <= PICO_UART_MAX_FRAME,
            "Gateway UART frame is oversized"
        );
        if state.uart_rx.len() < frame_length {
            break;
        }
        let frame: Vec<u8> = state.uart_rx.drain(..frame_length).collect();
        // UART-side commands are consumed by that Pico and never cross the
        // network bridge. Only DATA frames become I2C mailbox responses.
        if let Some(host_frame) = gateway_uart_to_i2c_response(&frame) {
            enqueue_host_i2c_slots(state, &host_frame);
            state.uart_to_i2c_frames += 1;
            if state.uart_to_i2c_frames <= 5 {
                let preview_len = host_frame.len().min(12);
                eprintln!(
                    "[SIM] Pico-Fi UART->I2C frame {} translated ({} payload bytes, head {:02x?})",
                    state.uart_to_i2c_frames,
                    host_frame.len(),
                    &host_frame[..preview_len]
                );
            }
        }
    }
    Ok(())
}

fn complete_uart_frame_length(frame: &[u8]) -> Option<usize> {
    if frame.len() < 4 {
        return None;
    }
    Some(4 + u16::from_le_bytes([frame[2], frame[3]]) as usize)
}

fn i2c_request_to_gateway_uart(frame: &[u8]) -> Result<Vec<u8>> {
    ensure!(frame.len() >= 4, "I2C DATA frame is missing its envelope");
    ensure!(
        matches!(&frame[..2], [0xA5, 0x5A] | [0x5A, 0xA5]),
        "invalid I2C DATA frame sync"
    );
    let payload_len = u16::from_le_bytes([frame[2], frame[3]]) as usize;
    ensure!(
        payload_len + 4 == frame.len(),
        "invalid I2C DATA frame length"
    );
    ensure!(payload_len <= u16::MAX as usize, "I2C payload is oversized");
    let payload = &frame[4..];
    let mut gateway_frame = Vec::with_capacity(frame.len());
    gateway_frame.extend_from_slice(&[0xA5, 0x5A]);
    gateway_frame.extend_from_slice(&(payload_len as u16).to_le_bytes());
    gateway_frame.extend_from_slice(payload);
    Ok(gateway_frame)
}

fn gateway_uart_to_i2c_response(frame: &[u8]) -> Option<Vec<u8>> {
    if complete_uart_frame_length(frame) != Some(frame.len())
        || !matches!(&frame[..2], [0xA5, 0x5A] | [0x5A, 0xA5])
    {
        return None;
    }
    // The UART Pico removes this envelope for its network hop; the I2C Pico
    // creates a fresh envelope before staging the mailbox response.
    Some(frame.to_vec())
}

fn enqueue_gateway_uart_packet(state: &mut PicoBridgeState, packet: Vec<u8>) {
    while state.uart_tx_packets.len() >= PICO_PACKET_QUEUE_DEPTH
        || state.uart_tx_bytes + packet.len() > PICO_PACKET_QUEUE_BYTES
    {
        let Some(dropped) = state.uart_tx_packets.pop_front() else {
            break;
        };
        state.uart_tx_bytes = state.uart_tx_bytes.saturating_sub(dropped.len());
        state.uart_tx_offset = 0;
    }
    state.uart_tx_bytes += packet.len();
    state.uart_tx_packets.push_back(packet);
}

fn drain_gateway_uart_tx(state: &mut PicoBridgeState, uart: &mut fs::File) -> Result<()> {
    drain_gateway_uart_tx_paced(state, uart, |delay| thread::sleep(delay))
}

fn drain_gateway_uart_tx_paced(
    state: &mut PicoBridgeState,
    uart: &mut impl Write,
    mut wait: impl FnMut(Duration),
) -> Result<()> {
    let byte_time = Duration::from_nanos(
        1_000_000_000u64 * PICO_UART_BITS_PER_BYTE * PICO_UART_EMULATION_PACING_SCALE
            / PICO_UART_BAUD,
    );
    loop {
        let Some(packet) = state.uart_tx_packets.front() else {
            return Ok(());
        };
        // A PTY accepts the entire frame immediately, unlike the physical
        // Pico-Fi UART. Renode advances virtual CPU time more slowly than wall
        // time in a seven-node bay, so scale the physical 8N1 byte interval to
        // preserve its interrupt cadence in virtual time instead of queuing a
        // host burst ahead of the emulated receiver.
        match uart.write(&packet[state.uart_tx_offset..state.uart_tx_offset + 1]) {
            Ok(0) => bail!("Gateway UART closed during Pico-Fi transfer"),
            Ok(length) => {
                state.uart_tx_offset += length;
                if state.uart_tx_offset == packet.len() {
                    let packet = state.uart_tx_packets.pop_front().unwrap();
                    state.uart_tx_bytes = state.uart_tx_bytes.saturating_sub(packet.len());
                    state.uart_tx_offset = 0;
                }
                wait(byte_time);
            }
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => return Ok(()),
            Err(error) => return Err(error).context("writing Gateway UART in Pico-Fi bridge"),
        }
    }
}

fn enqueue_host_i2c_slots(state: &mut PicoBridgeState, frame: &[u8]) {
    let transfer_id = state.next_transfer_id.max(1);
    state.next_transfer_id = transfer_id.wrapping_add(1).max(1);
    for offset in (0..frame.len()).step_by(PICO_I2C_PAYLOAD_SIZE) {
        let end = (offset + PICO_I2C_PAYLOAD_SIZE).min(frame.len());
        let mut slot = [0u8; PICO_I2C_SLOT_SIZE];
        slot[..2].copy_from_slice(&PICO_I2C_MAGIC);
        slot[2] = PICO_I2C_VERSION;
        slot[3] = PICO_I2C_DATA;
        slot[4] = if offset == 0 { PICO_I2C_START } else { 0 }
            | if end == frame.len() { PICO_I2C_END } else { 0 };
        slot[6..10].copy_from_slice(&(offset as u32).to_le_bytes());
        slot[10..14].copy_from_slice(&(frame.len() as u32).to_le_bytes());
        slot[14..16].copy_from_slice(&((end - offset) as u16).to_le_bytes());
        slot[16..18].copy_from_slice(&transfer_id.to_le_bytes());
        slot[PICO_I2C_HEADER_SIZE..PICO_I2C_HEADER_SIZE + end - offset]
            .copy_from_slice(&frame[offset..end]);
        state.host_reads.push_back(slot);
    }
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
    let mut renode_pty_paths = Vec::new();
    let mut pico_bridges = Vec::new();
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
            renode_pty_paths.push(path.clone());
            let host_link = topology
                .host_nodes
                .iter()
                .find(|host| host.name == host_endpoint.node)
                .and_then(|host| {
                    host.serial_links
                        .iter()
                        .find(|serial| serial.link == link.name)
                });
            let transport = host_link.map(|serial| serial.transport).unwrap_or_default();
            if transport == HostLinkTransport::PicoFiI2cToUart {
                ensure!(
                    matches!(link.kind, LinkKind::PicoFi),
                    "host link {} selects Pico-Fi I2C translation but is not pico_fi",
                    link.name
                );
                let socket = scratch
                    .path()
                    .join(format!("{}.i2c.sock", safe(&link.name)));
                host_serial_paths.insert(link.name.clone(), socket.clone());
                pico_bridges.push((link.name.clone(), path, socket));
            } else {
                host_serial_paths.insert(link.name.clone(), path);
            }
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
        let firmware_image = node_scratch.join("firmware-flash.bin");
        fs::write(
            &firmware_image,
            crate::elf::flash_image(
                &elf,
                &layout.memory,
                &format!("node {} firmware", node.name),
            )?,
        )
        .with_context(|| format!("writing node {} flash image", node.name))?;
        let overlay = node_scratch.join("peripherals.repl");
        fs::write(
            &overlay,
            crate::execution::render_peripheral_overlay(&layout)?,
        )?;
        let configured_platform =
            crate::execution::materialize_platform(&layout, &root, &node_scratch)?;
        let marker = format!("SEDS_NODE_BOOT_{}", safe(&node.name));
        let board_initialization = crate::execution::render_board_initialization_script(&layout);
        script += &format!(
            "mach create \"{}\"\nmachine LoadPlatformDescription @{}\nmachine LoadPlatformDescription @{}\n{}{}sysbus LoadSymbolsFrom @{}\nsysbus LoadBinary @{} 0x{:08x}\nphysicalFlash EndHostLoading\ncpu SetRegister 13 0x{:08x}\ncpu PC 0x{:08x}\ncpu VectorTableOffset 0x{:08x}\ncpu AddHook `sysbus GetSymbolAddress \"{}\"` 'self.InfoLog(\"{}\")'\n",
            safe(&node.name),
            configured_platform.display(),
            overlay.display(),
            if layout.board.strict_mmio { "sysbus UnhandledAccessBehaviour ThrowException\n" } else { "" },
            board_initialization,
            elf.display(),
            firmware_image.display(),
            layout.memory.flash_base,
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
        for path in &renode_pty_paths {
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
        for (name, uart_path, socket_path) in pico_bridges {
            let listener = UnixListener::bind(&socket_path).with_context(|| {
                format!(
                    "creating simulated Pico-Fi I2C socket {}",
                    socket_path.display()
                )
            })?;
            let bridge_name = name.clone();
            thread::spawn(move || {
                if let Err(error) = run_pico_fi_bridge(listener, &uart_path) {
                    eprintln!("[SIM] Pico-Fi bridge {bridge_name} failed: {error:#}");
                }
            });
            println!(
                "[SIM] Pico-Fi {} translating GroundStation I2C slots to Gateway UART frames",
                name
            );
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
            let variable_cache = host
                .network_variable_cache
                .as_ref()
                .map(|path| base.join(path))
                .unwrap_or_else(|| {
                    scratch
                        .path()
                        .join(format!("{}-network-variables.json", safe(&host.name)))
                });
            if let Some(parent) = variable_cache.parent() {
                fs::create_dir_all(parent).with_context(|| {
                    format!("creating host state directory {}", parent.display())
                })?;
            }
            command.env("GS_NETWORK_VARIABLE_CACHE", variable_cache);
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
    let mut host_outputs = BTreeMap::new();
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
        host_outputs.insert(name.clone(), combined_host);
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
        let error_context = combined
            .lines()
            .enumerate()
            .filter(|(_, line)| {
                let line = line.to_ascii_lowercase();
                line.contains("error")
                    || line.contains("exception")
                    || line.contains("fatal")
                    || line.contains("abort")
            })
            .flat_map(|(index, _)| {
                let lines = combined.lines().collect::<Vec<_>>();
                let start = index.saturating_sub(2);
                let end = (index + 3).min(lines.len());
                lines[start..end].to_vec()
            })
            .collect::<Vec<_>>()
            .join("\n");
        bail!(
            "linked bay execution failed (Renode status: {}):\n{}\n\nFinal Renode output:\n{}",
            status,
            if error_context.is_empty() {
                "No explicit Renode error line was emitted."
            } else {
                &error_context
            },
            combined
                .lines()
                .rev()
                .take(40)
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
        let profiles = parse_memory_profiles(
            node_name,
            layout,
            &combined,
            topology.sample_count,
            topology.enforce_end_drop,
        )
        .map_err(|error| {
            let host_detail = host_diagnostics
                .iter()
                .map(|(name, output)| format!("{name}:\n{output}"))
                .collect::<Vec<_>>()
                .join("\n");
            anyhow::anyhow!("{error:#}\n\nHost diagnostics:\n{host_detail}")
        })?;
        memory_profiles.insert(node_name.clone(), profiles);
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
    for assertion in &topology.host_log_assertions {
        let output = host_outputs.get(&assertion.node).with_context(|| {
            format!(
                "host-log assertion {} references unknown host node {}",
                assertion.name, assertion.node
            )
        })?;
        if !output.contains(&assertion.contains) {
            assertion_failures.push(format!(
                "{}: host {} did not report {:?}",
                assertion.name, assertion.node, assertion.contains
            ));
        }
    }
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
        let observed = if let Some(sample) = assertion.sample {
            *report.samples.get(sample).with_context(|| {
                format!(
                    "assertion {} requests sample {}, but {}.{} has {} samples",
                    assertion.name,
                    sample,
                    assertion.node,
                    assertion.probe,
                    report.samples.len()
                )
            })?
        } else {
            report.maximum_observed
        };
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
        let link_detail = link_reports
            .iter()
            .map(|link| {
                let endpoints = link
                    .endpoints
                    .iter()
                    .map(|endpoint| {
                        let tx = endpoint
                            .tx_probe
                            .as_deref()
                            .zip(endpoint.tx_observed)
                            .map(|(probe, value)| format!("{probe}={value}"))
                            .unwrap_or_else(|| "tx=n/a".to_owned());
                        let rx = endpoint
                            .rx_probe
                            .as_deref()
                            .zip(endpoint.rx_observed)
                            .map(|(probe, value)| format!("{probe}={value}"))
                            .unwrap_or_else(|| "rx=n/a".to_owned());
                        format!("{}({tx}, {rx})", endpoint.node)
                    })
                    .collect::<Vec<_>>()
                    .join(", ");
                format!("{}: {endpoints}", link.name)
            })
            .collect::<Vec<_>>()
            .join("\n");
        let assertion_detail = assertion_reports
            .iter()
            .map(|report| {
                format!(
                    "{}: {}.{}={}",
                    report.name, report.node, report.probe, report.observed
                )
            })
            .collect::<Vec<_>>()
            .join("\n");
        let host_detail = host_diagnostics
            .iter()
            .map(|(name, output)| format!("{name}:\n{output}"))
            .collect::<Vec<_>>()
            .join("\n");
        bail!(
            "{} network assertion(s) failed:\n- {}\n\nLink observations:\n{}\n\nAssertion observations:\n{}\n\nHost diagnostics:\n{}",
            assertion_failures.len(),
            assertion_failures.join("\n- "),
            link_detail,
            assertion_detail,
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
    let mut qualification_failures = Vec::new();
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
        let qualified_samples = &samples[layout.execution.memory_probe_warmup_samples..];
        let minimum_observed = *qualified_samples.iter().min().context("empty bay probe")?;
        let maximum_observed = *qualified_samples.iter().max().context("empty bay probe")?;
        let end_drop = i64::from(samples[layout.execution.memory_probe_warmup_samples])
            - i64::from(*samples.last().unwrap());
        if let Some(minimum) = probe.minimum {
            if minimum_observed < minimum {
                qualification_failures.push(format!(
                    "{} fell below {minimum}: {:?}",
                    probe.name, samples
                ));
            }
        }
        if let Some(maximum) = probe.maximum {
            if maximum_observed > maximum {
                qualification_failures
                    .push(format!("{} exceeded {maximum}: {:?}", probe.name, samples));
            }
        }
        if enforce_end_drop {
            if let Some(max_end_drop) = probe.max_end_drop {
                if end_drop > i64::from(max_end_drop) {
                    qualification_failures.push(format!(
                        "{} lost {end_drop} bytes: {:?}",
                        probe.name, samples
                    ));
                }
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
    if !qualification_failures.is_empty() {
        let observations = reports
            .iter()
            .map(|report| {
                format!(
                    "{}: min={}, max={}, samples={:?}",
                    report.name, report.minimum_observed, report.maximum_observed, report.samples
                )
            })
            .collect::<Vec<_>>()
            .join("\n");
        bail!(
            "node {node_name} failed {} probe threshold(s):\n- {}\n\nAll {node_name} probe observations:\n{}",
            qualification_failures.len(),
            qualification_failures.join("\n- "),
            observations
        );
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

#[cfg(test)]
mod pico_fi_tests {
    use super::*;

    #[test]
    fn i2c_mailbox_envelope_is_recreated_for_gateway_uart() {
        let payload = b"sedsnet packet";
        let mut mailbox = vec![0xA5, 0x5A, payload.len() as u8, 0];
        mailbox.extend_from_slice(payload);
        let translated = i2c_request_to_gateway_uart(&mailbox).unwrap();
        assert_eq!(&translated[..2], &[0xA5, 0x5A]);
        assert_eq!(u16::from_le_bytes([translated[2], translated[3]]), 14);
        assert_eq!(&translated[4..], payload);
    }

    #[test]
    fn uart_side_commands_do_not_cross_the_pico_network_bridge() {
        let data = [0xA5, 0x5A, 3, 0, 1, 2, 3];
        assert_eq!(gateway_uart_to_i2c_response(&data).unwrap(), data);
        let command = [0xA6, 0x5B, 3, 0, b'/', b'o', b'k'];
        assert!(gateway_uart_to_i2c_response(&command).is_none());
    }

    #[test]
    fn pico_i2c_mailbox_chunks_match_firmware_slot_layout() {
        let frame = [
            0xA5, 0x5A, 16, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
        ];
        let mut state = PicoBridgeState::default();
        enqueue_host_i2c_slots(&mut state, &frame);
        assert_eq!(state.host_reads.len(), 2);
        let first = state.host_reads.pop_front().unwrap();
        let second = state.host_reads.pop_front().unwrap();
        assert_eq!(&first[..3], &[0x49, 0x32, 1]);
        assert_eq!(first[3], PICO_I2C_DATA);
        assert_eq!(first[4], PICO_I2C_START);
        assert_eq!(u32::from_le_bytes(first[6..10].try_into().unwrap()), 0);
        assert_eq!(u16::from_le_bytes(first[14..16].try_into().unwrap()), 14);
        assert_eq!(second[4], PICO_I2C_END);
        assert_eq!(u32::from_le_bytes(second[6..10].try_into().unwrap()), 14);
        assert_eq!(u16::from_le_bytes(second[14..16].try_into().unwrap()), 6);
    }

    #[test]
    fn gateway_backpressure_does_not_reject_a_valid_mailbox_packet() {
        let mut state = PicoBridgeState::default();
        enqueue_gateway_uart_packet(&mut state, vec![1; 128]);
        assert_eq!(state.uart_tx_packets.len(), 1);
        assert_eq!(state.uart_tx_bytes, 128);
    }

    #[test]
    fn gateway_uart_delivery_is_paced_at_the_physical_wire_rate() {
        let packet = vec![0xA5, 0x5A, 3, 0, 1, 2, 3];
        let mut state = PicoBridgeState::default();
        enqueue_gateway_uart_packet(&mut state, packet.clone());
        let mut written = Vec::new();
        let mut delays = Vec::new();

        drain_gateway_uart_tx_paced(&mut state, &mut written, |delay| delays.push(delay)).unwrap();

        assert_eq!(written, packet);
        assert_eq!(delays.len(), packet.len());
        assert!(delays
            .iter()
            .all(|delay| *delay >= Duration::from_micros(86)));
        assert!(state.uart_tx_packets.is_empty());
        assert_eq!(state.uart_tx_bytes, 0);
    }
}
