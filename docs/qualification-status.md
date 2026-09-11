# Unreleased qualification work

This is a work log, not a claim of hardware qualification. At the user's request,
the current work is committed and pushed for hardware testing before the gates
below are complete. This checkpoint is not a qualified release; no new package
publication or release tag is authorized by that request.

## Current baseline (2026-09-11)

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

1. Finish `build.py test --all --release` on every board using the current image.
   Run linked tests serially when using Docker host networking.
2. Pass the complete seven-board + GroundStation 16-second linked gate before
   the 600-second linked soak. Require discovery/attribution, normal telemetry
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
