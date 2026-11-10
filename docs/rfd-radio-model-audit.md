# RFD900x2 radio model audit (2026-11-09)

This audit checks `renode/peripherals/SedsRadioMedium.cs` against what RFDesign documents and what the bench
measured. The deployed modems run `RFD ASYNC 4.03 on RFD900X2`. RFDesign publishes neither a manual nor release
notes for 4.03; its firmware index lists only the binary. The documents below are the closest official ones. Where
a value comes from them and not from 4.03 itself, the entry says so.

## Sources

| Key | Document |
| --- | --- |
| [M] | RFD900x Asynchronous firmware User Manual V1.1, 2018-07-09, https://files.rfdesign.com.au/Files/documents/RFD900x%20Asynchronous%20User%20Manual%20V1.1.pdf |
| [RN] | Asynchronous release notes 2.43-2.69 (V1 hardware), https://files.rfdesign.com.au/Files/firmware/RFD%20X%20modems%20Asynchronous%20release%20notes.txt |
| [DS] | RFD X Series datasheet V2.1, https://files.rfdesign.com.au/Files/documents/XseriesDatasheet2.1.pdf |
| [P2P] | Point-to-Point/SiK manual V1.5, https://files.rfdesign.com.au/Files/documents/RFD900x%20Peer-to-peer%20V3.X%20User%20Manual%20V1.5.pdf |
| [SIKRN] | SiK V3.X release notes, https://files.rfdesign.com.au/Files/firmware/RFD%20X%20modems%20SiK%20V3.X%20release%20notes.txt |
| [MP] | Multipoint manual V1.1, https://files.rfdesign.com.au/Files/documents/RFD900x%20Multipoint%20User%20Manual%20V1.1.pdf |
| [SIK] | Open SiK source (ArduPilot/SiK `Firmware/radio/tdm.c`, `serial.c`, `radio.h`). This is the 8051 reference design that async merged from ([RN] 2.60 "Merged changes from SiK V3.09"). |
| [SUP] | RFDesign support (Moreton King), quoted on https://discuss.ardupilot.org/t/rfd900x-asynchronous-mesh-firmware-multi-vehicle-connection-issue/77020 |
| [RB] | Bench readback 2026-10-28, `.coordination/hardware-20261028/rrls-live-radio-settings-attempt2.json` (RRLS: S2=64, S3=3968, S4=10, S5=10, S6=50, S11=32768, RTSCTS=0). The GS read is a cached 10-20 read. The RF modem was never read. |
| [ST] | Bench GS UART strace 2026-10-28, `.coordination/hardware-20261028/radio-wire-analysis.json`: 44.8 s; GS read 39,440 B (880 B/s); GS wrote 12,375 B (276 B/s). |

## Behaviour and model, item by item

| Item | Real behaviour (source) | Model before | Model now |
| --- | --- | --- | --- |
| Air rate vs serial rate | Async S2: 64/125/250/500/830; the air rate should be lower than the baud rate ([M] p. 6, p. 9). Deployed: 64 kbit/s air, 115200 UART ([RB]). | 64000 / 115200 | Unchanged. |
| TDM / frame structure | Async has no TDM: "There is no transmission coordination or synchronisation of the network" ([DS] §5.3); "nodes don't require synchronisation" ([M] p. 3). TDM rounds and MAX_WINDOW belong to SiK ([P2P] p. 20) and multipoint ([MP] p. 7). | A central round-robin arbiter: one station on air at a time, 2 ms turn. No collisions. | `FIRMWARE_SIM_RFD_MAC=async` (the default once `FIRMWARE_SIM_RFD_MODEM` is set). Every station transmits independently. The arbiter remains for older scenarios. |
| Collisions | Uncoordinated access with "random back off time to minimise the chance of data collision" ([DS] §5.3); "some nodes ... locked out by other nodes regularly transmitting" ([SUP]). The CRC rejects corrupted packets ([DS] p. 33). | None. | A copy is lost at every receiver that hears another station during it. There is no capture effect (assumption A4). Counter 12 counts these losses. |
| Half duplex | A single radio per modem ([DS]). | None. | A station transmitting, or within `turn_us` after, receives nothing. |
| LBT | Async: LBT_RSSI "shouldn't be changed", default 0, must match ([M] p. 7; S18 on 4.03). In SiK, 0 = LBT off; when on, the channel must be clear for 5 ms plus a random 0-5 ms ([SIK] tdm.c `LBT_MIN_TIME_USEC 5000`, `lbt_rand`; ETSI EN 300 220 9.2.2.2). | None. | Follows each modem's active LBT_RSSI: 0 is off; non-zero waits for a clear channel for 5 ms plus a random 0-5 ms, redrawn after each busy period. Counter 13 counts deferrals. `FIRMWARE_SIM_RFD_CARRIER_SENSE=always\|never` overrides this (assumption A2). |
| Max window | Async 4.03 lists S20 MAX_WINDOW=131. Its meaning is not documented for async. In SiK it is the TDM transmit window in ms ([P2P] p. 20). | Not modelled. | Not modelled (assumption A5). |
| Packet size | Async S3 MAX_DATA 1..4068, "Maximum size of packet data section", must match ([M] p. 6). Deployed 3968 ([RB]). SiK's 252-byte limit does not apply. | Global `max_burst_bytes` for every station. | Each modelled station uses its own S3, capped at 4068 and at its TX buffer. |
| ECC / Golay | Async has no ECC register ([M] Table 3-2); error detection is CRC ([DS] p. 33). SiK's Golay doubled the air bytes ([SIK] tdm.c) and was removed in 3.15 ([SIKRN]). | No FEC. | Unchanged. |
| Air framing per copy | Not documented. Open SiK uses an 8 B preamble, 2 sync, 2 header, a length byte, 2 CRC and a 2 B trailer, about 17 B ([SIK] radio_443x.c, tdm.c). | 160 bits (20 B). | Unchanged (assumption A3). |
| S5 GLOBAL_RETRIES | "Maximum number of retries for broadcast messages", 0..100, default 10, must match ([M] p. 7). Whether retries are blind repeats or only follow a missing acknowledgement is not documented. [RN] 2.45 lists rates "no acknowledgement / with acknowledgement". | One global value, read as blind: every packet on air 1+S5 times. | Applied by each sending modem from its own active S5. The reading is selectable with `FIRMWARE_SIM_RFD_BROADCAST_RETRY`: `on_loss` (the default) or `blind`. A receiver keeps and outputs the first intact copy (assumption A1). |
| S6 SER_BRK_DETMS | "Time in ms units for break detection. Set to 0 to turn this feature off", 0..500, default 50, need not match ([M] p. 7). | The scenario's global `packet_idle_us`. | Each modelled station uses its own S6. 0 is treated as one character time. |
| DUTY_CYCLE | Not in [M]; S17 on 4.03. [RN] 2.60: "duty cycle limiting working correctly"; SiK limits a running average of transmit time ([SIK] tdm.c). | Global `FIRMWARE_SIM_RFD_DUTY_CYCLE`, strict hold after each packet. | Per station from its own register; still a strict hold, not SiK's running average. |
| Settings take effect | `ATSn=` writes RAM; `AT&W` then `ATZ` (a reboot) applies it ([M] p. 5). | Air parameters were not read from registers. | Air parameters use the register's active value, which changes only at the modelled ATZ reboot. |
| Serial buffers | Async: heap 8 kB ([RN] 2.45) and "variable buffers" ([RN] 2.47). `AT+F?` reports a forwarding buffer (FwdBuff=13320 in the 4.03 field format), and lowering MAX_DATA enlarges it ([M] p. 6). The serial buffer size is not documented. | TX/RX FIFO 4096 in `rfd-faithful`. | Unchanged (assumption A6). |
| RTS/CTS | Async default S18 (4.03: S19) RTSCTS=1 ([M] p. 7). Deployed 0 ([RB]); GS and RF UARTs have no CTS wired. | No flow control. | Unchanged. |
| Overflow without flow control | Async: not documented. SiK discards bytes that do not fit ([SIK] serial.c `serial_rx_overflow`); "data loss due to input buffer overwrite" ([P2P] p. 20). | A full TX FIFO refuses new host bytes (counter 4); a full RX FIFO drops bytes (counter 5). | Unchanged. |
| Latency per hop | No figures for async; "Latency and reliability will vary with number of nodes and data traffic" ([M] p. 3). | Derived: UART in, then S6, then copies × air, then UART out. | The same terms, now from the sender's own registers. The receiver outputs at the first intact copy. |
| Multipoint vs point-to-point | Point-to-point is a TDM serial bridge; multipoint has a master that allocates slots ([MP] p. 10); async is single-hop with equal nodes, plus an optional fixed `AT+F` forwarding table ([M] pp. 3, 11). | Single hop, broadcast. | Unchanged. The forwarding table is not modelled; the deployment uses none (GS `rrls.md`). |
| Command mode | `+++` after at least 1 s of silence; in command mode the modem stops passing incoming data ([M] p. 5). | Modelled. | Unchanged. |

## Pinned timing

The tests are in `tests/radio-medium/Program.cs`. All use 64 kbit/s air, 115200 8N1 (87 µs per character) and
160 bits of framing per copy.

- **`RADIO_RFD_PER_STATION_AIR_TIMING_PASS`**
  - Setup: the GS is at the deployed drain profile (S5=1, S6=10); RF is at the 10-28 field readback (S5=10, S6=50).
    The frame is 40 B, single, and the air is otherwise quiet.
  - GS→RF: 3480 + 10000 + 7500 + 3480 = 24.46 ms. RF→GS: 3480 + 50000 + 7500 + 3480 = 64.46 ms.
  - This holds under both S5 readings, because the receiver outputs the first intact copy.
  - The arbiter MAC delivers after the last copy: 31.96 ms and 139.46 ms with blind copies.
  - The test also checks: a packet closes at the station's own MAX_DATA; a cut peer under `on_loss` costs every
    resend; and with seeded loss, delivered + lost = sent.
  - The old medium fails this test: it measured 16.5 ms in both directions.
- **`RADIO_RFD_ASYNC_COLLISION_HALF_DUPLEX_LBT_PASS`** checks that:
  - simultaneous GS and RF packets are lost at the RRLS and at each other;
  - with LBT off, a GS packet sent over a long RF packet destroys both at the RRLS;
  - with LBT on, nothing is lost, and the GS starts 5-10 ms after the channel clears (measured 8.8 ms);
  - a blind second copy that lands clear is delivered.
- **`RADIO_RFD_BROADCAST_COPIES_DUTY_PASS`** additionally checks that the default `on_loss` reading spends one
  copy per packet on a clean link.

## Open assumptions and the bench measurement that settles each

None of these needs a firmware change. Each measurement only reads the modems or captures the UARTs with strace,
using the existing passive capture script.

- **A1. GLOBAL_RETRIES reading.**
  - Evidence: [ST] carried 880 B/s RF→GS on 64 kbit/s air. The RF modem was at its factory S5=10: S5 is link-wide
    and the RRLS read 10, and no RF code wrote S5 before RFBoard26 2c471b4 (2026-11-08). Eleven blind copies
    would need 880 × 8 × 11 = 77 kbit/s. So the default is `on_loss`, and `blind` is kept as an option.
  - The `on_loss` model assumes an acknowledgement from every attached station. That is undocumented for
    broadcast.
  - Measurement: with all three modems at S5=0, then S5=1, then S5=10 on a clean link (antennas apart, no third
    transmitter), stream a fixed 2 kB/s of broadcast from one modem for 60 s. Capture the sender's UART and a
    receiver's UART.
    - Blind predicts receiver throughput collapsing near 64000 / (8 × (1+S5)) ≈ 727 B/s at S5=10.
    - On_loss predicts the same throughput at every S5.
    - Duplicate-suppressed receive counts at the receiver also reveal copies if a test build disables
      suppression.
- **A2. LBT with LBT_RSSI=0.**
  - The sim follows SiK: 0 means off, so stations collide freely. [DS] §5.3 says async transmits "if an RF channel
    is free", which suggests carrier sense regardless of LBT_RSSI.
  - Measurement: two modems each stream saturating broadcasts at the same time, and a third counts CRC-valid
    packets.
    - No carrier sense: both streams lose a large share, and the loss grows with load.
    - Carrier sense: the streams share the air with little loss.
  - Re-run the scenarios with `FIRMWARE_SIM_RFD_CARRIER_SENSE=always` to bound the alternative.
- **A3. Air framing per copy (160 bits).** Measurement: time a single 1-byte and a single 1000-byte broadcast
  from host write to remote UART output at S6=0 (or small) and S5=0. The intercept minus the UART time gives
  the framing and turnaround.
- **A4. No capture effect.** A stronger signal may survive an overlap. Measurement: the A2 setup with the
  receiving modem much closer to one sender.
- **A5. MAX_WINDOW in async.** If it caps one transmission at 131 ms (about 1 kB at 64 kbit/s), large packets
  split. Measurement: send a 3 kB burst and record the remote UART output's inter-byte gaps.
- **A6. Serial buffer size.** Measurement: write N bytes back to back at 115200 with RTSCTS=0, and count what the
  remote outputs, for N = 2, 4, 8, 16 kB.
- **Turnaround (`turn_us` 2 ms) and the ATZ reboot (1.5 s).** Both are undocumented. A3 also yields the
  turnaround. The GS modem check log already times ATZ to the next `OK`.

## Using it

- **`scripts/test-full-network.py --rfd-profile drain|field`.** This models every radio station's modem with the
  drain or field registers and selects the async MAC. Further options:
  - `--rfd-broadcast-retry on_loss|blind`;
  - `--rfd-air-loss-ppm N`, an independent loss per copy at each receiver;
  - `--rfd-carrier-sense register|always|never` (assumption A2).
- **`Snapshot()` per node.** It adds `global_retries`, `packet_idle_us`, `max_data` and the counters:
  - 10: bytes lost on air;
  - 11: copies sent;
  - 12: copy receptions lost to overlap;
  - 13: LBT deferrals.
