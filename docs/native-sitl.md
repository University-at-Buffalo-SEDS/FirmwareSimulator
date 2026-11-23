# Native SITL engine

A fast tier for the fullnet scenarios: each board's production firmware is compiled for the host and run as a
process on one virtual clock, with the real GroundStation backend as a host process. Renode stays the reference
for what native cannot show (see [What stays Renode-only](#what-stays-renode-only)).

```sh
# same arguments as a Renode fullnet, plus the engine switch
python3 scripts/test-full-network.py --engine native --mode test_fire --scenario clock_steps \
    --nodes gateway,actuator,valve,daq --faithful-time 10 --gs-clock-offset-ms -13680000 \
    --clock-steps forward,backward --duration-ms 600000 --cohort <cohort> --image <seds-faithful image> \
    --evidence <dir>
# or a branch's controller (unchanged) through the engine directly
python3 scripts/native_engine.py --controller <other checkout>/scripts/test-full-network.py <controller args>
```

Engine options (before or among the controller's): `--native-builds DIR` (default `build/native/boards`; missing
nodes are built from `--cohort`), `--native-pace X` (virtual s per wall s, default 10), `--native-quantum-us`,
`--native-seed`, `--drift NODE:PPM`, `--gs-dir` (default: extracted from `--image` into `build/native/gs/<tag>`).
Evidence has the Renode layout (record.jsonl, actions.jsonl, result.json, diagnostics/bay-live-*/memory-probes.txt,
diagnostics/temp/*/virtual-clock.txt), plus `diagnostics/native-<node>.log` (board stdout), `native-gpio.log`
(output pin edges, gates, CAN link changes), `native-can-trace.txt` (with `--can-trace`) and `native-report.json`.
`result.json` gains `engine`, `native_engine` (that report) and `native_pace_held`.

## Speed and parity (Phase 1)

| scenario (cohort) | Renode wall | native wall | virtual | checks (Renode / native) | parity |
|---|---|---|---|---|---|
| clock_steps forward,backward, GS -3.8 h (timesync final3) | 1196 s | 14 s | 111 s / 135 s | 38/38 pass / 38/38 pass | pass/fail 38/38, 38 metrics in tolerance |
| vent_countdown cut 120..310 s (vent1) | 3589 s | 53 s (2 runs in parallel) | 340 s | 9/9 / 9/9 | 9/9; vent at 270 s in both, countdown word 37125 in both |
| vent_countdown cut 120..180 s (vent1) | 3079 s | 30 s | 295 s | 7/7 / 7/7 | 7/7 |

About 100x less wall time per virtual second than Renode at its usual `--faithful-time 10`, at about 1-1.5 host cores
per run (Renode: 6 cores, gated to 4 runs). Board-only lockstep (no station) runs 4 boards at 12x real time; one board
alone runs 30 s of firmware time in 0.2 s. A board builds from scratch in about 30 s (ARM configure, host SDK,
compile) and incrementally in about 1 s.

Parity: `scripts/native_parity.py --renode RUN --native RUN` compares every check's pass/fail and the metrics the
checks record (recovery and confirmation durations, operator-UTC reach time, network-gap worst, per-board clock-stamp
median and most-ahead stamp, countdown start/word, vent time, Launch time) within tolerances;
`--renode RUN --run-native DIR` replays a finished Renode run's command natively first. Expected differences (reported,
never failing parity):

* clock-spread `min_ms`/`max_abs_ms`/packet counts: transport tails and sample counts differ by engine; `max_ms` (the
  most-ahead stamp) is one-sided: Renode shows up to ~90 ms of timesync jitter from its instruction-time pacing,
  native is tighter;
* DAQ stamp median is ~60 ms behind the station natively vs ~8 ms in Renode until the MCP3564R model lands (Phase 2):
  without load-cell samples the DAQ sends only health/analog traffic, which is stamped earlier relative to sending;
* `received_age_ms` series, wall stamps, event counters, stack/heap/pool figures (host stacks are not the board's).

Determinism: without a station the run is a pure function of inputs and seed (two 20 s four-board runs produce an
identical 6808-frame CAN trace). With the station it is reproducible up to the station's wall-clock scheduling
(below).

## How it works

* `native/build_board.py <board-src> --out DIR` configures the board's own CMake for ARM (configure only, pinned
  SEDSnet/LaunchCore/FileX like the cohort builds, the tree's own CMake options) and takes the flight link's exact
  sources, defines and include paths from `build.ninja`/`compile_commands.json`: application, ThreadX kernel
  (`common/src`), FileX, LaunchCore, middleware. They are compiled with host `gcc -m32` (32-bit pointers as on the
  MCU) against the board's real HAL/CMSIS/device headers. Replaced: HAL driver sources, the ThreadX Cortex-M port,
  startup/syscalls/sysmem/system, `stm32*_it.c`, USBX. The board's prepared SEDSnet SDK (its schema, features,
  `DEVICE_IDENTIFIER`, queue env) is built for `i686-unknown-linux-gnu` through a wrapper crate
  (`native/sedsnet_host_hooks.rs`) that supplies the allocator/panic hooks the bare-metal SDK build gets for
  `target_os = "none"` (allocations still come from the board's own pool via `telemetryMalloc`). No board or SEDSnet
  source is modified; ARM-EABI struct layout assertions are not evaluated (64-bit members are 4-aligned on i386).
* `native/shim/` is the hardware boundary, one library compiled per board against that board's headers:
  * `sim_core.c`: virtual time, timed interrupt events, simulated NVIC/PRIMASK, the lockstep link to the coordinator,
    CPU-cost accounting (shim calls and busy waits consume virtual time; idle time is skipped);
  * `sim_threadx_port.c` + `include/tx_port.h`: a ThreadX port for the real kernel. Threads are host coroutines,
    `_tx_thread_system_return` is the PendSV, interrupts preempt at interrupt points, the 1 ms tick drives the real
    timer wheel; scheduling is deterministic;
  * `sim_mmio.c`: the MCU's fixed address ranges mapped at their real addresses (flash and the backup-domain page
    file-backed so they persist across simulated reboots; first boot from the cohort's factory image), UID/flash
    size/calibration words, reset-cause flags, live SysTick/DWT values, IWDG and `NVIC_SystemReset` resets;
  * `hal_fdcan.c` (3-slot Tx FIFO with frame timing, filters, Rx FIFOs, unacknowledged-bus behaviour, register
    mirrors), `hal_uart.c` (DMA/IT/blocking Tx with byte timing; ReceiveToIdle/DMA with HT/TC/IDLE events and the DMA
    counter), `hal_core.c` (RCC/PWR/NVIC/GPIO/EXTI/TIM/flash/ADC+DMA/DAC/I2C/SPI/caches/USB), `hal_storage.c` (SD
    card image under the node state dir, HASH SHA-256), `sim_devices.c` (ADC channel samples with noise and faults,
    LTC2990 monitors, static input pins);
* `firmware-sim native-bay --topology T --native N` (src/bay/native.rs) runs the bay topology: lockstep grants to all
  nodes (1 ms quantum, idle skip up to 20 ms), CAN buses with the topology's `can_link_events` (a node alone on a bus
  gets no ACK: frames stay pending, error passive), frames delivered with their original spacing and serialized per
  bus, the Pico-Fi peer (src/bay/native_pico.rs: same discovery/handshake/framing/mailboxes as the Renode bay's model,
  stepped on virtual time), `SEDS_UART_GATE` files, `reboots`, stimulus files (pressure.raw, loadcell.raw), memory
  probes read from the processes' globals (`/proc/<pid>/mem` at the native symbol addresses, so `--extra-probes`
  works unchanged), and the station's virtual clock file;
* `scripts/native_engine.py` runs an unchanged fullnet controller: it serves the controller's docker calls locally,
  maps container paths onto the run directory, and runs everything in a private network and mount namespace
  (`unshare --user --net --mount`) so the station keeps its fixed ports and parallel runs never collide. The station
  is the image's `groundstation_backend` (its built-in source-tree default `/src/gs-head/backend` is moved to the
  run's private `/tmp/seds-gs/backend` by an equal-length string substitution) under the image's libfaketime.

### FC pad sit with weather drift

`scripts/native_baro_drift.py` runs the FC alone (no station) on a long pad sit: `SIM_BARO_PA` and
`SIM_BARO_RAMP_PA_PER_H` set its barometer's pressure and a steady drift from boot (the Renode flight-profile trim
set, P = raw / 128 Pa), the IMU rests at 1 g, and the coordinator probes the FC's `meas.baro.alt`, pressure and
`baro_ref` (FlightComputer26 `baro_reference.h`) once per virtual second. A fixed reference (the boot zero) is
computed from the same pressures. `--compare` runs a second build the same way.

```sh
python3 native/build_board.py ../FlightComputer26 --out build/native/fc
python3 scripts/native_baro_drift.py --build build/native/fc/flight --hours 1 --ramp-hpa-h -3 --out evidence/pad-sit
```

FlightComputer26 baro-drift (e615de3), -3 hPa/h: over 3190 virtual seconds the FC's altitude stayed within 0.22 m
(the 30 s tracking lag) while the reference moved -263 Pa; a fixed reference would have read +22 m. The FC alone
slows as its unacknowledged CAN traffic builds up (about 8x real time at first, near 1x after 50 min). Launch and
flight need the station, so the freeze at Launch is covered by the FC's host tests, not here.

## The station and the speed limit

The GroundStation backend is a tokio application on the wall clock (heartbeats, retries, refresh intervals,
freshness ages; about 800 `Instant`/`SystemTime` reads and 50 OS threads). By default (`--station-clock lockstep`)
its clocks follow the bay instead, with no station code change: `native/simclock/simclock.c`, preloaded into the
station, derives every clock read (`clock_gettime`, `gettimeofday`, `time`) from a shared page the coordinator updates
before each lockstep window (window start, horizon, measured virtual/wall rate) and turns every timed wait (sleeps,
`epoll_wait`/`poll`/`ppoll`, raw futex waits under Rust's Mutex/Condvar/park) into short wall slices of the remaining
virtual time. The station's time never runs ahead of the boards': when the host is loaded the bay slows and the
station waits with it, and when the bay runs faster the station's timers run faster with it.

Measured (clock_steps, current dev boards and station f34453b):

* load-insensitive: four runs in parallel at load 16 each fell ~7 s behind the 10x pace and all passed 38/38 (with
  the earlier fixed-rate libfaketime clock, two parallel runs already failed the DAQ launch-log checks);
* unlimited pace (`--native-pace 0`, `--native-quantum-us 2000`): 165 s of virtual time in 9 s (18x); with 1 ms quanta
  12x. The lockstep's process hand-offs are now the bound, not the station;
* above ~12x the station's own compute (not lockstepped: a request handled in 1 ms of wall time costs 18 ms of
  virtual time) shows: 1 of 3 unlimited runs passed every check; the others hit the station's 100 ms final-clock retry
  budget or an empty recording window right after StartWritingNow. The default stays a 10x cap
  (`--native-pace 10`), stable; use `--native-pace 0` for exploratory sweeps.

`--station-lockstep threads` puts the station's compute in the lockstep too: virtual time is granted only while
all of its threads are parked (see Status, Phase 3). `--station-clock faketime` keeps the previous fixed-rate libfaketime mode.

## Integrated ground test (igt_flow)

`--scenario igt_flow --mode integrated_ground_test` runs the integrated ground test (GroundStation26
`docs/integrated-ground-test.md`) on all seven boards:

1. GSE flows as for a test fire, with the AV bay present.
2. Enter ground test (prepare/confirm). The FC validates its SD-card CSVs.
3. Postinit, then Launch through the FC and VB.
4. The FC replays the flight into its real estimator and state machine.
5. Drogue and main events, landed, and GPS back to real; the replayed GPS track moves a few hundred metres in
   latitude and longitude.



The pyros are dry-run by default and must drive no GPIO edge in `native-gpio.log`. With
`--igt-live-pyro`, PB5/PB6 must rise.

* **FC image:** it needs the ground-test option. Build it once with
  `native/build_board.py <cohort>/FlightComputer26 --out <builds> --define INTEGRATED_GROUND_TEST=ON --factory`,
  then pass `--native-builds <builds>`.
* **CSVs:** generate them with GroundStation26 `tools/igt_csv.py model --out DIR`, then pass `--igt-csv DIR`.
  `build_config` writes them into a FAT16 image, `config/flight-sdcard.img`, using `scripts/fat_image.py`.
* **SD seeding:** `native_engine.py` copies any `config/<node>-sdcard.img` to `<state>/sdcard.img` before
  the boards start, so the SD shim serves it from first boot. Other scenarios use the same hook.
* **Pace:** run with `--native-pace 2`. It passes 32/32 in about 2 minutes of wall time. At the default pace of 10,
  the controller's GSE plant writes its PT samples too sparsely in virtual time: the station pauses on "Tank
  pressure is missing, invalid or stale". Plain test_fire `gse_flows` fails the same way at that pace.
* **HITL variant:** `--mode hitl` turns on the station's HITL replay setting (`hitl_replay`). It runs HITL's fill
  flows and phase path, and the FC's CSV replay is the flight data source.
* **Late SD card:** `--igt-sd-late` boots the FC with an empty card. The scenario checks the station's SD alert and
  that Enter faults at once. It then writes the CSV card image into the SD shim's backing file and presses Retry SD:
  the FC mounts the card and re-runs the validation.
* **Image guard:** `--scenario igt_image_guard --mode normal` (or `test_fire`) runs with the ground-test FC image.
  It checks that the station warns and latches Launch closed, with the reason on the Launch control.
* **GPS:** the native engine does not model the RF GPS, so the FC sees no real fix. The replay supplies GPS
  until landing. After landing, "GPS: real" means no fix.

## What stays Renode-only

Native runs the boards' logic, protocols, state machines, persistence and timing at the millisecond scale. It does not
show, and Renode (instruction-level, register-accurate) remains the evidence for:

* interrupt and DMA timing and races at instruction granularity (ISR preemption inside non-yielding code, NVIC
  priorities and nesting, PendSV tail-chaining, DMA vs CPU access ordering); native interrupts run at interrupt
  points with interrupts enabled and have one priority level;
* cycle-accurate CPU budget: native charges coarse costs per HAL call; loops that never call into the HAL take no
  virtual time (DWT busy-waits are broken by a CPU-time timer instead);
* flash/RAM fit, stack depth and overflow (threads run on host stacks; `*_stack_*` probes read the untouched board
  stacks), allocator fragmentation from ARM struct layout, ARM-EABI layout assertions;
* peripheral register behaviour: FDCAN message RAM and bit timing details, GPDMA linked lists, SPI/SDMMC timing, UART
  FIFO/overrun at the byte level, the USB device stack (not simulated natively), hardware CRC (the GB falls back to its
  software CRC natively), option bytes;
* the bootloader, image validation and the OTA/delta-update path (native starts the application directly from the
  factory image's flash contents);
* radio byte timing on the RFD900x air model (Phase 3: the native radio link is coarser until ported);
* the MCP3564R/SPI load-cell front end and other device models not yet ported (Phase 2).

### Acceptance set (Renode)

Run on Renode before a release and before any board firmware push to dev (not for everyday iteration):

* `clock_steps` (forward,backward; GS -3.8 h) and `bench_symptoms` on the faithful radio;
* `vent_countdown` (one long cut) and `config_resync`;
* `rrls_takeover` (RRLS, faithful radio) and `pyro_paths`;
* `gse_flows` once per release (fill flows, ADC front ends);
* any scenario whose parity check failed natively, plus the qualification-only ones (OTA/delta, flash-fit, stack).

Everything else, and every scenario during development, runs natively; `native_parity.py --run-native` on the
latest Renode acceptance runs keeps the native tier honest.

## Status

* Phase 1 (done): shim library; GB, AB, VB, DAQ native builds; CAN with link cuts; Pico-Fi umbilical; native station;
  clock_steps and vent_countdown pass natively with parity to their Renode runs.
* Station lockstep (done): `native/simclock` (above).
* Phase 2 (done), current dev boards (DAQ 55fe7a8, AB 9fa7b58, VB 1ac09c9, GB c68c3b5) and station f34453b:
  * MCP3564R model with data-ready (native/shim/src/sim_mcp3564r.c); with it the DAQ's stamp median matches Renode
    (-4..-11 ms) and its packet rate (~6.7k per window vs Renode ~6.1k);
  * `input_faults`: the 2026-11-16 fire patterns (VMAIN 11.1 V at boot, KG1000 clip at code 0xB40000 and the
    recorded clip, KG1000 low rail, P6 S+ dropout, dead chamber) replayed at their sample times
    (scripts/native_signals.py, diagnostics/native-signals.txt); the station reports and clears each DAQ input fault.
    The DAQ checks the 0-5 V chamber PT only in the launch burn window (T0 + 1.5 s to T0 + 5 s), so dead_chamber is
    replayed once at rest (no fault) and once around a real Launch at the fixture's launch timing; Launch in Armed
    needs `--mode hitl_mode` (test fire also wants the vent closed first), and `--flows` selects patterns;
  * `--native-blank-flash NODES|all`: first boot on erased flash (no bootloader/app/metadata);
  * config_resync (with daq/actuator reboots), launch_reset_idle and gse_flows pass natively.

  | scenario | Renode wall | native wall | checks |
  |---|---|---|---|
  | clock_steps (dev) | - | 14 s | 38/38 |
  | vent_countdown cut 190 / 60 (dev) | 3589 / 3079 s | 37 / 30 s | 9/9, 7/7 |
  | config_resync + reboots (dev) | 3217 s (cfg1) | 32 s | 6/6 |
  | launch_reset_idle (dev) | 1110 s (fix-dev) | 15 s | 11/11 |
  | gse_flows, test fire, umbilical (dev) | - | 18 s | all pass |
  | input_faults (dev) | - | 13 s | 14/14 |
  | input_faults, chamber burn window, HITL | - | 105 s | 18/18 |

  With `--native-blank-flash all`, config_resync's "went through resyncing" check fails: the station restores so fast
  that the controller's 1 s wall-clock watch polls (10 s of virtual time at 10x) never see the intermediate state. This
  is a controller sampling artifact, not a board difference.
* Phase 3: FC (STM32H523), RF and PB build and boot natively, and all seven boards reach readback_current over the
  native radio in HITL. What it took:
  * RF decoded no station frame at first: it reads USART CR3.DMAR to know reception is armed, and restarted it every
    10 ms. The UART shim now mirrors CR3 DMAR/DMAT, CR1 RE/IDLEIE and the DMA handle state.
  * FC sensors: the FC sensor bus (BMP390/BMI088) is modelled at rest.
  * FC boot Abort: the FC held an Abort latch from boot because, without LSI and a binary-mode RTC, it refused
    watchdog resume. LSI/RTC handshakes now complete, and RTC SSR runs from global virtual time.
  * Radio: the air medium has the Renode medium's RFD ASYNC command-mode model (FIRMWARE_SIM_RFD_MODEM) with
    per-modem GLOBAL_RETRIES, SER_BRK_DETMS, MAX_DATA and AIR_SPEED.
  * Stations: they log in UTC, and host stations restart on scheduled reboots (pairing needs both restarted).
  * Not modelled yet: the RF NEO-M9N GPS. Startup does not need it; gps_time does.

  | scenario (dev boards, station f34453b) | Renode | native | checks |
  |---|---|---|---|
  | rrls_takeover (cut 200-320 s, GS->RRLS loss 0-14 s) | 7215 s (TS-takeover-70f5f97) | 38 s | 9/9 both; RRLS Abort latched 503 ms vs 507-509 ms |
  | bench_symptoms (normal, RRLS, 200 s) | 5208 s (dev-confirm) | 23 s (pace 10), 103 s (pace 2) | both checks pass |
  | rrls_relay (new; cut 120-560 s) | - | 47 s (pace 10), 294 s (pace 2) | reproduces both field reports on f34453b, all pass on a2bd517 (below) |
  | rrls_idle (600 s idle) | 16320 s (I3-idle-dev-b15b54c) | 467 s (pace 2) | 7/7 on f34453b and a2bd517 |

  Latency parity caveat: the controllers measure latencies by polling on the wall clock (0.1-0.5 s). Natively the
  resolution is that poll period times the pace: underglow confirmation p50 measured 914 ms at 10x and 409 ms at 2x,
  against 221 ms in Renode (whose 1/20 pace makes polling fine-grained). Use `--native-pace 1` or lower when a
  sub-second latency is the result.

  rrls_relay reproduces the 2026-11-17 field reports, and shows them fixed by GS a2bd517. In relay mode (only the
  primary's direct path is cut, and the primary asks for the relay), the scenario toggles the underglow 3 times from
  each station, then changes the primary's flight state (Advance, Armed, Rewind) and T clock (Launch, ResetTClock)
  and times the RRLS's view (websocket FlightState, `/api/launch_clock`). Two runs per station build, pace 2,
  `--sched '{"cut0":120,"cut1":560}'`:

  | in relay | f34453b (before) | a2bd517 (after) |
  |---|---|---|
  | boards apply every toggle (either origin) | 0.6-2.0 s | 0.6-1.9 s |
  | RRLS underglow row requested + confirmed | never within 45 s (0 of 12 toggles) | 11 of 12 at or before the primary's row; 1 at 3.9 s after the boards, the same moment as the primary's own row |
  | RRLS-proxied outcome | `unknown` in 3 of 6 ("Primary response was not confirmed"), others `accepted_by_primary` | `accepted_by_primary` in 6 of 6 |
  | RRLS flight state after the primary's change | Startup throughout (the RRLS presents its own) | 0.3-1.0 s after the primary |
  | RRLS T clock after Launch | never (run 1); 3.4 s (run 2) | 0-0.6 s |
  | RRLS T clock after ResetTClock | stays t_minus (run 2) | 0-0.6 s |

  `device_confirmed` stays false on accepted proxied toggles in both builds (the RRLS's own confirmation is the
  underglow row). While relaying, the primary's own underglow row sometimes confirms 3.9-4.7 s after the boards
  applied the toggle (3 of 24 toggles: 2 on a2bd517, 1 on f34453b), so it is not a regression of a2bd517. On
  a2bd517 the RRLS row then follows the primary's at once; that is the one RRLS row over 3 s.

  rrls_idle (harness of radio-tx-storm-20261109, `--controller .../fullnet_calib.py`, 600 s idle, pace 2) passes 7/7
  natively on both f34453b and a2bd517: RRLS capability after the restart in 8.5 s / 10.5 s, no capability gap and no
  Offline over 600 s, RRLS underglow confirmed 4-5 ms after the GS. The Renode runs of that scenario on older
  stations (b15b54c, 5216d24, 2664546) failed the capability/Offline checks; native does not reproduce those.
* Thread-level station lockstep (`--station-lockstep threads`, opt-in). simclock keeps a table of every station
  thread: running, parked without a deadline, or parked until a virtual deadline (sleep, timed futex,
  epoll/poll/ppoll; fsync/fdatasync count as parked, so the host disk does not set the pace). In this mode:
  * the station's clocks read the granted horizon;
  * the bay grants the next window only once no station thread runs and none is due (seen quiet for 30 us,
    capped at 50 ms per step);
  * the bay wakes due threads with a signal;
  * a window ends at the next station deadline plus `FIRMWARE_SIM_STATION_DEADLINE_SLACK_US` (default two quanta).

  Station compute then costs no virtual time, so the final-clock retry budget cannot be starved at any speed.
  Measured uncapped (`--native-pace 0`) on clock_steps (f34453b, host load ~20 from other work):

  | quantum / slack | wall for ~130 s virtual | speed | checks |
  |---|---|---|---|
  | 2 ms / 0 | 44 s | 2.8x | 38/38 |
  | 2 ms / 2 ms | 28 s | 4.7x | 38/38 |
  | 4 ms / 4 ms | 16 s | 8.1x | 38/38 |
  | 4 ms / 8 ms | 15 s | 9.4x | 38/38 |
  | 4 ms / 8 ms, 3 runs in parallel | 23 s each | 6x each (18x total) | 38/38 x3 |
  | 10 ms / 10 ms | - | - | 13/38 (the boards' CAN timing needs quanta of at most 4 ms) |

  rrls_takeover in this mode (4 ms / 8 ms, uncapped): 9/9 in 52 s (6.9x, two stations and 7 boards). Its Abort latch
  time reads 384-388 ms (507-509 ms at pace 10, 503 ms in Renode); at this speed the controller's wall-clock polling
  is too coarse to compare.

  The bound is now the station's own periodic work: its tokio runtime and Pico-Fi poller wake every 1 ms, and each
  wake costs about 0.25 ms of wall time on this host, so a single run tops out near 10x. More than 12x needs coarser
  station ticks (more slack, so timers fire later) or less station work per tick, not more lockstep. Throughput
  scales with parallel runs: 3 at once all passed, where uncapped runs without the lockstep failed 2 of 3. The default
  stays `--station-lockstep off` with `--native-pace 10`. For throughput, use
  `--station-lockstep threads --native-pace 0 --native-quantum-us 4000` with several runs in parallel.
  `FIRMWARE_SIM_STATION_IDLE_GRANT` (the earlier schedstat governor) is superseded.

## Station-managed bay (GroundStation firmware simulation mode)

`scripts/gs_bay.py --manifest <cache>/manifest.json --out DIR [--mode MODE]` runs the seven boards for a station that
starts the bay itself and connects to it like hardware (GroundStation26 `docs/firmware-sim.md`,
`tools/firmware_sim.py` builds the cache). Differences from the scenario engine:

* `native.json` `external_station`: the topology's host node is not started; its endpoints (the radio PTY and the
  Pico-Fi discovery port) are written to `DIR/endpoints.json`. The bay runs at pace 1 (the station keeps its own wall
  clock; no simclock, no lockstep), forever, until SIGTERM. The launcher dies with its parent (PR_SET_PDEATHSIG).
* `plant` (`src/bay/native_plant.rs`, physics in `src/bay/native_physics.rs`): stepped every 10 ms of virtual time,
  it observes the boards' output pins (MSG_GPIO edges, kept per pin) and the VB servo compare registers (read from
  the node's mapped TIM2 page) and delivers sensor codes (MSG_ADC: VB `adc3`, DAQ `mcp3564r`, FC `flightsens`), input
  pins (`pin:<index>`) and CAN link changes. `--mode test_fire` keeps the rocket on the stand; `--mode hitl|testing`
  lights the motor without an igniter.
* `control` (`DIR/control.json`, polled every 200 ms of wall time): plant inputs, conditions, calibration masses and
  faults; `status` (`DIR/status.json`, every virtual second): pace, nodes, resets, firmware revisions, plant.
* `sim_flight_sensors.c` accepts live barometer pressure and body-Z specific force through the "flightsens" device
  (the weather trim set must be selected at boot with `SIM_BARO_PA`, which the launcher sets to the pad pressure).
