#!/usr/bin/env python3
"""Verify Cargo's resolved SDK, rather than merely declaring a path patch."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tomllib


def verify_metadata(metadata, source):
    source = Path(source).resolve()
    expected = tomllib.loads((source / "Cargo.toml").read_text())["package"]
    packages = [p for p in metadata["packages"] if p["name"] == "SEDSnet"]
    if len(packages) != 1:
        raise ValueError("SDK source selection is absent or ambiguous")
    package = packages[0]
    if (package["version"] != expected["version"] or package.get("source") is not None
            or Path(package["manifest_path"]).resolve() != source / "Cargo.toml"):
        raise ValueError("SDK source selection mismatch; Cargo may have ignored the path patch")
    graph = {n["id"]: [d["pkg"] for d in n.get("deps", [])]
             for n in metadata["resolve"]["nodes"]}
    pending = list(metadata["workspace_members"])
    seen = set()
    while pending:
        node = pending.pop()
        if node in seen:
            continue
        seen.add(node)
        pending.extend(graph.get(node, []))
    if package["id"] not in seen:
        raise ValueError("SDK override is not a resolved dependency of the workspace")
    return {k: package[k] for k in ("name", "version", "manifest_path", "source")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--update-lock", action="store_true",
                        help="Explicitly select the candidate version in the isolated workspace lockfile")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    source = args.source.resolve()
    version = tomllib.loads((source / "Cargo.toml").read_text())["package"]["version"]
    command = ["cargo"] + (["--offline"] if args.offline else [])
    command += ["--config", "patch.crates-io.SEDSnet.path=" + json.dumps(str(source))]
    if args.update_lock:
        result = subprocess.run(command + ["update", "-p", "SEDSnet", "--precise", version],
                                cwd=args.workspace, capture_output=True, text=True)
        if result.returncode:
            raise ValueError("Cargo could not select the candidate SDK; lockfile update failed")
    result = subprocess.run(command + ["metadata", "--locked", "--format-version", "1"],
                            cwd=args.workspace, capture_output=True, text=True)
    if result.returncode:
        raise ValueError("Cargo metadata failed; the candidate lockfile must be selected explicitly")
    print(json.dumps(verify_metadata(json.loads(result.stdout), source), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
