#!/usr/bin/env python3
"""Obtain every firmware repository and run the complete SEDS system test."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys


ORGANIZATION = "University-at-Buffalo-SEDS"
FIRMWARE_BRANCH = "migration/sedlaunch-sedsnet-mainline"
REPOSITORIES = (
    "RFBoard26",
    "PowerBoard26",
    "FlightComputer26",
    "gateway-board",
    "ActuatorBoard26",
    "ValveBoard26",
    "DAQ-Board",
)
ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    print(f"\n[RUN] ({cwd.name}) {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def obtain(repository: str, workspace: Path) -> Path:
    destination = workspace / repository
    remote = f"https://github.com/{ORGANIZATION}/{repository}.git"
    if destination.joinpath(".git").is_dir():
        if subprocess.run(
            ["git", "diff", "--quiet"], cwd=destination
        ).returncode != 0:
            raise RuntimeError(
                f"{destination} has local changes; commit or stash them before updating"
            )
        run(["git", "fetch", "origin", FIRMWARE_BRANCH], cwd=destination)
        run(["git", "switch", FIRMWARE_BRANCH], cwd=destination)
        run(
            ["git", "pull", "--ff-only", "origin", FIRMWARE_BRANCH],
            cwd=destination,
        )
    elif destination.exists():
        raise RuntimeError(f"{destination} exists but is not a Git repository")
    else:
        workspace.mkdir(parents=True, exist_ok=True)
        run(
            [
                "git",
                "clone",
                "--branch",
                FIRMWARE_BRANCH,
                "--single-branch",
                remote,
                str(destination),
            ],
            cwd=workspace,
        )
    return destination


def matrix(rows: list[tuple[str, str]]) -> str:
    width = max(len("Test stage"), *(len(name) for name, _ in rows))
    lines = [f"{'Test stage':<{width}}  Result", f"{'-' * width}  ------"]
    lines.extend(f"{name:<{width}}  {result}" for name, result in rows)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Clone or update all seven embedded repositories, run their unit tests, "
            "then build and execute the linked GroundStation/avionics/fill simulation."
        )
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=ROOT / "build" / "full-system",
        help="directory used for firmware checkouts and build artifacts",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="build debug firmware (release is the default qualification mode)",
    )
    parser.add_argument(
        "--skip-unit-tests",
        action="store_true",
        help="skip each repository's standalone unit-test stage",
    )
    args = parser.parse_args()

    for tool in ("git", "docker"):
        if shutil.which(tool) is None:
            parser.error(f"required tool is not installed or not on PATH: {tool}")

    workspace = args.workspace.expanduser().resolve()
    rows: list[tuple[str, str]] = []
    try:
        roots = {name: obtain(name, workspace) for name in REPOSITORIES}
        rows.append(("Firmware repositories", "PASS"))

        if not args.skip_unit_tests:
            for name, root in roots.items():
                run([sys.executable, "build.py", "test"], cwd=root)
                rows.append((f"{name} unit tests", "PASS"))

        environment = os.environ.copy()
        environment["SEDS_FIRMWARE_SIM_SOURCE"] = str(ROOT)
        environment["SEDS_FIRMWARE_SIM_SUITE_ROOT"] = str(workspace)
        command = [sys.executable, "build.py", "test", "--all"]
        if not args.debug:
            command.append("--release")
        run(command, cwd=roots["gateway-board"], env=environment)
        rows.append(("Linked seven-board system", "PASS"))
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        rows.append(("Full-system qualification", "FAIL"))
        print(f"\n{matrix(rows)}", file=sys.stderr)
        print(f"\nFailure: {error}", file=sys.stderr)
        print(
            "Check GitHub access, start Docker, and rerun with the same --workspace; "
            "completed clones and build caches will be reused.",
            file=sys.stderr,
        )
        return 1

    print(f"\n{matrix(rows)}")
    print("\nAll firmware and linked full-system validation stages passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
