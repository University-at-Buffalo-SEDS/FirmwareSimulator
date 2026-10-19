"""Reject the exact ignored-patch/old-lockfile validation failure."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("verify_sdk", Path(__file__).parents[1] / "scripts/verify-sedsnet-source.py")
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class VerifySdkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name)
        (self.source / "Cargo.toml").write_text('[package]\nname="SEDSnet"\nversion="4.2.4"\n')
        self.package = {"id": "candidate", "name": "SEDSnet", "version": "4.2.4",
                        "manifest_path": str(self.source / "Cargo.toml"), "source": None}
        self.metadata = {"packages": [self.package], "workspace_members": ["backend"],
                         "resolve": {"nodes": [{"id": "backend", "deps": [{"pkg": "candidate"}]},
                                                {"id": "candidate", "deps": []}]}}

    def test_actual_candidate_path_is_accepted(self):
        self.assertEqual(verify.verify_metadata(self.metadata, self.source)["version"], "4.2.4")

    def test_locked_registry_423_is_rejected_despite_declared_patch(self):
        self.package.update(version="4.2.3", source="registry+https://github.com/rust-lang/crates.io-index")
        with self.assertRaisesRegex(ValueError, "ignored the path patch"):
            verify.verify_metadata(self.metadata, self.source)

    def test_same_version_from_registry_is_still_rejected(self):
        self.package["source"] = "registry+https://github.com/rust-lang/crates.io-index"
        with self.assertRaises(ValueError):
            verify.verify_metadata(self.metadata, self.source)

    def test_same_version_wrong_checkout_is_rejected(self):
        self.package["manifest_path"] = str(self.source / "other/Cargo.toml")
        with self.assertRaises(ValueError):
            verify.verify_metadata(self.metadata, self.source)

    def test_unused_optional_candidate_does_not_qualify_the_host(self):
        self.metadata["resolve"]["nodes"][0]["deps"] = []
        with self.assertRaisesRegex(ValueError, "not a resolved dependency"):
            verify.verify_metadata(self.metadata, self.source)
