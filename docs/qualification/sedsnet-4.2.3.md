# SEDSnet 4.2.3 linked qualification

Status: **qualified in the simulator**. SDK `7f1a2ad`, all seven
`ReleaseBorrowedTopology` MCU images and automatic Ethernet discovery passed
startup25, service-restart30 and the full 600-second mixed-restart soak.
The [final summary](sedsnet-4.2.3-borrowed-topology600.json) records all 20 command
ACKs (maximum 230 ms valve / 60 ms actuator), all nine managed updates within
260 ms, no power-freshness violations and no sampled allocator, panic, stack or
UART-drop faults. It includes immutable image/source/ELF and capture hashes.
GroundStation restarts at 200/400 seconds, RF/PowerBoard/FC retained-flash restart
at 400 seconds and the radio receipt outage at 150–200 seconds are included.
Existing 500 ms managed, 3000 ms power and 2500 ms ACK limits were preserved.

The frozen host runtime is GS `1b80359`. Later camera discovery address binding
in GS `6ea1a82` changes only the separate video-discovery path and passes 14 tests
plus strict Clippy on macOS and Ubuntu Pi, with independent Python interoperability
against both release binaries. Camera publishing, field radio behavior and native
phone hardware are not covered by this MCU qualification.

All eight macOS and seven Jupiter MCU factories build. Full SDK release validation
passes 541 default / 561 compact-compression tests, and the installed macOS wheel
passes 28 Python tests. GroundStation passes 305/298/306 tests for flight/HITL/test
fire on macOS and 312 on Ubuntu Pi (one hardware test ignored per mode).

The following history records rejected candidates and comparisons; their failures
are not counted as passes for the final candidate.

Status: the original SDK `986ca72` / `ReleaseCompactFair` TCP cohort passed the
complete 600-second run. Its [summary](sedsnet-4.2.3-original-tcp600.json) records
20 command ACKs (maximum 160 ms VB / 20 ms AB), managed receipts up to 300 ms,
and no sampled fault/overflow/allocator failures. This used manual Ethernet TCP
and the earlier gateway UART implementation; it does not qualify later changes.

The later packed-UART/discovery cohort failed battery freshness near 163 seconds
(3010 ms gap). A diagnostic run reproduced a current-report gap near 53 seconds:
VB transmitted at 51728.8 and 52734.7 ms, CAN handoff accepted both at 51729.4 and
52735.2 ms, but neither reached gateway UART transmission. Failed captures remain
`captures/discovery-stale-partial` and `captures/queue-budget-loss-trace-partial`.
They are failures, not qualification.

SDK `f2dd7fe` fixes sender arbitration at bounded queue limits, beyond the existing
heap admission policy. Router/Relay regressions reproduce the old slow-report
eviction and cover shared bytes, per-queue bytes, fixed slots, small canonical
bursts, protected priorities and invalid victim indices. Arena inspection uses
temporary pins; non-fair embedded builds retain their original insertion path.
All eight macOS and seven Jupiter MCU factories build; FC retains 616 bytes of
application flash on Jupiter. The frozen release dry-run passes 534 default and
554 compact/compression tests, Clippy, packages and macOS wheels. The fresh wheel
passes all 28 Python tests. GS macOS flight/HITL/test-fire tests pass 305/298/306
with one ignored hardware test per mode. Camera discovery passes 12 tests and
strict Clippy on macOS and Ubuntu Pi, including actual responder processes.

The new all-seven-board `ReleaseBoundedFair` candidate uses image
`sha256:0e4306408748cfa2aa9b368b0178021adabcd28e8998971558ee7c7881808630`,
GS `1c46eb0`, Simulator `c86f078` and SDK `f2dd7fe`. Its startup25 passed, but restart30 failed because the last GroundStation boot
could not recover fill-system identity/routes. A repeat recovered all three boots
but failed one battery freshness gap (3010 ms). Neither run sampled allocator,
panic or UART-overflow faults. The original 500 ms receipt, 3 s power freshness,
2.5 s valve ACK and zero UART-drop limits remain unchanged. The valve acquisition repair `bb0457c`
retries transient fresh conversions without reusing old values; a separate
sensor-only comparison cohort has now passed all600 stages (see below). No new release is qualified.

On 2026-10-16 the user removed I²C from deployment and qualification. That work
has stopped; retained I²C failures are not claimed fixed. The supported gateway
and client path uses the Pi main Ethernet interface. No release is qualified yet.

## Reproduced failures and repairs

- An ACK waiting in a Router queue could follow a reflected CAN packet after
  its return-cache entry was evicted. The queue now retains its selected side;
  Router and standalone Relay tests exercise receipt, cache churn and echo.
  Alternate-path learning remains possible after the retry window or side disablement.
- Managed controls lost their service priority during threaded receive and
  transport retries. Preserve control scheduling and classify only bounded,
  CRC-validated registered or frozen byte controls for protected admission. Working estimates
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
The radio model uses the 64 kb/s air link and bounded airtime budgets. PICOFI1 TCP is the supported run, with seven MCU images and a real host backend.
Legacy I2C captures below are retained diagnostic history only.

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

A later I2C long run exposed a different failure at startup: `FLIGHT_STATE`
froze both RF and gateway bridge owners, while proxy retargeting only accepted
one frozen target. The RF branch acknowledged, but the gateway branch never
resolved its downstream cohort. Capture `release423-cohort-i2c-latency-partial`
retains the 510 ms deadline failure. Its TCP sibling was stopped and retained as
superseded, not qualified. A native two-bridge reproduction fails before repair;
the expanded 64-case loss/echo/churn matrix passes after every addressed proxy
resolves its own segment. ACK owner metadata is excluded from this rewriting,
and late receipts addressed to their publisher are consumed locally.

A subsequent linked run exposed protected admission being lost during full
payload decoding. The SDK now preserves the bounded work class and full ingress
estimate; a native regression reproduces successful enqueue followed by zero
application callbacks with the old decoder.

Read-only instruction traces then showed the same 59-byte flight-state frame
attempted 1,468 times at the gateway CAN callback while all three child receipts
remained pending. Its async CAN copy used ordinary allocation admission even
though the routed frame carried protected priority. Preserve that class through
the <=128-byte CAN packet copy, including its <=160-byte owned queue object.
The gateway and actuator fixes retain conservative working estimates, 512-byte
emergency headroom and all existing count/byte limits. Bulk copies and diagnostic
exports retain the ordinary 4 KiB reserve. Native queue tests verify priority
propagation; allocator tests materialize the pressure condition and keep bulk
refusal intact. No caller-controlled large allocation gains this privilege.

Previous extended-run SDK source: `d0aaf71`; GroundStation: `70599e9`; gateway:
`5386505`/`dab737f`; actuator: `5e350fb`. The other leaf adapters are DAQ
`1336eda`, valve `6cb1be2`, power `fc04e90`, and FC `d2bfa41`. GS, RF and gateway use a 250 ms
retry interval with 17 retries, retaining the 4.5-second window. All eight
macOS factory images and the matching Jupiter images build with watchdog enabled.
Release LTO fits both gateways; FC uses a measured Rust inline threshold of 20
without removing features or changing its flash layout. Prior partial runs remain
evidence, not qualification. Both 25-second linked transport gates pass. Their first managed rounds in the
long runs complete in 100–320 ms. The extended runs were stopped and retained
as failed partials after both missed one managed receipt after service restart.
Valve/actuator command ACKs remained 10–260 ms, with no sampled allocator, CPU
or stack fault. An 18-second restart fixture reproduces priority-CAN starvation
with 2,468 bytes free: its queue copy was conservatively charged for a second
decode. The new regression fails before the copy-only estimate repair and
passes afterward; decoder estimates and emergency reserves are unchanged.

The remaining delayed owner was actuator AB (`0x313af270084cc6d9`), not DAQ
(`0xeda42b4a790192ee`). An early diagnostic mapping swapped those labels; the
packet-byte trace and a direct SDK hash calculation corrected it. Actuator
receive returned `SEDS_IO` repeatedly at 5,912 bytes free because its legacy
probe required the ordinary reserve in addition to the full decoder estimate.
All five leaf adapters now register the extended bounded control probe. Tests
compile each board's actual adapter and registration, reproduce the old
registration's refusal at that bound, materialize admitted work, and preserve
bulk refusal and emergency headroom. Both build toolchains pass, including FC.
A short service-restart fixture isolates this recovery path; the full 600-second
matrix retains MCU reboots and all original fault/freshness guards.


The supported TCP candidate then passed the 200-second GroundStation restart:
managed control rounds completed in 170–180 ms. It nevertheless failed both
Valve power freshness channels at about 180 seconds (3010 ms), with zero sampled
allocator, CPU or stack faults. Partial capture
`release423-leaf-tcp-power-stale-partial` is retained as failure. Gateway tracing
also reproduces Valve battery publication followed by ingress `SEDS_IO` at 4908
bytes free; this is not a missing Valve sensor read.

Compact-frame reconstruction applied ordinary allocation admission before the
canonical fair-reclamation path could run. SDK `0e36f5d` now applies the same
bounded dominant-sender reclamation before compact expansion in both Router and
standalone Relay. A real-allocation regression fails before repair and passes
afterward, retaining low-rate reports and all CRC/estimate/reserve checks.
Corrupt wrappers do not evict backlog. Frozen/reliable/control traffic remains
ineligible as bulk victims. All seven `ReleaseCompactFair` Jupiter images and
all eight macOS factory images build. The host image also includes GroundStation
`a8774d8` with TCP defaults. Another short/restart/full TCP run is required.


The 600,000 ms virtual-time runs retain the 500 ms managed-control deadline,
scheduled actuator/valve command confirmations, continuous valve-power freshness,
GroundStation service restarts, RF/PowerBoard/FC reboots, an RF CAN ACK outage,
allocator/error guards and sustained memory checks. Wall time is not simulated
runtime. The full report and retained-capture checks are pending.

## Completed checks

- All eight macOS TLSF + compact factory images build; all seven Jupiter
  qualification images build against the 4.2.3 candidate.
- RF: 149 native checks; each gateway: 130; DAQ: 167; actuator: 103;
  valve: 118; power: 118; FC: 157. Unique GCC LTO symbol aliases resolve to
  the actual guest counter; ambiguous aliases fail before execution.
- Simulator native tests: 96 passed; formatting and strict Clippy pass.
- SDK full release dry-run passes 528 standard and 547 compact-compression/
  fair-admission tests, plus crate packaging and the macOS Python wheel.
  Installed wheel: 28 Python tests pass.
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

## Same-board stream arbitration candidate

The previous fairness policy balanced senders but could discard a board's only
slow report behind that same board's faster message types. A native regression
failed in Router and Relay. The candidate prefers repeated message types within
the selected sender; when possible, a new value replaces that type's older value.
RX/TX slot, byte and shared-budget cases pass. Catalogs are temporary and bounded;
no per-stream reservations or public API changes are added. All eight macOS and
seven Jupiter factories build, with unchanged static RAM. FC Jupiter remains
474008/474624 bytes. Full native/package validation and a new immutable simulator
cohort are pending. This candidate has not passed a long soak or been released.

GroundStation's isolated Ubuntu Pi backend suite passes 312 tests with one
hardware-dependent test ignored. Camera discovery's 12 real UDP/TCP and CLI
checks pass on macOS and Ubuntu Pi; actual camera publishing remains a field check.

## Recovery and stream candidate `21398ce`

Stream-only SDK `e7e488b` passed the release dry-run (538 default / 558 compact
checks, packages and a macOS wheel). The additional restart repair releases the
obsolete per-link delta baseline before constructing a requested full snapshot.
Actual-allocation-ledger regressions fail before and pass after in Router and
Relay: all downstream names recover, real peak stays below the admission ceiling,
and learned routes remain unchanged. This is derived cache reclamation, not
route/schema eviction or a reduced reserve.

All eight macOS and seven Jupiter factories build the combined `21398ce` runtime;
FC Jupiter uses 474024/474624 bytes. Static board RAM is unchanged. GS Mac flight,
HITL and test-fire pass 305/298/306 tests; Ubuntu Pi passes 312, each with one
hardware-dependent test ignored. Full combined SDK/package validation is running.

The immutable image is
`sha256:82f4ad557e2ac94ffbf444a7063ecbf40f9b31264c09960a73878d61a590ebc3`,
with all seven `ReleaseStreamFair` ELFs and source hashes recorded in
`captures/release423-recovery-stream-discovery-inputs.json`. Its startup25,
restart30 and 600-second stages are running under the unchanged guards. No
combined long-soak pass or release is claimed yet.

## Captured rejection of `21398ce` and borrowed codec repair

The first startup reached its end but could not save diagnostics because a new
Docker capture bind mount was root-owned755. It was not counted as a pass. The
unchanged rerun captured a real gateway allocation panic near16 seconds. A focused
trace reproduced it at3082.88251ms, requesting44 bytes after exhausting TLSF and
all64 fallback slots. Saved RAM and its call stack identify
`build_discovery_topology` cloning `TopologyBoardNode`/`Vec<String>` during
serialization. The model subsequently recorded an IWDG reset, confirming recovery
does not prevent the communication outage. Captures remain under
`captures/recovery-stream-discovery`; this cohort is rejected.

SDK `7f1a2ad` borrows normalized graph nodes/strings and reserves the wire payload
once, encoding deltas directly without an intermediate full packet. Public
noncanonical inputs retain normalization and consume owned nodes as encoding
progresses. The regression fails before (42 allocations for4 nodes) and passes
after (4 allocations each for full/delta at4,32,128 nodes), checking unchanged
input graphs, decoded removals and canonical wire bytes. Strict Clippy and the
frozen release dry-run pass541 default /561 compact+compression checks; the
installed macOS wheel passes all28 Python tests.

All8 Mac/all7 Jupiter MCU factories build. FC uses473944/474624 bytes on Mac
and474536/474624 on instrumented Jupiter (88 bytes remaining). Static RAM is
unchanged. GS flight/HITL/test-fire tests pass305/298/306 on Mac and312 on Ubuntu
Pi, with one hardware test ignored per mode. Runtime7f is pushed to SDKdev only.

The immutable image
`sha256:c5a95e11e6959aae8bdbeb6e3efaedc5ca331aae0cabd28f9e7094d2d5880529`
uses all7 `ReleaseBorrowedTopology` images. Its startup25 passed with captured
diagnostics; restart30 also passed (all three GS boots recovered and20 ACKs
completed within10–20ms). Full600 subsequently passed; see the final summary above. Original limits are
unchanged. Inputs are frozen in
`captures/release423-borrowed-topology-discovery-inputs.json`. The complete sequence passed; earlier candidate failures remain documented.

## Sensor-only comparison completion

The [sensor comparison summary](sedsnet-4.2.3-sensor-comparison600.json) records
a separate600-second pass with SDK986, packed-UART gateway and the transient
LTC2990 repair:20 command ACKs (VB/AB maximum20ms),9 managed updates maximum250ms,
no power-freshness errors and no sampled allocation/panic/stack/FIFO faults. It
includes both GS restarts and the retained-flash MCU restart checkpoint. This
is evidence for the sensor repair and is not qualification of the later SDK.

## Release integration

[SEDSnet4.2.3](https://github.com/Rylan-Meilutis/SEDSnet/releases/tag/v4.2.3)
is published on GitHub and crates.io with main/dev pointing to the tested runtime.
The [release workflow](https://github.com/Rylan-Meilutis/SEDSnet/actions/runs/38994937379)
is green, including every platform wheel. PyPI upload was skipped successfully
because no token was configured. The initial crates upload lacked a CI token;
Cargo's existing local login published it, and the existing-version rerun passed.
Future automatic crates publication still needs a configured registry token.

Normal GroundStation registry consumption keeps `SEDSnet = "4"`: macOS305 tests
and a release build pass; Ubuntu Pi312 tests pass after fresh dependency resolution
in the isolated workspace (one hardware test ignored each). Firmware selectors
remain main. Both gateway checkouts have identical tested trees and now share
merged source history1dd5a06; this introduces no binary source change. All source
branches and the [simulator0.4.13 release](https://github.com/University-at-Buffalo-SEDS/FirmwareSimulator/releases/tag/v0.4.13)
are pushed.

Manual container publication completed on2026-10-17 after the user requested
a manual build because organization Actions is unavailable. No Actions setting
was changed. Native AMD64 on Jupiter and resource-limited native ARM64 on Ubuntu
Pi both passed the image-build tests and full packaged MCU/peripheral/watchdog
suite. The [manual image report](sedsnet-4.2.3-manual-image.json) records source
revisions, image digests, build/test log hashes and anonymous native pull/run checks.

`ghcr.io/university-at-buffalo-seds/firmwaresimulator:v0.4.13`, `latest` and all
STM32 aliases share the Linux AMD64/ARM64 manifest:

`sha256:cc62b61c395704474b9300429f052220ffc76bca9fdbabeae1e6be75ecc49e83`

The tagged simulator source is78b67a4, with GS0849e84 and registry SDK4.2.3.
The earlier ten-minute networking qualification uses GS1b80359; later camera
discovery changes are tested separately. These native image checks are not an
additional600-second soak or a physical-hardware qualification.
