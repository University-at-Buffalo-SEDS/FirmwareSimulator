# CAN route-loss regression

Run on a Docker host with seven release firmware builds and a simulator image
containing the candidate GroundStation and SEDSNet implementations:

```sh
python3 scripts/test-route-recovery.py --workspace /path/to/boards --image candidate
python3 scripts/test-route-recovery.py --workspace /path/to/boards --image candidate --report-node valve
python3 scripts/test-route-recovery.py --workspace /path/to/boards --image candidate --report-node actuator
python3 scripts/test-route-recovery.py --workspace /path/to/boards --image candidate --restart-regression --timeout 2400
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
