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

/// RFD ASYNC 4.03 command mode for a modelled station modem (FIRMWARE_SIM_RFD_MODEM, as the Renode medium):
/// "+++" with 1 s guard times, echoed command lines, ATI/ATI5/ATSn?/ATSn=/AT&W/ATZ/ATO/AT+F?; ATSn= changes RAM,
/// AT&W saves it and ATZ (a 1.5 s reboot) makes it active. No air data passes in command mode or while rebooting.
#[derive(Clone)]
pub struct ModemRegs {
    regs: BTreeMap<u32, (String, i64, i64, i64)>, // number -> (name, ram, eeprom, active)
    state: u8,                                    // 0 data, 1 command, 2 rebooting
    pluses: u8,
    plus_t: u64,
    last_host_t: u64,
    line: String,
    reboot_until: u64,
}
impl ModemRegs {
    fn new(overrides: &str) -> Self {
        let mut regs = BTreeMap::new();
        for (n, name, v) in [(0, "FORMAT", 27), (1, "SERIAL_SPEED", 115), (2, "AIR_SPEED", 64), (3, "MAX_DATA", 3968), (4, "MAX_RETRIES", 10),
            (5, "GLOBAL_RETRIES", 10), (6, "SER_BRK_DETMS", 50), (7, "TX_ENCAP_METHOD", 0), (8, "RX_ENCAP_METHOD", 0), (9, "NETID", 25),
            (10, "NODEID", 2), (11, "DESTID", 32768), (12, "TXPOWER", 30), (13, "MAVLINK1", 0), (14, "MIN_FREQ", 915000),
            (15, "MAX_FREQ", 928000), (16, "NUM_CHANNELS", 20), (17, "DUTY_CYCLE", 100), (18, "LBT_RSSI", 0), (19, "RTSCTS", 0),
            (20, "MAX_WINDOW", 131), (22, "RESV", 0)] {
            regs.insert(n, (name.to_string(), v, v, v));
        }
        for item in overrides.split(',').map(str::trim).filter(|x| !x.is_empty()) {
            if let Some((left, v)) = item.rsplit_once('=') {
                let (num, name) = left.split_once(':').map(|(a, b)| (a, Some(b))).unwrap_or((left, None));
                if let (Ok(n), Ok(v)) = (num.parse::<u32>(), v.parse::<i64>()) {
                    let e = regs.entry(n).or_insert((format!("S{n}"), v, v, v));
                    if let Some(nm) = name { e.0 = nm.to_string(); }
                    e.1 = v; e.2 = v; e.3 = v;
                }
            }
        }
        Self { regs, state: 0, pluses: 0, plus_t: 0, last_host_t: 0, line: String::new(), reboot_until: 0 }
    }
    fn active(&self, name: &str) -> Option<i64> {
        self.regs.values().find(|r| r.0 == name).map(|r| r.3)
    }
}

pub struct Modem {
    pub name: String,
    pub cmd: Option<ModemRegs>,
    tx: VecDeque<u8>,
    tx_last_byte: u64,
    rx: VecDeque<u8>,
    pub rx_free_at: u64,
    pub stats: [u64; 5], // bytes in, tx overflow drops, packets sent, packets received, rx overflow drops
    /// A host station's bytes read from its PTY but not yet on its UART: the PTY is drained at once (a station's
    /// writes never stall on the bay) and the bytes enter the modem at the UART baud, as the station's kernel
    /// serial buffer would send them. `host_free_at`: when the UART finishes the last byte fed so far.
    host_staged: VecDeque<u8>,
    host_free_at: u64,
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

fn modem_spec(index: usize) -> Option<ModemRegs> {
    let spec = env::var("FIRMWARE_SIM_RFD_MODEM").ok()?;
    for part in spec.split(';') {
        if let Some((i, o)) = part.split_once('=') {
            if i.trim().parse::<usize>().ok() == Some(index) {
                return Some(ModemRegs::new(o));
            }
        }
    }
    None
}

impl Medium {
    fn say(&mut self, i: usize, text: &str) {
        let m = &mut self.modems[i];
        for b in text.bytes() {
            if m.rx.len() < self.p.rx_fifo_bytes { m.rx.push_back(b); } else { m.stats[4] += 1; }
        }
    }
    fn execute(&mut self, i: usize, cmd: &str, t: u64) {
        let mut reply = String::new();
        {
            let r = self.modems[i].cmd.as_mut().unwrap();
            if cmd == "AT" { reply += "OK\r\n"; }
            else if cmd == "ATI" { reply += "RFD ASYNC 4.03 on RFD900X2\r\n"; }
            else if cmd == "ATI5" { for (n, v) in &r.regs { reply += &format!("S{}:{}={}\r\n", n, v.0, v.1); } }
            else if cmd == "AT+F?" { for slot in 0..10 { reply += &format!("{slot}:0->0,"); } reply += " | FwdBuff=13320\r\n"; }
            else if cmd == "AT&W" { for v in r.regs.values_mut() { v.2 = v.1; } reply += "OK\r\n"; }
            else if cmd == "ATZ" { r.state = 2; r.reboot_until = t + 1_500_000_000; }
            else if cmd == "ATO" { r.state = 0; }
            else if let Some(rest) = cmd.strip_prefix("ATS") {
                if let Some(q) = rest.strip_suffix('?') {
                    match q.parse::<u32>().ok().and_then(|n| r.regs.get(&n)) { Some(v) => reply += &format!("{}\r\n", v.1), None => reply += "ERROR\r\n" }
                } else if let Some((n, v)) = rest.split_once('=') {
                    match (n.parse::<u32>(), v.parse::<i64>()) {
                        (Ok(n), Ok(v)) if r.regs.contains_key(&n) => { r.regs.get_mut(&n).unwrap().1 = v; reply += "OK\r\n"; }
                        _ => reply += "ERROR\r\n",
                    }
                } else { reply += "ERROR\r\n"; }
            } else { reply += "ERROR\r\n"; }
        }
        if !reply.is_empty() { self.say(i, &reply); }
    }
    /// Command-mode handling of one host byte; false: the byte is air data.
    fn modem_input(&mut self, i: usize, b: u8, t: u64) -> bool {
        let Some(r) = self.modems[i].cmd.as_mut() else { return false };
        let silent = t >= r.last_host_t + 1_000_000_000;
        r.last_host_t = t;
        match r.state {
            2 => true,
            1 => {
                let mut line = None;
                if b == b'\r' { let l = r.line.trim().to_string(); r.line.clear(); if !l.is_empty() { line = Some(l); } }
                else if r.line.len() < 128 { r.line.push(b as char); }
                self.say(i, &(b as char).to_string());
                if let Some(l) = line { self.execute(i, &l, t); }
                true
            }
            _ => {
                if b == b'+' && (r.pluses > 0 || silent) && r.pluses < 3 { r.pluses += 1; r.plus_t = t; true }
                else {
                    let held = r.pluses; r.pluses = 0;
                    let m = &mut self.modems[i];
                    for _ in 0..held { m.tx.push_back(b'+'); }
                    false
                }
            }
        }
    }
    fn modem_tick(&mut self, t: u64) {
        for i in 0..self.modems.len() {
            let mut ok = false;
            if let Some(r) = self.modems[i].cmd.as_mut() {
                if r.state == 2 && t >= r.reboot_until { r.state = 0; for v in r.regs.values_mut() { v.1 = v.2; v.3 = v.2; } }
                if r.pluses == 3 && t >= r.plus_t + 1_000_000_000 && r.last_host_t <= r.plus_t { r.pluses = 0; r.state = 1; ok = true; }
            }
            if ok { self.say(i, "OK\r\n"); }
        }
    }
    fn reg(&self, i: usize, name: &str) -> Option<i64> { self.modems[i].cmd.as_ref().and_then(|r| r.active(name)) }
    pub fn new(link: &str, p: RadioProfile, names: &[String], seed: u64) -> Self {
        let retries = env::var("FIRMWARE_SIM_RFD_GLOBAL_RETRIES").ok().and_then(|v| v.parse().ok()).unwrap_or(0);
        Self {
            link: link.into(),
            modems: names
                .iter()
                .enumerate()
                .map(|(i, n)| Modem { name: n.clone(), cmd: modem_spec(i), tx: VecDeque::new(), tx_last_byte: 0, rx: VecDeque::new(), rx_free_at: 0, stats: [0; 5], host_staged: VecDeque::new(), host_free_at: 0 })
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
        let byte_ns0 = 10_000_000_000 / self.p.uart_baud as u64;
        for (k, &b) in bytes.iter().enumerate() {
            if self.modem_input(i, b, t + k as u64 * byte_ns0) { continue; }
            let m = &mut self.modems[i];
            if m.tx.len() < self.p.tx_fifo_bytes {
                m.tx.push_back(b);
            } else {
                m.stats[1] += 1;
            }
        }
        let m = &mut self.modems[i];
        m.stats[0] += bytes.len() as u64;
        let byte_ns = 10_000_000_000 / self.p.uart_baud as u64;
        m.tx_last_byte = t + bytes.len() as u64 * byte_ns;
    }
    /// Bytes a host station wrote to its PTY at virtual time `t`, staged for its UART (see `Modem::host_staged`).
    pub fn host_write(&mut self, i: usize, t: u64, bytes: &[u8]) {
        let m = &mut self.modems[i];
        if m.host_staged.is_empty() {
            m.host_free_at = m.host_free_at.max(t);
        }
        m.host_staged.extend(bytes);
    }
    /// Bytes a host station wrote that its UART has not sent yet.
    pub fn host_staged(&self, i: usize) -> usize {
        self.modems[i].host_staged.len()
    }
    /// Staged host bytes whose UART time has come by `t` go into the modem, back to back at the baud.
    fn host_feed(&mut self, t: u64) {
        let byte_ns = 10_000_000_000 / self.p.uart_baud as u64;
        for i in 0..self.modems.len() {
            let m = &self.modems[i];
            if m.host_staged.is_empty() || m.host_free_at + byte_ns > t {
                continue;
            }
            let start = m.host_free_at;
            let n = (((t - start) / byte_ns) as usize).min(m.host_staged.len());
            let chunk: Vec<u8> = self.modems[i].host_staged.drain(..n).collect();
            self.modems[i].host_free_at = start + n as u64 * byte_ns;
            self.uart_in(i, start, &chunk);
        }
    }
    /// Advance the air to `t`: send closed packets, deliver arrivals into RX FIFOs.
    pub fn step(&mut self, t: u64) {
        self.host_feed(t);
        self.modem_tick(t);
        let idle_default = self.p.packet_idle_us * 1000;
        loop {
            let mut sent = false;
            for i in 0..self.modems.len() {
                if self.modems[i].cmd.as_ref().is_some_and(|r| r.state != 0) { continue; }
                let idle_ns = self.reg(i, "SER_BRK_DETMS").map(|v| v.max(1) as u64 * 1_000_000).unwrap_or(idle_default);
                let max_burst = self.reg(i, "MAX_DATA").map(|v| (v.max(1) as usize).min(self.p.max_burst_bytes.max(v as usize))).unwrap_or(self.p.max_burst_bytes);
                let air = self.reg(i, "AIR_SPEED").filter(|v| (4..=1000).contains(v)).map(|v| v as u64 * 1000).unwrap_or(self.p.air_bits_per_second as u64);
                let retries = self.reg(i, "GLOBAL_RETRIES").map(|v| v.max(0) as u64).unwrap_or(self.retries as u64);
                let ready = {
                    let m = &self.modems[i];
                    !m.tx.is_empty() && (m.tx.len() >= max_burst || t >= m.tx_last_byte + idle_ns)
                };
                if !ready {
                    continue;
                }
                let start = self.air_free_at.max(t);
                if start > t {
                    continue;
                }
                let n = self.modems[i].tx.len().min(max_burst);
                let bytes: Vec<u8> = self.modems[i].tx.drain(..n).collect();
                let copy_ns = ((bytes.len() as u64 * 8 + self.p.overhead_bits as u64) * 1_000_000_000) / air;
                let copies = 1 + retries;
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
        let byte_ns = 10_000_000_000 / self.p.uart_baud as u64;
        for m in &self.modems {
            if !m.host_staged.is_empty() {
                t = t.min(m.host_free_at + byte_ns);
            }
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

#[cfg(test)]
mod tests {
    use super::*;

    fn medium() -> Medium {
        let p = RadioProfile { air_bits_per_second: 64000, uart_baud: 115200, tx_fifo_bytes: 8192, rx_fifo_bytes: 8192,
            max_burst_bytes: 256, packet_idle_us: 2000, overhead_bits: 160, turn_us: 2000 };
        Medium::new("rocket_radio", p, &["groundstation".to_string(), "rf".to_string()], 1)
    }

    /// A station's burst larger than the modem FIFO is taken off its PTY at once and reaches the modem at the UART
    /// baud, so nothing overflows the TX FIFO and the far end receives every byte.
    #[test]
    fn host_burst_enters_the_modem_at_the_uart_baud() {
        let mut m = medium();
        let byte_ns = 10_000_000_000 / 115_200u64;
        let burst: Vec<u8> = (0..20_000u32).map(|i| i as u8).collect();
        m.host_write(0, 0, &burst);
        assert_eq!(m.host_staged(0), burst.len());
        // after 10 ms of UART time only about 115 bytes may have left the station
        m.step(10_000_000);
        let fed = burst.len() - m.host_staged(0);
        assert_eq!(fed as u64, 10_000_000 / byte_ns);
        // run until every byte crossed the air
        let mut t = 10_000_000;
        let mut got = Vec::new();
        while got.len() < burst.len() && t < 30_000_000_000 {
            t = m.next_event().max(t + 1).min(t + 5_000_000);
            m.step(t);
            while let Some((_, bytes)) = m.uart_out(1, t, t + 1_000_000) {
                got.extend(bytes);
            }
        }
        assert_eq!(m.modems[0].stats[1], 0, "no TX FIFO overflow");
        assert_eq!(got, burst);
        // the UART time of the whole burst at least (it cannot arrive faster than the station's UART sends it)
        assert!(t >= burst.len() as u64 * byte_ns);
    }

    /// Idle UART time before a write is not credited: a write after a quiet second still starts at its own time.
    #[test]
    fn staged_bytes_start_at_their_write_time() {
        let mut m = medium();
        m.step(1_000_000_000);
        m.host_write(0, 1_000_000_000, &[1, 2, 3, 4]);
        m.step(1_000_000_000);
        assert_eq!(m.host_staged(0), 4);
        assert!(m.next_event() > 1_000_000_000);
        m.step(1_000_000_000 + 4 * (10_000_000_000 / 115_200));
        assert_eq!(m.host_staged(0), 0);
    }
}
