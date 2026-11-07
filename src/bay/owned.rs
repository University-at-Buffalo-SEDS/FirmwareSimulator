//! Board-only Renode owner. This starts no GroundStation process and grants no
//! mode/output authority: Core must retain this owner beside its guarded drivers.
use super::*;
use sha2::{Digest, Sha256};
use std::sync::{atomic::AtomicUsize, Mutex, MutexGuard, OnceLock};
use std::time::Instant;

const LOAD_LIMIT: Duration = Duration::from_secs(15);
const STARTUP_LIMIT: Duration = Duration::from_secs(10);
const FAILED_START_CLEANUP: Duration = Duration::from_secs(2);
const MAX_ENDPOINTS: usize = 16;

/// Only exact topology/source inputs are accepted, never a caller-supplied script.
pub struct BoardOnlyConfig {
    pub topology: PathBuf,
    pub renode: PathBuf,
}
#[derive(Clone, Debug)]
pub struct BuildFile {
    pub relative_role: String,
    pub sha256: String,
}
#[derive(Clone, Debug)]
pub struct BuildManifest {
    pub files: Vec<BuildFile>,
    /// Cold, instance-private backing storage; never physical board NVM.
    pub nvm_namespace: PathBuf,
    pub reset_generation: u64,
}
#[derive(Clone, Copy, Debug)]
pub struct MonotonicSample {
    pub monotonic_ms: u64,
}
#[derive(Clone, Copy, Debug)]
pub struct BayClockSample {
    pub monotonic_ms: u64,
    pub utc_ms: u64,
}
/// One immutable modeled-fix observation from this owner's stdout worker.
/// The host timestamp is when that worker observed a complete console line,
/// subject to pipe buffering/backlog. It is not MCU/SDK ingress, authentication,
/// source election, freshness, or readiness. Sampling never refreshes it.
#[derive(Clone)]
pub struct BaySourceObservation {
    sample: BayClockSample,
    ingress: GnssIngress,
    failure: BayClockFailureFence,
}
#[derive(Clone, Copy)]
struct GnssIngress {
    index: u64,
    modeled_elapsed_ms: u64,
    modeled_utc_ms: u64,
    console_received_at: Instant,
}
impl BaySourceObservation {
    /// Current owner-clock mapping, distinct from the original modeled fix.
    pub fn sample(&self) -> BayClockSample {
        self.sample
    }
    pub fn ingress_index(&self) -> u64 {
        self.ingress.index
    }
    pub fn modeled_fix_elapsed_ms(&self) -> u64 {
        self.ingress.modeled_elapsed_ms
    }
    pub fn modeled_fix_utc_ms(&self) -> u64 {
        self.ingress.modeled_utc_ms
    }
    pub fn console_received_at(&self) -> Instant {
        self.ingress.console_received_at
    }
    /// Negative-only terminal fence for the same original clock instance.
    pub fn has_failed(&self) -> bool {
        self.failure.has_failed()
    }
}
#[derive(Default)]
struct ClockState {
    last: Option<u64>,
    failed: bool,
    gnss_epoch_ms: Option<u64>,
    gnss_ingress: Option<GnssIngress>,
}
/// Read-only terminal-failure evidence for one original clock instance.
/// Initial false proves neither a sample nor freshness, election or readiness.
#[derive(Clone)]
pub struct BayClockFailureFence {
    failed: Arc<AtomicBool>,
}
impl BayClockFailureFence {
    /// Pure RAM-only final-gate observation; no provider, weak upgrade or lock.
    pub fn has_failed(&self) -> bool {
        self.failed.load(Ordering::Acquire)
    }
}
// Every long-lived console/bridge worker invalidates on actual exit, including
// panic outside the clock mutex. Requested teardown has already fenced first.
struct WorkerClockExit(BayClockFailureFence);
impl Drop for WorkerClockExit {
    fn drop(&mut self) {
        self.0.failed.store(true, Ordering::Release);
    }
}
// The guard invalidates already-issued fences during panic unwinding, before
// the underlying Mutex becomes poisoned. Normal observation uses no callbacks.
struct ClockGuard<'a> {
    state: MutexGuard<'a, ClockState>,
    failure_flag: &'a AtomicBool,
}
impl std::ops::Deref for ClockGuard<'_> {
    type Target = ClockState;
    fn deref(&self) -> &ClockState {
        &self.state
    }
}
impl std::ops::DerefMut for ClockGuard<'_> {
    fn deref_mut(&mut self) -> &mut ClockState {
        &mut self.state
    }
}
impl Drop for ClockGuard<'_> {
    fn drop(&mut self) {
        if thread::panicking() {
            self.failure_flag.store(true, Ordering::Release);
        }
    }
}
/// Non-forgeable instance sampler. Model elapsed is not network UTC authority.
pub struct BayClock {
    state: Mutex<ClockState>,
    failed: Arc<AtomicBool>,
}
impl BayClock {
    fn new() -> Self {
        Self {
            state: Mutex::new(ClockState::default()),
            failed: Arc::new(AtomicBool::new(false)),
        }
    }
    pub fn failure_fence(&self) -> BayClockFailureFence {
        BayClockFailureFence {
            failed: self.failed.clone(),
        }
    }
    /// Original-instance binding; do this outside the final Core RAM gate.
    pub fn owns_failure_fence(&self, fence: &BayClockFailureFence) -> bool {
        Arc::ptr_eq(&self.failed, &fence.failed)
    }
    fn lock_state(&self) -> Result<ClockGuard<'_>> {
        match self.state.lock() {
            Ok(state) => Ok(ClockGuard {
                state,
                failure_flag: &self.failed,
            }),
            Err(_) => {
                self.failed.store(true, Ordering::Release);
                bail!("clock lock poisoned")
            }
        }
    }
    pub fn monotonic_sample(&self) -> Option<MonotonicSample> {
        if self.failed.load(Ordering::Acquire) {
            return None;
        }
        let state = self.lock_state().ok()?;
        (!state.failed && !self.failed.load(Ordering::Acquire)).then_some(MonotonicSample {
            monotonic_ms: state.last?,
        })
    }
    /// Actual same-instance modeled GNSS source mapping, observed from the owned
    /// console at fix creation. Core MUST independently correlate accepted SDK
    /// source/election/readback before admission; source mapping is not readiness.
    /// No wall-clock or caller-provided epoch is substituted here.
    pub fn sample(&self) -> Option<BayClockSample> {
        if self.failed.load(Ordering::Acquire) {
            return None;
        }
        let mut state = self.lock_state().ok()?;
        if state.failed || self.failed.load(Ordering::Acquire) {
            return None;
        }
        self.sample_locked(&mut state)
    }
    fn sample_locked(&self, state: &mut ClockState) -> Option<BayClockSample> {
        let monotonic_ms = state.last?;
        let epoch = state.gnss_epoch_ms?;
        let Some(utc_ms) = epoch
            .checked_add(monotonic_ms)
            .filter(|utc| *utc > 0 && *utc <= i64::MAX as u64)
        else {
            self.failed.store(true, Ordering::Release);
            state.failed = true;
            return None;
        };
        Some(BayClockSample {
            monotonic_ms,
            utc_ms,
        })
    }
    /// Snapshot current mapping and the actual last modeled-fix ingress under
    /// one mutex. Call outside Core/final dispatch guards. No wall UTC is used.
    pub fn source_observation(&self) -> Option<BaySourceObservation> {
        if self.failed.load(Ordering::Acquire) {
            return None;
        }
        let mut state = self.lock_state().ok()?;
        if state.failed || self.failed.load(Ordering::Acquire) {
            return None;
        }
        let sample = self.sample_locked(&mut state)?;
        let ingress = state.gnss_ingress?;
        let observation = BaySourceObservation {
            sample,
            ingress,
            failure: self.failure_fence(),
        };
        (!self.failed.load(Ordering::Acquire)).then_some(observation)
    }
    /// Original-instance identity only; liveness still requires its sticky fence.
    pub fn owns_source_observation(&self, observation: &BaySourceObservation) -> bool {
        self.owns_failure_fence(&observation.failure)
    }
    pub fn terminal_failure(&self) -> bool {
        if self.failed.load(Ordering::Acquire) {
            return true;
        }
        self.lock_state().map_or(true, |state| state.failed)
    }
    fn fail(&self) {
        // Publish terminal failure BEFORE any potentially blocking state lock.
        self.failed.store(true, Ordering::Release);
        if let Ok(mut state) = self.state.lock() {
            state.failed = true;
        }
    }
    fn observe_gnss(
        &self,
        elapsed_ms: u64,
        utc_ms: u64,
        console_received_at: Instant,
    ) -> Result<()> {
        let mut state = self.lock_state()?;
        let epoch = utc_ms.checked_sub(elapsed_ms).filter(|epoch| *epoch > 0);
        if self.failed.load(Ordering::Acquire)
            || state.failed
            || epoch.is_none()
            || utc_ms > i64::MAX as u64
            || state.gnss_epoch_ms.is_some_and(|old| Some(old) != epoch)
            || epoch.is_some_and(|epoch| {
                state.last.is_some_and(|value| {
                    epoch
                        .checked_add(value)
                        .is_none_or(|utc| utc > i64::MAX as u64)
                })
            })
        {
            self.failed.store(true, Ordering::Release);
            state.failed = true;
            bail!("owned GNSS source changed epoch");
        }
        let Some(index) = state
            .gnss_ingress
            .map_or(Some(1), |old| old.index.checked_add(1))
        else {
            self.failed.store(true, Ordering::Release);
            state.failed = true;
            bail!("owned GNSS observation index exhausted");
        };
        state.gnss_epoch_ms = epoch;
        state.gnss_ingress = Some(GnssIngress {
            index,
            modeled_elapsed_ms: elapsed_ms,
            modeled_utc_ms: utc_ms,
            console_received_at,
        });
        Ok(())
    }
    fn observe(&self, value: u64) -> Result<()> {
        let mut state = self.lock_state()?;
        if self.failed.load(Ordering::Acquire)
            || state.failed
            || value == u64::MAX
            || state.last.is_some_and(|old| value < old)
            || state.gnss_epoch_ms.is_some_and(|epoch| {
                epoch
                    .checked_add(value)
                    .is_none_or(|utc| utc > i64::MAX as u64)
            })
        {
            self.failed.store(true, Ordering::Release);
            state.failed = true;
            bail!("owned emulator clock failed or rolled back");
        }
        state.last = Some(value);
        Ok(())
    }
}
impl Drop for BayClock {
    fn drop(&mut self) {
        self.failed.store(true, Ordering::Release);
    }
}
struct Instance {
    sealed: AtomicBool,
    endpoints: AtomicUsize,
    clock: Arc<BayClock>,
}
/// Description only. Neither a path nor localhost/port is an IO permission.
#[derive(Clone, Debug)]
pub struct EndpointDescriptor {
    pub host: String,
    pub link: String,
    pub transport: HostLinkTransport,
}
/// Actual IO ownership transferred to the Core factory, not an ambient path.
/// Core must additionally apply the original simulation-domain IO guard.
pub struct OwnedEndpoint {
    instance: Arc<Instance>,
    descriptor: EndpointDescriptor,
    io: Option<EndpointIo>,
}
enum EndpointIo {
    Uart(fs::File),
    PicoI2c(UnixStream),
    PicoTcp(std::net::TcpStream),
}
impl OwnedEndpoint {
    pub fn descriptor(&self) -> &EndpointDescriptor {
        &self.descriptor
    }
    pub fn same_instance(&self, bay: &OwnedBay) -> bool {
        bay.state
            .as_ref()
            .is_some_and(|s| Arc::ptr_eq(&s.instance, &self.instance))
    }
}
// No raw handle/clone accessor: cloning a descriptor must not outlive executor
// accounting. Core supplies its original-domain guard around these operations.
impl Read for OwnedEndpoint {
    fn read(&mut self, bytes: &mut [u8]) -> std::io::Result<usize> {
        match self
            .io
            .as_mut()
            .ok_or_else(|| std::io::Error::from(std::io::ErrorKind::BrokenPipe))?
        {
            EndpointIo::Uart(io) => io.read(bytes),
            EndpointIo::PicoI2c(io) => io.read(bytes),
            EndpointIo::PicoTcp(io) => io.read(bytes),
        }
    }
}
impl Write for OwnedEndpoint {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        match self
            .io
            .as_mut()
            .ok_or_else(|| std::io::Error::from(std::io::ErrorKind::BrokenPipe))?
        {
            EndpointIo::Uart(io) => io.write(bytes),
            EndpointIo::PicoI2c(io) => io.write(bytes),
            EndpointIo::PicoTcp(io) => io.write(bytes),
        }
    }
    fn flush(&mut self) -> std::io::Result<()> {
        match self
            .io
            .as_mut()
            .ok_or_else(|| std::io::Error::from(std::io::ErrorKind::BrokenPipe))?
        {
            EndpointIo::Uart(io) => io.flush(),
            EndpointIo::PicoI2c(io) => io.flush(),
            EndpointIo::PicoTcp(io) => io.flush(),
        }
    }
}
impl Drop for OwnedEndpoint {
    fn drop(&mut self) {
        // Close actual IO BEFORE publishing the final lease release. Rust's
        // automatic field drop would otherwise follow this Drop body, allowing
        // concurrent poll_close to observe zero while the descriptor is live.
        drop(self.io.take());
        self.instance.endpoints.fetch_sub(1, Ordering::AcqRel);
    }
}
struct Worker {
    name: &'static str,
    join: Option<thread::JoinHandle<Result<()>>>,
}
// Private owned-console observation, not firmware readiness or mode authority.
struct LoadObservation {
    marker: String,
    observed: OnceLock<Instant>,
}
struct BayState {
    load: Arc<LoadObservation>,
    prepared: PreparedBay,
    instance: Arc<Instance>,
    manifest: BuildManifest,
    child: Option<Child>,
    stdin: Option<ChildStdin>,
    workers: Vec<Worker>,
    stop: Arc<AtomicBool>,
    child_reaped: bool,
    stop_started: bool,
    group_stop_proven: bool,
    group_stop_requested: bool,
    owned_group: Option<libc::pid_t>,
    spawned: bool,
    unknown: Vec<String>,
    endpoints_taken: bool,
    #[cfg(test)]
    capture: Option<PathBuf>,
}
/// Exclusive process/bridge owner. Drop retains unresolved owners, never detaches
/// a JoinHandle or labels a dropped process clean.
pub struct OwnedBay {
    state: Option<Box<BayState>>,
}
pub struct FailedStartOwnedBay {
    pub error: anyhow::Error,
    pub retained: Option<OwnedBay>,
    pub closed: Option<OwnedBayClosed>,
}
pub enum ClosePoll {
    Pending,
    Uncertain(Vec<String>),
    Closed(OwnedBayClosed),
}
/// Actual local termination only; NOT receiver-safe, SDK-drained or output-safe.
pub struct OwnedBayClosed {
    instance: Arc<Instance>,
    manifest: BuildManifest,
    unknown: Vec<String>,
}
impl OwnedBayClosed {
    /// Original lifetime identity only; never a permission or a clean receipt.
    pub fn owns_clock(&self, clock: &Arc<BayClock>) -> bool {
        Arc::ptr_eq(&self.instance.clock, clock)
    }
    pub fn manifest(&self) -> &BuildManifest {
        &self.manifest
    }
    pub fn unknown_evidence(&self) -> &[String] {
        &self.unknown
    }
}
static UNCLAIMED: OnceLock<Mutex<Vec<BayState>>> = OnceLock::new();
fn unclaimed() -> &'static Mutex<Vec<BayState>> {
    UNCLAIMED.get_or_init(Mutex::default)
}

impl OwnedBay {
    pub fn clock(&self) -> Arc<BayClock> {
        self.state
            .as_ref()
            .expect("live owner")
            .instance
            .clock
            .clone()
    }
    pub fn manifest(&self) -> &BuildManifest {
        &self.state.as_ref().expect("live owner").manifest
    }
    pub fn owns_clock(&self, clock: &Arc<BayClock>) -> bool {
        self.state
            .as_ref()
            .is_some_and(|s| Arc::ptr_eq(&s.instance.clock, clock))
    }
    pub fn descriptors(&self) -> Vec<EndpointDescriptor> {
        let s = self.state.as_ref().expect("live owner");
        s.prepared
            .topology
            .host_nodes
            .iter()
            .flat_map(|host| {
                host.serial_links.iter().map(|link| EndpointDescriptor {
                    host: host.name.clone(),
                    link: link.link.clone(),
                    transport: link.transport,
                })
            })
            .collect()
    }
    /// Connect once to this owner's already-created endpoints. No host executable
    /// or discovery task is started. Failure drops only the local endpoint handles;
    /// the Bay owner still owns every producer and bridge.
    pub fn take_endpoints(&mut self) -> Result<Vec<OwnedEndpoint>> {
        let s = self.state.as_mut().context("executor closed")?;
        ensure!(
            !s.endpoints_taken && !s.instance.sealed.load(Ordering::Acquire),
            "endpoint transfer sealed"
        );
        s.endpoints_taken = true;
        let mut endpoints = Vec::new();
        for host in &s.prepared.topology.host_nodes {
            for link in &host.serial_links {
                ensure!(endpoints.len() < MAX_ENDPOINTS, "too many endpoints");
                let path = &s.prepared.host_serial_paths[&(host.name.clone(), link.link.clone())];
                let io = match link.transport {
                    HostLinkTransport::UartPty => {
                        let file = OpenOptions::new().read(true).write(true).open(path)?;
                        configure_raw_nonblocking(file.as_raw_fd())?;
                        EndpointIo::Uart(file)
                    }
                    HostLinkTransport::PicoFiI2cToUart => {
                        let socket = UnixStream::connect(path)?;
                        socket.set_nonblocking(true)?;
                        EndpointIo::PicoI2c(socket)
                    }
                    HostLinkTransport::PicoFiTcpToUart => {
                        let socket = std::net::TcpStream::connect(
                            path.to_str().context("endpoint address encoding")?,
                        )?;
                        socket.set_nonblocking(true)?;
                        socket.set_nodelay(true)?;
                        EndpointIo::PicoTcp(socket)
                    }
                    HostLinkTransport::PicoFiDiscoveryToUart => {
                        bail!("unowned discovery unsupported")
                    }
                };
                s.instance.endpoints.fetch_add(1, Ordering::AcqRel);
                endpoints.push(OwnedEndpoint {
                    instance: s.instance.clone(),
                    descriptor: EndpointDescriptor {
                        host: host.name.clone(),
                        link: link.link.clone(),
                        transport: link.transport,
                    },
                    io: Some(io),
                });
            }
        }
        Ok(endpoints)
    }
    /// Seal new ordinary production only. Core retains accepted Stop/Recovery
    /// capabilities and original deadlines separately before invoking stop.
    pub fn seal_ordinary(&self) {
        if let Some(s) = &self.state {
            s.instance.sealed.store(true, Ordering::Release);
        }
    }
    /// Nonblocking close observation. The caller first reconciles accepted work;
    /// this method never clears unknown output evidence or manufactures drain.
    pub fn poll_close(&mut self) -> ClosePoll {
        let Some(s) = self.state.as_mut() else {
            return ClosePoll::Uncertain(vec!["already consumed".into()]);
        };
        s.instance.sealed.store(true, Ordering::Release);
        s.instance.clock.fail();
        s.stop.store(true, Ordering::Release);
        if !s.stop_started {
            s.stop_started = true;
            if let Some(child) = &mut s.child {
                // Fresh process_group(0), never a borrowed PID. Reap is observed
                // separately; a kill result alone is not completion.
                let pid = child.id() as libc::pid_t;
                let group = unsafe { libc::getpgid(pid) };
                if group == pid && group != unsafe { libc::getpgrp() } {
                    if unsafe { libc::kill(-group, libc::SIGKILL) } == 0 {
                        s.group_stop_requested = true;
                    } else {
                        s.unknown.push(format!(
                            "owned group kill: {}",
                            std::io::Error::last_os_error()
                        ));
                    }
                } else {
                    s.unknown
                        .push("owned process group could not be verified".into());
                }
                if let Err(error) = child.kill() {
                    s.unknown.push(format!("owned child kill: {error}"));
                }
            }
            s.stdin.take();
        }
        if !s.child_reaped {
            match s.child.as_mut().map(Child::try_wait).transpose() {
                Ok(Some(Some(_))) => {
                    s.child_reaped = true;
                    s.child.take();
                }
                Ok(None) => s.child_reaped = true,
                Ok(Some(None)) => {}
                Err(error) => {
                    return ClosePoll::Uncertain(vec![format!("owned child wait: {error}")])
                }
            }
        }
        for worker in &mut s.workers {
            if worker
                .join
                .as_ref()
                .is_some_and(thread::JoinHandle::is_finished)
            {
                match worker.join.take().expect("finished handle").join() {
                    Ok(Ok(())) => {}
                    Ok(Err(error)) => s.unknown.push(format!("{}: {error}", worker.name)),
                    Err(_) => s.unknown.push(format!("{} panicked", worker.name)),
                }
            }
        }
        if !s.child_reaped
            || s.workers.iter().any(|worker| worker.join.is_some())
            || s.instance.endpoints.load(Ordering::Acquire) != 0
        {
            return ClosePoll::Pending;
        }
        if s.spawned && !s.group_stop_requested {
            return ClosePoll::Uncertain(vec!["owned group stop was not established".into()]);
        }
        if s.spawned && !s.group_stop_proven {
            let Some(group) = s.owned_group else {
                return ClosePoll::Uncertain(vec!["original process group missing".into()]);
            };
            // Signal zero is only the kernel's atomic group-existence query;
            // it delivers no signal. Never send TERM/KILL after child reap.
            if unsafe { libc::kill(-group, 0) } == 0 {
                return ClosePoll::Pending;
            }
            let error = std::io::Error::last_os_error();
            if error.raw_os_error() != Some(libc::ESRCH) {
                return ClosePoll::Uncertain(vec![format!("owned group absence query: {error}")]);
            }
            s.group_stop_proven = true;
        }
        if s.child_reaped
            && !s.group_stop_proven
            && s.stop_started
            && s.unknown.iter().any(|v| v == "early child exit")
        {
            return ClosePoll::Uncertain(vec![
                "early child exit requires descendant reconciliation".into(),
            ]);
        }
        let s = self.state.take().expect("settled state");
        ClosePoll::Closed(OwnedBayClosed {
            instance: s.instance,
            manifest: s.manifest,
            unknown: s.unknown,
        })
    }
}
impl Drop for OwnedBay {
    fn drop(&mut self) {
        if self.state.is_none() {
            return;
        }
        if matches!(self.poll_close(), ClosePoll::Closed(_)) {
            return;
        }
        if let Some(state) = self.state.take() {
            // Process-lifetime retention is an explicit abnormal-drop fallback,
            // not a cleanup receipt. No replacement starts while it is present.
            match unclaimed().lock() {
                Ok(mut held) => held.push(*state),
                Err(_) => {
                    Box::leak(state);
                }
            }
        }
    }
}

pub fn start_owned_board_only(
    config: BoardOnlyConfig,
) -> std::result::Result<OwnedBay, FailedStartOwnedBay> {
    let prepare_result = (|| -> Result<PreparedBay> {
        ensure!(
            unclaimed()
                .lock()
                .map_err(|_| anyhow::anyhow!("unclaimed lock poisoned"))?
                .is_empty(),
            "unclaimed previous executor prevents replacement"
        );
        ensure!(
            config.renode.is_absolute() && config.renode.is_file(),
            "explicit Renode executable required"
        );
        prepare(&config.topology, true)
    })();
    let mut prepared = match prepare_result {
        Ok(value) => value,
        Err(error) => {
            return Err(FailedStartOwnedBay {
                error,
                retained: None,
                closed: None,
            })
        }
    };
    if let Err(error) = snapshot_dependencies(&mut prepared) {
        return Err(FailedStartOwnedBay {
            error,
            retained: None,
            closed: None,
        });
    }
    // Bind completion of THIS private script to THIS owned console. The digest
    // includes the private namespace, so identical-build bays cannot share it.
    // This marker starts the boot observation budget; it grants no readiness.
    #[cfg(test)]
    if OMIT_ONE_BOOT_HOOK.swap(false, Ordering::AcqRel) {
        let index = prepared
            .script
            .lines()
            .position(|line| line.starts_with("cpu AddHook "))
            .expect("real configured boot hook");
        prepared.script = prepared
            .script
            .lines()
            .enumerate()
            .filter(|(line_index, _)| *line_index != index)
            .map(|(_, line)| format!("{line}\n"))
            .collect();
    }
    let mut identity = Sha256::new();
    identity.update(prepared.scratch.path().as_os_str().as_encoded_bytes());
    identity.update(prepared.script.as_bytes());
    let load = Arc::new(LoadObservation {
        marker: format!("SEDS_OWNED_LOAD_READY {:x}", identity.finalize()),
        observed: OnceLock::new(),
    });
    #[cfg(test)]
    let emit_load_marker = !OMIT_LOAD_MARKER.swap(false, Ordering::AcqRel);
    #[cfg(not(test))]
    let emit_load_marker = true;
    if emit_load_marker {
        prepared.script += &format!("echo \"{}\"\n", load.marker);
    }
    // Freeze the exact launch script before constructing its immutable catalog.
    if let Err(error) = fs::write(prepared.scratch.path().join("owned.resc"), &prepared.script) {
        return Err(FailedStartOwnedBay {
            error: error.into(),
            retained: None,
            closed: None,
        });
    }
    let manifest = match manifest(&prepared, &config) {
        Ok(value) => value,
        Err(error) => {
            return Err(FailedStartOwnedBay {
                error,
                retained: None,
                closed: None,
            })
        }
    };
    #[cfg(test)]
    let capture = match capture::before_spawn(&prepared, &manifest) {
        Ok(value) => value,
        Err(error) => {
            return Err(FailedStartOwnedBay {
                error,
                retained: None,
                closed: None,
            })
        }
    };
    let instance = Arc::new(Instance {
        sealed: AtomicBool::new(false),
        endpoints: AtomicUsize::new(0),
        clock: Arc::new(BayClock::new()),
    });
    let mut bay = OwnedBay {
        state: Some(Box::new(BayState {
            load,
            prepared,
            instance,
            manifest,
            child: None,
            stdin: None,
            workers: Vec::new(),
            stop: Arc::new(AtomicBool::new(false)),
            child_reaped: false,
            stop_started: false,
            group_stop_proven: false,
            group_stop_requested: false,
            owned_group: None,
            spawned: false,
            unknown: Vec::new(),
            endpoints_taken: false,
            #[cfg(test)]
            capture,
        })),
    };
    match start(&mut bay, &config) {
        Ok(()) => Ok(bay),
        Err(error) => {
            let deadline = Instant::now() + FAILED_START_CLEANUP;
            loop {
                if let ClosePoll::Closed(receipt) = bay.poll_close() {
                    return Err(FailedStartOwnedBay {
                        error,
                        retained: None,
                        closed: Some(receipt),
                    });
                }
                if Instant::now() >= deadline {
                    return Err(FailedStartOwnedBay {
                        error,
                        retained: Some(bay),
                        closed: None,
                    });
                }
                thread::sleep(Duration::from_millis(5));
            }
        }
    }
}
fn digest(path: &Path) -> Result<String> {
    let mut file = fs::File::open(path)?;
    let mut hash = Sha256::new();
    let mut bytes = [0u8; 16384];
    loop {
        let n = file.read(&mut bytes)?;
        if n == 0 {
            break;
        }
        hash.update(&bytes[..n]);
    }
    Ok(format!("{:x}", hash.finalize()))
}
// Preserve exact launch inputs in this owner's private namespace before any
// process escapes. Model sources are copied, not read again through ambient paths.
fn snapshot_dependencies(prepared: &mut PreparedBay) -> Result<()> {
    fn copy_tree(source: &Path, target: &Path, count: &mut usize, total: &mut u64) -> Result<()> {
        ensure!(
            !fs::symlink_metadata(source)?.file_type().is_symlink(),
            "model source symlink refused"
        );
        fs::create_dir_all(target)?;
        for entry in fs::read_dir(source)? {
            let entry = entry?;
            let kind = entry.file_type()?;
            ensure!(!kind.is_symlink(), "model source symlink refused");
            if kind.is_dir() {
                copy_tree(&entry.path(), &target.join(entry.file_name()), count, total)?;
            } else {
                ensure!(kind.is_file(), "non-file model input refused");
                let size = entry.metadata()?.len();
                *count += 1;
                *total = total.checked_add(size).context("model size overflow")?;
                ensure!(
                    *count <= 4096 && size <= 16 * 1024 * 1024 && *total <= 128 * 1024 * 1024,
                    "model source snapshot budget exceeded"
                );
                fs::copy(entry.path(), target.join(entry.file_name()))?;
            }
        }
        Ok(())
    }
    let source_root = env::var_os("FIRMWARE_SIM_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")));
    let snapshot_root = prepared.scratch.path().join("source");
    copy_tree(
        &source_root.join("renode"),
        &snapshot_root.join("renode"),
        &mut 0,
        &mut 0,
    )?;
    let mut substitutions = vec![
        (
            source_root.display().to_string(),
            snapshot_root.display().to_string(),
        ),
        (
            "/opt/firmware-sim".into(),
            snapshot_root.display().to_string(),
        ),
    ];
    for node in &prepared.topology.nodes {
        let layout = BoardLayout::load(&prepared.base.join(&node.layout))?;
        let root = prepared.base.join(&node.firmware_root);
        let source = root.join(&layout.artifacts.elf);
        let symbols = prepared
            .scratch
            .path()
            .join(safe(&node.name))
            .join("owned-symbols.elf");
        fs::copy(&source, &symbols)?;
        substitutions.push((source.display().to_string(), symbols.display().to_string()));
        let descriptor = layout.resolve_mcu_descriptor()?;
        if descriptor.platform_from_firmware {
            let platform = root.join(&descriptor.platform_file);
            let destination = prepared
                .scratch
                .path()
                .join(safe(&node.name))
                .join("owned-platform.repl");
            fs::copy(&platform, &destination)?;
            substitutions.push((
                platform.display().to_string(),
                destination.display().to_string(),
            ));
        }
    }
    // Longest original prefixes first avoids a parent replacement consuming a
    // more specific actual symbol/platform input.
    substitutions.sort_by_key(|(old, _)| std::cmp::Reverse(old.len()));
    fn rewrite(directory: &Path, substitutions: &[(String, String)]) -> Result<()> {
        for entry in fs::read_dir(directory)? {
            let entry = entry?;
            if entry.file_type()?.is_dir() {
                rewrite(&entry.path(), substitutions)?;
            } else if matches!(
                entry.path().extension().and_then(|s| s.to_str()),
                Some("cs" | "repl" | "resc")
            ) {
                let mut text = fs::read_to_string(entry.path())?;
                for (old, new) in substitutions {
                    text = text.replace(old, new);
                }
                fs::write(entry.path(), text)?;
            }
        }
        Ok(())
    }
    for (old, new) in &substitutions {
        prepared.script = prepared.script.replace(old, new);
    }
    rewrite(prepared.scratch.path(), &substitutions)?;
    Ok(())
}

fn manifest(prepared: &PreparedBay, config: &BoardOnlyConfig) -> Result<BuildManifest> {
    let mut files = vec![
        BuildFile {
            relative_role: "topology".into(),
            sha256: format!("{:x}", Sha256::digest(&prepared.topology_bytes)),
        },
        BuildFile {
            relative_role: "renode".into(),
            sha256: digest(&config.renode)?,
        },
    ];
    fn record(directory: &Path, root: &Path, files: &mut Vec<BuildFile>) -> Result<()> {
        for entry in fs::read_dir(directory)? {
            let entry = entry?;
            if entry.file_type()?.is_dir() {
                record(&entry.path(), root, files)?;
            } else {
                files.push(BuildFile {
                    relative_role: entry.path().strip_prefix(root)?.display().to_string(),
                    sha256: digest(&entry.path())?,
                });
            }
        }
        Ok(())
    }
    record(prepared.scratch.path(), prepared.scratch.path(), &mut files)?;
    files.sort_by(|a, b| a.relative_role.cmp(&b.relative_role));
    Ok(BuildManifest {
        files,
        nvm_namespace: prepared.scratch.path().to_owned(),
        reset_generation: 0,
    })
}
fn start(bay: &mut OwnedBay, config: &BoardOnlyConfig) -> Result<()> {
    let s = bay.state.as_mut().context("missing startup owner")?;
    ensure!(
        s.prepared.host_serial_paths.len() <= MAX_ENDPOINTS,
        "too many endpoints"
    );
    let script_path = s.prepared.scratch.path().join("owned.resc");
    let sealed_script = s
        .manifest
        .files
        .iter()
        .find(|file| file.relative_role == "owned.resc")
        .context("launch script absent from prepared manifest")?;
    ensure!(
        digest(&script_path)? == sealed_script.sha256,
        "prepared launch script changed"
    );
    let load_deadline = Instant::now() + LOAD_LIMIT;
    let child = Command::new(&config.renode)
        .process_group(0)
        .args(["--disable-xwt", "--console", "--execute"])
        .arg(format!("include @{}; start", script_path.display()))
        .env("SEDS_SIM_PROBE_CAPTURE", &s.prepared.probe_path)
        .env(
            "SEDS_SIM_BOOT_CAPTURE_DIR",
            s.prepared.probe_path.parent().context("probe parent")?,
        )
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    // Owner is installed synchronously before any fallible operation or await.
    s.owned_group = Some(child.id() as libc::pid_t);
    s.child = Some(child);
    s.spawned = true;
    let child = s.child.as_mut().expect("installed child");
    s.stdin = Some(keep_console_stdin_open(child)?);
    let stdout = child.stdout.take().context("owned stdout")?;
    let stderr = child.stderr.take().context("owned stderr")?;
    for (name, pipe, parse_clock) in [
        ("stdout", Box::new(stdout) as Box<dyn Read + Send>, true),
        ("stderr", Box::new(stderr) as Box<dyn Read + Send>, false),
    ] {
        let clock = s.instance.clock.clone();
        let load = s.load.clone();
        let stop = s.stop.clone();
        #[cfg(test)]
        let mut trace = capture::Trace::open(s.capture.as_deref(), name)?;
        let handle = thread::Builder::new()
            .name(format!("owned-renode-{name}"))
            .spawn(move || {
                let _clock_exit = WorkerClockExit(clock.failure_fence());
                let result = (|| -> Result<()> {
                    let mut reader = BufReader::new(pipe);
                    let mut failure = None;
                    let mut failure_tail = VecDeque::new();
                    while let Some(line) = crate::bounded_diagnostics::read_line(&mut reader)? {
                        if failure.is_some() || console_execution_failed(&line) {
                            // Fence immediately, but retain bounded following exception
                            // context until real stop/EOF. Never turn this error clean.
                            clock.fail();
                            if failure.is_none() {
                                failure = Some("owned emulator execution failed");
                            }
                            // Keep the first eight cause lines plus last eight context
                            // lines; a long CLR stack must not evict its root cause.
                            if failure_tail.len() == 16 {
                                failure_tail.remove(8);
                            }
                            failure_tail.push_back(line.chars().take(512).collect::<String>());
                            // Existing fatal fence precedes every diagnostic syscall.
                            #[cfg(test)]
                            trace.line(&line)?;
                            continue;
                        }
                        // Existing fatal fencing above stays first. Capture this
                        // accepted complete-line observation before diagnostic IO
                        // or the clock mutex, never at adapter sampling.
                        let gnss_received_at = (parse_clock
                            && line.trim().starts_with("SEDS_OWNED_GNSS "))
                        .then(Instant::now);
                        // Record the actual pipe observation before diagnostics IO.
                        // Only the frozen instance-specific full line can start boot.
                        if parse_clock && line.trim() == load.marker {
                            load.observed.get_or_init(Instant::now);
                        }
                        #[cfg(test)]
                        trace.line(&line)?;
                        if parse_clock {
                            if let Some(value) = line.trim().strip_prefix("SEDS_OWNED_GNSS ") {
                                let mut fields = value.split_whitespace();
                                let elapsed =
                                    fields.next().context("GNSS elapsed missing")?.parse()?;
                                let utc = fields.next().context("GNSS UTC missing")?.parse()?;
                                ensure!(fields.next().is_none(), "invalid GNSS sample fields");
                                clock.observe_gnss(
                                    elapsed,
                                    utc,
                                    gnss_received_at.context("GNSS ingress timestamp absent")?,
                                )?;
                            }
                            if let Some(value) = line.trim().strip_prefix("SEDS_OWNED_CLOCK ") {
                                clock.observe(
                                    value.parse().context("invalid owned clock sample")?,
                                )?;
                            }
                        }
                    }
                    if let Some(failure) = failure {
                        bail!(
                            "{failure}: {}",
                            failure_tail.into_iter().collect::<Vec<_>>().join("\n")
                        );
                    }
                    ensure!(
                        stop.load(Ordering::Acquire),
                        "owned emulator console closed before stop"
                    );
                    Ok(())
                })();
                if result.is_err() {
                    clock.fail();
                }
                result
            })?;
        s.workers.push(Worker {
            name,
            join: Some(handle),
        });
    }
    #[cfg(test)]
    if FAIL_AFTER_CONSOLE_OWNERS.swap(false, Ordering::AcqRel) {
        bail!("injected failure after actual Renode and console owners installed");
    }
    // Total observation is <=15s load +10s boot, excluding separately bounded
    // failure cleanup. Neither phase substitutes for the real boot/PTY/clock checks.
    loop {
        if s.child
            .as_mut()
            .context("child owner")?
            .try_wait()?
            .is_some()
        {
            s.instance.clock.fail();
            s.child_reaped = true;
            s.child.take();
            s.unknown.push("early child exit".into());
            bail!("Renode exited during startup; descendant reconciliation required");
        }
        ensure!(
            !s.instance.clock.terminal_failure(),
            "emulator clock failed during startup"
        );
        let now = Instant::now();
        if let Some(observed) = s.load.observed.get() {
            ensure!(*observed <= load_deadline, "owned script load deadline");
            let boot_deadline = *observed + STARTUP_LIMIT;
            ensure!(now < boot_deadline, "owned board boot deadline");
            if s.prepared.renode_pty_paths.iter().all(|path| path.exists())
                && s.instance.clock.monotonic_sample().is_some()
                && s.prepared.boot_captures.iter().all(|(path, marker)| {
                    fs::read_to_string(path).is_ok_and(|s| s.trim() == marker)
                })
            {
                break;
            }
        } else {
            ensure!(now < load_deadline, "owned script load deadline");
        }
        thread::sleep(Duration::from_millis(5));
    }
    for (_, uart, socket, baud) in std::mem::take(&mut s.prepared.pico_bridges) {
        let listener = UnixListener::bind(&socket)?;
        let stop = s.stop.clone();
        let clock = s.instance.clock.clone();
        let handle = thread::Builder::new()
            .name("owned-pico-i2c".into())
            .spawn(move || {
                let _clock_exit = WorkerClockExit(clock.failure_fence());
                let result = run_pico_fi_bridge_until(listener, &uart, baud, || {
                    stop.load(Ordering::Acquire)
                });
                if result.is_err() {
                    clock.fail();
                }
                result
            })?;
        s.workers.push(Worker {
            name: "pico-i2c",
            join: Some(handle),
        });
    }
    for (_, uart, listener) in std::mem::take(&mut s.prepared.pico_tcp_bridges) {
        let stop = s.stop.clone();
        let clock = s.instance.clock.clone();
        let handle = thread::Builder::new()
            .name("owned-pico-tcp".into())
            .spawn(move || {
                let _clock_exit = WorkerClockExit(clock.failure_fence());
                let result =
                    picofi_tcp_bridge::run(listener, &uart, || stop.load(Ordering::Acquire));
                if result.is_err() {
                    clock.fail();
                }
                result
            })?;
        s.workers.push(Worker {
            name: "pico-tcp",
            join: Some(handle),
        });
    }
    ensure!(
        s.prepared.pico_discovery_bridges.is_empty(),
        "discovery cannot escape owner"
    );
    Ok(())
}

#[cfg(test)]
static OMIT_LOAD_MARKER: AtomicBool = AtomicBool::new(false);
#[cfg(test)]
static OMIT_ONE_BOOT_HOOK: AtomicBool = AtomicBool::new(false);
#[cfg(test)]
static FAIL_AFTER_CONSOLE_OWNERS: AtomicBool = AtomicBool::new(false);
#[cfg(test)]
#[path = "owned_tests.rs"]
mod tests;

#[cfg(test)]
#[path = "owned_capture.rs"]
mod capture;
