"""Match actual SDK-decoded identities; never substitute a child ACK."""
import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location('radio_rounds', Path(__file__).resolve().parents[1] / 'scripts/analyze-radio-rounds.py')
ROUNDS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ROUNDS)


def packet(at, identifier=42, ack_for=None, owner=None):
    return dict(at_us=at, packet_id=identifier, type='AV_BAY_UNDERGLOW' if ack_for is None else 'ReliableAck',
                targets=[77] if ack_for is None else [], managed_value=1,
                ack_for=ack_for, ack_owner=owner)


def report(tx, rx, ack_tx, ack_rx):
    return {'streams': [dict(station=1, direction=0, packets=tx),
                        dict(station=0, direction=1, packets=rx),
                        dict(station=0, direction=0, packets=ack_tx),
                        dict(station=1, direction=1, packets=ack_rx)]}


class RadioRoundTests(unittest.TestCase):
    def test_exact_owner_first_send_and_all_retry_times(self):
        rows = ROUNDS.analyze(report([packet(100), packet(200)], [packet(800), packet(850)],
                                    [packet(810, 90, 42, 5), packet(820, 91, 42, 7)],
                                    [packet(815, 90, 42, 5), packet(900, 91, 42, 7)]), remote_owner=7)['managed_updates']
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['source_tx_us'], [100, 200])
        self.assertEqual(row['first_uplink_us'], 700)
        self.assertEqual(row['first_remote_receipt_processing_us'], 20)
        self.assertEqual(row['first_return_us'], 80)
        self.assertEqual(row['first_wire_roundtrip_us'], 800)
        self.assertEqual(row['remote_ack_tx_us'], [820])

    def test_missing_receipt_not_replaced_with_different_identity(self):
        row = ROUNDS.analyze(report([packet(100)], [packet(800)], [packet(820, 91, 43, 7)], []),
                             remote_owner=7)['managed_updates'][0]
        self.assertEqual(row['remote_ack_tx_us'], [])
        self.assertNotIn('first_wire_roundtrip_us', row)

    def test_host_only_uplink_at_rf_is_not_counted_as_eligible_application_data(self):
        heard = packet(120)
        heard['relay_kind'] = 2
        eligible = packet(800)
        eligible['relay_kind'] = 5
        row = ROUNDS.analyze(report([packet(100)], [heard, eligible],
                                   [packet(820, 91, 42, 7)], [packet(900, 91, 42, 7)]),
                             remote_owner=7)['managed_updates'][0]
        self.assertEqual(row['first_uplink_us'], 700)
        self.assertEqual(row['first_remote_receipt_processing_us'], 20)
        self.assertEqual(row['remote_physical_rx'], [dict(at_us=120, relay_kind=2), dict(at_us=800, relay_kind=5)])

    def test_missing_direction_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'TX/RX'):
            ROUNDS.analyze({'streams': []})


if __name__ == '__main__':
    unittest.main()
