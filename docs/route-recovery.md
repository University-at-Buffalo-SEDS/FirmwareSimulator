# CAN route-loss regression

Run on a Docker host with seven release firmware builds and a simulator image
containing the candidate GroundStation and SEDSNet implementations:

```sh
python3 scripts/test-route-recovery.py --workspace /path/to/boards --image candidate
python3 scripts/test-route-recovery.py --workspace /path/to/boards --image candidate --report-node valve
python3 scripts/test-route-recovery.py --workspace /path/to/boards --image candidate --report-node actuator
python3 scripts/test-route-recovery.py --workspace /path/to/boards --image candidate --restart-regression --timeout 2400
python3 scripts/test-route-recovery.py --workspace /path/to/boards --image candidate --restart-regression --restart-nodes groundstation --timeout 2400
python3 scripts/test-route-recovery.py --workspace /path/to/boards --image candidate --restart-regression --restart-nodes groundstation --restart-samples 4 8 --timeout 2400
python3 scripts/test-route-recovery.py --workspace /path/to/boards --image candidate --restart-regression --restart-nodes gateway valve actuator daq --timeout 2400
python3 scripts/test-route-recovery.py --workspace /path/to/boards --image candidate --ultra-soak --timeout 10800
```

The normal 16-second network test runs without fault injection. The two 40-second
fault runs detach Gateway from CAN before boot, then reconnect it after three
samples (12 simulated seconds). The selected output board stays connected to local peers, but cannot
discover a route to GroundStation. Its own CAN transmit failure count must stay
zero. This distinguishes missing-route errors from local transport failures.
Simply disconnecting the output board was insufficient: the old library also
retried after CAN submission failures, hiding the missing-route bug.

The output board must expose the production retry probes `status_report_pending`,
`status_report_failures`, and `status_report_recovered`. The third sample must
show a failed submission and retained state. Recovery must retry retained state
and leave no pending states at the last sample. Gateway CAN errors and output
submission errors must stop increasing after reconnection. Existing GroundStation discovery,
telemetry, command/state round-trip and latency assertions remain enabled.
These are application state reports, not a replacement for protocol ACKs.
The ultra-soak option first runs the normal short gate, then 600 simulated
seconds with all seven boards and GroundStation. It retains the full-system
link-interruption, restart, traffic, memory and repeated-command assertions.
Allow sufficient wall time: virtual firmware time is slower than wall time.
The separate `--restart-regression` option runs the short gate and then a
120-second version of the same restart schedule. It is an iteration aid, not
a substitute for the 600-second qualification. GroundStation resumes the global
command round after restarting instead of replaying earlier commands in a burst.
Use `--restart-nodes groundstation` to reproduce service-only restarts while all
seven boards and the Pico-Fi pair remain powered. Updated board runners first
restart only GroundStation, then restart it with the avionics bay. Keeping the
service-only phase separate prevents fresh board announcements from masking
missing-topology recovery. `--restart-samples 4 8` repeats service restart twice; each
host process must discover all board names and correctly attribute their traffic.
Other node groups exercise firmware-only restarts without resetting GroundStation.
For a planned Gateway reboot, Valve may reject a status submission while its
upstream route is absent. The test requires zero such errors before the outage,
no further error-counter growth after the first post-restart sample, no pending
status at the end of each recovery interval, and the original fresh-command
responses within their latency bounds. Allocator and fault limits are unchanged.
External GPIO input voltages survive an MCU reset; clearing them would invent a
continuity/fault-input failure on otherwise healthy Valve and Actuator hardware.
The Docker image contracts include a negative-sensitive GPIO reset regression.
These options preserve the short gate, memory checks, repeated command/state
responses, and latency limits. A generated scenario is not a passing result:
inspect the complete run and retain its named topology JSON and output.
After reconnection, GroundStation also sends close/open/close commands to both
output boards and requires a fresh matching state response for each round.

The runner does not build firmware or pre-seed routes. It uses Release_Script
artifacts, except FlightComputer26 which uses Release. Build with simulator
instrumentation enabled and the candidate library prepared separately for each
board schema. An explicit simulator image prevents accidentally testing a stale
published image. Tests execute in Docker; the Python launcher runs on the host.

Each run has a 30-minute wall-clock deadline, configurable with `--timeout`.
Timeout is failure, and only the container created by this invocation is stopped.
Topology JSON is retained beside the board snapshots for inspection.

For a negative control, rebuild the selected board with the missing-route fix
reverted, keeping the retry probes. The missing-route assertions must fail.
Select that image with `--fault-elf build/RouteNegative/Valve_Board26.elf`
alongside `--report-node valve`; this leaves the fixed artifact untouched.
Do not label the regression validated until that control and the fixed build
have both been exercised. This short test does not replace a long-duration soak
or physical hardware validation.

## Candidate qualification (2026-09-15, Jupiter)

The normal 16-second seven-board run passed. The 40-second missing-route run
with Valve as the observed reporter passed with the candidate library; reverting
only the missing-route fix failed all three submission/retention/retry assertions.
This establishes a meaningful negative control, not just a happy-path pass.

The additional repeated close/open/close run exposed a GroundStation Tokio
worker stack overflow after reconnection. SEDSNet Display recursively invoked
ToString; the triggering simulation command was incorrectly encoded as i32
instead of u8. Both are fixed and regression-tested. The simulator now stops
promptly if a required host exits, rather than waiting for the firmware run.

The next run correctly rejected an Actuator open because its active-low driver
fault inputs were left at zero. The healthy board layout now drives those four
external signals high; production driver safety logic remains unchanged.
Both 40-second reporter runs now pass, including fresh close/open/close state
responses from both boards and seven-board memory/stack thresholds. The normal
16-second seven-board run also passes. The Actuator-focused rerun accounts for
expected route-loss submission errors on Valve too, while requiring its error
counter to stop advancing after recovery. Observed state-report latency in that
run was 42–71 ms for the first two rounds (simulation timing, not a hardware
latency guarantee). SEDSNet `./build.py test full` passes all nine stages.

Jupiter evidence: `baseline-qualified.log`, `valve-final-recovery.log` and
`actuator-qualified-recovery.log` in the qualification-route-recovery workspace.
Earlier failing logs remain alongside them. The Docker candidate was not
published, and these short regressions do not qualify a ten-minute soak.

## Restart regression (2026-09-16, Jupiter)

SEDSNet v4.0.30 passed its full release suite and was published. The first
600-second firmware run completed but failed command-response assertions after
the GroundStation restart (13/20 required responses). Its host test incorrectly
restarted the command schedule at round one. The fix preserves round numbers and
alternating target states across the restart, with a unit test for sample eight.

The corrected 120-second run passed all network and memory assertions, including
matching responses from both output boards after the restart. Actuator recorded
zero safety heartbeat timeouts, local safety aborts, command queue drops and
state-submission failures. Evidence: `restart-regression.log` in the Jupiter
qualification workspace. Do not treat the 120-second result as long-duration
qualification or hardware proof.

The subsequent investigation reproduced another independent library failure:
a known peer requesting topology after restart still received application
packets referencing its discarded compact-header dictionary. The deterministic
SEDSNet regression fails on v4.0.30 and passes with the dev fix. Routers and
relays now refresh TX headers on the requesting side without clearing RX state
or routes. The old-image 600-second rerun was stopped rather than qualifying
code known to lack this recovery. The fixed library passes its full built-in
suite and all seven firmware builds; updated full-system qualification is pending.

The restart-header-only image then failed the 120-second regression: Actuator
round four exceeded the response bound, and Gateway exceeded its pool end-drop
threshold. It is not qualified. Focused router and relay tests subsequently
reproduced protocol ACKs depending on a lost compact-header dictionary. The next
candidate sends reliability controls with complete headers while retaining
ordinary data compression. Its full-system results are pending; no memory or
latency limits were relaxed.

The control-header-only candidate then failed the short network check: nine
Valve-command transmissions were visible at GroundStation, but none decoded at
Gateway. The reliable retry budget could expire before periodic compact-header
refresh. The next candidate keeps reliable application frames self-describing
on every hop as well, retaining compression for best-effort telemetry. This is
still under qualification; earlier failures are retained in the Jupiter logs.

The dictionary-aligned candidate passed command delivery in its short run
(initial Valve state response: 745 ms in simulation) but failed Gateway memory
headroom: the shared pool low-water mark reached 740 bytes against a 1024-byte
minimum. No allocator failure or panic occurred, but this is still a failed
qualification. Reliable full frames were unnecessarily retaining compression
templates on both peers. The next candidate uses native self-describing packets
without those cache entries, with regression assertions for zero retained
templates. Memory thresholds are unchanged. Evidence:
`aligned-header-regression.log`; ten-minute qualification remains pending.

The bounded-header candidate then passed the normal gate and the 120-second
restart regression, including fresh command/state responses and all memory
thresholds. Gateway pool low water was 6952 bytes in that run (minimum required:
1024), versus 740 bytes in the failed candidate. A separate regression also
reproduces and fixes timestamp retention when template capacity is zero on
routers and relays. The full SEDSNet test suite passes all nine stages, with
364 tests in its main test stage. Evidence: `bounded-header-regression.log`.
The matching 600-second seven-board soak subsequently passed all network and
memory assertions, including the command/state rounds after GroundStation restart.
Gateway pool low water was 6312 bytes (1024 minimum), Valve 3936 and Actuator
13864. Evidence: `bounded-header-ten-minute.log`, ending with
`PASS: firmware network and route recovery`. This is simulated-time
qualification of the candidate sources released as SEDSNet v4.0.31, not a
guarantee of physical hardware behavior.

### SEDSNet v4.0.32 reconnection qualification

The new missing-topology-baseline unit regression passes on v4.0.32 and fails
on an otherwise unchanged v4.0.31 checkout: the older implementation emits no
bounded recovery request after the deliberately lost baseline. This negative
control distinguishes the protocol fix from simply generating more traffic.

The 120-second GroundStation-only restart passed with all seven MCU images and
the Pico-Fi bridge left running. Named discovery, per-board traffic attribution,
the network graph, all 20 fresh output-state response rounds, and memory checks
passed. Evidence: `topology-v4032-gs-only.log`.

The firmware-group restart exposed a simulator GPIO-reset error: external
continuity/fault inputs were cleared, causing a legitimate firmware safety
abort. The GPIO contract fails against the old model and passes after the fix.
With the corrected model, every post-restart Valve and Actuator response arrived
within the original latency bound. That run still failed its Gateway pool-trend
threshold, so it is not recorded as an overall pass. Evidence:
`topology-v4032-fill-restart.log` and `topology-v4032-fill-restart-gpio.log`.

The 600-second repeated GroundStation-only restart run completed but failed
Actuator's pool-trend threshold: a 3512-byte end drop was reported. Actuator
remained network-ready at every sample, but that does not satisfy memory
qualification. Evidence: topology-v4032-gs-repeated-ten-minute.log. The
Gateway-only rerun also failed its pool-trend threshold (5316 bytes), in
topology-v4032-gateway-recovery.log. These failures are unresolved; no memory
threshold has been relaxed.

The updated default mixed restart run failed because RF's network_ready probe
returned only 11 of 12 required samples (sample 2 missing). Evidence:
topology-v4032-mixed-restarts.log. Missing observations are not treated as
passes or filled in. The capture failure needs investigation before that
scenario can qualify. Hardware was unavailable; none of these results
constitutes hardware qualification.

### Allocation-maintenance and probe-capture candidate

The next candidate removes a full route-table clone from every discovery poll
in both SEDSNet routers and relays. A measured regression fails on the previous
implementation (73,000 allocations in 1,000 idle router polls) and passes with
zero allocations after the fix, while retaining routes and expiring stale peers.
The full library suite and all seven Release firmware builds pass.

Linked-bay memory probes now write to a dedicated file inside the simulator
container instead of sharing stdout with asynchronous peripheral logs. Capture
must contain exactly one reading per node/probe/sample index; missing,
duplicate, or out-of-range indices fail. This changes neither firmware
execution nor memory/latency thresholds.

The updated short gate and 120-second Gateway-only restart regression pass all
network, command-return and memory assertions. Gateway's pool end-drop was
20 bytes (previous candidate: 5316 bytes); Actuator's was zero. Evidence:
discovery-idle-gateway-restart.log. The ten-minute mixed-restart and repeated
GroundStation-only restart runs remain under qualification.

The 120-second fill-group restart completed with all memory limits and all 20
fresh command/state responses satisfied, but failed a test-counter assertion:
Valve's execution count reset from 9 to 3 across its scheduled reboot. This is
not a monotonic interval. The runner now requires at least one new execution in
that reboot interval, retaining normal counter-growth assertions elsewhere and
every independent GroundStation response/latency check. A zero-execution new
boot still fails. A focused regression covers this distinction and verifies that
GroundStation-only restarts do not alter the MCU counter assertions. Evidence:
discovery-idle-fill-restart.log.

The corrected full-group rerun passed its short gate and 120-second scenario,
including every command/state response, all memory thresholds, and post-reset
command execution. Gateway pool end-drop was 216 bytes and Actuator's was zero.
Evidence: discovery-idle-fill-restart-counter.log. This does not replace the
600-second scenarios.

The 600-second repeated GroundStation-only restart scenario now passes. All
seven boards remained powered across two service restarts. All 20 fresh
Valve/Actuator state responses, named discovery, per-board graph attribution,
and memory/stack thresholds passed. Actuator pool end-drop was 96 bytes
(previous failing candidate: 3512); Gateway's was 252 bytes. No recorded
allocator failures, panics, or HardFaults occurred. Observed command response
times were 29–843 ms using the host validator's existing time scaling, not a
hardware latency guarantee. Evidence: discovery-idle-gs-repeated-ten-minute.log
and discovery-idle-gs-repeated-host.log.

The 600-second mixed-restart scenario also passes. It first restarts only
GroundStation, then GroundStation with RF, Power, and Flight. All 20 scheduled
command/state responses passed (19–1008 ms with the same host-time scaling),
as did discovery, graph attribution, persistence and memory thresholds. Gateway
and Actuator pool end-drop were both zero; all recorded allocator failures,
panics and HardFault counters stayed zero. Evidence: discovery-idle-ten-minute.log
and discovery-idle-mixed-host.log. Both scenarios retain the initial 16-second
gate and exercise seven actual ARM firmware images plus the GroundStation
binary. These are finite simulation results, not hardware or indefinite-uptime
guarantees.
