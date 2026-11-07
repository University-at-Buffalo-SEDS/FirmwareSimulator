//! Real private UDP/TCP/PTY lifecycle checks; no RP2040 or electrical proof.
use super::*;
use discovery_wire::{Kind, Message, Party, SIZE};
use std::{ffi::CStr, net::UdpSocket, os::fd::FromRawFd};

struct Worker {
    stop: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<Result<()>>>,
}
impl Worker {
    fn stop_and_join(mut self) {
        self.stop.store(true, Ordering::Release);
        self.thread
            .take()
            .unwrap()
            .join()
            .expect("Pico worker panicked")
            .expect("Pico worker failed");
    }
}

impl Drop for Worker {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        if let Some(worker) = self.thread.take() {
            let _ = worker.join();
        }
    }
}
fn fixture() -> (fs::File, fs::File, PathBuf) {
    let (mut master, mut slave) = (-1, -1);
    let mut name = [0 as libc::c_char; 256];
    assert_eq!(
        unsafe {
            libc::openpty(
                &mut master,
                &mut slave,
                name.as_mut_ptr(),
                std::ptr::null(),
                std::ptr::null(),
            )
        },
        0
    );
    let master = unsafe { fs::File::from_raw_fd(master) };
    let slave = unsafe { fs::File::from_raw_fd(slave) };
    configure_raw_nonblocking(master.as_raw_fd()).unwrap();
    let path = PathBuf::from(unsafe { CStr::from_ptr(name.as_ptr()) }.to_str().unwrap());
    (master, slave, path)
}
fn accept_discovered(discovery: &UdpSocket, listener: &TcpListener) -> (TcpStream, [u8; 6]) {
    let mut raw = [0; SIZE];
    let (length, source) = discovery.recv_from(&mut raw).unwrap();
    let query = Message::decode(&raw[..length]).unwrap();
    assert_eq!(query.party, Party::Pico);
    let offer = Message {
        kind: Kind::Offer,
        party: Party::GroundStation,
        port: listener.local_addr().unwrap().port(),
        nonce: query.nonce,
        identity: [2, 4, 6, 8, 10, 12],
    };
    discovery.send_to(&offer.encode(), source).unwrap();
    let until = Instant::now() + Duration::from_secs(3);
    let mut host = loop {
        match listener.accept() {
            Ok((peer, _)) => break peer,
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                assert!(Instant::now() < until, "fresh connection missing");
                thread::sleep(Duration::from_millis(1));
            }
            Err(error) => panic!("{error}"),
        }
    };
    host.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
    let mut hello = [0; 7];
    host.read_exact(&mut hello).unwrap();
    assert_eq!(&hello, b"PICOFI1");
    (host, query.identity)
}
fn receive_uart(master: &mut fs::File) -> [u8; 5] {
    let until = Instant::now() + Duration::from_secs(2);
    let mut result = [0; 5];
    let mut n = 0;
    while n < result.len() {
        match master.read(&mut result[n..]) {
            Ok(count) => n += count,
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {}
            Err(error) => panic!("{error}"),
        }
        assert!(Instant::now() < until, "fresh frame did not reach UART");
        thread::sleep(Duration::from_millis(1));
    }
    result
}
#[test]
fn two_cold_model_pico_resets_rejoin_same_retained_host_and_discard_old_socket_prefix() {
    let (mut uart, _slave, path) = fixture();
    let discovery = UdpSocket::bind("127.0.0.1:0").unwrap();
    discovery
        .set_read_timeout(Some(Duration::from_secs(3)))
        .unwrap();
    let port = discovery.local_addr().unwrap().port();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let stop = Arc::new(AtomicBool::new(false));
    let stopped = stop.clone();
    let (requests, rx) = mpsc::sync_channel(16);
    let (done, completed) = mpsc::channel();
    let worker = Worker {
        stop,
        thread: Some(thread::spawn(move || {
            run_discovered_restartable(port, &path, || stopped.load(Ordering::Acquire), rx, done)
        })),
    };
    let mut identity = None;
    for round in 0..3 {
        let (mut host, actual_identity) = accept_discovered(&discovery, &listener);
        if let Some(original) = identity {
            assert_eq!(
                actual_identity, original,
                "same configured Pico identity after reset"
            );
        } else {
            identity = Some(actual_identity);
        }
        host.write_all(b"PICOFI1").unwrap();
        host.write_all(&[0xb5, 0x4e, 1, 0, 40 + round]).unwrap();
        assert_eq!(receive_uart(&mut uart), [0xa5, 0x5a, 1, 0, 40 + round]);
        uart.write_all(&[0xa5, 0x5a, 1, 0, 60 + round]).unwrap();
        let mut fresh = [0; 5];
        host.read_exact(&mut fresh).unwrap();
        assert_eq!(fresh, [0xb5, 0x4e, 1, 0, 60 + round]);
        if round < 2 {
            // A real accepted TCP prefix belongs to this old socket, not the
            // next generation; no claim that all bytes were parsed is made.
            host.write_all(&[0xb5, 0x4e, 3, 0, 90]).unwrap();
            requests.send(round as usize).unwrap();
            assert_eq!(
                completed.recv_timeout(Duration::from_secs(2)).unwrap(),
                round as usize
            );
            let mut byte = [0];
            match host.read(&mut byte) {
                Ok(0) => {}
                Err(error) if error.kind() == std::io::ErrorKind::ConnectionReset => {}
                other => panic!("old TCP session did not close: {other:?}"),
            }
        }
    }
    worker.stop_and_join();
}
#[test]
fn missing_first_greeting_recovers_without_a_second_power_cycle() {
    let (mut uart, _slave, path) = fixture();
    let discovery = UdpSocket::bind("127.0.0.1:0").unwrap();
    discovery
        .set_read_timeout(Some(Duration::from_secs(3)))
        .unwrap();
    let port = discovery.local_addr().unwrap().port();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let stop = Arc::new(AtomicBool::new(false));
    let stopped = stop.clone();
    let (_requests, rx) = mpsc::sync_channel(16);
    let (done, _completed) = mpsc::channel();
    let worker = Worker {
        stop,
        thread: Some(thread::spawn(move || {
            run_discovered_restartable(port, &path, || stopped.load(Ordering::Acquire), rx, done)
        })),
    };
    let (mut first, identity) = accept_discovered(&discovery, &listener);
    let mut one = [0];
    assert_eq!(
        first.read(&mut one).unwrap(),
        0,
        "no HELLO response must close this handshake"
    );
    let (mut second, again) = accept_discovered(&discovery, &listener);
    assert_eq!(again, identity);
    second.write_all(b"PICOFI1").unwrap();
    // Existing model timeout500ms is intentionally not an RP2040 2000ms claim.
    second.write_all(&[0xb5, 0x4e, 1, 0, 77]).unwrap();
    assert_eq!(receive_uart(&mut uart), [0xa5, 0x5a, 1, 0, 77]);
    uart.write_all(&[0xa5, 0x5a, 1, 0, 78]).unwrap();
    let mut reply = [0; 5];
    second.read_exact(&mut reply).unwrap();
    assert_eq!(reply, [0xb5, 0x4e, 1, 0, 78]);
    worker.stop_and_join();
}
