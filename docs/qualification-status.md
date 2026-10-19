# Qualification status

## SEDSnet 4.2.4 / simulator 0.4.14 (qualified and published, 2026-10-19)

The [network record](qualification/sedsnet-4.2.4-network.json) documents the
passing three strict starts, 120-second restart and 600-second mixed-restart
seven-board run. All 203 printed checks pass. The record includes measured acknowledgement and
managed-variable delays, stale events, allocation failures and admission refusals. DAQ and FC SD CSV counts, sequences and clocks
reconcile; accepted-but-buffered rows remain explicit. Physical 12 kHz card
throughput and power-loss durability are hardware follow-up.

Simulator 0.4.14 passes 103 Rust tests, 31 Python tests and strict lint/formatting.
The [manual image record](qualification/sedsnet-4.2.4-manual-image.json) records
passing native AMD64/ARM64 packaged checks, public publication and anonymous
pull/run checks. Version0.4.14, its source commit, latest and all four STM32 aliases
share the same two-platform manifest. The bundled backend uses registry SDK4.2.4
and committed GroundStation source; firmware keeps main and GroundStation keeps
its major-version4 dependency.

## Earlier mixed-version run (2026-10-19)

Cargo initially retained registry SDK4.2.3 in the hosted backend despite a declared
candidate path patch. That passing firmware4.2.4/host4.2.3 run is preserved in
[separate evidence](https://github.com/Rylan-Meilutis/SEDSnet/blob/main/docs/qualification-4.2.4-mixed-host423-2026-10-19.json).
It was not used as the all-current release gate. The current record above uses
verified SDK4.2.4 source in both hosted and embedded builds.

## SEDSnet 4.2.3 / simulator 0.4.13 (qualified and published, 2026-10-17)

The [qualification record](qualification/sedsnet-4.2.3.md) and retained JSON
proofs document the passing startup/restart and 600-second seven-board Ethernet
run: all20 command ACKs,9 managed updates and no power-freshness or sampled
allocator/panic/stack/UART-drop faults. I2C transport is excluded by user request.

The [manual container report](qualification/sedsnet-4.2.3-manual-image.json)
records native AMD64/ARM64 image builds and full packaged test passes, plus
anonymous registry pull and CLI checks on both hosts. Version0.4.13, latest and
all STM32 aliases are public and share the same two-platform manifest. No
organization Actions settings were changed. Physical field and native-phone
checks remain separate from this software qualification.

## SEDSnet 4.2.0 coordinated candidate (2026-10-15; linked network qualification passed)

The candidate uses canonical name-based schema identities, one reliability enum,
explicit application destinations, and fixed 32-bit C identity types. All seven
linked firmware images build with TLSF; gateway uses heap packet storage and
other boards use compact storage. The Mac GCC 13 production FC build also fits.
Board scripts select `main` by default for both stores, with compatible on-disk
fallback; GroundStation keeps the SEDSnet `4` dependency range.

The exact final candidate (`SEDSnet` commit `1b77df8`, gateway `e864d91`,
FC `0cdaac9`) passes all 65 assertions in the 32-second linked gate, capture
`bay-1792036832681388424`. Its startup application-status confirmation is 20 ms
against the unchanged 2,500 ms bound. The frozen images live in
`Release420ControlHeadroom_ARM`; the matching backend image is
`seds-firmware-simulator:release420-control-headroom`.

An earlier 600-second candidate failed after the first GroundStation restart:
only 13 of 20 scheduled VB/AB confirmations arrived. No allocator panic or
watchdog reset occurred. That failed baseline remains in
`release-420-cache-retry-soak-600.log`; the previously passed 120-second stage
did not establish longer-term qualification. The new small-control admission
class retains the full allocation estimate and contiguous-block guard while
allowing bounded byte commands/status to use the gateway's control reserve.
Bulk/schema work keeps the 4 KiB reserve. Router/Relay rejection regressions and
the actual gateway allocator's slot-rounding, exhaustion and recovery tests pass.
The final 600-second firmware execution is retained as
`bay-1792043310789324113`. All twenty scheduled VB/AB command confirmations
arrived in 10–520 ms against the unchanged 2,500 ms limit. The run includes a
50-second unacknowledged secondary CAN path, two GroundStation restarts, and
RF/PowerBoard/FC reboots. All 107 network assertions pass; none was omitted.
Allocator-failure, panic, watchdog-reset and controller CAN FIFO-loss counters
remain zero. Gateway raw CAN ring displacement counters also remain zero.
There are 17,036 ingress admission refusals and 295 cached-transit refusals;
these are not a lossless-telemetry claim. Sampled rejected headers mainly name
discovery traffic, with some telemetry/control traffic; there is no complete
per-type refusal histogram.

The original run verdict failed one valve memory-trend check. Its sampled
free-memory series was `[26480, 26468, 22044, 26192, 26420, 26404, 26392, 26316,
22492, 22492, 22076, 26460]`: the final value recovered to within 20 bytes of
start, and an earlier steady-state trough was already lower than the late trough.
The floor-only detector nevertheless reported 4,116 lost bytes after excluding
three warm-up samples. The corrected detector requires both the high and low
free-memory levels to shift downward. It retains the same 4 KiB tolerance and
separate absolute-minimum, allocation-failure and watchdog guards. The exact
trace fails against the old detector and passes after correction; an injected
sustained leak and unsafe dips/faults/incomplete captures still fail.

The shared production validators rechecked the unchanged captured observations,
including every link and host-log assertion. The report explicitly sets
`capture_recheck: true`; this was not a second firmware execution. The simulator's
87 native tests and strict lint check pass. Physical RF quality, SD-card sustained
writes, hardware maximum throughput and multi-hour hardware stability remain
outside this simulated network qualification.

macOS GCC 13 builds pass for all seven boards (TLSF, gateway heap storage,
other boards compact storage, watchdog enabled). FC uses 473,632 of 474,624
application flash bytes; Jupiter GCC 14 uses 474,504 bytes. Two unused legacy
slots and a DAQ-only calibration definition were removed from FC's boot catalog;
canonical identities and later runtime schema learning are unaffected.
All 822 board-native macOS regressions pass (DAQ 151, actuator 86, valve 100,
power 101, RF 130, FC 141, gateway 113). The natively built macOS Python wheel
passes all 26 installed-package integration tests, matching Linux. The final
release-tool regressions extend the Python suite to 28 passing tests.
GroundStation's macOS workspace tests pass: 241 backend, four downloader and
24 shared-sequence tests, with one existing ignored backend test. The web UI
and native desktop UI also compile on macOS. The SDK's full compact-compression macOS workflow passes,
including its 601-second native pressure test and embedded cross-build.

Native release gates passed for default, compact store, and compact compression,
including embedded cross-builds. Separate processes also validated runtime-only
schema merging between hosted, fileless embedded, and hosted-plus-embedded
profiles using a separately selected seed. These exercise unresolved endpoint
references, stable receipt IDs, and actual router delivery.

Two linked failures now have direct regressions:

- DAQ aborted while constructing a roughly 12 KiB compact receive ring despite
  about 35 KiB total free. Queue startup now checks admission, allocates fallibly,
  and retains a smaller fixed ring if needed. Captures
  `bay-1792014169273369741` and `bay-1792014570451010436` retain the failure;
  `bay-1792014911830835799` confirms startup allocation/panic counters remain zero
  in the bounded diagnostic. The short diagnostic is not qualification.
- Gateway command admission falsely charged the 4 KiB safety reserve as
  128-byte allocations, rounded to 256-byte fallback slots. Capture
  `bay-1792019883521873957` records reason 5, reserve bitmap `0x07ffffff`,
  2,195 requested bytes, and a 1,024-byte largest-buffer guard. The sanitizer
  regression fails before repair and passes after actual work is probed and the
  complete safety reserve is checked as remaining free bytes. Existing exhaustion,
  scattered-fragmentation, IRQ-state, integrity, and churn checks still pass.

The corrected gateway passes the 32-second linked test in
`bay-1792020585366458397`; its valve ACK arrives in 1,530 ms against the
2,500 ms bound. Three transient admission refusals remain during startup and
are recovered by reliable retries. This is not a zero-drop claim. No CAN receive
FIFO or allocator panic is hidden by extending the ACK deadline.

The frozen `Release420ControlReserve_ARM` images failed the 120-second
restart stage: capture `bay-1792022346274798526` records 20 assertions, with only
six of twenty scheduled application-status confirmations. The initial command
and forwarding-progress checks pass; the valve executes the first post-restart
failed round, but its status is delayed. No allocator panic or watchdog reset
occurs. The 600-second soak did not start.

A new Router/Relay regression reproduces reliable data retry suppression when
its return-route cache entry is evicted while RX deduplication retains the packet.
Relearning the route restarted the replay timer on every retry. Both implementations
now retain immediate retry eligibility in that case while suppressing immediate
echoes after forwarding. The repaired 120-second restart gate passes in
`bay-1792028397410598437`, including all twenty VB/AB application-status
confirmations across two GroundStation power cycles. Maximum observed command
confirmation latency is 2,020 ms against the unchanged 2,500 ms limit.
Allocator failures, panics and hardware CAN FIFO overruns remain zero.

The gateway uses TLSF, heap packet storage and cached-transit admission ON.
The normal gateway build defaults now select that same admission mode, including
when reusing a build cache. The successful restart capture records 88,546 cached
admission checks, 388 cached refusals and 6,761 CAN handoff admission refusals;
no raw CAN ring displacement/overflow occurs. These counters are not a zero-drop
claim. Reliable retries recover the control path; bulk telemetry remains bounded
and can be shed under pressure.

The same frozen firmware is running the 600-second soak. Separate installed-wheel
tests exposed and repaired a redundant Python Router wrapper mutex held across
callbacks; its timeout regression fails against the old wheel and the full Python
suite passes after repair. This Python-only change does not alter MCU images.
No public release or push has occurred yet. Hardware validation and physical SD throughput are
outside this simulator result.

## Scattered fallback preflight candidate (2026-10-13; under test)

Simulator-only gateway counters confirm UART valve-command admission refusals.
The 8-second detail capture records three refused commands, 7,300 available
bytes, 2,258 requested scratch bytes and a 1,024-byte largest-block guard.
The total plus 4,096-byte safety reserve fits available bytes; contiguous and
fragmented-scratch preflight still rejects it. Capture
`diagnostics/bay-1791943927524334002`; log `admission-detail-8.log`. Raw counters
are retained, but the diagnostic exits on the same invalid warm-up/sample-count
configuration and is not qualification. The private diagnostic now corrects
warm-up in copied layout files after topology preparation.

Gateway's fallback preflight previously required the remaining small objects
and safety headroom to fit one contiguous bitmap span. It now models each
128-byte small-object request individually, charging full fallback blocks and
padding in a private trial bitmap. Keep the largest allocation guard, 4 KiB
reserve and 128-trial cap unchanged; release all TLSF trial blocks and do not
mutate reserve ownership. A real-adapter scattered-hole regression fails before
repair, passes afterward, then allocates the entire admitted scratch/headroom
bundle and checks unchanged retained contents and allocator state. All 110
gateway tests pass, including 100,000 churn steps. Single-block bitmap searches
use bounded bit scans. Firmware builds at 479,416 / 491,008 flash and
114,664 / 114,688 RAM (simulator diagnostics included).

Frozen `ScatteredReserve_ARM` artifacts enter the short/restart/600-second
pipeline. Packet-loss elimination remains unqualified.

The preceding compact-control 32-second gate passes, but the 120-second restart
gate fails 27 network assertions: first host restart takes 11,260 ms against a
10,000 ms discovery bound and the scheduled post-restart VB command times out.
Only four of twenty scheduled commands are acknowledged; all three managed
rounds before restart meet the 1,000 ms observation window. Capture
`diagnostics/bay-1791943343702267988`; logs `compact-control-32.log` and
`compact-control-restart-120.log`. The 600-second qualification does not start.

## Compact control admission candidate (2026-10-13; under test)

The three-destination candidate passes both full ten-gate suites and builds
all seven firmware images, but its new linked 32-second gate fails two valve
acknowledgement assertions. The initial command needs seven publisher retries
before its receipt arrives; the managed-state checks pass. There are 1,582 CAN
admission refusals, no warmed-transit refusals and no allocator/FIFO/ring/queue
overflow. Capture `diagnostics/bay-1791939610727131781`; log
`three-destination-32.log`. Restart and 600-second qualification do not start.

A separate source/regression check identifies a compact admission gap: raw
and full-template protected ingress reclaim queued bulk on denied headroom;
compact reconstruction did not. Both Router and Relay now use the same
bulk-only policy for a high-priority retained template after wrapper CRC
validation. Raw/compact regressions fail before this change and pass afterward;
CRC-invalid compact frames preserve queued traffic. This addresses a verified
pressure case but does not establish that it caused the initial valve failure.
Full suites and all seven factory builds are running again.

The earlier ACK-stage 30-second diagnostic captures counter values but exits
with an invalid warm-up/sample-count configuration (3 warm-up samples / 3
samples), so it is not qualification. Capture
`diagnostics/bay-1791938955803169712`; log `ack-stage-diagnostic-30.log`.
Observed UART ACKs queued/sent are 15, 17, 19; CAN ACKs received 10, 12, 14;
UART failures zero. All three managed rounds complete within 1,000 ms and the
4,000 ms delay is not reproduced. The private runner's warm-up is corrected.

## CPU profile capture (bounded instruction trace)

For a short linked diagnostic, set `execution.trace` to `true` only in the
selected node layout. Bay captures binary program counters for at most 20 ms
of guest execution after the first checkpoint (or during the sole interval of
a one-sample run). It disables tracing before the rest of that interval and
preserves the total virtual run duration. Diagnostics retain
`profile-<node>.trace`, capped at 64 MiB per artifact. Aggregate PCs against the
matching ELF to measure function instruction costs without retaining stacks.
Keep tracing disabled for normal latency/soak qualification. See
[Renode binary execution tracing](https://renode.readthedocs.io/en/latest/execution-tracing/execution-tracing.html).

A collapsed-stack Cortex-M smoke check worked, but the real ThreadX gateway
capture grew to 32 GiB by its first checkpoint. That diagnostic was stopped;
only an 8 MiB prefix is retained, and it is not delivery qualification or a
valid steady-state CPU result. Log `gateway-cpu-profile-32.log`; prefix
`diagnostics/guest-profile-prefix.collapsed`. The replacement flat-PC window
avoids synthetic stack growth. Its native regression verifies the 20 ms cap,
trace shutdown before continuation, and unchanged run duration even for a
600-second interval. The bounded capture completed: 10,000,010 bytes,
2,000,000 gateway instructions, and 15,605 distinct PCs. Matching ELF
attribution includes zero-sized assembly symbols: ThreadX `__tx_ts_wait`
accounts for 29.49%, ULEB128 decode 5.237%, message metadata lookup 5.05%,
and repeated immutable schema accounting 2.244%. This is one 20 ms window,
not a steady-state saturation measurement. Capture
`diagnostics/bay-1791928724845538542`; aggregate
`bounded-gateway-cpu-complete.json` on Jupiter.

The profiled diagnostic passed its 32-second assertions, but tracing is not
latency qualification. Hosted receipts located the delayed `FLIGHT_STATE`
confirmation on the gateway/fill-system segment: RF's receipt was immediate,
while gateway's required retries and arrived after 1,510 ms. Normal-run delay
had previously exceeded the 2,500 ms gate; that failure remains unresolved.

Hosted receipt diagnostics use `SEDSNET_TRACE_RELIABLE=1` to log registration,
replay and matched receipts with packet IDs and pending destination hashes.
That trace is disabled by default and entirely compiled out of embedded builds.
It identifies which destination or packet delays the global ACK wait.

## Embedded metadata CPU candidate (2026-10-13; under test)

Replace repeated immutable schema byte counting with compile-time accounting
and generated-ID table lookup. Preserve runtime-overlay-first lookup and
reserved endpoint precedence (200–202); built-in type IDs use 0–18.
The embedded regression checks all compiled IDs, unknown-ID rejection,
unchanged byte-budget accounting, and known-schema reception under pressure.
Both local embedded tests and both full ten-gate suites pass (484 default /
498 compact native tests, one optional skip each). All seven factory images
build; gateway firmware is 479,288 / 491,008 bytes and FC is 471,576 / 474,624.

The normal 32-second `MetadataCpu_ARM` linked gate passes. All three managed
rounds complete within 1,000 ms against the unchanged 2,500 ms bound. Capture
`diagnostics/bay-1791931906517849568`; log `metadata-cpu-32.log`. No allocator,
enqueue, ring or CAN-FIFO failure is observed; CAN admissions still fail 1,555
times, so packet-loss elimination is not claimed.

The subsequent 120-second restart gate fails 45 network assertions. Initial
valve application acknowledgement is absent, and the second managed round
takes 4,000 ms; the gateway confirms that state was applied, but its receipt
requires eight publisher retries. RF receipts arrive promptly. The boards
continue running without allocator/FIFO/ring/enqueue failure, while gateway
CAN admission refusals reach 8,025. Capture
`diagnostics/bay-1791933201228011785`; log `metadata-cpu-restart-120.log`.
No 600-second qualification starts. A separate early-window diagnostic adds
aliases for the gateway's existing protocol-ACK stage counters, without
changing firmware RAM, to localize the lost confirmation.

Last-rejected headers include actuator pressure, three-destination heartbeat,
and discovery frames. The expanded native forwarding measurement uses three
allocations / 120 bytes per packet, largest 64 bytes, for both two and three
destinations. This alone does not qualify smaller asynchronous admission;
the experimental warmed-transit guard remains limited to two destinations.

## DAQ scheduling candidate (2026-10-13; under test)

The moved-update 120-second run fails the DAQ zero-overrun threshold once: a
three-tick raw-drain/enqueue stage exceeds the two-tick acquisition budget at
sample 38,014. Capture `diagnostics/bay-1791921182875757282`; log
`moved-updates-restart-120.log`. This remains a failed gate; the 600-second soak
does not start. Gateway has no allocator failure, enqueue loss, ring loss or
CAN-FIFO loss. It still refuses 5,975 CAN admissions, with a 55,156-byte peak.

All ten scheduled valve and ten actuator commands are explicitly acknowledged,
and named discovery recovers after both host restarts. Steady ACK latency is
10–30 ms; the combined-restart valve round takes 2,020 ms (bound 2,500 ms).
These observed command results do not waive the DAQ failure or replace the full
assertion gate, which stops on the probe threshold before later assertions.

DAQ telemetry was configured at acquisition priority 5 with no time slice,
allowing network work to prevent immediate acquisition resumption. It now uses
the existing I/O priority 6 and one-tick slice, below acquisition priority 5.
The native scheduling regression fails with the previous creation parameters
and passes with the repair; all 148 DAQ native tests pass, and its matching
factory image builds. Frozen `DaqScheduling_ARM` artifacts are in a new
32-second / 120-second / 600-second pipeline. No sustained qualification or
packet-loss elimination is claimed.

The subsequent scheduling 32-second gate has zero acquisition overruns, but
fails nine final managed-state observations. The first true update completed
in 1,000 ms; the second false update took 4,000 ms against a 2,500 ms delivery
bound, preventing the final true round. Capture
`diagnostics/bay-1791922017618793067`; log `daq-scheduling-linked-32.log`. Gateway
CAN admission refusals are 1,822; allocator peak 55,040 bytes, with no allocator,
enqueue, ring or FIFO failure. This remains a failed gate, and neither restart
qualification nor the long soak starts.

## Latest topology-update candidate (2026-10-13; qualification pending)

Router and Relay now compare normalized incoming topology nodes directly and
move replacement buffers instead of copying the retained graph for change
detection. The negative regression makes 19,000 allocations for 1,000 unchanged
updates; the repair makes zero. All 28 memory-pressure tests pass locally, all
seven matching firmware images build, and both full ten-gate suites pass (484 default / 498 compact native tests;
one optional skip each). Sustained/restart qualification remains pending.

A standalone release host benchmark, with no counting allocator, measures
unchanged two-node full/delta updates over 24 rounds of 10,000 operations,
alternating before/after order. Median cost falls from 390 ns to 118 ns; ranges
are 381–665 ns and 115–248 ns. This measures that update algorithm, not MCU clock
capacity or total packet-processing throughput. Input decoding/preparation is
outside the timed region; both variants consume the same owned input. Evidence:
`/tmp/network-topology-cpu/src/main.rs` and `/tmp/network-topology-cpu.jsonl` on the
local workstation.

Simulator-only allocation-failure capture now retains 128 stack words to identify callers beyond nested graph clones.

The matching moved-update 32-second linked gate passes all delivery/ACK checks,
with no allocator failure or watchdog reset. Capture
`diagnostics/bay-1791919562674926770`. CAN admission refusals remain 2,253, with
zero enqueue/ring/FIFO losses; allocator peak is 59,080 bytes. Queue-service
elapsed time is 14,167 ms and CAN service 2,646 ms. Work and startup timing differ
from the failed run, so these are not a normalized CPU before/after comparison.
The 120-second restart gate failed the DAQ deadline threshold (see above), so
the 600-second soak did not start. No packet-loss elimination or sustained qualification is claimed.

The preceding summary-reuse candidate also has a completed short run with two
failed valve ACK/latency assertions, despite no allocator failure: capture
`diagnostics/bay-1791918321007913255`, log `reused-summary-stack-32.log`. It records
1,969 CAN admission refusals, zero enqueue/ring/FIFO losses, and a 54,848-byte
allocator peak. Queue service takes 13,668 ms and CAN service 2,875 ms across
32 simulated seconds; these timings include preemption and are not pure CPU
measurements. A later diagnostic repeat was interrupted by a Jupiter host
reboot and is not qualification evidence. No successful long soak is claimed.

## Gateway allocation investigation (2026-10-13; qualification in progress)

Exact TLSF free-block accounting, a larger contiguous fallback carved from the
same pool, and a coalesced CAN activity wakeup reduce unnecessary admission
refusals. Separate matching short comparisons still have 5,152, 3,005 and 1,999
CAN-to-network admission refusals respectively; enqueue refusals remain zero.
These are stage counters, not a claim of loss-free forwarding. Physical CAN
controller/ring drops remain separately measured.

An optional cached-transit admission probe is disabled by default. A candidate
failed startup with a ten-byte allocator request, leaving no free memory.
The matching allocation-failure PSP capture identifies a cloned iterator in
`discovery::merge_topology_boards`, which duplicated the complete retained
board list into a temporary tree with duplicate sender strings. Capture:
`diagnostics/bay-1791860066821575768`; log: `cached-oom-linked-16.log`.
A four-second diagnostic was too short for PowerBoard's watchdog-feed threshold;
it did not reproduce gateway OOM and is not a qualification result.

Topology merging now reuses retained nodes and avoids duplicate strings/items.
The negative regression records 29,000 allocations for 1,000 unchanged merges;
the repair records zero and preserves duplicate-ID, union, and normalization
semantics. All sixteen memory-pressure tests and default/compact ten-gate
library checks pass. All seven matching firmware images build; gateway uses
114,256/114,688 SRAM bytes and 471,524/491,008 application flash bytes in the
instrumented image. The matching linked gate and restart/long-soak checks are
still required before promoting cached admission or claiming the loss is fixed.

## Discovery allocation profiling (2026-10-13; not qualified)

Unique-union scratch accounting passes its overlapping-catalog regression and
all seven builds, but its restart run exhausts the allocator during a string
vector clone. Capture `diagnostics/bay-1791914668567821314`.
Splitting subscription refresh admission from full schema work also passes
unit tests, but the matching restart run still fails. Its deeper stack identifies
`discovery::summarize_topology_boards` growing an endpoint vector while repeatedly
copying the same fields. Capture `diagnostics/bay-1791915985658461660`.

The repair collects unique endpoints and time sources before allocating, and
reuses the existing outbound topology for both summaries in Router and Relay.
The repeated-summary allocation regression fails before repair and passes
afterward. Both complete ten-gate suites pass (483 default / 497 compact native
tests; one optional skip each), and all seven firmware images fit.

However, the matching `reused-summary` 32-second linked test exhausts memory
during another `TopologyBoardNode` clone, then watchdog-resets. Live allocation
reaches 60,044 bytes. Capture `diagnostics/bay-1791916946738691068`; log
`reused-summary-linked-32.log`. The sequential restart/600-second soak pipeline
stops at this failure. A deeper caller capture is in progress; this candidate
has not qualified for sustained recovery or loss-free delivery.

Scalar forwarding allocation costs remain three allocations / 120 bytes per
packet versus nine / approximately 204 bytes before the repair. Pure host CPU
timing medians overlap (2.140 versus 2.151 microseconds); no CPU speedup or MCU
capacity conclusion follows from those measurements. Firmware service-phase
timings include preemption and are assessed alongside delivered packets,
admission refusals and command ACK latency.

## Latest recovery qualification (2026-10-13; incomplete)

Repeating the protected-ingress 120-second test with the corrected assertion
window reproduces 40 failures: only the initial named discovery/attribution and
four of twenty scheduled command acknowledgements occur. Capture
`diagnostics/bay-1791910252466319714`; log
`control-ingress-window-restart-120.log`. The prior all-ACK trace is genuine but
not deterministic qualification. The 600-second soak did not start.

The capture records allocator live bytes of 45,588–49,540, peak 55,988. Snapshot
admission still asks for 8,758–9,082 additional bytes with a 2,048-byte largest
block. The 4 KiB safety reserve makes that request inadmissible despite roughly
10 KiB free. Unknown-master election charged several copies of the global graph
although election and per-side snapshot construction are sequential phases.
The candidate now uses the maximum of their independent work estimates, retaining
a global election and a split-horizon response. The strengthened unknown-master
pressure regression fails with the previous estimate and passes after repair.

All 23 pressure tests, all seven factory builds, and both ten-gate suites pass
(479 default / 493 compact native tests, one optional skip each). Matching image
`seds-firmware-simulator:phase-scratch-20261013` and immutable `PhaseScratch_ARM`
artifacts are in the sequential 32-second / 120-second / 600-second pipeline.
No successful sustained qualification or packet-loss elimination is claimed.

A matching protected-ingress gateway comparison with cached admission disabled
passes its 32-second linked gate; capture `diagnostics/bay-1791910826151286887`.
CAN admission refusals are 3,468 versus 1,878 in the enabled short candidate;
queue/ring/FIFO losses remain zero. Peak allocation is 57,704 bytes. This measures
the default path, but it does not qualify it for restart recovery or a long run.

## Protected ingress and restart-window validation (2026-10-13; soak pending)

The split-horizon short gate fails two valve application-ACK assertions; capture
`diagnostics/bay-1791867991535785017`. Valve firmware receives and executes the
command, with eleven open-status callback attempts and no CAN send failures;
GroundStation records no open confirmation. Incoming status can be refused by
allocator admission before queue priority becomes effective.

Router and Relay now reclaim queued bulk payloads when a CRC-valid high-priority
frame fails admission. Existing controls and route/schema state remain protected;
normal successful admission does not add a priority parse. Canonical and full
header-bootstrap paths share the policy. Both new status-ingress tests fail before
the repair and pass afterward, including corrupt-frame non-eviction. All 22
pressure tests, default/compact ten-gate suites and seven factory builds pass.

The matching 32-second gate passes all assertions; capture
`diagnostics/bay-1791869568966877348`, image
`seds-firmware-simulator:control-ingress-20261013`, frozen `ControlIngress_ARM`.
CAN admission refusals are 1,878 versus 2,255 in the preceding candidate. Queue,
CAN-ring and hardware-FIFO losses remain zero; allocator peak is 54,628 bytes.
Valve's initial application ACK takes 1,520 ms against the 2,500 ms bound.

The matching 120-second run reports one failed per-interval command counter,
compared with 29 failed recovery assertions previously. Capture
`diagnostics/bay-1791870862283508152`. All ten scheduled valve rounds and ten
actuator rounds are explicitly acknowledged. Steady-state round latency is
10–30 ms; a restart valve round takes 520 ms. Named discovery and source
attribution recover after both GroundStation restarts. The failing counter window
is the first interval after the deliberate RF/FC/PowerBoard+GS restart; the host
waits for all named peers before issuing its scheduled command, then catches up
in the next interval. No missing command/ACK is shown by that trace.

The framework now permits one extra snapshot interval only immediately after a
configured host restart, retaining every scheduled command, per-command latency,
all ACK-count requirements, and strict steady-state progress. Four tests check
index conversion, normal-window strictness, the captured case, detection of a
genuine recovery stall, and the final-snapshot bound. The previous run remains
recorded as failed. A new matching 120-second run is in progress, followed by a
600-second soak only if it passes. No release or cached-admission promotion yet.

## Current profile and restart candidate (2026-10-13; not qualified)

The borrowed-topology linked gate passes but records 1,688 CAN admission refusals
(enqueue/ring/FIFO failures zero). Capture `diagnostics/bay-1791865768306801816`.
Its 120-second restart test fails 29 assertions, with no named AB/DAQ/VB recovery
after the first GroundStation restart. Capture
`diagnostics/bay-1791867240098547857`; log `topology-borrowed-restart-120.log`.
The gateway still advertises endpoint reachability, but no downstream board
names. This confirms summary-only recovery is insufficient for attribution.

The latest profile repair removes empty target arrays, endpoint scoring copies,
and owned schema definitions from ID-only lookup. Release allocation profiles
with compiled main/IPC schemas measure three allocations totaling 120 bytes per
one- or two-endpoint scalar frame (largest 64 bytes). Hosted and actual no_std
named-lookup regressions measure zero allocations. Restoring the previous target
path fails both forwarding allocation regressions. Twenty native pressure tests,
default/compact ten-gate validation, and all seven image builds pass.

With allocation counting disabled, twenty host scalar timings have medians of
2.140 microseconds before and 2.151 after; ranges overlap. No host CPU speedup is
claimed. The allocation-counted timings include instrumentation and cannot be
used as pure CPU comparisons. Evidence: `network-cpu-compare.json` and
`id-lookup-cpu-memory.log`. The matching 32-second gate passes; capture
`diagnostics/bay-1791866871324250225`. Queue-service elapsed time falls from
14,672 to 12,812 ms, but CAN admission refusals rise from 1,688 to 2,829. Zero
queue/ring/FIFO errors and zero allocator failures do not mean loss-free service.
Live allocation bytes are 48,232–49,492; peak 55,912. More service progress is not
a substitute for delivery qualification.

A new recovery candidate aligns scratch estimation with the actual split-horizon
response. Router excludes the requesting side when a fresh preferred master is
already present; unknown master election retains all-side headroom. Relay always
excludes the requesting side. Both regressions fail with a larger excluded
requester graph before the change, and recover all AB/DAQ/GB/VB names afterward.
All twenty pressure tests, default/compact ten-gate suites and seven image builds
pass. Matching image `seds-firmware-simulator:split-horizon-20261013` and frozen
`SplitHorizon_ARM` artifacts are under sequential qualification: 32-second linked
gate, then 120-second restart test, then 600-second soak only if both pass.
Do not publish the candidate or promote optional cached admission yet.

## Reliability migration and recovery profiling (2026-10-13; not yet qualified)

Schemas now require one `reliable` value: `Off`, `Ordered`, or `Unordered`.
Boolean reliability and `reliable_mode` are rejected, with no compatibility shim.
All seven board configurations, GroundStation, code generation, runtime loading,
editor, fixtures, and schema contracts are migrated together. Binary discovery
encoding remains unchanged. All 795 board-native checks and six GroundStation
schema checks pass.

The topology-in-place 32-second linked gate passes but still records 2,650 CAN
admission refusals (enqueue failures zero). Its 120-second restart run fails 29
assertions after GroundStation restart: AB, DAQ, GB and VB disappear while boards
remain alive and the gateway publishes an unnamed empty pressure keepalive.
Capture: `diagnostics/bay-1791862158935557580`. This is not a successful soak.

A later single-reliability 32-second gate passes with 2,029 CAN admission refusals
and zero enqueue failures; capture `diagnostics/bay-1791862114746215710`.
Sampled refusals include two-endpoint tank-pressure frames and metadata. The
optional warmed transit classification now includes two endpoints. Empty
requests no longer require full-schema scratch; topology-only response estimates
use retained graph size. Router and Relay regressions pass and independently
fail when the old 8 KiB request guard is restored.

The newest borrowed-topology candidate builds all seven factory images and
passes default/compact ten-gate library validation (475 default native tests).
It removes a second graph copy from advertisements and avoids constructing a
graph before checking a present preferred master. Its matching linked gate is
running; restart and long-soak qualification remain pending.

Release host profiles on Jupiter measure nine allocations per scalar transit:
204 bytes total for one endpoint and 208 bytes for two, with a largest allocation
of 64 bytes. Elapsed host times are approximately 1.96 and 2.35 microseconds per
packet, respectively. These are comparative host timings, not an MCU rate claim.
Unchanged two-node topology merges allocate zero times and measure about 39 ns
per merge. Logs: `network-cpu-memory-profile-valid.log`,
`topology-request-router-negative.log`, `topology-request-relay-negative.log`.
Gateway millisecond service probes separate UART, CAN handoff, queues, discovery,
time sync, OTA and waits; their elapsed times include preemption and are not
instruction-level CPU utilization. In the prior single-reliability gate, queue
service accumulated 14,274 ms versus 2,629 ms in CAN handoff over 32 seconds.

## Current linked gate and long soak (2026-10-12)

The matching 32-second linked gate passes with all seven simulator-instrumented
factory images and the current GroundStation/SEDSnet candidate. Capture:
`diagnostics/bay-1791848384711480875`; image:
`seds-firmware-simulator:template-index-20261012`. No linked assertion is omitted.
All seven builds use TLSF with heap packet storage. Compact storage passes the
library suites separately; this run does not qualify compact firmware.

The gate covers autonomous discovery, board attribution, managed-variable
updates, routed commands and application status acknowledgements, FC IMU and
barometer delivery, DAQ's configured 250 Hz cadence, and watchdog progress.
All 787 board native checks pass (146 DAQ, 126 RF, 97 power, 136 FC, 104 gateway,
82 actuator, 96 valve). Default/compact library suites pass all ten gates with
462/476 native tests respectively. The simulator's 86 native tests also pass.

Physical CAN sender-ring and controller-FIFO drops stay zero, but gateway
CAN-to-network handoff refusals reach 3,535 and memory-admission refusal events
reach 5,071. These counters are different stages, not additive packet-loss
counts. Allocator live bytes range from 45,572 to 52,752 in the eight snapshots.
The short gate proves delivery progress, not loss-free streaming.

The matching 600-second soak was stopped at 242.94 seconds after reproducing
failed recovery following the GroundStation restart at 200 seconds. RF, FC and
PowerBoard telemetry continued, but AB, DAQ, GB and VB routes never recovered.
The gateway advertised only an unnamed, empty pressure keepalive. Capture:
`diagnostics/template-index-restart-failure`. The first two command rounds
passed (30/20 ms and 20/20 ms valve/actuator response latency). This is a failed
restart qualification, not a successful long soak.

A scheduling-only comparison passes the short delivery assertions but increases
CAN-to-network admission refusals from 3,535 to 7,106. Physical ring/FIFO losses
and allocator failures remain zero. It is not qualified as a throughput fix.
Capture: `diagnostics/bay-1791850514610913825`.

New candidates remove the unused full-template wrapper on shared CAN links and
allow unchanged known peers to refresh their boot incarnation under bounded
memory pressure. Four router/relay canonical-frame regressions pass. The
known-peer restart regression fails with the old birth-time check and passes
with the candidate; changed ownership still requires full admission. Both default and compact ten-gate library validations pass (467/481
native tests; one optional skip each), and all seven matching firmware images
build. The extended restart regression also verifies that a contender retains
normal collision arbitration. Removing the transport fast path makes all four
new wrapper regressions fail.

The combined 32-second linked gate passes without omitted assertions:
`diagnostics/bay-1791852381092288200`, image
`seds-firmware-simulator:untemplated-restart-20261013`; 73 build artifacts are
preserved under `snapshots/untemplated-restart-20261013`. Handoff refusals reach
5,158 and memory-admission refusals 6,648; live bytes range 46,200–52,360 in the
reported snapshots. This improves the scheduling-only comparison but remains
worse than the earlier baseline; no loss-free throughput claim is made. The
120-second restart soak failed at 56.22 seconds after GroundStation restarted
at 40 seconds: AB, DAQ and VB remained missing, while only GB recovered on
the fill link. Capture: `diagnostics/untemplated-restart-failure`.
Restart recovery remains unqualified.

The private relay-eviction reversion fails exactly the new hash/ID eviction
regression; the other three cache tests still pass
(`template-eviction-negative.log`).

## Packet minimization and radio startup receipts (2026-10-12; linked qualification blocked)

Best-effort broadcasts to a sole discovered peer previously acquired frozen
receipt contracts and retained retransmission records. The captured six-float
ASCENT_STATE frame was 87 bytes; the ordinary encoding is 42 bytes and fits one
CAN-FD frame. Best-effort traffic now avoids that contract while reliable types,
managed variables and explicitly frozen contracts retain their guarantees.
Both packet-store regressions send 64 packets without receipts, retain zero
pending deliveries, and require wire size to equal the plain frame. The private
reversion fails on the first retained packet. Idle receipt maintenance also
measures zero allocations over 100 passes versus 300 before its repair.

The matching 32-second linked diagnostic still failed on one FC queue error,
with zero gateway ring loss, network handoff failures and controller FIFO loss.
Capture: `diagnostics/bay-1791837820005716083`. A separate first-failure capture
(`bay-1791833759309433654`) identifies WARNING (type 114), payload
`SD unavailable; flight/network continue`, ID `0x5bd205a7419a6244`. FC expires
it at 2392 ms; the correct GS receipt arrives at 2820 ms, after removal.
This is a measured startup latency failure, not a packet-identity mismatch.

FC and RF now use 24 retries at the existing 200 ms interval, retaining the
existing queue and pending-message capacities. The generic library default
remains eight retries. An isolated native regression holds a real receiver
receipt until 2820 ms: eight retries reproduce the exact 2392 ms expiry;
24 retries accept it without replay or increased retained capacity. All seven
factory images build. Fresh default and compact ten-gate suites pass.
The updated linked diagnostic (`bay-1791841870516146337`) clears FC receipt
exhaustion and gateway ring, handoff and controller-FIFO loss, but fails 26
later delivery assertions. Fill-side telemetry and command delivery remain
absent. No long soak has started or been qualified for this source snapshot. Missing diagnostic instrumentation is recorded in
`diagnostic-bay-busy-fair/omitted-instrumentation.json`; this production-image
run cannot replace all simulator-instrumented qualification assertions.

## Canonical endpoint identity and embedded storage (2026-10-11; linked diagnosis pending)

The new no_std allocation test exposed a temporary sorting Vec for unsorted
compiled schema destinations and a separate packet identity mismatch between
local declaration order and the canonical wire endpoint set. Packet IDs now
normalize endpoint order and duplicates without allocation. Unsorted schema
decoding uses a bounded bitmap and allocates only final endpoint storage. The
embedded small-packet decode measures two allocations, versus three before this
step. Hosted order/duplicate identity tests and embedded round trips pass;
private reversions reproduce both defects. All seven factory images build.
Fresh default/compact ten-gate suites pass (455/469 native tests, one optional
skip each), and the matching GroundStation image builds. The 32-second linked
gate fails on 110 gateway startup ring losses, flat across all eight samples.
FC queue errors, controller FIFO losses and gateway handoff failures remain zero.
Capture: `diagnostics/bay-1791766344384519441`; artifacts:
`snapshots/canonical-endpoints-20261011`. The strict failure prevents claiming
command/ACK or long-soak qualification.

A private gateway comparison with 192 ring slots and a 65,408-byte application
pool eliminates ring loss, but returns seven startup network-handoff busy errors
and one FC queue error. Allocator peak is 42,656 bytes in a 61,112-byte region;
memory-admission drops remain zero. This comparison is not promoted or qualified.
Capture: `diagnostics/bay-1791821329870569666`. An attempted 24 KiB queue
comparison retained the effective 16 KiB setting because CMake forces it; it
is not valid evidence about changing capacity. Second capture:
`diagnostics/bay-1791822046344394556`; allocator peak 41,352 bytes.

Private router-level diagnostics observe CRC errors on mixed CAN/UART input,
but the first CAN callback failure is SEDS_IO on a 61-byte packet whose complete
captured bytes pass an independent IEEE CRC check. Thus the mixed last-error
field must not be used to classify all seven CAN failures as CRC corruption.
An effective 24 KiB budget / 2 KiB fixed-queue diagnostic clears gateway ring
loss and handoff refusals; repeating with the original 16 KiB budget also
clears both. This isolates fixed slot capacity as the useful queue adjustment.

The production-code gateway comparison (192 ring slots, 65,408-byte application
pool, 2 KiB initial fixed queues, original 16 KiB queue budget) completes all
eight samples with zero ring drops and handoff failures. Allocator peak is
47,272 bytes in a 61,112-byte region. The linked gate still fails on two FC
queue errors, so command/ACK qualification remains blocked. Capture:
`diagnostics/bay-1791826372539849959`. A sanitizer regression checks startup
saturation, eight protected control slots, 1,000 wraps and IRQ-mask restoration
at both 48 and 192 ring depths. Those gateway capacities are now implemented
with the original 16 KiB network queue budget; they are not yet qualified for
release. Rich-error probes identify the FC failure as an ORDERED_MESSAGE
(type 122) exhausting end-to-end retries for GroundStation at about 2.4–2.6
seconds. A fixed-size, simulator-only receipt history records no matching ACK
before expiry. Capture: `diagnostics/bay-1791830970200076149`. Receipt tracing
continues; no long soak has started.


## Empty metadata and direct target storage (2026-10-11; validation in progress)

Small scalar decoding with a cached host sender now makes zero allocations;
explicit endpoint subsets and frozen target lists allocate only their final Arc.
Malformed target counts are rejected before any allocation. Wire bytes, packet
layout and packet identity are preserved. The ten focused memory-pressure tests
pass. Private reversions reproduce all three extra-allocation failures. The
default and compact ten-gate suites pass (454/468 native tests, one optional
skip each), all seven factory images build, and the matching GroundStation image
builds. The 32-second linked gate fails on one FC queue error. FC endpoint-failure
fields remain zero. Gateway startup admission losses remain 110 (flat across all
eight samples); handoff and controller FIFO losses remain zero. Capture:
`diagnostics/bay-1791764935206378156`; artifacts: `snapshots/target-storage-20261011`.
The early FC failure assertion prevents claiming subsequent network qualification.
No long soak has started or been qualified for this source snapshot.

## Endpoint storage and CAN-first burst service (2026-10-11; startup gate failed)

Decoding now retains validated sorted host schema endpoints directly, copies
static embedded schema endpoints directly into final shared storage, and writes
explicit endpoint bitmaps into their final Arc. Packet layout and encoded bytes
are unchanged. Cached-sender host scalar decoding measures one allocation,
versus three before this step and six in the original decoder; an explicit
subset bitmap measures two instead of three. Private reversions reproduce both
allocation regressions. Default and compact routes pass all ten gates (453/467
native tests, one optional skip each). All seven factory images build and their
native suites pass (766 checks, two skips).

Gateway's priority-eviction ring consumer now protects its slot copy and tail
update against interrupts. Its FIFO IRQ work is bounded to three frames. The
worker also drains an existing CAN backlog before starting potentially expensive
queued TX encoding; bounded combined RX/TX service still runs under permanent
load. Its executable burst regression includes the observed six-millisecond TX
overrun. FC retains the actual endpoint failure type/result separately from its
queue error counter so the timing-dependent startup handler failure can be
identified. A shortened diagnostic has no FC handler errors but cannot qualify
startup or watchdog progress; it fails on the shortened watchdog-feed window.

The matching GroundStation image builds. Checksummed artifacts are retained at
`snapshots/endpoint-storage-20261011`. The 32-second linked gate fails on 110
gateway software-ring admission drops during startup (flat across eight samples).
Controller FIFO losses, FC queue errors and CAN-to-network handoff failures are
zero across those samples. Capture: `diagnostics/bay-1791763464760909583`.
The early loss assertion prevents claiming subsequent command/ACK qualification.
No long soak has started or been qualified for this candidate.

## Bounded receive IRQ draining (2026-10-11; FIFO repaired, startup gate failed)

RF, FC and PowerBoard previously only set receive flags in the IRQ, leaving
three-slot hardware FIFOs for the worker to drain. Actuator and valve defaulted
to polling. All five now copy at most three frames per FIFO IRQ to their existing
software rings. Reassembly, packet decoding and subscriber callbacks remain in
thread context. Polling is still an explicit fallback; IRQ mode no longer also
uses the thread as a second hardware-FIFO producer. Ring consumption preserves
the interrupt mask and protects its copy/tail update from drop-oldest eviction.

All seven factory images build. Five executable regressions pass on repeated
three-frame bursts without worker service, bound the callback even when more
frames keep arriving, reject unrelated handles/events, and preserve the caller's
interrupt mask. Private reversions reproduce the original failures on all five
boards. Existing native suites pass after updating PowerBoard's polling-budget
contract for its parameterized drain helper. Matching artifacts are retained at
`snapshots/bounded-isr-20261011`. The 32-second linked repeat records zero
controller FIFO losses on every board across all eight samples, versus thousands
of rejected frames in the preceding run. It still fails on two FC startup queue
errors. Gateway also records 112 startup DAQ ring drops, flat afterward; its
CAN-to-network handoff failures stay zero. Capture:
`diagnostics/bay-1791762479591465200`. No long soak has started or been qualified
for this candidate.

## Finite controller FIFO and synchronized CAN (2026-10-11; linked gate failed)

The pinned Renode FDCAN model queued overflow arrivals in an unbounded host
queue and replayed them after FIFO space became available. The local register
model now admits into its three-slot FIFO immediately, preserving acceptance
filters, overwrite behavior, loss status and IRQ signaling. It records both
per-reset and lifetime rejected-arrival counts. Linked qualification includes
mandatory zero-rejection lifetime probes for every modeled controller; these
have no firmware warm-up exclusion and survive firmware resets. Missing samples
also fail the gate. CPU performance remains at its existing setting.

The upstream base is Renode infrastructure
[`9e90b52b321179001aa2126ee5abca638cb7f7ed`](https://github.com/renode/renode-infrastructure/blob/9e90b52b321179001aa2126ee5abca638cb7f7ed/src/Emulator/Peripherals/Peripherals/CAN/STM32_FDCAN.cs),
matching the pinned Renode build. Its MIT notice and license are retained.
All seven board runners now use the simulator default 100 µs quantum instead
of overriding it to 1 ms. At the configured wire rate, one 1 ms interval can
deliver more frames than the hardware FIFO holds before a receiver advances.
RF’s separate power-board-absent regression uses the same quantum. This fixes
model fidelity; it is not a firmware clock boost or a relaxed loss threshold.

Three-MCU register contracts pass: overflow raises the loss IRQ, exactly five
arrivals are rejected after filling three slots, no rejected frames are replayed,
and lifetime rejection counts survive reset. The original model fails the
loss-IRQ case. Existing shared-wire TX/cancellation/reset contracts pass. All
86 simulator native tests and the full image peripheral contracts pass. All
seven board native suites pass (761 checks, two skips). The matching simulator
image builds. Its linked 32-second run fails on finite-FIFO loss: RF rejects
437–1,969 arrivals, actuator 739–1,904, valve 1,160–2,795, PowerBoard 464–1,738,
and FC 418–1,755 across the eight samples. Gateway and DAQ hardware rejection
counts are zero; gateway's software ring still records 110 startup DAQ drops.
CAN-to-network handoff and FC queue errors are zero. Capture:
`diagnostics/bay-1791761577380278776`; checksummed artifacts:
`snapshots/finite-fifo-20261011`. The old deferred model hid this failure.
No long soak has been qualified by these checks.


## Direct inline payload decode (2026-10-11; native passes, prior model gate failed)

Small decoded values now enter inline packet storage directly rather than first
allocating a temporary Arc and immediately copying it inline. Large payloads
retain heap storage; the compression/decryption admission checks remain. The
scalar allocation regression measures three allocations in heap and compact
modes, with cached sender metadata. Restoring the temporary payload allocation
fails with four allocations; restoring both original copying paths fails with
six. Default and compact test routes pass all ten gates (452/466 tests, one
optional skip each), all seven factory images build, and the matching
GroundStation image builds. The old-model 32-second run failed: 127 startup DAQ
ring drops and later handoff failures reaching 61, with no allocator failures
or FC queue errors. That run used 1 ms synchronization and the unbounded host RX
overflow queue; it cannot establish physical MCU capacity. Matching artifacts
are retained at `snapshots/inline-decode-20261011`; capture:
`diagnostics/bay-1791760458289668355`. The corrected-model linked run above is
required before this candidate can qualify for a long soak.

## Owned decode metadata (2026-10-11; startup gate still failing)

Wire decoding now retains its sender and endpoint allocations directly in the
packet instead of copying both into new allocations. Numeric embedded sender
aliases format into a 16-byte stack buffer, removing a temporary String. This
change preserves encoded bytes and packet identity; it does not reduce every
packet to zero allocations. The allocation-counted scalar regression passes
with four allocations in heap and compact modes. A private comparison restores
the original metadata copying and fails with six allocations. The captured
MCU startup-warning identity regression still passes. All seven factory images
build, including FC; 66 checksummed artifacts are retained under
`snapshots/owned-decode-20261011`. Default and compact routes pass all ten gates (452/466 tests, one optional
skip each). The matching GroundStation image builds. The linked 32-second run
has zero FC queue errors, CAN-to-network handoff failures and actuator/valve
ring drops across all eight samples. The startup gate still fails on 110 DAQ
ring drops; they do not increase after the first sample. Gateway admission
rejections remain at 57 throughout, allocator failures are zero, and peak TLSF
live blocks are 60,496 bytes within a 71,480-byte region. This candidate is not
qualified for the long soak. Capture: `diagnostics/bay-1791759591399538779`.

## Gateway startup catalog (2026-10-11; rejected comparison)

The gateway queues its first local discovery catalog after router construction
and before enabling CAN reception. Packet transmission still runs through the
normal worker after CAN starts, and admission failures retain the ordinary
retry path. This moves catalog construction out of the first receive burst;
it does not enlarge buffers, disable loss counters or change the qualification
gates. All 102 native gateway checks pass, including the actual worker's
catalog-before-CAN ordering, UART/dispatch fairness and maintenance-burst cases.
The factory image builds with unchanged memory reservations. Matching artifacts
are retained under `snapshots/prewarm-20261011`. The linked repeat failed:
94 startup DAQ drops and later CAN-to-network handoff failures rising to 1,540.
Quiet-peer ring drops and FC queue errors remained zero, but the early queued
catalog increases pressure when later snapshots arrive. The startup change has
been reverted. Capture: `diagnostics/bay-1791752109150196397`. This comparison
must not be shipped or used to start the long soak.

## Allocation-free queue sizing (2026-10-11; linked validation pending)

Router and standalone Relay now calculate packet lengths without constructing,
copying or checksumming a throwaway frame for queue accounting. The helper
matches the encoder's frozen contract, endpoint omission and default compact
unsequenced reliable header. The regression covers scalar, reliable scalar and
captured frozen receipts. Sixty-four small-frame measurements allocate zero
buffers; restoring the old encode-to-measure behavior reproduces 128 allocations
and fails the regression. Wire encoding itself is unchanged by this step.
Compression may still require its existing bounded work; this is not a claim
of zero allocations for every compressed packet.

Default and compact test routes pass all ten gates (451/465 nextest checks,
one optional skip each, Python checks and ARM builds). All seven factory images
build; 66 checksummed artifacts are retained at `snapshots/size-fixed-20261011`.
The larger gateway ring has been reverted after the failed comparison below.
The matching GroundStation image builds. Its 32-second linked repeat has zero
FC queue errors, CAN-to-network handoff failures and actuator/valve ring drops
at all eight samples. It remains a failed startup qualification: 110 DAQ ring
drops occur before the first sample and do not increase afterward. Gateway
TLSF peak is 60,516 bytes in a 71,480-byte region; this is live-block accounting,
not a largest-free-block guarantee. Managed rounds complete in 1,820/1,000/1,000 ms.
Capture: `diagnostics/bay-1791751683268436965`. No long soak has qualified it.

## Gateway burst storage (2026-10-11; rejected comparison)

The gateway candidate allocates a 96-frame CAN receive ring once before
interrupts start, with a 16-frame static fallback on allocation failure. The
smaller static reservation increases the allocator pool by 2,176 bytes; the
13 KiB worker stack is preserved. This absorbs receive stalls without a fresh
allocation per frame. It is bounded buffering, not a throughput or fragmentation
cure. Runtime diagnostics expose allocation failures and active capacity.

All 103 gateway native checks pass, including startup allocation failure,
reuse on initialization and 1,000 ordered 80-frame DAQ bursts followed by an
actuator frame. The factory image builds with 114,448/114,688 bytes of static
RAM and 470,836/491,008 bytes of application flash. The linked 32-second comparison failed: startup DAQ drops decreased from 110
to 62 and quiet-peer ring drops were zero, but CAN-to-network handoff failures
rose to 1,656 and memory-admission drops to 1,739. Ring allocation succeeded;
hardware FIFO overflow stayed zero. The additional persistent buffer consumes
the headroom needed by dispatch. This experiment has been reverted; it must not
be shipped or used to justify a soak. Failed capture:
`diagnostics/bay-1791750460059859097`; retained comparison artifacts:
`snapshots/heap-ring-20261011`.

## Packet allocation and bounded queue classification (2026-10-11; pending)

Writing contracts and shapes directly into the header removes two temporary
buffers and their growth allocations from the captured small ACK path: the
negative comparison performs six allocations; the repaired encoder performs
two. The scalar path also performs two. The regression checks identical packed
bytes after warm-up and unchanged decoded identity/payload. This complements
the 83-to-51-byte ACK reduction below; it is not a claim that every packet has
the same size or allocation count.

All seven factory images build for this encoder candidate. Default and compact
full test gates pass. Its eight-second linked diagnostic at 100 µs quantum still
has 110 startup DAQ ring drops and one FC queue error, with no quiet-peer ring
drops or CAN-to-network handoff failures. The run is not qualification and does
not justify starting a soak. Capture: `diagnostics/bay-1791746731178151231`;
matching artifacts: `snapshots/direct-contract-20261011` on Jupiter.

Read-only hooks against the exact production ELF identify the FC error as
`ordered end-to-end delivery exhausted retries` at tick 2852, retaining a WARNING
(type 114) awaiting GroundStation. This is a receipt-path failure, not evidence
of an FC hardware exception or watchdog reset.

The next candidate reads only a bounded type prefix when selecting queued
control traffic in both routers and relays. Full dispatch still validates the
CRC/envelope. Its malformed-prefix and corrupt-frame regression passes, as do
the router/relay control-budget fairness regressions. Linked validation is still
pending; the long soak remains gated on a clean short run.

## Cross-platform receipt identity (2026-10-11; pending linked validation)

The packet ID used native `usize` bytes for payload length, while the wire
parser uses `u64`. MCU packet IDs therefore differed from hosted receipt IDs.
Unresolved numeric sender aliases also rehashed their textual alias during
repacking/identity calculation instead of retaining the source address.
Both are corrected; the alias/repack/receipt regression fails with the old
behavior and passes with the repair. An isolated regression also decodes the
captured FC "SD unavailable; flight/network continue" WARNING with its actual
application schema IDs, and checks receipt identity through repacking. It
passes in heap and compact modes; restoring four-byte length hashing in a
private comparison reproduces the identity mismatch. All boards and GroundStation need the
coordinated identity update.

Default and compact test routes pass all ten gates (449/463 nextest checks,
one optional skip each, Python checks and ARM build). All seven factory images
build, retained with 66 checksummed artifacts under
`snapshots/canonical-id-20261011` on Jupiter. The matched canonical-ID linked run has zero FC queue errors across all eight
samples. It still fails on gateway drops: 110 DAQ startup drops and two frames
each from actuator and valve later, plus one CAN-to-network handoff failure.
Capture: `diagnostics/bay-1791748447636918687`. Thus the receipt identity repair
is supported by linked evidence, but gateway qualification remains incomplete.

The next candidate removes owned endpoint/target metadata from wire-ID hashing.
The captured frozen ACK performs zero allocations for 64 identity calculations;
the negative comparison restoring the old parser performs 256 allocations.
Wire validation remains enabled. Seven firmware images build. Default and compact
routes pass all ten gates (451/465 nextest checks, one optional skip each,
Python checks and ARM builds). The matched 32-second linked run has zero FC
queue errors and zero CAN-to-network handoff failures at all eight samples.
It remains a failed gateway qualification: 110 startup DAQ drops, one valve
frame at startup and one later actuator frame. Capture:
`diagnostics/bay-1791749280296525871`; 66 checksummed matching artifacts:
`snapshots/borrowed-id-20261011`. No long soak has started for this candidate.
The earlier direct-contract 32-second diagnostic still reproduced 110 startup
DAQ drops and one ordered WARNING retry exhaustion; retained capture:
`diagnostics/bay-1791747534952239476`. No soak has qualified this candidate.

## Receipt capture and header reduction (2026-10-11; pending)

Read-only hooks identify exhausted FC startup WARNING/INFO delivery records
waiting for GroundStation (owner hash `a7fcbb129e92435f`). Their ACK packet IDs
are visible in RF radio ingress, RF CAN transmission and FC CAN ingress; this
narrows the investigation to receipt processing/timing rather than claiming
all such receipts disappear on transport. CFSR/HFSR remain zero.

A separate deterministic transport bug is reproduced in both Router and Relay:
a rejected first full template frame updates TX state, and its retry becomes a
compact frame before RX has received the template. Both regressions fail without
the repair and pass with it. Recovery forces full refresh while preserving RX
state and eviction history, without allocating. The header/template gates below pass.

Built-in ACK/control endpoint metadata is protocol-defined. Omitting its redundant
32-byte bitmap retains the existing wire flags/decoder and frozen owner/publisher
contract; an 83-byte ACK becomes 51 bytes. Round-trip routing, packet identity,
payload, targets and non-default explicit endpoints pass the focused regression.
Application contracts still carry explicit endpoints during schema changes.
Full gates pass for the header/template candidate: 442 default and 456 compact
nextest checks, one optional skip each, Python checks and ARM build/Clippy.
All seven factory images build. Its linked 32-second repeat emits actual 51-byte
ACKs, has zero FC queue errors, zero quiet-peer ring drops and zero CAN-to-network
handoff failures. Managed rounds finish in 1,560/1,000/1,000 ms. It still fails
qualification with 110 DAQ startup ring drops (including 36 bulk-admission drops).
Retained capture: `diagnostics/bay-1791740537189017138` on Jupiter.

A second exact regression reproduces retry exhaustion with the matching ACK
already queued. Router normal/ISR receive and relay TX-service tests pass after
processing queued controls before timeout checks. Removing that service-order
repair in a private copy reproduces both failures. The control drain shares the
caller deadline. Its first linked repeat still fails: 110 startup ring drops and
324 CAN-to-network handoff failures, despite zero FC queue errors. Returning
after control RX consumes the deadline can starve outgoing work. Router and
Relay now allow one ready transmission before yielding and defer timeout checks
when control RX used the budget. Both starvation regressions fail with the early
return restored and pass with the repair. Full host gates pass: 446 default and
460 compact checks, one optional skip each. All seven images build. The matching
ARM gates also pass in both modes. The linked 32-second repeat has zero
CAN-to-network handoff failures at all eight samples (previous candidate: 324),
zero quiet-peer ring drops and no panic/watchdog reset. It remains a failed
qualification: 110 DAQ startup drops and one FC queue error. Managed-variable
rounds take 1,360/1,000/1,000 ms. Retained capture:
`diagnostics/bay-1791742409300332946`. Matching images/maps are preserved
at `snapshots/receipt-fairness-20261011` with SHA-256 hashes.

Gateway also catches up CAN frames arriving during maintenance when the ring
was drained before maintenance began. Permanent overload retains the existing
cycle bound. The extracted regression fails without this catch-up; all 102 host
checks and the factory build pass. The linked comparison above includes this
change; its startup loss gate still fails.
The release remains held while the linked loss gates fail.

## Gateway burst-drain and packed-schema checkpoint (2026-10-11; pending)

Gateway now drains a transient CAN backlog before running expensive queued
network RX work. UART remains serviced between slices; network dispatch,
maintenance and ThreadX sleep still run once per bounded cycle under permanent
CAN overload. All 102 host checks pass. The extracted-worker regression fails
against the old drain order and passes with sustained ingress and clock wrap.

A 32-second repeat has zero actuator/valve ring drops and no FC queue errors.
It still records 110 DAQ startup drops and eight CAN-to-network handoff failures;
this remains a failed qualification. All three managed-variable rounds finish
within 2,500 ms (2,030/1,010/1,000 ms). Retained probes are on Jupiter at
`diagnostics/bay-1791722855115358664`. An experiment removing forced DAQ routes
records 117 startup drops, so it is withdrawn and the routes are restored.

The packed-schema fast path avoids allocating another complete payload for an
already-known broadcast snapshot. Raw/full-wrapper tests pass in heap and
compact modes, including CRC/truncation, unknown-schema, compression and receipt
processing guards. Disabling the repair reproduces memory-pressure refusal.
Full SEDSnet checks pass: 439 default and 453 compact nextest tests (one optional
skip in each), Python checks and ARM build/Clippy gates. All seven updated factory
images build. The linked 32-second comparison eliminates CAN-to-network handoff
failures (zero at all eight samples), but still fails qualification: 114 DAQ
startup ring drops and one FC queue error (HandlerError, result -9). The
managed-variable rounds finish in 1,480/1,540/1,000 ms. The 58 admission-probe
refusals count attempted probes, not necessarily lost packets. Retained probes
are at diagnostics/bay-1791734219979923702. FC error capture uses simulator
hooks on the unchanged production image; adding firmware diagnostic globals
altered timing/layout and did not reproduce this error.

An unchanged-image hook reproduces the FC error in a second 32-second run:
`ordered end-to-end delivery exhausted retries`, with CFSR/HFSR both zero.
That repeat records 138 startup ring drops and 16 CAN-to-network handoff
failures; the handoff error is -14 (Io). Capture is retained at
`diagnostics/bay-1791735976787836662`. A 16-second comparison has no FC error
but 122 startup drops (120 DAQ, two valve); it is not a qualification pass.
An experiment skipping the end-of-cycle network pass when a CAN slice already
serviced network work passes the extracted-worker test but fails the linked
comparison: ring drops grow from 116 to 155, including 11 actuator and 17 valve
frames, and handoff failures reach 201. It is withdrawn; both the worker and its
regression are restored to the prior baseline. Capture is retained at
`diagnostics/bay-1791736459047794346`. The repeated end-of-cycle service is
necessary in the actual linked workload even though the stubbed worker's
progress counts alone do not expose this queue pressure.

Linked-run teardown now also emits R0–R15 and ICSR/CFSR/HFSR for each node.
All 85 simulator native checks pass; the monitor commands were separately
validated against the actual Renode runtime. These are read-only operations.

The earlier production candidate completed a 600-second virtual-time soak
with repeated underglow/buzzer receipt checks. No node panicked or watchdog-reset,
and FC queue errors remained zero. It still fails qualification: gateway ring
drops increase from 125 to 151, and one managed-variable round takes 2,610 ms
against the 2,500 ms limit. Its matching factory/ELF/maps
are preserved under `snapshots/production-burst-600` with SHA-256 hashes before
building the next candidate. That soak does not include the packed-schema change
and cannot qualify the latest code. Hardware testing and reset/rejoin remain
pending. The SEDSnet release remains held while linked loss gates fail.

## RF startup and schema-progress checkpoint (2026-10-11; pending)

Latest shared library checks pass: 439 default and 453 compact nextest checks
(one optional test skipped in each), all build.py test gates including ARM.
All seven factory images build with the forwarding changes. The simulator's
85 native checks and full Docker peripheral contracts pass. The image test
script now avoids SIGPIPE from an early-exiting grep under pipefail.

The 32-second known-schema comparison remains failed: Gateway ring-drop counts
advance from 152 to 157, and one managed-variable round takes 3,150 ms against
a 2,500 ms limit. Memory-admission refusals fall to 16 and CAN handoff failures
stop at two; this does not qualify the return path. Diagnostic capture is
retained on Jupiter under `diagnostics/bay-1791706936051056162`.

The updated known-schema fast path runs before full decoder admission on both
embedded router and relay. Its low-headroom regression passes in heap and compact
modes; disabling the router repair reproduces a memory-pressure failure. Compact
decoding also borrows its input instead of copying a temporary body. All seven
updated factory images build. Integrated qualification remains pending.

The independent watchdog exposed RF startup exceeding its timeout when GPS
configuration waits separately for every setting. RF now bounds the whole
configuration attempt, including send and ACK waits. Its 119 host checks pass
(one optional check skipped). A 32-second linked run advances RF watchdog
feeds without resetting and continues FC IMU/barometer reporting. It still
exceeds the managed-variable latency bound and drops startup Gateway CAN
frames, so network qualification remains failed.

Router and standalone Relay preserve an unchanged queued schema packet's
identity and retry deadline. A regression permits only one real chunk per
callback while repeated advertisements arrive: both implementations complete
one transfer. Removing the repair makes both tests fail. Changed schema bodies
still replace stale queued snapshots.

Linked simulation now loads the matching factory bootloader, application header
and metadata rather than leaving boot flash blank. It checks application LOAD
bytes against the probe ELF without overwriting the image header. Firmware
reset/rejoin qualification remains pending. Simulator failures terminate the
owned runtime process group and retain probes/logs when diagnostics are enabled.

## Chunk reclamation and watchdog checkpoint (2026-10-11; pending)

All seven factory images build after the shared side-chunk receive-budget
repair. The full heap and compact SEDSnet test workflows pass, including ARM
Clippy and the captured incomplete-transfer saturation case. The raw TLSF
alignment regression also passes on a 32-bit Cortex-M model.

A linked 16-second run with the updated GroundStation receives continuous FC
IMU/barometer telemetry and reports no FC ordered-delivery error. Gateway
still drops startup CAN frames; this remains a failed gate. A backlog-aware
scheduling experiment increased memory-admission refusals and was withdrawn.
Queue/pool capacity and MCU clocks are unchanged. This is not a ten-minute qualification.

The simulator now models the G4/H5/U5 independent watchdog. Register/timing
contracts pass, and unexpected watchdog resets fail qualification. Full linked
firmware validation with this peripheral remains pending. Evidence is in
`startup-trace-bay.log`, `watchdog-contract.log` and the
`chunk-reclamation-*-tests-gates.log` logs on Jupiter.

## Shared-clock / production CAN checkpoint (2026-10-10; pending)

Current diagnostics use all seven hardware firmware images with production
UART DMA (`SEDS_FIRMWARE_SIM_TEST=OFF`) and the SEDSnet 4.1.4 development
candidate. GroundStation routing and test deadlines now share Renode virtual
time. The intermediate-hop end-to-end ACK regression passes, and a 16-second
run acknowledges all three flight-state, underglow, and buzzer update rounds
within the 2,500 ms virtual-time bound. FC IMU and barometer data keep arriving.

The linked FDCAN model now reserves three TX slots and serializes a shared wire
rather than completing every transmission immediately. Eight register/wire
contracts pass, including canceled-slot reuse and two-controller timing.
The timed network still reports Gateway CAN receive-ring drops. This is not
waived: queue-service profiling and production ingress changes are being tested.
Retryable UART enqueue refusals and FC FIFO BUSY attempts are counted separately
from exhausted UART retries and HAL CAN errors; these attempt counters do not
prove loss. Actual receive-ring overflow remains a zero-loss gate.

The G4 CRC model now covers the production hardware adapter. Four CRC
contracts pass; the gateway reports an active hardware CRC provider during the
linked run. Guarded heap queue growth avoids the captured contiguous-allocation
OOM: its 16-second comparison made forwarding progress with zero allocation
failures, but increased memory-admission refusals and still failed CAN/UART loss
bounds. Queue growth is therefore not enabled in the default gateway build.
A fragmented-allocation regression also returns NULL from the allocator and
checks that retained work drains and later growth can recover.

Embedded identifier sorting reduces the FC compact+TLSF application to 463,752
bytes with GCC 14.3 (474,624-byte capacity). Flight features and flash partitions
remain unchanged. SEDSnet's compact and heap full build/test gates passed
(446 and 432 nextest checks, respectively), including the allocator-NULL
regression. A separate learned-route idle-dispatch regression passes with zero
allocations across 100 drain passes. The full GroundStation default backend
suite passes (235 tests, one manual fixture ignored), as do all 101 Gateway
host regressions. The gateway now has explicit
forwarding-task progress assertions; flat loss counters cannot imply a pass.
Neither result is a ten-minute linked soak or physical-hardware qualification.
Missing simulator-only probes are recorded in diagnostic manifests, never
counted as passed checks. GroundStation's embedded link policy also disables
zstd payload compression because the firmware builds omit that decoder;
header-template transport remains enabled. Its nine policy/clock tests pass.

Evidence: `/home/rylan/seds-return-path-20261008/logs/` on Jupiter, especially
`ack-bridge-bay.log`, `can-wire-bay.log`, `can-profile-bay.log`,
`fc-small-sort-compact-build.log`, and `fc-drain-compact-build.log`.


## Production UART DMA checkpoint (2026-10-08)

USART1/2 now model real byte RX/TX DMA, DMAMUX selection, IDLE/overrun,
circular reload and half/full completion interrupts. Thirteen Renode checks
cover these paths, including exact bytes when HAL changes interrupt enables
mid-transfer and DMA2's DMAMUX channel offset. The concurrent UART regression
preserves 500,000 TX and 500,000 RX bytes. Rust tests, formatting and Clippy pass.

A 30-second virtual-time RF/Power/FC run on Jupiter used hardware firmware
images (`SEDS_FIRMWARE_SIM_TEST=OFF`), not the legacy UART polling workaround.
GroundStation's IMU and barometer 5 Hz return-path checks passed; the RF link
transmitted 413 and received 252 frames. FC sensor timestamps kept advancing,
and recorded allocator failures and panics remained zero. This is a short
acquisition/transport result, not a ten-minute soak or hardware timing proof.

A separate 16-second seven-board run discovered all boards and delivered a
Valve ACK within the test's scaled 2,500 ms bound. It failed the retained FC
zero-queue-error threshold (one error), reported a managed-variable reliable
ACK timeout, and missed Gateway traffic/graph attribution. Those failures are
not waived. Production images lack some simulator-only probes; the omitted
instrumentation was recorded, so this run is diagnostic rather than full
qualification. FC's physical image uses SEDSnet 4.1.3; the other snapshots use
the current 4.1.4 development candidate with pending queue-reclamation changes.

The old DMA model restarted a transfer when HAL disabled its half-transfer
interrupt, repeating buffer bytes and causing radio CRC errors. That model bug
is fixed and covered by an exact-byte regression. Future UART validation must
use hardware images or remove the old board-side polling workarounds; setting
`SEDS_FIRMWARE_SIM_TEST=ON` alone still selects those workarounds in existing RF
and Gateway sources.

Evidence on Jupiter: `/home/rylan/seds-return-path-20261008/logs/`, particularly
`fc-return-dma-ccr.log`, `real-dma-full-bay-ccr.log`,
`uart-dma2-contract.log`, `uart-dma-final-contracts.log`, and
`uart-dma-rust-tests.log`. The candidate Docker image is
`seds-firmware-simulator:uart-dma-20261008`.

## Current codec/arena candidate (2026-10-05; pending)

The SEDSnet development candidate is `9f48a8e`; all seven board host/Python
and GoogleTest suites pass. The simulator Rust/Python suites and Docker image
contracts pass, including byte UART DMA request pacing and completion IRQs.
Native codec pressure passed 600 wall-clock seconds, and native network churn
passed 600 seconds of protocol time. These are separate from linked ARM runs.

The latest board, GroundStation, and SEDSnet snapshots are isolated on Jupiter
for allocator OFF/ON comparison. Both gateway variants use 1 Mbaud Pico UART.
The short baseline fails the UART queue-refusal bound. The compact candidate
reports no UART queue refusals but fails the DAQ zero-overrun bound with one
startup overrun. Neither is a ten-minute linked pass; no throughput improvement
is established. All memory, progress, fresh command ACK and fault bounds remain
enforced. `--ultra-soak --soak-only` permits a diagnostic long run independently
of the short gate, and explicitly does not qualify that short gate.

The previous 4.0.33 results below do not qualify current firmware. Initial runs
also exposed incompatible ThreadX-only probes in TLSF builds and unsupported
byte DMA writes to USART2; the corrected model preserves physical wire timing
and bounded buffering. Gateway queue parking uses the plain arena; embedded
compression remains unqualified due to its workspace cost.

## Historical v0.4.12 qualification (2026-09-16)

The SEDSNet v4.0.33 source candidate passes all seven firmware builds, the
16-second linked gate, Gateway-only and fill-group 120-second restart tests,
and two complete 600-second seven-board/GroundStation soaks. One long run
restarts only GroundStation twice with the boards powered; the other restarts
GroundStation alone and then with RF, Power, and Flight. Every scheduled fresh
Valve/Actuator state response, named discovery, graph/traffic attribution and
memory threshold passes. No recorded allocator failure, panic or HardFault
occurred. Response times use the existing scaled host validation clock and do
not establish hardware latency. See [route recovery](route-recovery.md) for
measurements, negative controls and retained evidence filenames.

The simulator also rejects missing/duplicate probe observations, preserves
external GPIO inputs across MCU reset, and accounts for explicitly reset
command counters without dropping the independent GroundStation state checks.
This qualification does not replace hardware testing or the separate OTA/SD
content gates described below. Historical pending/failing entries retain their
original context and do not supersede this checkpoint.

## Earlier bounded-header checkpoint (2026-09-16)

The bounded-header candidate passed the 16-second gate, the 120-second restart
regression, and the full 600-second seven-board/GroundStation network soak.
Command/state responses, restart/reconnection, attribution, traffic and memory
assertions passed without relaxing their limits. Gateway pool low water was
6312 bytes in the soak (1024-byte minimum). Evidence is retained in
`qualification-route-recovery/bounded-header-ten-minute.log` on Jupiter.
SEDSNet v4.0.31 contains the tested router/relay fixes. Physical hardware
validation remains required; this does not qualify the separate live OTA and
SD-content gates below. The following entries document earlier checkpoints,
including failed attempts, rather than superseding this result.

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
- The release-pinned 600-second run on Jupiter failed its first periodic Valve
  command at sample 1 (50 seconds simulated): GroundStation observed no matching
  ACK within its 2,500 ms normalized test-clock limit. Ordered end-to-end retries
  were also exhausted. FC/RF telemetry continued, so this was not a total CPU
  crash. The command-test task returned after the failure; the run was stopped
  early, not completed or passed. Logs are retained in the Jupiter qualification
  directory as `release-0411-soak.log` and `release-0411-soak-groundstation.log`.
  Root cause is not yet established. The earlier restart/soak attempts below
  must not be reported as passes either.

## ACK-failure investigation (2026-09-15; not yet qualified)

- The 60-second diagnostic reproduced the late Valve ACK timeout without any
  reboot or injected CAN outage. Gateway CAN/UART counters kept advancing;
  UART transmit failures and allocator failures remained zero. The late command
  did not reach the Gateway valve-command receive counter.
- A regression demonstrated that the simulator could overwrite a partially
  transmitted Pico UART frame under queue pressure. The physical Pico byte ring
  preserves that frame. The simulator now preserves it and evicts only whole
  pending packets; packet-count and byte-budget regressions pass in Docker.
- The first uninstrumented short rerun still failed with UART framing errors.
  A byte-traced 16-second rerun passed with identical UART-model/Pico-input bytes.
  This timing-sensitive result is not proof that the original failure is fixed;
  the 50-second command reproduction and full soak remain required.
- The lower-overhead capture reproduced the failure and localized a second
  defect: firmware TDR writes were valid, but the simulated TX FIFO replaced
  bytes before emitting them (first captured mismatch at byte 8,317). The PTY
  delivered those corrupted bytes unchanged. The UART model now uses concurrent
  queues without taking a model lock across Renode clock calls. It also rechecks
  for concurrent enqueue after disabling an idle timer, preventing a lost
  wake-up. A deterministic interleaving regression covers that race; the old
  model also stalled after 39 bytes in the concurrent stress test.
  A C# regression exercises the
  actual model with 500,000 concurrent TX and 500,000 RX bytes; Docker builds
  and image validation run it. The revised, uninstrumented 16-second seven-board
  gate passes (`uart-concurrent-short.log`). The 600-second run is in progress
  on Jupiter (`uart-concurrent-soak.log`); it is not yet a soak pass.

## Pre-release candidate validation (historical)

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

## Earlier remaining gates (600-second network gate now passed)

The current launcher now requests both the short gate and --ultra-soak by
default. Previously it invoked only --all, which omitted the ten-minute check.
Board runners require each individual Valve and Actuator response, alternating
open/closed states, and use normal startup discovery. GroundStation matches
response source, valve ID, requested state, and a fresh receive generation.
These stricter gates are candidates, not a hardware or linked-soak pass.
SEDSNet dev also fixes endpoint ownership being erased by empty keepalives;
its full built-in workflow passes, but integrated qualification is pending.

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

## Gateway UART route isolation (2026-10-12; candidate validation running)

A private, allocation-free route-plan probe (`bay-1791844883292921303`)
records only the CAN-side discovery route: GroundStation's UART route is
absent, and the valve command never reaches route selection. Eleven early
actuator status packets have no remote destination; subsequent status sends
cannot clear their retained delivery window. Ring, network handoff and memory
admission counters remain zero. This is not evidence of CPU saturation.

Gateway UART framing now delegates raw canonical and SDT packets to the
side-aware network receiver without allocating a temporary decoded packet.
An executable sanitizer test verifies one central validation for canonical,
invalid and SDT frames, and preserves nested framing compatibility. Gateway's
104 native checks pass. Its linked diagnostic (`bay-1791845288373937566`)
still fails valve status delivery: some raw commands now reach UART admission,
but the downstream handler remains untouched. This change removes redundant
allocation, but does not resolve the observed linked failure.

PowerBoard simulator builds now retain production ThreadX stack checking.
Both preprocessor configurations are checked by an executable regression; all
97 PowerBoard native checks pass. RF's simulator-only heartbeat scheduler now
uses one wrap-safe 32-bit deadline instead of redundant 64-bit state. Its fully
instrumented image fits physical RAM exactly; production pools are unchanged.
126 RF native checks pass, including timer rollover and backpressure. All seven
boards now use simulator instrumentation. The remaining omitted probes describe
allocator diagnostics absent from these builds; no linked assertion is omitted.

## Duplicate UART pacing diagnostic (2026-10-12; CRC comparison passed)

Private capture `bay-1791845428775281826` measures 88 CRC failures in 89
raw validator calls over eight virtual seconds. A captured 20-byte frame begins
`ST` instead of `SDT` and consumes `A5 5A` from the next framing header.
The host bridge previously paced bytes in wall time (with a one-millisecond
service loop) while the USART model already serializes RX in virtual time.
This creates spurious inter-character IDLE interrupts and DMA rearming.

The bridge now submits a frame through one nonblocking write and retains
partial-write/backpressure state. Byte timing belongs to the USART BRR timers.
Native tests cover whole-frame submission, partial writes, WouldBlock, and
bounded queue overwrite without corrupting an in-flight frame. With the same
private firmware probe, capture `bay-1791845885553945309` records 158 validator
calls and zero CRC failures. Discovery and command route selection recover.

The following fully instrumented 32-second linked run
(`bay-1791846303524963238`) passes command/ACK, managed-variable, IMU/barometer,
and all other delivery assertions except the obsolete DAQ 50 Hz check. DAQ is
configured for 250 Hz; the host and all board runners now require five consecutive
2–6 ms intervals and reject 20 ms intervals. This is a corrected test contract,
not a firmware rate reduction. The next matching linked gate is running.

SEDSnet receive-template storage now owns each header once, with a four-byte
wire-ID secondary index. Relay eviction now matches transmitter lowest-hash
eviction. Default and compact suites pass all ten gates (462/476 native tests).
All seven updated factory images build, preserved in
`snapshots/template-index-20261012` with SHA-256 manifests.

Gateway physical CAN FIFO/ring loss remains zero in the preceding run, but
6,273 CAN-to-network memory-admission failures accumulate by 32 seconds. These
are explicit bounded shedding, not proof of loss-free throughput. Sustained
command/telemetry and reconnect validation remains required before qualification.
