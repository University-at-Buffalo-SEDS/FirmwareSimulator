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
