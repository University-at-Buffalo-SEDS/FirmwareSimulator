"""Verify cache cleanup cannot erase checkouts, evidence, or active Cargo work."""
import fcntl
import importlib.util
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("sim_build", Path(__file__).parents[1] / "build.py")
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


class CacheCleanupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def file(self, name, body=b"retained"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path

    def test_clean_preserves_checkouts_recordings_and_cleanup_history(self):
        retained = [self.file(name) for name in (
            "build/full-system/DAQ-Board/Core/Src/main.c",
            "build/results/trace.bin", "build/cache-maintenance.jsonl",
            "src/main.rs", "Cargo.lock")]
        self.file("target/debug/deps/rebuild.o")
        self.file("build/cache/native/rebuild.o")
        self.file("tests/uart/uart.csproj")
        self.file("tests/uart/bin/test.dll")
        self.file("tests/uart/obj/test.o")
        result = BUILD.cleanup(self.root, dry_run=False, incremental_only=False)
        self.assertEqual(len(result["paths"]), 4)
        for path in retained:
            self.assertEqual(path.read_bytes(), b"retained")
        self.assertFalse((self.root / "target").exists())
        self.assertFalse((self.root / "tests/uart/bin").exists())

    def test_incremental_only_keeps_linked_binary_and_dependency_objects(self):
        binary = self.file("target/debug/firmware-sim")
        dependency = self.file("target/debug/deps/compiler.rlib")
        self.file("target/debug/incremental/session/cache.o")
        BUILD.cleanup(self.root, dry_run=False, incremental_only=True)
        self.assertTrue(binary.exists())
        self.assertTrue(dependency.exists())
        self.assertFalse((self.root / "target/debug/incremental").exists())

    def test_dry_run_changes_no_cache(self):
        cached = self.file("target/debug/incremental/test.o")
        result = BUILD.cleanup(self.root, dry_run=True, incremental_only=False)
        self.assertTrue(result["dry_run"])
        self.assertTrue(cached.exists())

    def test_symlinked_cache_aborts_entire_plan_before_any_removal(self):
        cached = self.file("target/debug/keep.o")
        outside = self.root / "unrelated"
        outside.mkdir()
        (self.root / "build").mkdir()
        (self.root / "build/cache").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            BUILD.cleanup(self.root, dry_run=False, incremental_only=False)
        self.assertTrue(cached.exists())
        self.assertTrue(outside.exists())

    def test_link_inside_cache_cannot_delete_source(self):
        source = self.file("src/keep.rs")
        (self.root / "target").mkdir()
        (self.root / "target/source").symlink_to(source.parent, target_is_directory=True)
        BUILD.cleanup(self.root, dry_run=False, incremental_only=False)
        self.assertTrue(source.exists())

    def test_active_cargo_lock_refuses_cleanup(self):
        cached = self.file("target/debug/keep.o")
        lock = self.file("target/debug/.cargo-lock")
        with lock.open("rb") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(ValueError):
                BUILD.cleanup(self.root, dry_run=False, incremental_only=False)
        self.assertTrue(cached.exists())

    def test_build_script_operations_exclude_each_other(self):
        with BUILD.maintenance_lock(self.root):
            with self.assertRaises(ValueError):
                with BUILD.maintenance_lock(self.root):
                    self.fail("A second maintenance owner was admitted")


if __name__ == "__main__":
    unittest.main()
