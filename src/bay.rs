use crate::{core::Architecture, execution::MemoryProbeReport, layout::BoardLayout};
use anyhow::{bail, ensure, Context, Result};
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    env, fs,
    fs::OpenOptions,
    io::{BufReader, Read, Write},
    os::fd::AsRawFd,
    os::unix::net::{UnixListener, UnixStream},
    os::unix::process::CommandExt,
    path::{Path, PathBuf},
    process::{Child, ChildStdin, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc,
    },
    thread,
    time::Duration,
};
#[path = "picofi_tcp_bridge.rs"]
mod picofi_tcp_bridge;

pub(crate) fn keep_console_stdin_open(child: &mut Child) -> Result<ChildStdin> {
    child
        .stdin
        .take()
        .context("retaining the Renode console stdin guard")
}

// Renode's launcher can spawn a runtime child which inherits its console
// pipes. Kill the owned process group on failure, otherwise reader joins can
// wait forever after only the launcher exits.
fn terminate_simulator(child: &mut Child) {
    unsafe {
        libc::kill(-(child.id() as libc::pid_t), libc::SIGKILL);
    }
    let _ = child.kill();
}

fn render_checkpoint_registers(node: &str, sample: usize) -> String {
    let mut script = String::new();
    for register in 0..16 {
        script += &format!(
            "echo \"SEDS_CHECKPOINT {sample} {} R{register}\"\ncpu GetRegister {register}\n",
            safe(node)
        );
    }
    for (name, address) in [
        ("ICSR", 0xe000_ed04u32),
        ("CFSR", 0xe000_ed28),
        ("HFSR", 0xe000_ed2c),
    ] {
        script += &format!(
            "echo \"SEDS_CHECKPOINT {sample} {} {name}\"\nsysbus ReadDoubleWord 0x{address:08x}\n",
            safe(node)
        );
    }
    script
}

fn execution_tail(text: &str, count: usize) -> String {
    let mut lines = text.lines().rev().take(count).collect::<Vec<_>>();
    lines.reverse();
    lines.join("\n")
}

fn console_execution_failed(line: &str) -> bool {
    let lower = line.to_ascii_lowercase();
    lower.contains("there was an error executing command")
        || lower.contains("no such command or device")
        || lower.contains("parameters did not match")
        || lower.contains("seds_iwdg_reset")
        || lower.contains("fatal error")
        || unmapped_cpu_ram_access(&lower)
}

// Renode's default bus returns zero for unmapped memory instead of raising the
// physical CPU's bus fault. Continuing a corrupt SRAM copy can generate millions
// of warnings and make a failed guest look like a slow, healthy simulation.
// Leave missing MMIO diagnostics and explicit host probes alone.
fn unmapped_cpu_ram_access(line: &str) -> bool {
    if !line.contains("[cpu:") || !line.contains("non existing peripheral at 0x") {
        return false;
    }
    let Some((_, address)) = line.split_once("non existing peripheral at 0x") else {
        return false;
    };
    let hex = address
        .split(|ch: char| !ch.is_ascii_hexdigit())
        .next()
        .unwrap_or("");
    u32::from_str_radix(hex, 16).is_ok_and(|address| {
        (0x2000_0000..0x2200_0000).contains(&address)
            || (0x3000_0000..0x3200_0000).contains(&address)
    })
}

fn release_host_pty_exclusive(guard: &fs::File) -> Result<()> {
    // SIGKILL cannot run serialport's Drop/TIOCNXCL. Unlike a real machine
    // reboot, the simulator keeps the PTY master alive in Renode, so TIOCEXCL
    // survives the dead host. Clear it only after that host has been reaped.
    let result = unsafe { libc::ioctl(guard.as_raw_fd(), libc::TIOCNXCL as _) };
    if result < 0 {
        return Err(std::io::Error::last_os_error()).context("releasing rebooted host UART PTY");
    }
    Ok(())
}

#[derive(Debug, Deserialize)]
pub struct BayTopology {
    pub name: String,
    #[serde(default = "default_quantum")]
    pub quantum_seconds: f64,
    #[serde(default = "default_duration")]
    pub virtual_time_ms: u64,
    #[serde(default = "default_sample_count")]
    pub sample_count: usize,
    /// Optional interval index for the bounded guest PC trace. By default it
    /// follows the first checkpoint; later indices can sample a warmed system.
    #[serde(default)]
    pub trace_sample: Option<usize>,
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
    /// Cold-reset selected firmware nodes after a completed sample. Physical
    /// flash is retained, allowing board persistence to be verified while the
    /// rest of the linked network remains online.
    #[serde(default)]
    pub reboots: Vec<RebootEvent>,
    /// Change whether an emulated CAN controller sees link-layer ACKs after a
    /// sample. This models a disconnected peer or transient physical-bus
    /// outage without stopping the firmware or its scheduler.
    #[serde(default)]
    pub can_ack_events: Vec<CanAckEvent>,
    /// Detach/attach a firmware CAN endpoint; sample zero is before boot.
    #[serde(default)]
    pub can_link_events: Vec<CanLinkEvent>,
}
#[derive(Debug, Deserialize)]
pub struct CanLinkEvent {
    pub node: String,
    pub peripheral: String,
    pub link: String,
    pub after_sample: usize,
    pub connected: bool,
}

fn render_can_link_event(event: &CanLinkEvent) -> String {
    format!(
        "mach set \"{}\"\nconnector {} sysbus.{} {}\nsysbus.{} Acknowledged {}\necho \"SEDS_BAY_CAN_LINK {} {} {} {}\"\n",
        safe(&event.node),
        if event.connected {
            "Connect"
        } else {
            "Disconnect"
        },
        safe(&event.peripheral),
        safe(&event.link),
        safe(&event.peripheral),
        event.connected,
        safe(&event.node),
        safe(&event.link),
        event.after_sample,
        event.connected
    )
}
#[derive(Clone, Debug, Deserialize)]
pub struct RebootEvent {
    pub node: String,
    /// One-based sample number after which the reset is applied.
    pub after_sample: usize,
}
#[derive(Debug, Deserialize)]
pub struct CanAckEvent {
    pub node: String,
    pub peripheral: String,
    /// One-based sample number after which the state is changed.
    pub after_sample: usize,
    pub acknowledged: bool,
}
#[derive(Debug, Deserialize)]
pub struct Node {
    pub name: String,
    pub layout: PathBuf,
    pub firmware_root: PathBuf,
}
#[derive(Clone, Debug, Deserialize)]
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
#[derive(Clone, Debug, Deserialize)]
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
    /// Pico-Fi Ethernet framing over a loopback TCP server, connected to the
    /// Gateway UART without using I2C transactions.
    PicoFiTcpToUart,
    /// Pico discovers the GroundStation listener over UDP before opening TCP.
    PicoFiDiscoveryToUart,
}
#[derive(Debug, Deserialize)]
pub struct Link {
    /// UART baud for a modeled Pico pair; must match both firmware ends.
    #[serde(default = "default_pico_uart_baud")]
    pub baud_rate: u64,
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
    /// Require the probe to advance by at least this amount between two
    /// zero-based samples. This detects a live RTOS thread whose transport has
    /// stopped making progress late in a soak.
    #[serde(default)]
    pub minimum_gain: Option<u32>,
    #[serde(default)]
    pub maximum_gain: Option<u32>,
    #[serde(default)]
    pub from_sample: Option<usize>,
    #[serde(default)]
    pub to_sample: Option<usize>,
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HostLogAssertion {
    pub name: String,
    pub node: String,
    pub contains: String,
    #[serde(default = "default_minimum_activity")]
    pub minimum_occurrences: u32,
    #[serde(default)]
    pub maximum_occurrences: Option<u32>,
}

impl HostLogAssertion {
    fn accepts(&self, observed: u32) -> bool {
        observed >= self.minimum_occurrences
            && self
                .maximum_occurrences
                .is_none_or(|maximum| observed <= maximum)
    }
}

#[cfg(test)]
mod assertion_format_tests {
    use super::*;
    #[test]
    fn misspelled_log_bounds_are_rejected_instead_of_silently_ignored() {
        let invalid = r#"{"name":"no failure","node":"groundstation","contains":"failure","minimum":0,"maximum":0}"#;
        assert!(serde_json::from_str::<HostLogAssertion>(invalid).is_err());
        let valid = invalid
            .replace("\"minimum\"", "\"minimum_occurrences\"")
            .replace("\"maximum\"", "\"maximum_occurrences\"");
        let assertion: HostLogAssertion = serde_json::from_str(&valid).unwrap();
        assert!(assertion.accepts(0));
        assert!(!assertion.accepts(1));
    }
}

struct HostRuntime {
    name: String,
    child: Child,
    stdout_path: PathBuf,
    stderr_path: PathBuf,
}

fn exited_host(hosts: &mut [HostRuntime]) -> Result<Option<String>> {
    for host in hosts {
        if let Some(status) = host.child.try_wait()? {
            return Ok(Some(format!(
                "host node {} exited early with {status}",
                host.name
            )));
        }
    }
    Ok(None)
}

fn spawn_host_node(
    host: &HostNode,
    base: &Path,
    scratch: &Path,
    sample_path: &Path,
    host_serial_paths: &BTreeMap<String, PathBuf>,
    append_logs: bool,
) -> Result<HostRuntime> {
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
        .unwrap_or_else(|| scratch.join(format!("{}-network-variables.json", safe(&host.name))));
    if let Some(parent) = variable_cache.parent() {
        fs::create_dir_all(parent)
            .with_context(|| format!("creating host state directory {}", parent.display()))?;
    }
    command.env("GS_NETWORK_VARIABLE_CACHE", variable_cache);
    command.env("FIRMWARE_SIM_SAMPLE_FILE", sample_path);
    command.env("GS_SIM_CLOCK_FILE", scratch.join("virtual-clock.txt"));
    let stdout_path = scratch.join(format!("{}-stdout.log", safe(&host.name)));
    let stderr_path = scratch.join(format!("{}-stderr.log", safe(&host.name)));
    let open_log = |path: &Path| {
        OpenOptions::new()
            .create(true)
            .write(true)
            .append(append_logs)
            .truncate(!append_logs)
            .open(path)
    };
    let child = command
        .stdout(Stdio::from(open_log(&stdout_path)?))
        .stderr(Stdio::from(open_log(&stderr_path)?))
        .spawn()
        .with_context(|| format!("starting host node {}", host.name))?;
    Ok(HostRuntime {
        name: host.name.clone(),
        child,
        stdout_path,
        stderr_path,
    })
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
    /// True when existing observations were checked without executing firmware.
    pub capture_recheck: bool,
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
const PICO_HOST_MAILBOX_DEPTH: usize = 128;
const PICO_PACKET_QUEUE_BYTES: usize = 8_192;
const PICO_UART_QUEUE_BYTES: usize = PICO_PACKET_QUEUE_DEPTH * PICO_UART_MAX_FRAME;
const PICO_UART_BAUD: u64 = 115_200;
fn default_pico_uart_baud() -> u64 {
    PICO_UART_BAUD
}

#[derive(Default)]
struct PicoBridgeState {
    host_assembly: Option<(u16, usize, Vec<u8>)>,
    uart_rx: Vec<u8>,
    uart_tx_packets: VecDeque<Vec<u8>>,
    uart_tx_offset: usize,
    ethernet_mailboxes: bool,
    ethernet_connected: bool,
    uart_tx_active: Option<Vec<u8>>,
    uart_tx_bytes: usize,
    host_reads: VecDeque<[u8; PICO_I2C_SLOT_SIZE]>,
    host_packets: VecDeque<Vec<u8>>,
    host_packet_bytes: usize,
    v2_mailbox: Option<Vec<u8>>,
    next_transfer_id: u16,
    i2c_to_uart_frames: u32,
    uart_to_i2c_frames: u32,
    uart_framing_errors: u32,
    host_packet_rejections: u32,
}

fn run_pico_fi_bridge_until(
    listener: UnixListener,
    uart_path: &Path,
    baud: u64,
    mut stop: impl FnMut() -> bool,
) -> Result<()> {
    let mut uart = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(uart_path)
        .with_context(|| format!("opening Pico-Fi Gateway UART {}", uart_path.display()))?;
    configure_raw_nonblocking(uart.as_raw_fd())?;
    ensure!(baud > 0, "Pico UART baud must be positive");
    let mut state = PicoBridgeState {
        next_transfer_id: 1,
        ..Default::default()
    };
    listener.set_nonblocking(true)?;
    let mut host: Option<PicoHostSession> = None;
    while !stop() {
        // The Pico UART remains active without an I2C controller. Never let a
        // dead/idle host fill the PTY and block Renode's Gateway CPU thread.
        read_gateway_uart(&mut state, &mut uart)?;
        drain_gateway_uart_tx(&mut state, &mut uart)?;
        if host.is_none() {
            match listener.accept() {
                Ok((stream, _)) => host = Some(PicoHostSession::new(stream)?),
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {}
                Err(error) => return Err(error).context("accepting GroundStation I2C endpoint"),
            }
        }
        if let Some(session) = &mut host {
            match session.service(&mut state) {
                Ok(true) => {}
                Ok(false) => {
                    host = None;
                    state.host_assembly = None;
                }
                Err(error) => {
                    eprintln!(
                        "[SIM] Pico-Fi I2C host disconnected: {error}; accepting replacement"
                    );
                    host = None;
                    state.host_assembly = None;
                }
            }
        }
        thread::sleep(Duration::from_millis(1));
    }
    Ok(())
}

struct PicoHostSession {
    stream: UnixStream,
    incoming: Vec<u8>,
    reply: Vec<u8>,
    reply_offset: usize,
}

impl PicoHostSession {
    fn new(stream: UnixStream) -> std::io::Result<Self> {
        stream.set_nonblocking(true)?;
        Ok(Self {
            stream,
            incoming: Vec::with_capacity(33),
            reply: Vec::with_capacity(32),
            reply_offset: 0,
        })
    }

    fn service(&mut self, state: &mut PicoBridgeState) -> std::io::Result<bool> {
        if self.reply.is_empty() {
            // Read exactly one operation, retaining partial slots across polls.
            let needed = match self.incoming.first() {
                Some(b'W') => 33,
                Some(b'w' | b'r') if self.incoming.len() < 3 => 3,
                Some(b'w') => {
                    let length = u16::from_le_bytes([self.incoming[1], self.incoming[2]]) as usize;
                    if !(4..=4096).contains(&length) {
                        return Err(std::io::Error::other("invalid I2C v2 write length"));
                    }
                    3 + length
                }
                Some(b'r') => 3,
                _ => 1,
            };
            let mut bytes = [0; 4099];
            match self.stream.read(&mut bytes[..needed - self.incoming.len()]) {
                Ok(0) => return Ok(false),
                Ok(count) => self.incoming.extend_from_slice(&bytes[..count]),
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => return Ok(true),
                Err(error) => return Err(error),
            }
            match self.incoming[0] {
                b'W' if self.incoming.len() < 33 => return Ok(true),
                b'W' => {
                    let slot = self.incoming[1..].try_into().expect("complete I2C slot");
                    self.reply
                        .push(if ingest_host_i2c_slot(state, slot).is_ok() {
                            0
                        } else {
                            1
                        });
                }
                b'R' => self.reply.extend_from_slice(&next_host_i2c_slot(state)),
                b'w' | b'r' if self.incoming.len() < 3 => return Ok(true),
                b'w' => {
                    let length = u16::from_le_bytes([self.incoming[1], self.incoming[2]]) as usize;
                    if self.incoming.len() < 3 + length {
                        return Ok(true);
                    }
                    self.reply.push(u8::from(
                        ingest_host_i2c_v2(state, &self.incoming[3..]).is_err(),
                    ));
                }
                b'r' => {
                    let length = u16::from_le_bytes([self.incoming[1], self.incoming[2]]) as usize;
                    self.reply = read_host_i2c_v2(state, length).map_err(std::io::Error::other)?;
                }
                _ => {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::InvalidData,
                        "unknown I2C operation",
                    ));
                }
            }
            self.incoming.clear();
        }
        match self.stream.write(&self.reply[self.reply_offset..]) {
            Ok(0) => return Ok(false),
            Ok(count) => self.reply_offset += count,
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => return Ok(true),
            Err(error) => return Err(error),
        }
        if self.reply_offset == self.reply.len() {
            self.reply.clear();
            self.reply_offset = 0;
        }
        Ok(true)
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

#[allow(clippy::manual_is_multiple_of)] // Keep compatibility with Rust 1.85.
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
        ensure!(
            enqueue_gateway_uart_packet(state, gateway_frame),
            "Pico UART egress full"
        );
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
                break;
            }
            Err(error) => return Err(error).context("reading Gateway UART in Pico-Fi bridge"),
        }
    }
    process_gateway_uart_rx(state)
}

fn process_gateway_uart_rx(state: &mut PicoBridgeState) -> Result<()> {
    loop {
        let Some(sync) = state.uart_rx.windows(2).position(|bytes| {
            matches!(
                bytes,
                [0xA5, 0x5A] | [0x5A, 0xA5] | [0xA6, 0x5B] | [0x5B, 0xA6] | [0xA7, 0x7A]
            )
        }) else {
            // Nonblocking reads can split the two-byte sync marker. Preserve
            // a possible first byte for the next UART service iteration.
            let trailing = state
                .uart_rx
                .last()
                .copied()
                .filter(|byte| matches!(byte, 0xA5 | 0x5A | 0xA6 | 0x5B | 0xA7));
            state.uart_rx.clear();
            if let Some(byte) = trailing {
                state.uart_rx.push(byte);
            }
            break;
        };
        if sync > 0 {
            state.uart_rx.drain(..sync);
        }
        let Some(frame_length) = complete_uart_frame_length(&state.uart_rx) else {
            break;
        };
        if frame_length > PICO_UART_MAX_FRAME {
            // pico-fi read_uart_request_frame consumes the four-byte header
            // and retries on an oversized length. A malformed UART frame is
            // not a reason to kill either independent Pico or their link.
            state.uart_framing_errors += 1;
            if state.uart_framing_errors <= 10 {
                eprintln!(
                    "[SIM] Pico-Fi UART framing error {}: length {}, header {:02x?}; resynchronizing",
                    state.uart_framing_errors,
                    frame_length,
                    &state.uart_rx[..4]
                );
            }
            state.uart_rx.drain(..4);
            continue;
        }
        if state.uart_rx.len() < frame_length {
            break;
        }
        let frame: Vec<u8> = state.uart_rx.drain(..frame_length).collect();
        if env::var_os("FIRMWARE_SIM_TRACE_PICO_ACKS").is_some() && frame.get(6) == Some(&1) {
            eprintln!("[SIM] Pico-Fi gateway ACK received: {:02x?}", &frame[4..]);
        }
        // UART-side commands are consumed by that Pico and never cross the
        // network bridge. Only DATA frames become I2C mailbox responses.
        if let Some(host_frame) = gateway_uart_to_i2c_response(&frame) {
            enqueue_host_packet(state, host_frame.clone());
            state.uart_to_i2c_frames += 1;
            if state.uart_to_i2c_frames <= 5 {
                let preview_len = host_frame.len().min(12);
                eprintln!(
                    "[SIM] Pico-Fi UART ingress frame {} translated ({} payload bytes, head {:02x?})",
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

const PICO_ETHERNET_MAILBOX_DEPTH: usize = 128;
const PICO_ETHERNET_MAILBOX_BYTES: usize = 16_384;
impl PicoBridgeState {
    fn ethernet() -> Self {
        Self {
            ethernet_mailboxes: true,
            ..Self::default()
        }
    }
    fn packet_cost(&self, wire_len: usize) -> usize {
        // Production stores kind + payload, excluding the four-byte envelope.
        if self.ethernet_mailboxes {
            wire_len.saturating_sub(3)
        } else {
            wire_len
        }
    }
    fn uart_has_room(&self, wire_len: usize) -> bool {
        let (depth, bytes) = if self.ethernet_mailboxes {
            (PICO_ETHERNET_MAILBOX_DEPTH, PICO_ETHERNET_MAILBOX_BYTES)
        } else {
            (PICO_PACKET_QUEUE_DEPTH, PICO_UART_QUEUE_BYTES)
        };
        wire_len <= PICO_UART_MAX_FRAME
            && self.uart_tx_packets.len() < depth
            && self.packet_cost(wire_len) <= bytes.saturating_sub(self.uart_tx_bytes)
    }
    fn clear_disconnected_upload(&mut self) {
        self.host_packets.clear();
        self.host_packet_bytes = 0;
    }
}
fn enqueue_gateway_uart_packet(state: &mut PicoBridgeState, packet: Vec<u8>) -> bool {
    if !state.uart_has_room(packet.len()) {
        return false;
    }
    state.uart_tx_bytes += state.packet_cost(packet.len());
    state.uart_tx_packets.push_back(packet);
    true
}

fn drain_gateway_uart_tx(state: &mut PicoBridgeState, uart: &mut fs::File) -> Result<()> {
    step_gateway_uart_tx(state, uart)
}

fn step_gateway_uart_tx(state: &mut PicoBridgeState, uart: &mut impl Write) -> Result<()> {
    // The UART worker owns one in-flight frame outside the shared mailbox.
    // It returns queue credit immediately on pop, and retains partial writes.
    if state.ethernet_mailboxes {
        if state.uart_tx_active.is_none() {
            state.uart_tx_active = state.uart_tx_packets.pop_front();
            if let Some(packet) = &state.uart_tx_active {
                state.uart_tx_bytes -= state.packet_cost(packet.len());
            }
        }
        if let Some(packet) = &state.uart_tx_active {
            match uart.write(&packet[state.uart_tx_offset..]) {
                Ok(0) => bail!("Gateway UART closed during Pico-Fi transfer"),
                Ok(length) => {
                    state.uart_tx_offset += length;
                    if state.uart_tx_offset == packet.len() {
                        state.uart_tx_active = None;
                        state.uart_tx_offset = 0;
                    }
                }
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {}
                Err(error) => return Err(error).context("writing Ethernet Pico UART"),
            }
        }
        return Ok(());
    }
    let Some(packet) = state.uart_tx_packets.front() else {
        return Ok(());
    };
    // The UART peripheral serializes this buffer using its configured BRR in
    // virtual time. Pacing individual bytes in wall time as well creates
    // spurious IDLE interrupts, DMA rearming and lost bytes between characters.
    // One nonblocking write preserves duplex service and partial-write state.
    match uart.write(&packet[state.uart_tx_offset..]) {
        Ok(0) => bail!("Gateway UART closed during Pico-Fi transfer"),
        Ok(length) => {
            state.uart_tx_offset += length;
            if state.uart_tx_offset == packet.len() {
                let packet = state.uart_tx_packets.pop_front().unwrap();
                state.uart_tx_bytes = state.uart_tx_bytes.saturating_sub(packet.len());
                state.uart_tx_offset = 0;
            }
        }
        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {}
        Err(error) => return Err(error).context("writing Gateway UART in Pico-Fi bridge"),
    }
    Ok(())
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

fn enqueue_host_packet(state: &mut PicoBridgeState, frame: Vec<u8>) {
    // Byte-backed Pico mailboxes retain accepted packets. At saturation the
    // network reader pauses; further unaccepted upstream UART traffic can be
    // refused, but must never evict an accepted SDT template or receipt.
    if state.ethernet_mailboxes && !state.ethernet_connected {
        return;
    }
    let bytes = if state.ethernet_mailboxes {
        PICO_ETHERNET_MAILBOX_BYTES
    } else {
        PICO_PACKET_QUEUE_BYTES
    };
    if state.host_packets.len() >= PICO_HOST_MAILBOX_DEPTH
        || state.packet_cost(frame.len()) > bytes.saturating_sub(state.host_packet_bytes)
    {
        state.host_packet_rejections += 1;
        if env::var_os("FIRMWARE_SIM_TRACE_PICO_ACKS").is_some() {
            eprintln!(
                "[SIM] Pico-Fi upstream frame refused at mailbox backpressure {}",
                state.host_packet_rejections
            );
        }
        return;
    }
    state.host_packet_bytes += state.packet_cost(frame.len());
    state.host_packets.push_back(frame);
}

fn next_host_i2c_slot(state: &mut PicoBridgeState) -> [u8; PICO_I2C_SLOT_SIZE] {
    if state.host_reads.is_empty() {
        // Match FIFO staging in both Pico I2C protocols. Distinct ACKs and
        // template-dependent packets must all survive an ordinary poll.
        // Queue overflow remains bounded separately in enqueue_host_packet.
        if let Some(frame) = state.host_packets.pop_front() {
            if env::var_os("FIRMWARE_SIM_TRACE_PICO_ACKS").is_some() && frame.get(6) == Some(&1) {
                eprintln!("[SIM] Pico-Fi gateway ACK staged: {:02x?}", &frame[4..]);
            }
            state.host_packet_bytes -= frame.len();
            enqueue_host_i2c_slots(state, &frame);
        }
    }
    state
        .host_reads
        .pop_front()
        .unwrap_or([0; PICO_I2C_SLOT_SIZE])
}

// I2C v2 selects a four-byte packet header. Header-only and incomplete reads
// retain the staged packet; a completed body read consumes exactly one packet.
fn ingest_host_i2c_v2(state: &mut PicoBridgeState, packet: &[u8]) -> Result<()> {
    ensure!(
        packet.len() >= 4 && packet[0] == 0xd2,
        "invalid I2C v2 header"
    );
    let length = u16::from_le_bytes([packet[2], packet[3]]) as usize;
    ensure!(
        length <= 4092 && packet.len() == length + 4,
        "invalid I2C v2 packet length"
    );
    match packet[1] {
        0 => ensure!(length == 0, "invalid I2C v2 selection"),
        1 => {
            ensure!(length > 0, "empty I2C v2 DATA packet");
            let mut frame = vec![0xa5, 0x5a];
            frame.extend_from_slice(&(length as u16).to_le_bytes());
            frame.extend_from_slice(&packet[4..]);
            ensure!(
                enqueue_gateway_uart_packet(state, frame),
                "Pico UART egress full"
            );
            state.i2c_to_uart_frames += 1;
        }
        _ => bail!("unsupported simulated I2C v2 command"),
    }
    Ok(())
}

fn read_host_i2c_v2(state: &mut PicoBridgeState, length: usize) -> Result<Vec<u8>> {
    ensure!((4..=4096).contains(&length), "invalid I2C v2 read length");
    if state.v2_mailbox.is_none() {
        if let Some(frame) = state.host_packets.pop_front() {
            state.host_packet_bytes -= frame.len();
            ensure!(
                frame.len() >= 4 && frame.len() <= 4096,
                "invalid staged UART frame"
            );
            let payload_len = frame.len() - 4;
            let mut packet = vec![0xd2, 1];
            packet.extend_from_slice(&(payload_len as u16).to_le_bytes());
            packet.extend_from_slice(&frame[4..]);
            state.v2_mailbox = Some(packet);
        }
    }
    let mut result = vec![0; length];
    if let Some(packet) = &state.v2_mailbox {
        let copied = length.min(packet.len());
        result[..copied].copy_from_slice(&packet[..copied]);
        if length >= packet.len() {
            state.v2_mailbox = None;
        }
    } else {
        result[..4].copy_from_slice(&[0xd2, 0, 0, 0]);
    }
    Ok(result)
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
    select_profile_interval(topology.sample_count, topology.trace_sample)?;
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
    for reboot in &topology.reboots {
        ensure!(
            topology.nodes.iter().any(|node| node.name == reboot.node)
                || topology
                    .host_nodes
                    .iter()
                    .any(|node| node.name == reboot.node),
            "reboot references unknown node {}",
            reboot.node
        );
        ensure!(
            reboot.after_sample > 0 && reboot.after_sample < topology.sample_count,
            "reboot for {} must occur after samples 1..{}",
            reboot.node,
            topology.sample_count.saturating_sub(1)
        );
    }
    for event in &topology.can_ack_events {
        ensure!(
            topology.nodes.iter().any(|node| node.name == event.node),
            "CAN ACK event references unknown firmware node {}",
            event.node
        );
        ensure!(
            event.after_sample > 0 && event.after_sample < topology.sample_count,
            "CAN ACK event for {}.{} must occur after samples 1..{}",
            event.node,
            event.peripheral,
            topology.sample_count.saturating_sub(1)
        );
        ensure!(
            topology.links.iter().any(|link| {
                matches!(link.kind, LinkKind::Can)
                    && link.endpoints.iter().any(|endpoint| {
                        endpoint.node == event.node && endpoint.peripheral == event.peripheral
                    })
            }),
            "CAN ACK event references unlinked CAN endpoint {}.{}",
            event.node,
            event.peripheral
        );
    }
    for event in &topology.can_link_events {
        ensure!(
            event.after_sample < topology.sample_count,
            "CAN link event is outside the run"
        );
        ensure!(
            topology.nodes.iter().any(|node| node.name == event.node),
            "CAN link event references unknown firmware node {}",
            event.node
        );
        ensure!(
            topology.links.iter().any(|link| link.name == event.link
                && matches!(link.kind, LinkKind::Can)
                && link
                    .endpoints
                    .iter()
                    .any(|ep| ep.node == event.node && ep.peripheral == event.peripheral)),
            "CAN link event references an unlinked endpoint"
        );
    }
    let base = topology_path.parent().unwrap_or_else(|| Path::new("."));
    let scratch = tempfile::tempdir()?;
    // Probe records must not share a stream with asynchronous device logs.
    let diagnostics_root = env::var_os("FIRMWARE_SIM_DIAGNOSTICS_DIR").map(PathBuf::from);
    let live_directory = crate::bounded_diagnostics::live_directory(diagnostics_root.as_deref())
        .context("creating live bay diagnostics")?;
    let probe_path = live_directory.as_ref().map_or_else(
        || scratch.path().join("memory-probes.txt"),
        |directory| directory.join("memory-probes.txt"),
    );
    fs::write(&probe_path, "").context("creating bay probe capture")?;
    let mut script = String::new();
    let mut boot_captures = Vec::new();
    let mut node_layouts = Vec::new();
    let mut node_reset_vectors = BTreeMap::new();
    let mut host_serial_paths = BTreeMap::new();
    let mut renode_pty_paths = Vec::new();
    let mut pico_bridges = Vec::new();
    let mut pico_tcp_bridges = Vec::new();
    let mut pico_discovery_bridges = Vec::new();
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
            if transport == HostLinkTransport::PicoFiDiscoveryToUart {
                ensure!(
                    matches!(link.kind, LinkKind::PicoFi),
                    "Pico discovery requires a pico_fi link"
                );
                let reservation = std::net::UdpSocket::bind("127.0.0.1:0")?;
                let port = reservation.local_addr()?.port();
                drop(reservation);
                host_serial_paths.insert(link.name.clone(), PathBuf::from(port.to_string()));
                pico_discovery_bridges.push((link.name.clone(), path, port));
            } else if transport == HostLinkTransport::PicoFiTcpToUart {
                ensure!(
                    matches!(link.kind, LinkKind::PicoFi),
                    "Pico-Fi TCP requires a pico_fi link"
                );
                let listener = std::net::TcpListener::bind("127.0.0.1:0")?;
                host_serial_paths.insert(
                    link.name.clone(),
                    PathBuf::from(listener.local_addr()?.to_string()),
                );
                pico_tcp_bridges.push((link.name.clone(), path, listener));
            } else if transport == HostLinkTransport::PicoFiI2cToUart {
                ensure!(
                    matches!(link.kind, LinkKind::PicoFi),
                    "host link {} selects Pico-Fi I2C translation but is not pico_fi",
                    link.name
                );
                let socket = scratch
                    .path()
                    .join(format!("{}.i2c.sock", safe(&link.name)));
                host_serial_paths.insert(link.name.clone(), socket.clone());
                ensure!(link.baud_rate > 0, "Pico UART baud must be positive");
                pico_bridges.push((link.name.clone(), path, socket, link.baud_rate));
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
            crate::elf::validated_factory_image(
                &root.join(&layout.artifacts.factory),
                &elf,
                &layout.memory,
                firmware_vtor,
                &format!("node {} firmware", node.name),
            )?,
        )
        .with_context(|| format!("writing node {} flash image", node.name))?;
        let overlay = node_scratch.join("peripherals.repl");
        fs::write(&overlay, {
            let mut overlay = crate::execution::render_peripheral_overlay(&layout)?;
            if node_layouts.is_empty() {
                overlay += &format!(
                    "\nsimulationClock: Miscellaneous.SedsSimulationClock\n    path: \"{}\"\n    preinit:\n        include @{}\n",
                    scratch.path().join("virtual-clock.txt").display(),
                    PathBuf::from(
                        env::var_os("FIRMWARE_SIM_ROOT")
                            .unwrap_or_else(|| root.clone().into_os_string())
                    )
                    .join("renode/peripherals/SedsSimulationClock.cs")
                    .display()
                );
            }
            overlay
        })?;
        let configured_platform =
            crate::execution::materialize_platform(&layout, &root, &node_scratch)?;
        let marker = format!("SEDS_NODE_BOOT_{}", safe(&node.name));
        let boot_path = probe_path
            .parent()
            .expect("probe directory")
            .join(format!("boot-{}.marker", safe(&node.name)));
        let boot_python = format!(
            "with open({}, 'w') as f: f.write({} + chr(10))",
            serde_json::to_string(&boot_path.to_string_lossy())?,
            serde_json::to_string(&marker)?,
        );
        boot_captures.push((boot_path, marker.clone()));
        let board_initialization = crate::execution::render_board_initialization_script(&layout);
        script += &format!(
            "mach create \"{}\"\nmachine LoadPlatformDescription @{}\nmachine LoadPlatformDescription @{}\n{}{}sysbus LoadSymbolsFrom @{}\nsysbus LoadBinary @{} 0x{:08x}\nphysicalFlash EndHostLoading\ncpu SetRegister 13 0x{:08x}\ncpu PC 0x{:08x}\ncpu VectorTableOffset 0x{:08x}\ncpu AddHook `sysbus GetSymbolAddress \"{}\"` {}\n",
            safe(&node.name),
            configured_platform.display(),
            overlay.display(),
            if layout.board.strict_mmio {
                "sysbus UnhandledAccessBehaviour ThrowException\n"
            } else {
                ""
            },
            board_initialization,
            elf.display(),
            firmware_image.display(),
            layout.memory.flash_base,
            firmware_msp,
            firmware_pc,
            firmware_vtor,
            layout.execution.boot_success_symbol,
            serde_json::to_string(&boot_python)?
        );
        for link in &topology.links {
            for endpoint in link
                .endpoints
                .iter()
                .filter(|endpoint| endpoint.node == node.name)
            {
                if matches!(link.kind, LinkKind::Can)
                    && fs::read_to_string(&configured_platform)?.contains("SedsFixedFdcan")
                {
                    script += &format!(
                        "sysbus.{} BusName \"{}\"\n",
                        safe(&endpoint.peripheral),
                        safe(&link.name)
                    );
                }
                script += &format!(
                    "connector Connect sysbus.{} {}\n",
                    safe(&endpoint.peripheral),
                    safe(&link.name)
                );
            }
        }
        node_layouts.push((node.name.clone(), layout));
        node_reset_vectors.insert(
            node.name.clone(),
            (firmware_msp, firmware_pc, firmware_vtor, elf),
        );
    }
    for event in topology
        .can_link_events
        .iter()
        .filter(|event| event.after_sample == 0)
    {
        script += &render_can_link_event(event);
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
    let profiled_nodes: Vec<&str> = node_layouts
        .iter()
        .filter(|(_, layout)| layout.execution.trace)
        .map(|(name, _)| name.as_str())
        .collect();
    let profile_sample = select_profile_interval(topology.sample_count, topology.trace_sample)?;
    for sample in 0..topology.sample_count {
        let duration_ms = base_ms + u64::from((sample as u64) < remainder_ms);
        if sample == profile_sample && !profiled_nodes.is_empty() {
            script += &render_cpu_profile_step(scratch.path(), &profiled_nodes, duration_ms)?;
        } else {
            script += &format!("emulation RunFor \"{}s\"\n", duration_ms as f64 / 1000.0);
        }
        for (node_name, layout) in &node_layouts {
            script += &format!("mach set \"{}\"\n", safe(node_name));
            if live_directory.is_some() {
                script += &render_checkpoint_registers(node_name, sample);
            }
            for probe in &layout.execution.memory_probes {
                script += &render_probe_capture(
                    &probe_path,
                    node_name,
                    &probe.name,
                    sample,
                    &probe.symbol,
                    probe.byte_offset,
                    probe.address,
                )?;
            }
        }
        for (node_name, layout) in &node_layouts {
            script += &format!("mach set \"{}\"\n", safe(node_name));
            for probe in can_fifo_model_probes(layout) {
                script += &render_can_fifo_probe_capture(
                    &probe_path,
                    node_name,
                    &probe.name,
                    sample,
                    &probe.symbol,
                )?;
            }
        }
        for event in topology
            .can_ack_events
            .iter()
            .filter(|event| event.after_sample == sample + 1)
        {
            script += &format!(
                "mach set \"{}\"\nsysbus.{} Acknowledged {}\necho \"SEDS_BAY_CAN_ACK {} {} {} {}\"\n",
                safe(&event.node),
                safe(&event.peripheral),
                if event.acknowledged { "true" } else { "false" },
                safe(&event.node),
                safe(&event.peripheral),
                event.after_sample,
                event.acknowledged
            );
        }
        for event in topology
            .can_link_events
            .iter()
            .filter(|event| event.after_sample == sample + 1)
        {
            script += &render_can_link_event(event);
        }
        for reboot in topology.reboots.iter().filter(|event| {
            event.after_sample == sample + 1
                && topology.nodes.iter().any(|node| node.name == event.node)
        }) {
            let (msp, pc, vtor, elf) = &node_reset_vectors[&reboot.node];
            script += &format!(
                "mach set \"{}\"\nmachine Reset\nsysbus LoadSymbolsFrom @{}\ncpu SetRegister 13 0x{:08x}\ncpu PC 0x{:08x}\ncpu VectorTableOffset 0x{:08x}\necho \"SEDS_BAY_REBOOT {} {}\"\n",
                safe(&reboot.node),
                elf.display(),
                msp,
                pc,
                vtor,
                safe(&reboot.node),
                sample + 1
            );
        }
        // Publish the sample only after its fault/recovery and reboot events
        // have been applied. Host validators can then inject work into the
        // exact post-event interval without racing the simulator monitor.
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
        // Capture registers before any reset/teardown. These are read-only
        // monitor operations, so the firmware image and memory layout stay intact.
        for register in 0..16 {
            script += &format!(
                "echo \"SEDS_NODE {} R{}\"\ncpu GetRegister {}\n",
                safe(&node.name),
                register,
                register
            );
        }
        for (name, address) in [
            ("ICSR", 0xe000_ed04u32),
            ("CFSR", 0xe000_ed28),
            ("HFSR", 0xe000_ed2c),
        ] {
            script += &format!(
                "echo \"SEDS_NODE {} {}\"\nsysbus ReadDoubleWord 0x{:08x}\n",
                safe(&node.name),
                name,
                address
            );
        }
    }
    if let Some(directory) = env::var_os("FIRMWARE_SIM_DIAGNOSTICS_DIR") {
        let directory = PathBuf::from(directory);
        fs::create_dir_all(&directory).context("creating SD capture directory")?;
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_nanos();
        script += &render_sd_card_captures(&node_layouts, &directory, stamp);
    }
    script += "echo \"SEDS_BAY_COMPLETE\"\n";
    let script_path = scratch.path().join("bay.resc");
    fs::write(&script_path, script)?;
    if let Some(directory) = &live_directory {
        fs::copy(&script_path, directory.join("bay.resc"))?;
        println!(
            "[SIM] Live bay diagnostics retained in {}",
            directory.display()
        );
    }
    let renode = env::var_os("RENODE")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/usr/bin/renode"));
    println!(
        "[SIM] starting {} nodes across {} physical links for {} ms",
        topology.nodes.len(),
        topology.links.len(),
        topology.virtual_time_ms
    );
    fs::write(scratch.path().join("virtual-clock.txt"), "0\n")?;
    let sample_path = scratch.path().join("network-sample.txt");
    fs::write(&sample_path, "0\n").context("creating linked-bay sample marker")?;
    let mut child = Command::new(&renode)
        .process_group(0)
        .args(["--disable-xwt", "--console", "--execute"])
        .arg(format!("include @{}; quit", script_path.display()))
        .env("SEDS_SIM_PROBE_CAPTURE", &probe_path)
        .env(
            "SEDS_SIM_BOOT_CAPTURE_DIR",
            probe_path.parent().expect("probe directory"),
        )
        // Renode's redirected console input handler must not observe EOF during
        // a long run. Child::wait closes an internally owned stdin handle, so
        // take and retain this private pipe below until emulation completes.
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .context("starting Renode")?;
    let _renode_stdin_guard = keep_console_stdin_open(&mut child)?;
    let (console_failure_tx, console_failure_rx) = mpsc::channel();
    let bridge_stop = Arc::new(AtomicBool::new(false));
    let mut host_children = Vec::new();
    let mut host_pty_guards = BTreeMap::new();
    let stdout = child.stdout.take().context("capturing Renode stdout")?;
    let stderr = child.stderr.take().context("capturing Renode stderr")?;
    let progress_sample_path = sample_path.clone();
    let stderr_failure_tx = console_failure_tx.clone();
    let stdout_failure_tx = console_failure_tx.clone();
    let stdout_bridge_stop = bridge_stop.clone();
    let mut stdout_log = live_directory
        .as_ref()
        .map(|directory| {
            crate::bounded_diagnostics::RotatingLog::create(directory.join("renode.stdout.log"))
        })
        .transpose()?;
    let mut stderr_log = live_directory
        .as_ref()
        .map(|directory| {
            crate::bounded_diagnostics::RotatingLog::create(directory.join("renode.stderr.log"))
        })
        .transpose()?;
    let boot_markers: Vec<String> = topology
        .nodes
        .iter()
        .map(|node| format!("SEDS_NODE_BOOT_{}", safe(&node.name)))
        .collect();
    let stdout_boot_markers = boot_markers.clone();
    let stdout_reader = thread::spawn(move || -> Result<String> {
        let result = (|| -> Result<String> {
            let mut captured =
                crate::bounded_diagnostics::Capture::with_markers(stdout_boot_markers);
            let mut reader = BufReader::new(stdout);
            while let Some(line) = crate::bounded_diagnostics::read_line(&mut reader)? {
                if let Some(log) = &mut stdout_log {
                    log.line(
                        &line,
                        line.starts_with("SEDS_BAY_SAMPLE_DONE ") || line == "SEDS_BAY_COMPLETE",
                    )?;
                }
                if console_execution_failed(&line) {
                    let _ = stdout_failure_tx.send(line.clone());
                }
                if line.trim() == "SEDS_BAY_COMPLETE" {
                    // Renode closes its PTYs when the script exits. Stop the
                    // simulated cable first; that expected close is not a live
                    // transport failure. Probe/host validation still runs below.
                    stdout_bridge_stop.store(true, Ordering::Release);
                }
                if let Some(progress) = line.trim().strip_prefix("SEDS_BAY_SAMPLE_DONE ") {
                    println!("[SIM] network sample {progress} complete");
                    if let Some(sample) = progress.split_whitespace().next() {
                        fs::write(&progress_sample_path, format!("{sample}\n"))
                            .context("updating linked-bay sample marker")?;
                    }
                }
                captured.push(&line);
            }
            if let Some(log) = &mut stdout_log {
                log.flush()?;
            }
            Ok(captured.finish())
        })();
        if let Err(error) = &result {
            let _ = stdout_failure_tx.send(format!("stdout diagnostic capture failed: {error}"));
        }
        result
    });
    let stderr_reader = thread::spawn(move || -> Result<String> {
        let result = (|| -> Result<String> {
            let mut captured = crate::bounded_diagnostics::Capture::with_markers(boot_markers);
            let mut reader = BufReader::new(stderr);
            while let Some(line) = crate::bounded_diagnostics::read_line(&mut reader)? {
                if let Some(log) = &mut stderr_log {
                    log.line(&line, false)?;
                }
                if console_execution_failed(&line) {
                    let _ = stderr_failure_tx.send(line.clone());
                }
                captured.push(&line);
            }
            if let Some(log) = &mut stderr_log {
                log.flush()?;
            }
            Ok(captured.finish())
        })();
        if let Err(error) = &result {
            let _ = stderr_failure_tx.send(format!("stderr diagnostic capture failed: {error}"));
        }
        result
    });
    if !topology.host_nodes.is_empty() {
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(4);
        while renode_pty_paths.iter().any(|path| !path.exists())
            && std::time::Instant::now() < deadline
        {
            if child.try_wait()?.is_some() || console_failure_rx.try_recv().is_ok() {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(25));
        }
        if let Some(path) = renode_pty_paths.iter().find(|path| !path.exists()) {
            terminate_simulator(&mut child);
            let _ = child.wait();
            let stdout = stdout_reader
                .join()
                .ok()
                .and_then(Result::ok)
                .unwrap_or_default();
            let stderr = stderr_reader
                .join()
                .ok()
                .and_then(Result::ok)
                .unwrap_or_default();
            bail!(
                "Renode did not create host UART PTY {}; console: {}\n{}",
                path.display(),
                execution_tail(&stdout, 30),
                execution_tail(&stderr, 30)
            );
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
        for (name, uart_path, socket_path, baud) in pico_bridges {
            let listener = UnixListener::bind(&socket_path).with_context(|| {
                format!(
                    "creating simulated Pico-Fi I2C socket {}",
                    socket_path.display()
                )
            })?;
            let bridge_name = name.clone();
            let bridge_failure_tx = console_failure_tx.clone();
            let worker_stop = bridge_stop.clone();
            thread::spawn(move || {
                let result = run_pico_fi_bridge_until(listener, &uart_path, baud, || {
                    worker_stop.load(Ordering::Acquire)
                });
                if let Err(error) = result {
                    if worker_stop.load(Ordering::Acquire) {
                        return;
                    }
                    let failure = format!("Pico-Fi bridge {bridge_name} failed: {error:#}");
                    eprintln!("[SIM] {failure}");
                    let _ = bridge_failure_tx.send(failure);
                }
            });
            println!(
                "[SIM] Pico-Fi {} translating GroundStation I2C slots to Gateway UART frames",
                name
            );
        }
        for (name, uart_path, listener) in pico_tcp_bridges {
            let bridge_failure_tx = console_failure_tx.clone();
            let worker_stop = bridge_stop.clone();
            thread::spawn(move || {
                if let Err(error) = picofi_tcp_bridge::run(listener, &uart_path, || {
                    worker_stop.load(Ordering::Acquire)
                }) {
                    if !worker_stop.load(Ordering::Acquire) {
                        let _ = bridge_failure_tx
                            .send(format!("Pico-Fi TCP bridge {name} failed: {error:#}"));
                    }
                }
            });
        }
        for (name, uart_path, port) in pico_discovery_bridges {
            let bridge_failure_tx = console_failure_tx.clone();
            let worker_stop = bridge_stop.clone();
            thread::spawn(move || {
                if let Err(error) = picofi_tcp_bridge::run_discovered(port, &uart_path, || {
                    worker_stop.load(Ordering::Acquire)
                }) {
                    if !worker_stop.load(Ordering::Acquire) {
                        let _ = bridge_failure_tx
                            .send(format!("Pico-Fi discovery bridge {name} failed: {error:#}"));
                    }
                }
            });
        }
        for host in &topology.host_nodes {
            for serial in &host.serial_links {
                let path = &host_serial_paths[&serial.link];
                if renode_pty_paths.contains(path) {
                    // Retain a non-reading descriptor before the host claims
                    // exclusivity. Never apply terminal ioctls to I2C sockets.
                    let guard = OpenOptions::new()
                        .read(true)
                        .write(true)
                        .open(path)
                        .with_context(|| format!("retaining host UART PTY {}", path.display()))?;
                    host_pty_guards.insert((host.name.clone(), serial.link.clone()), guard);
                }
            }
            let child = spawn_host_node(
                host,
                base,
                scratch.path(),
                &sample_path,
                &host_serial_paths,
                false,
            )?;
            println!("[SIM] host node {} started", host.name);
            host_children.push(child);
        }
    }
    let mut completed_host_reboots = BTreeSet::new();
    let status = loop {
        if let Ok(error) = console_failure_rx.try_recv() {
            eprintln!("[SIM] Renode execution failed: {error}");
            // A failed include can leave the interactive console alive with
            // stdin held open. Stop it so diagnostics reach the test runner.
            // Allow its subsequent exception lines to reach the readers.
            thread::sleep(Duration::from_millis(250));
            terminate_simulator(&mut child);
        }
        if let Some(status) = child.try_wait()? {
            break status;
        }
        if let Some(error) = exited_host(&mut host_children)? {
            eprintln!("[SIM] {error}; stopping firmware execution to collect diagnostics");
            terminate_simulator(&mut child);
            break child
                .wait()
                .context("reaping simulator after host failure")?;
        }
        let completed_sample = fs::read_to_string(&sample_path)
            .ok()
            .and_then(|value| value.trim().parse::<usize>().ok())
            .unwrap_or(0);
        for (event_index, reboot) in topology.reboots.iter().enumerate() {
            if reboot.after_sample > completed_sample
                || completed_host_reboots.contains(&event_index)
                || !topology
                    .host_nodes
                    .iter()
                    .any(|host| host.name == reboot.node)
            {
                continue;
            }
            let runtime = host_children
                .iter_mut()
                .find(|runtime| runtime.name == reboot.node)
                .with_context(|| format!("host reboot references inactive node {}", reboot.node))?;
            let _ = runtime.child.kill();
            runtime
                .child
                .wait()
                .context("reaping rebooted host before releasing UART")?;
            for ((owner, _), guard) in &host_pty_guards {
                if owner == &reboot.node {
                    release_host_pty_exclusive(guard)?;
                }
            }
            let host = topology
                .host_nodes
                .iter()
                .find(|host| host.name == reboot.node)
                .expect("validated host reboot");
            *runtime = spawn_host_node(
                host,
                base,
                scratch.path(),
                &sample_path,
                &host_serial_paths,
                true,
            )?;
            completed_host_reboots.insert(event_index);
            println!(
                "[SIM] host node {} power-cycled after sample {} (persistent state retained)",
                reboot.node, reboot.after_sample
            );
        }
        thread::sleep(Duration::from_millis(20));
    };
    bridge_stop.store(true, Ordering::Release);
    let mut host_failures = Vec::new();
    let mut host_diagnostics = BTreeMap::new();
    let mut host_outputs = BTreeMap::new();
    for mut runtime in host_children {
        if let Some(status) = runtime.child.try_wait()? {
            let stdout = fs::read_to_string(&runtime.stdout_path).unwrap_or_default();
            let stderr = fs::read_to_string(&runtime.stderr_path).unwrap_or_default();
            host_failures.push(format!(
                "host node {} exited early with {status}; verify its serial-port and layout configuration\n{}",
                runtime.name,
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
            let _ = runtime.child.kill();
            let _ = runtime.child.wait();
        }
        let stdout = fs::read_to_string(&runtime.stdout_path).unwrap_or_default();
        let stderr = fs::read_to_string(&runtime.stderr_path).unwrap_or_default();
        let combined_host = [stdout, stderr].concat();
        let lines = combined_host.lines().collect::<Vec<_>>();
        let diagnostic = if lines.len() <= 120 {
            lines.join("\n")
        } else {
            let validation = lines
                .iter()
                .copied()
                .filter(|line| line.contains("full-bay"))
                .collect::<Vec<_>>();
            format!(
                "{}\n... {} host log lines omitted ...\n{}\n{}",
                lines[..60].join("\n"),
                lines.len() - 120,
                validation.join("\n"),
                lines[lines.len() - 60..].join("\n")
            )
        };
        host_outputs.insert(runtime.name.clone(), combined_host);
        host_diagnostics.insert(runtime.name, diagnostic);
    }
    let stdout = stdout_reader
        .join()
        .map_err(|_| anyhow::anyhow!("Renode stdout reader panicked"))??;
    let stderr = stderr_reader
        .join()
        .map_err(|_| anyhow::anyhow!("Renode stderr reader panicked"))??;
    let mut combined = format!("{stdout}{stderr}");
    // Only the CPU hook creates these records. Never infer boot success from
    // later telemetry or synthesize a marker when a hook was not observed.
    for (path, marker) in boot_captures {
        match fs::File::open(&path) {
            Ok(file) => {
                let mut record = String::new();
                std::io::Read::read_to_string(
                    &mut std::io::Read::take(file, marker.len() as u64 + 2),
                    &mut record,
                )?;
                ensure!(
                    record == format!("{marker}\n"),
                    "invalid boot capture for {marker}"
                );
                combined.push('\n');
                combined.push_str(&record);
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error).context("reading CPU boot capture"),
        }
    }
    if let Some(directory) = env::var_os("FIRMWARE_SIM_DIAGNOSTICS_DIR") {
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_nanos();
        let destination = PathBuf::from(directory).join(format!("bay-{stamp}"));
        fs::create_dir_all(&destination).context("creating bay diagnostics directory")?;
        fs::write(destination.join("renode.log"), &combined)?;
        fs::copy(&script_path, destination.join("bay.resc"))?;
        if probe_path.exists() {
            fs::copy(&probe_path, destination.join("memory-probes.txt"))?;
        }
        for (name, output) in &host_outputs {
            fs::write(destination.join(format!("{}-host.log", safe(name))), output)?;
        }
        for entry in fs::read_dir(scratch.path())? {
            let entry = entry?;
            if entry
                .path()
                .extension()
                .is_some_and(|extension| extension == "trace")
            {
                ensure!(
                    entry.metadata()?.len() <= 64 * 1024 * 1024,
                    "CPU trace exceeded the 64 MiB diagnostic artifact limit"
                );
                fs::copy(entry.path(), destination.join(entry.file_name()))?;
            }
        }
        println!(
            "[SIM] Bay diagnostics retained in {}",
            destination.display()
        );
    }

    ensure!(
        host_failures.is_empty(),
        "{}\nRenode output:\n{}",
        host_failures.join("; "),
        combined
            .lines()
            .rev()
            .take(80)
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .collect::<Vec<_>>()
            .join("\n")
    );
    let lower = combined.to_ascii_lowercase();
    if !status.success()
        || !combined
            .lines()
            .any(|line| line.trim() == "SEDS_BAY_COMPLETE")
        || lower.contains("there was an error executing command")
        || lower.contains("no such command or device")
        || lower.contains("parameters did not match")
        || lower.contains("seds_iwdg_reset")
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
    println!("[SIM] Final CPU positions:\n{}", register_dump.join("\n"));
    let probe_output = fs::read_to_string(&probe_path).context("reading bay probe capture")?;
    qualify_observations(
        topology,
        &node_layouts,
        &probe_output,
        &host_outputs,
        &host_diagnostics,
        register_dump,
    )
}

/// Re-evaluate a retained run with the production memory/link/host validators.
/// This does not execute firmware or change its captured register values.
pub fn check_capture(topology_path: &Path, capture: &Path) -> Result<BayReport> {
    let topology: BayTopology = serde_json::from_slice(&fs::read(topology_path)?)?;
    let console = fs::read_to_string(capture.join("renode.log"))?;
    ensure!(
        observed_marker(&console, "SEDS_BAY_COMPLETE"),
        "capture does not contain a completed bay run"
    );
    ensure!(
        !console.lines().any(console_execution_failed),
        "capture contains a simulator execution failure"
    );
    let mut layouts = Vec::new();
    for node in &topology.nodes {
        ensure!(
            observed_marker(&console, &format!("SEDS_NODE_BOOT_{}", safe(&node.name))),
            "capture lacks boot confirmation for {}",
            node.name
        );
        let path = if node.layout.is_absolute() {
            node.layout.clone()
        } else {
            topology_path
                .parent()
                .unwrap_or(Path::new("."))
                .join(&node.layout)
        };
        layouts.push((node.name.clone(), BoardLayout::load(&path)?));
    }
    let mut hosts = BTreeMap::new();
    for host in &topology.host_nodes {
        hosts.insert(
            host.name.clone(),
            fs::read_to_string(capture.join(format!("{}-host.log", safe(&host.name))))?,
        );
    }
    let probes = fs::read_to_string(capture.join("memory-probes.txt"))?;
    let mut registers = Vec::new();
    let lines: Vec<_> = console.lines().collect();
    for (index, line) in lines.iter().enumerate() {
        if line.trim().starts_with("SEDS_NODE ") {
            registers.push(line.trim().to_owned());
            if let Some(value) = lines.get(index + 1) {
                registers.push((*value).to_owned());
            }
        }
    }
    let mut report = qualify_observations(topology, &layouts, &probes, &hosts, &hosts, registers)?;
    report.capture_recheck = true;
    Ok(report)
}

fn qualify_observations(
    topology: BayTopology,
    node_layouts: &[(String, BoardLayout)],
    probe_output: &str,
    host_outputs: &BTreeMap<String, String>,
    host_diagnostics: &BTreeMap<String, String>,
    register_dump: Vec<String>,
) -> Result<BayReport> {
    let mut memory_profiles = BTreeMap::new();
    for (node_name, layout) in node_layouts {
        let profiles = parse_memory_profiles(
            node_name,
            layout,
            probe_output,
            topology.sample_count,
            topology.enforce_end_drop,
            &topology
                .reboots
                .iter()
                .filter(|event| &event.node == node_name)
                .map(|event| event.after_sample)
                .collect::<Vec<_>>(),
        )
        .and_then(|mut profiles| {
            // Controller lifetime loss counters include startup and survive
            // firmware resets. Never exclude them with a firmware warm-up.
            let mut hardware = layout.clone();
            hardware.execution.memory_probes = can_fifo_model_probes(layout);
            hardware.execution.memory_probe_warmup_samples = 0;
            profiles.extend(parse_memory_profiles(
                node_name, &hardware, probe_output, topology.sample_count, false, &[],
            )?);
            Ok(profiles)
        })
        .map_err(|error| {
            let host_detail = host_diagnostics
                .iter()
                .map(|(name, output)| format!("{name}:\n{output}"))
                .collect::<Vec<_>>()
                .join("\n");
            let probes = raw_probe_diagnostics(probe_output);
            anyhow::anyhow!("{error:#}\n\nAll-node probe observations:\n{probes}\n\nHost diagnostics:\n{host_detail}")
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
        let probe_detail = memory_profiles
            .iter()
            .flat_map(|(node, probes)| {
                probes
                    .iter()
                    .map(move |probe| format!("{node}.{}: {:?}", probe.name, probe.samples))
            })
            .collect::<Vec<_>>()
            .join("\n");
        bail!(
            "{} network endpoint check(s) failed:\n- {}\n\nEndpoint diagnostics:\n{}\n\nFirmware probe samples (oldest to newest):\n{}",
            link_failures.len(),
            link_failures.join("\n- "),
            diagnostics.join("\n"),
            probe_detail
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
        let observed = output.matches(&assertion.contains).count() as u32;
        if !assertion.accepts(observed) {
            assertion_failures.push(format!(
                "{}: host {} reported {:?} {} time(s), expected at least {} and at most {:?}",
                assertion.name,
                assertion.node,
                assertion.contains,
                observed,
                assertion.minimum_occurrences,
                assertion.maximum_occurrences
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
        ensure!(
            (assertion.minimum_gain.is_none() && assertion.maximum_gain.is_none())
                || (assertion.from_sample.is_some() && assertion.to_sample.is_some()),
            "assertion {} with minimum_gain or maximum_gain requires from_sample and to_sample",
            assertion.name
        );
        let observed = if assertion.minimum_gain.is_some() || assertion.maximum_gain.is_some() {
            let minimum_gain = assertion.minimum_gain.unwrap_or(0);
            let from_sample = assertion.from_sample.unwrap();
            let to_sample = assertion.to_sample.unwrap();
            ensure!(
                from_sample < to_sample,
                "assertion {} requires from_sample < to_sample",
                assertion.name
            );
            let from = *report.samples.get(from_sample).with_context(|| {
                format!(
                    "assertion {} requests from_sample {}, but {}.{} has {} samples",
                    assertion.name,
                    from_sample,
                    assertion.node,
                    assertion.probe,
                    report.samples.len()
                )
            })?;
            let to = *report.samples.get(to_sample).with_context(|| {
                format!(
                    "assertion {} requests to_sample {}, but {}.{} has {} samples",
                    assertion.name,
                    to_sample,
                    assertion.node,
                    assertion.probe,
                    report.samples.len()
                )
            })?;
            let gain = to.saturating_sub(from);
            if let Some(maximum_gain) = assertion.maximum_gain {
                if to < from || gain > maximum_gain {
                    assertion_failures.push(format!(
                        "{}: counter {} -> {} exceeds maximum gain {} or reset unexpectedly",
                        assertion.name, from, to, maximum_gain
                    ));
                }
            }
            if gain < minimum_gain {
                assertion_failures.push(format!(
                    "{}: {}.{} advanced {} ({} -> {}) between samples {} and {}, below {}",
                    assertion.name,
                    assertion.node,
                    assertion.probe,
                    gain,
                    from,
                    to,
                    from_sample,
                    to_sample,
                    minimum_gain
                ));
            }
            gain
        } else if let Some(sample) = assertion.sample {
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
        if assertion.minimum_gain.is_none() && assertion.maximum_gain.is_none() {
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
        let probe_detail = memory_profiles
            .iter()
            .flat_map(|(node, probes)| {
                probes
                    .iter()
                    .map(move |probe| format!("{node}.{}: {:?}", probe.name, probe.samples))
            })
            .collect::<Vec<_>>()
            .join("\n");
        bail!(
            "{} network assertion(s) failed:\n- {}\n\nLink observations:\n{}\n\nAssertion observations:\n{}\n\nFirmware probe samples (oldest to newest):\n{}\n\nHost diagnostics:\n{}",
            assertion_failures.len(),
            assertion_failures.join("\n- "),
            link_detail,
            assertion_detail,
            probe_detail,
            host_detail
        );
    }
    Ok(BayReport {
        capture_recheck: false,
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

// Export only after the final measurement, without advancing firmware time.
// The SD model preserves sparse holes and streams its written pages.
fn render_sd_card_captures(
    layouts: &[(String, BoardLayout)],
    directory: &Path,
    stamp: u128,
) -> String {
    let mut script = String::new();
    for (node, layout) in layouts {
        if !layout
            .peripherals
            .iter()
            .any(|p| p.model.as_deref() == Some("sd_card"))
        {
            continue;
        }
        let controller = if layout.architecture == crate::core::ArchitectureKind::Stm32h5 {
            "sdmmc"
        } else {
            "sdmmc1"
        };
        let name = safe(node);
        let path = directory.join(format!("sd-{name}-{stamp}.img"));
        let quoted =
            serde_json::to_string(&path.to_string_lossy()).expect("serialize SD capture path");
        script += &format!("mach set \"{name}\"\n{controller} SaveCardImage {quoted}\necho \"SEDS_BAY_SD_IMAGE {name} {stamp}\"\n");
    }
    script
}

fn can_fifo_model_probes(layout: &BoardLayout) -> Vec<crate::layout::MemoryProbe> {
    use crate::core::ArchitectureKind;
    let controllers: &[&str] = match layout.architecture {
        ArchitectureKind::Stm32g4 => &["fdcan1", "fdcan2"],
        ArchitectureKind::Stm32h5 | ArchitectureKind::Stm32u5 => &["fdcan1"],
        ArchitectureKind::Stm32 | ArchitectureKind::Stm32h7 => &[],
    };
    controllers
        .iter()
        .map(|controller| crate::layout::MemoryProbe {
            name: format!("can_fifo_overflow_{controller}"),
            symbol: (*controller).into(),
            address: None,
            byte_offset: 0,
            minimum: None,
            maximum: Some(0),
            max_end_drop: None,
            minimum_interval_gain: None,
            ignore_leading_zeroes: false,
        })
        .collect()
}

fn render_can_fifo_probe_capture(
    path: &Path,
    node: &str,
    probe: &str,
    sample: usize,
    controller: &str,
) -> Result<String> {
    let marker = format!("SEDS_BAY_PROBE {} {probe} {sample}", safe(node));
    let python = format!(
        "with open({}, 'a') as f: f.write({} + chr(10) + ('0x%08x' % monitor.Machine[{}].LifetimeReceivedOverflowFrames) + chr(10))",
        serde_json::to_string(&path.to_string_lossy())?,
        serde_json::to_string(&marker)?,
        serde_json::to_string(&format!("sysbus.{controller}"))?,
    );
    Ok(format!("python {}\n", serde_json::to_string(&python)?))
}

fn render_probe_capture(
    path: &Path,
    node: &str,
    probe: &str,
    sample: usize,
    symbol: &str,
    byte_offset: u32,
    address: Option<u64>,
) -> Result<String> {
    ensure!(
        byte_offset.is_multiple_of(4),
        "memory probe offset must be word aligned"
    );
    let expression = crate::layout::probe_address(symbol, address, byte_offset)?;
    let marker = format!("SEDS_BAY_PROBE {} {probe} {sample}", safe(node));
    let python = format!(
        "with open({}, 'a') as f: f.write({} + chr(10) + ('0x%08x' % monitor.Machine.SystemBus.ReadDoubleWord({})) + chr(10))",
        serde_json::to_string(&path.to_string_lossy())?,
        serde_json::to_string(&marker)?,
        expression,
    );
    Ok(format!("python {}\n", serde_json::to_string(&python)?))
}

fn validate_probe_indices(indexed: &[(usize, u32)], sample_count: usize) -> Result<()> {
    ensure!(
        indexed.len() == sample_count,
        "returned {} of {sample_count} samples",
        indexed.len()
    );
    for (expected, (actual, _)) in indexed.iter().enumerate() {
        ensure!(
            *actual == expected,
            "missing or duplicate sample: expected index {expected}, found {actual}"
        );
    }
    Ok(())
}

/// Trace only a short steady-state window. Stack tracing cannot follow every
/// RTOS context switch and can grow huge synthetic call stacks; flat binary PCs
/// have a fixed cost per guest instruction and are aggregated offline.
fn select_profile_interval(sample_count: usize, requested: Option<usize>) -> Result<usize> {
    ensure!(sample_count != 0, "profiling requires a nonzero sample count");
    let interval = requested.unwrap_or(usize::from(sample_count > 1));
    ensure!(interval < sample_count, "trace_sample must be less than sample_count");
    Ok(interval)
}

fn render_cpu_profile_step(directory: &Path, nodes: &[&str], duration_ms: u64) -> Result<String> {
    let capture_ms = duration_ms.min(20);
    let mut script = String::new();
    for node in nodes {
        let trace = directory.join(format!("profile-{}.trace", safe(node)));
        script += &format!(
            "mach set \"{}\"\ncpu CreateExecutionTracing \"seds-cpu-profile\" {} PC true\n",
            safe(node),
            serde_json::to_string(&trace.to_string_lossy())?
        );
    }
    script += &format!("emulation RunFor \"{}s\"\n", capture_ms as f64 / 1000.0);
    for node in nodes {
        script += &format!("mach set \"{}\"\ncpu DisableExecutionTracing\n", safe(node));
    }
    if duration_ms > capture_ms {
        script += &format!(
            "emulation RunFor \"{}s\"\n",
            (duration_ms - capture_ms) as f64 / 1000.0
        );
    }
    Ok(script)
}

fn raw_probe_diagnostics(output: &str) -> String {
    let mut observations = BTreeMap::<String, Vec<String>>::new();
    for block in output.split("SEDS_BAY_PROBE ").skip(1) {
        let mut lines = block.lines();
        let fields: Vec<_> = lines
            .next()
            .unwrap_or_default()
            .split_whitespace()
            .collect();
        if fields.len() != 3 {
            continue;
        }
        let value = lines
            .map(str::trim)
            .find_map(|line| u32::from_str_radix(line.strip_prefix("0x")?, 16).ok());
        observations
            .entry(format!("{}.{}", fields[0], fields[1]))
            .or_default()
            .push(format!(
                "{}={}",
                fields[2],
                value.map_or("missing".into(), |v| v.to_string())
            ));
    }
    observations
        .into_iter()
        .map(|(name, values)| format!("{name}: {}", values.join(", ")))
        .collect::<Vec<_>>()
        .join("\n")
}

fn parse_memory_profiles(
    node_name: &str,
    layout: &BoardLayout,
    output: &str,
    sample_count: usize,
    enforce_end_drop: bool,
    reset_after_samples: &[usize],
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
        validate_probe_indices(&indexed, sample_count)
            .with_context(|| format!("node {node_name} probe {}", probe.name))?;
        let samples: Vec<u32> = indexed.into_iter().map(|(_, value)| value).collect();
        ensure!(
            layout.execution.memory_probe_warmup_samples < samples.len(),
            "node {node_name} memory_probe_warmup_samples must be less than sample_count"
        );
        let mut qualified_samples = &samples[layout.execution.memory_probe_warmup_samples..];
        if probe.ignore_leading_zeroes {
            let Some(first_initialized) = qualified_samples.iter().position(|value| *value != 0)
            else {
                qualification_failures.push(format!(
                    "{} never produced an initialized value: {:?}",
                    probe.name, samples
                ));
                continue;
            };
            qualified_samples = &qualified_samples[first_initialized..];
        }
        let minimum_observed = *qualified_samples.iter().min().context("empty bay probe")?;
        let maximum_observed = *qualified_samples.iter().max().context("empty bay probe")?;
        // Use the same sustained-envelope comparison as the single-board
        // profiler. Comparing two arbitrary endpoints falsely classifies a
        // healthy allocator's periodic high/low oscillation as a leak.
        let epochs = probe_epochs(
            &samples,
            reset_after_samples,
            samples.len() - qualified_samples.len(),
        );
        let end_drop = epochs
            .iter()
            .map(|epoch| crate::execution::sustained_end_drop(epoch))
            .max()
            .unwrap_or(0);
        for epoch in epochs {
            if let Err(error) = crate::execution::validate_counter_progress(
                &probe.name,
                epoch,
                probe.minimum_interval_gain,
            ) {
                qualification_failures.push(error.to_string());
            }
        }
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
            address: probe.address,
            byte_offset: probe.byte_offset,
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

fn probe_epochs<'a>(samples: &'a [u32], resets: &[usize], first: usize) -> Vec<&'a [u32]> {
    let mut boundaries = resets
        .iter()
        .copied()
        .filter(|&index| index > first && index < samples.len())
        .collect::<Vec<_>>();
    boundaries.sort_unstable();
    boundaries.dedup();
    boundaries.push(samples.len());
    let mut start = first;
    boundaries
        .into_iter()
        .map(|end| {
            let epoch = &samples[start..end];
            start = end;
            epoch
        })
        .collect()
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
    fn bay_exports_sparse_sd_evidence_for_each_storage_node_only() {
        let plain: BoardLayout =
            serde_json::from_str(include_str!("../examples/stm32g4-board.json")).unwrap();
        let mut fc = plain.clone();
        fc.architecture = crate::core::ArchitectureKind::Stm32h5;
        fc.peripherals = serde_json::from_value(
            serde_json::json!([{"type":"storage", "name":"sd", "model":"sd_card", "bus":"sdmmc1"}]),
        )
        .unwrap();
        let mut daq = fc.clone();
        daq.architecture = crate::core::ArchitectureKind::Stm32u5;
        let script = render_sd_card_captures(
            &[
                ("gateway".into(), plain),
                ("flight".into(), fc),
                ("daq".into(), daq),
            ],
            Path::new("/tmp/sd evidence"),
            123,
        );
        assert_eq!(script.matches("SaveCardImage").count(), 2);
        assert!(script.contains("sdmmc SaveCardImage \"/tmp/sd evidence/sd-flight-123.img\""));
        assert!(script.contains("sdmmc1 SaveCardImage \"/tmp/sd evidence/sd-daq-123.img\""));
        assert!(!script.contains("sd-gateway"));
        assert!(!script.contains("RunFor"));
        assert!(script.contains("SEDS_BAY_SD_IMAGE flight 123"));
    }

    #[test]
    fn saved_capture_keeps_minimum_fault_and_completion_guards() {
        let directory = tempfile::tempdir().unwrap();
        let mut value: serde_json::Value =
            serde_json::from_str(include_str!("../examples/stm32g4-board.json")).unwrap();
        value["execution"]["memory_probe_warmup_samples"] = serde_json::json!(3);
        value["execution"]["memory_probes"] = serde_json::json!([
            {"name":"pool_available", "symbol":"pool", "minimum":20_000, "max_end_drop":4_096},
            {"name":"allocation_failures", "symbol":"faults", "maximum":0}
        ]);
        let layout: BoardLayout = serde_json::from_value(value.clone()).unwrap();
        let layout_path = directory.path().join("board.json");
        fs::write(&layout_path, serde_json::to_vec(&value).unwrap()).unwrap();
        let topology = serde_json::json!({"name":"recorded", "virtual_time_ms":600_000,
            "sample_count":12, "nodes":[{"name":"valve", "layout":layout_path,
                "firmware_root":directory.path()}]});
        let topology_path = directory.path().join("topology.json");
        fs::write(&topology_path, serde_json::to_vec(&topology).unwrap()).unwrap();
        let pools = [
            26_480, 26_468, 22_044, 26_192, 26_420, 26_404, 26_392, 26_316, 22_492, 22_492, 22_076,
            26_460,
        ];
        let capture = directory.path().join("capture");
        fs::create_dir(&capture).unwrap();
        let console = "SEDS_NODE_BOOT_valve\nSEDS_BAY_COMPLETE\n";
        fs::write(capture.join("renode.log"), console).unwrap();
        let render = |values: &[u32], fault: u32| {
            let mut text = String::new();
            for (sample, value) in values.iter().enumerate() {
                text += &format!("SEDS_BAY_PROBE valve pool_available {sample}\n0x{value:08x}\n");
                text +=
                    &format!("SEDS_BAY_PROBE valve allocation_failures {sample}\n0x{fault:08x}\n");
                for probe in can_fifo_model_probes(&layout) {
                    text += &format!("SEDS_BAY_PROBE valve {} {sample}\n0x00000000\n", probe.name);
                }
            }
            text
        };
        let good = render(&pools, 0);
        fs::write(capture.join("memory-probes.txt"), &good).unwrap();
        assert!(
            check_capture(&topology_path, &capture)
                .unwrap()
                .capture_recheck
        );
        let mut unsafe_dip = pools;
        unsafe_dip[7] = 1_000;
        fs::write(capture.join("memory-probes.txt"), render(&unsafe_dip, 0)).unwrap();
        assert!(check_capture(&topology_path, &capture)
            .unwrap_err()
            .to_string()
            .contains("fell below"));
        fs::write(capture.join("memory-probes.txt"), render(&pools, 1)).unwrap();
        assert!(check_capture(&topology_path, &capture)
            .unwrap_err()
            .to_string()
            .contains("exceeded"));
        fs::write(capture.join("memory-probes.txt"), &good).unwrap();
        fs::write(capture.join("renode.log"), "SEDS_NODE_BOOT_valve\n").unwrap();
        assert!(check_capture(&topology_path, &capture).is_err());
        fs::write(
            capture.join("renode.log"),
            format!("{console}SEDS_IWDG_RESET\n"),
        )
        .unwrap();
        assert!(check_capture(&topology_path, &capture).is_err());
    }

    #[test]
    fn guest_cpu_profile_can_target_a_warm_interval_without_changing_defaults() {
        assert_eq!(select_profile_interval(1, None).unwrap(), 0);
        assert_eq!(select_profile_interval(12, None).unwrap(), 1);
        assert_eq!(select_profile_interval(12, Some(6)).unwrap(), 6);
        assert_eq!(select_profile_interval(12, Some(11)).unwrap(), 11);
        assert!(select_profile_interval(12, Some(12)).is_err());
        assert!(select_profile_interval(0, None).is_err());
    }

    #[test]
    fn guest_cpu_trace_is_bounded_and_preserves_run_duration() {
        for duration in [1u64, 20, 21, 600_000] {
            let script =
                render_cpu_profile_step(Path::new("/tmp"), &["gateway", "daq"], duration).unwrap();
            let steps: Vec<f64> = script
                .lines()
                .filter_map(|line| line.strip_prefix("emulation RunFor \""))
                .map(|step| step.trim_end_matches("s\"").parse().unwrap())
                .collect();
            assert!(steps[0] <= 0.020);
            assert!((steps.iter().sum::<f64>() - duration as f64 / 1000.0).abs() < 1e-9);
            assert_eq!(script.matches("CreateExecutionTracing").count(), 2);
            assert_eq!(script.matches("DisableExecutionTracing").count(), 2);
            assert!(!script.contains("EnableProfiler"));
            if steps.len() == 2 {
                let disable = script.rfind("DisableExecutionTracing").unwrap();
                assert!(disable < script.rfind("emulation RunFor").unwrap());
            }
        }
    }

    #[test]
    fn controller_fifo_loss_is_captured_and_fails_the_gate() {
        let mut layout: BoardLayout =
            serde_json::from_str(include_str!("../examples/stm32g4-board.json")).unwrap();
        layout.execution.memory_probes = can_fifo_model_probes(&layout);
        layout.execution.memory_probe_warmup_samples = 0;
        let mut good = String::new();
        for sample in 0..2 {
            for probe in &layout.execution.memory_probes {
                good += &format!(
                    "SEDS_BAY_PROBE gateway {} {}\n0x00000000\n",
                    probe.name, sample
                );
            }
        }
        assert_eq!(
            parse_memory_profiles("gateway", &layout, &good, 2, false, &[])
                .unwrap()
                .len(),
            2
        );
        let lost = good.replacen("0x00000000", "0x00000001", 1);
        assert!(parse_memory_profiles("gateway", &layout, &lost, 2, false, &[]).is_err());
        let command =
            render_can_fifo_probe_capture(Path::new("/tmp/probes"), "gateway", "loss", 0, "fdcan1")
                .unwrap();
        assert!(command.contains("LifetimeReceivedOverflowFrames"));
        assert!(!command.contains("WriteDoubleWord"));
        assert!(!command.contains("print("));
    }

    #[test]
    fn array_probe_offsets_are_read_only_and_word_aligned() {
        let command = render_probe_capture(
            Path::new("/tmp/probes.txt"),
            "gateway",
            "stack",
            2,
            "g_stack",
            508,
            None,
        )
        .unwrap();
        assert!(command.contains(" + 508"));
        assert!(!command.contains("WriteDoubleWord"));
        assert!(render_probe_capture(
            Path::new("/tmp/probes.txt"),
            "gateway",
            "stack",
            2,
            "g_stack",
            3,
            None,
        )
        .is_err());
    }

    #[test]
    fn probe_capture_uses_a_private_file_and_never_the_console() {
        let command = render_probe_capture(
            Path::new("/tmp/probes.txt"),
            "rf",
            "pool",
            2,
            "g_pool",
            0,
            None,
        )
        .unwrap();
        assert!(command.starts_with("python "));
        assert!(command.contains("with open("));
        assert!(command.contains("ReadDoubleWord"));
        assert!(command.contains("GetSymbolAddress"));
        assert!(!command.contains("echo"));
        assert!(!command.contains("print("));
        assert_eq!(command.lines().count(), 1);
    }

    #[test]
    fn physical_register_probes_read_without_writes_or_fake_firmware_symbols() {
        let command = render_probe_capture(
            Path::new("/tmp/probes.txt"),
            "valve",
            "adc_cr",
            1,
            "",
            0,
            Some(0x50000408),
        )
        .unwrap();
        assert!(command.contains("ReadDoubleWord(0x50000408)"));
        assert!(!command.contains("WriteDoubleWord"));
        assert!(!command.contains("GetSymbolAddress"));
        let alias = render_probe_capture(
            Path::new("/tmp/probes.txt"),
            "valve",
            "init_fail",
            1,
            "data_acq_pressure_init_fail_count.lto_priv.0",
            0,
            None,
        )
        .unwrap();
        assert!(alias.contains("data_acq_pressure_init_fail_count.lto_priv.0"));
        assert!(!alias.contains("WriteDoubleWord"));
        for (symbol, address, offset) in [
            ("fake", Some(0x50000408), 0),
            ("", Some(0x50000409), 0),
            ("", Some(0xfffffffc), 4),
            ("", None, 0),
        ] {
            assert!(render_probe_capture(
                Path::new("/tmp/probes.txt"),
                "valve",
                "adc_cr",
                1,
                symbol,
                offset,
                address
            )
            .is_err());
        }
    }
    #[test]
    fn missing_duplicate_and_out_of_range_probe_indices_fail_closed() {
        assert!(validate_probe_indices(&[(0, 1), (1, 2)], 2).is_ok());
        for bad in [vec![(0, 1)], vec![(0, 1), (0, 2)], vec![(0, 1), (2, 2)]] {
            assert!(validate_probe_indices(&bad, 2).is_err());
        }
    }

    #[test]
    fn failing_probe_diagnostics_retain_other_boards_and_missing_samples() {
        let output = "SEDS_BAY_PROBE actuator pool 0\n0x100\nSEDS_BAY_PROBE valve rx 0\n0x12\nSEDS_BAY_PROBE valve rx 1\nmissing\n";
        assert_eq!(
            raw_probe_diagnostics(output),
            "actuator.pool: 0=256\nvalve.rx: 0=18, 1=missing"
        );
    }

    #[test]
    fn pico_rejects_bad_lengths_without_losing_next_frame_or_split_sync() {
        let mut state = PicoBridgeState::default();
        state
            .uart_rx
            .extend_from_slice(&[0xa5, 0x5a, 0xff, 0xff, 0, 0xa5]);
        process_gateway_uart_rx(&mut state).unwrap();
        assert_eq!(state.uart_framing_errors, 1);
        assert_eq!(state.uart_rx, [0xa5]);
        state.uart_rx.extend_from_slice(&[0x5a, 1, 0, 42]);
        process_gateway_uart_rx(&mut state).unwrap();
        assert_eq!(state.uart_to_i2c_frames, 1);
        assert_eq!(
            &next_host_i2c_slot(&mut state)[18..23],
            &[0xa5, 0x5a, 1, 0, 42]
        );
    }

    #[test]
    fn planned_reboots_split_probe_epochs_but_stalls_and_leaks_still_fail() {
        let counter = [100, 120, 5, 10];
        let epochs = probe_epochs(&counter, &[2], 0);
        assert_eq!(epochs, [&[100, 120][..], &[5, 10][..]]);
        for epoch in epochs {
            assert!(crate::execution::validate_counter_progress("loop", epoch, Some(1)).is_ok());
        }
        assert!(crate::execution::validate_counter_progress("loop", &counter, Some(1)).is_err());
        let stalled = [100, 120, 5, 5];
        assert!(crate::execution::validate_counter_progress(
            "loop",
            probe_epochs(&stalled, &[2], 0)[1],
            Some(1)
        )
        .is_err());
        let leaking = [1000, 900, 1000, 800];
        assert_eq!(
            probe_epochs(&leaking, &[2], 0)
                .iter()
                .map(|epoch| crate::execution::sustained_end_drop(epoch))
                .max(),
            Some(200)
        );
    }

    #[test]
    fn pico_tx_step_leaves_byte_pacing_to_virtual_uart() {
        let mut state = PicoBridgeState::default();
        let packet = vec![0xa5, 0x5a, 3, 0, 1, 2, 3];
        enqueue_gateway_uart_packet(&mut state, packet.clone());
        let mut bytes = Vec::new();
        step_gateway_uart_tx(&mut state, &mut bytes).unwrap();
        assert_eq!(bytes, packet);
        assert_eq!(state.uart_tx_bytes, 0);
        assert_eq!(state.uart_tx_offset, 0);
    }

    #[test]
    fn pico_host_disconnects_mid_slot_and_mid_reply_do_not_reset_device_state() {
        use std::time::Instant;
        fn expect_disconnect(session: &mut PicoHostSession, state: &mut PicoBridgeState) {
            let deadline = Instant::now() + Duration::from_secs(1);
            loop {
                match session.service(state) {
                    Ok(false) => return,
                    Err(error) => {
                        assert!(matches!(
                            error.kind(),
                            std::io::ErrorKind::BrokenPipe | std::io::ErrorKind::ConnectionReset
                        ));
                        return;
                    }
                    Ok(true) => {
                        assert!(Instant::now() < deadline, "closed host was not detected");
                        thread::sleep(Duration::from_millis(1));
                    }
                }
            }
        }
        let mut state = PicoBridgeState::default();
        enqueue_host_packet(&mut state, vec![0xA5, 0x5A, 1, 0, 42]);
        let (stream, mut peer) = UnixStream::pair().unwrap();
        let mut session = PicoHostSession::new(stream).unwrap();
        peer.write_all(b"Wpartial").unwrap();
        assert!(session.service(&mut state).unwrap());
        assert!(session.service(&mut state).unwrap());
        drop(peer);
        expect_disconnect(&mut session, &mut state);
        assert_eq!(state.host_packets.len(), 1);

        let (stream, mut peer) = UnixStream::pair().unwrap();
        let mut session = PicoHostSession::new(stream).unwrap();
        peer.write_all(b"R").unwrap();
        assert!(session.service(&mut state).unwrap());
        let mut slot = [0; 32];
        peer.read_exact(&mut slot).unwrap();
        assert_eq!(&slot[18..23], &[0xA5, 0x5A, 1, 0, 42]);
        peer.write_all(b"R").unwrap();
        // Fully close the peer: shutdown(Read) alone does not make the
        // opposite write fail on every Unix kernel (notably macOS).
        peer.shutdown(std::net::Shutdown::Both).unwrap();
        drop(peer);
        expect_disconnect(&mut session, &mut state);
    }

    #[test]
    fn i2c_poll_preserves_distinct_acks_and_telemetry_instead_of_latest_only() {
        let mut state = PicoBridgeState::default();
        let packets = [
            vec![0xa5, 0x5a, 3, 0, 1, 7, 0],
            vec![0xa5, 0x5a, 3, 0, 1, 8, 0],
            vec![0xa5, 0x5a, 3, 0, 42, 9, 0],
        ];
        for packet in &packets {
            enqueue_host_packet(&mut state, packet.clone());
        }
        for (index, packet) in packets.iter().enumerate() {
            let slot = next_host_i2c_slot(&mut state);
            assert_eq!(slot[4], PICO_I2C_START | PICO_I2C_END);
            assert_eq!(&slot[18..18 + packet.len()], packet.as_slice());
            assert_eq!(state.host_packets.len(), packets.len() - index - 1);
        }
        assert_eq!(state.host_packet_bytes, 0);
        assert_eq!(next_host_i2c_slot(&mut state), [0; PICO_I2C_SLOT_SIZE]);
    }

    #[test]
    fn i2c_v2_peek_partial_read_and_reconnect_keep_the_staged_packet() {
        let mut state = PicoBridgeState::default();
        ingest_host_i2c_v2(&mut state, &[0xd2, 0, 0, 0]).unwrap();
        assert_eq!(read_host_i2c_v2(&mut state, 4).unwrap(), [0xd2, 0, 0, 0]);
        for payload in [b"ack-one".as_slice(), b"ack-two", b"telemetry"] {
            let mut frame = vec![0xa5, 0x5a];
            frame.extend_from_slice(&(payload.len() as u16).to_le_bytes());
            frame.extend_from_slice(payload);
            enqueue_host_packet(&mut state, frame);
        }
        for payload in [b"ack-one".as_slice(), b"ack-two", b"telemetry"] {
            let header = [0xd2, 1, payload.len() as u8, 0];
            assert_eq!(read_host_i2c_v2(&mut state, 4).unwrap(), header);
            assert_eq!(read_host_i2c_v2(&mut state, 5).unwrap()[..4], header);
            // Re-selecting after a host reconnect must not discard the body.
            ingest_host_i2c_v2(&mut state, &[0xd2, 0, 0, 0]).unwrap();
            assert_eq!(
                &read_host_i2c_v2(&mut state, payload.len() + 4).unwrap()[4..],
                payload
            );
        }
        assert_eq!(state.host_packet_bytes, 0);
        assert_eq!(read_host_i2c_v2(&mut state, 4).unwrap(), [0xd2, 0, 0, 0]);
    }

    #[test]
    fn i2c_v2_write_wraps_raw_network_data_and_rejects_malformed_lengths() {
        let mut state = PicoBridgeState::default();
        ingest_host_i2c_v2(&mut state, &[0xd2, 1, 3, 0, 42, 43, 44]).unwrap();
        assert_eq!(
            state.uart_tx_packets.pop_front().unwrap(),
            [0xa5, 0x5a, 3, 0, 42, 43, 44]
        );
        for packet in [
            &[0xd2, 1, 3, 0, 42][..],
            &[0xd2, 0, 1, 0, 42],
            &[0xd2, 1, 0, 0],
            &[0xd2, 2, 0, 0],
        ] {
            assert!(ingest_host_i2c_v2(&mut state, packet).is_err());
        }
        assert!(read_host_i2c_v2(&mut state, 4097).is_err());
        assert!(read_host_i2c_v2(&mut state, 3).is_err());
    }

    #[test]
    fn i2c_v2_socket_transactions_handle_fragmented_prefixes_and_peek_body_reads() {
        let (mut host, bridge) = UnixStream::pair().unwrap();
        host.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        let worker = thread::spawn(move || {
            let mut state = PicoBridgeState::default();
            enqueue_host_packet(&mut state, vec![0xa5, 0x5a, 3, 0, 11, 12, 13]);
            let mut session = PicoHostSession::new(bridge).unwrap();
            while session.service(&mut state).unwrap() {
                thread::sleep(Duration::from_millis(1));
            }
            state
        });
        // Actual GroundStation v2 operations: w + u16 length + packet,
        // r + u16 length, followed by a separate full-body transaction.
        for byte in [b'w', 4, 0, 0xd2, 0, 0, 0] {
            host.write_all(&[byte]).unwrap();
        }
        let mut status = [1];
        host.read_exact(&mut status).unwrap();
        assert_eq!(status, [0]);
        for length in [4u16, 4, 7, 4] {
            host.write_all(b"r").unwrap();
            host.write_all(&length.to_le_bytes()).unwrap();
            let mut reply = vec![0; length as usize];
            host.read_exact(&mut reply).unwrap();
            match length {
                7 => assert_eq!(reply, [0xd2, 1, 3, 0, 11, 12, 13]),
                _ if reply[1] == 1 => assert_eq!(reply, [0xd2, 1, 3, 0]),
                _ => assert_eq!(reply, [0xd2, 0, 0, 0]),
            }
        }
        host.write_all(&[b'w', 7, 0, 0xd2, 1, 3, 0, 21, 22, 23])
            .unwrap();
        host.read_exact(&mut status).unwrap();
        assert_eq!(status, [0]);
        drop(host);
        let mut state = worker.join().unwrap();
        assert_eq!(
            state.uart_tx_packets.pop_front().unwrap(),
            [0xa5, 0x5a, 3, 0, 21, 22, 23]
        );
        assert_eq!(state.host_packet_bytes, 0);
    }
    #[test]
    fn mailbox_burst_keeps_sdt_template_and_dependent_data() {
        let mut state = PicoBridgeState::default();
        for byte in 0..32 {
            enqueue_host_packet(&mut state, vec![byte; 32]);
        }
        assert_eq!(state.host_packet_rejections, 0);
        for byte in 0..32 {
            assert_eq!(state.host_packets.pop_front().unwrap(), vec![byte; 32]);
        }
    }
    #[test]
    fn absent_i2c_host_keeps_bounded_fifo_packets_and_active_transfer() {
        let mut state = PicoBridgeState::default();
        for byte in 0..100 {
            enqueue_host_packet(&mut state, vec![byte; 100]);
        }
        assert_eq!(state.host_packets.len(), 81);
        assert_eq!(state.host_packet_bytes, 8100);
        let first = next_host_i2c_slot(&mut state);
        assert_eq!(first[18], 0);
        assert_eq!(state.host_packet_bytes, 8000);
        for _ in 0..100 {
            enqueue_host_packet(&mut state, vec![7; 4096]);
        }
        assert_eq!(state.host_packets.len(), 80);
        assert_eq!(state.host_packet_bytes, 8000);
        let continuation = next_host_i2c_slot(&mut state);
        assert_eq!(continuation[18], 0);
        assert_eq!(continuation[4] & PICO_I2C_START, 0);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn pico_uart_keeps_running_without_host_and_reconnects_with_split_sync() {
        use std::os::fd::FromRawFd;
        use std::sync::{
            atomic::{AtomicBool, Ordering},
            Arc,
        };
        let (mut master_fd, mut slave_fd) = (-1, -1);
        assert_eq!(
            unsafe {
                libc::openpty(
                    &mut master_fd,
                    &mut slave_fd,
                    std::ptr::null_mut(),
                    std::ptr::null(),
                    std::ptr::null(),
                )
            },
            0
        );
        let mut master = unsafe { fs::File::from_raw_fd(master_fd) };
        let slave = unsafe { fs::File::from_raw_fd(slave_fd) };
        configure_raw_nonblocking(master.as_raw_fd()).unwrap();
        let path = PathBuf::from(format!("/proc/self/fd/{}", slave.as_raw_fd()));
        let scratch = tempfile::tempdir().unwrap();
        let socket = scratch.path().join("pico.sock");
        let listener = UnixListener::bind(&socket).unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        let ready = Arc::new(AtomicBool::new(false));
        let (worker_stop, worker_ready) = (stop.clone(), ready.clone());
        let worker = thread::spawn(move || {
            run_pico_fi_bridge_until(listener, &path, PICO_UART_BAUD, || {
                worker_ready.store(true, Ordering::Release);
                worker_stop.load(Ordering::Acquire)
            })
        });
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        while !ready.load(Ordering::Acquire) {
            assert!(std::time::Instant::now() < deadline);
            thread::sleep(Duration::from_millis(1));
        }
        // More than the PTY capacity, with no GroundStation at all.
        let burst = [0xA5, 0x5A, 1, 0, 42].repeat(12_000);
        let mut sent = 0;
        while sent < burst.len() {
            match master.write(&burst[sent..]) {
                Ok(count) => sent += count,
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_millis(1))
                }
                Err(error) => panic!("{error}"),
            }
            assert!(
                std::time::Instant::now() < deadline,
                "UART blocked while host absent"
            );
        }
        thread::sleep(Duration::from_millis(20));
        for _ in 0..3 {
            let mut peer = UnixStream::connect(&socket).unwrap();
            peer.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
            // Release a retained mailbox slot before new upstream data; a
            // saturated disconnected link refuses new work, not old frames.
            peer.write_all(b"R").unwrap();
            let mut retained = [0; 32];
            peer.read_exact(&mut retained).unwrap();
            master.write_all(&[0xA5]).unwrap();
            thread::sleep(Duration::from_millis(10));
            master.write_all(&[0x5A, 1, 0, 43]).unwrap();
            thread::sleep(Duration::from_millis(10));
            let mut received_new = false;
            // FIFO preserves the bounded pre-reconnect telemetry instead of
            // destroying it when the first new packet arrives.
            for _ in 0..=PICO_HOST_MAILBOX_DEPTH {
                peer.write_all(b"R").unwrap();
                let mut slot = [0; 32];
                peer.read_exact(&mut slot).unwrap();
                if slot[22] == 43 {
                    assert_eq!(&slot[18..23], &[0xA5, 0x5A, 1, 0, 43]);
                    received_new = true;
                    break;
                }
                assert_eq!(&slot[18..23], &[0xA5, 0x5A, 1, 0, 42]);
            }
            assert!(
                received_new,
                "reconnection drains retained packets and reaches new data"
            );
            peer.write_all(b"Wpartial").unwrap();
            drop(peer);
            thread::sleep(Duration::from_millis(10));
        }
        stop.store(true, Ordering::Release);
        worker.join().unwrap().unwrap();
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn killed_host_exclusivity_is_released_without_recreating_radio_cable() {
        use std::os::fd::FromRawFd;
        let (mut master, mut slave) = (-1, -1);
        assert_eq!(
            unsafe {
                libc::openpty(
                    &mut master,
                    &mut slave,
                    std::ptr::null_mut(),
                    std::ptr::null(),
                    std::ptr::null(),
                )
            },
            0
        );
        let _master = unsafe { fs::File::from_raw_fd(master) };
        let host_port = unsafe { fs::File::from_raw_fd(slave) };
        let reboot_guard = host_port.try_clone().unwrap();
        let path = format!("/proc/self/fd/{}", reboot_guard.as_raw_fd());

        for _ in 0..3 {
            assert_eq!(
                unsafe { libc::ioctl(host_port.as_raw_fd(), libc::TIOCEXCL) },
                0
            );
            let error = OpenOptions::new()
                .read(true)
                .write(true)
                .open(&path)
                .unwrap_err();
            assert_eq!(error.raw_os_error(), Some(libc::EBUSY));
            release_host_pty_exclusive(&reboot_guard).unwrap();
            let reopened = OpenOptions::new()
                .read(true)
                .write(true)
                .open(&path)
                .unwrap();
            drop(reopened);
        }
        // Abrupt process death closes its fd without serialport's Drop ioctl.
        assert_eq!(
            unsafe { libc::ioctl(host_port.as_raw_fd(), libc::TIOCEXCL) },
            0
        );
        drop(host_port);
        assert_eq!(
            OpenOptions::new()
                .read(true)
                .write(true)
                .open(&path)
                .unwrap_err()
                .raw_os_error(),
            Some(libc::EBUSY)
        );
        release_host_pty_exclusive(&reboot_guard).unwrap();
        assert!(OpenOptions::new().read(true).write(true).open(path).is_ok());
    }

    #[test]
    fn console_command_failure_is_fatal_but_injected_device_errors_are_not() {
        assert!(console_execution_failed(
            "There was an error executing command 'include @board.repl'"
        ));
        assert!(console_execution_failed(
            "Parameters did not match the signature"
        ));
        assert!(console_execution_failed(
            "No such command or device: missingPeripheral"
        ));
        assert!(!console_execution_failed(
            "[WARNING] ADC injected read error"
        ));
        assert!(!console_execution_failed(
            "SEDS_BAY_PROBE daq sd_write_errors 0"
        ));
    }

    #[test]
    fn unmapped_cpu_sram_fails_without_hiding_missing_mmio_or_host_probes() {
        for address in ["20033000", "21f7a988", "30033000"] {
            assert!(console_execution_failed(&format!(
                "[WARNING] flight/sysbus: [cpu: 0x8044D16] WriteByte to non existing peripheral at 0x{address}, value 0x0.")));
        }
        for line in [
            "[WARNING] flight/sysbus: [cpu: 0x8044D16] WriteByte to non existing peripheral at 0x40012300, value 0x0.",
            "[WARNING] flight/sysbus: ReadDoubleWord from non existing peripheral at 0x21F7A988.",
            "[WARNING] flight/sysbus: [cpu: 0x8044D16] ReadByte from non existing peripheral at 0x22000000.",
            "[WARNING] flight/sysbus: [cpu: 0x8044D16] ReadByte from non existing peripheral at 0xnotanaddress.",
        ] { assert!(!console_execution_failed(line), "{line}"); }
    }

    #[test]
    fn console_stdin_guard_prevents_eof_during_long_emulation() {
        let mut child = Command::new("sh")
            .args(["-c", "read _line"])
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let guard = keep_console_stdin_open(&mut child).unwrap();

        thread::sleep(Duration::from_millis(25));
        assert!(child.try_wait().unwrap().is_none());

        drop(guard);
        assert_eq!(child.wait().unwrap().code(), Some(1));
    }

    #[test]
    fn unexpected_host_exit_is_failure_even_with_zero_status() {
        for code in [0, 7] {
            let mut host = HostRuntime {
                name: "groundstation".into(),
                child: Command::new("sh")
                    .args(["-c", &format!("exit {code}")])
                    .spawn()
                    .unwrap(),
                stdout_path: PathBuf::new(),
                stderr_path: PathBuf::new(),
            };
            host.child.wait().unwrap();
            let failure = exited_host(std::slice::from_mut(&mut host))
                .unwrap()
                .unwrap();
            assert!(failure.contains("groundstation"));
            assert!(failure.contains(&code.to_string()));
        }
    }

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
    fn dual_core_ethernet_mailboxes_match_production_depth_bytes_and_kind_cost() {
        let mut state = PicoBridgeState::ethernet();
        state.ethernet_connected = true;
        for _ in 0..128 {
            assert!(enqueue_gateway_uart_packet(&mut state, vec![1; 128]));
        }
        assert_eq!(state.uart_tx_bytes, 128 * 125);
        assert!(!enqueue_gateway_uart_packet(&mut state, vec![2; 5]));
        for n in 0..128 {
            enqueue_host_packet(&mut state, vec![n; 128]);
        }
        assert_eq!(state.host_packets.len(), 128);
        enqueue_host_packet(&mut state, vec![9; 5]);
        assert_eq!(state.host_packet_rejections, 1);
        assert_eq!(state.host_packets.front().unwrap()[0], 0);
        let mut state = PicoBridgeState::ethernet();
        state.ethernet_connected = true;
        for _ in 0..3 {
            assert!(enqueue_gateway_uart_packet(&mut state, vec![1; 4100]));
        }
        assert!(!enqueue_gateway_uart_packet(&mut state, vec![1; 4100]));
        assert!(enqueue_gateway_uart_packet(&mut state, vec![1; 4096]));
        assert_eq!(state.uart_tx_bytes, 16_384);
    }
    #[test]
    fn ethernet_inflight_uart_returns_credit_without_overwriting_partial_frame() {
        struct Blocked;
        impl Write for Blocked {
            fn write(&mut self, _: &[u8]) -> std::io::Result<usize> {
                Err(std::io::ErrorKind::WouldBlock.into())
            }
            fn flush(&mut self) -> std::io::Result<()> {
                Ok(())
            }
        }
        let mut s = PicoBridgeState::ethernet();
        assert!(enqueue_gateway_uart_packet(&mut s, vec![42; 128]));
        step_gateway_uart_tx(&mut s, &mut Blocked).unwrap();
        assert_eq!(s.uart_tx_bytes, 0);
        assert!(s.uart_tx_packets.is_empty());
        for _ in 0..128 {
            assert!(enqueue_gateway_uart_packet(&mut s, vec![43; 128]));
        }
        assert_eq!(s.uart_tx_active.as_ref().unwrap(), &vec![42; 128]);
        let mut output = Vec::new();
        step_gateway_uart_tx(&mut s, &mut output).unwrap();
        assert_eq!(output, vec![42; 128]);
        assert!(s.uart_tx_active.is_none());
    }
    #[test]
    fn disconnected_ethernet_upload_does_not_replay_stale_telemetry() {
        let mut s = PicoBridgeState::ethernet();
        enqueue_host_packet(&mut s, vec![1; 5]);
        assert!(s.host_packets.is_empty());
        s.ethernet_connected = true;
        enqueue_host_packet(&mut s, vec![2; 5]);
        assert_eq!(s.host_packets.len(), 1);
        s.clear_disconnected_upload();
        assert!(s.host_packets.is_empty());
        assert_eq!(s.host_packet_bytes, 0);
    }

    #[test]
    fn gateway_backpressure_does_not_reject_a_valid_mailbox_packet() {
        let mut state = PicoBridgeState::default();
        enqueue_gateway_uart_packet(&mut state, vec![1; 128]);
        assert_eq!(state.uart_tx_packets.len(), 1);
        assert_eq!(state.uart_tx_bytes, 128);
    }

    #[test]
    fn gateway_queue_overflow_preserves_in_flight_uart_frame() {
        for sent in 1..7 {
            let first = vec![0xA5, 0x5A, 3, 0, 1, 2, 3];
            let mut state = PicoBridgeState::default();
            enqueue_gateway_uart_packet(&mut state, first.clone());
            let mut wire = first[..sent].to_vec();
            state.uart_tx_offset = sent;
            for value in 0..32 {
                enqueue_gateway_uart_packet(&mut state, vec![0xA5, 0x5A, 1, 0, value]);
                assert!(state.uart_tx_packets.len() <= PICO_PACKET_QUEUE_DEPTH);
                assert!(state.uart_tx_bytes <= PICO_UART_QUEUE_BYTES);
            }
            while !state.uart_tx_packets.is_empty() {
                step_gateway_uart_tx(&mut state, &mut wire).unwrap();
            }
            assert_eq!(&wire[..first.len()], &first);
            assert_eq!(&wire[first.len()..first.len() + 5], &[0xA5, 0x5A, 1, 0, 0]);
            assert_eq!(state.uart_tx_bytes, 0);
        }
    }

    #[test]
    fn gateway_byte_budget_preserves_in_flight_uart_frame() {
        let mut state = PicoBridgeState::default();
        let first = vec![42; PICO_UART_MAX_FRAME];
        enqueue_gateway_uart_packet(&mut state, first.clone());
        state.uart_tx_offset = 1;
        for _ in 1..PICO_PACKET_QUEUE_DEPTH {
            assert!(enqueue_gateway_uart_packet(
                &mut state,
                vec![43; PICO_UART_MAX_FRAME]
            ));
        }
        let accepted = state.uart_tx_packets.clone();
        assert!(!enqueue_gateway_uart_packet(
            &mut state,
            vec![44; PICO_UART_MAX_FRAME]
        ));
        assert_eq!(state.uart_tx_packets, accepted);
        assert_eq!(state.uart_tx_packets.front(), Some(&first));
        assert_eq!(state.uart_tx_offset, 1);
        assert!(state.uart_tx_bytes <= PICO_UART_QUEUE_BYTES);
    }

    #[test]
    fn gateway_uart_partial_write_and_backpressure_preserve_frame() {
        struct Limited {
            bytes: Vec<u8>,
            blocked: bool,
        }
        impl Write for Limited {
            fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
                if self.blocked {
                    return Err(std::io::ErrorKind::WouldBlock.into());
                }
                let n = bytes.len().min(2);
                self.bytes.extend_from_slice(&bytes[..n]);
                Ok(n)
            }
            fn flush(&mut self) -> std::io::Result<()> {
                Ok(())
            }
        }
        let packet = vec![0xa5, 0x5a, 3, 0, 1, 2, 3];
        let mut state = PicoBridgeState::default();
        enqueue_gateway_uart_packet(&mut state, packet.clone());
        let mut wire = Limited {
            bytes: Vec::new(),
            blocked: false,
        };
        step_gateway_uart_tx(&mut state, &mut wire).unwrap();
        assert_eq!(state.uart_tx_offset, 2);
        wire.blocked = true;
        step_gateway_uart_tx(&mut state, &mut wire).unwrap();
        assert_eq!(state.uart_tx_offset, 2);
        assert_eq!(state.uart_tx_bytes, packet.len());
        wire.blocked = false;
        while !state.uart_tx_packets.is_empty() {
            step_gateway_uart_tx(&mut state, &mut wire).unwrap();
        }
        assert_eq!(wire.bytes, packet);
        assert_eq!(state.uart_tx_bytes, 0);
    }

    #[test]
    fn forbidden_stale_markers_fail_even_when_minimum_is_zero() {
        let assertion: HostLogAssertion = serde_json::from_value(serde_json::json!({
            "name":"no stale telemetry","node":"groundstation","contains":"stale",
            "minimum_occurrences":0,"maximum_occurrences":0
        }))
        .unwrap();
        assert!(assertion.accepts(0));
        assert!(!assertion.accepts(1));
        assert!(!assertion.accepts(10));
    }

    #[test]
    fn topology_accepts_can_ack_faults_and_late_progress_assertions() {
        let topology: BayTopology = serde_json::from_value(serde_json::json!({
            "name": "fault-rejoin-soak",
            "virtual_time_ms": 600000,
            "sample_count": 12,
            "nodes": [],
            "can_ack_events": [
                {"node": "rf", "peripheral": "fdcan2", "after_sample": 3,
                 "acknowledged": false},
                {"node": "rf", "peripheral": "fdcan2", "after_sample": 4,
                 "acknowledged": true}
            ],
            "assertions": [{
                "name": "late transport progress",
                "node": "rf",
                "probe": "fdcan_tx_ok",
                "minimum_gain": 1,
                "from_sample": 9,
                "to_sample": 11
            }],
            "host_log_assertions": [{
                "name": "every soak command returned",
                "node": "groundstation",
                "contains": "soak command acknowledged",
                "minimum_occurrences": 11
            }]
        }))
        .unwrap();

        assert_eq!(topology.can_ack_events.len(), 2);
        assert!(!topology.can_ack_events[0].acknowledged);
        assert_eq!(topology.assertions[0].minimum_gain, Some(1));
        assert_eq!(topology.assertions[0].from_sample, Some(9));
        assert_eq!(topology.assertions[0].to_sample, Some(11));
        assert_eq!(topology.host_log_assertions[0].minimum_occurrences, 11);
    }

    #[test]
    fn can_link_loss_detaches_reception_and_restores_acknowledgements() {
        let mut event = CanLinkEvent {
            node: "valve".into(),
            peripheral: "fdcan2".into(),
            link: "fill_can".into(),
            after_sample: 0,
            connected: false,
        };
        let detached = render_can_link_event(&event);
        assert!(detached.contains("connector Disconnect sysbus.fdcan2 fill_can"));
        assert!(detached.contains("Acknowledged false"));
        event.connected = true;
        event.after_sample = 1;
        let attached = render_can_link_event(&event);
        assert!(attached.contains("connector Connect sysbus.fdcan2 fill_can"));
        assert!(attached.contains("Acknowledged true"));
        assert!(attached.contains("SEDS_BAY_CAN_LINK valve fill_can 1 true"));
    }
    #[test]
    fn simulator_failure_closes_console_pipes_held_by_runtime_children() {
        let mut child = Command::new("sh")
            .args(["-c", "sleep 60 & printf R; wait"])
            .process_group(0)
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let mut output = child.stdout.take().unwrap();
        let mut ready = [0u8];
        output.read_exact(&mut ready).unwrap();
        assert_eq!(ready, [b'R']); // The runtime descendant exists before termination.
        let (tx, rx) = mpsc::channel();
        let reader = thread::spawn(move || {
            let mut bytes = Vec::new();
            let result = output.read_to_end(&mut bytes);
            tx.send(result).unwrap();
        });
        terminate_simulator(&mut child);
        child.wait().unwrap();
        rx.recv_timeout(Duration::from_secs(2))
            .expect("runtime descendant kept the console open")
            .unwrap();
        reader.join().unwrap();
    }
}

#[cfg(test)]
mod checkpoint_diagnostic_tests {
    #[test]
    fn checkpoint_register_reads_do_not_mutate_cpu_or_use_final_parser_markers() {
        let script = super::render_checkpoint_registers("rf", 2);
        assert_eq!(script.matches("cpu GetRegister ").count(), 16);
        assert_eq!(script.matches("sysbus ReadDoubleWord ").count(), 3);
        assert!(script.contains("SEDS_CHECKPOINT 2 rf R15"));
        assert!(script.contains("0xe000ed28"));
        assert!(!script.contains("SEDS_NODE"));
        assert!(!script.contains("Write"));
        assert!(!script.contains("Reset"));
    }
}
