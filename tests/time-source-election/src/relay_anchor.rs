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

/// RFBoard26's timesync configuration: a priority-1 Source only with GPS UTC
/// (`gps_utc`), otherwise a priority-100 consumer, as telemetry.c applies it.
fn configure_rf(rf: &Router, gps_utc: Option<u64>) {
    let (role, priority) = if gps_utc.is_some() { (TimeSyncRole::Source, 1) } else { (TimeSyncRole::Consumer, 100) };
    rf.set_timesync_config(Some(TimeSyncConfig {
        role,
        priority,
        source_timeout_ms: 5_000,
        announce_interval_ms: 2_000,
        request_interval_ms: 2_000,
        consumer_promotion_enabled: true,
        max_slew_ppm: 50_000,
    }));
    if let Some(utc) = gps_utc {
        rf.set_local_network_time(PartialNetworkTime::from_unix_ms(utc));
    }
}

/// GS - GB - AB as above, plus the RF board reached through the gateway.
/// RF, GB and AB boot first; the GS arrives two seconds later.
fn rf_network(clock: &Arc<AtomicU64>, rf_gps: impl Fn(u64) -> Option<u64>, rf_source_without_fix: bool) -> (Arc<Router>, Arc<Router>) {
    let gs = node("GS", TimeSyncRole::Source, 50, clock);
    let gb = node("GB", TimeSyncRole::Consumer, 100, clock);
    let ab = node("AB", TimeSyncRole::Consumer, 100, clock);
    let rf = node("RF", TimeSyncRole::Consumer, 100, clock);
    if rf_source_without_fix {
        // The former firmware: Source at priority 1 from boot, with no date.
        rf.set_timesync_config(Some(TimeSyncConfig {
            role: TimeSyncRole::Source,
            priority: 1,
            source_timeout_ms: 5_000,
            announce_interval_ms: 2_000,
            request_interval_ms: 2_000,
            ..TimeSyncConfig::default()
        }));
    } else {
        configure_rf(&rf, None);
    }
    link(&gs, &gb, ("umbilical", "uart"), true);
    link(&gb, &ab, ("can", "can"), false);
    link(&gb, &rf, ("radio", "radio"), true);
    let all = [rf.clone(), gb.clone(), ab.clone(), gs.clone()];
    let mut source = false;
    let mut now = 1_000;
    while now < 41_000 {
        clock.store(now, Ordering::SeqCst);
        let active = if now < 3_000 { &all[..3] } else { &all[..] };
        if now >= 3_000 {
            gs.set_local_network_time(PartialNetworkTime::from_unix_ms(UTC + now));
        }
        if let Some(utc) = rf_gps(now) {
            if !source {
                configure_rf(&rf, Some(utc));
                source = true;
            } else if now % 1_000 == 0 {
                rf.set_local_network_time(PartialNetworkTime::from_unix_ms(utc));
            }
        }
        for router in active {
            let _ = router.poll_discovery();
            let _ = router.poll_timesync();
            let _ = router.process_all_queues_with_timeout(0);
        }
        now += 10;
    }
    (ab, rf)
}

/// The former RF firmware announced its uptime at priority 1 before a GPS
/// fix. The relayed board anchors to it and, slewing at 5%, is still far from
/// UTC long after the GS arrives.
#[test]
fn rf_source_without_a_fix_becomes_the_relayed_board_uptime_anchor() {
    let clock = Arc::new(AtomicU64::new(1_000));
    let (ab, _) = rf_network(&clock, |_| None, true);
    let board = ab.network_time_ms().unwrap();
    assert!(board < 1_000_000_000, "relayed board left the RF uptime anchor: {board}");
}

/// RFBoard26 now stays a consumer until GPS has UTC, so the relayed board's
/// first anchor is the GS; once RF has a fix it leads with GPS UTC.
#[test]
fn rf_without_a_fix_is_not_an_anchor_and_leads_once_gps_has_utc() {
    let clock = Arc::new(AtomicU64::new(1_000));
    // No fix for the whole run: the relayed board anchors to the GS UTC.
    let (ab, rf) = rf_network(&clock, |_| None, false);
    let now = clock.load(Ordering::SeqCst);
    let offset = ab.network_time_ms().unwrap() as i64 - (UTC + now) as i64;
    assert!(offset.abs() < 1_000, "relayed board offset without RF fix {offset}");
    assert!(rf.network_time_ms().unwrap() > UTC, "RF follows the GS");
    // A GPS fix at 20 s makes RF the priority-1 source with UTC; the relayed
    // board stays on UTC through the leader change.
    let clock = Arc::new(AtomicU64::new(1_000));
    let (ab, rf) = rf_network(&clock, |now| (now >= 20_000).then_some(UTC + now), false);
    let now = clock.load(Ordering::SeqCst);
    for router in [&ab, &rf] {
        let offset = router.network_time_ms().unwrap() as i64 - (UTC + now) as i64;
        assert!(offset.abs() < 1_000, "offset after RF fix {offset}");
    }
}
