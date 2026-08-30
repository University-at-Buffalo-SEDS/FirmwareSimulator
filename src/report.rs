use crate::{bay::BayReport, simulator::SimulationReport, update::UpdateStrategy};

pub fn simulation(report: &SimulationReport) -> String {
    let ota = report
        .ota_bytes
        .map(bytes)
        .unwrap_or_else(|| "not packaged".into());
    let summary = table(
        &["Check", "Result", "Details"],
        vec![
            vec![
                "Artifact validation".into(),
                "PASS".into(),
                format!(
                    "firmware {}, bootloader {}, factory {}, OTA {}",
                    bytes(report.firmware_bytes),
                    bytes(report.bootloader_bytes),
                    bytes(report.factory_bytes),
                    ota,
                ),
            ],
            vec![
                "Physical memory".into(),
                "PASS".into(),
                format!(
                    "flash {}, RAM {}",
                    bytes(report.physical_flash_bytes as usize),
                    bytes(report.physical_ram_bytes as usize),
                ),
            ],
            vec![
                "Firmware boot".into(),
                pass(report.execution.firmware_boot_reached),
                format!(
                    "{} backend, {} ms",
                    report.execution.backend, report.execution.virtual_time_ms,
                ),
            ],
            vec![
                "Factory boot".into(),
                pass(report.execution.factory_boot_reached),
                "bootloader selected valid application".into(),
            ],
            vec![
                "Instruction execution".into(),
                pass(report.execution.instruction_execution_observed),
                report.execution.elf.clone(),
            ],
            vec![
                "Peripheral models".into(),
                pass(report.devices.iter().all(|device| device.fault_test_passed)),
                format!(
                    "{} exercised; {} fault-injection schedules passed",
                    report.devices.len(),
                    report
                        .devices
                        .iter()
                        .filter(|device| device.faults_configured)
                        .count(),
                ),
            ],
            vec![
                "SEDSNet traffic model".into(),
                pass(report.traffic.pool.allocation_failures == 0),
                format!(
                    "{} / {} packets, pool high-water {} / {}",
                    report.traffic.packets_dispatched,
                    report.traffic.packets_attempted,
                    bytes(report.traffic.pool.high_water),
                    bytes(report.traffic.pool.capacity),
                ),
            ],
            vec![
                "OTA interruption matrix".into(),
                "PASS".into(),
                format!(
                    "{}; {} chunks; {} interruption points",
                    strategy(report.update.strategy),
                    report.update.chunks,
                    report.update.interruption_points_tested,
                ),
            ],
            vec![
                "Firmware memory probes".into(),
                "PASS".into(),
                format!(
                    "{} thresholds satisfied",
                    report.execution.memory_profile.len()
                ),
            ],
        ],
    );

    let peripherals = if report.devices.is_empty() {
        "No configured peripheral models.".into()
    } else {
        table(
            &[
                "Peripheral",
                "Kind",
                "Model / Bus",
                "Reads",
                "Fault test",
                "Injected / Expected",
                "Disconnected / Expected",
            ],
            report
                .devices
                .iter()
                .map(|device| {
                    vec![
                        device.name.clone(),
                        device.kind.clone(),
                        format!(
                            "{} / {}",
                            device.model.as_deref().unwrap_or("behavioral"),
                            device.bus.as_deref().unwrap_or("-"),
                        ),
                        device.successful_reads.to_string(),
                        if device.faults_configured {
                            pass(device.fault_test_passed).into()
                        } else {
                            "N/A".into()
                        },
                        format!(
                            "{} / {}",
                            device.injected_errors, device.expected_injected_errors
                        ),
                        format!(
                            "{} / {}",
                            device.disconnected_reads, device.expected_disconnected_reads
                        ),
                    ]
                })
                .collect(),
        )
    };

    let probes = if report.execution.memory_profile.is_empty() {
        "No firmware memory probes configured.".into()
    } else {
        table(
            &["Probe", "Minimum", "Maximum", "End drop", "Samples"],
            report
                .execution
                .memory_profile
                .iter()
                .map(|probe| {
                    vec![
                        probe.name.clone(),
                        probe.minimum_observed.to_string(),
                        probe.maximum_observed.to_string(),
                        probe.end_drop.to_string(),
                        probe.samples.len().to_string(),
                    ]
                })
                .collect(),
        )
    };

    let registers = pairs(&report.execution.register_dump);
    format!(
        "Firmware simulation: {} ({})\n\nTEST MATRIX\n{}\n\nPERIPHERALS\n{}\n\nMEMORY PROBES\n{}\n\nCPU REGISTERS\n{}",
        report.board, report.architecture, summary, peripherals, probes, registers,
    )
}

pub fn bay(report: &BayReport) -> String {
    let summary = table(
        &["Check", "Result", "Details"],
        vec![
            vec![
                "Firmware nodes".into(),
                "PASS".into(),
                report.nodes_executed.to_string(),
            ],
            vec![
                "Virtual links".into(),
                "PASS".into(),
                report.links_connected.to_string(),
            ],
            vec![
                "Execution time".into(),
                "PASS".into(),
                format!("{} ms", report.virtual_time_ms),
            ],
        ],
    );
    let probes = table(
        &["Node", "Probe", "Minimum", "Maximum", "End drop", "Samples"],
        report
            .memory_profiles
            .iter()
            .flat_map(|(node, probes)| {
                probes.iter().map(move |probe| {
                    vec![
                        node.clone(),
                        probe.name.clone(),
                        probe.minimum_observed.to_string(),
                        probe.maximum_observed.to_string(),
                        probe.end_drop.to_string(),
                        probe.samples.len().to_string(),
                    ]
                })
            })
            .collect(),
    );
    format!(
        "Avionics bay simulation: {}\n\nTEST MATRIX\n{}\n\nMEMORY PROBES\n{}\n\nCPU REGISTERS\n{}",
        report.bay,
        summary,
        probes,
        pairs(&report.register_dump),
    )
}

fn pass(value: bool) -> String {
    if value { "PASS" } else { "FAIL" }.into()
}

fn strategy(value: UpdateStrategy) -> &'static str {
    match value {
        UpdateStrategy::DualSlot => "dual slot",
        UpdateStrategy::DeltaOnly => "delta only",
        UpdateStrategy::RecoveryTransport => "recovery transport",
    }
}

fn bytes(value: usize) -> String {
    if value >= 1024 {
        format!("{value} B ({:.2} KiB)", value as f64 / 1024.0)
    } else {
        format!("{value} B")
    }
}

fn pairs(values: &[String]) -> String {
    let rows: Vec<Vec<String>> = values
        .chunks(2)
        .map(|pair| {
            vec![
                pair.first().cloned().unwrap_or_default(),
                pair.get(1).cloned().unwrap_or_default(),
            ]
        })
        .collect();
    if rows.is_empty() {
        "No register snapshot returned.".into()
    } else {
        table(&["Register", "Value"], rows)
    }
}

fn table(headers: &[&str], rows: Vec<Vec<String>>) -> String {
    let mut widths: Vec<usize> = headers.iter().map(|value| value.len()).collect();
    for row in &rows {
        for (index, value) in row.iter().enumerate() {
            if index < widths.len() {
                widths[index] = widths[index].max(value.len());
            }
        }
    }
    let border = format!(
        "+{}+",
        widths
            .iter()
            .map(|width| "-".repeat(width + 2))
            .collect::<Vec<_>>()
            .join("+")
    );
    let render = |row: &[String]| {
        format!(
            "| {} |",
            row.iter()
                .enumerate()
                .map(|(index, value)| { format!("{value:<width$}", width = widths[index]) })
                .collect::<Vec<_>>()
                .join(" | ")
        )
    };
    let header: Vec<String> = headers.iter().map(|value| (*value).into()).collect();
    let mut lines = vec![border.clone(), render(&header), border.clone()];
    lines.extend(rows.iter().map(|row| render(row)));
    lines.push(border);
    lines.join("\n")
}

#[cfg(test)]
mod tests {
    use super::table;

    #[test]
    fn table_aligns_columns_and_includes_results() {
        let output = table(
            &["Check", "Result"],
            vec![vec!["Firmware boot".into(), "PASS".into()]],
        );
        assert!(output.contains("| Check         | Result |"));
        assert!(output.contains("| Firmware boot | PASS   |"));
        assert!(output
            .lines()
            .all(|line| line.len() == output.lines().next().unwrap().len()));
    }
}
