"""The station-managed bay's station clock: a station preloading simclock reads the bay's virtual time from the page
(and waits when the bay stalls), runs on at the wall rate once gs_bay.py releases the page, and without SIMCLOCK_FILE
reads the wall clock untouched."""
import importlib.util
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('gs_bay', ROOT / 'scripts/gs_bay.py')
gs_bay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gs_bay)

PROBE = "import time; t0 = time.time(); m0 = time.monotonic(); time.sleep(float(__import__('sys').argv[1])); " \
        "print(t0, time.time(), time.monotonic() - m0)"


def write_page(path: Path, virt_ns: int, horizon_ns: int, rate_milli: int, seq: int = 2):
    data = bytearray(4096)
    struct.pack_into("<QQQQQ", data, 0, seq, virt_ns, horizon_ns, time.monotonic_ns(), rate_milli)
    path.write_bytes(bytes(data))


@unittest.skipUnless(shutil.which("gcc") and sys.platform.startswith("linux"), "needs gcc on Linux")
class StationClock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.lib = gs_bay.build_simclock(ROOT, Path(cls.tmp.name))
        assert cls.lib and cls.lib.exists()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def probe(self, sleep_s: float, page: Path | None, epoch_ms: int = 0, timeout: float = 10.0):
        env = {k: v for k, v in os.environ.items() if not k.startswith("SIMCLOCK_")}
        env["LD_PRELOAD"] = str(self.lib)
        if page is not None:
            env["SIMCLOCK_FILE"] = str(page)
            env["SIMCLOCK_EPOCH_MS"] = str(epoch_ms)
        out = subprocess.run([sys.executable, "-c", PROBE, str(sleep_s)], env=env, capture_output=True, text=True,
                             timeout=timeout, check=True).stdout.split()
        return [float(x) for x in out]

    def test_without_a_page_the_wall_clock_passes_through(self):
        before = time.time()
        t0, t1, slept = self.probe(0.2, None)
        self.assertLess(abs(t0 - before), 5.0)
        self.assertGreaterEqual(slept, 0.19)

    def test_the_station_reads_the_bays_virtual_time_and_waits_for_a_stalled_bay(self):
        page = Path(self.tmp.name) / "stalled.page"
        epoch_ms = 1_700_000_000_000
        # virtual 5 s, the bay stalled at its horizon (5.1 s): the clock stops there, a sleep cannot finish
        write_page(page, 5_000_000_000, 5_100_000_000, 1000)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.probe(0.5, page, epoch_ms, timeout=2.0)
        t0, _t1, _ = self.probe(0.0, page, epoch_ms)
        self.assertAlmostEqual(t0, epoch_ms / 1000 + 5.0, delta=0.2)

    def test_a_released_page_runs_on_at_the_wall_rate(self):
        page = Path(self.tmp.name) / "released.page"
        epoch_ms = 1_700_000_000_000
        write_page(page, 7_000_000_000, 7_000_000_000, 1000)
        self.assertTrue(gs_bay.release_station_clock(page))
        seq = struct.unpack_from("<Q", page.read_bytes(), 0)[0]
        self.assertEqual(seq % 2, 0)
        t0, t1, slept = self.probe(0.3, page, epoch_ms)
        self.assertGreaterEqual(t0, epoch_ms / 1000 + 7.0)
        self.assertGreaterEqual(slept, 0.29)
        self.assertLess(slept, 3.0)


if __name__ == "__main__":
    unittest.main()
