#!/usr/bin/env python3
"""Check AV-bay data at the GroundStation from its own recording database.

The GroundStation records every packet it receives. After a linked run this
proves, from the ground side, that:

* the RF and Power boards report no failed router services
  (BOARD_HEALTH word 6 bits 16..23 are zero in every report);
* Flight Computer telemetry reaches the GroundStation through the RF relay;
* RF BOARD_HEALTH and Flight Computer packets keep arriving with no gap above
  the given limits across at least the given window of firmware time.

Gaps use the packet source timestamps (the network clock the firmware runs
on), so a simulator that runs slower than real time is measured in firmware
time, not host time.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

SERVICE_NAMES = {1: "timesync", 2: "OTA", 4: "underglow", 8: "flight-state cache"}


def rows(database: Path):
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        yield from connection.execute(
            "SELECT COALESCE(sender_id,''), data_type, payload_json, "
            "COALESCE(source_timestamp_ms, timestamp_ms) FROM telemetry ORDER BY id"
        )
    finally:
        connection.close()


def words(payload_json: str | None) -> list[int]:
    """BOARD_HEALTH is recorded as its 96 little-endian payload bytes."""
    try:
        payload = json.loads(payload_json or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list) or len(payload) % 4 or not all(isinstance(b, int) for b in payload):
        return []
    return [int.from_bytes(bytes(payload[i:i + 4]), "little") for i in range(0, len(payload), 4)]


def matches(sender: str, name: str) -> bool:
    return sender.upper() == name.upper() or sender.upper().startswith(name.upper() + ":")


def stream_report(times: list[int], window_ms: int, max_gap_ms: int) -> tuple[bool, str]:
    if len(times) < 2:
        return False, f"{len(times)} packet(s)"
    times = sorted(times)
    span = times[-1] - times[0]
    gap = max(b - a for a, b in zip(times, times[1:]))
    ok = span >= window_ms and gap <= max_gap_ms
    return ok, f"{len(times)} packets over {span / 1000:.1f} s, largest gap {gap / 1000:.2f} s"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path, help="GroundStation data directory or recording .db")
    parser.add_argument("--window-ms", type=int, default=60_000)
    parser.add_argument("--max-health-gap-ms", type=int, default=15_000,
                        help="RF BOARD_HEALTH is sent every 5 s")
    parser.add_argument("--max-fc-gap-ms", type=int, default=5_000)
    parser.add_argument("--rf", default="RF")
    parser.add_argument("--power", default="PB")
    parser.add_argument("--flight", default="FC")
    parser.add_argument("--alias", action="append", default=[], metavar="SENDER=BOARD",
                        help="recorded sender id (for example @addr:123) that is BOARD")
    args = parser.parse_args()
    aliases = dict(item.split("=", 1) for item in args.alias)

    databases = ([args.data] if args.data.is_file()
                 else sorted(args.data.glob("groundstation_recording_*.db")))
    if not databases:
        print(f"FAIL no GroundStation recording database under {args.data}")
        return 1

    health: dict[str, list[tuple[int, int]]] = {args.rf: [], args.power: []}
    rf_health_times: list[int] = []
    flight_times: list[int] = []
    senders: dict[tuple[str, str], int] = {}
    for database in databases:
        for sender, data_type, payload_json, stamp in rows(database):
            sender = aliases.get(sender, sender)
            senders[(sender, data_type)] = senders.get((sender, data_type), 0) + 1
            # REC_* rows are the GroundStation's own derived records.
            if matches(sender, args.flight) and not data_type.startswith("REC_"):
                flight_times.append(int(stamp))
            if data_type != "BOARD_HEALTH":
                continue
            board_words = words(payload_json)
            for board in health:
                if matches(sender, board) and len(board_words) > 6:
                    health[board].append((int(stamp), (board_words[6] >> 16) & 0xFF))
            if matches(sender, args.rf):
                rf_health_times.append(int(stamp))

    failures = 0

    def check(ok: bool, text: str) -> None:
        nonlocal failures
        print(("PASS " if ok else "FAIL ") + text)
        failures += 0 if ok else 1

    for board, reports in health.items():
        faults = {mask for _, mask in reports}
        named = sorted({n for m in faults for b, n in SERVICE_NAMES.items() if m & b})
        check(bool(reports) and faults == {0},
              f"{board} BOARD_HEALTH service faults are zero "
              f"({len(reports)} reports, masks {sorted(faults)}{', ' + ', '.join(named) if named else ''})")
    check(bool(flight_times), f"{args.flight} telemetry reached the GroundStation through {args.rf}"
          f" ({len(flight_times)} packets)")
    ok, text = stream_report(rf_health_times, args.window_ms, args.max_health_gap_ms)
    check(ok, f"{args.rf} BOARD_HEALTH arrived steadily: {text} "
              f"(need >= {args.window_ms / 1000:.0f} s, gap <= {args.max_health_gap_ms / 1000:.0f} s)")
    ok, text = stream_report(flight_times, args.window_ms, args.max_fc_gap_ms)
    check(ok, f"{args.flight} packets arrived steadily: {text} "
              f"(need >= {args.window_ms / 1000:.0f} s, gap <= {args.max_fc_gap_ms / 1000:.0f} s)")
    if failures:
        print("Recorded senders/types: " + ", ".join(
            f"{s or '?'}:{t}={n}" for (s, t), n in sorted(senders.items())))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
