"""The image context must carry only verified binaries, never host secrets."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "build-session-image.py"
spec = importlib.util.spec_from_file_location("session_image", SCRIPT)
image = importlib.util.module_from_spec(spec)
spec.loader.exec_module(image)
installer_spec = importlib.util.spec_from_file_location(
    "protected_installer", SCRIPT.parent / "install_codex_protected.py")
installer = importlib.util.module_from_spec(installer_spec)
installer_spec.loader.exec_module(installer)
ACTUAL_PATCH_SHA256 = hashlib.sha256(installer.PATCH.read_bytes()).hexdigest()


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="session-image-package-",
                                                     dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.worker = self.root / "worker-source"
        self.worker.write_bytes(b"reviewed worker")
        self.worker.chmod(0o700)
        self.provider = self.root / "provider"
        self.provider.mkdir(mode=0o700)
        self.receipt = {"contract": image.CONTRACT, "source_commit": image.SOURCE,
                        "patch_sha256": ACTUAL_PATCH_SHA256,
                        "code_mode_host_source_commit": image.SOURCE,
                        "official_cli": "/an/unused/original/path"}
        for name, key in image.CODEX_FILES.items():
            payload = b"dispatcher" if key == "code_mode_host_dispatcher_sha256" else name.encode()
            (self.provider / name).write_bytes(payload)
            (self.provider / name).chmod(0o700)
            self.receipt[key] = hashlib.sha256(payload).hexdigest()
        self.save_receipt()

    def save_receipt(self):
        (self.provider / "receipt.json").write_text(json.dumps(self.receipt) + "\n")
        (self.provider / "receipt.json").chmod(0o600)

    def context(self):
        context = self.root / "context"
        context.mkdir(mode=0o700)
        return context

    def test_builder_contract_matches_current_installer_and_patch_source(self):
        self.assertEqual(image.PATCH, ACTUAL_PATCH_SHA256)
        self.assertEqual(image.PATCH, installer.PATCH_SHA256)
        self.assertEqual(image.SOURCE, installer.SOURCE)
        self.assertEqual(image.CONTRACT, installer.CONTRACT)

    def test_apt_inputs_are_snapshot_and_version_pinned(self):
        context = self.context()
        manifest = image.copy_apt_inputs(context, "debian-bookworm")
        ubuntu_context = self.root / "ubuntu-context"
        ubuntu_context.mkdir(mode=0o700)
        ubuntu_manifest = image.copy_apt_inputs(ubuntu_context, "ubuntu-noble")
        self.assertEqual(manifest["apt_snapshot"], "20260901T000000Z")
        self.assertEqual(manifest["apt_family"], "debian-bookworm")
        self.assertEqual(manifest["apt_supported_architectures"], ["amd64", "arm64"])
        self.assertEqual(ubuntu_manifest["apt_family"], "ubuntu-noble")
        self.assertEqual(ubuntu_manifest["apt_supported_architectures"], ["amd64", "arm64"])
        exact_pins = {
            "debian-bookworm": {
                "bash": "5.2.15-2+b13", "ca-certificates": "20230311+deb12u1",
                "git": "1:2.39.5-0+deb12u3", "python3": "3.11.2-1+b1",
            },
            "ubuntu-noble": {
                "bash": "5.2.21-2ubuntu4", "ca-certificates": "20260601~24.04.1",
                "git": "1:2.43.0-1ubuntu7.3", "python3": "3.12.3-0ubuntu2.1",
            },
        }
        for family, root in (("debian-bookworm", context), ("ubuntu-noble", ubuntu_context)):
            sources = root / "apt" / f"{family}.sources"
            packages_path = root / "apt" / f"{family}.packages"
            self.assertTrue(sources.is_file())
            packages = packages_path.read_text().splitlines()
            self.assertEqual(set(row.split("=", 1)[0] for row in packages), image.APT_PACKAGES)
            self.assertEqual(dict(row.split("=", 1) for row in packages), exact_pins[family])
        dockerfile = (SCRIPT.parents[1] / "containers/session/Dockerfile").read_text()
        self.assertIn('debian:bookworm:debian-bookworm', dockerfile)
        self.assertIn('ubuntu:noble:ubuntu-noble', dockerfile)
        self.assertIn("apt-get update", dockerfile)
        self.assertIn("apt-get install -y --no-install-recommends", dockerfile)
        self.assertIn("COPY apt/ /opt/doxa-apt/", dockerfile)
        self.assertIn("case \"$arch\" in amd64|arm64)", dockerfile)

    def test_rejects_legacy_protected_patch_receipt(self):
        self.receipt["patch_sha256"] = installer.LEGACY_PATCH_SHA256
        self.save_receipt()
        with self.assertRaises(ValueError):
            image.populate_context(self.context(), self.worker, None, self.provider)

    def test_preserves_verified_receipt_and_excludes_unlisted_secret(self):
        (self.provider / "auth.json").write_text("never copy this")
        context = self.context()
        image.populate_context(context, self.worker, None, self.provider)
        package = context / "codex-provider"
        self.assertEqual((package / "receipt.json").read_bytes(),
                         (self.provider / "receipt.json").read_bytes())
        self.assertEqual({path.name for path in package.iterdir()},
                         set(image.CODEX_FILES) | {"receipt.json"})
        self.assertFalse((package / "auth.json").exists())

    def test_rejects_changed_payload_even_with_valid_contract(self):
        (self.provider / "codex-app-server").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "differs from receipt"):
            image.populate_context(self.context(), self.worker, None, self.provider)

    def test_rejects_symlinked_package_payload(self):
        payload = self.provider / "codex-app-server"
        payload.unlink()
        payload.symlink_to(self.worker)
        with self.assertRaises(OSError):
            image.populate_context(self.context(), self.worker, None, self.provider)

    def test_rejects_foreign_contract_before_copying(self):
        self.receipt["contract"] = "stock provider"
        self.save_receipt()
        with self.assertRaisesRegex(ValueError, "reviewed provider contract"):
            image.populate_context(self.context(), self.worker, None, self.provider)

    def test_rejects_shared_or_public_package(self):
        (self.provider / "receipt.json").chmod(0o644)
        with self.assertRaisesRegex(ValueError, "unsafe provider artifact"):
            image.populate_context(self.context(), self.worker, None, self.provider)

    def test_cargo_hardlinked_worker_is_copied_without_linking_context(self):
        os.link(self.worker, self.root / "cargo-deps-worker")
        context = self.context()
        image.populate_context(context, self.worker, None, None)
        self.assertEqual((context / "worker").read_bytes(), self.worker.read_bytes())
        self.assertEqual((context / "worker").stat().st_nlink, 1)

    def test_protected_package_hardlink_is_rejected(self):
        os.link(self.provider / "codex-app-server", self.root / "shared-payload")
        with self.assertRaisesRegex(ValueError, "unsafe provider artifact"):
            image.populate_context(self.context(), self.worker, None, self.provider)

    def test_fixture_image_contains_no_cli_or_provider_package(self):
        context = self.context()
        evidence = image.populate_context(context, self.worker, None, None)
        self.assertEqual(list((context / "providers").iterdir()), [])
        self.assertEqual(list((context / "codex-provider").iterdir()), [])
        self.assertEqual(evidence["worker_sha256"], hashlib.sha256(self.worker.read_bytes()).hexdigest())
        self.assertEqual(evidence["apt_snapshot"], image.APT_SNAPSHOT)
        self.assertEqual(evidence["apt_family"], "debian-bookworm")
        self.assertEqual(evidence["apt_supported_architectures"], ["amd64", "arm64"])
        self.assertEqual(evidence["apt_sources_sha256"], hashlib.sha256((context / "apt/debian-bookworm.sources").read_bytes()).hexdigest())
        self.assertEqual(evidence["apt_packages_sha256"], hashlib.sha256((context / "apt/debian-bookworm.packages").read_bytes()).hexdigest())
        self.assertEqual(evidence["dockerfile_sha256"], hashlib.sha256((context / "Dockerfile").read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
