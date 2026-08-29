use anyhow::Result;
use clap::{Parser, Subcommand};
use firmware_sim::{core::ArchitectureKind, layout::BoardLayout, simulator};
use std::path::PathBuf;

#[derive(Parser)]
#[command(about = "Deterministic STM32 firmware behavior and update simulator")]
struct Cli {
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
}

fn main() -> Result<()> {
    match Cli::parse().command {
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
        } => {
            let layout = BoardLayout::load(&layout)?;
            match simulator::run(&layout, &firmware_root, seed) {
                Ok(report) => println!("{}", serde_json::to_string_pretty(&report)?),
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
        } => {
            let mut layout = BoardLayout::load(&layout)?;
            layout.execution.virtual_time_ms = virtual_time_ms;
            layout.execution.sample_count = sample_count;
            layout.traffic.iterations = traffic_iterations;
            match simulator::run(&layout, &firmware_root, seed) {
                Ok(report) => println!("{}", serde_json::to_string_pretty(&report)?),
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
            println!("{}", serde_json::to_string_pretty(&report)?);
        }
    }
    Ok(())
}
