use firmware_sim::traffic::{run, TrafficConfig};

#[test]
fn immediate_dispatch_does_not_need_a_queue() {
    let report = run(
        &TrafficConfig {
            iterations: 100_000,
            max_payload: 512,
            queue_depth: 0,
            immediate_dispatch: true,
        },
        4096,
        9,
    )
    .unwrap();
    assert_eq!(report.packets_dispatched, 100_000);
    assert_eq!(report.pool.bytes_in_use, 0);
    assert_eq!(report.pool.allocation_failures, 0);
}

#[test]
fn overload_is_bounded_and_drains() {
    let report = run(
        &TrafficConfig {
            iterations: 10_000,
            max_payload: 1024,
            queue_depth: 16,
            immediate_dispatch: false,
        },
        2048,
        9,
    )
    .unwrap();
    assert_eq!(report.pool.bytes_in_use, 0);
    assert!(report.pool.high_water <= report.pool.capacity);
}
