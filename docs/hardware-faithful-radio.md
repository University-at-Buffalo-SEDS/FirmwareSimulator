# Hardware-faithful radio and station time

Linked simulations used to look healthy while the bench was crippled. There were two causes:

- **Air model.** The medium modeled 64 kbit/s air with a 2 ms idle cutoff and one copy per packet. The deployed
  RFD900x2 ASYNC 4.03 profile (`GroundStation26/backend/comms/rfd-profile.json`,
  `rfd-async-64k-compatible`) uses `SER_BRK_DETMS=50`, `MAX_DATA=3968` and `GLOBAL_RETRIES=10`. The DESTID is
  broadcast, so every packet holds the air for 1 + 10 copies. The UART runs at 115200 without RTS/CTS.
- **Two clocks.** Renode runs 10 to 25 times slower than real time, but the hosted GroundStation runs on wall
  time. Its heartbeat, discovery, time sync, reliable retries, the 2 s `ReadonlyQueryRefresh` and the
  15 s / 5 s freshness TTLs therefore ran 10 to 25 times faster than the boards. `GS_SIM_WALL_FRESHNESS_SCALE`
  stretched the freshness windows another 30 times, so every readback looked fresh.

## Model

`renode/peripherals/SedsRadioMedium.cs` reads two optional variables. Without them it behaves exactly as before.

| Variable | Meaning |
| --- | --- |
| `FIRMWARE_SIM_RFD_GLOBAL_RETRIES` (0..100) | Each packet occupies the air `(payload*8 + overhead_bits) * (1 + n)` bits. Receivers get one copy. |
| `FIRMWARE_SIM_RFD_DUTY_CYCLE` (1..100 %) | After each packet the sender stays off the air for `duration * (100/duty - 1)`. |

The radio profile fields keep their meaning:
- `packet_idle_us` is `SER_BRK_DETMS`: a packet closes after that much UART idle.
- `max_burst_bytes` is `MAX_DATA`.
- `tx_fifo_bytes` is the modem buffer. A full buffer drops host bytes (refused counter 4) because there is no RTS/CTS.
- `uart_baud` serializes ingress.

Snapshot counter 9 counts packets on air, each counted once regardless of copies.

`renode/peripherals/SedsSimulationClock.cs` reads `FIRMWARE_SIM_WALL_PACE`, the number of wall seconds per
virtual second. It holds the emulation so that virtual time never runs ahead of `wall / pace`. Every 1000 virtual
ms it writes `virtual-clock.txt.pace` with three values: the virtual ms, the wall ms and the largest lag behind
the pace. Run the hosted station with libfaketime at the same factor:

    LD_PRELOAD=/usr/lib/x86_64-linux-gnu/faketime/libfaketimeMT.so.1  FAKETIME="+0 x0.05"   # pace 20

The station's wall timers then age at the boards' rate. Its radio capacity pacing already follows
`GS_SIM_CLOCK_FILE` (100 ms burst, 55 ms gap). Do not set `GS_SIM_WALL_FRESHNESS_SCALE`. The image installs
`libfaketime`. Choose a pace above the bay's natural slowdown. Seven boards on a loaded 24-core host need about
20, and the pace file shows whether the run kept up.

## Scenario

`scripts/test-full-network.py --scenario faithful` (default `--rfd-global-retries 3`, see Calibration) runs the seven-board network in Normal mode at Idle on
`--radio rfd-faithful --faithful-time 20`. It records the counters the Pi exposes every 5 virtual seconds into
`faithful.jsonl`:
- from `/api/network_diagnostics`: `radio_tx_budget`, `queues.tx_len`, `reliable` and per-type tx/retries;
- from `/api/network_topology`: per-board health, including `clock_offset_ms` and DAQ SD status;
- `/api/station-modes`;
- `/api/board-recovery/status`, the action policy and the board aborts.

It then asserts the following, numbered by the bench fault list:

1. The router tx queue does not grow. Refusal spins stay at or below 20 per on-air frame. Reliable pending stays
   at or below 8. HEARTBEAT, TIME_SYNC_ANNOUNCE and DISCOVERY_ANNOUNCE reach the air.
2. After an Abort at Idle, Clear GSE abort succeeds within 60 s.
3. The HITL mode switch is offered at Idle and while Aborted with nothing moving. It is refused while the Abort is
   still in delivery.
6. DAQ SD status is shown and the DAQ readback is confirmed.
8. `clock_offset_ms` is present for every board.
10. All seven boards stay launch-proof fresh (`readback_current`, age at most 5 s) in at least 98 % of samples.
    Launch never reports missing fresh board state.

The dilated station clock starts at the simulation network epoch (`FAKETIME="@2026-01-01 00:00:00 x<1/pace>"`).
Host wall time and the boards' network time then share one base, which the RRLS clock comparison relies on.

`SEDS_UART_GATE` accepts several `machine:peripheral:file` entries separated by `;`.
`stale_auto_abort` uses `gateway:usart2` to take the Gateway Board's Pico-Fi link to the station down, alongside its CAN cut.

The harness stops a run whose virtual clock makes no progress for 900 s of wall time.
Several runs with RF 74e78f4 stalled in Renode's `RunFor` of the tenth sample; see "Known issue" below.

Use `--rrls` to add a hosted RRLS on the same medium (`--scenario pairing` drives UI pairing over the radio).
`--gs-env` passes extra station variables.

More scenarios:

| Scenario | What it asserts |
| --- | --- |
| `mode_switch` | Only an active sequence, cold flow, turbocharge, a pending Launch or T-minus may hold the mode (GS 0be3424 and 322cfd8). The in-process normal/hitl switch must succeed in each of these states: at Idle right after the startup sequence; with DAQ missing (scheduled `dq0`/`dq1` CAN cut); with a board recovery query pending; while Aborted with a board aborted and the AV-bay ABORT undeliverable (`cut0`/`cut1` GS-RF radio cut); after the abort is cleared; after a manual valve command completes. It must be refused while the startup sequence is still moving valves. T-minus, turbocharge and cold flow need a fill or launch sequence and are not covered. |
| `rrls_faithful` | The RRLS on the same medium (`--rrls`, AV-bay nodes). Pairing over the radio finishes within about 1 s per step. The idle RRLS is seen alive at the primary. An RRLS-issued query reaches the primary within 1 s. There is no relay traffic on a healthy direct path. With the direct path cut, the relay engages at or below a 15 % air share and stops afterwards. The idle RRLS shows live data from every board, and its network clock stays within 1 s of the primary. |
| `gps_time` | `--gps-initial 2`: a cold u-blox receiver (no fix, placeholder date). Every board keeps network UTC, none runs ahead of the station or steps backwards, before and after a valid fix. |
| `stale_auto_abort` | A GB CAN cut (`gbc0`/`gbc1`) raises the Normal-mode automatic Abort while the radio is saturated. If GB returns before 30 s, the abort is retired by age with its cause cleared. If GB stays offline (60 s or more), it is not retired by age, and an operator Clear retires it. In both cases the AV bay never aborts afterwards. An automatic Abort leaves the station's flight state alone, so the scenario detects it by `gse.automatic_abort_claimed` (an operator Abort shows as flight_state 15). |
| `vb_stale` | No Valve Board stale-sensor [S] warning during an outage or within 10 s of the VB coming back. The outage is either a VB power cycle (`vbrb`, a scheduled reboot) or a VB CAN cut (`vbc0`/`vbc1`). An injected F fault inside the grace still warns. |

## Calibration

The bench snapshot covers 60 s in Normal mode at Idle, all boards, RRLS off. On rocket_comms it shows:
- about 1,135 rx and 392 tx frames per minute, 346 of them RELIABLE_ACK;
- tx_len growing about 12 frames/s;
- refusals dominated by control_budget.

The seven-board faithful sim on GS aed4351 at pace 20 measured over 95 virtual s:

| Extra copies | rx/min | tx/min | ACK/min | HEARTBEAT/min | tx_len growth/s | Launch-proof fresh |
| ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 0 | 1029 | 549 | 280 | 64 | 0.83 | all boards |
| 3 | 736 | 557 | 246 | 64 | 0.76 | 10-45 % per board |
| 10 | 180 | 410 | 16 | 64 | 0.46 | 0-17 % per board |

Station tx stays at 6.4-9 frames/s on every row, which is the 100 + 55 ms pacing cap. The uplink and ACK volume
come closest to the bench with 0 to 3 extra copies; the default is 3.

Differences from the bench that remain:
- Heartbeat and time sync reach the air in the sim but almost never on the Pi.
- tx_len grows about 15 times faster on the Pi.
- The refusal mix differs: control_budget on the Pi, large_frame_reservation in the sim.

That refusal mix suggests the snapshot came from a station older than aed4351.

## Known issue

About 40 % of the faithful runs with RF 74e78f4 and the RF modem model (`--rfd-modem 0=`) hung in Renode.
- The hang came right after `SEDS_BAY_SAMPLE_DONE 9`: every machine was resumed, but `RunFor` never returned.
- The station stayed responsive.
- The cause is not yet known.
- Runs with the older RF, and other runs with the same heads, completed.
