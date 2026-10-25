//! Firmware consumer holdover policy against the real SDK election algorithm.
#[cfg(test)]
mod tests {
    use sedsnet::timesync::{
        TimeSyncConfig, TimeSyncLeader, TimeSyncRole, TimeSyncTracker,
        build_timesync_announce_with_sender,
    };

    #[test]
    fn promoted_board_yields_to_returning_groundstation_then_rf() {
        // Each board's C regression compiles its actual helper and verifies 100.
        let mut tracker = TimeSyncTracker::new(TimeSyncConfig {
            role: TimeSyncRole::Consumer,
            priority: 100,
            source_timeout_ms: 1_000,
            consumer_promotion_enabled: true,
            ..Default::default()
        });
        let announce = |source: &'static str, priority: u64, now: u64| {
            build_timesync_announce_with_sender(source, priority, 1_767_225_600_000 + now).unwrap()
        };
        tracker.handle_announce(&announce("RF", 1, 0), 0).unwrap();
        assert!(matches!(tracker.leader(0, true), Some(TimeSyncLeader::Remote(source)) if source.sender == "RF"));
        tracker.refresh(1_001);
        assert!(matches!(tracker.leader(1_001, true), Some(TimeSyncLeader::Local { priority: 100 })));
        tracker.handle_announce(&announce("GS", 50, 1_100), 1_100).unwrap();
        assert!(matches!(tracker.leader(1_100, true), Some(TimeSyncLeader::Remote(source)) if source.sender == "GS"));
        tracker.handle_announce(&announce("RF", 1, 1_200), 1_200).unwrap();
        assert!(matches!(tracker.leader(1_200, true), Some(TimeSyncLeader::Remote(source)) if source.sender == "RF"));
        assert!(!tracker.should_announce(1_200, true));
    }
}
