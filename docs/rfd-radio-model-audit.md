# RFD900x2 radio model audit

`renode/peripherals/SedsRadioMedium.cs` models the RFD900x2 modems running `RFD ASYNC 4.03 on RFD900X2`. Since
2026-11-10 its timing is calibrated to bench measurements of the real modem pair, not to assumptions. This page
lists every parameter with the source it comes from, the calibration table, the remaining mismatches, and what is
still unmeasured.

RFDesign publishes neither a manual nor release notes for 4.03. The documents below are the closest official ones;
where a behaviour comes from them and not from 4.03 or the bench, the entry says so.

## Sources

| Key | Source |
| --- | --- |
| [BENCH] | Bench pair 2026-11-10: GS Pi and RRLS Mac RFD900x2 ASYNC 4.03, services stopped, GroundStation26 `scripts/rfd_link_test.py` (GS 84a8c13; method in GS `docs/backend/rfd-modem-check.md`, "S5=0"). Measured by the Mac agent. Rows are listed in the calibration table. |
| [M] | RFD900x Asynchronous firmware User Manual V1.1, 2018-07-09, https://files.rfdesign.com.au/Files/documents/RFD900x%20Asynchronous%20User%20Manual%20V1.1.pdf |
| [RN] | Asynchronous release notes 2.43-2.69 (V1 hardware), https://files.rfdesign.com.au/Files/firmware/RFD%20X%20modems%20Asynchronous%20release%20notes.txt |
| [DS] | RFD X Series datasheet V2.1, https://files.rfdesign.com.au/Files/documents/XseriesDatasheet2.1.pdf |
| [P2P] | Point-to-Point/SiK manual V1.5, https://files.rfdesign.com.au/Files/documents/RFD900x%20Peer-to-peer%20V3.X%20User%20Manual%20V1.5.pdf |
| [MP] | Multipoint manual V1.1, https://files.rfdesign.com.au/Files/documents/RFD900x%20Multipoint%20User%20Manual%20V1.1.pdf |
| [SIK] | Open SiK source (ArduPilot/SiK `Firmware/radio/tdm.c`, `serial.c`), which async merged from ([RN] 2.60). |
| [RB] | Bench readback 2026-10-28 (RRLS: S2=64, S3=3968, S5=10, S6=50, RTSCTS=0). |
| [ST] | Bench GS UART strace 2026-10-28: GS read 880 B/s, wrote 276 B/s. |

## Calibrated parameters

All values apply to stations with a modelled modem (`FIRMWARE_SIM_RFD_MODEM`, or `ConfigureModem`) under
`FIRMWARE_SIM_RFD_CALIBRATION=bench-20261110`, the default. `=none` restores the uncalibrated mechanics; the
medium's mechanics tests use it. Each value can be overridden by its environment variable.

| Parameter | Value | Source | Notes |
| --- | --- | --- | --- |
| Air rate | S2 AIR_SPEED (64 kbit/s) | [M] p. 6, [RB] | Now read from each modem's own S2. [BENCH]: 125 and 250 lowered RTT by about 25 ms and did not move the collapse. |
| Packetisation | S6 idle gap, S3 MAX_DATA | [M] p. 7, [BENCH] row 4 | Frames written with a gap under S6 coalesce into one air packet ([BENCH]). |
| Air framing | 160 bits per packet | assumption A3 | Not separated from TX setup by any bench row. |
| TX setup (`TX_SETUP_US`, `TX_JITTER_US`) | 4 ms + 0-3 ms | [BENCH] rows 3, 4 | Time from packet close to air. It occupies the sender (one-way 40/s loses 6% from this), but the modem keeps receiving during it. |
| Deafness | on air plus `turn_us` (1 ms) | [BENCH] row 4 | Transmitting pre-empts a reception in progress: both ends transmitting collapse (97-99%). A modem in setup still hears; the bench burst echo (5/s×4, 265 ms) needs that. `SETUP_DEAF=1` makes setup deaf. |
| Listen-before-talk | sampled at packet close, before setup | [BENCH] row 5 | The other station can start during setup, which carrier sense never saw, so LBT does not remove collisions (LBT_RSSI=25 lost more on the bench). This replaces assumption A2. |
| Receive output | streamed while the packet is on air | [BENCH] rows 2, 4 | The receiving modem outputs bytes as they arrive; an overlap truncates the stream, so a corrupt packet arrives cut short and fails the application CRC. |
| Base loss (`AIR_LOSS_PPM`) | 0.5% per packet per receiver, uniform | [BENCH] row 3 | One-way 20/s to a silent receiver lost 0.5%. The "about 5% uniform" figure needs its test conditions (bench request 4). |
| Per-path loss (`SetLinkLoss`) | 0 by default | [BENCH] (GS heard RF at about 7 f/s, RRLS at about 25 f/s) | Extra loss on one directed path, on top of the base loss; schedulable like `SetLink`. |
| Input buffer (`INPUT_BUFFER_BYTES`) | 2048 B, then serial input is dropped (RTSCTS=0) | [BENCH] row 1 (S5=1 10/s RTT capped at 2.6 s) | Bounds the backlog under overload. Undocumented by RFDesign (assumption A6). |
| S5 GLOBAL_RETRIES | acknowledged retries: ACK 60 ms after a copy, resend after 80 ms without one; the receiver outputs after its ACK | provisional; [BENCH] row 1, [RN] 2.45, [ST] | Matches the S5=1 10/s loss but queues 3× too long, and gives no loss at 2/s. Blind copies contradict [ST]. `FIRMWARE_SIM_RFD_BROADCAST_RETRY=ack\|on_loss\|blind`. |
| Command mode | `+++` after 1 s silence, ATZ 1.5 s | [M] p. 5 | Unchanged. |

The host side of `rfd_link_test.py` also shapes the bench numbers, and the calibration harness models it: the
tool reads the modem through USB serial, which hands over 62-byte chunks at once and flushes partial chunks after
a 16 ms latency timer (FTDI default; bench request 3), and its paced writes land up to 4 ms late. Most of the
"about 45 ms fixed per packet" in RTT is the latency timer at each end plus S6.

## Calibration table

`tests/radio-medium/BenchCalibration.cs` replays each `rfd_link_test.py` pattern (frame = pad + 21 B, burst writes,
20 s plus 5 s drain, the tool's percentile method) on two modem stations and compares with [BENCH]. It runs in
every `tests/radio-medium` run; a row not marked as a known residual must pass. `RADIO_CALIBRATION=1` prints the
table alone.

| Row | Bench | Tolerance | Sim (calibrated) | Status |
| --- | --- | --- | --- | --- |
| S5=0 2/s, 120 B, ping/echo | 2.5% loss, p50 120 ms, max 133 | ±3 pp, ±20%, max ×2 | 0%, 125, 126 | pass |
| S5=0 10/s | 5.5%, 120, 137 | ±4 pp, ±20% | 3.5%, 124, 127 | pass |
| S5=0 1/s, 120 B | 120 ms | ±10% | 125 | pass |
| S5=0 1/s, 32 B | 94 ms | ±10% | 79 | residual R1 |
| one-way 20/s, 145 B, silent receiver | 0.5% | ±2 pp | 1.8% | pass |
| one-way 40/s, 145 B | 6% | ±4 pp | 7.1% | pass |
| both ends 20/s × 1 frame | 97-99% | ±6 pp | 99.8% | pass |
| both ends 10/s × 4 frames | 97-99% | ±6 pp | 99.8% | pass |
| both ends 5/s × 8 frames | 97-99% | ±6 pp | 87.6% | residual R2 |
| both ends 5/s × 4 frames (coalesced) | 5-9%, p50 265 ms | ±5 pp, ±25% | 1.8%, 214 | residual R3 |
| 5/s × 4 at S6=20 | 28% (worse than S6=10) | ±10 pp | 46% | residual R3 |
| S5=1 2/s | 5%, 262, max 1427 | ±4 pp, ±20%, max ×2 | 0%, 288, 292 | residual R4 |
| S5=1 10/s | 49%, 1044, max 2614 | ±12 pp, ±35%, max ×2 | 53%, 3587, 3689 | residual R4 |

The uncalibrated medium (FirmwareSimulator ec4a76a) failed eight of these rows: S5=1 lost nothing and did not
queue, 32 B RTT was 57 ms, one-way 40/s lost nothing, and the coalesced 5/s×4 burst lost everything. Its passing
collapse rows were an artefact of deterministic phase-locking, because it had no timing jitter.

### Known residuals

- **R1. RTT slope.** The sim adds 46 ms of RTT from 32 B to 120 B; the bench adds 26 ms. Serial input (87 µs/B)
  plus air (125 µs/B) already exceeds the bench slope per hop, so the real sending modem must overlap serial
  input with air in some way the documented S6 packetisation does not describe. Unexplained; not modelled.
- **R2. 5/s × 8 collapse** stops at 88% instead of 97%+. The cause is not yet identified.
- **R3. Coalesced burst echo.** Right regime (the burst survives at S6=10, and S6=20 is worse), but the loss is
  off in both directions. It is a timing knife-edge between the echo's coalesced reply and the next burst.
- **R4. S5 ≥ 1.** The mechanism is provisional (bench request 2 separates acknowledged retries from blind copies).
  Conclusions drawn from S5=1 runs are provisional until it is settled.

## Still unmeasured

- **Bench request 1.** Exact command lines (pong size, duration) for the both-ends rows and the S6=20 row.
- **Bench request 2.** S5 mechanism: ping/echo 2/s with S5 asymmetric (pinger S5=1 / echo S5=0, then the reverse),
  and one-way S5=1 10/s to a silent echo, counting duplicates.
- **Bench request 3.** USB-serial latency timer on the Pi and the Mac.
- **Bench request 4.** Conditions of the "about 5% uniform single-frame loss".
- **A3. Air framing** versus TX setup: only their sum is constrained.
- **A4. Capture effect.** A stronger signal may survive an overlap; the bench pair was close together.
- **A5. MAX_WINDOW (S20=131).** Whether it splits large packets.
- **A6. Input buffer.** 2048 B is inferred from the S5=1 backlog, not measured directly: write N bytes back to
  back at 115200 with RTSCTS=0 and count the remote output, for N = 1, 2, 4, 8 kB.
- **DUTY_CYCLE.** Modelled as a strict hold after each packet, not SiK's running average.

## Using it

- **`scripts/test-full-network.py --rfd-profile shipped|drain|field`.** Models every radio station's modem with
  the shipped (S5=0, S6=10), previous drain (S5=1, S6=10) or 2026-10-28 field (S5=10, S6=50) registers and
  selects the async MAC with the bench calibration. Further options:
  - `--radio-path-loss sender:receiver:s0:s1:ppm` (stations gs, rf, rrls) degrades one directed path between
    virtual seconds s0 and s1 (bay `radio_link_events` with `loss_ppm`);
  - `--rfd-broadcast-retry ack|on_loss|blind`;
  - `--rfd-air-loss-ppm N` replaces the 0.5% base loss;
  - `--rfd-carrier-sense register|always|never`.
- **`Snapshot()` per node** reports `global_retries`, `packet_idle_us`, `max_data` and the counters: 4 refused
  serial input bytes, 10 bytes lost on air, 11 copies sent, 12 copy receptions lost to overlap, 13 LBT deferrals.
- **Replay.** `RADIO_REPLAY_TRACE_DIR=<run>/diagnostics/radio-trace RADIO_REPLAY_RF_KEEP_PERCENT=50 dotnet
  RadioMedium.dll` replays a linked run's UART TX traces into a fresh medium and keeps that share of the RF
  board's frames. It estimates per-frame loss, not retry dynamics.
