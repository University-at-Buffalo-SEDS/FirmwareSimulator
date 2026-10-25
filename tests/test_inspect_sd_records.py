import csv
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("sd_records", Path(__file__).resolve().parents[1]/"scripts/inspect-sd-records.py")
records = importlib.util.module_from_spec(spec)
spec.loader.exec_module(records)


def document(items, repeat_header=False):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(records.HEADER)
    for i, item in enumerate(items):
        row = dict.fromkeys(records.HEADER, "")
        row.update(timestamp_ms=str(i), monotonic_ms=str(i), sensor="IMU", value="1 2 3 4 5 6",
                   time_source="local", uptime_ticks=str(i), uptime_frequency_hz="1000", record_sequence=str(i+1))
        row.update(item)
        writer.writerow([row[key] for key in records.HEADER])
    if repeat_header:
        writer.writerow(records.HEADER)
    stream.seek(0)
    return stream


class SdRecordInspectionTests(unittest.TestCase):
    def test_vectors_quoted_messages_and_utc_steps_keep_their_original_values(self):
        source = document([dict(time_source="network", timestamp_ms="2000000"),
                           dict(sensor="Message", value='hello, "rocket"\nsecond line', time_source="network", timestamp_ms="1000000")])
        result = records.inspect_csv(source, "fc.csv")
        self.assertEqual(result["records"], 2)
        self.assertEqual(result["raw_records"], 0)
        self.assertEqual(result["clock_frequencies_hz"], [1000])
        self.assertEqual(result["sensors"], {"IMU": 1, "Message": 1})

    def test_raw_sequence_wrap_is_valid_but_a_gap_or_duplicate_fails(self):
        values = [dict(raw_adc_code="-123", record_sequence=str(n)) for n in (2**32-2, 2**32-1, 0)]
        result = records.inspect_csv(document(values), "raw.csv")
        self.assertEqual(result["raw_records"], 3)
        for sequences in [(1,3), (1,1)]:
            with self.assertRaisesRegex(ValueError, "sequence gap/duplicate"):
                records.inspect_csv(document([dict(raw_adc_code="0",record_sequence=str(n)) for n in sequences]), "raw.csv")

    def test_header_and_clock_corruption_are_rejected(self):
        for row in [dict(uptime_frequency_hz="0"), dict(monotonic_ms="99"), dict(timestamp_ms="77"), dict(record_sequence="-1")]:
            with self.assertRaises(ValueError):
                records.inspect_csv(document([row]), "bad.csv")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            records.inspect_csv(document([{}], repeat_header=True), "bad.csv")
        with self.assertRaisesRegex(ValueError, "header"):
            records.inspect_csv(io.StringIO("timestamp_ms,value\n0,1\n"), "legacy.csv")

    def test_reconciliation_uses_flush_lower_bound_and_processed_upper_bound(self):
        files = [dict(raw_records=3, records=5)]
        self.assertEqual(records.reconcile(files, "raw", dict(flushed=2,processed=4,accepted=5))["recorded_rows"],3)
        for expected in [dict(flushed=4,processed=4,accepted=5),dict(flushed=1,processed=2,accepted=5),dict(flushed=1,processed=4,accepted=3)]:
            with self.assertRaises(ValueError):records.reconcile(files, "raw", expected)

    def test_unaccounted_acquisition_loss_fails_even_when_file_write_counters_agree(self):
        files = [dict(raw_records=3, records=3)]
        expected = dict(flushed=3, processed=3, accepted=3, acquired=7, boots=2)
        records.reconcile(files, "raw", expected, 2)
        with self.assertRaisesRegex(ValueError, "acquired/accepted"):
            records.reconcile(files, "raw", expected, 1)
        with self.assertRaisesRegex(ValueError, "acquired counters"):
            records.reconcile(files, "raw", dict(flushed=3,processed=3,accepted=3), 2)

    def test_per_boot_counter_totals_preserve_the_last_sample_before_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lines = []
            for sample, (accepted,processed,flushed) in enumerate([(5,4,3),(10,9,8),(3,2,1),(8,7,6)]):
                for label,value in [("accepted",accepted),("written",processed),("flushed",flushed)]:
                    lines += [f"SEDS_BAY_PROBE daq sd_raw_records_{label} {sample}",f"0x{value:08x}"]
            (root/"memory-probes.txt").write_text("\n".join(lines)+"\n")
            (root/"renode.log").write_text("SEDS_BAY_REBOOT daq 2\n")
            self.assertEqual(records.expected_counts(root,"daq"),dict(accepted=18,processed=16,flushed=14))

    def test_fc_raw_vectors_are_distinct_from_daq_raw_sequence(self):
        values = [dict(sensor="barometer_sample",raw_adc_code="16777215 123 nan",value="1 2 3",raw_value="16777215 123 nan",calibrated_value="1 2 3",record_sequence="12"),
                  dict(sensor="gyroscope_sample",raw_adc_code="-32768 0 32767",value="4 5 6",raw_value="-32768 0 32767",calibrated_value="4 5 6",record_sequence="20")]
        result=records.inspect_csv(document(values),"fc.csv")
        self.assertEqual(result["sensor_vector_records"],2)
        self.assertEqual(result["raw_records"],0)
        for change in [dict(raw_adc_code="1 2 3"),dict(raw_adc_code="16777216 1 nan"),dict(calibrated_value="1 2 nan")]:
            with self.subTest(change=change),self.assertRaises(ValueError):
                records.inspect_csv(document([values[0]|change]),"bad.csv")

    def test_pressure_has_separate_source_sequence_and_four_ms_cadence(self):
        def pressure(n,t):return dict(sensor="valve_pressure_psi",raw_adc_code="1234",calibrated_value="1.5",record_sequence=str(n),uptime_ticks=str(t),monotonic_ms=str(t),timestamp_ms=str(t))
        rows=[pressure(2**32-1,100),dict(raw_adc_code="-1",record_sequence="300"),pressure(0,104)]
        result=records.inspect_csv(document(rows),"mixed.csv")
        self.assertEqual(result["pressure_records"],2)
        self.assertEqual(result["raw_records"],1)
        for second in [pressure(1,104),pressure(0,108),pressure(0,104)|dict(raw_adc_code="4096")]:
            with self.assertRaises(ValueError):records.inspect_csv(document([rows[0],second]),"bad.csv")

    def test_reliable_pressure_reordering_preserves_completeness_and_bounded_history(self):
        def pressure(n):
            t=100+4*n
            return dict(sensor="valve_pressure_psi",raw_adc_code="1234",calibrated_value="1.5",record_sequence=str((2**32-4+n) % 2**32),uptime_ticks=str(t),monotonic_ms=str(t),timestamp_ms=str(t))
        order=list(range(4,12))+list(range(4))+list(range(12,4200))
        self.assertEqual(records.inspect_csv(document([pressure(n) for n in order]),"reordered.csv")["pressure_records"],4200)
        for bad in [order+[0],order[:-1]+[4198],order[:500]+order[501:], [0,4100]]:
            with self.assertRaisesRegex(ValueError,"sequence/time gap or duplicate"):
                records.inspect_csv(document([pressure(n) for n in bad]),"bad.csv")

    def test_pressure_card_rows_are_reconciled_with_pressure_writer_and_flush_counters(self):
        files=[dict(raw_records=100,records=105,pressure_records=5)]
        expected=dict(accepted=7,processed=6,flushed=5)
        self.assertEqual(records.reconcile(files,"pressure",expected)["recorded_rows"],5)
        for wrong in [dict(accepted=7,processed=6,flushed=6),dict(accepted=7,processed=4,flushed=3)]:
            with self.assertRaises(ValueError):records.reconcile(files,"pressure",wrong)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/"memory-probes.txt").write_text("SEDS_BAY_PROBE daq pressure_sd_enqueued 0\n0x7\nSEDS_BAY_PROBE daq pressure_sd_written 0\n0x6\nSEDS_BAY_PROBE daq pressure_sd_flushed 0\n0x5\n")
            (root/"renode.log").write_text("")
            self.assertEqual(records.expected_counts(root,"daq","pressure"),expected)
            with self.assertRaisesRegex(ValueError,"belongs to the DAQ"):
                records.expected_counts(root,"flight","pressure")

    def test_card_inspection_mount_is_read_only_and_preserves_spaces(self):
        command = records.image_command("mtype",Path("/tmp/saved cards/daq.img"),"simulator:test")
        self.assertIn("/tmp/saved cards:/card:ro",command)
        self.assertEqual(command[-3:],["simulator:test","-i","/card/daq.img"])


if __name__ == "__main__":unittest.main()
