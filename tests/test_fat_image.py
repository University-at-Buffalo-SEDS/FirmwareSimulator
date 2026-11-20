"""scripts/fat_image.py: the FAT16 SD image the native engine hands the FC
(integrated ground test CSVs in /IGT)."""
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import fat_image  # noqa: E402


class FatImageTests(unittest.TestCase):
    def test_round_trip_one_directory_level(self):
        files = {
            'IGT/IMU0.CSV': b't_ms,ax_mps2,ay_mps2,az_mps2,gx_dps,gy_dps,gz_dps\n' + b'0,0,0,9.8,0,0,0\n' * 3000,
            'IGT/BARO0.CSV': b't_ms,alt_m,pressure_pa,temp_c\n0,0,101000,20\n',
            'IGT/EMPTY.CSV': b'',
            'README.TXT': b'root file',
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'sd.img'
            fat_image.build(path, files)
            self.assertEqual(fat_image.read_files(path), files)
            raw = path.read_bytes()
            self.assertEqual(len(raw), 64 * 1024 * 1024)
            self.assertEqual(raw[510:512], b'\x55\xaa')
            self.assertEqual(raw[54:62], b'FAT16   ')
            bps, spc, reserved, fats, root_entries, total16, media, fat_sz = struct.unpack_from('<HBHBHHBH', raw, 11)
            total = struct.unpack_from('<L', raw, 32)[0]
            clusters = (total - reserved - root_entries * 32 // bps - fats * fat_sz) // spc
            self.assertTrue(4085 <= clusters < 65525)  # FAT16 by cluster count, as FileX decides
            self.assertEqual((bps, media, total16), (512, 0xF8, 0))
            # Both FAT copies agree and start with the media marker.
            fat = raw[reserved * bps:(reserved + fat_sz) * bps]
            self.assertEqual(fat, raw[(reserved + fat_sz) * bps:(reserved + 2 * fat_sz) * bps])
            self.assertEqual(struct.unpack_from('<HH', fat), (0xFFF8, 0xFFFF))

    def test_rejects_names_filex_would_not_find(self):
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ('IGT/LONGFILENAME.CSV', 'A/B/C.CSV', 'IGT/X.CSVX'):
                with self.assertRaises(ValueError, msg=bad):
                    fat_image.build(Path(tmp) / 'sd.img', {bad: b'x'})


if __name__ == '__main__':
    unittest.main()
