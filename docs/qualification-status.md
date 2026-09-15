# v0.4.11 qualification status

This is a work log, not a claim of hardware qualification. The earlier checkpoint
was pushed for hardware testing before all gates were complete. The candidate
below is being released for continued testing; its final linked soak is still pending.

## Release checkpoint (2026-09-15)

- All seven board host/Python suites and GoogleTests pass after release pinning.
- SEDSNet 4.0.28 passed its full built-in test suite through the existing publishing
  script. It contains the ordered-ACK and compact discovery changes below.
- The simulator Docker Rust tests and the 16-second seven-board network gate pass
  with the persistent Pico-Fi transport, malformed-frame resynchronization,
  baud-timed Gateway UART, real FC sensors, and reset-aware progress assertions.
- The Pico-Fi pair remains running independently of GroundStation. Disconnecting
  the host does not reset its UART or device queues; three host reconnects pass
  in the Linux PTY regression.
- The latest standalone run passed 20/21 stages: RF memory profiling intermittently
  captured 19/20 monitor samples. Five diagnostic reruns captured every sample.
  This remains a test-capture issue under investigation, not a waived pass.
- The full 600-second release-pinned network run is pending. The incomplete
  restart/soak attempts below must not be reported as passes.

## Current candidate validation

- GroundStation uses the current `ef9a39d` development baseline plus the pending
  telemetry-cadence checks. SEDSNet includes unpublished ordered-ACK and compact
  discovery fixes after 4.0.27; `build.py test full` passed on Jupiter. Testing a
  patched checkout is not evidence that the published 4.0.27 contains these fixes.
- All seven boards' host suites and GoogleTests pass. All 21 standalone stages
  (boot/OTA artifacts, memory profile, and disconnected CAN for each board) pass
  with `seds-firmware-simulator:can-init-fixed-20260911`.
- The real seven-board/GroundStation 16-second network gate passes with the
  latest source snapshots: named discovery, attribution, command/ACK and
  managed-variable bounds, FC IMU and barometer 5 Hz, RF GPS 1 Hz, Power's
  five-second stream, and DAQ raw-value telemetry at 50 Hz.
- FC telemetry injection is removed. GPIO CS, configured sensor conversions,
  EXTI, packed/RX-only SPI accesses and GPDMA exercise actual acquisition.
  Firmware now retains DMA completion with a semaphore instead of losing an
  early interrupt before `tx_thread_sleep`.
- A stale DAQ polling-driver copy was detected and replaced before the final
  gate. The current interrupt driver starts HAL before enabling notifications,
  drains overflow-only events, and yields during bounded CAN TX backpressure.
  The simulator no longer accepts RX frames while CCCR.INIT is set. Tests cover
  these cases, including zero SD drops without a CAN acknowledger.
- A new ten-second DAQ SD capture contains readable `DAQ_48_000.CSV` with
  calibration metadata, 32,119 raw records (3,571.047 Hz) and 450 replay rows
  (49.961 Hz). Raw/calibrated fields parse correctly and timestamps are
  monotonic. The last portion is still buffered until flush; writer counts
  must not be mistaken for durable records. This isolated capture has no
  network time source and does not prove calibration changes/restart boundaries.
- The 600-second run reached the 400-second restart point with six periodic
  valve ACKs at 39–65 ms, but failed GroundStation radio reopen: Linux retained
  TIOCEXCL on Renode's live PTY after the host was killed. The failed run was
  stopped; it is not a soak pass. A simulator-only cleanup and Linux regression
  now clear that flag after reaping the old host. Restart and full-soak retests
  are pending. Older soaks using injected FC data or the stale DAQ driver do not
  qualify this candidate either.
- The shorter restart retest reopened the radio, but exposed a second simulator
  defect: a broken I2C reply pipe terminated the Pico-Fi bridge. Gateway then
  blocked inside Renode's UART host write and all seven virtual CPUs stopped
  advancing. A captured .NET stack identifies that blocked write. Pico-Fi now
  remains independent of GroundStation sessions, drains UART while the host is
  absent, retains bounded device queues, and reconnects after partial operations.
  Linux socket/PTY regression tests pass. Applying the real Pico-Fi newest-packet
  mailbox policy also exposed unrealistically instantaneous Gateway UART bursts;
  a baud-timed TX FIFO model and linked-network retests are in progress.

## Earlier checkpoint (2026-09-11; historical results)

- GroundStation checkpoint: `f21ce48866912f95ad200a2befbf6b48154d54a5`, including
  compile-time telemetry-rate migration and preferred-master checks.
- SEDSNet: crates.io/release `4.0.27`.
- GroundStation HITL Rust tests: 146 passed; one manual fixture excluded.
- GroundStation's isolated browser model regression: passed, including delayed
  prelaunch equipment, launch transition and missing-state behavior.
- All seven board host suites and GoogleTests passed after the profile-duration
  changes. The active Gateway checkout on Jupiter is `gateway-board26`.
- DAQ standalone firmware/bootloader/OTA execution and sampled allocator checks
  passed with SD writes and zero logging drops. The latest driver uses a real
  one-microsecond TIM2 counter and a 20 ms acquisition period.
- Simulator Docker register contracts passed for G4/H5/U5, including corrected
  GPDMA offsets, DCACHE completion and SDMMC FIFO transfers.
- DAQ's latest high-rate 16-second isolated run passed: 172,150 raw samples,
  171,738 SD records written (the remainder still queued at the snapshot),
  157 telemetry publications, zero SD errors/batch drops/allocation failures.
  Raw acquisition and SD counters advanced at every qualified observation.
- Normal ADC layouts no longer inject ADC disconnection/read failures. Explicit
  peripheral fault unit tests remain separate. The 4 MiB SD fixture exhausted
  its space during high-rate logging; normal DAQ qualification now uses 1 GiB.
- Latest DAQ host suite: 50 passed. Simulator Docker Rust suites passed, including
  rejection of stalled counters. The seven-board soak started before these
  latest DAQ/layout changes and does not qualify this checkpoint.

## Remaining gates

1. Preserve the passing board/unit/peripheral and 16-second linked checks when
   updating dependency releases. Run linked tests serially when sharing a
   GroundStation listen address under Docker host networking.
2. Finish the 600-second linked soak. Require discovery/attribution, normal telemetry
   cadence, bounded command/ACK latency, restart/rejoin and command progress
   throughout the soak, including the final interval.
3. Finish SD-content validation, not only writer counters: export/read actual
   FAT files, compare network aggregates with recorded rows, change calibration
   during queued acquisition, and check file boundaries/metadata after reset.
   Verify optional/missing-card behavior and Flight Computer logging too.
4. Finish end-to-end live OTA qualification from GroundStation through the
   routed network, including target restart and persisted setting retention.
   Standalone recovery-image tests do not replace this path.
5. Review remaining API/documentation and release requirements against the
   implementation. Run library-owned tests and the existing publishing script
   if a library changes; do not publish based only on firmware simulation.
6. Commit/release the validated simulator and any changed libraries, then update
   consuming repositories, rerun required checks, and commit/push the boards.

## Model limitations to retain in reports

Instruction execution and behavioral peripheral counters are different evidence.
The MCP3564R model currently supplies deterministic samples; full conversion
timing/configuration fidelity still needs verification. SD writes exercise FIFO
and FileX paths, not physical card wear or worst-case latency. Passing these tests
does not guarantee that hardware cannot fail.
