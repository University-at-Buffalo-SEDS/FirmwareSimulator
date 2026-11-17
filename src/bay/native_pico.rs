//! Event-driven Pico-Fi Ethernet peer for the native bay. Same wire contract as the Renode bay's modeled Pico
//! (picofi_tcp_bridge.rs: PCFIND01 discovery on loopback, PICOFI1 handshake, B5 4E <len> TCP frames <-> A5 5A
//! <len> gateway UART frames, 128-frame/16 KiB mailboxes each way), but stepped by the coordinator instead of a
//! 1 ms wall-clock poll loop: the bay runs faster than real time, so a wall-paced Pico would become the bottleneck
//! and add wall latency multiplied by the pace. Pico timers (query cadence, settle) run on virtual time.
use super::*;
use std::net::{Ipv4Addr, SocketAddr, TcpStream, UdpSocket};

#[path = "../picofi_discovery_wire.rs"]
mod discovery_wire;
use discovery_wire::{hardware_mac, Kind, Message, Party, SIZE};

const MAILBOX_DEPTH: usize = 128;
const MAILBOX_BYTES: usize = 16_384;
const MAX_FRAME: usize = 4_096 + 4;

pub struct NativePico {
    port: u16,
    udp: UdpSocket,
    identity: [u8; 6],
    nonce: u64,
    next_query_ns: u64,
    settle_until_ns: Option<u64>,
    query: Option<Message>,
    best: Option<([u8; 6], SocketAddr)>,
    tcp: Option<TcpStream>,
    hello_out: usize,
    hello_in: Vec<u8>,
    connected: bool,
    tcp_in: Vec<u8>,
    tcp_out: Vec<u8>,
    tcp_out_frames: VecDeque<usize>,
    uart_rx: Vec<u8>,
    /// station -> gateway frames waiting for the UART (mailbox)
    pub to_gateway: VecDeque<Vec<u8>>,
    to_gateway_bytes: usize,
    pub stats: [u64; 6], // frames up, frames down, upstream refused, downstream deferred, sessions, framing errors
}

impl NativePico {
    pub fn new(port: u16) -> Result<Self> {
        let udp = UdpSocket::bind((Ipv4Addr::LOCALHOST, 0))?;
        udp.set_nonblocking(true)?;
        Ok(Self {
            port,
            identity: hardware_mac(&port.to_le_bytes()),
            udp,
            nonce: 1,
            next_query_ns: 0,
            settle_until_ns: None,
            query: None,
            best: None,
            tcp: None,
            hello_out: 0,
            hello_in: Vec::new(),
            connected: false,
            tcp_in: Vec::new(),
            tcp_out: Vec::new(),
            tcp_out_frames: VecDeque::new(),
            uart_rx: Vec::new(),
            to_gateway: VecDeque::new(),
            to_gateway_bytes: 0,
            stats: [0; 6],
        })
    }

    fn drop_session(&mut self, why: &str) {
        if self.tcp.is_some() {
            eprintln!("[native-pico] session closed: {why}; rediscovering");
        }
        self.tcp = None;
        self.connected = false;
        self.hello_out = 0;
        self.hello_in.clear();
        self.tcp_in.clear();
        self.tcp_out.clear();
        self.tcp_out_frames.clear();
        self.query = None;
        self.best = None;
        self.settle_until_ns = None;
    }

    /// Gateway UART bytes (gateway TX).
    pub fn from_gateway(&mut self, bytes: &[u8]) {
        self.uart_rx.extend_from_slice(bytes);
        loop {
            let Some(sync) = self.uart_rx.windows(2).position(|b| matches!(b, [0xA5, 0x5A] | [0x5A, 0xA5] | [0xA6, 0x5B] | [0x5B, 0xA6] | [0xA7, 0x7A])) else {
                let keep = self.uart_rx.last().copied().filter(|b| matches!(b, 0xA5 | 0x5A | 0xA6 | 0x5B | 0xA7));
                self.uart_rx.clear();
                if let Some(b) = keep {
                    self.uart_rx.push(b);
                }
                break;
            };
            self.uart_rx.drain(..sync);
            if self.uart_rx.len() < 4 {
                break;
            }
            let len = 4 + u16::from_le_bytes([self.uart_rx[2], self.uart_rx[3]]) as usize;
            if len > MAX_FRAME {
                self.stats[5] += 1;
                self.uart_rx.drain(..4);
                continue;
            }
            if self.uart_rx.len() < len {
                break;
            }
            let frame: Vec<u8> = self.uart_rx.drain(..len).collect();
            if !matches!(&frame[..2], [0xA5, 0x5A] | [0x5A, 0xA5]) {
                continue;
            }
            // Upstream mailbox (Ethernet cost = wire length - 3), only while a station is connected.
            if !self.connected {
                continue;
            }
            let queued: usize = self.tcp_out.len();
            if self.tcp_out_frames.len() >= MAILBOX_DEPTH || queued + frame.len() > MAILBOX_BYTES {
                self.stats[2] += 1;
                continue;
            }
            self.tcp_out.extend_from_slice(&[0xB5, 0x4E]);
            self.tcp_out.extend_from_slice(&frame[2..]);
            self.tcp_out_frames.push_back(frame.len());
            self.stats[0] += 1;
        }
    }

    /// Next frame for the gateway UART, if the mailbox holds one.
    pub fn pop_to_gateway(&mut self) -> Option<Vec<u8>> {
        let f = self.to_gateway.pop_front()?;
        self.to_gateway_bytes -= f.len().saturating_sub(3);
        Some(f)
    }

    /// Network side: discovery, connect, handshake, move bytes. t: virtual ns.
    pub fn service(&mut self, t: u64) {
        if self.tcp.is_none() {
            if t >= self.next_query_ns {
                let q = Message { kind: Kind::Query, party: Party::Pico, port: 0, nonce: self.nonce, identity: self.identity };
                self.nonce = self.nonce.wrapping_add(1).max(1);
                let _ = self.udp.send_to(&q.encode(), (Ipv4Addr::LOCALHOST, self.port));
                self.query = Some(q);
                self.best = None;
                self.settle_until_ns = Some(t + 400_000_000);
                self.next_query_ns = t + 1_000_000_000;
            }
            let mut buf = [0u8; SIZE + 1];
            while let Ok((n, src)) = self.udp.recv_from(&mut buf) {
                if let (Some(m), Some(q)) = (Message::decode(&buf[..n]), self.query) {
                    if src.ip().is_loopback() && m.compatible_offer(q) && self.best.as_ref().is_none_or(|(id, _)| m.identity < *id) {
                        self.best = Some((m.identity, SocketAddr::new(src.ip(), m.port)));
                    }
                }
            }
            if self.settle_until_ns.is_some_and(|u| t >= u) {
                self.settle_until_ns = None;
                if let Some((_, ep)) = self.best.take() {
                    if let Ok(s) = TcpStream::connect_timeout(&ep, Duration::from_millis(250)) {
                        let _ = s.set_nonblocking(true);
                        let _ = s.set_nodelay(true);
                        self.tcp = Some(s);
                        self.hello_out = 0;
                        self.stats[4] += 1;
                    }
                }
            }
            return;
        }
        let mut close: Option<&str> = None;
        {
            let s = self.tcp.as_mut().unwrap();
            if self.hello_out < 7 {
                match s.write(&b"PICOFI1"[self.hello_out..]) {
                    Ok(n) => self.hello_out += n,
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {}
                    Err(_) => close = Some("handshake write"),
                }
            }
            let mut buf = [0u8; 65536];
            loop {
                if close.is_some() {
                    break;
                }
                // Downstream mailbox backpressure: stop reading TCP while the gateway mailbox is full.
                if self.connected && (self.to_gateway.len() >= MAILBOX_DEPTH || self.to_gateway_bytes + 4100 > MAILBOX_BYTES) {
                    self.stats[3] += 1;
                    break;
                }
                match s.read(&mut buf) {
                    Ok(0) => close = Some("peer closed"),
                    Ok(n) => {
                        if self.connected {
                            self.tcp_in.extend_from_slice(&buf[..n]);
                        } else {
                            self.hello_in.extend_from_slice(&buf[..n]);
                        }
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => break,
                    Err(_) => close = Some("read error"),
                }
                if !self.connected && self.hello_in.len() >= 7 {
                    if &self.hello_in[..7] != b"PICOFI1" {
                        close = Some("bad handshake");
                        break;
                    }
                    self.connected = true;
                    let rest = self.hello_in.split_off(7);
                    self.tcp_in.extend_from_slice(&rest);
                }
                // Parse whole frames into the gateway mailbox.
                while self.tcp_in.len() >= 4 {
                    if self.tcp_in[..2] != [0xB5, 0x4E] {
                        close = Some("bad sync");
                        break;
                    }
                    let len = u16::from_le_bytes([self.tcp_in[2], self.tcp_in[3]]) as usize;
                    if !(1..=4096).contains(&len) {
                        close = Some("bad length");
                        break;
                    }
                    if self.tcp_in.len() < 4 + len {
                        break;
                    }
                    let mut f = vec![0xA5, 0x5A];
                    f.extend_from_slice(&self.tcp_in[2..4 + len]);
                    self.tcp_in.drain(..4 + len);
                    self.to_gateway_bytes += f.len().saturating_sub(3);
                    self.to_gateway.push_back(f);
                    self.stats[1] += 1;
                }
            }
            if close.is_none() && self.connected && !self.tcp_out.is_empty() {
                match s.write(&self.tcp_out) {
                    Ok(n) => {
                        self.tcp_out.drain(..n);
                        let mut gone = n;
                        while let Some(&f) = self.tcp_out_frames.front() {
                            let wire = f;
                            if gone >= wire {
                                gone -= wire;
                                self.tcp_out_frames.pop_front();
                            } else {
                                break;
                            }
                        }
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {}
                    Err(_) => close = Some("write error"),
                }
            }
        }
        if let Some(why) = close {
            self.drop_session(why);
        }
    }
}
