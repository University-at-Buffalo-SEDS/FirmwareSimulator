#!/usr/bin/env python3
"""Stream-check DAQ/FC CSV records and compare exported-card rows with counters."""
import argparse
import csv
import io
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

HEADER = ("timestamp_ms", "monotonic_ms", "sensor", "value", "raw_adc_code", "raw_value", "calibrated_value", "time_source", "adc_temperature_c", "adc_temperature_code", "uptime_ticks", "uptime_frequency_hz", "record_sequence")


def inspect_csv(stream, name):
    header = False
    result = dict(file=name, records=0, raw_records=0, first_sequence=None, last_sequence=None,
                  min_uptime_ticks=None, max_uptime_ticks=None, clock_frequencies_hz=[], sensors={},
                  pressure_records=0, sensor_vector_records=0)
    frequencies = set()
    previous_raw = None
    pressure_origin = None
    pressure_seen = set()
    pressure_floor = None
    pressure_peak = None
    for number, row in enumerate(csv.reader(stream, strict=True), 1):
        if not row or row[0].startswith("#"):
            continue
        if tuple(row) == HEADER:
            if header:
                raise ValueError(f"{name}: duplicate CSV header at record {number}")
            header = True
            continue
        if not header or len(row) != len(HEADER):
            raise ValueError(f"{name}: missing header or wrong column count at record {number}")
        fields = dict(zip(HEADER, row))
        ticks = int(fields["uptime_ticks"])
        hz = int(fields["uptime_frequency_hz"])
        sequence = int(fields["record_sequence"])
        monotonic = int(fields["monotonic_ms"])
        timestamp = int(fields["timestamp_ms"])
        if not (0 <= ticks <= 2**64-1 and 0 < hz <= 2**32-1 and 0 <= sequence <= 2**64-1):
            raise ValueError(f"{name}: invalid uptime/frequency/sequence at record {number}")
        if monotonic != ticks * 1000 // hz:
            raise ValueError(f"{name}: monotonic time disagrees with ticks/frequency at record {number}")
        if fields["time_source"] not in ("local", "network") or timestamp < 0:
            raise ValueError(f"{name}: invalid timestamp/source at record {number}")
        if fields["time_source"] == "local" and timestamp != monotonic:
            raise ValueError(f"{name}: local timestamp disagrees with uptime at record {number}")
        sensor = fields["sensor"]
        if sensor in ("barometer_sample", "gyroscope_sample", "accelerometer_sample"):
            raw = [float(v) for v in fields["raw_adc_code"].split()]
            converted = [float(v) for v in fields["calibrated_value"].split()]
            if len(raw) != 3 or len(converted) != 3 or not all(math.isfinite(v) for v in converted):
                raise ValueError(f"{name}: invalid raw/converted sensor vector at record {number}")
            limit = (0, 2**24-1) if sensor == "barometer_sample" else (-32768, 32767)
            codes = raw[:2] if sensor == "barometer_sample" else raw
            if not all(math.isfinite(v) and v.is_integer() and limit[0] <= v <= limit[1] for v in codes):
                raise ValueError(f"{name}: invalid raw sensor codes at record {number}")
            if sensor == "barometer_sample" and not math.isnan(raw[2]):
                raise ValueError(f"{name}: derived altitude must have no raw code at record {number}")
            if fields["raw_value"] != fields["raw_adc_code"] or fields["value"] != fields["calibrated_value"]:
                raise ValueError(f"{name}: sensor vector columns disagree at record {number}")
            result["sensor_vector_records"] += 1
        elif sensor == "valve_pressure_psi":
            code = int(fields["raw_adc_code"])
            pressure = float(fields["calibrated_value"])
            if not 0 <= code <= 4095 or not math.isfinite(pressure) or not 0 <= pressure <= 2000 or hz != 1000:
                raise ValueError(f"{name}: invalid valve pressure sample at record {number}")
            # Unordered reliable delivery may replay a delayed batch after newer
            # samples. Verify completeness in source-clock order with bounded
            # memory, while still rejecting duplicates, holes and altered ticks.
            if pressure_origin is None:
                pressure_origin = (sequence, ticks)
            delta = ticks-pressure_origin[1]
            if (sequence > 2**32-1 or delta % 4 or
                    sequence != (pressure_origin[0]+delta//4) % 2**32):
                raise ValueError(f"{name}: valve pressure sequence/time gap or duplicate at record {number}")
            index = ticks//4
            if index in pressure_seen or (pressure_floor is not None and index < pressure_floor):
                raise ValueError(f"{name}: valve pressure sequence/time gap or duplicate at record {number}")
            pressure_seen.add(index)
            pressure_peak = index if pressure_peak is None else max(pressure_peak,index)
            # 4096 samples cover 16.384 seconds, longer than bounded transport
            # retries. Retire only complete prefixes; late repeats still fail.
            while pressure_seen and pressure_peak-min(pressure_seen) >= 4096:
                earliest = min(pressure_seen)
                if pressure_floor is not None and earliest != pressure_floor:
                    raise ValueError(f"{name}: valve pressure sequence/time gap or duplicate at record {number}")
                pressure_seen.remove(earliest)
                pressure_floor = earliest+1
            result["pressure_records"] += 1
        elif fields["raw_adc_code"]:
            int(fields["raw_adc_code"])
            if sequence > 2**32-1 or (previous_raw is not None and (sequence-previous_raw) % 2**32 != 1):
                raise ValueError(f"{name}: raw record sequence gap/duplicate at record {number}")
            previous_raw = sequence
            result["raw_records"] += 1
        result["records"] += 1
        if result["first_sequence"] is None:
            result["first_sequence"] = sequence
        result["last_sequence"] = sequence
        result["min_uptime_ticks"] = ticks if result["min_uptime_ticks"] is None else min(ticks, result["min_uptime_ticks"])
        result["max_uptime_ticks"] = ticks if result["max_uptime_ticks"] is None else max(ticks, result["max_uptime_ticks"])
        frequencies.add(hz)
        if len(frequencies) > 16 or (fields["sensor"] not in result["sensors"] and len(result["sensors"]) >= 256):
            raise ValueError(f"{name}: unexpected clock/sensor catalog growth")
        result["sensors"][fields["sensor"]] = result["sensors"].get(fields["sensor"], 0) + 1
    if not header:
        raise ValueError(f"{name}: no compatible CSV header")
    if pressure_seen and ((pressure_floor is not None and min(pressure_seen) != pressure_floor) or
            len(pressure_seen) != max(pressure_seen)-min(pressure_seen)+1):
        raise ValueError(f"{name}: valve pressure sequence/time gap or duplicate at end of file")
    result["clock_frequencies_hz"] = sorted(frequencies)
    return result


def expected_counts(capture, node, kind=None):
    lines = (capture/"memory-probes.txt").read_text().splitlines()
    points = {}
    for i, line in enumerate(lines[:-1]):
        tokens = line.split()
        if len(tokens) == 4 and tokens[:2] == ["SEDS_BAY_PROBE", node]:
            points.setdefault(tokens[2], {})[int(tokens[3])] = int(lines[i+1].strip(), 16)
    resets = sorted(set(int(x) for x in re.findall(r"SEDS_BAY_REBOOT " + re.escape(node) + r" (\d+)", (capture/"renode.log").read_text())))
    names = {"accepted": "sd_raw_records_accepted", "processed": "sd_raw_records_written", "flushed": "sd_raw_records_flushed"} if node == "daq" else {"accepted":"sd_accepted_records", "processed":"sd_processed_records", "flushed":"sd_flushed_records"}
    if kind == "pressure":
        if node != "daq":
            raise ValueError("pressure recording belongs to the DAQ SD card")
        names = {"accepted":"pressure_sd_enqueued", "processed":"pressure_sd_written", "flushed":"pressure_sd_flushed"}
    if kind != "pressure" and node == "daq" and "sd_raw_adc_drained" in points:
        names["acquired"] = "sd_raw_adc_drained"
    result = {}
    for label, name in names.items():
        values = points.get(name)
        if not values:
            raise ValueError(f"capture has no {node}.{name} counter")
        last = max(values)
        end_indices = [boundary-1 for boundary in resets if boundary-1 in values] + [last]
        result[label] = sum(values[index] for index in sorted(set(end_indices)))
    if "acquired" in result:
        result["boots"] = 1 + sum(boundary <= last for boundary in resets)
    return result


def reconcile(files, kind, expected, max_acquisition_pending_per_boot=None):
    count = sum(f[{"raw":"raw_records", "pressure":"pressure_records", "all":"records"}[kind]] for f in files)
    if count == 0:
        raise ValueError("no recorded data rows found")
    if expected and not (expected["flushed"] <= count <= expected["processed"] <= expected["accepted"]):
        raise ValueError(f"on-card {kind} rows {count} disagree with per-boot counters {expected}")
    if max_acquisition_pending_per_boot is not None:
        if max_acquisition_pending_per_boot < 0 or not expected or "acquired" not in expected:
            raise ValueError("acquisition reconciliation needs DAQ acquired counters and a nonnegative pending limit")
        pending = expected["acquired"] - expected["accepted"]
        limit = max_acquisition_pending_per_boot * expected.get("boots", 1)
        if not 0 <= pending <= limit:
            raise ValueError(f"acquired/accepted difference {pending} exceeds the per-boot producer-batch allowance {limit}")
    return dict(kind=kind, recorded_rows=count, expected=expected, files=files)


def image_command(tool, image, docker_image):
    if docker_image:
        return ["docker", "run", "--rm", "-v", f"{image.parent}:/card:ro", "--entrypoint", tool, docker_image, "-i", f"/card/{image.name}"]
    if not shutil.which(tool):
        raise ValueError(f"{tool} unavailable; install mtools or pass --docker-image")
    return [tool, "-i", str(image)]


def inspect_image(image, docker_image):
    listed = subprocess.run(image_command("mdir", image, docker_image)+["-s", "-b", "::"], check=True, capture_output=True, text=True).stdout
    names = [line.strip() for line in listed.splitlines() if line.strip().lower().endswith(".csv")]
    if not names or len(names) > 10000:
        raise ValueError("SD image has no CSV logs or too many files")
    results = []
    for name in names:
        with tempfile.TemporaryFile() as error:
            process = subprocess.Popen(image_command("mtype", image, docker_image)+[name], stdout=subprocess.PIPE, stderr=error)
            try:
                with io.TextIOWrapper(process.stdout, encoding="utf-8", newline="") as stream:
                    results.append(inspect_csv(stream, name))
                status = process.wait()
                if status:
                    error.seek(0)
                    raise ValueError(f"mtype failed for {name}: {error.read(4096).decode(errors='replace')}")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", nargs="*", type=Path, help="CSV files to stream-check")
    parser.add_argument("--image", type=Path, help="sparse FAT32 SD image to inspect with mtools")
    parser.add_argument("--docker-image", help="run read-only mtools inside this simulator image")
    parser.add_argument("--capture", type=Path, help="retained bay capture containing counters and SD-image markers")
    parser.add_argument("--node", choices=["daq", "flight"], help="node to reconcile across explicit simulated reboots")
    parser.add_argument("--kind", choices=["raw", "pressure", "all"], help="record count to reconcile (defaults to raw for DAQ capture, all otherwise)")
    parser.add_argument("--max-acquisition-pending-per-boot", type=int,
                        help="require DAQ acquired/accepted counts to differ by at most this configured producer-batch size per boot")
    parser.add_argument("--output", type=Path, help="write the JSON summary here")
    args = parser.parse_args()
    expected = None
    if args.capture:
        if not args.node:
            parser.error("--capture requires --node")
        expected = expected_counts(args.capture, args.node, args.kind)
        if not args.image:
            markers = re.findall(r"SEDS_BAY_SD_IMAGE " + re.escape(args.node) + r" (\d+)", (args.capture/"renode.log").read_text())
            if not markers:
                parser.error("capture contains no saved SD-image marker")
            args.image = args.capture.parent/f"sd-{args.node}-{markers[-1]}.img"
    files = []
    for path in args.csv:
        with path.open(newline="") as stream:
            files.append(inspect_csv(stream, str(path)))
    if args.image:
        files += inspect_image(args.image.resolve(), args.docker_image)
    result = reconcile(files, args.kind or ("raw" if args.node == "daq" else "all"), expected, args.max_acquisition_pending_per_boot)
    text = json.dumps(result, indent=2)+"\n"
    if args.output:
        args.output.write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
