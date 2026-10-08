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


class PackageTests(unittest.TestCase):
    def setUp(self):
        directory = SCRIPT.parents[1] / "target/script-tests"
        directory.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=directory)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.worker = self.root / "worker-source"
        self.worker.write_bytes(b"reviewed worker")
        self.worker.chmod(0o700)
        self.provider = self.root / "provider"
        self.provider.mkdir(mode=0o700)
        self.receipt = {"contract": image.CONTRACT, "source_commit": image.SOURCE,
                        "patch_sha256": image.PATCH,
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

    def test_fixture_image_contains_no_cli_or_provider_package(self):
        context = self.context()
        evidence = image.populate_context(context, self.worker, None, None)
        self.assertEqual(list((context / "providers").iterdir()), [])
        self.assertEqual(list((context / "codex-provider").iterdir()), [])
        self.assertEqual(evidence, {"worker_sha256": hashlib.sha256(self.worker.read_bytes()).hexdigest()})


if __name__ == "__main__":
    unittest.main()
