#!/usr/bin/env python3
"""Air budget per packet type and direction from an SDK-decoded radio trace (decode-radio-trace.py output).

usage: analyze-radio-air-budget.py DECODED.json FROM_S TO_S [--copies 2] [--framing 20] [--air-bps 64000]
Offered air = (UART frame bytes + per-packet air framing) x broadcast copies x 8 / air rate.
"""
import argparse, collections, json

ap = argparse.ArgumentParser(); ap.add_argument('decoded'); ap.add_argument('a', type=float); ap.add_argument('b', type=float)
ap.add_argument('--copies', type=int, default=2); ap.add_argument('--framing', type=int, default=20); ap.add_argument('--air-bps', type=int, default=64000)
x = ap.parse_args(); a, b = x.a * 1e6, x.b * 1e6; span = x.b - x.a
d = json.load(open(x.decoded))
names = {'@addr:1010450070': 'FC', '@addr:213986670': 'PB', '@addr:3997908571': 'RF'}
total = 0.0
for s in d['streams']:
    name = s['file'].split('/')[-1]
    if not name.endswith('-tx.bin'): continue
    who = 'RF->GS' if 'station-0' in name else 'GS->RF'
    pk = s['packets']; by = collections.Counter(); n = collections.Counter()
    for f in s['frames']:
        if not (a <= f['at_us'] < b): continue
        if f.get('packet_index') is not None and f['packet_index'] < len(pk):
            p = pk[f['packet_index']]; k = f"{p['type']} ({names.get(p['sender'], 'GS' if who == 'GS->RF' else '?')})"
        elif f.get('relay_kind') is not None and f.get('relay_kind') != 5:
            k = f"relay envelope kind {f['relay_kind']}"
        else:
            k = 'SDT chunk / uncoded'
        by[k] += (f['length'] + 4 + x.framing) * x.copies * 8; n[k] += 1
    dir_total = sum(by.values()) / span / x.air_bps; total += dir_total
    print(f'\n{who}: {sum(n.values())/span:.1f} frames/s, offered {100*dir_total:.1f}% of air')
    for k, v in by.most_common(14):
        print(f'   {k:44} {n[k]/span:6.2f}/s  {100*v/span/x.air_bps:5.1f}%')
print(f'\nTOTAL offered {100*total:.1f}% of {x.air_bps} bit/s (copies {x.copies}, framing {x.framing} B)')
