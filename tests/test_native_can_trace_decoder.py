import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location('native_trace', Path(__file__).resolve().parents[1] / 'scripts/decode-native-can-trace.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)

# A gateway FLIGHT_STATE relay from a native bay trace (SEDSnet 7, link v2 SINGLE+PAD).
GB_FLIGHT_STATE = ('28185541335 gateway 21 2eb8d7b47a97d78ea20df9e8be591c07420103d9c64c0870f23a313d562c55ddd0e24fee'
                   '9201794a2ba4ed87a2ffe2a034a30180012faba02c00000000000007')


class NativeCanTraceDecoderTests(unittest.TestCase):
    def test_relayed_managed_write_header_and_payload(self):
        (now, node, ident, packet), = list(M.packets([GB_FLIGHT_STATE]))
        self.assertEqual((now, node, ident), (28185541335, 'gateway', 0x21))
        row = M.decode(packet)
        self.assertEqual(row['type'], 256715704)
        self.assertEqual(row['source'], 3561204631)
        self.assertEqual(row['reliable'], {'flags': 0x80, 'seq': 0, 'ack': 0})  # unsequenced end-to-end wrapper
        self.assertTrue(row['contract'].startswith('07420103'))                    # three frozen owners
        self.assertEqual(row['payload'], '01')                                    # Idle

    def test_fragments_reassemble_per_sender_and_side_transport_is_not_decoded(self):
        first = bytes([0x00, 0x05, 0x07, 0x09]) + bytes(60)      # flags, type 5, source 7, timestamp 9, payload...
        last = bytes(10) + b'\xaa\xbb\xcc\xdd'                     # ... and a CRC
        lines = [f'1 a 132 {first.hex()}', f'2 b 130 {(b"\x70" + bytes(8)).hex()}', f'3 a 134 {last.hex()}']
        got = list(M.packets(lines))
        self.assertEqual([(n, node) for n, node, _, _ in got], [(2, 'b'), (3, 'a')])
        self.assertIsNone(M.decode(got[0][3]))
        row = M.decode(got[1][3])
        self.assertEqual((row['type'], row['source'], row['timestamp']), (5, 7, 9))
        self.assertEqual(len(bytes.fromhex(row['payload'])), 70)


if __name__ == '__main__':
    unittest.main()
