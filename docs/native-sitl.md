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

Removing the remaining limit needs the station's compute in the lockstep too: grant it virtual time only while all of
its threads are blocked (the simclock can report that through the same page from its wait wrappers), after which the
whole system is lockstep and deterministic. `--station-clock faketime` keeps the previous fixed-rate libfaketime mode.

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

* Phase 1 (done): shim library; GB, AB, VB, DAQ native builds (dev heads and cohorts); CAN with link cuts; Pico-Fi
  umbilical; native station; clock_steps and vent_countdown pass natively with parity to their Renode runs.
* Phase 2 (next): MCP3564R SPI model + EXTI data-ready, ADC fault/rail injection patterns (KG1000 rail, supply dips),
  config_resync (blank-flash boot option), launch_reset_idle, gse_flows.
* Phase 3: FC, RF, PB builds (H5/H7/U5 variants), the RFD900x air model, bench_symptoms, rrls_takeover, rrls_idle.
