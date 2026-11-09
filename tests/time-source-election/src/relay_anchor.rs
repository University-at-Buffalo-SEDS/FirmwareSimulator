//! Bench reproduction: the Actuator Board's network clock stayed uptime-based.
//!
//! The real SDK routers are linked GS(source) - GB(consumer) - AB(consumer)
//! with one virtual clock. Each consumer starts from an uptime-based anchor,
//! as an announce from a source without real time (an RF board without a GPS
//! fix) leaves it. The GS then announces UTC and serves requests for a minute.
//! The gateway, one hop from the GS, is stepped to UTC by a verified exchange;
//! the board behind it never completes that exchange and keeps its anchor,
//! slewing at 5%. Board firmware therefore judges recovery freshness with a
//! station clock and restarts timesync when the two disagree by over 60 s.
//! If an SDK change lets relayed consumers complete the exchange, the second
//! assertion fails and this reproduction should be inverted.
use sedsnet::router::{Router, RouterConfig, RouterSideId, RouterSideOptions};
use sedsnet::timesync::{PartialNetworkTime, TimeSyncConfig, TimeSyncRole};
use std::sync::{Arc, Mutex, atomic::{AtomicU64, Ordering}};

const UTC: u64 = 1_794_183_600_000;
type Peer = Arc<Mutex<Option<(Arc<Router>, RouterSideId)>>>;

fn node(name: &'static str, role: TimeSyncRole, priority: u64, clock: &Arc<AtomicU64>) -> Arc<Router> {
    let clock = clock.clone();
    let cfg = RouterConfig::new([]).with_sender(name).with_timesync(TimeSyncConfig {
        role,
        priority,
        source_timeout_ms: 5_000,
        announce_interval_ms: 2_000,
        request_interval_ms: 2_000,
        ..TimeSyncConfig::default()
    });
    Arc::new(Router::new_with_clock(cfg, Box::new(move || clock.load(Ordering::SeqCst))))
}

fn link(a: &Arc<Router>, b: &Arc<Router>, names: (&str, &str), reliable: bool) {
    let (to_b, to_a): (Peer, Peer) = (Arc::new(Mutex::new(None)), Arc::new(Mutex::new(None)));
    let options = || RouterSideOptions { reliable_enabled: reliable, link_local_enabled: true, ..Default::default() };
    let peer = to_b.clone();
    let side_a = a.add_side_packed_with_options(names.0, move |bytes| {
        let (router, side) = peer.lock().unwrap().clone().unwrap();
        router.rx_packed_queue_from_side(bytes, side)
    }, options());
    let peer = to_a.clone();
    let side_b = b.add_side_packed_with_options(names.1, move |bytes| {
        let (router, side) = peer.lock().unwrap().clone().unwrap();
        router.rx_packed_queue_from_side(bytes, side)
    }, options());
    *to_b.lock().unwrap() = Some((b.clone(), side_b));
    *to_a.lock().unwrap() = Some((a.clone(), side_a));
}

#[test]
fn relayed_board_keeps_an_uptime_anchor_that_the_gateway_corrects() {
    let clock = Arc::new(AtomicU64::new(1_000));
    let gs = node("GS", TimeSyncRole::Source, 50, &clock);
    let gb = node("GB", TimeSyncRole::Consumer, 100, &clock);
    let ab = node("AB", TimeSyncRole::Consumer, 100, &clock);
    link(&gs, &gb, ("umbilical", "uart"), true);
    link(&gb, &ab, ("can", "can"), false);
    for board in [&gb, &ab] {
        board.update_network_time_source("uptime", 200, PartialNetworkTime::from_unix_ms(2_000), Some(1));
        board.clear_network_time_source("uptime");
    }
    let all = [gs.clone(), gb.clone(), ab.clone()];
    for router in &all {
        let _ = router.announce_discovery();
    }
    let mut now = 1_000;
    while now < 61_000 {
        clock.store(now, Ordering::SeqCst);
        gs.set_local_network_time(PartialNetworkTime::from_unix_ms(UTC + now));
        for router in &all {
            let _ = router.poll_discovery();
            let _ = router.poll_timesync();
            let _ = router.process_all_queues_with_timeout(0);
        }
        now += 10;
    }
    let offset = |router: &Router| router.network_time_ms().unwrap() as i64 - (UTC + now - 10) as i64;
    assert!(offset(&gb).abs() < 1_000, "gateway offset {}", offset(&gb));
    let board = ab.network_time_ms().unwrap();
    assert!(board < 1_000_000_000, "relayed board was corrected: {board}");
}
