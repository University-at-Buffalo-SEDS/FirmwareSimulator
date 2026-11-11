# RFD air scheduling: turn-taking proposal and TDM assessment

Status: proposal. Nothing here is implemented in firmware or in the station. The model results come from
`tests/radio-medium/TurnTaking.cs` on the bench-calibrated medium (`docs/rfd-radio-model-audit.md`).

## Why

The RFD900x2 modems run the ASYNC firmware: "There is no transmission coordination or synchronisation of the
network" (RFD X Series datasheet V2.1, §5.3, p. 13). On the 2026-11-10 bench pair, traffic in one direction
works: 0.5% loss at 20 frames/s, 6% at 40. When both ends transmit at 20 frames/s, 97-99% is lost, and
listen-before-talk does not help. The calibrated model reproduces this: a modem cannot hear while it
transmits, and its carrier sense is sampled before its own transmit setup.

## Model result

Synthetic traffic, sized from the faithful runs:

- RF downlink: 28 frames/s, 8 of them reliable, with end-to-end retries every 550 ms.
- Station: an ACK per reliable frame, heartbeat at 2/s, time announce at 1/s, recovery queries, and an operator
  command every 5 s that the AV bay confirms reliably.
- RRLS: presence once a second.

| | Uncoordinated (today) | Turn-taking | Target |
| --- | --- | --- | --- |
| End-to-end ACKs completed | 13.8% (7.7 attempts) | 100% (1.01 attempts) | all |
| Time announce one-way latency, p95 (clock offset) | 12.9 s | 265 ms | < 500 ms |
| Operator confirmation p50 / p95 | 6.6 s / 12.3 s, 5 of 12 missing | 287 / 485 ms, none missing | < 1 s |
| On air | 5.1 KB/s | 1.9 KB/s | < 3 KB/s |
| Air packets/s (RF / GS / RRLS) | 38 / 25 / 1 | 5 / 5 / 1 | about 5 per node |

Both rows are asserted in every `tests/radio-medium` run.

## Proposed scheduling (held for approval)

### RF board (RFBoard26 radio egress)

1. Write the radio UART once every 200 ms: all frames pending for the air, coalesced into one write. One write is
   one air packet when it fits MAX_DATA (3968 B) and contains no gap of S6 (10 ms) or more. Never write between
   turns.
2. Retransmissions of reliable frames join the next turn.
3. Keep the downlink under about 1.5 KB/s, so that one turn's packet stays under about 300 B (40 ms on air at
   64 kbit/s), which leaves the rest of the 200 ms for the station and the RRLS.
4. Decimation on the RF path while relaying (proposal (d), pending) fits inside this cadence.

### Station (GroundStation26 radio_io)

1. Transmit only in the gap after an RF packet: once a frame from the AV bay has arrived and no further AV-bay
   byte has arrived for 20 ms, write everything pending (ACKs, heartbeat, time announce, operator commands,
   recovery queries) as one coalesced write. At most one write per RF packet.
2. Fallback when the AV bay is silent for 1 s (RF off, out of range, booting): free-run at 2 writes/s, so that
   heartbeats, Abort and commands still go out.
3. Abort is never held behind the turn. It is written at once, in addition to joining the next turn. One
   unaligned packet costs at most one collision, against a 550 ms retry.
4. Operator-initiated traffic is never throttled; it only waits for the next turn, at most 200 ms.

### RRLS

1. At most one write a second, at least 60 ms after an RF packet ends (after the station's turn).
2. While relaying, the RRLS takes the station's slot towards the AV bay and the station stays quiet towards
   the RRLS except in its own gap. This needs its own model run before adoption.

Cost: radio_io scheduling and the turn detector in GroundStation26 (radio agent review); one egress scheduler in
RFBoard26. RF has 5.2 KB of flash free at 7067e57 (2.8 KB on the simulation build). No SEDSnet change.

## TDM or multipoint firmware instead of ASYNC?

Sources, all RFDesign:

- [DS] RFD X Series datasheet V2.1, §5.1-5.3, pp. 12-13;
- [MP] Multipoint Firmware User Manual (last update 06/05/2021), pp. 3, 9, 11, 13;
- [P2P] Point-to-Point/SiK Firmware User Manual V1.5, pp. 19-20.

What the documents say:

- **ASYNC** is "best suited to systems with independent coordination or low data polling rates and tolerance to
  data loss/errors, e.g. remotely deployed temperature sensor that reports every ten minutes" ([DS] §5.3). Our
  continuous bidirectional traffic is the case it is not meant for. The bench collapse is the consequence.
- **Multipoint** gives "Coordinated transmission to minimises interference and collision and maximises data
  throughput" ([DS] §5.2). One node "assumes a master role to control the timeslot distribution" ([MP] p. 3), and
  "The total amount of data is split evenly between all the nodes on the network" ([MP] p. 9).
  - At 64 kbit/s the measured total is about 43 kbit/s for 3 nodes, about 14.3 kbit/s each ([MP] p. 13).
  - A relay is supported, but only as the master: "all nodes must be able to see the master and ... Vehicle1 will
    always send messages via the relay even if it is in range to receive them direct from the GCS" ([MP] p. 11).
  - "Use of flow control is strongly recommended to prevent data loss" ([MP] p. 3).
  - "after receiving data it is not possible to identify which node it came from" ([MP] p. 3).
- **Point-to-point (SiK)** is TDM between exactly two modems ([DS] §5.1). It cannot include the RRLS.
  MAX_WINDOW "can decrease latency but may reduce the effective throughput" ([P2P] p. 20), and LBT is an
  "Experimental feature not fully tested" ([P2P] p. 20).

Assessment:

- **Multipoint would remove the dominant failure,** both ends transmitting at once, by construction. It also
  fits the measured demand: the RF downlink is about 1.5 KB/s (12 kbit/s) against about 14.3 kbit/s per node
  at 64 kbit/s.
- **Its costs:**
  1. **Fixed per-node shares.** The RF board gets a third of the air even when the station and the RRLS are idle,
     and none of the station's or RRLS's unused share. There is no headroom for bursts (the readback after an
     Abort, discovery) at 64 kbit/s; 125 kbit/s gives about 26 kbit/s per node ([MP] p. 13) but shorter range
     ([MP] p. 9).
  2. **A master the others must hear.** If the station is master and the AV bay loses it (the hidden-node and
     far-range cases this project fights), the AV bay loses its timeslots: a dependency ASYNC does not have. If
     the RRLS is master and relay, all AV-bay traffic goes through it, even when the station hears the AV bay
     directly ([MP] p. 11). The application-level relay in GroundStation26 (`rrls.rs`) would be replaced, not
     reused.
  3. **Engineering.** Flash multipoint on all three modems. Rewrite the modem check and profiles in
     GroundStation26 and RFBoard26 for the multipoint register map (NODEID, NODEDESTINATION, master settings),
     and enable flow control, which needs RTS/CTS wiring the GS and RF UARTs do not have today. Then re-validate
     the whole radio stack.
  4. **Latency.** A node waits for its slot. With three nodes and MAX_WINDOW 80 ms (the multipoint default), a
     reply can wait up to about two other windows, comparable to the 200 ms turn proposed above.
- **Recommendation.** First implement the turn-taking schedule above on ASYNC. It reaches the targets in the
  model, keeps the RRLS relay design, and needs no modem or wiring change. Bench-test multipoint on the Pi and Mac
  pair (with `scripts/rfd_link_test.py` and a third modem) as the fallback if turn-taking on hardware misses the
  targets. That A/B is a hardware request.
