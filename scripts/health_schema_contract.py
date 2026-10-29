#!/usr/bin/env python3
"""Check application health-report contracts before multi-board qualification."""
import argparse
import json
from pathlib import Path

HEALTH_TYPES = ("BATTERY_VOLTAGE", "BATTERY_CURRENT")


def validate_schemas(paths):
    if len(paths) < 2:
        raise ValueError("compare at least two application schemas")
    for path in paths:
        schema = json.loads(Path(path).read_text())
        types = {item["name"]: item for item in schema["types"]}
        for name in HEALTH_TYPES:
            definition = types.get(name)
            if definition is None:
                raise ValueError(f"{path}: missing {name}")
            if definition.get("reliable") != "Unordered":
                raise ValueError(f"{path}: {name} must use Unordered on every peer; mixed metadata can revert delivery policy during discovery")
            if definition.get("priority", 0) != 0:
                raise ValueError(f"{path}: {name} must retain telemetry priority 0 on every peer")
            if definition.get("element") != {"kind": "Static", "data_type": "Float32", "count": 1}:
                raise ValueError(f"{path}: {name} must remain one Float32 sample")
    return {"passed": True, "schemas": len(paths), "health_types": list(HEALTH_TYPES)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = validate_schemas(args.schema)
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
