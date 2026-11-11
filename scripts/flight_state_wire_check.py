#!/usr/bin/env python3
"""Assert every FLIGHT_STATE frame seen in a simulation run is in set (a).

Set (a) (state-split contract, 2026-11-11): 0 Startup, 1 Idle, 2 Fill,
6 Armed, 7 Launch, 13 Landed, 15 Aborted, 16 InFlight.

Evidence read from a run directory:
  - every SQLite recording under it (the GroundStation's `telemetry` table):
    each received FLIGHT_STATE row, from every sender (FC reports, board
    reports, relayed station writes);
  - every *.log under it: the GroundStation's own writes, one
    `FLIGHT_STATE wire value=N` line per packet its single FLIGHT_STATE funnel
    builds.

Frames between two boards that never reach the station are not visible here;
the static contract check (flight_state_contract.py) covers their sources.
"""
import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

SET_A = {0, 1, 2, 6, 7, 13, 15, 16}
WIRE_LINE = re.compile(r"FLIGHT_STATE wire value=(\d+)")


def recorded_frames(path):
    frames = []
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return frames
    try:
        rows = db.execute(
            "SELECT sender_id, values_json, timestamp_ms FROM telemetry WHERE data_type='FLIGHT_STATE'").fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        db.close()
    for sender, values, timestamp in rows:
        try:
            value = json.loads(values)[0]
        except (TypeError, ValueError, IndexError):
            frames.append({"source": str(path.name), "sender": sender, "value": values, "timestamp_ms": timestamp})
            continue
        frames.append({"source": str(path.name), "sender": sender, "value": value, "timestamp_ms": timestamp})
    return frames


def logged_frames(path):
    frames = []
    with path.open(errors="replace") as handle:
        for number, line in enumerate(handle, 1):
            match = WIRE_LINE.search(line)
            if match:
                frames.append({"source": f"{path.name}:{number}", "sender": "GS", "value": int(match.group(1))})
    return frames


def check(run_dir):
    run_dir = Path(run_dir)
    frames = []
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix in (".db", ".sqlite", ".sqlite3"):
            frames += recorded_frames(path)
        elif path.suffix == ".log":
            frames += logged_frames(path)
    bad = [frame for frame in frames
           if not (isinstance(frame["value"], (int, float)) and float(frame["value"]).is_integer()
                   and int(frame["value"]) in SET_A)]
    return {"pass": bool(frames) and not bad, "frames": len(frames),
            "station_writes": sum(1 for f in frames if f["sender"] == "GS" and ":" in f["source"]),
            "values": sorted({int(f["value"]) for f in frames if isinstance(f["value"], (int, float))}),
            "violations": bad[:50], "note": None if frames else "no FLIGHT_STATE frame found in the run"}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    result = check(args.run_dir)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["pass"] else 1)


if __name__ == "__main__":
    main()
