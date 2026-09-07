use anyhow::Result;
use clap::{Parser, Subcommand};
use firmware_sim::{
    core::{mcu_catalog, ArchitectureKind},
    layout::{BoardLayout, MemoryProbe},
    simulator,
};
use std::path::PathBuf;

fn configure_unacknowledged_can(layout: &mut BoardLayout) {
    layout.execution.can_acknowledged = false;
    configure_unacknowledged_can_probes(&mut layout.execution.memory_probes);
}

fn configure_unacknowledged_can_probes(probes: &mut [MemoryProbe]) {
    for probe in probes {
        if probe.name == "fdcan_tx_fail" {
            // STM32 HAL reports successful FIFO enqueue even while FDCAN
            // retries a frame that receives no physical-layer ACK. A HAL
            // failure is therefore valid but is not required proof of the
            // disconnected-bus stimulus.
            probe.minimum = None;
            probe.maximum = None;
        }
        if probe.name == "fdcan_tx_ok" {
            // Prove the firmware exercised its CAN transmit path; the rest of
            // the profile verifies it remains alive and memory-bounded.
            probe.minimum = Some(1);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::configure_unacknowledged_can_probes;
    use firmware_sim::layout::MemoryProbe;

    fn probe(name: &str, minimum: Option<u32>, maximum: Option<u32>) -> MemoryProbe {
        MemoryProbe {
            name: name.into(),
            symbol: name.into(),
            minimum,
            maximum,
            max_end_drop: None,
        }
    }

    #[test]
    fn disconnected_can_requires_tx_activity_without_inventing_a_hal_failure() {
        let mut probes = vec![
            probe("fdcan_tx_fail", Some(1), Some(5)),
            probe("fdcan_tx_ok", None, None),
            probe("allocator_failures", None, Some(0)),
        ];

        configure_unacknowledged_can_probes(&mut probes);

        assert_eq!(probes[0].minimum, None);
        assert_eq!(probes[0].maximum, None);
        assert_eq!(probes[1].minimum, Some(1));
        assert_eq!(probes[2].maximum, Some(0));
    }
}

#[derive(Parser)]
#[command(about = "Deterministic STM32 firmware behavior and update simulator")]
struct Cli {
    /// Emit machine-readable JSON instead of the terminal report matrix.
    #[arg(long, global = true)]
    json: bool,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    Validate {
        #[arg(long)]
        layout: PathBuf,
        #[arg(long, default_value = ".")]
        firmware_root: PathBuf,
    },
    Run {
        #[arg(long)]
        layout: PathBuf,
        #[arg(long, default_value = ".")]
        firmware_root: PathBuf,
        #[arg(long, default_value_t = 0x5ed5)]
        seed: u64,
        /// Model an isolated CAN controller receiving no physical-layer ACKs.
        #[arg(long)]
        can_unacknowledged: bool,
    },
    /// Run a longer real-firmware soak with interval allocator sampling.
    Profile {
        #[arg(long)]
        layout: PathBuf,
        #[arg(long, default_value = ".")]
        firmware_root: PathBuf,
        #[arg(long, default_value_t = 10_000)]
        virtual_time_ms: u64,
        #[arg(long, default_value_t = 20)]
        sample_count: usize,
        #[arg(long, default_value_t = 1_000_000)]
        traffic_iterations: usize,
        #[arg(long, default_value_t = 0x5ed5)]
        seed: u64,
        /// Model an isolated CAN controller receiving no physical-layer ACKs.
        #[arg(long)]
        can_unacknowledged: bool,
    },
    /// Execute multiple firmware ELFs in one deterministic, linked avionics bay.
    Bay {
        #[arg(long)]
        topology: PathBuf,
    },
    SelfTest {
        #[arg(long)]
        arch: ArchitectureKind,
    },
    /// Print the exact STM32 silicon models packaged in this image.
    ListMcus,
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Command::Validate {
            layout,
            firmware_root,
        } => {
            let layout = BoardLayout::load(&layout)?;
            simulator::validate(&layout, &firmware_root)?;
            println!("layout and firmware artifacts are valid");
        }
        Command::Run {
            layout,
            firmware_root,
            seed,
            can_unacknowledged,
        } => {
            let mut layout = BoardLayout::load(&layout)?;
            if can_unacknowledged {
                configure_unacknowledged_can(&mut layout);
            }
            match simulator::run(&layout, &firmware_root, seed) {
                Ok(report) if cli.json => println!("{}", serde_json::to_string_pretty(&report)?),
                Ok(report) => println!("{}", firmware_sim::report::simulation(&report)),
                Err(error) => {
                    eprintln!(
                        "{}",
                        serde_json::to_string_pretty(&simulator::diagnose(&layout, seed, &error))?
                    );
                    return Err(error);
                }
            }
        }
        Command::Profile {
            layout,
            firmware_root,
            virtual_time_ms,
            sample_count,
            traffic_iterations,
            seed,
            can_unacknowledged,
        } => {
            let mut layout = BoardLayout::load(&layout)?;
            layout.execution.virtual_time_ms = virtual_time_ms;
            layout.execution.sample_count = sample_count;
            layout.traffic.iterations = traffic_iterations;
            if can_unacknowledged {
                configure_unacknowledged_can(&mut layout);
            }
            match simulator::run(&layout, &firmware_root, seed) {
                Ok(report) if cli.json => println!("{}", serde_json::to_string_pretty(&report)?),
                Ok(report) => println!("{}", firmware_sim::report::simulation(&report)),
                Err(error) => {
                    eprintln!(
                        "{}",
                        serde_json::to_string_pretty(&simulator::diagnose(&layout, seed, &error))?
                    );
                    return Err(error);
                }
            }
        }
        Command::SelfTest { arch } => {
            simulator::self_test(arch)?;
            println!("{arch} simulator self-test passed");
        }
        Command::Bay { topology } => {
            let report = firmware_sim::bay::run(&topology)?;
            if cli.json {
                println!("{}", serde_json::to_string_pretty(&report)?);
            } else {
                println!("{}", firmware_sim::report::bay(&report));
            }
        }
        Command::ListMcus => {
            println!("{}", serde_json::to_string_pretty(mcu_catalog())?);
        }
    }
    Ok(())
}
