use super::*;

#[test]
fn clock_fences_rollback_changed_epoch_and_overflow() {
    let clock = BayClock::new();
    let fence = clock.failure_fence();
    clock.observe(100).unwrap();
    assert!(clock.sample().is_none());
    assert!(
        !fence.has_failed(),
        "missing initial UTC is unready, not a failure"
    );
    clock
        .observe_gnss(0, 1_767_225_600_000, Instant::now())
        .unwrap();
    assert_eq!(clock.sample().unwrap().utc_ms, 1_767_225_600_100);
    assert!(clock.observe(99).is_err());
    assert!(fence.has_failed());
    assert!(clock.monotonic_sample().is_none());
    assert!(clock.observe(101).is_err());
    let clock = BayClock::new();
    let fence = clock.failure_fence();
    clock
        .observe_gnss(0, 1_767_225_600_000, Instant::now())
        .unwrap();
    assert!(clock
        .observe_gnss(1, 1_767_225_600_002, Instant::now())
        .is_err());
    assert!(fence.has_failed());
    assert!(clock.terminal_failure());
    let clock = BayClock::new();
    let fence = clock.failure_fence();
    clock
        .observe_gnss(0, i64::MAX as u64, Instant::now())
        .unwrap();
    assert!(clock.observe(1).is_err());
    assert!(fence.has_failed());
    assert!(clock.sample().is_none());
    // Opposite observation order must fail immediately as well: an already
    // issued fence cannot stay live until a later sampler discovers overflow.
    let clock = BayClock::new();
    let fence = clock.failure_fence();
    clock.observe(100).unwrap();
    assert!(clock
        .observe_gnss(0, i64::MAX as u64, Instant::now())
        .is_err());
    assert!(fence.has_failed());
    assert!(clock.sample().is_none());
}
fn actual_config() -> BoardOnlyConfig {
    BoardOnlyConfig {
        topology: std::env::var_os("OWNED_BAY_TEST_TOPOLOGY")
            .expect("explicit actual board cohort")
            .into(),
        renode: std::env::var_os("OWNED_BAY_TEST_RENODE")
            .expect("actual pinned Renode executable")
            .into(),
    }
}
fn close(mut bay: OwnedBay) -> OwnedBayClosed {
    bay.seal_ordinary();
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        match bay.poll_close() {
            ClosePoll::Closed(receipt) => return receipt,
            ClosePoll::Pending => {}
            ClosePoll::Uncertain(reason) => {
                panic!("actual cleanup uncertain, owner retained: {reason:?}")
            }
        }
        assert!(
            Instant::now() < deadline,
            "actual close exceeded observation bound; owner retained"
        );
        thread::sleep(Duration::from_millis(5));
    }
}

fn attest_private_runtime() {
    let devices = fs::read_dir("/sys/class/net")
        .unwrap()
        .map(|e| e.unwrap().file_name())
        .collect::<Vec<_>>();
    assert_eq!(devices, vec![std::ffi::OsString::from("lo")]);
    let status = fs::read_to_string("/proc/self/status").unwrap();
    for key in ["CapInh:", "CapPrm:", "CapEff:", "CapAmb:", "CapBnd:"] {
        let value = status
            .lines()
            .find_map(|s| s.strip_prefix(key))
            .unwrap()
            .trim();
        assert_eq!(u64::from_str_radix(value, 16).unwrap(), 0, "{key}");
    }
    assert!(status.lines().any(|s| s == "NoNewPrivs:\t1"));
    let expected = [
        ("memory.max", "4294967296"),
        ("memory.swap.max", "0"),
        ("cpu.max", "200000 100000"),
        ("pids.max", "256"),
    ];
    for (name, value) in expected {
        assert_eq!(
            fs::read_to_string(Path::new("/sys/fs/cgroup").join(name))
                .unwrap()
                .trim(),
            value
        );
    }
    println!(
        "ACTUAL_OWNED_BAY_SANDBOX {}",
        serde_json::json!({"interfaces":["lo"],
        "all_capability_sets_zero":true,"no_new_privileges":true,"memory_bytes":4294967296u64,
        "memory_swap_max":0,"cpu_quota":"200000 100000","pids_max":256})
    );
    assert!(!Path::new("/dev/serial").exists());
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let client = std::net::TcpStream::connect(listener.local_addr().unwrap()).unwrap();
    let (accepted, _) = listener.accept().unwrap();
    drop((client, accepted, listener));
}
fn transfer_until(endpoint: &mut OwnedEndpoint, mut outgoing: &[u8], length: usize) -> Vec<u8> {
    let deadline = Instant::now() + Duration::from_secs(3);
    let mut received = Vec::with_capacity(length);
    while !outgoing.is_empty() || received.len() < length {
        if !outgoing.is_empty() {
            match endpoint.write(outgoing) {
                Ok(n) if n > 0 => outgoing = &outgoing[n..],
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {}
                other => panic!("actual endpoint write: {other:?}"),
            }
        }
        if received.len() < length {
            let mut bytes = [0; 4096];
            let count = bytes.len().min(length - received.len());
            match endpoint.read(&mut bytes[..count]) {
                Ok(n) if n > 0 => received.extend_from_slice(&bytes[..n]),
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {}
                other => panic!("actual endpoint read: {other:?}"),
            }
        }
        assert!(
            Instant::now() < deadline,
            "actual endpoint traffic observation expired"
        );
        thread::sleep(Duration::from_millis(1));
    }
    received
}
#[test]
#[ignore = "manual actual linked ARM Renode cohort; private network/device namespace only"]
fn actual_board_only_owned_endpoints_clock_and_join() {
    attest_private_runtime();
    let mut bay = start_owned_board_only(actual_config()).unwrap_or_else(|error| {
        panic!(
            "actual startup: {}; closed evidence {:?}",
            error.error,
            error
                .closed
                .as_ref()
                .map(|closed| closed.unknown_evidence())
        )
    });
    let original_load_marker = bay.state.as_ref().unwrap().load.marker.clone();
    assert!(bay.state.as_ref().unwrap().load.observed.get().is_some());
    let original_clock = bay.clock();
    let original_fence = original_clock.failure_fence();
    assert!(!original_fence.has_failed());
    assert!(original_clock.owns_failure_fence(&original_fence));
    assert!(bay.owns_clock(&original_clock));
    let foreign_clock = Arc::new(BayClock::new());
    assert!(!bay.owns_clock(&foreign_clock));
    assert!(!foreign_clock.owns_failure_fence(&original_fence));
    let descriptors = bay.descriptors();
    assert!(!descriptors.is_empty());
    let mut endpoints = bay.take_endpoints().unwrap();
    assert_eq!(descriptors.len(), endpoints.len());
    assert!(endpoints
        .iter()
        .all(|endpoint| endpoint.same_instance(&bay)));
    assert!(bay.take_endpoints().is_err());
    // The fixture explicitly uses owned Pico TCP, not discovery equivalence.
    let tcp = endpoints
        .iter_mut()
        .find(|e| e.descriptor().transport == HostLinkTransport::PicoFiTcpToUart)
        .expect("reviewed topology must contain actual Pico TCP");
    assert_eq!(transfer_until(tcp, b"PICOFI1", 7), b"PICOFI1");
    let header = transfer_until(tcp, &[], 4);
    assert_eq!(&header[..2], &[0xb5, 0x4e]);
    let body_length = u16::from_le_bytes([header[2], header[3]]) as usize;
    assert!((1..=4096).contains(&body_length));
    let body = transfer_until(tcp, &[], body_length);
    assert_eq!(body.len(), body_length); // actual Gateway UART packet, no command minted
    let radio = endpoints
        .iter_mut()
        .find(|e| e.descriptor().transport == HostLinkTransport::UartPty)
        .expect("reviewed topology must retain actual radio endpoint");
    let actual_radio_bytes = transfer_until(radio, &[], 4);
    assert_eq!(actual_radio_bytes.len(), 4);
    let deadline = Instant::now() + Duration::from_secs(5);
    while original_clock.sample().is_none() {
        assert!(!original_clock.terminal_failure());
        assert!(
            Instant::now() < deadline,
            "no actual modeled GNSS source observation"
        );
        thread::sleep(Duration::from_millis(5));
    }
    let sample = original_clock.sample().unwrap();
    // Exact reviewed cohort uses the maintained GNSS fixture epoch. This is a
    // source-mapping assertion, never generic SDK time-authority admission.
    assert_eq!(sample.utc_ms - sample.monotonic_ms, 1_767_225_600_000);
    let deadline = Instant::now() + Duration::from_secs(2);
    let advanced_sample = loop {
        let next = original_clock
            .sample()
            .expect("same owned source remains available");
        assert_eq!(
            next.utc_ms - next.monotonic_ms,
            sample.utc_ms - sample.monotonic_ms
        );
        if next.monotonic_ms > sample.monotonic_ms {
            break next;
        }
        assert!(
            Instant::now() < deadline,
            "owned emulator clock did not actually advance"
        );
        thread::sleep(Duration::from_millis(5));
    };
    // Inspect this private PID namespace while the actual instance is active.
    // The image may contain a GS binary; none may be launched by this component.
    for entry in fs::read_dir("/proc").unwrap().flatten() {
        if entry
            .file_name()
            .to_string_lossy()
            .chars()
            .all(|c| c.is_ascii_digit())
        {
            if let Ok(command) = fs::read(entry.path().join("cmdline")) {
                let executable = command.split(|b| *b == 0).next().unwrap_or_default();
                assert!(
                    !executable.ends_with(b"/groundstation_backend"),
                    "unexpected GS child"
                );
            }
        }
    }
    bay.seal_ordinary();
    assert!(
        !original_fence.has_failed(),
        "ordinary seal alone must preserve accepted Stop clock"
    );
    assert!(bay.take_endpoints().is_err());
    // Actual transferred handles hold termination accounting until dropped.
    assert!(matches!(bay.poll_close(), ClosePoll::Pending));
    assert!(
        original_fence.has_failed(),
        "close fences before endpoint accounting settles"
    );
    drop(endpoints);
    let actual_group = bay.state.as_ref().unwrap().owned_group.unwrap();
    let closed = close(bay);
    assert_eq!(unsafe { libc::kill(-actual_group, 0) }, -1);
    assert_eq!(
        std::io::Error::last_os_error().raw_os_error(),
        Some(libc::ESRCH)
    );
    println!(
        "ACTUAL_OWNED_BAY {}",
        serde_json::json!({"pico_hello_written":7,"pico_hello_read":7,
        "actual_gateway_uart_packet_bytes":body_length,"actual_radio_bytes":actual_radio_bytes.len(),
        "original_monotonic_ms":sample.monotonic_ms,"advanced_monotonic_ms":advanced_sample.monotonic_ms,"source_epoch_ms":sample.utc_ms-sample.monotonic_ms,
        "actual_process_group_absent":true,"endpoint_count":descriptors.len(),"no_core_ready_claim":true})
    );
    assert!(
        closed.unknown_evidence().is_empty(),
        "successful close retained actual uncertainty: {:?}",
        closed.unknown_evidence()
    );
    assert!(!closed.manifest().files.is_empty());
    assert_eq!(closed.manifest().reset_generation, 0);
    assert!(original_clock.sample().is_none());
    assert!(closed.owns_clock(&original_clock));
    // A second genuinely started instance has the same source/build but a
    // different sealed lifetime. It is sequential: no concurrent extra bay.
    let second = start_owned_board_only(actual_config())
        .unwrap_or_else(|error| panic!("second actual startup: {}", error.error));
    assert_ne!(
        second.state.as_ref().unwrap().load.marker,
        original_load_marker
    );
    assert!(second.state.as_ref().unwrap().load.observed.get().is_some());
    let second_clock = second.clock();
    let second_fence = second_clock.failure_fence();
    assert!(!second_fence.has_failed());
    assert!(second_clock.owns_failure_fence(&second_fence));
    assert!(!second_clock.owns_failure_fence(&original_fence));
    assert!(!original_clock.owns_failure_fence(&second_fence));
    assert!(original_fence.has_failed());
    assert!(!closed.owns_clock(&second_clock));
    let second_closed = close(second);
    assert!(second_closed.unknown_evidence().is_empty());
    assert!(second_closed.owns_clock(&second_clock));
    assert!(!second_closed.owns_clock(&original_clock));
    assert!(second_clock.sample().is_none());
    assert!(second_fence.has_failed());
    println!(
        "ACTUAL_OWNED_BAY_FAILURE_FENCES {}",
        serde_json::json!({
        "genuine_sequential_instances":2,"foreign_fence_rejected":true,
        "ordinary_seal_preserves_clock":true,"actual_close_fences_both":true,
        "no_core_ready_claim":true})
    );
    println!(
        "ACTUAL_OWNED_BAY_FOREIGN_RECEIPT {}",
        serde_json::json!({
        "actual_sequential_instances":2,"same_build_foreign_clock_rejected":true,
        "both_actual_local_unknown_counts":0,"no_core_ready_claim":true})
    );
}
#[test]
#[ignore = "manual actual Renode partial startup; fail after real child and console-owner creation"]
fn actual_board_startup_failure_retains_or_joins_every_owner() {
    // The exact real Renode is spawned and both console workers are retained
    // before this test-only error. No replacement executable/clean setter.
    attest_private_runtime();
    FAIL_AFTER_CONSOLE_OWNERS.store(true, Ordering::Release);
    let result = start_owned_board_only(actual_config());
    let mut failure = match result {
        Ok(bay) => {
            let _ = close(bay);
            panic!("negative topology unexpectedly started")
        }
        Err(error) => error,
    };
    assert!(
        !FAIL_AFTER_CONSOLE_OWNERS.load(Ordering::Acquire),
        "failure happened before actual child/console ownership"
    );
    assert!(failure.closed.is_some() || failure.retained.is_some());
    let closed = if let Some(bay) = failure.retained.take() {
        close(bay)
    } else {
        failure
            .closed
            .take()
            .expect("actual failed-start cleanup receipt")
    };
    assert!(
        closed.unknown_evidence().is_empty(),
        "actual failed-start cleanup uncertain: {:?}",
        closed.unknown_evidence()
    );
    println!(
        "ACTUAL_OWNED_BAY_PARTIAL_START {}",
        serde_json::json!({"real_spawn_before_injected_error":true,
        "actual_cleanup_receipt":true,"local_unknown_count":closed.unknown_evidence().len(),"no_core_ready_claim":true})
    );
}

#[test]
#[ignore = "manual actual seven-board preparation only; no Renode or GS process"]
fn actual_emitted_board_only_script_omits_host_wait_and_legacy_keeps_it() {
    attest_private_runtime();
    let config = actual_config();
    let board = prepare(&config.topology, true).expect("actual board-only preparation");
    let legacy = prepare(&config.topology, false).expect("actual legacy preparation");
    let marker = "echo \"SEDS_HOST_ENDPOINTS_READY\"";
    assert_eq!(
        board.script.lines().filter(|line| *line == marker).count(),
        1
    );
    assert_eq!(
        legacy.script.lines().filter(|line| *line == marker).count(),
        1
    );
    assert!(!board.script.lines().any(|line| line == "sleep 5"));
    assert_eq!(
        legacy
            .script
            .lines()
            .filter(|line| *line == "sleep 5")
            .count(),
        1
    );
    assert!(board.script.contains("emulation SetGlobalQuantum"));
    assert!(legacy.script.contains("emulation SetGlobalQuantum"));
    let destination =
        PathBuf::from(env::var_os("OWNED_BAY_TEST_CAPTURE_DIR").expect("owned script capture"))
            .join("actual-emitted-policy");
    fs::create_dir(&destination).unwrap();
    fs::write(destination.join("board-only.resc"), &board.script).unwrap();
    fs::write(destination.join("legacy.resc"), &legacy.script).unwrap();
    println!(
        "ACTUAL_EMITTED_SCRIPT_POLICY {}",
        serde_json::json!({
        "actual_maintained_preparations":2,"processes_spawned":0,"board_only_sleep5":0,"legacy_sleep5":1,
        "board_script_sha256":format!("{:x}", Sha256::digest(board.script.as_bytes())),
        "legacy_script_sha256":format!("{:x}", Sha256::digest(legacy.script.as_bytes())),
        "note":"Actual same required7ELFs/layouts; legacy hostbinary validated but never spawned; clock/model bytes unchanged"})
    );
}

// Real process negative controls. Each is run as its own exact libtest process
// in a fresh private container; neither changes production timer/model inputs.
fn phase_failure(expected: &str, minimum: Duration) {
    let started = Instant::now();
    let result = start_owned_board_only(actual_config());
    let mut failure = match result {
        Ok(bay) => {
            let _ = close(bay);
            panic!("missing phase evidence unexpectedly admitted an owner")
        }
        Err(error) => error,
    };
    assert_eq!(failure.error.to_string(), expected);
    assert!(
        started.elapsed() >= minimum,
        "phase failed before its actual observation bound"
    );
    let closed = if let Some(bay) = failure.retained.take() {
        close(bay)
    } else {
        failure
            .closed
            .take()
            .expect("actual phase failure cleanup receipt")
    };
    assert!(
        closed.unknown_evidence().is_empty(),
        "phase cleanup uncertainty: {:?}",
        closed.unknown_evidence()
    );
    assert!(closed.instance.clock.terminal_failure());
    assert!(closed.instance.clock.failure_fence().has_failed());
    assert!(closed.instance.clock.sample().is_none());
    println!(
        "ACTUAL_OWNED_PHASE_FAILURE {}",
        serde_json::json!({
        "phase_error":expected,"elapsed_ms":started.elapsed().as_millis(),
        "actual_process_spawned":true,"actual_local_unknown_count":closed.unknown_evidence().len(),
        "load_budget_ms":LOAD_LIMIT.as_millis(),"boot_budget_ms":STARTUP_LIMIT.as_millis(),
        "no_core_ready_claim":true})
    );
}
#[test]
#[ignore = "manual actual seven-board process, omitted script marker must expire the real load phase"]
fn actual_missing_owned_load_marker_is_bounded_and_joined() {
    attest_private_runtime();
    OMIT_LOAD_MARKER.store(true, Ordering::Release);
    phase_failure("owned script load deadline", LOAD_LIMIT);
    assert!(!OMIT_LOAD_MARKER.load(Ordering::Acquire));
}
#[test]
#[ignore = "manual actual seven-board process, missing real boot hook must expire the separate boot phase"]
fn actual_missing_boot_hook_after_owned_load_is_bounded_and_joined() {
    attest_private_runtime();
    OMIT_ONE_BOOT_HOOK.store(true, Ordering::Release);
    phase_failure("owned board boot deadline", STARTUP_LIMIT);
    assert!(!OMIT_ONE_BOOT_HOOK.load(Ordering::Acquire));
}

#[test]
fn clock_failure_fence_is_original_unready_and_sticky_after_drop() {
    let clock = BayClock::new();
    let fence = clock.failure_fence();
    let retained = fence.clone();
    let foreign = BayClock::new();
    assert!(!fence.has_failed());
    assert!(clock.sample().is_none());
    assert!(clock.monotonic_sample().is_none());
    assert!(clock.owns_failure_fence(&fence));
    assert!(!foreign.owns_failure_fence(&fence));
    drop(clock);
    assert!(fence.has_failed());
    assert!(retained.has_failed());
    assert!(!foreign.failure_fence().has_failed());
}
#[test]
fn clock_failure_fence_precedes_held_mutex_and_poison_unwind() {
    let clock = Arc::new(BayClock::new());
    let fence = clock.failure_fence();
    clock.observe(10).unwrap();
    clock
        .observe_gnss(0, 1_767_225_600_000, Instant::now())
        .unwrap();
    let source = clock.source_observation().unwrap();
    let held = clock.lock_state().unwrap();
    let worker_clock = clock.clone();
    let (entered, received) = mpsc::channel();
    let worker = thread::spawn(move || {
        entered.send(()).unwrap();
        worker_clock.fail();
    });
    let signaled = received.recv_timeout(Duration::from_secs(1)).is_ok();
    let deadline = Instant::now() + Duration::from_secs(1);
    let observed = loop {
        if fence.has_failed() {
            break true;
        }
        if Instant::now() >= deadline {
            break false;
        }
        thread::yield_now();
    };
    let still_held = !worker.is_finished();
    let source_failed_while_held = source.has_failed();
    // Always release and join even if the ordering assertion will fail.
    drop(held);
    let joined = worker.join();
    assert!(signaled, "worker did not reach its actual failure call");
    assert!(
        observed,
        "issued fence did not fail while state mutex remained held"
    );
    assert!(
        still_held,
        "worker must still be blocked by the genuinely held mutex"
    );
    joined.unwrap();
    assert!(
        source_failed_while_held,
        "retained ingress snapshot must share pure atomic fence"
    );
    assert!(fence.has_failed());
    let clock = Arc::new(BayClock::new());
    let fence = clock.failure_fence();
    clock.observe(10).unwrap();
    clock
        .observe_gnss(0, 1_767_225_600_000, Instant::now())
        .unwrap();
    let source = clock.source_observation().unwrap();
    let panicking_clock = clock.clone();
    let worker = thread::spawn(move || {
        let _actual_guard = panicking_clock.lock_state().unwrap();
        panic!("intentional clock-state unwind");
    });
    let panic = worker
        .join()
        .expect_err("the intentional guard unwind must occur");
    assert_eq!(
        panic.downcast_ref::<&str>(),
        Some(&"intentional clock-state unwind")
    );
    // Check BEFORE any sampler/terminal_failure call could notice poison.
    assert!(
        fence.has_failed(),
        "actual guard unwind must fence without a later sample"
    );
    assert!(
        source.has_failed(),
        "snapshot must fence before sampler notices poison"
    );
    assert!(clock.state.is_poisoned());
    assert!(clock.sample().is_none());
    assert!(clock.terminal_failure());
    assert!(fence.has_failed());
}

#[test]
fn clock_failure_fence_observes_actual_worker_unwind_without_sampler() {
    let clock = Arc::new(BayClock::new());
    let fence = clock.failure_fence();
    let worker_clock = clock.clone();
    let worker = thread::spawn(move || {
        let _actual_worker_exit = WorkerClockExit(worker_clock.failure_fence());
        panic!("intentional worker unwind");
    });
    let panic = worker
        .join()
        .expect_err("the intentional worker unwind must occur");
    assert_eq!(
        panic.downcast_ref::<&str>(),
        Some(&"intentional worker unwind")
    );
    assert!(
        fence.has_failed(),
        "worker termination must fence before a subsequent sampler"
    );
    assert!(clock.sample().is_none());
}

#[test]
fn source_observation_preserves_ingress_across_sampling_and_owner_clock_advances() {
    let clock = BayClock::new();
    let foreign = BayClock::new();
    assert!(clock.source_observation().is_none());
    clock.observe(10).unwrap();
    let observed_at = Instant::now();
    clock
        .observe_gnss(0, 1_767_225_600_000, observed_at)
        .unwrap();
    let first = clock.source_observation().unwrap();
    assert!(clock.owns_source_observation(&first));
    assert!(!foreign.owns_source_observation(&first));
    assert!(!first.has_failed());
    for modeled_now in [20, 30, 40] {
        clock.observe(modeled_now).unwrap();
        let next = clock.source_observation().unwrap();
        assert_eq!(next.ingress_index(), 1);
        assert_eq!(next.console_received_at(), observed_at);
        assert_eq!(next.modeled_fix_elapsed_ms(), 0);
        assert_eq!(next.modeled_fix_utc_ms(), 1_767_225_600_000);
        assert_eq!(next.sample().monotonic_ms, modeled_now);
        assert_eq!(next.sample().utc_ms, 1_767_225_600_000 + modeled_now);
    }
    assert_eq!(first.console_received_at(), observed_at);
    clock.fail();
    assert!(first.has_failed());
    assert!(clock.source_observation().is_none());
    assert!(!foreign.failure_fence().has_failed());
}

#[test]
fn source_observation_counts_actual_observations_and_fences_exhaustion() {
    let clock = BayClock::new();
    clock.observe(10).unwrap();
    let first_at = Instant::now();
    clock.observe_gnss(0, 1_767_225_600_000, first_at).unwrap();
    let first = clock.source_observation().unwrap();
    let next_at = Instant::now();
    // A second actual input counts even if its modeled values repeat. No
    // deduplication or authenticity claim is inferred from source values.
    clock.observe_gnss(0, 1_767_225_600_000, next_at).unwrap();
    let second = clock.source_observation().unwrap();
    assert_eq!(second.ingress_index(), 2);
    assert_eq!(second.console_received_at(), next_at);
    assert_eq!(first.ingress_index(), 1);
    assert_eq!(first.console_received_at(), first_at);
    clock
        .lock_state()
        .unwrap()
        .gnss_ingress
        .as_mut()
        .unwrap()
        .index = u64::MAX;
    assert!(clock
        .observe_gnss(0, 1_767_225_600_000, Instant::now())
        .is_err());
    assert!(first.has_failed() && second.has_failed());
    assert!(clock.source_observation().is_none());
}
