#!/usr/bin/env python3
"""Cross-repo FLIGHT_STATE contract check (state split, 2026-11-11).

Only set (a) may ever be on the network as FLIGHT_STATE:
0 Startup, 1 Idle, 2 Fill, 6 Armed, 7 Launch, 13 Landed, 15 Aborted, 16 InFlight.

This reads the sources of every repo statically and reports each place where a
value outside set (a) can reach the network, and each receiver that would
refuse a set-(a) value (a refused managed delivery is retransmitted forever).

It checks:
  GS  (GroundStation26)      the NetworkFlightState enum, the single funnel
                             every GS FLIGHT_STATE write goes through, and the
                             simulated FC/RRLS emitters;
  FC  (FlightComputer26)     fcstructs.h gnd_state, fcapi.h global_state_of(),
                             every FLIGHT_STATE publication, and its receivers;
  AB/VB                      thread_comm.h state enums, VB's publication call
                             sites, and their flight_state_cache bounds;
  GB/DAQ/PB/RF               flight_state_cache bounds;
  schemas                    every config/sedsnet.json and the GS
                             backend/tests/board-schemas tables.

Exit status 1 and one line per violation when anything fails.
"""
import argparse
import json
import re
import sys
from pathlib import Path

SET_A = {0: "Startup", 1: "Idle", 2: "Fill", 6: "Armed", 7: "Launch",
         13: "Landed", 15: "Aborted", 16: "InFlight"}
RETIRED = {3, 4, 5, 8, 9, 10, 11, 12, 14}
IN_FLIGHT = 16

BOARD_REPOS = ("ActuatorBoard26", "ValveBoard26", "gateway-board26", "DAQ-Board",
               "PowerBoard26", "RFBoard26", "FlightComputer26")
ENUM_BOARDS = {"ActuatorBoard26": "ACTUATOR_FLIGHT_STATE_",
               "ValveBoard26": "VALVE_FLIGHT_STATE_"}


def strip_c_comments(text):
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def strip_rust_comments(text):
    return re.sub(r"//[^\n]*", "", text)


def parse_c_enum(body):
    """Members of a C enum body in order, with implicit numbering."""
    values, nxt = {}, 0
    for raw in body.split(","):
        item = raw.strip()
        if not item:
            continue
        match = re.match(r"([A-Za-z_]\w*)\s*(?:=\s*([^,]+))?$", item, flags=re.S)
        if not match:
            continue
        name, expr = match.group(1), match.group(2)
        if expr is not None:
            expr = expr.strip().rstrip("uU")
            if re.fullmatch(r"(0x[0-9a-fA-F]+|\d+)[uU]?", expr):
                nxt = int(expr.rstrip("uU"), 0)
            elif expr in values:
                nxt = values[expr]
            else:
                continue
        values[name] = nxt
        nxt += 1
    return values


def non_test_rust(text):
    """Rust source with every #[cfg(test)] item blanked (line numbers kept)."""
    out, at = [], 0
    for match in re.finditer(r"#\[cfg\(test\)\]", text):
        if match.start() < at:
            continue
        end = item_end(text, match.end())
        out.append(text[at:match.start()])
        out.append("\n" * text.count("\n", match.start(), end))
        at = end
    out.append(text[at:])
    return "".join(out)


def item_end(text, start):
    """End of the item after an attribute: its matching brace, or its `;`."""
    depth, index = 0, start
    while index < len(text):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
        elif char == ";" and depth == 0:
            return index + 1
        index += 1
    return len(text)


class Report:
    def __init__(self):
        self.violations = []
        self.checked = []

    def fail(self, where, message):
        self.violations.append(f"{where}: {message}")

    def ok(self, what):
        self.checked.append(what)


# ---------------------------------------------------------------- GS

def check_gs(root, report):
    gs = root / "GroundStation26" / "backend" / "src"
    types = gs / "types.rs"
    if not types.exists():
        report.fail("GroundStation26", "backend/src/types.rs not found")
        return
    text = strip_rust_comments(types.read_text())
    match = re.search(r"enum\s+NetworkFlightState\s*\{(.*?)\}", text, flags=re.S)
    if not match:
        report.fail("GroundStation26/backend/src/types.rs",
                    "no NetworkFlightState enum: GS FLIGHT_STATE writes are not restricted to set (a)")
    else:
        body = re.sub(r"#\[[^\]]*\]", "", match.group(1))
        values = parse_c_enum(body)
        if sorted(values.values()) != sorted(SET_A):
            report.fail("GroundStation26/backend/src/types.rs",
                        f"NetworkFlightState values {sorted(values.values())} != set (a) {sorted(SET_A)}")
        else:
            report.ok("GS NetworkFlightState == set (a)")

    nv = gs / "network_variables.rs"
    if nv.exists():
        source = non_test_rust(strip_rust_comments(nv.read_text()))
        for found in re.finditer(r"packet_bytes_at\(\s*FLIGHT_STATE_TYPE", source):
            line = source.count("\n", 0, found.start()) + 1
            head = source[:found.start()]
            fn = list(re.finditer(r"\bfn\s+\w+\s*(?:<[^>]*>)?\s*\(([^)]*)\)", head, flags=re.S))
            signature = fn[-1].group(1) if fn else ""
            if "NetworkFlightState" not in signature:
                report.fail(f"GroundStation26/backend/src/network_variables.rs:{line}",
                            "FLIGHT_STATE packet built outside a NetworkFlightState-typed funnel")
        report.ok("GS network_variables FLIGHT_STATE writes")

    for name in ("flight_sim.rs", "rrls_sim_primary.rs"):
        path = gs / name
        if not path.exists():
            continue
        source = non_test_rust(strip_rust_comments(path.read_text()))
        lines = source.split("\n")
        seen = set()
        for index, line in enumerate(lines):
            if '"FLIGHT_STATE"' not in line:
                continue
            window = "\n".join(lines[max(0, index - 8): index + 12])
            if "network" in window.lower():
                continue
            for cast in re.finditer(r"([\w:.]+)\s+as\s+u8", window):
                expr = cast.group(1)
                named = re.match(r"(?:crate::types::)?FlightState::(\w+)$", expr)
                if named and named.group(1) in SET_A.values():
                    continue
                if expr in seen:
                    continue
                seen.add(expr)
                report.fail(f"GroundStation26/backend/src/{name}:{index + 1}",
                            f"FLIGHT_STATE payload `{expr} as u8` is not limited to set (a)")
        report.ok(f"GS {name} emitters")


# ---------------------------------------------------------------- FC

def fc_enum(root):
    path = root / "FlightComputer26" / "Core" / "Inc" / "fcstructs.h"
    text = strip_c_comments(path.read_text())
    match = re.search(r"enum\s+global_state[^{]*\{(.*?)\}", text, flags=re.S)
    return parse_c_enum(match.group(1)) if match else {}


def check_fc(root, report):
    base = root / "FlightComputer26" / "Core"
    if not base.exists():
        report.fail("FlightComputer26", "Core/ not found")
        return
    values = fc_enum(root)
    if not values:
        report.fail("FlightComputer26/Core/Inc/fcstructs.h", "gnd_state enum not found")
        return
    if IN_FLIGHT not in [v for k, v in values.items() if k.startswith("G_")]:
        report.fail("FlightComputer26/Core/Inc/fcstructs.h", "gnd_state has no InFlight (16)")
    fcapi = strip_c_comments((base / "Inc" / "fcapi.h").read_text())
    match = re.search(r"global_state_of\s*\(\s*state\s+\w+\s*\)\s*\{(.*?)\n\}", fcapi, flags=re.S)
    if not match:
        report.fail("FlightComputer26/Core/Inc/fcapi.h", "global_state_of() not found")
    else:
        for ret in re.finditer(r"return\s+(G_\w+)\s*;", match.group(1)):
            name = ret.group(1)
            value = values.get(name)
            if value not in SET_A:
                report.fail("FlightComputer26/Core/Inc/fcapi.h",
                            f"global_state_of() publishes {name}={value}, outside set (a)")
    report.ok("FC global_state_of")
    for path in sorted((base / "Src").glob("*.c")) + sorted((base / "Inc").glob("*.h")):
        text = strip_c_comments(path.read_text())
        rel = path.relative_to(root)
        for found in re.finditer(r"\blog_flight_state\s*\(([^;]*)\)\s*;", text):
            if path.name == "platform.h":
                continue
            arg = found.group(1)
            if "to_global_state" not in arg and "global_state_of" not in arg:
                line = text.count("\n", 0, found.start()) + 1
                report.fail(f"{rel}:{line}", f"log_flight_state({arg.strip()}) bypasses global_state_of()")
        for found in re.finditer(r"log_telemetry_queued_u8\s*\(\s*SEDS_DT_FLIGHT_STATE", text):
            if path.name == "platform.h":
                continue
            start = text.rfind("\n{", 0, found.start())
            body = text[start:found.start()]
            if "global_state_of" not in body:
                line = text.count("\n", 0, found.start()) + 1
                report.fail(f"{rel}:{line}", "FLIGHT_STATE published without global_state_of()")
    report.ok("FC FLIGHT_STATE publications")
    distribution = base / "Src" / "distribution.c"
    if distribution.exists():
        text = strip_c_comments(distribution.read_text())
        for found in re.finditer(r"\*\s*data\s*>\s*(G_\w+|\d+)", text):
            bound = found.group(1)
            limit = values.get(bound, int(bound) if bound.isdigit() else None)
            if limit is not None and limit < IN_FLIGHT:
                line = text.count("\n", 0, found.start()) + 1
                report.fail(f"FlightComputer26/Core/Src/distribution.c:{line}",
                            f"receiver refuses FLIGHT_STATE above {bound}={limit}; InFlight (16) would be refused")


# ---------------------------------------------------------------- boards

def check_board_enums(root, report):
    for repo, prefix in ENUM_BOARDS.items():
        path = root / repo / "Core" / "Inc" / "thread_comm.h"
        if not path.exists():
            report.fail(repo, "Core/Inc/thread_comm.h not found")
            continue
        text = strip_c_comments(path.read_text())
        members = {}
        for body in re.findall(r"enum[^{;]*\{(.*?)\}", text, flags=re.S):
            parsed = parse_c_enum(body)
            if any(name.startswith(prefix) for name in parsed):
                members.update({k: v for k, v in parsed.items() if k.startswith(prefix)})
        if not members:
            report.fail(f"{repo}/Core/Inc/thread_comm.h", f"no {prefix}* enum")
            continue
        for name, value in members.items():
            if value not in SET_A and "RETIRED" not in name:
                report.fail(f"{repo}/Core/Inc/thread_comm.h",
                            f"{name}={value} is outside set (a); a value kept only to map old firmware must be named *RETIRED*")
        if IN_FLIGHT not in members.values():
            report.fail(f"{repo}/Core/Inc/thread_comm.h", f"no {prefix}* member for InFlight (16)")
        for src in sorted((root / repo / "Core" / "Src").glob("*.c")):
            body = strip_c_comments(src.read_text())
            for call in re.finditer(r"telemetry_publish_flight_state(?:_at)?\s*\(\s*([A-Z_][A-Z0-9_]*)", body):
                name = call.group(1)
                if name in members and members[name] not in SET_A:
                    line = body.count("\n", 0, call.start()) + 1
                    report.fail(f"{src.relative_to(root)}:{line}", f"publishes {name}={members[name]}, outside set (a)")
        report.ok(f"{repo} state enum")


def check_caches(root, report):
    for repo in BOARD_REPOS:
        path = root / repo / "Core" / "Src" / "flight_state_cache.c"
        if not path.exists():
            continue
        text = strip_c_comments(path.read_text())
        match = re.search(r"#define\s+FLIGHT_STATE_MAX_VALUE\s+(\d+)", text)
        if match and int(match.group(1)) < IN_FLIGHT:
            report.fail(f"{repo}/Core/Src/flight_state_cache.c",
                        f"FLIGHT_STATE_MAX_VALUE {match.group(1)} refuses InFlight (16)")
        report.ok(f"{repo} flight_state_cache bound")


# ---------------------------------------------------------------- schemas

def schema_paths(root):
    paths = [root / repo / "config" / "sedsnet.json" for repo in BOARD_REPOS]
    paths += sorted((root / "GroundStation26" / "backend" / "tests" / "board-schemas").glob("*.json"))
    return [path for path in paths if path.exists()]


def check_schemas(root, report):
    definitions = {}
    for path in schema_paths(root):
        rel = path.relative_to(root)
        schema = json.loads(path.read_text())
        entry = next((t for t in schema.get("types", []) if t.get("name") == "FLIGHT_STATE"), None)
        if entry is None:
            report.fail(str(rel), "no FLIGHT_STATE type")
            continue
        if entry.get("element") != {"kind": "Static", "data_type": "UInt8", "count": 1}:
            report.fail(str(rel), f"FLIGHT_STATE element {entry.get('element')} is not one UInt8")
        for key in ("values", "enum", "variants", "allowed"):
            if key in entry:
                listed = entry[key]
                numbers = set(listed.values() if isinstance(listed, dict) else listed)
                extra = sorted(v for v in numbers if v not in SET_A)
                if extra:
                    report.fail(str(rel), f"FLIGHT_STATE {key} lists values outside set (a): {extra}")
        definitions[str(rel)] = json.dumps({k: entry.get(k) for k in ("element", "reliable", "priority")}, sort_keys=True)
        report.ok(f"schema {rel}")
    if len(set(definitions.values())) > 1:
        report.fail("schemas", "FLIGHT_STATE definitions differ between repos: "
                    + "; ".join(f"{k}={v}" for k, v in sorted(definitions.items())))


def run(root):
    report = Report()
    root = Path(root)
    check_gs(root, report)
    check_fc(root, report)
    check_board_enums(root, report)
    check_caches(root, report)
    check_schemas(root, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2],
                        help="directory holding every repo (default: the FirmwareSimulator parent)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = run(args.root)
    if args.json:
        print(json.dumps({"passed": not report.violations, "violations": report.violations,
                          "checked": report.checked}, indent=2))
    else:
        for line in report.violations:
            print(f"VIOLATION {line}")
        print(f"{'PASS' if not report.violations else 'FAIL'}: {len(report.checked)} checks, "
              f"{len(report.violations)} violations")
    sys.exit(1 if report.violations else 0)


if __name__ == "__main__":
    main()
