"""Focused provider installer transactions; all artifacts and providers are synthetic."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import subprocess
import hashlib
import io
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
        self.helper = self.root / "code-mode-host-built"
        self.helper.write_bytes(b"synthetic compiled helper fixture")
        self.helper.chmod(0o700)
        self.destination = self.root / "providers" / installer.PROVIDER

    def install(self):
        result = installer.install(self.binary, self.root / "providers", Path("/usr/bin/true"), self.launcher, self.helper)
        self.destination = result.parent
        return result

    def test_unchanged_reinstall_is_idempotent(self):
        first = self.install()
        before = {file.name: file.read_bytes() for file in self.destination.iterdir()}
        self.assertEqual(first, self.install())
        self.assertEqual(before, {file.name: file.read_bytes() for file in self.destination.iterdir()})
        self.assertEqual(self.destination, (self.root / "providers/codex-current").resolve())

    def test_legacy_install_is_preserved_during_pointer_migration(self):
        self.install()
        legacy = self.root / "providers" / installer.PROVIDER
        self.destination.rename(legacy)
        (self.root / "providers/codex-current").unlink()
        before = {file.name: file.read_bytes() for file in legacy.iterdir()}
        result = self.install()
        self.assertNotEqual(legacy, result.parent)
        self.assertEqual(before, {file.name: file.read_bytes() for file in legacy.iterdir()})
        self.assertEqual(result.parent, (self.root / "providers/codex-current").resolve())

    def test_corrupt_installed_payload_or_receipt_refuses_publication(self):
        for damage in ("server", "helper", "dispatcher", "receipt"):
            with self.subTest(damage=damage):
                self.install()
                target = {"server": "codex-app-server", "helper": "codex-code-mode-host-payload", "dispatcher": "codex"}.get(damage)
                path = self.destination / (target or "receipt.json")
                original = path.read_bytes()
                if target:
                    path.write_bytes(b"corrupted")
                else:
                    value = json.loads(original)
                    value["binary_sha256"] = "0" * 64
                    path.write_text(json.dumps(value))
                before = os.readlink(self.root / "providers/codex-current")
                with self.assertRaisesRegex(ValueError, "differs"):
                    self.install()
                self.assertEqual(before, os.readlink(self.root / "providers/codex-current"))
                path.write_bytes(original)

    def test_verified_rebuild_publishes_new_immutable_directory(self):
        self.install()
        old = self.destination
        before = {file.name: file.read_bytes() for file in old.iterdir()}
        self.binary.write_text(self.binary.read_text() + "# independently rebuilt fixture\n")
        self.helper.write_bytes(b"independently rebuilt helper")
        with self.assertRaisesRegex(ValueError, "verified build provenance"):
            self.install()
        identity = {"source_commit": installer.SOURCE, "patch_sha256": installer.PATCH_SHA256,
                    "profile": "dev-small", "toolchain": "1.95.0"}
        helper = dict(identity, product="codex-code-mode-host", v8_inputs={"fixture": "reviewed"})
        (self.root / "build.json").write_text(json.dumps(dict(identity, binary_sha256=installer.digest(self.binary))))
        (self.root / "code-mode-host-build.json").write_text(json.dumps(dict(helper, binary_sha256=installer.digest(self.helper))))
        for name in ("build.json", "code-mode-host-build.json"):
            (self.root / name).chmod(0o600)
        verified = installer.verified_artifacts(self.root, self.binary, self.helper, helper)
        result = installer.install(self.binary, self.root / "providers", Path("/usr/bin/true"), self.launcher, self.helper, helper, verified)
        self.assertNotEqual(old, result.parent)
        self.assertEqual(before, {file.name: file.read_bytes() for file in old.iterdir()})
        self.assertEqual(result.parent, (self.root / "providers/codex-current").resolve())
        self.helper.write_bytes(b"corrupted incoming artifact")
        with self.assertRaisesRegex(ValueError, "fingerprints"):
            installer.verified_artifacts(self.root, self.binary, self.helper, helper)

    def test_probe_isolates_operator_home_and_credentials(self):
        with patch.dict(os.environ, {"DOXA_INSTALL_TEST_SECRET": "must-not-reach-provider", "OPENAI_API_KEY": "fixture"}):
            installer.probe(self.binary)
        self.assertEqual([], list(self.root.glob(".codex-probe-*")))

    def test_failed_pointer_publication_keeps_old_install(self):
        self.install()
        old = self.destination
        before = {file.name: file.read_bytes() for file in old.iterdir()}
        self.launcher.write_bytes(b"new dispatcher")
        with patch.object(installer.os, "replace", side_effect=OSError("synthetic publication fault")):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual(before, {file.name: file.read_bytes() for file in old.iterdir()})
        self.assertEqual(old, (self.root / "providers/codex-current").resolve())
        self.assertEqual([], list((self.root / "providers").glob(".codex-stage-*")))

    def test_explicit_wrong_compiler_refuses_and_environment_drops_overrides(self):
        for name in ("cargo", "rustc"):
            file = self.root / name
            file.write_text("#!/bin/sh\necho '%s 1.93.0 (fixture)'\n" % name)
            file.chmod(0o700)
        with self.assertRaisesRegex(ValueError, "both be Rust 1.95.0"):
            installer.toolchain(self.root, str(self.root / "cargo"))
        injected = {"RUSTC": "/unsafe/compiler", "RUSTC_WRAPPER": "/unsafe/wrapper", "RUSTFLAGS": "unsafe",
                    "RUSTY_V8_ARCHIVE": "/unsafe/archive", "RUSTY_V8_MIRROR": "https://unsafe.invalid",
                    "RUSTY_V8_SRC_BINDING_PATH": "/unsafe/bindings", "V8_FROM_SOURCE": "1", "V8_FORCE_DEBUG": "1",
                    "DENO_TRYBUILD": "1", "DOCS_RS": "1", "CARGO_BUILD_TARGET": "unreviewed-target"}
        with patch.dict(os.environ, injected):
            environment = installer.build_environment({"RUSTC": "/reviewed/compiler"})
        self.assertEqual("/reviewed/compiler", environment["RUSTC"])
        self.assertNotIn("RUSTC_WRAPPER", environment)
        self.assertNotIn("RUSTFLAGS", environment)
        for key in injected:
            if key != "RUSTC":
                self.assertNotIn(key, environment)

    def test_v8_inputs_verify_source_manifest_and_archive_binding_bytes(self):
        target = "x86_64-unknown-linux-gnu"
        source = self.root / "v8-source"
        (source / "codex-rs").mkdir(parents=True)
        (source / "third_party/v8").mkdir(parents=True)
        (source / "codex-rs/Cargo.lock").write_text('[[package]]\nname="v8"\nversion="150.4.0"\n')
        archive = f"librusty_v8_ptrcomp_sandbox_release_{target}.a.gz"
        binding = f"src_binding_ptrcomp_sandbox_release_{target}.rs"
        name = f"rusty_v8_ptrcomp_sandbox_release_{target}.sha256"
        artifacts = {archive: b"approved archive fixture", binding: b"approved binding fixture"}
        manifest = "".join(f"{hashlib.sha256(data).hexdigest()}  {artifact}\n" for artifact, data in artifacts.items()).encode()
        artifacts[name] = manifest
        (source / "third_party/v8/rusty_v8_150_4_0_release_manifests.sha256").write_text(f"{hashlib.sha256(manifest).hexdigest()}  {name}\n")
        def response(url, **_):
            self.assertTrue(url.startswith("https://github.com/openai/codex/releases/download/rusty-v8-v150.4.0/"))
            return io.BytesIO(artifacts[url.rsplit("/", 1)[1]])
        with patch.object(installer.urllib.request, "urlopen", side_effect=response):
            environment, identity = installer.v8_inputs(self.root, source, target)
            self.assertEqual(hashlib.sha256(artifacts[archive]).hexdigest(), identity["archive_sha256"])
            self.assertEqual(hashlib.sha256(artifacts[binding]).hexdigest(), identity["binding_sha256"])
            self.assertEqual(artifacts[archive], Path(environment["RUSTY_V8_ARCHIVE"]).read_bytes())
            Path(environment["RUSTY_V8_ARCHIVE"]).write_bytes(b"corrupted cache")
            installer.v8_inputs(self.root, source, target)
            self.assertEqual(artifacts[archive], Path(environment["RUSTY_V8_ARCHIVE"]).read_bytes())
            # A mirror/release delivering different bytes cannot become trusted.
            Path(environment["RUSTY_V8_ARCHIVE"]).unlink()
            artifacts[archive] = b"unreviewed native archive"
            with self.assertRaisesRegex(ValueError, "pinned checksum"):
                installer.v8_inputs(self.root, source, target)
            self.assertFalse(Path(environment["RUSTY_V8_ARCHIVE"]).exists())

    def test_native_input_download_bounds_and_checksum_fail_before_publication(self):
        path = self.root / "input"
        for data, bound in [(b"wrong", 100), (b"over bound", 1)]:
            with patch.object(installer.urllib.request, "urlopen", return_value=io.BytesIO(data)):
                with self.assertRaises(ValueError):
                    installer.verified_download(path, "https://fixture.invalid/input", "0" * 64, bound)
            self.assertFalse(path.exists())
            self.assertEqual([], list(self.root.glob(".v8-download-*")))

    def test_explicit_rustup_proxy_keeps_cargo_and_rustc_dispatch_names(self):
        rustup = self.root / "rustup"
        rustup.write_text("#!/bin/sh\nname=${0##*/}\necho \"$name 1.95.0 (fixture)\"\n")
        rustup.chmod(0o700)
        for name in ("cargo", "rustc"):
            (self.root / name).symlink_to(rustup)
        cargo, environment = installer.toolchain(self.root, str(self.root / "cargo"))
        self.assertEqual(str(self.root / "cargo"), cargo)
        self.assertEqual(str(self.root / "rustc"), environment["RUSTC"])

    def test_source_verification_rejects_unrelated_staged_changes(self):
        source = self.root / "source"
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.name", "Fixture"], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.email", "fixture@example.invalid"], check=True)
        first, unrelated = source / "first", source / "Cargo.toml"
        first.write_text("original\n")
        unrelated.write_text("original package\n")
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-qm", "test: fixture source"], check=True)
        head = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        first.write_text("reviewed change\n")
        patch_file = self.root / "fixture.patch"
        patch_file.write_bytes(subprocess.check_output(["git", "-C", str(source), "diff", "--binary"]))
        with patch.multiple(installer, SOURCE=head, PATCH=patch_file, PATCH_SHA256=installer.digest(patch_file)):
            self.assertEqual(source, installer.prepare_source(self.root))
            unrelated.write_text("unreviewed staged package\n")
            subprocess.run(["git", "-C", str(source), "add", "Cargo.toml"], check=True)
            with self.assertRaisesRegex(ValueError, "do not match the reviewed patch"):
                installer.prepare_source(self.root)


if __name__ == "__main__":
    unittest.main()
