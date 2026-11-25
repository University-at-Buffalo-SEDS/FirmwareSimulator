"""The native SITL GPS (native/shim/src/sim_gps.c) dates its NMEA from the bay's virtual time on the station's epoch.

The station on the bay's clock (simclock) reads UTC as the wall clock at virtual 0 plus virtual time. A GPS that read
the host clock instead ran ahead of it whenever the bay slipped under host load, and in Testing (no station network
time) the RF GPS became the boards' network time: board stamps 13 s ahead of the station, so fresh-only conditions
(the fill-system low-voltage condition) never raised.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHIM = ROOT / "native" / "shim"

HARNESS = r"""
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
static uint64_t now;
uint64_t sim_now_ns(void) { return now; }
uint64_t sim_local_to_global(uint64_t local) { return 5000000000ull + local; }  /* booted at virtual 5 s */
int sim_gps_xfer(const char *bus, const uint8_t *tx, uint8_t *rx, uint16_t n);
int main(int argc, char **argv)
{
    now = strtoull(argv[1], NULL, 10);
    uint8_t rx[400] = {0};
    if (!sim_gps_xfer("SPI2", NULL, rx, sizeof rx)) return 2;
    for (size_t i = 0; i < sizeof rx && rx[i] != 0xFF; i++) putchar(rx[i]);  /* idle 0xFF ends the sentences */
    return 0;
}
"""


@unittest.skipUnless(shutil.which("cc"), "a C compiler is required")
class NativeGpsUtc(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        (self.tmp / "main.h").write_text("")
        (self.tmp / "harness.c").write_text(HARNESS)
        (self.tmp / "devices").write_text("gps SPI2\n")
        self.exe = self.tmp / "gps"
        subprocess.run(["cc", "-std=gnu11", "-I", str(self.tmp), "-I", str(SHIM / "include"), "-o", str(self.exe),
                        str(SHIM / "src" / "sim_gps.c"), str(self.tmp / "harness.c"), "-lm"], check=True)

    def rmc_time(self, now_ns, epoch_ms=None):
        env = {k: v for k, v in os.environ.items() if k != "SIM_UTC_EPOCH_MS"}
        env["SIM_DEVICES"] = str(self.tmp / "devices")
        if epoch_ms is not None:
            env["SIM_UTC_EPOCH_MS"] = str(epoch_ms)
        out = subprocess.run([str(self.exe), str(now_ns)], env=env, capture_output=True, text=True, check=True).stdout
        rmc = next(line.strip() for line in out.splitlines() if line.startswith("$GPRMC"))
        fields = rmc.split(",")
        return fields[1], fields[9]  # hhmmss.ss, ddmmyy

    def test_utc_is_the_station_epoch_plus_global_virtual_time(self):
        # 2026-11-25 01:00:00.000 UTC at virtual 0; the node booted at virtual 5 s and has run 12.34 s
        epoch_ms = 1795568400000
        self.assertEqual(self.rmc_time(12_340_000_000, epoch_ms), ("010017.34", "251126"))

    def test_without_an_epoch_it_falls_back_to_the_host_clock(self):
        hms, dmy = self.rmc_time(0)
        self.assertRegex(hms, r"^\d{6}\.\d{2}$")
        self.assertRegex(dmy, r"^\d{6}$")


if __name__ == "__main__":
    unittest.main()
