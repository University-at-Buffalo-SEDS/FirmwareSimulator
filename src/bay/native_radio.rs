//! Shared RFD900x-class air medium for the native bay, on virtual time, from the topology's RadioProfile (the same
//! parameters as the Renode bay's SedsRadioMedium): each station's modem takes UART bytes into a bounded TX FIFO
//! (overflow drops bytes: no RTS/CTS), closes a packet after `packet_idle_us` of UART idle or at `max_burst_bytes`,
//! and sends it on one half-duplex channel (`turn_us` turnaround, `overhead_bits` framing per copy, broadcast sent
//! 1 + global retries times; copies after the first are lost/duplicates-suppressed). Receivers get the packet into a
//! bounded RX FIFO drained to their UART at the modem's baud. Directed cuts and per-path loss (ppm, seeded) apply per
//! air copy. Coarser than the Renode model: no per-modem register/AT command model, no carrier-sense or ACK retry
//! policies, no frequency hopping; see docs/native-sitl.md (Renode-only: radio byte timing).
use super::*;
use std::collections::VecDeque;

pub struct Modem {
    pub name: String,
    tx: VecDeque<u8>,
    tx_last_byte: u64,
    rx: VecDeque<u8>,
    pub rx_free_at: u64,
    pub stats: [u64; 5], // bytes in, tx overflow drops, packets sent, packets received, rx overflow drops
}

pub struct Medium {
    pub link: String,
    p: RadioProfile,
    pub modems: Vec<Modem>,
    air_free_at: u64,
    in_flight: Vec<(u64, usize, Vec<u8>)>, // (arrival, sender, bytes)
    pub cut: BTreeMap<(String, String), bool>,
    pub loss_ppm: BTreeMap<(String, String), u32>,
    retries: u32,
    rng: u64,
}

impl Medium {
    pub fn new(link: &str, p: RadioProfile, names: &[String], seed: u64) -> Self {
        let retries = env::var("FIRMWARE_SIM_RFD_GLOBAL_RETRIES").ok().and_then(|v| v.parse().ok()).unwrap_or(0);
        Self {
            link: link.into(),
            modems: names
                .iter()
                .map(|n| Modem { name: n.clone(), tx: VecDeque::new(), tx_last_byte: 0, rx: VecDeque::new(), rx_free_at: 0, stats: [0; 5] })
                .collect(),
            p,
            air_free_at: 0,
            in_flight: Vec::new(),
            cut: BTreeMap::new(),
            loss_ppm: BTreeMap::new(),
            retries,
            rng: seed.wrapping_mul(0x9E3779B97F4A7C15) | 1,
        }
    }
    fn rand_ppm(&mut self) -> u32 {
        self.rng ^= self.rng << 13;
        self.rng ^= self.rng >> 7;
        self.rng ^= self.rng << 17;
        (self.rng % 1_000_000) as u32
    }
    pub fn index(&self, name: &str) -> Option<usize> {
        self.modems.iter().position(|m| m.name == name)
    }
    /// UART bytes from a station into its modem at virtual time t.
    pub fn uart_in(&mut self, i: usize, t: u64, bytes: &[u8]) {
        let m = &mut self.modems[i];
        for &b in bytes {
            if m.tx.len() < self.p.tx_fifo_bytes {
                m.tx.push_back(b);
            } else {
                m.stats[1] += 1;
            }
        }
        m.stats[0] += bytes.len() as u64;
        let byte_ns = 10_000_000_000 / self.p.uart_baud as u64;
        m.tx_last_byte = t + bytes.len() as u64 * byte_ns;
    }
    /// Advance the air to `t`: send closed packets, deliver arrivals into RX FIFOs.
    pub fn step(&mut self, t: u64) {
        let idle_ns = self.p.packet_idle_us * 1000;
        loop {
            let mut sent = false;
            for i in 0..self.modems.len() {
                let ready = {
                    let m = &self.modems[i];
                    !m.tx.is_empty() && (m.tx.len() >= self.p.max_burst_bytes || t >= m.tx_last_byte + idle_ns)
                };
                if !ready {
                    continue;
                }
                let start = self.air_free_at.max(self.modems[i].tx_last_byte.min(t)).max(t.saturating_sub(idle_ns));
                if start > t {
                    continue;
                }
                let n = self.modems[i].tx.len().min(self.p.max_burst_bytes);
                let bytes: Vec<u8> = self.modems[i].tx.drain(..n).collect();
                let copy_ns = ((bytes.len() as u64 * 8 + self.p.overhead_bits as u64) * 1_000_000_000) / self.p.air_bits_per_second as u64;
                let copies = 1 + self.retries as u64;
                let end = start + self.p.turn_us * 1000 + copy_ns * copies;
                self.air_free_at = end;
                self.modems[i].stats[2] += 1;
                // a receiver gets the packet if any copy survives its path
                for j in 0..self.modems.len() {
                    if j == i {
                        continue;
                    }
                    let key = (self.modems[i].name.clone(), self.modems[j].name.clone());
                    if *self.cut.get(&key).unwrap_or(&false) {
                        continue;
                    }
                    let loss = *self.loss_ppm.get(&key).unwrap_or(&0);
                    let mut ok = false;
                    for c in 0..copies {
                        if loss == 0 || self.rand_ppm() >= loss {
                            ok = true;
                            self.in_flight.push((start + self.p.turn_us * 1000 + copy_ns * (c + 1), j, bytes.clone()));
                            break;
                        }
                    }
                    let _ = ok;
                }
                sent = true;
            }
            if !sent {
                break;
            }
        }
        self.in_flight.sort_by_key(|x| x.0);
        while self.in_flight.first().is_some_and(|x| x.0 <= t) {
            let (_, j, bytes) = self.in_flight.remove(0);
            let m = &mut self.modems[j];
            m.stats[3] += 1;
            for b in bytes {
                if m.rx.len() < self.p.rx_fifo_bytes {
                    m.rx.push_back(b);
                } else {
                    m.stats[4] += 1;
                }
            }
        }
    }
    /// RX FIFO bytes that leave the modem's UART before `horizon`, with their start time.
    pub fn uart_out(&mut self, i: usize, t: u64, horizon: u64) -> Option<(u64, Vec<u8>)> {
        let byte_ns = 10_000_000_000 / self.p.uart_baud as u64;
        let m = &mut self.modems[i];
        if m.rx.is_empty() {
            return None;
        }
        let start = m.rx_free_at.max(t);
        if start >= horizon {
            return None;
        }
        let n = (((horizon - start) / byte_ns).max(1) as usize).min(m.rx.len());
        let bytes: Vec<u8> = m.rx.drain(..n).collect();
        m.rx_free_at = start + n as u64 * byte_ns;
        Some((start, bytes))
    }
    pub fn next_event(&self) -> u64 {
        let idle_ns = self.p.packet_idle_us * 1000;
        let mut t = self.in_flight.first().map(|x| x.0).unwrap_or(u64::MAX);
        for m in &self.modems {
            if !m.tx.is_empty() {
                t = t.min(m.tx_last_byte + idle_ns);
            }
            if !m.rx.is_empty() {
                t = t.min(m.rx_free_at);
            }
        }
        t
    }
}
