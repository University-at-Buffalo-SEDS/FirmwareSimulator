//! Bounded Pico-Fi Ethernet peer for linked firmware qualification.
//! Uses PICOFI1 and B5/4E packets, independently of the I2C mailbox model.
use super::*;
use std::net::{TcpListener, TcpStream};
use std::time::Instant;

struct Session {
    socket: TcpStream,
    hello: bool,
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
            incoming: Vec::new(),
            outgoing: Vec::new(),
            offset: 0,
            write_started: Instant::now(),
        })
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
        let can_receive = !self.hello
            || total == 4
            || (state.uart_tx_packets.len() < PICO_PACKET_QUEUE_DEPTH
                && state.uart_tx_bytes.saturating_add(total) <= PICO_PACKET_QUEUE_BYTES);
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
                self.outgoing = b"PICOFI1".to_vec();
                self.write_started = Instant::now();
                self.hello = true;
                self.incoming.clear();
            } else if total > 4 && can_receive {
                let mut frame = vec![0xa5, 0x5a];
                frame.extend_from_slice(&self.incoming[2..]);
                enqueue_gateway_uart_packet(state, frame);
                state.i2c_to_uart_frames += 1;
                self.incoming.clear();
            }
        }
        if self.hello && self.outgoing.is_empty() {
            if let Some(frame) = state.host_packets.pop_front() {
                state.host_packet_bytes -= frame.len();
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
    let mut state = PicoBridgeState::default();
    let mut session = None;
    while !stop() {
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

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn stalled_uart_backpressures_tcp_without_evicting_accepted_packets() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let mut host = TcpStream::connect(listener.local_addr().unwrap()).unwrap();
        host.set_read_timeout(Some(Duration::from_secs(1))).unwrap();
        let (socket, _) = listener.accept().unwrap();
        let mut peer = Session::new(socket).unwrap();
        let mut state = PicoBridgeState::default();
        host.write_all(b"PICOFI1").unwrap();
        peer.service(&mut state).unwrap();
        let mut hello = [0; 7];
        host.read_exact(&mut hello).unwrap();
        for index in 0..PICO_PACKET_QUEUE_DEPTH {
            enqueue_gateway_uart_packet(&mut state, vec![index as u8; 8]);
        }
        let accepted = state.uart_tx_packets.clone();
        host.write_all(&[0xb5, 0x4e, 1, 0, 42]).unwrap();
        for _ in 0..4 {
            peer.service(&mut state).unwrap();
        }
        assert_eq!(state.uart_tx_packets, accepted);
        assert_eq!(peer.incoming, [0xb5, 0x4e, 1, 0]);
        let first = state.uart_tx_packets.pop_front().unwrap();
        state.uart_tx_bytes -= first.len();
        for _ in 0..4 {
            peer.service(&mut state).unwrap();
        }
        assert_eq!(state.uart_tx_packets.len(), PICO_PACKET_QUEUE_DEPTH);
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
            let mut state = PicoBridgeState::default();
            enqueue_host_packet(&mut state, vec![0xa5, 0x5a, 3, 0, 11, 12, 13]);
            while peer.service(&mut state).is_ok() {
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
