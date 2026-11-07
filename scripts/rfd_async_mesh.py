"""Empirical discrete-event model of three RFD900x modems on ASYNC 4.03.

RFDesign has published no description of the 4.03 MAC. This is therefore not
an emulation of the firmware. It is a parameterised model whose every input is
labelled in ``PARAMS`` as one of:

* ``measured``   read from SEDS hardware captures or live modem readback;
* ``documented`` stated by an RFDesign document or by SEDS source/config;
* ``inferred``   a modelling choice with no direct evidence, to be calibrated
                 or swept, never quoted as a firmware fact.

Simulation only. Nothing here opens a serial port or talks to a modem.
"""
from __future__ import annotations

import argparse
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
import heapq
import json
import math
from pathlib import Path
import random
import statistics
import sys

MEASURED, DOCUMENTED, INFERRED = 'measured', 'documented', 'inferred'
HW = '.coordination/hardware-20261028/'
MANUAL = 'RFD900x Asynchronous User Manual V1.1 (2018)'
NODES = ('gs', 'rf', 'rrls')


@dataclass(frozen=True)
class Param:
    value: object
    kind: str
    source: str


def _p(value, kind, source):
    return Param(value, kind, source)


PARAMS = {
    # Modem S-registers. Async numbering, not SiK: S4 is MAX_RETRIES, S5 is
    # GLOBAL_RETRIES (broadcast); there is no ECC/duty-cycle/MAX_WINDOW register.
    'uart_baud': _p(115200, MEASURED, HW + 'rrls-live-radio-settings-attempt2.json S1=115'),
    'air_kbps': _p(64, MEASURED, 'RRLS readback S2=64; GS cached 10-20 read; RF never read'),
    'max_data': _p(3968, MEASURED, 'RRLS readback S3=3968 (manual default 4068, p6)'),
    'max_retries': _p(10, MEASURED, 'RRLS readback S4=10; unicast only, unused while DESTID is broadcast'),
    'global_retries': _p(10, MEASURED, 'RRLS readback S5=10; broadcast retries, manual p6-7'),
    'serial_break_ms': _p(50, MEASURED, 'RRLS readback S6=50; manual p6-7: bytes grouped until S6 gap or S3'),
    'broadcast_dest': _p(True, MEASURED, 'RRLS readback S11=32768 (broadcast from V2.45F, manual p7); RF unread'),
    'cts_gs': _p(False, DOCUMENTED, 'GS comms.json raw_uart; worklist R6: RTS/CTS wired only on RRLS'),
    'cts_rf': _p(False, DOCUMENTED, 'RFBoard26 main.c:320 disables UART hardware flow control'),
    'cts_rrls': _p(True, INFERRED, 'wired per worklist R6, but RRLS readback S18=0; set False to follow readback'),
    # Physical layer and MAC.
    'fec_factor': _p(1.0, DOCUMENTED, 'async S-register table has no ECC register (manual p6-8)'),
    'phy_overhead_bytes': _p(24, INFERRED, 'preamble+sync+header+CRC; RFD gives no byte counts'),
    'turnaround_ms': _p(1.0, INFERRED, 'release notes v2.45 add a 1 ms delay for ack overlap'),
    'carrier_sense': _p(True, DOCUMENTED, 'datasheet 2.1 s5.3 "if an RF channel is free"; RFD support: LBT + random backoff'),
    'cca_delay_ms': _p(2.0, INFERRED, 'time before a started transmission is sensed by others'),
    'backoff_slot_ms': _p(1.0, INFERRED, 'datasheet s5.3 says random backoff; no bounds published'),
    'backoff_slots': _p(32, INFERRED, 'initial contention window; no bounds published'),
    'backoff_growth': _p(2.0, INFERRED, 'window multiplier per retry; not published'),
    'retry_mode': _p('collision', INFERRED,
                     'manual p11: resend "whenever a package collision happens"; how a broadcast '
                     'sender learns of a collision is undocumented, so detection is an oracle. '
                     'Alternatives: unconditional (blind copies), none'),
    'unconditional_copy_fraction': _p(1.0, INFERRED,
                                      'only used with retry_mode=unconditional: copies = '
                                      'max(1, round(fraction*S5)); S4=1 disables resend (manual p11), '
                                      'so the register is read as a total attempt count'),
    'aggregate_on_send': _p(True, INFERRED,
                            'at channel access the modem sends all queued serial bytes up to S3 as one '
                            'packet; datasheet s5.3 "transmit ... as soon as they receive anything"; '
                            'matches ~3 kB RX bursts after silences (strace)'),
    'stall_duty_gs': _p(0.0, INFERRED, 'fraction of time the GS modem cannot start a transmission'),
    'stall_duty_rf': _p(0.0, INFERRED, 'fraction of time the RF modem cannot start a transmission'),
    'stall_duty_rrls': _p(0.0, INFERRED, 'fraction of time the RRLS modem cannot start a transmission'),
    'stall_mean_ms': _p(2000.0, INFERRED,
                        'mean TX-blocked interval. Empirical stand-in for LBT lockout ("some nodes '
                        'locked out by other nodes", RFD support on ArduPilot Discourse 77020), '
                        'local EMI above the LBT threshold, or hop desync; cause unidentified'),
    'ber': _p(2e-6, INFERRED, 'bit error rate; checked against 2 CRC failures in 750 frames (radio-wire-analysis.json)'),
    'serial_buffer_bytes': _p(8192, INFERRED, 'release notes v2.45: 8 kB heap; v2.47 variable buffers; 4.03 unknown'),
    'dedup': _p(True, INFERRED, 'only 12 of 221 RX packet ids repeated (radio-packets.json), consistent with SDK retries'),
    # SEDS host behaviour.
    'gs_retransmit_ms': _p(250, DOCUMENTED, 'GroundStation26 backend/src/main.rs:173 (GS_RELIABLE_RETRANSMIT_MS)'),
    'gs_max_sends': _p(18, DOCUMENTED, 'main.rs:174 reliable_max_retries=17'),
    'usable_air_bps': _p(32000, DOCUMENTED, 'config/radio rfd-async-64k profiles transport.usable_air_bps'),
    'gs_share_pct': _p(15, DOCUMENTED, 'rrls/budget_negotiation.rs:327 primary_fallback 15 until a relay BaseAck'),
    'rf_share_pct': _p(35, DOCUMENTED, 'RFBoard26 radio.c RF share 35% (1400 B/s)'),
    'batch_bytes': _p(192, DOCUMENTED, 'GS comms.rs:172 and RF radio.c lookahead cap 192 B'),
    'packet_gap_ms': _p(55, DOCUMENTED, 'rfd_capacity.rs:611-612, radio.c RADIO_PACKET_GAP_MS'),
    'rf_queue_slots': _p(12, DOCUMENTED, 'radio.c RADIO_UART_TX_QUEUE_SLOTS'),
    'rf_max_age_ms': _p(1500, DOCUMENTED, 'radio.c RADIO_UART_TX_MAX_AGE_MS'),
    'rf_retransmit_ms': _p(250, DOCUMENTED, 'RFBoard26 telemetry.c:778-779'),
    'rf_max_sends': _p(18, DOCUMENTED, 'RF_sdk_max_retries 17 (radio-source-latency-audit)'),
    'rf_ack_delay_ms': _p(5, INFERRED, 'RF owner ACK generation; no joined trace exists (worklist R4)'),
    'gs_ack_delay_ms': _p(3, MEASURED, HW + 'latency-status.json first ACK 2-67 ms, median 3'),
    'rrls_role': _p(True, DOCUMENTED, 'raw UART links default to primary (docs/backend/rrls.md)'),
    'relay_loss_ms': _p(3000, DOCUMENTED, 'GroundStation26 rrls.rs:21 LOSS_MS'),
    'relay_restore_ms': _p(2000, DOCUMENTED, 'rrls.rs:22 RESTORE_MS'),
    'relay_lease_ms': _p(2000, DOCUMENTED, 'rrls.rs:23 LEASE_MS'),
    'health_ms': _p(500, DOCUMENTED, 'rrls.rs tick: HEALTH every 500 ms; 90 kind-0 envelopes in 45 s measured'),
    'health_bytes': _p(21, MEASURED, '21-byte GS writes in underglow-repeat-20261029.strace'),
    'ready_ms': _p(500, DOCUMENTED, 'rrls.rs tick: READY every 500 ms while relaying'),
    'gs_heartbeat_ms': _p(500, MEASURED, '88 HEARTBEAT writes in 45 s (radio-wire-analysis.json)'),
    'gs_frame_bytes': _p(36, MEASURED, 'median GS write 36 B (strace)'),
    'gs_misc_bps': _p(70, INFERRED, 'time sync/discovery filler so GS TX totals the measured 277 B/s'),
    'command_bytes': _p(40, MEASURED, 'underglow command writes 35-40 B (strace)'),
    'envelope_bytes': _p(8, INFERRED, 'RRLS relay envelope overhead'),
    'rf_load_scale': _p(1.0, INFERRED, 'multiplier on the measured RF stream table'),
}

# RF downlink streams: (name, period_ms, wire_bytes, reliable). Periods and
# counts are measured in radio-packets.json (10-28); wire sizes add the
# measured payload size to an inferred ~28 B SDK/UART header. Only board health
# is reliable: latency-status.json saw 12 unique reliable originals in 29.5 s
# (0.41/s), each ACKed about four times.
RF_STREAMS = (
    ('barometer', 190, 40, False),
    ('gps_sats', 1000, 30, False),
    ('battery', 1000, 64, False),
    ('time_sync', 2000, 44, False),
    ('discovery_address', 2300, 113, False),
    ('message', 2322, 48, False),
    ('board_health_rf', 5000, 124, True),
    ('board_health_pb', 4750, 124, True),
    ('board_health_fc', 4750, 124, True),
    ('topology', 6000, 138, False),
    # Schema transfer: 21 fragments, ~3.68 kB in ~1.25 s, about every 6 s.
    ('schema', 6000, 3678, False),
)

# Measured 4-command underglow sequence, 10-29 00:05Z (command-ack-timeline.json),
# acceptance times relative to the capture origin 1793232335.574626.
UNDERGLOW_ACCEPT_MS = (3533.0, 7885.8, 10055.8, 12274.0)

MEASUREMENTS = {
    'first_ack_ms': [3126, 2460, 1169, 4976],
    'uart_sends': [13, 10, 5, 18],
    'exhausted': [False, False, False, True],
    'zero_rx_seconds_of_45': [15, 13],      # 10-28 capture, 10-29 capture
    'longest_rx_gap_s': [5.07, 4.32],
    'rx_mean_Bps': [869, 1068],
    'return_ms_lower_bound': [2487, 1895, 815, 2386],
    'forward_ms_remainder': [639, 565, 354, 2590],
    'telemetry_rel_delay_p50_ms': [1126, 484, 714, 571],
    'telemetry_rel_delay_p90_ms': [2084, 1560, 1663, 1436],
    'barometer_delivery_ratio': 0.85,
    'crc_per_frame': 2 / 750,
}


def defaults():
    return {k: v.value for k, v in PARAMS.items()}


def make_config(overrides=None):
    cfg = defaults()
    for key, value in (overrides or {}).items():
        if key not in cfg:
            raise ValueError(f'unknown model parameter {key!r}')
        cfg[key] = value
    if cfg['retry_mode'] not in ('collision', 'unconditional', 'none'):
        raise ValueError('retry_mode must be collision, unconditional or none')
    if not 1 <= cfg['global_retries'] <= 100 or not 1 <= cfg['max_retries'] <= 100:
        raise ValueError('S4/S5 outside documented range')
    if not 0 <= cfg['serial_break_ms'] <= 500 or not 1 <= cfg['max_data'] <= 4068:
        raise ValueError('S6/S3 outside documented range')
    if not all(0 <= cfg['stall_duty_' + n] < 0.95 for n in NODES) or cfg['stall_mean_ms'] <= 0:
        raise ValueError('stall duty must be in [0, 0.95) and stall_mean_ms positive')
    return cfg


def profile_overrides(path):
    """Map a GroundStation26 config/radio profile onto model inputs."""
    profile = json.loads(Path(path).read_text())
    modem, transport = profile['modem'], profile['transport']
    return {
        'uart_baud': modem['serial_baud'], 'air_kbps': modem['air_bps'] // 1000,
        'max_data': modem['max_data_bytes'], 'max_retries': modem['max_retries'],
        'global_retries': max(1, modem['broadcast_retries']),
        'serial_break_ms': modem['serial_break_ms'],
        'usable_air_bps': transport['usable_air_bps'],
        'packet_gap_ms': transport['packet_gap_ms'], 'rf_share_pct': transport['rf_share_percent'],
    }


@dataclass(eq=False)
class Frame:
    kind: str
    src: str
    size: int
    created: float
    ref: object = None
    inner: object = None
    corrupt: bool = False
    stream: str = ''


@dataclass(eq=False)
class Packet:
    pid: int
    src: str
    frames: list
    size: int
    attempts: int = 0
    copies_left: int = 0
    collisions: int = 0


@dataclass(eq=False)
class Tx:
    src: str
    start: float
    end: float
    packet: Packet


@dataclass
class Command:
    index: int
    accept: float
    started: float = None
    sends: int = 0
    first_ack: float = None
    exhausted_at: float = None
    rf_first_rx: float = None
    rf_ack_created: float = None
    done: bool = False
    send_times: list = field(default_factory=list)


class Modem:
    """One RFD900x: serial ingress, S6/S3 packetiser, CSMA queue, serial egress."""

    def __init__(self, sim, name, cts):
        self.sim, self.name, self.cts = sim, name, cts
        self.pending, self.pending_bytes, self.last_in = [], 0, None
        self.close_token = 0
        self.txq = deque()
        self.queued_bytes = 0
        self.busy = False          # contending or transmitting
        self.tx_until = -1.0
        self.out_free = 0.0
        self.seen = set()
        self.overflow_bytes = 0
        self.blocked_until = -1.0
        duty = sim.cfg['stall_duty_' + name]
        if duty > 0:
            sim.at(sim.rng.expovariate(1.0) * self._up_mean(duty), self._stall)

    def _up_mean(self, duty):
        return self.sim.cfg['stall_mean_ms'] * (1 - duty) / duty

    def _stall(self):
        sim = self.sim
        mean = sim.cfg['stall_mean_ms']
        down = sim.rng.uniform(0.25 * mean, 1.75 * mean)
        self.blocked_until = sim.now + down
        sim.stats['stall_ms'][self.name] += down
        up = sim.rng.expovariate(1.0) * self._up_mean(sim.cfg['stall_duty_' + self.name])
        sim.at(self.blocked_until + up, self._stall)

    def buffered(self):
        return self.pending_bytes + self.queued_bytes

    def space(self):
        return self.sim.cfg['serial_buffer_bytes'] - self.buffered()

    def serial_in(self, frames):
        """Host bytes finished arriving on the modem UART at sim.now."""
        sim, cfg = self.sim, self.sim.cfg
        for frame in frames:
            room = self.space()
            if room < frame.size:
                # No CTS: the modem has nowhere to put the bytes. A partial
                # frame reaches the air and fails the receiver's CRC.
                self.overflow_bytes += frame.size - max(room, 0)
                if room <= 0:
                    sim.stats['overflow_frames'] += 1
                    continue
                frame.corrupt = True
                size = room
            else:
                size = frame.size
            if self.pending_bytes + size > cfg['max_data']:
                self.close_packet()
            self.pending.append(frame)
            self.pending_bytes += size
        self.last_in = sim.now
        self.close_token += 1
        token = self.close_token
        sim.at(sim.now + cfg['serial_break_ms'], self._break_timer, token)

    def _break_timer(self, token):
        if token == self.close_token and self.pending:
            self.close_packet()

    def close_packet(self):
        sim, cfg = self.sim, self.sim.cfg
        if not self.pending:
            return
        sim.pid += 1
        s5 = cfg['global_retries'] if cfg['broadcast_dest'] else cfg['max_retries']
        copies = 1
        if cfg['retry_mode'] == 'unconditional':
            copies = max(1, round(cfg['unconditional_copy_fraction'] * s5))
        packet = Packet(sim.pid, self.name, self.pending, self.pending_bytes, copies_left=copies)
        self.pending, self.pending_bytes = [], 0
        self.txq.append(packet)
        self.queued_bytes += packet.size
        if not self.busy:
            self.busy = True
            self._contend(packet)

    def _window(self, attempts):
        cfg = self.sim.cfg
        slots = cfg['backoff_slots'] * cfg['backoff_growth'] ** min(attempts, 6)
        return self.sim.rng.uniform(0, slots) * cfg['backoff_slot_ms']

    def _contend(self, packet):
        self.sim.at(self.sim.now + self._window(packet.collisions), self._try_send, packet)

    def _try_send(self, packet):
        sim, cfg = self.sim, self.sim.cfg
        if sim.now < self.blocked_until:
            sim.at(self.blocked_until + self._window(packet.collisions), self._try_send, packet)
            return
        if cfg['carrier_sense']:
            busy_until = sim.sensed_busy_until(self.name)
            if busy_until is not None:
                sim.at(busy_until + self._window(packet.collisions), self._try_send, packet)
                return
        if cfg['aggregate_on_send'] and packet.attempts == 0:
            while len(self.txq) > 1 and packet.size + self.txq[1].size <= cfg['max_data']:
                extra = self.txq[1]
                del self.txq[1]
                packet.frames.extend(extra.frames)
                packet.size += extra.size
        start = sim.now + cfg['turnaround_ms']
        bits = (cfg['phy_overhead_bytes'] + packet.size * cfg['fec_factor']) * 8
        end = start + bits / cfg['air_kbps']    # kbit/s == bit/ms
        tx = Tx(self.name, start, end, packet)
        sim.channel.append(tx)
        self.tx_until = end
        sim.stats['air_ms'][self.name] += end - start
        sim.at(end, self._tx_done, tx)

    def _tx_done(self, tx):
        sim, cfg = self.sim, self.sim.cfg
        packet = tx.packet
        packet.attempts += 1
        collided = sim.deliver(tx)
        packet.collisions += collided
        retry = False
        if cfg['retry_mode'] == 'unconditional':
            packet.copies_left -= 1
            retry = packet.copies_left > 0
        elif cfg['retry_mode'] == 'collision' and collided:
            limit = cfg['global_retries'] if cfg['broadcast_dest'] else cfg['max_retries']
            retry = packet.attempts < limit
        if retry:
            sim.stats['modem_retries'][self.name] += 1
            self._contend(packet)
            return
        self.txq.popleft()
        self.queued_bytes -= packet.size
        if self.txq:
            self._contend(self.txq[0])
        else:
            self.busy = False
        self.sim.host[self.name].modem_space_changed()

    def receive(self, packet):
        sim, cfg = self.sim, self.sim.cfg
        key = (packet.src, packet.pid)
        if cfg['dedup'] and key in self.seen:
            return
        self.seen.add(key)
        start = max(sim.now, self.out_free)
        per_byte = 10.0 / cfg['uart_baud'] * 1000
        t = start
        for frame in packet.frames:
            t += frame.size * per_byte
            sim.at(t, sim.host[self.name].frame_in, frame, packet.src)
        self.out_free = t
        sim.rx_bursts[self.name].append((start, t, packet.size))


class Host:
    """SEDS software behind a modem: priority queue, share pacing, batching."""

    def __init__(self, sim, name, share_pct):
        self.sim, self.name = sim, name
        self.queue = []            # list of (priority, seq, frame)
        self.seq = 0
        self.rate = sim.cfg['usable_air_bps'] / 8000 * share_pct / 100   # bytes per ms
        self.next_write = 0.0
        self.line_free = 0.0
        self.scheduled = False
        self.waiting_cts = False

    def enqueue(self, frame, priority=5):
        sim = self.sim
        if self.name == 'rf':
            self._expire()
            if len(self.queue) >= sim.cfg['rf_queue_slots']:
                victim = max(self.queue, key=lambda q: (q[0], -q[1]))
                if victim[0] < priority:
                    sim.stats['rf_queue_full_drops'] += 1
                    return
                self.queue.remove(victim)
                sim.stats['rf_queue_full_drops'] += 1
        self.seq += 1
        self.queue.append((priority, self.seq, frame))
        self._kick()

    def _expire(self):
        limit = self.sim.cfg['rf_max_age_ms']
        keep = [q for q in self.queue if self.sim.now - q[2].created < limit or q[2].kind in ('ack', 'relack')]
        self.sim.stats['rf_stale_drops'] += len(self.queue) - len(keep)
        self.queue = keep

    def cancel(self, predicate):
        self.queue = [q for q in self.queue if not predicate(q[2])]

    def _kick(self):
        if not self.scheduled and not self.waiting_cts and self.queue:
            self.scheduled = True
            self.sim.at(max(self.sim.now, self.next_write, self.line_free), self._service)

    def modem_space_changed(self):
        if self.waiting_cts:
            self.waiting_cts = False
            self._kick()

    def _service(self):
        sim, cfg = self.sim, self.sim.cfg
        self.scheduled = False
        if self.name == 'rf':
            self._expire()
        if not self.queue:
            return
        self.queue.sort(key=lambda q: (q[0], q[1]))
        batch, size = [], 0
        while self.queue and (not batch or size + self.queue[0][2].size <= cfg['batch_bytes']):
            frame = self.queue.pop(0)[2]
            batch.append(frame)
            size += frame.size
        modem = sim.modem[self.name]
        if modem.cts and modem.space() < size:
            for frame in reversed(batch):
                self.queue.insert(0, (0, 0, frame))
            self.waiting_cts = True
            return
        for frame in batch:
            if self.name == 'gs' and isinstance(frame.ref, Command):
                frame.ref.sends += 1
                frame.ref.send_times.append(sim.now)
            sim.stats['tx_bytes'][self.name] += frame.size
        line = size * 10.0 / cfg['uart_baud'] * 1000
        done = sim.now + line
        self.line_free = done
        self.next_write = done + max(cfg['packet_gap_ms'], size / self.rate - line)
        sim.at(done, modem.serial_in, batch)
        self._kick()

    def frame_in(self, frame, via):
        sim = self.sim
        if frame.corrupt:
            sim.stats['crc_errors'][self.name] += 1
            return
        sim.stats['rx_frames'][self.name] += 1
        sim.on_frame(self.name, frame, via)


class MeshSim:
    def __init__(self, cfg, seed, duration_ms, commands, measure_from=0.0, background=True):
        self.cfg, self.rng = cfg, random.Random(seed)
        self.background = background
        self.now, self.events, self.eseq, self.pid = 0.0, [], 0, 0
        self.duration, self.measure_from = duration_ms, measure_from
        self.channel = []
        self.stats = {
            'air_ms': dict.fromkeys(NODES, 0.0), 'modem_retries': dict.fromkeys(NODES, 0),
            'tx_bytes': dict.fromkeys(NODES, 0), 'crc_errors': dict.fromkeys(NODES, 0),
            'rx_frames': dict.fromkeys(NODES, 0), 'overflow_frames': 0,
            'rf_stale_drops': 0, 'rf_queue_full_drops': 0, 'relay_on_ms': 0.0,
            'relayed_frames': 0, 'stall_ms': dict.fromkeys(NODES, 0.0),
        }
        self.rx_bursts = {n: [] for n in NODES}
        role = cfg['rrls_role']
        names = NODES if role else ('gs', 'rf')
        self.modem = {n: Modem(self, n, cfg['cts_' + n]) for n in names}
        gs_share = cfg['gs_share_pct'] if role else 25
        self.host = {'gs': Host(self, 'gs', gs_share), 'rf': Host(self, 'rf', cfg['rf_share_pct'])}
        if role:
            self.host['rrls'] = Host(self, 'rrls', 35)
        self.commands = [Command(i, t) for i, t in enumerate(commands)]
        self.active_cmd = None
        self.rel = {}
        self.tele = []          # (stream, created, delivered or None)
        self.tele_index = {}
        # relay state (GS primary and RRLS)
        self.last_direct, self.direct_since, self.fallback = None, None, False
        self.fallback_since = None
        self.rrls_health_at, self.rrls_primary_direct = None, True

    # -- event loop ---------------------------------------------------------
    def at(self, t, fn, *args):
        self.eseq += 1
        heapq.heappush(self.events, (t, self.eseq, fn, args))

    def run(self):
        if self.background:
            self._start_background()
        for cmd in self.commands:
            self.at(cmd.accept, self._cmd_accept, cmd)
        while self.events:
            t, _, fn, args = heapq.heappop(self.events)
            if t > self.duration:
                break
            self.now = t
            fn(*args)
        self.now = self.duration
        return self.result()

    def _start_background(self):
        cfg = self.cfg
        for name, period, size, reliable in RF_STREAMS:
            period = period / cfg['rf_load_scale']
            self.at(self.rng.uniform(0, period), self._rf_stream, name, period, size, reliable)
        self.at(self.rng.uniform(0, 500), self._gs_periodic, 'hb', cfg['gs_heartbeat_ms'], cfg['gs_frame_bytes'])
        if cfg['gs_misc_bps'] > 0:
            self.at(self.rng.uniform(0, 1000), self._gs_misc)
        if cfg['rrls_role']:
            self.at(self.rng.uniform(0, 500), self._health)
            self.at(self.rng.uniform(0, 500), self._rrls_tick)
            self.at(self.rng.uniform(0, 5000), self._rrls_position)
            self.at(0.0, self._relay_tick)

    # -- channel ------------------------------------------------------------
    def sensed_busy_until(self, node):
        delay = self.cfg['cca_delay_ms']
        until = None
        for tx in self.channel:
            if tx.src != node and tx.start + delay <= self.now < tx.end:
                until = tx.end if until is None else max(until, tx.end)
        return until

    def deliver(self, tx):
        turn = self.cfg['turnaround_ms']
        self.channel = [x for x in self.channel if x.end > self.now - 600]
        others = [x for x in self.channel if x is not tx and x.start < tx.end and x.end > tx.start]
        collided = any(x.src != tx.src for x in others)
        p_ok = (1 - self.cfg['ber']) ** ((tx.packet.size + self.cfg['phy_overhead_bytes']) * 8)
        for node, modem in self.modem.items():
            if node == tx.src:
                continue
            blocked = any(x.src == node and x.start < tx.end and x.end + turn > tx.start for x in self.channel)
            if collided or blocked:
                continue
            if self.rng.random() > p_ok:
                self.stats['crc_errors'][node] += 1
                continue
            modem.receive(tx.packet)
        return collided

    # -- traffic sources ------------------------------------------------------
    def _rf_stream(self, name, period, size, reliable):
        if name == 'schema':
            for k in range(21):
                self.at(self.now + k * 60, self._rf_emit, name, size // 21, False)
        else:
            self._rf_emit(name, size, reliable)
        self.at(self.now + period, self._rf_stream, name, period, size, reliable)

    def _rf_emit(self, name, size, reliable):
        frame = Frame('rel' if reliable else 'tele', 'rf', size, self.now, stream=name)
        self.tele_index[id(frame)] = len(self.tele)
        self.tele.append([name, self.now, None, size])
        if reliable:
            state = {'acked': False, 'sends': 0}
            self.rel[id(frame)] = state
            frame.ref = state
            self._rf_rel_send(frame)
        else:
            self.host['rf'].enqueue(frame, 6)

    def _rf_rel_send(self, frame):
        state = frame.ref
        if state['acked'] or state['sends'] >= self.cfg['rf_max_sends']:
            return
        state['sends'] += 1
        copy = Frame('rel', 'rf', frame.size, self.now, ref=state, inner=frame, stream=frame.stream)
        self.host['rf'].enqueue(copy, 4)
        self.at(self.now + self.cfg['rf_retransmit_ms'], self._rf_rel_send, frame)

    def _gs_periodic(self, kind, period, size):
        self.gs_send(Frame(kind, 'gs', size, self.now), 5)
        self.at(self.now + period, self._gs_periodic, kind, period, size)

    def _gs_misc(self):
        size = self.rng.choice((44, 60, 90, 150))
        self.gs_send(Frame('misc', 'gs', size, self.now), 6)
        self.at(self.now + self.rng.expovariate(self.cfg['gs_misc_bps'] / size) * 1000, self._gs_misc)

    def gs_send(self, frame, priority):
        if self.fallback and frame.kind in ('cmd', 'hb', 'misc', 'relack'):
            frame = Frame('up', 'gs', frame.size + self.cfg['envelope_bytes'], self.now,
                          ref=frame.ref, inner=frame)
        self.host['gs'].enqueue(frame, priority)

    def _health(self):
        self.host['gs'].enqueue(Frame('health', 'gs', self.cfg['health_bytes'], self.now,
                                      ref=not self.fallback), 1)
        self.at(self.now + self.cfg['health_ms'], self._health)

    def _rrls_active(self):
        fresh = self.rrls_health_at is not None and self.now - self.rrls_health_at < self.cfg['relay_lease_ms']
        return fresh and not self.rrls_primary_direct

    def _rrls_tick(self):
        if self._rrls_active():
            self.host['rrls'].enqueue(Frame('ready', 'rrls', 24, self.now), 1)
        self.at(self.now + self.cfg['ready_ms'], self._rrls_tick)

    def _rrls_position(self):
        self.host['rrls'].enqueue(Frame('position', 'rrls', 48, self.now), 6)
        self.at(self.now + 5000, self._rrls_position)

    def _relay_tick(self):
        cfg = self.cfg
        last = self.last_direct if self.last_direct is not None else 0.0
        before = self.fallback
        if self.now - last >= cfg['relay_loss_ms']:
            self.fallback = True
        elif self.direct_since is not None and self.now - self.direct_since >= cfg['relay_restore_ms']:
            self.fallback = False
        if self.fallback and not before:
            self.fallback_since = self.now
        if before and not self.fallback and self.fallback_since is not None:
            self._relay_window(self.fallback_since, self.now)
        self.at(self.now + 50, self._relay_tick)

    def _relay_window(self, start, end):
        lo, hi = max(start, self.measure_from), min(end, self.duration)
        if hi > lo:
            self.stats['relay_on_ms'] += hi - lo

    # -- commands -------------------------------------------------------------
    def _cmd_accept(self, cmd):
        if self.active_cmd is None:
            self._cmd_start(cmd)

    def _cmd_start(self, cmd):
        self.active_cmd = cmd
        cmd.started = self.now
        horizon = self.cfg['gs_retransmit_ms'] * self.cfg['gs_max_sends']
        for k in range(self.cfg['gs_max_sends']):
            self.at(self.now + k * self.cfg['gs_retransmit_ms'], self._cmd_copy, cmd)
        self.at(self.now + horizon, self._cmd_exhaust, cmd)

    def _cmd_copy(self, cmd):
        if cmd.done:
            return
        self.gs_send(Frame('cmd', 'gs', self.cfg['command_bytes'], self.now, ref=cmd), 2)

    def _cmd_finish(self, cmd):
        cmd.done = True
        self.host['gs'].cancel(lambda f: f.ref is cmd)
        self.active_cmd = None
        for nxt in self.commands:
            if nxt.started is None and nxt.accept <= self.now:
                self._cmd_start(nxt)
                return

    def _cmd_exhaust(self, cmd):
        if not cmd.done:
            cmd.exhausted_at = self.now
            self._cmd_finish(cmd)

    # -- receive handling -------------------------------------------------------
    def on_frame(self, node, frame, via):
        cfg = self.cfg
        if node == 'gs':
            if via == 'rf':
                self.last_direct_update()
            payload = frame.inner if frame.kind == 'down' else frame
            if payload.kind == 'ack':
                cmd = payload.ref
                if cmd.first_ack is None:
                    cmd.first_ack = self.now
                if not cmd.done:
                    self._cmd_finish(cmd)
            elif payload.kind in ('rel', 'tele'):
                base = payload.inner or payload
                idx = self.tele_index.get(id(base))
                if idx is not None and self.tele[idx][2] is None:
                    self.tele[idx][2] = self.now
                if payload.kind == 'rel':
                    ack = Frame('relack', 'gs', cfg['gs_frame_bytes'], self.now, ref=payload.ref)
                    self.at(self.now + cfg['gs_ack_delay_ms'], self.gs_send, ack, 3)
        elif node == 'rf':
            payload = frame
            if frame.kind == 'up':
                return          # RF parses only the direct-path framing
            if frame.kind == 'relayed':
                payload = frame.inner
            if payload.kind == 'cmd':
                cmd = payload.ref
                if cmd.rf_first_rx is None:
                    cmd.rf_first_rx = self.now
                self.at(self.now + cfg['rf_ack_delay_ms'], self._rf_ack, cmd)
            elif payload.kind == 'relack':
                payload.ref['acked'] = True
                self.host['rf'].cancel(lambda f: f.ref is payload.ref)
        elif node == 'rrls':
            if frame.kind == 'health':
                self.rrls_health_at = self.now
                self.rrls_primary_direct = frame.ref
            elif not self._rrls_active():
                return
            elif frame.kind == 'up':
                self.stats['relayed_frames'] += 1
                self.host['rrls'].enqueue(Frame('relayed', 'rrls', frame.size, self.now, inner=frame.inner), 2)
            elif via == 'rf' and frame.kind in ('tele', 'rel', 'ack'):
                self.stats['relayed_frames'] += 1
                self.host['rrls'].enqueue(Frame('down', 'rrls', frame.size + cfg['envelope_bytes'],
                                                self.now, inner=frame), 3 if frame.kind == 'ack' else 6)

    def last_direct_update(self):
        if self.last_direct is None or self.now - self.last_direct >= self.cfg['relay_loss_ms']:
            self.direct_since = self.now
        self.last_direct = self.now

    def _rf_ack(self, cmd):
        if cmd.rf_ack_created is None:
            cmd.rf_ack_created = self.now
        self.host['rf'].enqueue(Frame('ack', 'rf', self.cfg['gs_frame_bytes'], self.now, ref=cmd), 1)

    # -- results ------------------------------------------------------------------
    def result(self):
        if self.fallback and self.fallback_since is not None:
            self._relay_window(self.fallback_since, self.duration)
        lo, hi = self.measure_from, self.duration
        seconds = int((hi - lo) // 1000)
        per_s = [0] * seconds
        bursts = sorted(self.rx_bursts['gs'])
        times = []
        for start, end, size in bursts:
            if lo <= start < hi:
                k = int((start - lo) // 1000)
                if k < seconds:
                    per_s[k] += size
                times.append(start)
        edges = [lo] + times + [hi]
        gap = max(b - a for a, b in zip(edges, edges[1:])) / 1000
        cmds = []
        for c in self.commands:
            first = None if c.first_ack is None else c.first_ack - c.started
            fwd = None if c.rf_first_rx is None else c.rf_first_rx - c.started
            ret = None
            if c.first_ack is not None and c.rf_ack_created is not None:
                ret = c.first_ack - c.rf_ack_created
            cmds.append({'accept_ms': c.accept, 'started': c.started is not None,
                         'first_ack_ms': first, 'sends': c.sends,
                         'exhausted': c.exhausted_at is not None,
                         'ack_after_exhaust': c.exhausted_at is not None and c.first_ack is not None,
                         'forward_ms': fwd, 'return_ms': ret,
                         'queue_wait_ms': None if c.started is None else c.started - c.accept})
        window = [t for t in self.tele if lo <= t[1] < hi - 3000]
        delays = {}
        for name, created, delivered, _ in window:
            if delivered is not None:
                delays.setdefault(name, []).append(delivered - created)
        baro = [t for t in window if t[0] == 'barometer']
        span_s = (hi - lo) / 1000
        return {
            'commands': cmds,
            'rx_per_second': per_s,
            'zero_rx_seconds': sum(1 for v in per_s if v == 0),
            'longest_rx_gap_s': round(gap, 3),
            'rx_mean_Bps': sum(per_s) / max(seconds, 1),
            'unique_telemetry_Bps': sum(t[3] for t in window if t[2] is not None) / (span_s - 3),
            'telemetry_delay_ms': {k: _pct(v) for k, v in delays.items()},
            'telemetry_delivery_ratio': (sum(1 for t in window if t[2] is not None) / len(window)) if window else None,
            'barometer_delivery_ratio': (sum(1 for t in baro if t[2] is not None) / len(baro)) if baro else None,
            'air_utilisation': {k: v / self.duration for k, v in self.stats['air_ms'].items()},
            'modem_retries': self.stats['modem_retries'],
            'crc_errors_gs': self.stats['crc_errors']['gs'],
            'rx_frames_gs': self.stats['rx_frames']['gs'],
            'overflow_frames': self.stats['overflow_frames'],
            'rf_stale_drops': self.stats['rf_stale_drops'],
            'rf_queue_full_drops': self.stats['rf_queue_full_drops'],
            'relay_on_fraction': self.stats['relay_on_ms'] / (hi - lo),
            'relayed_frames': self.stats['relayed_frames'],
            'gs_tx_Bps': self.stats['tx_bytes']['gs'] / (self.duration / 1000),
            'rf_tx_Bps': self.stats['tx_bytes']['rf'] / (self.duration / 1000),
            'measure_seconds': span_s,
        }


def _pct(values):
    values = sorted(values)
    if not values:
        return None
    def q(p):
        return values[min(len(values) - 1, int(p * len(values)))]
    return {'n': len(values), 'min': values[0], 'p50': q(0.5), 'p90': q(0.9), 'max': values[-1],
            'rel_p50': q(0.5) - values[0], 'rel_p90': q(0.9) - values[0]}


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------
WARMUP_MS = 15000.0


def underglow_replay(cfg, seed):
    """Replay the measured 4-command sequence inside a 45 s window."""
    accepts = [WARMUP_MS + t for t in UNDERGLOW_ACCEPT_MS]
    return MeshSim(cfg, seed, WARMUP_MS + 45000.0, accepts, WARMUP_MS).run()


def command_stream(cfg, seed, duration_ms=180000.0, mean_gap_ms=8000.0):
    """Operator commands at uniform mean+-2.5 s spacing for latency statistics."""
    rng = random.Random(seed * 7919 + 1)
    t, accepts = WARMUP_MS, []
    while t < duration_ms - 25000:
        t += rng.uniform(mean_gap_ms - 2500, mean_gap_ms + 2500)
        accepts.append(t)
    return MeshSim(cfg, seed, duration_ms, accepts, WARMUP_MS).run()


def _run(args):
    kind, cfg, seed, gap = args
    return underglow_replay(cfg, seed) if kind == 'replay' else command_stream(cfg, seed, mean_gap_ms=gap)


def run_many(kind, cfg, seeds, jobs=1, gap_ms=8000.0):
    work = [(kind, cfg, s, gap_ms) for s in seeds]
    if jobs <= 1:
        return [_run(w) for w in work]
    with ProcessPoolExecutor(max_workers=min(jobs, 6)) as pool:
        return list(pool.map(_run, work))


# ---------------------------------------------------------------------------
# Calibration and evaluation summaries
# ---------------------------------------------------------------------------
def _median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def summarize_replay(results):
    cmds = [c for r in results for c in r['commands'] if c['started']]
    acks = [c['first_ack_ms'] if c['first_ack_ms'] is not None else math.inf for c in cmds]
    finite = sorted(a for a in acks if a != math.inf)
    return {
        'runs': len(results),
        'first_ack_ms_p10_p50_p90': _quantiles(acks),
        'first_ack_in_measured_range': sum(1 for a in finite if 1169 <= a <= 4976) / max(len(acks), 1),
        'sends_mean': statistics.fmean(c['sends'] for c in cmds) if cmds else None,
        'exhausted_fraction': sum(c['exhausted'] for c in cmds) / max(len(cmds), 1),
        'forward_ms_p50': _median([c['forward_ms'] for c in cmds]),
        'return_ms_p50': _median([c['return_ms'] for c in cmds]),
        'zero_rx_seconds_mean': statistics.fmean(r['zero_rx_seconds'] for r in results),
        'longest_rx_gap_s_mean': statistics.fmean(r['longest_rx_gap_s'] for r in results),
        'rx_mean_Bps': statistics.fmean(r['rx_mean_Bps'] for r in results),
        'telemetry_rel_p50_ms': _median([r['telemetry_delay_ms'].get('barometer', {}).get('rel_p50')
                                         for r in results if r['telemetry_delay_ms'].get('barometer')]),
        'telemetry_rel_p90_ms': _median([r['telemetry_delay_ms'].get('barometer', {}).get('rel_p90')
                                         for r in results if r['telemetry_delay_ms'].get('barometer')]),
        'barometer_delivery_ratio': _median([r['barometer_delivery_ratio'] for r in results]),
        'crc_per_frame': sum(r['crc_errors_gs'] for r in results) / max(1, sum(r['rx_frames_gs'] for r in results)),
        'air_utilisation_total': statistics.fmean(sum(r['air_utilisation'].values()) for r in results),
        'relay_on_fraction': statistics.fmean(r['relay_on_fraction'] for r in results),
        'gs_tx_Bps': statistics.fmean(r['gs_tx_Bps'] for r in results),
    }


def _quantiles(values):
    values = sorted(values)
    if not values:
        return None
    pick = [values[min(len(values) - 1, int(p * len(values)))] for p in (0.1, 0.5, 0.9)]
    return [None if v == math.inf else round(v) for v in pick]


def fit_score(summary):
    """Log-ratio distance to the hardware targets; lower is better.

    Each term is |ln(sim/target)|, so a factor-of-two miss costs 0.69.
    """
    m = MEASUREMENTS
    def term(sim, target):
        if sim is None:
            return 3.0
        return abs(math.log(max(sim, 1e-3) / target))
    q = summary['first_ack_ms_p10_p50_p90']
    ack50 = None if q is None or q[1] is None else q[1]
    terms = {
        'first_ack_p50': term(ack50, statistics.median(m['first_ack_ms'])),
        'sends_mean': term(summary['sends_mean'], statistics.fmean(m['uart_sends'])),
        'zero_rx_seconds': term(summary['zero_rx_seconds_mean'] + 0.5, 14.5),
        'longest_gap': term(summary['longest_rx_gap_s_mean'], 4.7),
        'rx_mean': term(summary['rx_mean_Bps'], 970),
        'telemetry_rel_p50': term(summary['telemetry_rel_p50_ms'], 700),
        'barometer_delivery': term(summary['barometer_delivery_ratio'], m['barometer_delivery_ratio']),
    }
    ret, fwd = summary['return_ms_p50'], summary['forward_ms_p50']
    terms['return_worse_than_forward'] = 0.0 if (ret and fwd and ret > fwd) else 0.5
    return sum(terms.values()), terms


CALIBRATION_FAMILIES = {
    # Null hypothesis: working LBT, resend only on collision.
    'csma_collision_resend': [{'retry_mode': 'collision'}],
    # LBT ineffective (S17 LBT_RSSI=0 is unread; long CCA blind time), collision resend.
    'weak_lbt_collision_resend': [
        {'retry_mode': 'collision', 'carrier_sense': cs, 'cca_delay_ms': d, 'backoff_slots': b}
        for cs, d in ((False, 2.0), (True, 20.0), (True, 60.0)) for b in (32, 256)],
    # Blind broadcast copies tied to S5.
    'unconditional_copies': [
        {'retry_mode': 'unconditional', 'unconditional_copy_fraction': f, 'backoff_slots': b}
        for f in (0.2, 0.3, 0.4, 1.0) for b in (64, 128, 256, 512)],
    # Exogenous TX-blocking intervals (LBT lockout stand-in) on working CSMA.
    'tx_stall': [
        {'retry_mode': 'collision', 'stall_duty_rf': rf, 'stall_duty_gs': gs, 'stall_duty_rrls': gs,
         'stall_mean_ms': mean}
        for rf in (0.4, 0.5, 0.6) for gs in (0.1, 0.2, 0.3) for mean in (1000.0, 1500.0, 2000.0)],
}


def calibrate(seeds=8, jobs=6, extra=None):
    rows = []
    for family, grid in CALIBRATION_FAMILIES.items():
        for overrides in grid:
            for scale in (0.8, 1.0):
                ov = dict(overrides, rf_load_scale=scale, **(extra or {}))
                summary = summarize_replay(run_many('replay', make_config(ov), range(seeds), jobs))
                score, terms = fit_score(summary)
                rows.append({'family': family, 'overrides': ov, 'score': round(score, 3),
                             'terms': {k: round(v, 2) for k, v in terms.items()}, 'summary': summary})
    rows.sort(key=lambda r: r['score'])
    return rows


CANDIDATES = {
    'baseline': {},
    'retransmit_1000ms': {'gs_retransmit_ms': 1000},
    'rrls_off': {'rrls_role': False},
    'low_latency_profile': 'PROFILE',
    'primary_share_25': {'gs_share_pct': 25},
    'relay_hysteresis_6s': {'relay_loss_ms': 6000},
}


def summarize_stream(results):
    cmds = [c for r in results for c in r['commands'] if c['started']]
    rtt = [c['first_ack_ms'] if c['first_ack_ms'] is not None else math.inf for c in cmds]
    total_wait = [(c['queue_wait_ms'] or 0) + (c['first_ack_ms'] if c['first_ack_ms'] is not None else math.inf)
                  for c in cmds]
    return {
        'commands': len(cmds),
        'rtt_ms_p10_p50_p90': _quantiles(rtt),
        'rtt_p99_ms': _q(rtt, 0.99),
        'accept_to_ack_p90_ms': _q(total_wait, 0.9),
        'ack_loss_rate': sum(c['exhausted'] and not c['ack_after_exhaust'] for c in cmds) / max(len(cmds), 1),
        'exhausted_rate': sum(c['exhausted'] for c in cmds) / max(len(cmds), 1),
        'within_500ms': sum(1 for x in rtt if x <= 500) / max(len(rtt), 1),
        'uplink_copies_mean': statistics.fmean(c['sends'] for c in cmds) if cmds else None,
        'downlink_goodput_Bps': statistics.fmean(r['unique_telemetry_Bps'] for r in results),
        'gs_rx_wire_Bps': statistics.fmean(r['rx_mean_Bps'] for r in results),
        'telemetry_delivery_ratio': _median([r['telemetry_delivery_ratio'] for r in results]),
        'barometer_p90_age_ms': _median([r['telemetry_delay_ms'].get('barometer', {}).get('p90')
                                         for r in results if r['telemetry_delay_ms'].get('barometer')]),
        'relay_on_fraction': statistics.fmean(r['relay_on_fraction'] for r in results),
        'air_utilisation_total': statistics.fmean(sum(r['air_utilisation'].values()) for r in results),
    }


def _q(values, p):
    values = sorted(values)
    if not values:
        return None
    v = values[min(len(values) - 1, int(p * len(values)))]
    return None if v == math.inf else round(v)


def evaluate(base_overrides, candidates, profile_path=None, seeds=6, jobs=6, gap_ms=8000.0):
    out = {}
    for name, ov in candidates.items():
        if ov == 'PROFILE':
            if profile_path is None:
                continue
            ov = profile_overrides(profile_path)
        cfg = make_config({**base_overrides, **ov})
        out[name] = summarize_stream(run_many('stream', cfg, range(100, 100 + seeds), jobs, gap_ms))
    return out


RANK_EXHAUST_PENALTY_MS = 10000.0


def rank_cost(summary):
    """p90 first-ACK latency plus 10 s per unit of GS retry exhaustion.

    Exhaustion is what the operator sees as a failed command, so a config that
    confirms every command slowly can beat one that is fast but drops some.
    """
    q = summary['rtt_ms_p10_p50_p90']
    p90 = math.inf if not q or q[2] is None else q[2]
    return p90 + RANK_EXHAUST_PENALTY_MS * summary['exhausted_rate']


def rank(evaluation):
    return [name for name, _ in sorted(evaluation.items(), key=lambda item: rank_cost(item[1]))]


def assumptions_table():
    return [{'name': k, 'default': v.value, 'kind': v.kind, 'source': v.source} for k, v in PARAMS.items()]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='cmd', required=True)
    sub.add_parser('assumptions')
    c = sub.add_parser('calibrate')
    c.add_argument('--seeds', type=int, default=8)
    c.add_argument('--jobs', type=int, default=6)
    c.add_argument('--top', type=int, default=8)
    e = sub.add_parser('evaluate')
    e.add_argument('--base', type=Path, help='JSON object of fitted parameter overrides')
    e.add_argument('--config', type=Path, action='append', default=[],
                   help='JSON object {name: overrides} of extra candidates')
    e.add_argument('--profile', type=Path, help='GroundStation26 config/radio/rfd-async-64k-low-latency.json')
    e.add_argument('--seeds', type=int, default=6)
    e.add_argument('--command-gap-ms', type=float, default=8000.0,
                   help='mean operator command spacing (uniform +-2.5 s)')
    e.add_argument('--jobs', type=int, default=6)
    r = sub.add_parser('replay')
    r.add_argument('--base', type=Path)
    r.add_argument('--seeds', type=int, default=8)
    r.add_argument('--jobs', type=int, default=6)
    args = parser.parse_args(argv)
    if args.cmd == 'assumptions':
        print(json.dumps(assumptions_table(), indent=1))
    elif args.cmd == 'calibrate':
        rows = calibrate(args.seeds, args.jobs)
        print(json.dumps(rows[:args.top], indent=1, default=str))
    elif args.cmd == 'replay':
        base = json.loads(args.base.read_text()) if args.base else {}
        print(json.dumps(summarize_replay(run_many('replay', make_config(base), range(args.seeds), args.jobs)),
                         indent=1))
    else:
        base = json.loads(args.base.read_text()) if args.base else {}
        cands = dict(CANDIDATES)
        for path in args.config:
            cands.update(json.loads(path.read_text()))
        result = evaluate(base, cands, args.profile, args.seeds, args.jobs, args.command_gap_ms)
        costs = {k: rank_cost(v) for k, v in result.items()}
        print(json.dumps({'ranking': rank(result), 'rank_cost_ms': costs, 'candidates': result}, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
