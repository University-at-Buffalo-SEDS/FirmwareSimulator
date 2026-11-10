#!/usr/bin/env python3
"""Where AV-bay packets are lost: RF UART TX (unique and copies) vs GS modem RX, plus the RF reliable ACK round trip.

usage: analyze-radio-stage-loss.py DECODED.json FROM_S TO_S [--sender FC=@addr:1010450070 ...]
DECODED.json is decode-radio-trace.py output. Station 0 is RF and station 1 the GS (rocket_radio endpoint order).
Compare the RF-TX unique rate with the producer's publish rate (SIM_TEST probes) to get the loss before the radio.
"""
import argparse, json

ap = argparse.ArgumentParser(); ap.add_argument('decoded'); ap.add_argument('a', type=float); ap.add_argument('b', type=float)
ap.add_argument('--sender', action='append', default=['FC=@addr:1010450070', 'PB=@addr:213986670', 'RF=@addr:3997908571'])
x = ap.parse_args(); a, b = x.a * 1e6, x.b * 1e6; span = x.b - x.a
addr = dict(s.split('=', 1) for s in x.sender)
d = json.load(open(x.decoded))
st = {s['file'].split('/')[-1].replace('rocket_radio-', '').replace('.bin', ''): [p for p in s['packets'] if a <= p['at_us'] < b]
      for s in d['streams']}
rftx, gsrx, rfrx = st['station-0-tx'], st['station-1-rx'], st['station-0-rx']


def pick(ps, ty, snd):
    return [p for p in ps if p['type'] == ty and (snd is None or p['sender'] == addr[snd])]


print(f"{'type/sender':32} {'RFtx_unique/s':>13} {'RFtx_copies/s':>13} {'GSrx_unique/s':>13} {'air_loss%':>9}")
for ty, snd in [('IMU_DATA', 'FC'), ('BAROMETER_DATA', 'FC'), ('BATTERY_VOLTAGE', 'PB'), ('BATTERY_CURRENT', 'PB'),
                ('GPS_STATUS', 'RF'), ('GPS_SATELLITE_NUMBER', 'RF'), ('BOARD_STATE_STATUS', None), ('BOARD_HEALTH', None)]:
    tx = pick(rftx, ty, snd); u = {p['packet_id'] for p in tx}; g = {p['packet_id'] for p in pick(gsrx, ty, snd)}
    print(f"{ty + '/' + str(snd):32} {len(u)/span:13.2f} {len(tx)/span:13.2f} {len(g)/span:13.2f} {100*(1-len(g & u)/max(1, len(u))):9.1f}")
first = {}
for p in rftx:
    if p.get('reliable') and p['packet_id'] not in first: first[p['packet_id']] = p['at_us']
acked = {}
for p in rfrx:
    i = p.get('ack_for')
    if i in first and i not in acked: acked[i] = p['at_us']
r = sorted((acked[i] - first[i]) / 1000 for i in acked)
q = lambda s, f: round(s[min(len(s) - 1, int(f * len(s)))]) if s else None
print(f'reliable RF packets {len(first)}, acked {len(acked)}, ACK round trip ms p50/p95/p99 {q(r, .5)}/{q(r, .95)}/{q(r, .99)}')
