use firmware_sim::peripherals::{exercise_all, PeripheralSpec};

#[test]
fn devices_survive_fault_and_disconnect_behaviors() {
    let base = |kind: &str, name: &str| PeripheralSpec {
        kind: kind.into(),
        name: name.into(),
        model: None,
        bus: None,
        failure_every: None,
        disconnect_after: None,
        bits: None,
        channels: None,
        max_psi: None,
    };
    let mut imu = base("imu", "imu1");
    imu.failure_every = Some(3);
    let mut baro = base("barometer", "baro1");
    baro.disconnect_after = Some(5);
    let mut adc = base("adc", "adc1");
    adc.bits = Some(12);
    adc.channels = Some(8);
    let mut pressure = base("pressure_transducer", "pt1");
    pressure.max_psi = Some(5000.0);
    let reports = exercise_all(&[imu, baro, base("gps", "gps1"), adc, pressure], 12, 42).unwrap();
    assert_eq!(reports.len(), 5);
    assert_eq!(reports[0].injected_errors, 4);
    assert_eq!(reports[1].disconnected_reads, 7);
}

#[test]
fn report_identifies_instruction_coupled_devices() {
    let coupled = PeripheralSpec {
        kind: "gps".into(),
        name: "gps1".into(),
        model: Some("neo_m9n".into()),
        bus: Some("spi1".into()),
        failure_every: None,
        disconnect_after: None,
        bits: None,
        channels: None,
        max_psi: None,
    };
    let report = exercise_all(&[coupled], 1, 1).unwrap();
    assert!(report[0].instruction_coupled);
    assert_eq!(report[0].model.as_deref(), Some("neo_m9n"));
    assert_eq!(report[0].bus.as_deref(), Some("spi1"));
}
