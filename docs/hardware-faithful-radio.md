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

`scripts/test-full-network.py --scenario faithful` runs the seven-board network in Normal mode at Idle on
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

Use `--rrls` to add a hosted RRLS on the same medium (`--scenario pairing` drives UI pairing over the radio).
`--gs-env` passes extra station variables.
