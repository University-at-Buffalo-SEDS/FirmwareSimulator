# Bounded radio-medium contracts

`RadioMedium.csproj` compiles the production `SedsRadioMedium.cs` against narrow
Renode API stubs. It checks host/guest directionality, no local echo or recursive
retransmission, raw-byte burst arbitration, directed disconnect/recovery,
8N1 serialization, finite FIFO ownership including the active air burst,
observable receive overflow, saturated aggregate airtime and sender fairness.
It contains no SDT decoder, so it cannot accidentally grant application frames
atomic delivery across radio bursts.

The declared idle-cutoff envelope seals each sender's serialized UART packet
independently of airtime arbitration. A later write after an idle gap cannot
reopen an earlier packet while another sender owns the air. The two-sender
regression checks both 10 ms and 50 ms cutoffs, ordered delivery and retained
FIFO ownership. Packet descriptors contain lengths/times only and are bounded
by accepted FIFO bytes; payload bytes are not copied into another queue.
This is a contract of this emulator envelope, not evidence that the active
RFD firmware uses idle-delimited packetization. Hardware configuration and
proprietary framing remain unqualified.


Delayed target-time-domain delivery is explicitly deferred by the .NET fixture.
The detach cases cover replacement UARTs at the same index, reattaching the same
UART, moving a UART to a different index, captured input delegates, pending RX
UART bytes, and sender/receiver removal during an air burst. Each attachment owns
its callbacks by Node identity. An old callback cannot enqueue into a replacement
index or deliver into a reattached UART. The delivered counter and RX trace count
only callbacks that actually reach the currently attached target. Newly attached
receivers do not inherit a burst that began before their attachment.

`detach-reattach.resc` verifies in-flight sender/receiver detach and same-UART
reattach against actual Renode, followed by a fresh successful delivery. Require
all three `RADIO_RENODE_*_REATTACH_*` markers. The .NET deferral fixture covers the
scheduler boundary deterministically; the real Renode fixture does not claim to
force an arbitrary delayed cross-machine callback ordering. These are peripheral
contracts, not RF application latency or reliability qualification.

The actual Renode 1.16.1 smoke uses two host proxies connected to independent
PTY backends plus the real STM32G491 UART model. It checks named attachment,
host-to-UART delivery, UART-to-both-host delivery and raw-burst interleaving. Its
CPU is halted; the UART therefore retains only its real eight-byte FIFO, while
the two host proxies independently verify the complete interleaved byte streams.
This is a peripheral contract, not execution of the RF application firmware.

The smoke expects the source checkout mounted at `/src` and the existing
simulator image's platform assets at `/opt/firmware-sim`. Run its `smoke.resc`
with Renode's console `include` command and require all three
`RADIO_RENODE_*_PASS` markers; Renode can return success after a script error,
so the exit code alone is insufficient.

The host proxy has an inert `0x100`-byte MMIO anchor solely to register it in the
machine tree and supply its terminal backend's virtual time domain. Firmware
never accesses this anchor. It does not represent physical hardware. Host
`WriteChar` raises only `HostInput`; medium output raises only `CharReceived`.
The hub calls the target machine's `HandleTimeDomainEvent` outside its state
lock. Resetting a station does not disconnect or power-cycle the shared modem
medium. Physical detach is explicit through `DetachFrom` or directed `SetLink`.

The model intentionally approximates contention using deterministic fair burst
turns. FIFO sizes, burst length, idle cutoff, overhead and turnaround time are
explicit scenario inputs, not asserted RFD900x firmware constants. It does not
model proprietary MAC backoff, FEC, RF propagation, RTS/CTS or vendor framing.

`cross-machine.resc` repeats the real UART test with two distinct STM32G491
machines, a 100 µs global quantum and global serial execution disabled. It
resets the timer-owning machine during an active host burst, observes the UART
control register reset, checks preserved radio queue data, then verifies a new
peer byte reaches the reset owner and host. Require its four
`RADIO_RENODE_*` markers, including `RADIO_RENODE_OWNER_UART_RESET_OBSERVED`.
The fixture's missing optional reset-macro warning does not indicate a skipped
peripheral reset; the control-register assertion establishes that separately.

## Optional raw UART trace

Set `FIRMWARE_SIM_RADIO_TRACE_DIR` to a new writable capture directory and
optionally `FIRMWARE_SIM_RADIO_TRACE_UNTIL_MS` (default `8000`, range
`1..3600000`). With no directory variable the model creates no trace objects or
files. The capture window uses absolute emulator time from startup. I/O errors
are fatal diagnostic failures, rather than silently dropping capture bytes.

Each station has `<medium>-station-<index>-tx.bin` and `-rx.bin`. TX captures
all offered UART input before finite modem FIFO admission, including bytes
subsequently refused by that FIFO; RX captures actual target-machine event
delivery immediately before `UART.WriteChar` or host `EmitReceived`. Comparing
TX and RX therefore distinguishes bytes the application sent from modem drops.
It does not parse or grant atomic delivery to any application protocol.

The 32-byte header is little endian:

| Offset | Field |
| --- | --- |
| 0 | Eight ASCII bytes `SEDSRBT1` |
| 8 | `u32` version (`1`) |
| 12 | `u32` record width (`9`) |
| 16 | `u32` station index |
| 20 | `u32` direction (`0` TX, `1` RX) |
| 24 | `u64` capture cutoff in virtual microseconds |

Each subsequent record is `u64` virtual microseconds followed by one raw byte.
Each stream, including its header, is capped at 4 MiB. The mandatory
`.bin.status.json` sidecar declares the cap, exact record count, last timestamp,
cutoff and completion status. A cap is flagged immediately and the binary file
stops growing; it cannot silently masquerade as a complete stream. Status is
flushed periodically and finalized after the virtual capture window. A process
killed early can leave an unfinished capture, which must be treated as partial.

Use `python3 scripts/inspect-radio-trace.py <file>` for strict reconciliation.
`--raw-output <file>` extracts the raw bytes for a separate wire decoder. The
explicit `--allow-incomplete`/`--allow-capped` flags are for diagnostic inspection
only and do not qualify packet preservation. The optional
`--max-clock-backstep-us` (default zero, maximum 100) permits a bounded
Renode quantum timestamp backstep while preserving raw byte order and times;
the report always states the count, maximum and non-monotonic status. It never
sorts or clamps evidence. Only the offline SDK replay clock uses a running
maximum for decoder maintenance. Five Python reader tests cover refusal
of incomplete/capped/truncated/reversed/out-of-window/mismatched captures.


`decode-radio-trace.py` invokes the actual SEDSnet test-only side codec to
reconstruct full, compact and chunked packets. It does not add a production
firmware API. `analyze-radio-rounds.py` then matches exact packet IDs and
receipt-owner identities, separating sender-to-radio delay, radio receipt
processing and return delay. Four correlation tests ensure retries and
forwarded child ACKs cannot masquerade as the required radio receipt.

The trace driver compiles and executes the selected GroundStation source's
actual `rrls::encode`, `decode` and CRC functions to produce six valid/corrupt
RRv1 fixtures. The test-only SDK diagnostic verifies its envelope handling
against these before decoding kinds 1, 2 and 5, and records the source-contract
hash. Physical reception of host-only kind 2 by RF is not eligible SDK ingress:
`recovery_mesh_uplink` accepts raw data or kind 5. The round analyzer preserves
all physical arrivals separately and applies that explicit distinction.

The ignored SDK capture entry participates in `build.py test --full`: when
both capture environment arguments are absent it creates a small temporary
UART trace using the actual SDK transmitter, runs the same manifest/decoder
pipeline, verifies two reconstructed packets, and removes its fixtures. One
missing argument or any malformed explicit capture fails; it never silently
skips requested input.

## Packetization latency regression

`tests/test_radio_packetization_latency.py` compiles the production GS UART
capacity module and feeds its timestamps to this actual model. The raw probes
are 42 bytes, matching small GS writes in the Pi capture. At 64 kbit/s, the older
256-byte / 2 ms packetization profile concealed a 5.11-second stall exposed by
the 3,968-byte / 50 ms idle envelope. The test enforces the same 500 ms deadline,
complete byte order and zero loss in both profiles. Disabling only the new host
packet boundary must still fail at 5.11 seconds. It also runs the existing modem
contracts unchanged.

Set `CSHARP_TEST_DOTNET_IMAGE` to a local .NET 8 SDK image, optionally set `GS_REPO`
if the GS checkout is not a sibling, and run:

```
python3 -m unittest discover -s tests -p test_radio_packetization_latency.py
```

Also set `FIRMWARE_SIM_RENODE_TEST_IMAGE` to run the same production schedule on
Renode's real clock with named host UARTs. That test enforces the deadline during
the stream, then checks every delivered byte; console exit status alone is not
accepted as a pass. The CPU is halted, so this is a modem/host-pacing regression,
not a complete application-firmware run.

`examples/radio/async-64k-50ms-envelope.json` makes this configuration envelope
available to the full linked-firmware controller. Its FIFO sizes and idle behavior
are explicit test assumptions, not verified register values from the active
three-modem installation. Vendor retries, RF propagation and collision behavior
remain outside this deterministic model. Keep the original reliable-command and
receipt gates when using this profile; do not increase deadlines to accept it.


The captured-load congestion envelope runs with
`CSHARP_TEST_DOTNET_IMAGE=mcr.microsoft.com/dotnet/sdk:8.0 python3 -m unittest discover -s tests -p test_radio_congestion_envelope.py`.
It keeps RF telemetry at approximately 1093 bytes/s, ordinary GS traffic at
281 bytes/s, and one 24-byte RRLS report per second. Four logical commands
retain their 250 ms retry cadence and 18-attempt ceiling. All scenarios use
identical offered source rates, 8192-byte FIFOs, 3968-byte maximum packets,
and 50 ms packetization idle time.

The scenario sweeps effective air service capacity from 64 kbit/s downward.
This is a sensitivity envelope for contention/retransmission overhead, not
an implementation of RFD ASYNC's proprietary MAC or proof of the number of
physical retransmissions. The normal control requires every first receipt
within 500 ms and no FIFO or record-checksum failures. The overloaded negative
control must reproduce seconds of ACK buffering, redundant command traffic,
FIFO overflow, and resulting stream-framing loss without injected bit flips.
Checksums here protect opaque test records; they are not SEDSnet SDT CRCs.
This fixture identifies a failure mechanism and does not qualify hardware.
