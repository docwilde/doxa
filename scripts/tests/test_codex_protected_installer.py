"""Focused provider installer transactions; all artifacts and providers are synthetic."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("protected_installer", Path(__file__).resolve().parents[1] / "install_codex_protected.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="installer-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.binary = self.root / "server"
        # The real initialize probe must receive a fresh home and no credentials.
        self.binary.write_text("""#!/usr/bin/python3
import json,os,sys
from pathlib import Path
assert 'DOXA_INSTALL_TEST_SECRET' not in os.environ
assert 'OPENAI_API_KEY' not in os.environ
assert Path(os.environ['HOME']).parent == Path(__file__).parent
assert Path(os.environ['CODEX_HOME']).parent == Path(os.environ['HOME'])
assert Path.cwd() == Path(os.environ['HOME'])
assert not Path(os.environ['CODEX_HOME'],'auth.json').exists()
query=json.loads(sys.stdin.readline())
print(json.dumps({'id':query['id'],'result':{'userAgent':%r}}),flush=True)
sys.stdin.read()
""" % (installer.AGENT_PREFIX + "fixture)"))
        self.binary.chmod(0o700)
        self.launcher = self.root / "launcher"
        self.launcher.write_bytes(b"synthetic native dispatcher fixture")
        self.launcher.chmod(0o700)
        self.destination = self.root / "providers" / installer.PROVIDER

    def install(self):
        return installer.install(self.binary, self.root / "providers", Path("/usr/bin/true"), self.launcher)

    def test_probe_isolates_operator_home_and_credentials(self):
        with patch.dict(os.environ, {"DOXA_INSTALL_TEST_SECRET": "must-not-reach-provider", "OPENAI_API_KEY": "fixture"}):
            installer.probe(self.binary)
        self.assertEqual([], list(self.root.glob(".codex-probe-*")))

    def test_refresh_repairs_corrupted_and_missing_installed_payload(self):
        self.install()
        payload = self.destination / "codex-app-server"
        payload.write_bytes(b"damaged")
        self.launcher.write_bytes(b"new dispatcher")
        self.install()
        self.assertEqual(self.binary.read_bytes(), payload.read_bytes())
        self.assertEqual(self.launcher.read_bytes(), (self.destination / "codex").read_bytes())
        payload.unlink()
        self.install()
        self.assertEqual(self.binary.read_bytes(), payload.read_bytes())

    def test_failed_exchange_keeps_complete_old_install(self):
        self.install()
        before = {file.name: file.read_bytes() for file in self.destination.iterdir()}
        self.launcher.write_bytes(b"new dispatcher")
        with patch.object(installer, "exchange_directories", side_effect=OSError("synthetic exchange fault")):
            with self.assertRaises(OSError):
                self.install()
        after = {file.name: file.read_bytes() for file in self.destination.iterdir()}
        self.assertEqual(before, after)
        self.assertEqual([], list((self.root / "providers").glob(".codex-stage-*")))

    def test_explicit_wrong_compiler_refuses_and_environment_drops_overrides(self):
        for name in ("cargo", "rustc"):
            file = self.root / name
            file.write_text("#!/bin/sh\necho '%s 1.93.0 (fixture)'\n" % name)
            file.chmod(0o700)
        with self.assertRaisesRegex(ValueError, "both be Rust 1.95.0"):
            installer.toolchain(self.root, str(self.root / "cargo"))
        with patch.dict(os.environ, {"RUSTC": "/unsafe/compiler", "RUSTC_WRAPPER": "/unsafe/wrapper", "RUSTFLAGS": "unsafe"}):
            environment = installer.build_environment({"RUSTC": "/reviewed/compiler"})
        self.assertEqual("/reviewed/compiler", environment["RUSTC"])
        self.assertNotIn("RUSTC_WRAPPER", environment)
        self.assertNotIn("RUSTFLAGS", environment)

    def test_explicit_rustup_proxy_keeps_cargo_and_rustc_dispatch_names(self):
        rustup = self.root / "rustup"
        rustup.write_text("#!/bin/sh\nname=${0##*/}\necho \"$name 1.95.0 (fixture)\"\n")
        rustup.chmod(0o700)
        for name in ("cargo", "rustc"):
            (self.root / name).symlink_to(rustup)
        cargo, environment = installer.toolchain(self.root, str(self.root / "cargo"))
        self.assertEqual(str(self.root / "cargo"), cargo)
        self.assertEqual(str(self.root / "rustc"), environment["RUSTC"])


if __name__ == "__main__":
    unittest.main()
