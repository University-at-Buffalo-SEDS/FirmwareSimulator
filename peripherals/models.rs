use anyhow::{bail, ensure, Result};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Deserialize)]
pub struct PeripheralSpec {
    #[serde(rename = "type")]
    pub kind: String,
    pub name: String,
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub bus: Option<String>,
    #[serde(default)]
    pub failure_every: Option<u64>,
    #[serde(default)]
    pub disconnect_after: Option<u64>,
    #[serde(default)]
    pub bits: Option<u8>,
    #[serde(default)]
    pub channels: Option<u8>,
    #[serde(default)]
    pub max_psi: Option<f64>,
    #[serde(default)]
    pub channel_samples: Vec<u32>,
    #[serde(default)]
    pub noise_lsb: Option<u32>,
}

#[derive(Clone, Debug, Serialize)]
pub struct DeviceReport {
    pub name: String,
    pub kind: String,
    pub model: Option<String>,
    pub bus: Option<String>,
    pub instruction_coupled: bool,
    pub instruction_faults_configured: bool,
    pub behavioral_counts_only: bool,
    pub successful_reads: u64,
    pub injected_errors: u64,
    pub disconnected_reads: u64,
}

pub fn exercise_all(
    specs: &[PeripheralSpec],
    iterations: u64,
    seed: u64,
) -> Result<Vec<DeviceReport>> {
    specs
        .iter()
        .enumerate()
        .map(|(index, spec)| exercise(spec, iterations, seed ^ ((index as u64 + 1) * 0x9e37_79b9)))
        .collect()
}

fn exercise(spec: &PeripheralSpec, iterations: u64, seed: u64) -> Result<DeviceReport> {
    validate(spec)?;
    let mut rng = Lcg(seed.max(1));
    let mut report = DeviceReport {
        name: spec.name.clone(),
        kind: spec.kind.clone(),
        model: spec.model.clone(),
        bus: spec.bus.clone(),
        instruction_coupled: spec.model.is_some() && spec.bus.is_some(),
        instruction_faults_configured: spec.model.is_some()
            && (spec.failure_every.is_some() || spec.disconnect_after.is_some()),
        behavioral_counts_only: true,
        successful_reads: 0,
        injected_errors: 0,
        disconnected_reads: 0,
    };
    for attempt in 1..=iterations {
        if spec.disconnect_after.is_some_and(|limit| attempt > limit) {
            report.disconnected_reads += 1;
        } else if spec
            .failure_every
            .is_some_and(|period| period > 0 && attempt % period == 0)
        {
            report.injected_errors += 1;
        } else {
            sample(spec, &mut rng);
            report.successful_reads += 1;
        }
    }
    Ok(report)
}

fn validate(spec: &PeripheralSpec) -> Result<()> {
    ensure!(!spec.name.is_empty(), "peripheral name cannot be empty");
    ensure!(
        spec.model.is_some() == spec.bus.is_some(),
        "peripheral model and bus must be specified together"
    );
    if let Some(model) = spec.model.as_deref() {
        let supported = matches!(
            (spec.kind.as_str(), model),
            ("gps", "neo_m9n")
                | ("imu", "bmi088")
                | ("barometer", "bmp390")
                | ("adc", "ltc2990")
                | ("adc", "stm32_adc")
                | ("pressure_transducer", "stm32_adc")
                | ("storage", "sd_card")
        );
        ensure!(supported, "unsupported {0} model {model}", spec.kind);
    }
    match spec.kind.as_str() {
        "imu" | "barometer" | "gps" | "storage" => Ok(()),
        "adc" => {
            ensure!(
                (1..=16).contains(&spec.bits.unwrap_or(12)),
                "ADC bits must be 1..=16"
            );
            ensure!(spec.channels.unwrap_or(1) > 0, "ADC needs a channel");
            let maximum = (1_u32 << spec.bits.unwrap_or(12)) - 1;
            ensure!(
                spec.channel_samples.iter().all(|sample| *sample <= maximum),
                "ADC channel sample exceeds configured resolution"
            );
            Ok(())
        }
        "pressure_transducer" => {
            ensure!(
                spec.max_psi.unwrap_or(5000.0) > 0.0,
                "max_psi must be positive"
            );
            Ok(())
        }
        other => bail!("unsupported peripheral type {other}"),
    }
}

fn sample(spec: &PeripheralSpec, rng: &mut Lcg) {
    match spec.kind.as_str() {
        "imu" => {
            let _ = [rng.signed(), rng.signed(), 1.0 + rng.signed() * 0.02];
        }
        "barometer" => {
            let _ = 101_325.0 + rng.signed() * 120.0;
        }
        "gps" => {
            let _ = (
                43.000 + rng.signed() * 0.001,
                -78.780 + rng.signed() * 0.001,
            );
        }
        "adc" => {
            let max = (1_u32 << spec.bits.unwrap_or(12)) - 1;
            for _ in 0..spec.channels.unwrap_or(1) {
                let _ = rng.next() % (max + 1);
            }
        }
        "pressure_transducer" => {
            let _ = rng.unit() * spec.max_psi.unwrap_or(5000.0);
        }
        "storage" => {}
        _ => unreachable!(),
    }
}

struct Lcg(u64);
impl Lcg {
    fn next(&mut self) -> u32 {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        (self.0 >> 32) as u32
    }
    fn unit(&mut self) -> f64 {
        self.next() as f64 / u32::MAX as f64
    }
    fn signed(&mut self) -> f64 {
        self.unit() * 2.0 - 1.0
    }
}
