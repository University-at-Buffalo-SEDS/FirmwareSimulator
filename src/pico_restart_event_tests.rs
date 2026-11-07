use super::*;
fn scene() -> BayTopology {
    serde_json::from_value(serde_json::json!({
        "name":"restart shape", "sample_count":5, "nodes":[],
        "links":[{"name":"fill_pico","kind":"pico_fi","endpoints":[]}],
        "host_nodes":[{"name":"groundstation","binary":"unused",
            "serial_links":[{"link":"fill_pico","env":"GS_FILL_PICO_DISCOVERY_PORT","transport":"pico_fi_discovery_to_uart"}]}],
        "pico_restarts":[{"link":"fill_pico","after_sample":1},{"link":"fill_pico","after_sample":3}]
    })).unwrap()
}
#[test]
fn pico_reset_events_require_original_discovered_link_and_distinct_bounded_samples() {
    let mut topology = scene();
    assert!(validate_pico_restarts(&topology).is_ok());
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("topology.json");
    fs::write(
        &path,
        serde_json::to_vec(&serde_json::json!({
            "name":"capture cannot invent lifecycle", "nodes":[],
            "pico_restarts":[{"link":"fill_pico","after_sample":1}]
        }))
        .unwrap(),
    )
    .unwrap();
    let error = check_capture(&path, &directory.path().join("missing-capture")).unwrap_err();
    assert!(error
        .to_string()
        .contains("require live software Pico execution"));
    topology.pico_restarts[0].after_sample = 0;
    assert!(validate_pico_restarts(&topology).is_err());
    topology.pico_restarts[0].after_sample = 5;
    assert!(validate_pico_restarts(&topology).is_err());
    topology.pico_restarts[0].after_sample = 3;
    assert!(validate_pico_restarts(&topology).is_err());
    topology.pico_restarts[0].after_sample = 1;
    topology.pico_restarts[0].link = "not-the-original-link".into();
    assert!(validate_pico_restarts(&topology).is_err());
    topology = scene();
    topology.host_nodes[0].serial_links[0].transport = HostLinkTransport::PicoFiTcpToUart;
    assert!(validate_pico_restarts(&topology).is_err());
    topology = scene();
    for n in 0..17 {
        topology.pico_restarts.push(PicoRestartEvent {
            link: "fill_pico".into(),
            after_sample: n,
        });
    }
    assert!(validate_pico_restarts(&topology).is_err());
}
#[test]
fn absent_pico_reset_field_preserves_legacy_empty_behavior() {
    let topology: BayTopology =
        serde_json::from_value(serde_json::json!({"name":"legacy","nodes":[]})).unwrap();
    assert!(topology.pico_restarts.is_empty());
    assert!(validate_pico_restarts(&topology).is_ok());
}

#[test]
fn restart_worker_owner_joins_on_error_exit_and_reports_actual_worker_failure() {
    use std::net::{TcpListener, TcpStream};
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let mut host = TcpStream::connect(listener.local_addr().unwrap()).unwrap();
    host.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
    let (socket, _) = listener.accept().unwrap();
    let stop = Arc::new(AtomicBool::new(false));
    let stopped = stop.clone();
    let mut owner = PicoRestartWorkers {
        stop: stop.clone(),
        handles: vec![],
    };
    owner.handles.push(thread::spawn(move || {
        while !stopped.load(Ordering::Acquire) {
            thread::sleep(Duration::from_millis(1));
        }
        drop(socket);
        Ok(())
    }));
    fn fail_with_owner(_owner: PicoRestartWorkers) -> Result<()> {
        bail!("controlled caller error before normal teardown")
    }
    assert!(fail_with_owner(owner).is_err());
    assert!(stop.load(Ordering::Acquire));
    let mut byte = [0];
    assert_eq!(
        host.read(&mut byte).unwrap(),
        0,
        "actual worker socket must be closed before error returns"
    );
    let mut owner = PicoRestartWorkers {
        stop,
        handles: vec![thread::spawn(|| bail!("controlled worker error"))],
    };
    assert!(owner.stop_and_join().is_err());
    assert!(owner.handles.is_empty());
}
