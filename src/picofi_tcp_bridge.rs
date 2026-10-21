//! Bounded Pico-Fi Ethernet peer for linked firmware qualification.
//! Uses PICOFI1 and B5/4E packets, independently of the I2C mailbox model.
use super::*;
use std::net::{TcpListener, TcpStream};
use std::time::Instant;

struct Session {
    socket: TcpStream,
    hello: bool,
    client: bool,
    incoming: Vec<u8>,
    outgoing: Vec<u8>,
    offset: usize,
    write_started: Instant,
}
impl Session {
    fn new(socket: TcpStream) -> Result<Self> {
        socket.set_nonblocking(true)?;
        socket.set_nodelay(true)?;
        Ok(Self {
            socket,
            hello: false,
            client: false,
            incoming: Vec::new(),
            outgoing: Vec::new(),
            offset: 0,
            write_started: Instant::now(),
        })
    }
    fn client(socket: TcpStream) -> Result<Self> {
        let mut session = Self::new(socket)?;
        session.client = true;
        session.outgoing = b"PICOFI1".to_vec();
        Ok(session)
    }
    fn service(&mut self, state: &mut PicoBridgeState) -> Result<()> {
        ensure!(
            self.hello || self.write_started.elapsed() < Duration::from_millis(500),
            "Pico-Fi TCP handshake timed out"
        );
        let total = if !self.hello {
            7
        } else if self.incoming.len() < 4 {
            4
        } else {
            ensure!(
                self.incoming[..2] == [0xb5, 0x4e],
                "invalid Pico-Fi TCP sync"
            );
            let length = u16::from_le_bytes([self.incoming[2], self.incoming[3]]) as usize;
            ensure!((1..=4096).contains(&length), "invalid Pico-Fi TCP length");
            4 + length
        };
        // TCP can apply stream backpressure. Do not consume a body that
        // cannot fit the bounded UART queue, or evict an accepted command to
        // admit it. Keep serving the opposite direction while reads pause.
        // The production core owns one complete pending receive frame outside
        // the mailbox. Read at most that frame, then retain it under pressure.
        let can_receive = true;
        let mut bytes = [0; 4100];
        let need = total - self.incoming.len();
        if need != 0 && can_receive {
            match self.socket.read(&mut bytes[..need]) {
                Ok(0) => bail!("Pico-Fi TCP peer closed"),
                Ok(count) => self.incoming.extend_from_slice(&bytes[..count]),
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {}
                Err(e) => return Err(e.into()),
            }
        }
        if self.incoming.len() == total {
            if !self.hello {
                ensure!(self.incoming == b"PICOFI1", "invalid Pico-Fi TCP handshake");
                if !self.client {
                    self.outgoing = b"PICOFI1".to_vec();
                }
                self.write_started = Instant::now();
                self.hello = true;
                state.ethernet_connected = true;
                self.incoming.clear();
            } else if total > 4 && state.uart_has_room(total) {
                let mut frame = vec![0xa5, 0x5a];
                frame.extend_from_slice(&self.incoming[2..]);
                ensure!(
                    enqueue_gateway_uart_packet(state, frame),
                    "Pico UART egress full"
                );
                state.i2c_to_uart_frames += 1;
                self.incoming.clear();
            }
        }
        if self.hello && self.outgoing.is_empty() {
            if let Some(frame) = state.host_packets.pop_front() {
                state.host_packet_bytes -= state.packet_cost(frame.len());
                ensure!(
                    frame.len() >= 5 && frame.len() <= 4100,
                    "invalid gateway UART frame"
                );
                self.outgoing = vec![0xb5, 0x4e];
                self.outgoing.extend_from_slice(&frame[2..]);
                self.write_started = Instant::now();
            }
        }
        if !self.outgoing.is_empty() {
            ensure!(
                self.write_started.elapsed() < Duration::from_millis(500),
                "Pico-Fi TCP writer stalled"
            );
            match self.socket.write(&self.outgoing[self.offset..]) {
                Ok(0) => bail!("Pico-Fi TCP writer closed"),
                Ok(count) => {
                    self.offset += count;
                    if self.offset == self.outgoing.len() {
                        self.outgoing.clear();
                        self.offset = 0;
                    }
                }
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {}
                Err(e) => return Err(e.into()),
            }
        }
        Ok(())
    }
}

pub(super) fn run(listener: TcpListener, uart_path: &Path, stop: impl Fn() -> bool) -> Result<()> {
    let mut uart = OpenOptions::new().read(true).write(true).open(uart_path)?;
    configure_raw_nonblocking(uart.as_raw_fd())?;
    listener.set_nonblocking(true)?;
    let mut state = PicoBridgeState::ethernet();
    let mut session = None;
    while !stop() {
        state.ethernet_connected = session.as_ref().is_some_and(|peer: &Session| peer.hello);
        if !state.ethernet_connected {
            state.clear_disconnected_upload();
        }
        read_gateway_uart(&mut state, &mut uart)?;
        drain_gateway_uart_tx(&mut state, &mut uart)?;
        if session.is_none() {
            match listener.accept() {
                Ok((socket, _)) => session = Some(Session::new(socket)?),
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {}
                Err(e) => return Err(e.into()),
            }
        }
        if let Some(peer) = &mut session {
            if let Err(error) = peer.service(&mut state) {
                eprintln!("[SIM] Pico-Fi TCP disconnected: {error}; accepting replacement");
                session = None;
            }
        }
        thread::sleep(Duration::from_millis(1));
    }
    Ok(())
}

#[path = "picofi_discovery_wire.rs"]
mod discovery_wire;

/// Exercise the deployed direction: Pico discovers a GS-selected TCP endpoint.
pub(super) fn run_discovered(port: u16, uart_path: &Path, stop: impl Fn() -> bool) -> Result<()> {
    use discovery_wire::{hardware_mac, Kind, Message, Party, PORT, SIZE};
    ensure!(
        port != 0,
        "Pico discovery UDP port must be nonzero (default {PORT})"
    );
    use std::net::{Ipv4Addr, UdpSocket};
    let mut uart = OpenOptions::new().read(true).write(true).open(uart_path)?;
    configure_raw_nonblocking(uart.as_raw_fd())?;
    let discovery = UdpSocket::bind((Ipv4Addr::LOCALHOST, 0))?;
    discovery.set_nonblocking(true)?;
    let identity = hardware_mac(&discovery.local_addr()?.port().to_le_bytes());
    let mut state = PicoBridgeState::ethernet();
    let mut session = None;
    let mut next_query = Instant::now();
    let mut query = None;
    let mut settle = None;
    let mut best: Option<([u8; 6], std::net::SocketAddr)> = None;
    let mut nonce = 1u64;
    while !stop() {
        state.ethernet_connected = session.as_ref().is_some_and(|peer: &Session| peer.hello);
        if !state.ethernet_connected {
            state.clear_disconnected_upload();
        }
        read_gateway_uart(&mut state, &mut uart)?;
        drain_gateway_uart_tx(&mut state, &mut uart)?;
        let now = Instant::now();
        if session.is_none() {
            if now >= next_query {
                let message = Message {
                    kind: Kind::Query,
                    party: Party::Pico,
                    port: 0,
                    nonce,
                    identity,
                };
                nonce = nonce.wrapping_add(1).max(1);
                discovery.send_to(&message.encode(), (Ipv4Addr::LOCALHOST, port))?;
                query = Some(message);
                best = None;
                settle = Some(now + Duration::from_millis(400));
                next_query = now + Duration::from_secs(1);
            }
            let mut bytes = [0; SIZE + 1];
            for _ in 0..8 {
                match discovery.recv_from(&mut bytes) {
                    Ok((n, source)) => {
                        if let Some(message) = Message::decode(&bytes[..n]) {
                            if source.ip().is_loopback()
                                && settle.is_some_and(|until| now <= until)
                                && query.is_some_and(|q| message.compatible_offer(q))
                                && best.as_ref().is_none_or(|(id, _)| message.identity < *id)
                            {
                                best = Some((
                                    message.identity,
                                    std::net::SocketAddr::new(source.ip(), message.port),
                                ));
                            }
                        }
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => break,
                    Err(error) => return Err(error.into()),
                }
            }
            if settle.is_some_and(|until| now >= until) {
                settle = None;
                if let Some((_, endpoint)) = best.take() {
                    if let Ok(socket) =
                        TcpStream::connect_timeout(&endpoint, Duration::from_millis(250))
                    {
                        session = Some(Session::client(socket)?);
                    }
                }
            }
        }
        if let Some(peer) = &mut session {
            if let Err(error) = peer.service(&mut state) {
                eprintln!("[SIM] Discovered Pico-Fi TCP disconnected: {error}; rediscovering");
                session = None;
                query = None;
                best = None;
                settle = None;
                next_query = Instant::now();
            }
        }
        thread::sleep(Duration::from_millis(1));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn discovery_bridge_finds_changed_host_port_and_preserves_duplex_uart_frames() {
        use discovery_wire::{Kind, Message, Party, SIZE};
        use std::{
            ffi::CStr,
            net::{Ipv4Addr, UdpSocket},
            os::fd::FromRawFd,
        };
        let (mut master_fd, mut slave_fd) = (-1, -1);
        let mut name = [0 as libc::c_char; 256];
        assert_eq!(
            unsafe {
                libc::openpty(
                    &mut master_fd,
                    &mut slave_fd,
                    name.as_mut_ptr(),
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                )
            },
            0
        );
        let mut master = unsafe { fs::File::from_raw_fd(master_fd) };
        let _slave = unsafe { fs::File::from_raw_fd(slave_fd) };
        let path = PathBuf::from(unsafe { CStr::from_ptr(name.as_ptr()) }.to_str().unwrap());
        configure_raw_nonblocking(master.as_raw_fd()).unwrap();
        let discovery = UdpSocket::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        discovery
            .set_read_timeout(Some(Duration::from_secs(3)))
            .unwrap();
        let port = discovery.local_addr().unwrap().port();
        let stop = Arc::new(AtomicBool::new(false));
        let stopped = stop.clone();
        let worker =
            thread::spawn(move || run_discovered(port, &path, || stopped.load(Ordering::Acquire)));
        for value in [31, 32] {
            let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
            listener.set_nonblocking(true).unwrap();
            let mut bytes = [0; SIZE];
            let (n, source) = discovery.recv_from(&mut bytes).unwrap();
            let query = Message::decode(&bytes[..n]).unwrap();
            assert_eq!(query.party, Party::Pico);
            let offer = Message {
                kind: Kind::Offer,
                party: Party::GroundStation,
                port: listener.local_addr().unwrap().port(),
                nonce: query.nonce,
                identity: [2, 4, 6, 8, 10, 12],
            };
            discovery.send_to(&offer.encode(), source).unwrap();
            let deadline = Instant::now() + Duration::from_secs(3);
            let mut host = loop {
                match listener.accept() {
                    Ok((socket, _)) => break socket,
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        assert!(Instant::now() < deadline);
                        thread::sleep(Duration::from_millis(1));
                    }
                    Err(e) => panic!("{e}"),
                }
            };
            host.set_nonblocking(false).unwrap();
            host.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
            let mut hello = [0; 7];
            host.read_exact(&mut hello).unwrap();
            assert_eq!(&hello, b"PICOFI1");
            host.write_all(b"PICOFI1").unwrap();
            host.write_all(&[0xb5, 0x4e, 1, 0, value + 1]).unwrap();
            let mut collected = vec![];
            while collected.len() < 5 {
                let mut buffer = [0; 5];
                match master.read(&mut buffer) {
                    Ok(n) => collected.extend_from_slice(&buffer[..n]),
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {}
                    Err(e) => panic!("{e}"),
                };
                assert!(Instant::now() < deadline);
                thread::sleep(Duration::from_millis(1));
            }
            assert_eq!(collected, [0xa5, 0x5a, 1, 0, value + 1]);
            master.write_all(&[0xa5, 0x5a, 1, 0, value]).unwrap();
            let mut frame = [0; 5];
            host.read_exact(&mut frame).unwrap();
            assert_eq!(frame, [0xb5, 0x4e, 1, 0, value]);
            host.shutdown(std::net::Shutdown::Both).unwrap();
        }
        stop.store(true, Ordering::Release);
        worker.join().unwrap().unwrap();
    }
    #[test]
    fn stalled_uart_backpressures_tcp_without_evicting_accepted_packets() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let mut host = TcpStream::connect(listener.local_addr().unwrap()).unwrap();
        host.set_nodelay(true).unwrap();
        host.set_read_timeout(Some(Duration::from_secs(1))).unwrap();
        let (socket, _) = listener.accept().unwrap();
        let mut peer = Session::new(socket).unwrap();
        let mut state = PicoBridgeState::ethernet();
        host.write_all(b"PICOFI1").unwrap();
        let deadline = Instant::now() + Duration::from_secs(1);
        while !peer.hello || !peer.outgoing.is_empty() {
            assert!(Instant::now() < deadline, "handshake did not complete");
            peer.service(&mut state).unwrap();
            thread::sleep(Duration::from_millis(1));
        }
        let mut hello = [0; 7];
        host.read_exact(&mut hello).unwrap();
        for index in 0..PICO_ETHERNET_MAILBOX_DEPTH {
            enqueue_gateway_uart_packet(&mut state, vec![index as u8; 8]);
        }
        let accepted = state.uart_tx_packets.clone();
        host.write_all(&[0xb5, 0x4e, 1, 0, 42]).unwrap();
        while peer.incoming.len() < 5 {
            assert!(Instant::now() < deadline, "header did not arrive");
            peer.service(&mut state).unwrap();
            thread::sleep(Duration::from_millis(1));
        }
        assert_eq!(state.uart_tx_packets, accepted);
        assert_eq!(peer.incoming, [0xb5, 0x4e, 1, 0, 42]);
        let first = state.uart_tx_packets.pop_front().unwrap();
        state.uart_tx_bytes -= state.packet_cost(first.len());
        while !peer.incoming.is_empty() {
            assert!(Instant::now() < deadline, "paused body did not resume");
            peer.service(&mut state).unwrap();
            thread::sleep(Duration::from_millis(1));
        }
        assert_eq!(state.uart_tx_packets.len(), PICO_ETHERNET_MAILBOX_DEPTH);
        assert_eq!(state.uart_tx_packets.front(), accepted.get(1));
        assert_eq!(
            state.uart_tx_packets.back().unwrap(),
            &[0xa5, 0x5a, 1, 0, 42]
        );
        assert!(peer.incoming.is_empty());
    }

    #[test]
    fn ethernet_peer_frames_both_directions_without_mailbox_slots() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let mut host = TcpStream::connect(listener.local_addr().unwrap()).unwrap();
        host.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        let (socket, _) = listener.accept().unwrap();
        let worker = thread::spawn(move || {
            let mut peer = Session::new(socket).unwrap();
            let mut state = PicoBridgeState::ethernet();
            let mut injected = false;
            while peer.service(&mut state).is_ok() {
                if peer.hello && !injected {
                    enqueue_host_packet(&mut state, vec![0xa5, 0x5a, 3, 0, 11, 12, 13]);
                    injected = true;
                }
                thread::sleep(Duration::from_millis(1));
            }
            state
        });
        for byte in b"PICOFI1" {
            host.write_all(&[*byte]).unwrap();
        }
        let mut hello = [0; 7];
        host.read_exact(&mut hello).unwrap();
        assert_eq!(&hello, b"PICOFI1");
        let mut packet = [0; 7];
        host.read_exact(&mut packet).unwrap();
        assert_eq!(packet, [0xb5, 0x4e, 3, 0, 11, 12, 13]);
        for byte in [0xb5, 0x4e, 3, 0, 21, 22, 23] {
            host.write_all(&[byte]).unwrap();
        }
        // A following invalid frame ensures the first packet was processed
        // before the peer exits, rather than racing a close with unread bytes.
        host.write_all(&[0xb5, 0x4e, 1, 0x10]).unwrap();
        let mut state = worker.join().unwrap();
        assert_eq!(
            state.uart_tx_packets.pop_front().unwrap(),
            [0xa5, 0x5a, 3, 0, 21, 22, 23]
        );
        assert_eq!(state.host_packet_bytes, 0);
    }
}
