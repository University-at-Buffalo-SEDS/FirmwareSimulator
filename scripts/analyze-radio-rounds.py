#!/usr/bin/env python3
"""Correlate decoded managed updates with exact destination receipt identities."""
import argparse
import json
from pathlib import Path

MANAGED = {'FLIGHT_STATE', 'FLIGHT_BUZZER', 'AV_BAY_UNDERGLOW'}


def analyze(report, source_station=1, remote_station=0, remote_owner=9644134183172063844):
    streams = {(stream['station'], stream['direction']): stream['packets'] for stream in report['streams']}
    for station in (source_station, remote_station):
        if (station, 0) not in streams or (station, 1) not in streams:
            raise ValueError('source and remote TX/RX streams are required')
    originals = {}
    for row in streams[source_station, 0]:
        if row['type'] in MANAGED and row['targets']:
            originals.setdefault(row['packet_id'], row)
    rows = []
    for packet_id, original in originals.items():
        def matching(station, direction, receipt=False, eligible=True):
            rows = [row for row in streams[station, direction]
                    if (row['ack_for'] == packet_id and row['ack_owner'] == remote_owner
                        if receipt else row['packet_id'] == packet_id)]
            if eligible and direction == 1:
                # Actual RF recovery_mesh_uplink accepts raw SDK or kind5 data,
                # while kinds1/2 physically heard by RF are host-only envelopes.
                # Actual primary Mesh::incoming accepts raw SDK or kind1 data.
                kinds = (None, 5) if station == remote_station else (None, 1)
                rows = [row for row in rows if row.get('relay_kind') in kinds]
            return rows
        sent = matching(source_station, 0)
        received = matching(remote_station, 1)
        ack_sent = matching(remote_station, 0, True)
        ack_received = matching(source_station, 1, True)
        result = {'packet_id': packet_id, 'type': original['type'], 'value': original['managed_value'],
                  'frozen_targets': original['targets'],
                  'source_tx_us': [row['at_us'] for row in sent],
                  'remote_physical_rx': [{'at_us': row['at_us'], 'relay_kind': row.get('relay_kind')} for row in matching(remote_station, 1, eligible=False)],
                  'remote_rx_us': [row['at_us'] for row in received],
                  'remote_ack_tx_us': [row['at_us'] for row in ack_sent],
                  'source_ack_rx_us': [row['at_us'] for row in ack_received]}
        if received:
            result['first_uplink_us'] = received[0]['at_us'] - sent[0]['at_us']
        if received and ack_sent:
            result['first_remote_receipt_processing_us'] = ack_sent[0]['at_us'] - received[0]['at_us']
        if ack_sent and ack_received:
            result['first_return_us'] = ack_received[0]['at_us'] - ack_sent[0]['at_us']
            result['first_wire_roundtrip_us'] = ack_received[0]['at_us'] - sent[0]['at_us']
        rows.append(result)
    return {'version': 1, 'scope': 'CRC-valid raw UART candidates decoded with actual SDK; not proof of production parser acceptance',
            'source_station': source_station, 'remote_station': remote_station,
            'receipt_owner': remote_owner, 'eligibility': 'RF data raw/kind5 only; primary return data raw/kind1 only, per production recovery_mesh.h and rrls::incoming; physical candidate arrival is not application acceptance proof', 'managed_updates': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('decoded', type=Path)
    parser.add_argument('--source-station', type=int, default=1)
    parser.add_argument('--remote-station', type=int, default=0)
    parser.add_argument('--remote-owner', type=int, default=9644134183172063844)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = analyze(json.loads(args.decoded.read_text()), args.source_station, args.remote_station, args.remote_owner)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    for row in result['managed_updates']:
        print(json.dumps(row, sort_keys=True))


if __name__ == '__main__':
    main()
