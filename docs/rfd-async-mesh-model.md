# RFD900x ASYNC 4.03 mesh model

`scripts/rfd_async_mesh.py` is a discrete-event model of the three RFD900x
modems (ground station, rocket RF board, RRLS) running RFDesign ASYNC 4.03, plus
the SEDS host behaviour in front of each modem. It runs in pure Python in well
under a second per simulated minute. It opens no serial port and writes nothing
to any modem.

RFDesign has not published the 4.03 MAC. The newest async manual is V1.1 (2018)
and the release notes stop at 2.69. So this is an **empirical model calibrated
against the 10-28 and 10-29 captures**, not an emulation. Every input in
`PARAMS` is labelled `measured`, `documented` (an RFDesign document or SEDS
source/config) or `inferred`. Run `python3 scripts/rfd_async_mesh.py assumptions`
for the full ledger with sources.

It does not replace the UART-hub radio medium used by the linked-firmware scenes
(`renode/peripherals/SedsRadioMedium.cs`). It answers a different question: which
MAC behaviours are consistent with the hardware evidence, and how candidate
settings would move latency under each one.

## Register numbering

ASYNC firmware does not use the SiK register map. On ASYNC, S4 is MAX_RETRIES
(unicast), S5 is GLOBAL_RETRIES (broadcast), S6 is the serial break in ms, S11 is
DESTID and S12 is TXPOWER. There is no ECC, MAVLink-framing, duty-cycle or
MAX_WINDOW register (manual V1.1 pp. 6-8). The "matched S4/S5 change" in
`GroundStation26/config/radio/rfd-async-64k-low-latency.json` sets S4 10->2,
S5 10->1 and S6 50->10. The same profile also sets the host packet gap 55->15 ms
and the RF share 35->40%.

## What is modelled

| Part | Behaviour | Label |
|---|---|---|
| Host UART | 115200 8N1 serial time per write | measured (S1) |
| Packetiser | bytes grouped until an S6 idle gap or S3 bytes | documented (manual pp. 6-7) |
| Send-time aggregation | at channel access the modem sends all queued bytes up to S3 as one packet | inferred (fits the ~3 kB RX bursts) |
| Air time | (overhead + payload x FEC factor) x 8 / air kbit/s; no ECC | S2 measured, overhead inferred |
| Channel access | carrier sense with a CCA delay, random backoff, binary growth per collision | LBT and backoff documented; all numbers inferred |
| Collisions | any overlap destroys the packet at every receiver; no capture effect | inferred |
| Half duplex | a node cannot receive while transmitting, or within the turnaround time after | inferred (single radio) |
| Modem retries | `collision` (resend up to S5 total attempts, oracle detection), `unconditional` (blind copies = round(f x S5)) or `none` | manual p. 11 says resend on collision and S4=1 disables it; detection method undocumented |
| Duplicate suppression | receivers drop repeat copies | inferred (12 of 221 RX ids repeat, consistent with SDK retries) |
| Serial buffer | 8 kB shared ingress and queue; overflow truncates frames (CRC failure) without CTS, blocks the host with CTS | size inferred; CTS: GS and RF none (documented), RRLS on (wired, but its readback says S18=0) |
| TX blocking | optional per-node intervals in which a modem cannot start a transmission | inferred stand-in for LBT lockout, local EMI or hop desync |
| GS host | 250 ms reliable copies x 18, ordered commands, 192 B batches, 55 ms gap, 15% share of 32 kbit/s, HEALTH every 500 ms | documented (GS source) |
| RF host | measured stream table, 12-slot queue, 1500 ms max age, 35% share, 250 ms x 18 reliable reports, ACK for every received command copy | documented / measured |
| RRLS relay | GS falls back after 3 s without direct RF frames and restores after 2 s; RRLS forwards while HEALTH says the direct link is lost | documented (`rrls.rs`) |

Frequency hopping is not modelled (S16 exists, but the hop pattern is unpublished).

## Calibration

`calibrate` replays the measured 4-command underglow sequence, at the measured
acceptance times, inside a 45 s window after 15 s of warm-up. It scores each
parameter set by summed |ln(sim/target)| over: median first ACK, mean UART
sends, zero-RX seconds, longest RX gap, RX mean, barometer relative delay,
barometer delivery ratio, and whether the return leg is slower than the forward
leg. It uses 16 seeds per point.

| Target (hardware) | Null: working CSMA | Fit A: TX blocking | Fit B: blind copies |
|---|---|---|---|
| First ACK 1169 / 2460 / 3126 / 4976 ms | p10/50/90 184 / 225 / 306 | 281 / 1953 / 4156 | 946 / 2189 / 4947 |
| UART sends per command 5-18, mean 11.5 | 1.3 | 8.2 | 9.7 |
| Retry exhaustion 1 of 4 | 0 | 5% | 11% |
| Zero-RX seconds 13-15 of 45 | 0 | 9.0 | 12.1 |
| Longest RX gap 4.3-5.1 s | 0.3 | 3.4 | 2.5 |
| GS RX mean 869-1068 B/s | 948 | 1313 | 1170 |
| Barometer delivered ~85% | 92% | 86% | 87% |
| Return slower than forward | yes, slightly | yes (647 vs 371 ms) | no (1001 vs 1085 ms) |
| CRC per frame 0.27% | 0.09% | 0.18% | 0.52% |
| Score (lower is better) | 13.3 | 2.15 | 2.32 |

Fit A (`examples/radio/rfd-async-mesh/fitted-tx-stall.json`): working CSMA, but
the RF modem cannot transmit 50% of the time and GS/RRLS 30% of the time, in
intervals of about 2 s (uniform 0.5-3.5 s). RF stream table scaled by 0.8.

Fit B (`fitted-unconditional-copies.json`): every broadcast packet is sent 4
times at S5=10 (fraction 0.4), with a 128-slot backoff window. RF table scaled
by 0.8.

What the fit says:

* **Working CSMA with collision-only resend is rejected.** At the measured load
  it predicts 225 ms round trips and no RX gaps, which matches the existing
  simulator and not the hardware. Some extra mechanism has to hold traffic for
  seconds.
* **Ten blind copies at S5=10 are rejected.** Ten copies of the measured load
  exceed the 64 kbit/s channel. The link collapses: no ACKs, and CRC failures
  above 10% from buffer overflow, where hardware shows 0.27%.
* **Two families fit about equally, and they disagree about the candidates.**
  TX blocking (A) is the only one that reproduces the return-leg asymmetry.
  Blind copies (B) reproduce the zero-RX seconds better.
* **Systematic misses in both fits:** the longest gap is short by 1-2 s, and GS RX
  is 10-35% high even after scaling the RF table down. So the stream table
  probably over-offers, or hardware loses more data than the model.
* **Both fits depend on send-time aggregation.** With it off, Fit B collapses
  (score 14.5) and Fit A gets worse (3.0). Fit B is also close to a capacity
  cliff: RF load x1.0 instead of x0.8 doubles its latency.
  `serial_buffer_bytes` (4-16 kB), `cts_rrls` and PHY overhead (12-48 B) move the
  scores by less than 1.

## Evaluation

`evaluate` runs each candidate with operator commands at 8 +- 2.5 s spacing for
165 s, 16 seeds, about 290 commands. It reports the first-ACK distribution, ACK
loss (exhausted and never ACKed), exhaustion, uplink copies, unique downlink
telemetry goodput and relay duty. The rank cost is p90 first ACK plus 10 s per
unit of exhaustion rate.

```
python3 scripts/rfd_async_mesh.py evaluate --base examples/radio/rfd-async-mesh/fitted-tx-stall.json \
    --profile examples/radio/rfd-async-mesh/gs-rfd-async-64k-low-latency.json --seeds 16 --jobs 6
```

| Candidate | A: p50 / p90 ms | A: exhausted | B: p50 / p90 ms | B: exhausted | Copies A / B |
|---|---|---|---|---|---|
| baseline | 1797 / 4118 | 6.2% | 9200 / 24325 | 73% | 7.7 / 15.8 |
| GS_RELIABLE_RETRANSMIT_MS=1000 | 1503 / 4130 | 0% | 7068 / 18593 | 5.1% | 2.4 / 7.8 |
| GS_RRLS_ROLE=off | 1279 / 3592 | 4.8% | 2382 / 6942 | 24% | 6.6 / 10.9 |
| low-latency profile (S4=2, S5=1, S6=10) | 1447 / 3525 | 4.8% | **229 / 421** | **0%** | 6.8 / 1.4 |
| primary share 25% | 1562 / 3837 | 4.8% | 9141 / 31837 | 70% | 7.3 / 15.4 |
| relay loss 6 s (hysteresis) | 1474 / 3751 | 4.1% | 5120 / 9484 | 61% | 7.2 / 15.4 |

Under Fit B the baseline gets worse the longer it runs. Relay fallback adds
traffic, which lengthens the gaps that trigger it. The 45 s replay showed 11%
exhaustion; 165 s of steady commanding shows 73%.

Ranking. Under A, the spread between candidates (p90 3.5-4.1 s) is about the
same as seed noise, so the order within A is not robust:

1. **Low-latency profile**: decisive if B is true (p50 229 ms, 96% within
   500 ms); neutral if A is true. It needs matching RF firmware for the 15 ms gap.
2. **RRLS off**: second under both fits. It removes HEALTH and relay traffic.
   First confirm that RF accepts unenveloped uplinks.
3. **Retransmit 1000 ms**: removes exhaustion under both fits and cuts uplink
   copies by about 3x. Latency is roughly unchanged. Never use it in the
   test-fire configuration: it is a global setting.
4. **Relay hysteresis 6 s**: modest gain under both; slower real failover.
5. **Primary share 25%**: no clear effect.
6. **Baseline**.

## Hardware test plan

Each test discriminates between Fit A and Fit B. Use the existing passive GS
strace capture and the 4-command underglow sequence.

1. **S5-only A/B (one matched write on all three modems).** Set S5 10->1 and
   change nothing else, then repeat the underglow capture. Fit B predicts a
   first-ACK p50 near 250 ms, about 2 sends per command and no zero-RX seconds.
   Fit A predicts no change (p50 1.5-2 s, about 9 zero-RX seconds). One
   five-minute session decides between them. Read ATI and S1-S18 on all three
   modems first (worklist R2), especially the never-read RF modem's S5, S11 and
   S17 (LBT_RSSI).
2. **Five-minute passive baseline, no writes.** Fit B predicts drift: exhaustion
   and relay duty rising over minutes, RRLS forwarding counters climbing. Fit A
   predicts stationary behaviour, with about 6% exhaustion and about 19% relay
   duty. Record `/api/rrls` status once per second alongside.
3. **RF-side split (R4 counters, or a passive logic-analyser tap on the RF
   modem's UART).** Fit A predicts that RF writes reach the modem promptly and
   leave its antenna 0.5-3.5 s later, in bursts of up to S3 bytes. It also
   predicts that forward commands reach RF in about 370 ms. If RF's modem
   egress is prompt but GS sees gaps, both fits are wrong, and the problem is
   at the GS modem or on the air path.

## Biggest uncertainties

* How a broadcast sender detects a collision, and what S5 actually counts.
  Test 1 settles this.
* What causes the TX-blocking intervals in Fit A (LBT threshold, EMI in the AV
  bay, hopping) and whether they depend on traffic. If they depend on traffic,
  removing RRLS traffic would help more than the model shows.
* Send-time aggregation: both fits need it.
* The RF offered load: the stream table is reconstructed from decoded types,
  and both fits needed it scaled to 0.8.
* RTS/CTS on RRLS: the user says it is wired, but the readback says S18=0. It has
  no effect at the RRLS traffic level.

## Groundstation changes the model suggests (not made here)

* Surface "unconfirmed" and re-publish desired state when the GS horizon expires
  (worklist R1). The model predicts that ACKs arriving after exhaustion are
  routine under both fits.
* Let the primary use 25% when no relay has been seen (P1). The model shows
  little latency effect. The benefit is capacity headroom.
