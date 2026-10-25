# Linked operational GSE acceptance

Use `Dockerfile.operational-sequences` with a source-only `gs`, `sdk` and `sim`
context and the SDK verifier/provenance files. The backend is explicitly compiled
with `test_fire_mode,firmware_sim`, without `hitl_mode` or `testing`. The recipe
contract rejects other policy feature combinations. The runtime controller also
requires startup flags `testing=false hitl_mode=false test_fire_mode=true` and an
ordinary qualification response from `/api/gse/status`.

`scripts/test-operational-gse.py` takes a frozen MCU cohort, a linked topology
template, an immutable image ID and a NEW evidence directory. It uses only the
four fill-system MCU images and the actual Pi Ethernet-discovery/TCP and CAN
paths. It creates a disposable synthetic account and isolated settings. No
physical device or socket is mounted, and no qualification fields or command
acknowledgements are injected.

The default scenario verifies real valve self-test and nitrogen test completion,
automatic persisted checklist receipts, then starts fill with an intentionally
unmet mass target, rejects Fire and pauses all valves closed. `--positive-fill`
uses actual MCP3564R channel1 raw conversions and real DAQ calibration/publication
to reach the target and enter Armed. `--positive-fill --fire` additionally sends
Fire only when the ordinary API interlocks allow it, observes held countdown
samples, then waits for pilot-status-driven T-plus and the cold-flow checklist
reset. These options exercise simulated outputs only.

Assumed plant inputs are explicit: nitrogen adds35psi/s with relief closed;
vent/dump removes200psi/s and dominates supply; closed valves have zero model
leak. Nitrous adds2kg/s only while the actual reported nitrous valve is open and
GSE phase is Filling. The positive fixture uses the DAQ's existing historical
raw-code formula with an affine100kg/raw-unit calibration and a zero empty
baseline; it preserves the nominal die-temperature input. ADC acquisition,
firmware filtering, network transport, ACKs, procedure thresholds and the
200-sample automatic-tare gate remain active.

This is software and transport acceptance for those assumed inputs. It does not
characterize real valves, pneumatic settling, leakage, loadcell calibration,
combustion or physical safety. Flight-launch GPS/FC-route/QD requirements need a
separate flight-mode scenario; test-fire correctly has no AV-bay requirement.
Existing HITL behavior is unchanged.

The controller saves its exact source alongside the result and fake settings.
Interrupted or incomplete attempts remain failures, and neither helper tests nor
an image build alone count as completed linked acceptance.

The source-qualified 2026-10-25 positive run is recorded in
[`qualification/operational-gse-linked-2026-10-25.json`](qualification/operational-gse-linked-2026-10-25.json).
It runs ordinary policy, reaches Armed using the actual quantized mass cutoff,
and observes the actual valve-board pilot pulse and actuator GPIO pulse before
completing all 60 virtual seconds. The controller reads qualification and checklist
receipts from their actual saved files before and after Fire. The model cannot
validate physical actuation or an independent enable key.

Two production regressions exposed by earlier incomplete runs are preserved in
that report: the Paused engine incorrectly blocked pilot-driven T-plus after
committed Fire, and the automatic cutoff used a different mass predicate from
qualification. The fixes retain the existing fill tolerance, valve ACKs, fresh
measurements and ordinary interlocks. A below-target plateau cannot qualify a
fill. DAQ initially uses countdown T0 and then accepts the actual pilot-status
T0 refinement (30ms in this run), preserving the same recording session.

## Ordinary flight variant

`Dockerfile.operational-flight` enables only `firmware_sim`; ordinary operational
policy stays active. Run the controller with `--flight --positive-fill --fire`
against a separately frozen seven-board cohort. The fixture initially supplies
raw no-fix NMEA, verifies that QD is blocked after filling, then enables a real
NMEA fix and waits for RF GPS telemetry. QD must receive its actual actuator
confirmation before the five-second model-time settlement can release queued
Launch. `--radio-trace` ends capture one second before execution ends, allowing
strict trace reconciliation.

The flight proof requires real FC received/accepted/processed counters, the actual
LaunchRequested configuration bit, a flight-mode FSM state and an unset watchdog
resume-rejection safety flag. A nonzero boot rejection is a safety failure; it is
never accepted as a harmless counter baseline. The H5 RTC/register models must
support the firmware's actual recovery-clock initialization. Canonical Armed
must reach FC's managed-state preparation hook before Launch; qualification
flags or transport ACKs alone cannot establish readiness.

A separate fresh run with `--gps-loss-during-qd` switches raw NMEA back to no-fix
after queuing Launch during QD settlement. It must observe lost RF lock and queue
cancellation without Fire. This negative run does not retry the one-way QD
operation or stand in for a fresh positive flight run. Every run uses disposable
accounts, calibration, receipt files, pressure/mass fixtures and simulated GPIO.
No physical valve, deployment or ignition test is performed.
