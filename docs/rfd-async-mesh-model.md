# RFD900x ASYNC 4.03 mesh model

`scripts/rfd_async_mesh.py` is a discrete-event model of the three RFD900x
modems (ground station, rocket RF board, RRLS) running RFDesign ASYNC 4.03,
together with the SEDS host behaviour in front of each modem. It runs in pure
Python in well under a second per simulated minute. It opens no serial port
and writes nothing to any modem.

RFDesign has not published the 4.03 MAC: only the 4.03 binary is on the
firmware index, the newest async manual is V1.1 (2018), and the release notes
stop at 2.69. So this is an **empirical model calibrated against the 10-28
and 10-29 captures**, not an emulation. Every input in `PARAMS` is labelled
`measured`, `documented` (an RFDesign document, or SEDS source/config) or
`inferred`. Run `python3 scripts/rfd_async_mesh.py assumptions` for the full
ledger with sources.

It does not replace the UART-hub radio medium used by the linked-firmware
scenes (`renode/peripherals/SedsRadioMedium.cs`). It answers a different
question: which MAC behaviours are consistent with the hardware evidence,
and how candidate settings would change latency under each one.

## Documentation check (2026-11-07)

Sources, all official RFDesign files except the last:

* **[M]** RFD900x Asynchronous User Manual V1.1, 09/07/2018, pages as printed:
  https://files.rfdesign.com.au/Files/documents/RFD900x%20Asynchronous%20User%20Manual%20V1.1.pdf
* **[RN]** Asynchronous release notes 2.43-2.69:
  https://files.rfdesign.com.au/Files/firmware/RFD%20X%20modems%20Asynchronous%20release%20notes.txt
* **[DS]** X Series datasheet 2.1 and uX Series datasheet 2.2:
  https://files.rfdesign.com.au/Files/documents/XseriesDatasheet2.1.pdf,
  https://files.rfdesign.com.au/Files/documents/uXseriesDatasheet2.2.pdf
* **[FAQ]** Modem Support FAQs V1.2 (2021):
  https://files.rfdesign.com.au/Files/documents/Modem%20Support%20FAQs%20V1.2.pdf
* **[TR]** RFD Modem Tools release notes:
  https://files.rfdesign.com.au/Files/tools/RFD%20Modem%20Tools%20Release%20Notes.txt
* **[FW]** Firmware index, which lists `RFDAsync V4.03 rfd900x2.gbl` and
  `rfd900ux2.gbl` and no 4.x notes: https://files.rfdesign.com.au/firmware/
* **[SUP]** RFDesign support reply quoted on ArduPilot Discourse (second-hand):
  https://discuss.ardupilot.org/t/rfd900x-asynchronous-mesh-firmware-multi-vehicle-connection-issue/77020

| Model assumption | Verdict | Evidence |
|---|---|---|
| Async register map, not SiK: S3 MAX_DATA, S4 MAX_RETRIES, S5 GLOBAL_RETRIES (broadcast), S6 SER_BREAK_DETECT_MS, S11 DESTID, S12 TXPOWER; no ECC, duty-cycle or MAX_WINDOW register | **Confirmed** | [M] Table 3-2, pp. 6-8 |
| S5 counts total attempts, so S5=1 disables resend | **Contradicted** | [M] p. 7: S5 range 0-100, while S4 has minimum 1 and "S4=1" disables resend (p. 11). S5 is now read as resends after the first send (attempts = 1 + S5), and S5=0 is allowed. The old reading is kept as `s5_counts=attempts` |
| S4 applies only to unicast | **Undocumented** | [M] says only "Maximum number of retries"; the p. 11 disable example uses S4 with no DESTID qualifier. Still inferred. No candidate changes S4 |
| Resend on collision | **Confirmed** | [M] p. 11: "whenever a package collision happens, the radio will automatically resend it" |
| How a broadcast sender detects a collision | **Undocumented**; the documents imply an ACK | [RN] 2.45: air rates "no acknowledgement" / "with acknowledgement", an ack-overlap fix; 2.59: "ack back on port received on". No single receiver can ACK a broadcast. Collision-detected resend of broadcasts is therefore an oracle. The doc-consistent reading is blind S5 copies, and Fit A was refitted on that basis (below) |
| Listen-before-talk with random backoff | **Confirmed** | [DS] §5.3 "transmit ... if an RF channel is free", "random back off"; [M] S17 LBT_RSSI, default 0, "shouldn't be changed", must match; [SUP] "listen before talk and random backoff ... some nodes ... locked out by other nodes regularly transmitting", "not designed for continous messaging e.g. telemetry", "precisely control the timing of data packets from each node" |
| CCA threshold, backoff slot and window | **Undocumented** | none; still inferred and swept |
| Fit A TX-blocking caused by LBT lockout, EMI or hop desync | Lockout **supported**; hop desync **contradicted** | [SUP] describes lockout. [M] pp. 3, 10: "asynchronous non-hopping mesh", "nodes don't require synchronisation". The hop explanation is removed. S16 NUM_CHANNELS exists, but its async meaning is undocumented |
| Modems forward/route on their own | **Documented as opt-in**: a fixed table only | [M] §4.1: `AT+Fx=Src,Dst`, up to ten entries, `AT+F?` shows the table; [RN] 2.45 forwarding, 2.47 queue, 2.69 `65535` = forward to all and out of serial. A table entry would put every relayed packet on the air twice beside the software RRLS relay. Not modelled; the GS check now reads `AT+F?` and warns on entries |
| Serial buffer 8 kB | **Undocumented** for 4.03 | [RN] 2.45 heap 8 kB, 2.47 variable buffers; [M] `AT+F?`: lowering MAX_DATA gives more forwarding buffer (this may be why S3=3968). Still inferred |
| CTS on RRLS | **Contradicted** (model said on) | [M] p. 7: S18 enables RTS/CTS (async default 1); the RRLS readback is S18=0. `cts_rrls` is now False. With no CTS wired on GS/RF, S18 must be 0 there too, or an unwired CTS input can hold the modem's output |
| Settings need `AT&W` then `ATZ` | **Confirmed** | [M] p. 5 and every example. Exception: [RN] 2.45 "Destination ID takes affect without reboot" |
| Reboot time after `ATZ` | **Undocumented** | none; GS waits 4 s |
| `+++` guard | **Confirmed** (before); after `+++` undocumented | [M] p. 5: at least one second after data before `+++`; in command mode "the RFD900x modem will stop displaying incoming data". [RN] 2.69: three `+` in quick succession accepted, and a fix for AT mode entered from the data stream. [TR] 2.45/2.46: a 50 ms gap between `+` is not needed |
| Air rate and other parameters must match | **Confirmed** | [M] Table 3-2 "Must be the same at both ends": S2, S3, S4, S5, S7, S8, S9, S14-S17, S19 yes; S1, S6, S10-S13, S18, S20-S22 no. Whether an S4/S5 mismatch breaks the link is undocumented |
| Slower, more robust air rates exist | **Contradicted for async** | [M] p. 6: async S2 accepts 64, 125, 250, 500, 830, with minimum 64, and p. 9 says the air rate should be below the UART rate (115200), which leaves only 64. [DS] sensitivity at 1e-5 BER: X 64k -101 dBm vs 56k/12k -102 dBm; uX -98 vs -99. Even where a slower rate exists, it buys about 1 dB |
| Remote AT (RT) commands | **Not supported** | [M] pp. 4-5: "RT commands are not currently supported in the asynchronous firmware" |
| EEPROM write endurance | **Undocumented** | no RFDesign figure. GS bounds saves to 3 per process and writes only on mismatch |
| No FEC (factor 1.0) | **Confirmed** | [M] Table 3-2 has no ECC register |
| 4.03 behaves like V1.1/2.69 | **Undocumented** | [FW] has only the binary. [DS] §4.5: async does not interoperate between V1 and V2 hardware, and only matching firmware versions are tested together |

## Register numbering

ASYNC firmware does not use the SiK register map. On ASYNC, S4 is
MAX_RETRIES (minimum 1; 1 disables resend), S5 is GLOBAL_RETRIES for
broadcast (0-100; resends after the first), S6 is the serial break in ms
(local to each node), S11 is DESTID and S12 is TXPOWER ([M] Table 3-2).
The 4.03 map itself is unpublished. The GS modem check therefore writes a
register only when `ATI5` gives it the V1.1 name.

## What is modelled

| Part | Behaviour | Label |
|---|---|---|
| Host UART | 115200 8N1 serial time per write | measured (S1) |
| Packetiser | bytes grouped until an S6 idle gap or S3 bytes; S6 per node | documented ([M] pp. 6-7) |
| Send-time aggregation | at channel access the modem sends all queued bytes up to S3 as one packet | inferred (fits the ~3 kB RX bursts) |
| Air time | (overhead + payload) x 8 / air kbit/s; no ECC | S2 measured, overhead inferred |
| Channel access | carrier sense with a CCA delay, random backoff, binary growth per collision | LBT and backoff documented; all numbers inferred |
| Collisions | any overlap destroys the packet at every receiver; no capture effect | inferred |
| Half duplex | a node cannot receive while transmitting, or within the turnaround time after | inferred (single radio) |
| Modem retries | `unconditional` (1 + round(f x S5) blind copies; doc-consistent for broadcast), `collision` (up to 1 + S5 sends, oracle detection) or `none`; S5 per node | resend documented; detection inferred |
| Duplicate suppression | receivers drop repeat copies | inferred |
| Serial buffer | 8 kB shared ingress and queue; without CTS an overflow truncates frames (CRC failure), with CTS the host is blocked | size inferred; CTS off on all three (GS/RF unwired, RRLS S18=0) |
| TX blocking | optional per-node intervals in which a modem cannot start a transmission | inferred stand-in for LBT lockout or EMI |
| GS host | 250 ms reliable copies x 18 (or adaptive RTO), ordered commands, 192 B batches, 55 ms gap, 15% share of 32 kbit/s, HEALTH every 500 ms; optional backlog cap and quiet-on-loss | documented (GS source) / candidate |
| RF host | measured stream table, 12-slot queue, 1500 ms max age, 35% share, 250 ms x 18 reliable reports, ACK for every received command copy | documented / measured |
| RRLS relay | GS falls back after 3 s without direct RF frames and restores after 2 s; RRLS forwards while HEALTH says the direct link is lost | documented (`rrls.rs`) |

## Calibration

`calibrate` replays the measured 4-command underglow sequence, at the
measured acceptance times, inside a 45 s window after 15 s of warm-up. It
scores each parameter set by the summed |ln(sim/target)| over: median first
ACK, mean UART sends, zero-RX seconds, longest RX gap, RX mean, barometer
relative delay, barometer delivery ratio, and whether the return leg is
slower than the forward leg. It uses 16 seeds per point. These are the
refitted values after the documentation corrections:

| Target (hardware) | Null: working CSMA | **Fit A**: TX blocking + blind copies | **Fit B**: blind copies | A-oracle (pre-11-07 Fit A) |
|---|---|---|---|---|
| First ACK 1169 / 2460 / 3126 / 4976 ms | p10/50/90 170 / 239 / 327 | 512 / 2004 / 4258 | 946 / 2189 / 4947 | 281 / 1953 / 4156 |
| UART sends per command 5-18, mean 11.5 | 1.4 | 8.8 | 9.7 | 8.2 |
| Retry exhaustion 1 of 4 | 0 | 8% | 11% | 5% |
| Zero-RX seconds 13-15 of 45 | 0 | 11.6 | 12.1 | 9.0 |
| Longest RX gap 4.3-5.1 s | 0.3 | 3.2 | 2.5 | 3.4 |
| GS RX mean 869-1068 B/s | 919 | 1207 | 1170 | 1313 |
| Barometer delivered ~85% | 92% | 86% | 87% | 86% |
| Return slower than forward | yes, slightly | yes (1096 vs 497 ms) | no (1001 vs 1085 ms) | yes (647 vs 371 ms) |
| CRC per frame 0.27% | 0.1% | 0.3% | 0.5% | 0.2% |
| Score (lower is better) | 13.3 | **2.01** | 2.32 | 2.15 |

**Fit A** (`examples/radio/rfd-async-mesh/fitted-tx-stall.json`): working
carrier sense, broadcast resend as blind copies (f=0.1, so 2 copies at
S5=10). The RF modem cannot transmit 60% of the time and GS/RRLS 30% of the
time, in intervals of about 1.5 s (uniform 0.4-2.6 s). The RF table is scaled
by 0.8. This fits best, it is consistent with the documents, and it is the
only family that keeps the return-leg asymmetry.

**Fit B** (`fitted-unconditional-copies.json`): 4 blind copies of every
broadcast at S5=10 (f=0.3 with the corrected S5 meaning; the same behaviour
as the old f=0.4), a 128-slot backoff window, and the RF table scaled by 0.8.

**A-oracle** (`fitted-tx-stall-collision-oracle.json`) is the previous Fit A,
with collision-detected broadcast resend. It is kept only as a sensitivity
case.

What the fits say, unchanged in substance from the first fit:

* **Working CSMA with no extra mechanism is rejected.** It predicts 239 ms
  round trips and no RX gaps.
* **Ten blind copies at S5=10 are rejected.** They exceed the 64 kbit/s
  channel.
* **TX blocking (A) and blind copies (B) both fit.** They agree on the
  ranking of modem settings (below) and disagree on the size of the gain.
* **Both fits depend on send-time aggregation**, and both miss the longest
  gap by 1-2 s.

## Evaluation and ranking

`evaluate` runs each candidate with operator commands at 8 +- 2.5 s spacing
for 165 s, 32 seeds, about 580 commands. The rank cost is the p90 first ACK
plus 10 s per unit of exhaustion rate. "P" is the phase-1 change: GS and
RRLS modems at S5=0 and S6=20, with the RF modem left at S5=10 and S6=50.
"P2" sets all three modems.

```
python3 scripts/rfd_async_mesh.py evaluate --base examples/radio/rfd-async-mesh/fitted-tx-stall.json \
    --profile examples/radio/rfd-async-mesh/gs-rfd-async-64k-low-latency.json \
    --config examples/radio/rfd-async-mesh/phase1-candidates.json --seeds 32 --jobs 6
```

| Candidate | A: p50 / p90 ms, exhausted | B: p50 / p90 ms, exhausted | A-oracle: p90, exhausted | Uplink copies A / B |
|---|---|---|---|---|
| baseline (S5=10, S6=50 everywhere) | 2547 / 5805, 18.9% | 8569 / 19333, 67.5% | 4180, 7.1% | 10.5 / 15.3 |
| **P: GS+RRLS S5=0, S6=20** | 1805 / 4163, 7.7% | 726 / 1217, 0% | 4040, 6.2% | 8.3 / 3.5 |
| P, S5=0 only | 1939 / 4380, 9.1% | 734 / 1348, 0% | 4069, 6.0% | 8.6 / 3.6 |
| P, S6=20 only | 2321 / 5082, 14.8% | 8372 / 20691, 71% | 4248, 8.3% | 9.7 / 15.8 |
| P with S6=10 / S6=30 | p90 4551 / 4267 | p90 1198 / 1320 | 3599 / 3656 | |
| P + adaptive retry (needs SDK) | 2196 / 5372, 0% | 696 / 1482, 0% | 4406, 0% | 1.6 / 1.2 |
| P + quieter RRLS (HEALTH 1 s) | 1883 / 4291, 7.9% | 694 / 1262, 0% | 3866, 5.9% | 8.4 / 3.5 |
| P + GS quiet-on-loss | 1773 / 4003, 5.9% | 749 / 1275, 0% | 3714, 4.8% | 7.9 / 3.5 |
| P + GS batch 512 B (aggregation) | 1897 / 4536, 10.5% | 701 / 1275, 0% | 4138, 7.6% | 8.4 / 3.5 |
| P + GS backlog cap 512 B | identical to P | identical to P | identical | |
| **P2: all three S5=0, S6=20** | 1231 / 2934, 0.3% | 265 / 460, 0% | 4028, 5.7% | 6.0 / 1.6 |
| P2 + adaptive retry | 1470 / 3593, 0% | 266 / 452, 0% | 4700, 0% | 1.4 / 1.1 |
| P2 + RF ACK piggyback | 1279 / 3056, 1.4% | 265 / 459, 0% | 3611, 4.5% | |
| old low-latency profile (S4=2, S5=1, S6=10, gap 15, RF 40%) | 1209 / 2982, 1.5% | 233 / 425, 0% | 3845, 6.4% | |
| RRLS off | 1930 / 4068, 7.6% | 2512 / 6201, 23.9% | 3530, 4.1% | |
| GS retransmit 1000 ms (global) | 2151 / 5320, 0% | 5852 / 16822, 4.3% | 4142, 0% | |

Ranking, with the reasons:

1. **S5=0 on every modem (one retry layer), with S6=20.** It is the best
   real change under A and B. GS and RRLS alone (P) cut p90 by 28% under A
   and by 94% under B. Adding the RF modem (P2) gives a further 30% under A
   and 60% under B. A-oracle is flat (within seed noise) for every modem
   setting, so nothing is lost if that fit is true. It needs no transport
   change, so the negotiated budget tuple stays the same. S6=10, 20 and 30
   are within noise of each other; 20 keeps the most margin against a frame
   being split.
2. **Adaptive, RTT-derived retry for commands.** It removes exhaustion under
   all three fits and cuts uplink copies about 5x. Its p90 is equal or
   slightly worse. It needs a per-side SDK retry policy
   (`GroundStation26/docs/backend/rfd-modem-check.md`), because
   `GS_RELIABLE_RETRANSMIT_MS` is global and also governs the fill link.
3. **GS quiet-on-loss** (drop filler and half the heartbeats after 1 s
   without RF frames) and **quieter RRLS**: small gains under A and A-oracle,
   none under B. Both are within about 1.5x seed noise. Not implemented.
4. **No gain:** a GS backlog cap or pacing closer to the air rate (GS share
   pacing at 15-25% of 32 kbit/s never fills the modem, so the cap never
   engages), larger GS batches (the modem already aggregates at send time),
   and RF ACK piggybacking.
5. **Unicast DESTID with modem retries** is not viable here. DESTID is a
   single per-modem destination; changing it costs a command-mode entry,
   with at least one second of guard; RRLS must hear both GS HEALTH and the RF
   downlink; and whether non-addressed nodes still pass unicast data to their
   host is undocumented. Fit A-oracle stands in for ACKed unicast resend,
   and it does not beat blind broadcast with S5=0.

Best profile: `GroundStation26/config/radio/rfd-async-64k-one-retry-layer.json`
(S5=0, S6=20, everything else unchanged). It is **opt-in**. S5 must match on
every node (manual Table 3-2), so the GS startup check enforces the deployed,
matched settings by default (S4=10, S5=10, S6=50). It writes link-wide
registers only for a profile marked as rolled out to all nodes; the RF board
enforces the same selected profile in phase 2. Select it only after the
hardware A/B (test 1 below), and set it on GS, RRLS and RF together. The
mixed "P" rows above show what a partial rollout would give; they are not a
recommended state.

## Mode switching on an unstable link: not recommended

`python3 scripts/rfd_async_mesh.py rendezvous` models a GS and an RF node that
each run the same deterministic rule. On the primary rate, if nothing is
heard for N s (3 s), the node writes the fallback rate and reboots. On the
fallback rate, it stays at least H s (20 s), then writes the primary rate
and reboots. H > 2N + 2 reboots means a node that switched alone is always
joined by its peer before it leaves, so the pair can never stay split. The
longest split in every run is at most N plus one reboot. Fades are Poisson
with random depth and duration, and some hit only one direction.

| Scenario (16 h simulated each) | Fixed 64k availability | Fallback availability | EEPROM writes/h |
|---|---|---|---|
| default: 6 dB margin, 1 fade/min, 0-20 dB, 6 s | 93.4% | 90.7% | 89 |
| 0.5 dB margin | 91.1% | 87.6% | 116 |
| 0.5 dB margin, 4 fades/min | 67.6% | 59.7% | 312 |
| 2 dB margin, 80% one-way fades, 1 s reboot | 91.3% | 88.7% | 110 |
| contrived best case: 0.5 dB margin, fades only 0-2 dB, 60 s long, 1 s reboot | 72.9% | 82.0% | 179 |

The switch gains only in the contrived case, where every fade is shallower
than the 1 dB a slower rate buys. Even there it costs about 180 EEPROM
writes per hour, and it would require the rocket's RF board to rewrite and
reboot its modem in flight. The documents add three harder constraints:

* async S2 has no rate below 64 ([M] p. 6), and the air rate must stay
  below the UART rate ([M] p. 9), so there may be nothing to fall back to;
* S2 must match on every node, and RT commands are unsupported, so no node
  can move another;
* every switch is an `AT&W` plus `ATZ`.

Verdict: do not switch air rates. The robust "mode switch" that the model
supports is on the host side, needs no rendezvous and cannot strand the link:
S5=0 everywhere, then adaptive retry, then shedding offered load when the
link is lost (rank 3). Leave TX power (S12, local) at the legal maximum the
operator chooses. The check reports S12 but never writes it.

## Hardware test plan

Each test discriminates between Fit A and Fit B. Use the passive GS strace
capture and the 4-command underglow sequence.

1. **Matched A/B of the opt-in profile.** In a quiet window, write S5=0 and
   S6=20 on all three modems (GS and RRLS through the check with
   `GS_RADIO_PROFILE` plus `GS_RFD_PROFILE_ROLLED_OUT`, and RF with a one-time
   matched write or phase-2 firmware). Repeat the underglow capture. Fit B
   predicts a first-ACK p50 near 270 ms; Fit A predicts about 1.2 s with no
   exhaustion; A-oracle predicts no change. To revert, select the default
   profile on all three.
2. **Default-profile startup check.** With the default profile, the check
   should report `matched` on all stations and write nothing. Any write it
   makes is a drift repair to note.
3. **Five-minute passive baseline.** Fit B predicts drift: exhaustion and
   relay duty rising over minutes. Fit A predicts stationary behaviour.
4. **RF-side split** (the RF TX timing counters already in board
   diagnostics, or a logic-analyser tap on the RF modem's UART). Fit A
   predicts that RF writes reach the modem promptly and leave the antenna
   0.4-2.6 s later.

## Biggest uncertainties

* What S5 means on 4.03, including whether S5=0 really means "no resend".
  Test 1 and the readback settle the value; the latency change settles the
  meaning.
* What causes the TX-blocking intervals in Fit A. [SUP] names LBT lockout by
  nodes that transmit regularly. If that is the cause, the blocking depends
  on traffic, and cutting offered load (ranks 2-3) helps more than the
  exogenous model shows.
* Send-time aggregation: both fits need it.
* The RF offered load: the stream table is reconstructed, and both fits
  needed it scaled to 0.8.
