//! Native SITL bay: the same topology as `bay`, but every firmware node is the board's production code compiled
//! for the host (native/build_board.py) and run as a process on one virtual clock. This coordinator owns time:
//! it grants nodes a common horizon, routes what they sent (CAN frames on the shared buses with the topology's
//! link cuts, UART bytes to the modeled Pico-Fi), applies scheduled reboots and stimulus files, samples memory
//! probes from the processes' globals once per virtual second (same memory-probes.txt as the Renode bay) and
//! paces virtual time against the wall clock so a host station (GroundStation under a matching time-dilation
//! factor) shares the time base.
use super::*;
use std::io::IoSlice;

use std::time::Instant;

const MSG_HELLO: u16 = 1;
const MSG_DONE: u16 = 2;
const MSG_CAN_TX: u16 = 3;
const MSG_UART_TX: u16 = 4;
const MSG_GPIO: u16 = 5;
const MSG_RESET: u16 = 6;
const MSG_GRANT: u16 = 101;
const MSG_CAN_RX: u16 = 102;
const MSG_UART_RX: u16 = 103;
const MSG_ADC: u16 = 104;
const MSG_LINK: u16 = 105;
const MSG_STOP: u16 = 106;

#[derive(Debug, Deserialize, Clone)]
pub struct NativeNode {
    pub exe: PathBuf,
    #[serde(default)]
    pub flash_init: Option<PathBuf>,
    #[serde(default = "default_flash")]
    pub flash_size: u64,
    #[serde(default)]
    pub devices: Option<PathBuf>,
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    #[serde(default)]
    pub drift_ppm: i64,
    #[serde(default)]
    pub symbols: Option<PathBuf>,
}
fn default_flash() -> u64 {
    512 * 1024
}
#[derive(Debug, Deserialize, Clone)]
pub struct Stimulus {
    pub node: String,
    pub device: String,
    pub path: PathBuf,
}
#[derive(Debug, Deserialize, Clone)]
pub struct Probe {
    pub node: String,
    pub name: String,
    pub address: u32,
    #[serde(default = "default_width")]
    pub width: u32,
}
fn default_width() -> u32 {
    4
}
#[derive(Debug, Deserialize, Clone)]
pub struct UartGate {
    pub node: String,
    pub peripheral: String,
    pub path: PathBuf,
}
#[derive(Debug, Deserialize)]
pub struct NativeConfig {
    pub nodes: BTreeMap<String, NativeNode>,
    /// Virtual seconds per wall second (0 = as fast as possible; only without host nodes).
    #[serde(default)]
    pub pace: f64,
    #[serde(default = "default_quantum_us")]
    pub quantum_us: u64,
    #[serde(default = "default_skip_us")]
    pub max_skip_us: u64,
    pub scratch: PathBuf,
    pub diagnostics: PathBuf,
    #[serde(default)]
    pub stimulus: Vec<Stimulus>,
    #[serde(default)]
    pub probes: Vec<Probe>,
    #[serde(default)]
    pub uart_gates: Vec<UartGate>,
    #[serde(default)]
    pub seed: u64,
    #[serde(default)]
    pub can_trace: bool,
    /// Shared page the host station's clocks follow (native/simclock): the station runs on the bay's virtual time.
    #[serde(default)]
    pub station_clock: Option<PathBuf>,
}

/// The station clock page (layout shared with native/simclock/simclock.c).
struct StationClock {
    map: *mut u64,
    rate_milli: u64,
    mark: (u64, Instant),
}
impl StationClock {
    fn open(path: &Path, initial_rate: f64) -> Result<Self> {
        let f = OpenOptions::new().create(true).read(true).write(true).truncate(true).open(path)?;
        f.set_len(4096)?;
        let map = unsafe { libc::mmap(std::ptr::null_mut(), 4096, libc::PROT_READ | libc::PROT_WRITE, libc::MAP_SHARED, f.as_raw_fd(), 0) };
        ensure!(map != libc::MAP_FAILED, "mapping the station clock page");
        let mut c = Self { map: map as *mut u64, rate_milli: (initial_rate.max(0.1) * 1000.0) as u64, mark: (0, Instant::now()) };
        c.publish(0, 0);
        Ok(c)
    }
    fn real_mono_ns() -> u64 {
        let mut ts = libc::timespec { tv_sec: 0, tv_nsec: 0 };
        unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut ts) };
        ts.tv_sec as u64 * 1_000_000_000 + ts.tv_nsec as u64
    }
    /// The window [t, horizon] the station may observe next, and the measured virtual/wall rate.
    fn publish(&mut self, t: u64, horizon: u64) {
        let el = self.mark.1.elapsed();
        if el >= Duration::from_millis(100) {
            let r = ((t - self.mark.0) as f64 / el.as_nanos() as f64 * 1000.0) as u64;
            self.rate_milli = (self.rate_milli * 3 + r.max(50)) / 4;
            self.mark = (t, Instant::now());
        }
        unsafe {
            let seq = std::ptr::read_volatile(self.map);
            std::ptr::write_volatile(self.map, seq + 1);
            std::sync::atomic::fence(Ordering::SeqCst);
            std::ptr::write_volatile(self.map.add(1), t);
            std::ptr::write_volatile(self.map.add(2), horizon);
            std::ptr::write_volatile(self.map.add(3), Self::real_mono_ns());
            std::ptr::write_volatile(self.map.add(4), self.rate_milli);
            std::sync::atomic::fence(Ordering::SeqCst);
            std::ptr::write_volatile(self.map, seq + 2);
        }
    }
}
fn default_quantum_us() -> u64 {
    1000
}
fn default_skip_us() -> u64 {
    20_000
}

struct Proc {
    name: String,
    child: Child,
    sock: UnixStream,
    next_wakeup: u64,
    boots: u32,
    inbound: Vec<(u64, u16, Vec<u8>)>,
    alive: bool,
    pending_reset: Option<u8>,
}

fn port8(name: &str) -> [u8; 8] {
    let mut p = [0u8; 8];
    for (i, b) in name.bytes().take(8).enumerate() {
        p[i] = b;
    }
    p
}
fn port_str(p: &[u8]) -> String {
    String::from_utf8_lossy(p).trim_end_matches('\0').to_string()
}

fn write_msg(sock: &mut UnixStream, ty: u16, payload: &[u8]) -> std::io::Result<()> {
    let len = (payload.len() + 2) as u32;
    let head = [len.to_le_bytes().as_slice(), ty.to_le_bytes().as_slice()].concat();
    let mut bufs = [IoSlice::new(&head), IoSlice::new(payload)];
    let total = head.len() + payload.len();
    let mut written = 0;
    while written < total {
        let n = sock.write_vectored(&bufs)?;
        if n == 0 {
            return Err(std::io::ErrorKind::WriteZero.into());
        }
        written += n;
        IoSlice::advance_slices(&mut &mut bufs[..], n);
    }
    Ok(())
}
fn read_msg(sock: &mut UnixStream) -> std::io::Result<(u16, Vec<u8>)> {
    let mut head = [0u8; 6];
    sock.read_exact(&mut head)?;
    let len = u32::from_le_bytes(head[..4].try_into().unwrap()) as usize;
    let ty = u16::from_le_bytes(head[4..6].try_into().unwrap());
    let mut body = vec![0u8; len.saturating_sub(2)];
    sock.read_exact(&mut body)?;
    Ok((ty, body))
}

fn spawn_node(name: &str, n: &NativeNode, cfg: &NativeConfig, t_global: u64, cause: u8, boots: u32) -> Result<Proc> {
    let (parent, child_sock) = UnixStream::pair()?;
    let state = cfg.scratch.join(format!("{name}-state"));
    fs::create_dir_all(&state)?;
    let log = OpenOptions::new()
        .create(true)
        .append(true)
        .open(cfg.diagnostics.join(format!("native-{name}.log")))?;
    let fd = child_sock.as_raw_fd();
    let node_nice: i32 = env::var("FIRMWARE_SIM_NODE_NICE").ok().and_then(|v| v.parse().ok()).unwrap_or(10);
    let mut cmd = Command::new(&n.exe);
    cmd.env("SIM_NODE", name)
        .env("SIM_FD", fd.to_string())
        .env("SIM_STATE_DIR", &state)
        .env("SIM_BOOT_GLOBAL_NS", t_global.to_string())
        .env("SIM_RESET_CAUSE", cause.to_string())
        .env("SIM_FLASH_SIZE", n.flash_size.to_string())
        .env("SIM_DRIFT_PPM", n.drift_ppm.to_string())
        .env("SIM_SEED", (cfg.seed.wrapping_mul(1000) + boots as u64).to_string())
        .envs(&n.env)
        .stdin(Stdio::null())
        .stdout(Stdio::from(log.try_clone()?))
        .stderr(Stdio::from(log));
    if let Some(p) = &n.flash_init {
        cmd.env("SIM_FLASH_INIT", p);
    }
    if let Some(p) = &n.devices {
        cmd.env("SIM_DEVICES", p);
    }
    unsafe {
        cmd.pre_exec(move || {
            libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL);
            // Board processes yield the host CPU to the station: its compute is not lockstepped, so preempting it
            // stretches its work in virtual time.
            libc::nice(node_nice);
            let flags = libc::fcntl(fd, libc::F_GETFD);
            libc::fcntl(fd, libc::F_SETFD, flags & !libc::FD_CLOEXEC);
            Ok(())
        });
    }
    let child = cmd.spawn().with_context(|| format!("starting native node {name} ({})", n.exe.display()))?;
    drop(child_sock);
    let mut p = Proc {
        name: name.to_string(),
        child,
        sock: parent,
        next_wakeup: t_global,
        boots,
        inbound: Vec::new(),
        alive: true,
        pending_reset: None,
    };
    // HELLO then the first DONE (the boot horizon).
    loop {
        let (ty, body) = read_msg(&mut p.sock).with_context(|| format!("node {name} did not start"))?;
        if ty == MSG_DONE {
            p.next_wakeup = u64::from_le_bytes(body[8..16].try_into().unwrap());
            break;
        }
        ensure!(ty == MSG_HELLO, "node {name}: unexpected first message {ty}");
    }
    Ok(p)
}

struct CanEndpoint {
    link: String,
    node: String,
    peripheral: String,
    connected: bool,
}

fn atomic_write(path: &Path, text: &str) {
    let tmp = path.with_extension("tmp");
    if fs::write(&tmp, text).is_ok() {
        let _ = fs::rename(&tmp, path);
    }
}

pub fn run_native(topology_path: &Path, config_path: &Path) -> Result<()> {
    let topology: BayTopology = serde_json::from_str(&fs::read_to_string(topology_path)?)
        .with_context(|| format!("reading {}", topology_path.display()))?;
    let cfg: NativeConfig = serde_json::from_str(&fs::read_to_string(config_path)?)
        .with_context(|| format!("reading {}", config_path.display()))?;
    fs::create_dir_all(&cfg.scratch)?;
    let live = cfg.diagnostics.join("bay-live-native");
    fs::create_dir_all(&live)?;
    let clock_dir = cfg.diagnostics.join("temp/native");
    fs::create_dir_all(&clock_dir)?;
    let clock_file = cfg.scratch.join("virtual-clock.txt");
    let clock_mirror = clock_dir.join("virtual-clock.txt");
    atomic_write(&clock_file, "0\n");
    atomic_write(&clock_mirror, "0\n");
    let mirror_distinct = fs::canonicalize(&clock_mirror).ok() != fs::canonicalize(&clock_file).ok();
    let mut probes_out = OpenOptions::new().create(true).append(true).open(live.join("memory-probes.txt"))?;
    let mut gpio_out = std::io::BufWriter::new(OpenOptions::new().create(true).append(true).open(cfg.diagnostics.join("native-gpio.log"))?);
    let mut can_trace = if cfg.can_trace {
        Some(std::io::BufWriter::new(OpenOptions::new().create(true).append(true).open(cfg.diagnostics.join("native-can-trace.txt"))?))
    } else {
        None
    };
    let duration_ns = topology.virtual_time_ms.saturating_mul(1_000_000);
    let quantum = cfg.quantum_us.max(10) * 1000;
    let max_skip = cfg.max_skip_us.max(cfg.quantum_us) * 1000;

    // CAN buses and the Pico-Fi link (firmware side) from the topology.
    let mut can: Vec<CanEndpoint> = Vec::new();
    let mut pico: Option<(String, String, String, u64)> = None; // (link, node, peripheral, baud)
    for link in &topology.links {
        match link.kind {
            LinkKind::Can => {
                for ep in &link.endpoints {
                    if cfg.nodes.contains_key(&ep.node) {
                        can.push(CanEndpoint { link: link.name.clone(), node: ep.node.clone(), peripheral: ep.peripheral.clone(), connected: true });
                    }
                }
            }
            LinkKind::PicoFi => {
                if let Some(ep) = link.endpoints.iter().find(|e| cfg.nodes.contains_key(&e.node)) {
                    pico = Some((link.name.clone(), ep.node.clone(), ep.peripheral.clone(), link.baud_rate));
                }
            }
            _ => {}
        }
    }
    // Scheduled events (virtual seconds).
    for ev in &topology.can_link_events {
        if ev.after_sample == 0 {
            if let Some(e) = can.iter_mut().find(|e| e.node == ev.node && e.peripheral == ev.peripheral && e.link == ev.link) {
                e.connected = ev.connected;
            }
        }
    }

    // Pico-Fi: the gateway UART talks to an event-driven Pico model (discovery + TCP to the station).
    let stop = Arc::new(AtomicBool::new(false));
    let mut host_serial_paths: BTreeMap<(String, String), PathBuf> = BTreeMap::new();
    let mut pico_model: Option<super::native_pico::NativePico> = None;
    if let Some((link_name, _, _, _)) = &pico {
        if let Some(host) = topology.host_nodes.iter().find(|h| h.serial_links.iter().any(|s| &s.link == link_name)) {
            let reservation = std::net::UdpSocket::bind("127.0.0.1:0")?;
            let port = reservation.local_addr()?.port();
            drop(reservation);
            host_serial_paths.insert((host.name.clone(), link_name.clone()), PathBuf::from(port.to_string()));
            pico_model = Some(super::native_pico::NativePico::new(port)?);
        }
    }

    // Radio links: one shared air medium per link; host stations reach it through a PTY (their serial port).
    let mut radios: Vec<(super::native_radio::Medium, Vec<Option<fs::File>>, Vec<(String, String)>)> = Vec::new();
    for link in topology.links.iter().filter(|l| matches!(l.kind, LinkKind::Radio)) {
        let profile = link.radio.clone().unwrap_or(super::radio_link::RadioProfile {
            air_bits_per_second: 64000, uart_baud: 115200, tx_fifo_bytes: 8192, rx_fifo_bytes: 8192, max_burst_bytes: 256,
            packet_idle_us: 2000, overhead_bits: 160, turn_us: 2000 });
        let names: Vec<String> = link.endpoints.iter().map(|e| e.node.clone()).collect();
        let mut masters = Vec::new();
        let mut eps = Vec::new();
        for ep in &link.endpoints {
            eps.push((ep.node.clone(), ep.peripheral.clone()));
            if topology.host_nodes.iter().any(|h| h.name == ep.node) {
                let (mut master, mut slave) = (-1, -1);
                let mut name = [0 as libc::c_char; 256];
                ensure!(unsafe { libc::openpty(&mut master, &mut slave, name.as_mut_ptr(), std::ptr::null_mut(), std::ptr::null_mut()) } == 0, "openpty");
                configure_raw_nonblocking(master)?;
                configure_raw_nonblocking(slave)?;
                let path = PathBuf::from(unsafe { std::ffi::CStr::from_ptr(name.as_ptr()) }.to_str()?);
                std::mem::forget(unsafe { <fs::File as std::os::fd::FromRawFd>::from_raw_fd(slave) });
                host_serial_paths.insert((ep.node.clone(), link.name.clone()), path);
                masters.push(Some(unsafe { <fs::File as std::os::fd::FromRawFd>::from_raw_fd(master) }));
            } else {
                masters.push(None);
            }
        }
        let mut m = super::native_radio::Medium::new(&link.name, profile, &names, cfg.seed);
        for ev in topology.radio_link_events.iter().filter(|e| e.link == link.name && e.after_sample == 0) {
            m.cut.insert((ev.sender.clone(), ev.receiver.clone()), !ev.connected);
            if let Some(l) = ev.loss_ppm { m.loss_ppm.insert((ev.sender.clone(), ev.receiver.clone()), l); }
        }
        radios.push((m, masters, eps));
    }

    // Nodes.
    let mut procs: Vec<Proc> = Vec::new();
    for node in &topology.nodes {
        let n = cfg.nodes.get(&node.name).with_context(|| format!("no native build for node {}", node.name))?;
        procs.push(spawn_node(&node.name, n, &cfg, 0, 0, 0)?);
    }
    let push_link_state = |procs: &mut Vec<Proc>, can: &Vec<CanEndpoint>, t: u64| {
        for p in procs.iter_mut() {
            for e in can.iter().filter(|e| e.node == p.name) {
                let acked = e.connected && can.iter().any(|o| o.link == e.link && o.node != e.node && o.connected);
                let mut m = port8(&e.peripheral).to_vec();
                m.push(acked as u8);
                p.inbound.push((t, MSG_LINK, m));
            }
        }
    };
    push_link_state(&mut procs, &can, 0);

    // Host nodes (GroundStation) start with virtual time 0.
    let scratch = cfg.scratch.clone();
    let sample_path = scratch.join("sample.txt");
    atomic_write(&sample_path, "0\n");
    let mut station_clock = match &cfg.station_clock {
        Some(p) => Some(StationClock::open(p, if cfg.pace > 0.0 { cfg.pace } else { 10.0 })?),
        None => None,
    };
    let mut hosts = Vec::new();
    for host in &topology.host_nodes {
        hosts.push(spawn_host_node(host, topology_path.parent().unwrap_or(Path::new(".")), &scratch, &sample_path, &host_serial_paths, false)?);
    }
    let wall0 = Instant::now();
    let mut t: u64 = 0;
    let mut second: u64 = 0;
    let mut last_clock_ms: u64 = u64::MAX;
    let mut stim_last: BTreeMap<String, String> = BTreeMap::new();
    let mut next_stim: u64 = 0;
    let mut next_sig_poll: u64 = 0;
    let mut sig_offset: u64 = 0;
    let mut sig_queue: Vec<(u64, String, String, String)> = Vec::new();
    let mut gate_cut: BTreeMap<String, bool> = BTreeMap::new();
    let mut next_gate_poll = Instant::now();
    let mut pico_rx_free_at: u64 = 0;
    let mut bus_free: BTreeMap<String, u64> = BTreeMap::new();
    let mut node_wait: BTreeMap<String, f64> = BTreeMap::new();
    let mut gpio_edges: BTreeMap<String, u64> = BTreeMap::new();
    let mut slowest_step: (f64, u64) = (0.0, 0); // virtual time the GB UART RX wire is free again
    let mut steps: u64 = 0;
    let mut max_lag_ms: f64 = 0.0;
    let mut can_frames: u64 = 0;
    let mut uart_bytes = [0u64; 2];
    let reboots = topology.reboots.clone();
    let mut resets: Vec<serde_json::Value> = Vec::new();
    let symbols_cache: BTreeMap<String, ()> = BTreeMap::new();
    let _ = symbols_cache;

    let terminated = Arc::new(AtomicBool::new(false));
    {
        let flag = terminated.clone();
        let _ = ctrlc_like(flag);
    }

    while t < duration_ns && !terminated.load(Ordering::Acquire) {
        // ---- choose the next horizon
        let mut horizon = t + quantum;
        let earliest_need = procs.iter().filter(|p| p.alive).map(|p| p.next_wakeup).min().unwrap_or(u64::MAX);
        let earliest_in = procs.iter().flat_map(|p| p.inbound.iter().map(|m| m.0)).min().unwrap_or(u64::MAX);
        let next_second = (second + 1) * 1_000_000_000;
        let radio_next = radios.iter().map(|r| r.0.next_event()).min().unwrap_or(u64::MAX);
        let wake = earliest_need.min(earliest_in).min(next_second).min(radio_next.max(t + 1));
        if wake > horizon {
            horizon = wake.min(t + max_skip);
        }
        horizon = horizon.min(duration_ns).min(next_second.max(t + 1));
        // ---- pace against the wall clock
        if cfg.pace > 0.0 {
            let target = Duration::from_nanos((horizon as f64 / cfg.pace) as u64);
            let elapsed = wall0.elapsed();
            if target > elapsed {
                thread::sleep(target - elapsed);
            } else {
                let lag = (elapsed - target).as_secs_f64() * 1000.0;
                if lag > max_lag_ms {
                    max_lag_ms = lag;
                }
            }
        }
        // ---- Pico-Fi: station bytes toward the gateway UART (wire-paced), gate files
        if Instant::now() >= next_gate_poll {
            next_gate_poll = Instant::now() + Duration::from_millis(50);
            for g in &cfg.uart_gates {
                let cut = fs::read_to_string(&g.path).map(|s| s.trim() == "1").unwrap_or(false);
                let key = format!("{}:{}", g.node, g.peripheral);
                if gate_cut.get(&key) != Some(&cut) {
                    let _ = writeln!(gpio_out, "{} gate {} {}", t, key, cut as u8);
                    gate_cut.insert(key.clone(), cut);
                    let _ = fs::write(g.path.with_extension("txt.status"), format!("{}\n", cut as u8));
                }
            }
        }
        if let (Some(pm), Some((_, node, periph, baud))) = (pico_model.as_mut(), pico.as_ref()) {
            pm.service(t);
            let byte_ns = 10_000_000_000u64 / (*baud).max(1);
            let cut = *gate_cut.get(&format!("{node}:{periph}")).unwrap_or(&false);
            // frames that start on the wire before the horizon, back to back at the UART baud
            while pico_rx_free_at.max(t) < horizon {
                let Some(frame) = pm.pop_to_gateway() else { break };
                if cut {
                    continue;
                }
                let start = pico_rx_free_at.max(t);
                pico_rx_free_at = start + frame.len() as u64 * byte_ns;
                if let Some(p) = procs.iter_mut().find(|p| &p.name == node && p.alive) {
                    let mut m = start.to_le_bytes().to_vec();
                    m.extend_from_slice(&port8(periph));
                    m.extend_from_slice(&frame);
                    p.inbound.push((start, MSG_UART_RX, m));
                }
                uart_bytes[1] += frame.len() as u64;
            }
        }
        // ---- radio: station bytes into their modems, the air to the horizon, modem bytes out to the UARTs
        for (medium, masters, eps) in radios.iter_mut() {
            for (i, master) in masters.iter_mut().enumerate() {
                if let Some(f) = master.as_mut() {
                    let mut buf = [0u8; 8192];
                    if let Ok(n) = f.read(&mut buf) {
                        if n > 0 { medium.uart_in(i, t, &buf[..n]); }
                    }
                }
            }
            medium.step(t);
            for i in 0..eps.len() {
                while let Some((start, bytes)) = medium.uart_out(i, t, horizon) {
                    if let Some(f) = masters[i].as_mut() {
                        let _ = f.write_all(&bytes);
                    } else {
                        let (node, periph) = &eps[i];
                        if *gate_cut.get(&format!("{node}:{periph}")).unwrap_or(&false) { continue; }
                        if let Some(p) = procs.iter_mut().find(|p| &p.name == node && p.alive) {
                            let mut m = start.to_le_bytes().to_vec();
                            m.extend_from_slice(&port8(periph));
                            m.extend_from_slice(&bytes);
                            p.inbound.push((start, MSG_UART_RX, m));
                        }
                    }
                }
            }
        }
        // ---- scripted signals: diagnostics/native-signals.txt lines "<virtual ms> <node> <device> <codes>" (appended by
        // scenarios; applied at their virtual time, so fast fault patterns keep their sample spacing)
        if t >= next_sig_poll {
            next_sig_poll = t + 20_000_000;
            if let Ok(mut f) = fs::File::open(cfg.diagnostics.join("native-signals.txt")) {
                use std::io::Seek;
                if f.seek(std::io::SeekFrom::Start(sig_offset)).is_ok() {
                    let mut text = String::new();
                    if f.read_to_string(&mut text).is_ok() {
                        let complete = text.rfind('\n').map(|i| i + 1).unwrap_or(0);
                        sig_offset += complete as u64;
                        for line in text[..complete].lines() {
                            let parts: Vec<&str> = line.split_whitespace().collect();
                            if parts.len() == 4 {
                                if let Ok(ms) = parts[0].parse::<u64>() {
                                    sig_queue.push((ms * 1_000_000, parts[1].to_string(), parts[2].to_string(), parts[3].to_string()));
                                }
                            }
                        }
                        sig_queue.sort_by_key(|x| x.0);
                    }
                }
            }
        }
        while sig_queue.first().is_some_and(|x| x.0 <= horizon) {
            let (at, node, device, codes) = sig_queue.remove(0);
            if let Some(p) = procs.iter_mut().find(|p| p.name == node) {
                let mut dev = [0u8; 16];
                for (i, b) in device.bytes().take(16).enumerate() {
                    dev[i] = b;
                }
                let mut m = dev.to_vec();
                m.extend_from_slice(codes.as_bytes());
                p.inbound.push((at.max(t), MSG_ADC, m));
            }
        }
        // ---- stimulus files (every 100 ms virtual)
        if t >= next_stim {
            next_stim = t + 100_000_000;
            for s in &cfg.stimulus {
                if let Ok(text) = fs::read_to_string(&s.path) {
                    let text = text.trim().to_string();
                    let key = format!("{}/{}", s.node, s.device);
                    if !text.is_empty() && stim_last.get(&key) != Some(&text) {
                        if let Some(p) = procs.iter_mut().find(|p| p.name == s.node) {
                            let mut dev = [0u8; 16];
                            for (i, b) in s.device.bytes().take(16).enumerate() {
                                dev[i] = b;
                            }
                            let mut m = dev.to_vec();
                            m.extend_from_slice(text.as_bytes());
                            p.inbound.push((t, MSG_ADC, m));
                        }
                        stim_last.insert(key, text);
                    }
                }
            }
        }
        if let Some(c) = station_clock.as_mut() {
            c.publish(t, horizon);
        }
        // ---- step every node to the horizon
        for p in procs.iter_mut().filter(|p| p.alive) {
            p.inbound.sort_by_key(|m| m.0);
            let keep: Vec<_> = p.inbound.iter().filter(|m| m.0 > horizon).cloned().collect();
            let mut ok = true;
            for (_, ty, body) in p.inbound.iter().filter(|m| m.0 <= horizon) {
                ok &= write_msg(&mut p.sock, *ty, body).is_ok();
            }
            ok &= write_msg(&mut p.sock, MSG_GRANT, &horizon.to_le_bytes()).is_ok();
            p.inbound = keep;
            if !ok {
                p.alive = false;
            }
        }
        let mut deliveries: Vec<(String, u64, u16, Vec<u8>)> = Vec::new();
        let mut can_batch: Vec<(u64, String, String, Vec<u8>)> = Vec::new();
        let step_wall = Instant::now();
        for i in 0..procs.len() {
            if !procs[i].alive {
                continue;
            }
            let node_wall = Instant::now();
            loop {
                let msg = read_msg(&mut procs[i].sock);
                let (ty, body) = match msg {
                    Ok(m) => m,
                    Err(_) => {
                        let status = procs[i].child.wait().ok();
                        eprintln!("[native-bay] node {} exited at {:.3} s: {:?}", procs[i].name, t as f64 / 1e9, status);
                        procs[i].alive = false;
                        resets.push(serde_json::json!({"node": procs[i].name, "t_ms": t / 1_000_000, "kind": "exited", "status": format!("{status:?}")}));
                        break;
                    }
                };
                match ty {
                    MSG_DONE => {
                        procs[i].next_wakeup = u64::from_le_bytes(body[8..16].try_into().unwrap());
                        let w = node_wall.elapsed().as_secs_f64();
                        *node_wait.entry(procs[i].name.clone()).or_insert(0.0) += w;
                        break;
                    }
                    MSG_CAN_TX => {
                        let ts = u64::from_le_bytes(body[..8].try_into().unwrap());
                        let periph = port_str(&body[8..16]);
                        let from = procs[i].name.clone();
                        if let Some(src) = can.iter().find(|e| e.node == from && e.peripheral == periph) {
                            if src.connected {
                                can_frames += 1;
                                if let Some(tr) = can_trace.as_mut() {
                                    let id = u32::from_le_bytes(body[20..24].try_into().unwrap());
                                    let _ = writeln!(tr, "{} {} {:x} {}", ts, from, id, body[24..].iter().map(|b| format!("{b:02x}")).collect::<String>());
                                }
                                can_batch.push((ts, src.link.clone(), from.clone(), body.clone()));
                            }
                        }
                    }
                    MSG_UART_TX => {
                        let periph = port_str(&body[8..16]);
                        let ts = u64::from_le_bytes(body[..8].try_into().unwrap());
                        let from = procs[i].name.clone();
                        for (medium, _, eps) in radios.iter_mut() {
                            if let Some(k) = eps.iter().position(|(n, p)| n == &from && p == &periph) {
                                if !*gate_cut.get(&format!("{from}:{periph}")).unwrap_or(&false) {
                                    medium.uart_in(k, ts, &body[20..]);
                                }
                            }
                        }
                        if let (Some(pm), Some((_, node, pp, _))) = (pico_model.as_mut(), pico.as_ref()) {
                            if node == &procs[i].name && pp == &periph && !*gate_cut.get(&format!("{node}:{pp}")).unwrap_or(&false) {
                                let data = &body[20..];
                                uart_bytes[0] += data.len() as u64;
                                pm.from_gateway(data);
                            }
                        }
                    }
                    MSG_GPIO => {
                        // Every edge of a pin up to 200, then counts only (chip selects and blinkers are chatty).
                        let ts = u64::from_le_bytes(body[..8].try_into().unwrap());
                        let key = format!("{} {} {}", procs[i].name, body[8], body[9]);
                        let n = gpio_edges.entry(key).or_insert(0u64);
                        *n += 1;
                        if *n <= 200 {
                            let _ = writeln!(gpio_out, "{} {} {} {} {}", ts, procs[i].name, body[8], body[9], body[10]);
                        }
                    }
                    MSG_RESET => {
                        procs[i].pending_reset = Some(body[0]);
                    }
                    _ => {}
                }
            }
        }
        // CAN: frames completed inside this window reach the other controllers after it, keeping their spacing
        // (one shift for the whole window) and serialized per bus, so receive FIFOs see the real inter-frame gaps.
        if !can_batch.is_empty() {
            can_batch.sort_by_key(|f| f.0);
            let shift = horizon.saturating_sub(can_batch[0].0);
            for (ts, link, from, body) in can_batch {
                let len = body[17] as u64;
                let wire_ns = ((67 + 8 * len) * 6 / 5) * 1000; // nominal 1 Mbit/s, stuffing allowance
                let free = bus_free.entry(link.clone()).or_insert(0);
                let at = (ts + shift).max(*free);
                *free = at + wire_ns;
                for dst in can.iter().filter(|e| e.link == link && e.node != from && e.connected) {
                    let mut m = body.clone();
                    m[..8].copy_from_slice(&at.to_le_bytes());
                    m[8..16].copy_from_slice(&port8(&dst.peripheral));
                    deliveries.push((dst.node.clone(), at, MSG_CAN_RX, m));
                }
            }
        }
        for (node, at, ty, m) in deliveries {
            if let Some(p) = procs.iter_mut().find(|p| p.name == node && p.alive) {
                p.inbound.push((at, ty, m));
            }
        }
        let sw = step_wall.elapsed().as_secs_f64() * 1000.0;
        if sw > slowest_step.0 {
            slowest_step = (sw, t / 1_000_000);
        }
        t = horizon;
        steps += 1;
        // ---- firmware-requested resets (software reset, watchdog): restart with the flash/backup domain kept
        for i in 0..procs.len() {
            if let Some(cause) = procs[i].pending_reset.take() {
                let _ = procs[i].child.wait();
                let name = procs[i].name.clone();
                resets.push(serde_json::json!({"node": name, "t_ms": t / 1_000_000, "cause": cause}));
                let boots = procs[i].boots + 1;
                procs[i] = spawn_node(&name, &cfg.nodes[&name], &cfg, t, cause, boots)?;
                let mut tmp = vec![];
                std::mem::swap(&mut tmp, &mut procs);
                push_link_state(&mut tmp, &can, t);
                procs = tmp;
            }
        }
        // ---- virtual clock for the station and the controller
        if t / 1_000_000 != last_clock_ms {
            last_clock_ms = t / 1_000_000;
            let text = format!("{}\n", t / 1_000_000);
            atomic_write(&clock_file, &text);
            if mirror_distinct {
                atomic_write(&clock_mirror, &text);
            }
        }
        // ---- per virtual second: probes, scheduled events
        if t >= next_second {
            second += 1;
            atomic_write(&sample_path, &format!("{second}\n"));
            let mut out = String::new();
            for pr in &cfg.probes {
                if let Some(p) = procs.iter().find(|p| p.name == pr.node && p.alive) {
                    if let Some(v) = read_word(p.child.id(), pr.address, pr.width) {
                        out += &format!("SEDS_BAY_PROBE {} {} {}\n0x{:08x}\n", pr.node, pr.name, second - 1, v);
                    }
                }
            }
            let _ = probes_out.write_all(out.as_bytes());
            let _ = gpio_out.flush();
            let mut changed = false;
            for ev in topology.can_link_events.iter().filter(|e| e.after_sample as u64 == second) {
                if let Some(e) = can.iter_mut().find(|e| e.node == ev.node && e.peripheral == ev.peripheral && e.link == ev.link) {
                    e.connected = ev.connected;
                    changed = true;
                    let _ = writeln!(gpio_out, "{} can_link {} {} {}", t, ev.node, ev.peripheral, ev.connected as u8);
                }
            }
            if changed {
                push_link_state(&mut procs, &can, t);
            }
            for ev in topology.radio_link_events.iter().filter(|e| e.after_sample as u64 == second) {
                for (medium, _, _) in radios.iter_mut().filter(|r| r.0.link == ev.link) {
                    medium.cut.insert((ev.sender.clone(), ev.receiver.clone()), !ev.connected);
                    medium.loss_ppm.insert((ev.sender.clone(), ev.receiver.clone()), ev.loss_ppm.unwrap_or(0));
                    let _ = writeln!(gpio_out, "{} radio {} {}->{} connected={} loss_ppm={}", t, ev.link, ev.sender, ev.receiver, ev.connected as u8, ev.loss_ppm.unwrap_or(0));
                }
            }
            for rb in reboots.iter().filter(|r| r.after_sample as u64 == second) {
                if let Some(i) = procs.iter().position(|p| p.name == rb.node) {
                    let _ = procs[i].child.kill();
                    let _ = procs[i].child.wait();
                    let name = procs[i].name.clone();
                    resets.push(serde_json::json!({"node": name, "t_ms": t / 1_000_000, "cause": "scheduled power cycle"}));
                    let boots = procs[i].boots + 1;
                    procs[i] = spawn_node(&name, &cfg.nodes[&name], &cfg, t, 0, boots)?;
                    push_link_state(&mut procs, &can, t);
                }
            }
            if let Some(msg) = exited_host(&mut hosts)? {
                eprintln!("[native-bay] {msg}");
            }
            let report = serde_json::json!({"virtual_ms": t / 1_000_000, "wall_s": wall0.elapsed().as_secs_f64(), "steps": steps,
                "max_lag_ms": max_lag_ms, "can_frames": can_frames, "pico_uart_bytes_tx_rx": uart_bytes, "resets": resets,
                "node_wall_s": node_wait, "slowest_step_ms_at": slowest_step});
            atomic_write(&cfg.diagnostics.join("native-report.json"), &serde_json::to_string_pretty(&report)?);
        }
    }
    stop.store(true, Ordering::Release);
    for p in procs.iter_mut().filter(|p| p.alive) {
        let _ = write_msg(&mut p.sock, MSG_STOP, &[]);
    }
    thread::sleep(Duration::from_millis(100));
    for p in procs.iter_mut() {
        let _ = p.child.kill();
        let _ = p.child.wait();
    }
    for h in hosts.iter_mut() {
        unsafe { libc::kill(h.child.id() as i32, libc::SIGTERM) };
    }
    thread::sleep(Duration::from_millis(500));
    for h in hosts.iter_mut() {
        let _ = h.child.kill();
        let _ = h.child.wait();
    }
    let report = serde_json::json!({"virtual_ms": t / 1_000_000, "wall_s": wall0.elapsed().as_secs_f64(), "steps": steps,
        "max_lag_ms": max_lag_ms, "can_frames": can_frames, "pico_uart_bytes_tx_rx": uart_bytes, "resets": resets, "completed": true,
        "node_wall_s": node_wait, "slowest_step_ms_at": slowest_step, "gpio_edges": gpio_edges,
        "station_clock": cfg.station_clock.is_some(),
        "radio": radios.iter().map(|r| (r.0.link.clone(), r.0.modems.iter().map(|m| (m.name.clone(), m.stats)).collect::<Vec<_>>())).collect::<Vec<_>>(),
        "pico": pico_model.as_ref().map(|p| p.stats)});
    atomic_write(&cfg.diagnostics.join("native-report.json"), &serde_json::to_string_pretty(&report)?);
    eprintln!("[native-bay] done: {:.1} s virtual in {:.1} s wall, {} steps", t as f64 / 1e9, wall0.elapsed().as_secs_f64(), steps);
    Ok(())
}

fn read_word(pid: u32, address: u32, width: u32) -> Option<u32> {
    use std::os::unix::fs::FileExt;
    let f = fs::File::open(format!("/proc/{pid}/mem")).ok()?;
    let mut b = [0u8; 4];
    let w = width.clamp(1, 4) as usize;
    f.read_exact_at(&mut b[..w], address as u64).ok()?;
    Some(u32::from_le_bytes(b))
}

static TERM: AtomicBool = AtomicBool::new(false);
extern "C" fn on_term(_: libc::c_int) {
    TERM.store(true, Ordering::Release);
}
fn ctrlc_like(flag: Arc<AtomicBool>) -> Result<()> {
    unsafe {
        libc::signal(libc::SIGTERM, on_term as *const () as libc::sighandler_t);
        libc::signal(libc::SIGINT, on_term as *const () as libc::sighandler_t);
    }
    thread::spawn(move || loop {
        if TERM.load(Ordering::Acquire) {
            flag.store(true, Ordering::Release);
            return;
        }
        thread::sleep(Duration::from_millis(50));
    });
    Ok(())
}
