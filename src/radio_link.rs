//! Opt-in capacity model, not a proprietary RFD MAC or propagation model.
use anyhow::{ensure, Result};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RadioProfile {
    pub air_bits_per_second: u32,
    pub uart_baud: u32,
    pub tx_fifo_bytes: usize,
    pub rx_fifo_bytes: usize,
    pub max_burst_bytes: usize,
    pub packet_idle_us: u64,
    pub overhead_bits: u32,
    pub turn_us: u64,
}
impl RadioProfile {
    pub fn validate(&self, nodes: usize) -> Result<()> {
        ensure!(
            (2..=16).contains(&nodes),
            "radio model requires2..16 distinct nodes"
        );
        ensure!(
            (1..=10_000_000).contains(&self.air_bits_per_second),
            "invalid radio air rate"
        );
        ensure!(
            (1..=5_000_000).contains(&self.uart_baud),
            "invalid radio UART baud"
        );
        ensure!(
            (1..=65_536).contains(&self.max_burst_bytes),
            "invalid radio burst limit"
        );
        ensure!(
            (self.max_burst_bytes..=1_048_576).contains(&self.tx_fifo_bytes)
                && (self.max_burst_bytes..=1_048_576).contains(&self.rx_fifo_bytes),
            "radio FIFO budgets must hold a burst and stay within1MiB each"
        );
        ensure!(
            (1..=1_000_000).contains(&self.packet_idle_us)
                && self.turn_us <= 1_000_000
                && self.overhead_bits <= 65_536,
            "invalid radio packetization/turn overhead"
        );
        Ok(())
    }
    pub fn create_command(&self, name: &str) -> String {
        // The caller validates names and all numeric bounds before rendering.
        format!(
            "emulation CreateSedsRadioMedium \"{}\" {} {} {} {} {} {} {} {}\n",
            name,
            self.air_bits_per_second,
            self.uart_baud,
            self.tx_fifo_bytes,
            self.rx_fifo_bytes,
            self.max_burst_bytes,
            self.packet_idle_us,
            self.overhead_bits,
            self.turn_us
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn profile() -> RadioProfile {
        RadioProfile {
            air_bits_per_second: 64_000,
            uart_baud: 115_200,
            tx_fifo_bytes: 4096,
            rx_fifo_bytes: 4096,
            max_burst_bytes: 256,
            packet_idle_us: 2000,
            overhead_bits: 160,
            turn_us: 2000,
        }
    }
    #[test]
    fn three_radio_capacity_profile_is_explicit_and_bounded() {
        let p = profile();
        p.validate(3).unwrap();
        assert_eq!(p.create_command("rocket_radio"),
            "emulation CreateSedsRadioMedium \"rocket_radio\" 64000 115200 4096 4096 256 2000 160 2000\n");
        assert!(p.validate(1).is_err());
        assert!(p.validate(17).is_err());
        let mut bad = p.clone();
        bad.tx_fifo_bytes = 255;
        assert!(bad.validate(3).is_err());
        bad = p.clone();
        bad.rx_fifo_bytes = usize::MAX;
        assert!(bad.validate(3).is_err());
        bad = p;
        bad.air_bits_per_second = 0;
        assert!(bad.validate(3).is_err());
    }
    #[test]
    fn packetization_assumptions_cannot_be_silently_defaulted_or_misspelled() {
        let mut p = serde_json::to_value(profile()).unwrap();
        p.as_object_mut().unwrap().remove("packet_idle_us");
        assert!(serde_json::from_value::<RadioProfile>(p).is_err());
        let mut p = serde_json::to_value(profile()).unwrap();
        p["vendor_mac"] = true.into();
        assert!(serde_json::from_value::<RadioProfile>(p).is_err());
    }
}
