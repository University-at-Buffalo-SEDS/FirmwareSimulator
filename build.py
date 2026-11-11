#!/usr/bin/env python3
"""Build the simulator and manage only its generated compiler caches."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys


ROOT = Path(__file__).resolve().parent


def checked_path(root: Path, path: Path) -> Path:
    root = Path(os.path.abspath(root))
    path = Path(os.path.abspath(path))
    relative = path.relative_to(root)
    if not relative.parts:
        raise ValueError("Refusing to clean the repository root")
    current = root
    if current.is_symlink():
        raise ValueError(f"Refusing symbolic link: {current}")
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"Refusing symbolic link: {current}")
    return path


def cache_roots(root: Path) -> list[Path]:
    # build/full-system holds source checkouts and evidence, so never clean build/.
    candidates = [root / "target", root / "build" / "cache"]
    for project in (root / "tests").rglob("*.csproj"):
        candidates.extend(project.parent / name for name in ("bin", "obj"))
    return sorted({checked_path(root, path) for path in candidates})


def allocated_bytes(path: Path) -> int:
    """Allocated-block estimate; shared Btrfs/APFS extents are not exclusive."""
    seen = set()
    total = 0
    for directory, folders, names in os.walk(path, followlinks=False):
        folders[:] = [name for name in folders if not (Path(directory) / name).is_symlink()]
        for name in names:
            info = (Path(directory) / name).lstat()
            key = (info.st_dev, info.st_ino)
            if stat.S_ISREG(info.st_mode) and key not in seen:
                seen.add(key)
                total += getattr(info, "st_blocks", 0) * 512
    return total


def usage(root: Path) -> dict:
    rows = [{"path": str(p.relative_to(root)), "allocated_bytes_estimate": allocated_bytes(p)}
            for p in cache_roots(root) if p.exists()]
    return {"utc": datetime.now(timezone.utc).isoformat(), "caches": rows,
            "allocated_bytes_estimate": sum(row["allocated_bytes_estimate"] for row in rows),
            "filesystem_free_bytes": shutil.disk_usage(root).free,
            "measurement_note": "Allocated-block estimates include shared extents. Free-space deltas may include concurrent activity."}


@contextmanager
def maintenance_lock(root: Path):
    import fcntl
    directory = checked_path(root, root / "build")
    directory.mkdir(exist_ok=True)
    lock = checked_path(root, directory / ".build-script.lock")
    with lock.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("Another build.py operation is active") from error
        yield


@contextmanager
def cargo_locks(paths: list[Path]):
    """Check Cargo's existing output locks without creating new cache files."""
    import fcntl
    with ExitStack() as stack:
        for path in paths:
            for lock in path.rglob(".cargo-lock") if path.exists() else []:
                if lock.is_symlink():
                    raise ValueError(f"Refusing symbolic Cargo lock: {lock}")
                stream = stack.enter_context(lock.open("rb"))
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise ValueError(f"Cargo is using {path}; finish the build before cleaning") from error
        yield


def cleanup(root: Path, *, dry_run: bool, incremental_only: bool) -> dict:
    roots = cache_roots(root)
    paths = roots
    if incremental_only:
        paths = []
        for cache in roots:
            for folder, children, _ in os.walk(cache, followlinks=False):
                children[:] = [name for name in children if not (Path(folder) / name).is_symlink()]
                if Path(folder).name == "incremental":
                    paths.append(checked_path(root, Path(folder)))
                    children.clear()
    # Validate the complete plan before the first removal.
    paths = [checked_path(root, p) for p in paths if p.exists()]
    before = usage(root)
    result = {"command": "clean", "dry_run": dry_run, "incremental_only": incremental_only,
              "paths": [str(p.relative_to(root)) for p in paths], "before": before}
    with cargo_locks(roots):
        if not dry_run:
            for path in paths:
                shutil.rmtree(path)
    result["after"] = usage(root)
    result["observed_free_space_increase_bytes"] = (
        result["after"]["filesystem_free_bytes"] - before["filesystem_free_bytes"])
    return result


def record(root: Path, result: dict) -> None:
    destination = checked_path(root, root / "build" / "cache-maintenance.jsonl")
    destination.parent.mkdir(exist_ok=True)
    with destination.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(result, sort_keys=True) + "\n")


def run(command: list[str], root: Path, jobs: int) -> int:
    environment = os.environ.copy()
    environment["CARGO_TARGET_DIR"] = str(root / "target")
    environment.setdefault("CARGO_INCREMENTAL", "0")
    environment["CARGO_BUILD_JOBS"] = str(jobs)
    print("Running:", " ".join(command), flush=True)
    return subprocess.run(command, cwd=root, env=environment).returncode


def source_revision(root: Path) -> tuple[str, str]:
    """The checkout's commit (with -dirty for uncommitted changes) and its
    commit time in UTC, for the image labels."""
    git = ["git", "-C", str(root)]
    revision = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.run([*git, "diff", "--quiet", "HEAD", "--"]).returncode != 0
    stamp = subprocess.check_output([*git, "show", "-s", "--format=%ct", "HEAD"], text=True).strip()
    when = datetime.fromtimestamp(int(stamp), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (revision + ("-dirty" if dirty else ""), when)


def image_command(root: Path, tag: str) -> list[str]:
    revision, when = source_revision(root)
    return ["docker", "build",
            "--build-arg", f"SOURCE_REVISION={revision}",
            "--build-arg", f"SOURCE_COMMIT_TIME={when}",
            "-t", tag, str(root)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=2)
    commands = parser.add_subparsers(dest="command")
    build = commands.add_parser("build", help="Build the native orchestrator")
    build.add_argument("--debug", action="store_true")
    build.add_argument("--offline", action="store_true")
    test = commands.add_parser("test", help="Run Rust and Python native tests")
    test.add_argument("--offline", action="store_true")
    image = commands.add_parser("image", help="Build the supported Docker executor")
    image.add_argument("--tag", default="firmware-simulator:local")
    clean = commands.add_parser("clean", help="Remove generated compiler caches; preserve evidence and checkouts")
    clean.add_argument("--dry-run", action="store_true")
    clean.add_argument("--incremental-only", action="store_true", help="Keep compiled binaries and dependencies")
    commands.add_parser("cache", help="Report generated cache sizes and filesystem free space")
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    command = args.command or "build"
    try:
        with maintenance_lock(ROOT):
            if command == "clean":
                result = cleanup(ROOT, dry_run=args.dry_run, incremental_only=args.incremental_only)
                record(ROOT, result)
                print(json.dumps(result, indent=2))
                return 0
            if command == "cache":
                result = usage(ROOT)
                record(ROOT, {"command": "cache", **result})
                print(json.dumps(result, indent=2))
                return 0
            before = usage(ROOT)
            if command == "image":
                code = run(image_command(ROOT, args.tag), ROOT, args.jobs)
            else:
                cargo = ["cargo", command, "--locked"]
                if command == "build" and not getattr(args, "debug", False):
                    cargo.append("--release")
                if getattr(args, "offline", False):
                    cargo.append("--offline")
                if command == "test":
                    cargo.append("--all-targets")
                code = run(cargo, ROOT, args.jobs)
                if code == 0 and command == "test":
                    code = run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"], ROOT, args.jobs)
            record(ROOT, {"command": command, "exit": code, "before": before, "after": usage(ROOT)})
            return code
    except (ValueError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
