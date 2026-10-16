# SEDSnet 4.2.3 linked qualification

Status: extended qualification is running. This document does not yet certify
sustained delivery or a hardware release.

## Reproduced failures and repairs

- An ACK waiting in a Router queue could follow a reflected CAN packet after
  its return-cache entry was evicted. The queue now retains its selected side;
  Router and standalone Relay tests exercise receipt, cache churn and echo.
  Alternate-path learning remains possible after the retry window or side disablement.
- Managed controls lost their service priority during threaded receive and
  transport retries. Preserve control scheduling and classify only registered,
  CRC-validated fixed-byte controls for protected admission. Working estimates
  and decoding bounds remain intact.
- RF still registered legacy memory admission, preventing small controls from
  using the existing protected headroom. Register the extended callback and use
  exact constant-time TLSF free accounting. Allocator regressions materialize
  admitted scratch, verify the complete reserve, exhaust it and verify recovery.
- Four short UART IDLE callbacks exhausted RF's four DMA-sized software slots.
  Use the same 1024-byte capacity as a byte ring. Reject complete incoming chunks
  when full; never overwrite accepted bytes.
- Normal ReceiveToIdle DMA had a stop/rearm gap on RF and both gateways. Circular
  DMA consumes HT/TC/IDLE position deltas without rearming. Tests cover wraparound,
  duplicate notifications, partial reads and restart generations.
- Pico I2C-v1/SPI staging discarded distinct queued replies by selecting the
  newest item. Stage FIFO replies. UART receive remains polled while transmitting;
  a full UART egress queue applies backpressure instead of overwriting accepted bytes.
- Simulator GPIO alternate-function reads and LTC2990 HAL RELOAD transactions
  did not reproduce populated valve hardware. Correct external-pad reads and
  split transactions; retain finite eight-byte G4 UART FIFOs and overflow checks.

## Candidate and limits

The seven-board linked network uses RF and gateway TLSF + heap packet storage;
other boards use TLSF + compact storage. Bridges enable bounded `fair-admission`;
leaf boards omit that cold path to preserve flash headroom. Watchdogs remain enabled and board-owned.
The paired gateway/Pico UART is explicitly 1,000,000 baud; RFD remains 115,200.
The radio model uses the 64 kb/s air link and bounded airtime budgets. I2C-v2 and
PICOFI1 TCP are separate runs, each with seven MCU images and a real host backend.

The first I2C long run failed the freshness guard at roughly 69 seconds. Capture
`release423-i2c-stale-partial` logs 32 mailbox evictions, including SDT templates,
and 3010 ms valve battery gaps. This is a failure, not qualification. Packed
mailbox storage and non-evicting network replies passed their burst/backpressure
regressions, but a subsequent I2C run still had a battery gap without mailbox
rejection. Read-only gateway instruction traces then showed two consecutive
battery CAN receives refused by memory admission while the valve published
successfully. Fair bulk reclamation alone did not consistently remove the gap.
The current candidate also avoids replacing optional header-compression entries
under pressure for warm routes, types and known peers; Router and standalone
Relay regressions preserve cold-work refusal. Linked qualification is ongoing.

The 600,000 ms virtual-time runs retain the 500 ms managed-control deadline,
scheduled actuator/valve command confirmations, continuous valve-power freshness,
GroundStation service restarts, RF/PowerBoard/FC reboots, an RF CAN ACK outage,
allocator/error guards and sustained memory checks. Wall time is not simulated
runtime. The full report and retained-capture checks are pending.

## Completed checks

- All eight macOS TLSF + compact factory images build; all seven Jupiter
  qualification images build against the 4.2.3 candidate.
- RF: 146 native checks; each gateway: 127; DAQ: 164; actuator: 100;
  valve: 115; power: 115; FC: 154.
- Simulator native tests: 96 passed; formatting and strict Clippy pass.
- SDK full release dry-run and compact-compression gate pass, including crate
  packaging and the macOS Python wheel. Installed wheel: 28 Python tests pass.
  Strict all-feature/all-target Clippy passes.
- GroundStation macOS backend: HITL 291, test-fire 299, flight 298 passed;
  one existing ignored test per profile. Ubuntu ARM64 backend with Raspberry Pi feature: 305 passed,
  one ignored. Final host radio scheduling regressions: 19 passed.
- Native frontend: 96 passed, seven ignored; Wasm build passes. Isolated Mac
  and Ubuntu ARM64 browser tests cover all 55 dashboard command controls,
  permission failures, reset confirmation, charts, synthetic sensors and maps.
- Both 16-second linked transports complete three managed-control rounds within
  140–240 ms, with zero sampled RF/gateway UART errors.

Physical antenna/phone sensors, RFD RF propagation, real camera/audio devices,
native iOS/Android packaging and physical output timing are not validated by this
simulation. No hardware was actuated or flashed. Bulk telemetry remains bounded
and may be shed under pressure; this is not a zero-loss claim.

## Reproducing the transport matrix

All board `sim/run_full.py` runners select I2C-v2 by default and accept
`SEDS_FIRMWARE_SIM_FILL_TRANSPORT=tcp` for the bounded PICOFI1 TCP peer. They
create an isolated comms configuration and retain the 500 ms managed-delivery
limit. Neither path uses the operator's live I2C settings or a fixed HTTP port.
Simulator 0.4.13 includes the matching bridge and sensor models; the source and
container release are pending qualification.

From a board checkout, the normal `python3 build.py test --all --ultra-soak`
flow runs the linked gate and extended fault/rejoin soak. Apply its normal
release/allocator/storage options for the desired firmware configuration. Run
once with each transport environment value; save the image digest, firmware
hashes and retained capture alongside the verdict. Candidate images can be
selected explicitly with `SEDS_FIRMWARE_SIM_IMAGE` before registry publication.
