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
update_spec = importlib.util.spec_from_file_location("protected_update_fixture", Path(__file__).resolve().parents[1] / "codex-update/update.py")
updater = importlib.util.module_from_spec(update_spec)
update_spec.loader.exec_module(updater)


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

    def test_outer_receipt_restores_legacy_pointer_after_publication(self):
        self.install()
        legacy=self.root/"providers"/installer.PROVIDER
        self.destination.rename(legacy)
        (self.root/"providers/codex-current").unlink()
        receipt=self.root/"rollback.json"
        installer.install(self.binary,self.root/"providers",Path("/usr/bin/true"),self.launcher,self.helper,rollback_receipt=receipt)
        self.assertTrue((self.root/"providers/codex-current").is_symlink())
        installer.rollback_publication(receipt)
        self.assertFalse((self.root/"providers/codex-current").exists())
        self.assertEqual(installer.active_installation(self.root/"providers"),legacy)

    def test_rollback_refuses_intervening_same_artifact_publication(self):
        self.install()
        receipt=self.root/"rollback.json"
        installer.install(self.binary,self.root/"providers",Path("/usr/bin/true"),self.launcher,self.helper,rollback_receipt=receipt)
        # A second install creates a different pointer inode even when bytes
        # and the destination match. The first transaction cannot undo it.
        self.install()
        before=os.readlink(self.root/"providers/codex-current")
        with self.assertRaisesRegex(ValueError,"pointer changed"):
            installer.rollback_publication(receipt)
        self.assertEqual(before,os.readlink(self.root/"providers/codex-current"))

    def test_rollback_refuses_unsafe_receipt_without_changing_provider(self):
        self.install()
        receipt=self.root/"rollback.json"
        installer.install(self.binary,self.root/"providers",Path("/usr/bin/true"),self.launcher,self.helper,rollback_receipt=receipt)
        receipt.chmod(0o644)
        before=os.readlink(self.root/"providers/codex-current")
        with self.assertRaises(ValueError): installer.rollback_publication(receipt)
        self.assertEqual(before,os.readlink(self.root/"providers/codex-current"))

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
                    "profile": "dev-small", "toolchain": "1.99.1"}
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

    def test_verified_patch_upgrade_keeps_the_previous_provider_immutable(self):
        self.install()
        receipt_path = self.destination / "receipt.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["source_commit"] = installer.PREVIOUS_SOURCE
        receipt["code_mode_host_source_commit"] = installer.PREVIOUS_SOURCE
        receipt["patch_sha256"] = next(value for value in installer.PREVIOUS_PATCHES if value != installer.LEGACY_PATCH_SHA256)
        receipt["toolchain"] = "1.95.0"
        receipt_path.write_text(json.dumps(receipt, sort_keys=True) + "\n")
        old = self.root / "providers" / (installer.PREVIOUS_PROVIDER + "-" + installer.digest(receipt_path))
        self.destination.rename(old)
        pointer = self.root / "providers/codex-current"
        pointer.unlink()
        pointer.symlink_to(old.name)
        before = {file.name: file.read_bytes() for file in old.iterdir()}
        with self.assertRaisesRegex(ValueError, "verified build provenance"):
            self.install()
        self.assertEqual(pointer.resolve(), old)
        identity = {"source_commit": installer.SOURCE, "patch_sha256": installer.PATCH_SHA256,
                    "profile": "dev-small", "toolchain": "1.99.1"}
        helper = dict(identity, product="codex-code-mode-host", v8_inputs={"fixture": "reviewed"})
        for filename, value in (("build.json", dict(identity, binary_sha256=installer.digest(self.binary))),
                                ("code-mode-host-build.json", dict(helper, binary_sha256=installer.digest(self.helper)))):
            path = self.root / filename
            path.write_text(json.dumps(value))
            path.chmod(0o600)
        verified = installer.verified_artifacts(self.root, self.binary, self.helper, helper)
        rollback = self.root / "upgrade-rollback.json"
        result = installer.install(self.binary, self.root / "providers", Path("/usr/bin/true"),
                                   self.launcher, self.helper, helper, verified, rollback_receipt=rollback)
        self.assertNotEqual(result.parent, old)
        self.assertEqual(before, {file.name: file.read_bytes() for file in old.iterdir()})
        self.assertEqual(pointer.resolve(), result.parent)
        installer.rollback_publication(rollback)
        self.assertEqual(pointer.resolve(), old)
        self.assertEqual(before, {file.name: file.read_bytes() for file in old.iterdir()})
        (old / "codex-code-mode-host-payload").write_bytes(b"corrupt old helper")
        with self.assertRaisesRegex(ValueError, "artifact differs"):
            self.install()
        self.assertEqual(pointer.resolve(), old)

    def test_two_release_rotations_upgrade_and_rollback_an_older_reviewed_installation(self):
        self.install()
        old = self.destination
        old_bytes = {file.name:file.read_bytes() for file in old.iterdir()}
        old_receipt = json.loads((old/'receipt.json').read_text())
        fixture = self.root/'future-source'
        source = fixture/'scripts/install_codex_protected.py'
        source.parent.mkdir(parents=True)
        source.write_text(Path(installer.__file__).read_text())
        files = {
            'rust/doxa-tui/Cargo.toml':'[package]\nname = "doxa-tui"\nversion = "2.0.0-beta.65"\n',
            'Cargo.lock':'version = 4\n\n[[package]]\nname = "doxa-tui"\nversion = "2.0.0-beta.65"\n',
            'CHANGELOG.md':'# Changelog\n\n## 2.0.0-beta.65 — 2026-10-21\n\nSynthetic fixture.\n',
        }
        for name, raw in files.items():
            path=fixture/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_text(raw)
        pin = dict(version=installer.SOURCE_TAG.removeprefix('rust-v'),tag=installer.SOURCE_TAG,
                   source_commit=installer.SOURCE,patch_sha256=installer.PATCH_SHA256,provider=installer.PROVIDER)
        for version, digit in [('0.165.0','a'),('0.166.0','b')]:
            carried = ('synthetic reviewed protection '+version+'\n').encode()
            new = dict(pin,version=version,tag='rust-v'+version,source_commit=digit*40,
                       patch_sha256=hashlib.sha256(carried).hexdigest(),provider='codex-'+version+'-precompact-v1')
            revised,_,_=updater.replacement_files(fixture,pin,new,carried,'2026-10-21')
            for name,raw in revised.items():
                path=fixture/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(raw)
            pin=new
        future_spec=importlib.util.spec_from_file_location('twice_rotated_installer',source)
        future=importlib.util.module_from_spec(future_spec); future_spec.loader.exec_module(future)
        self.assertEqual('codex-0.165.0-precompact-v1',future.PREVIOUS_PROVIDER)
        self.assertEqual(installer.PROVIDER,future.receipt_provider(old_receipt))
        self.assertIn((installer.SOURCE,installer.PATCH_SHA256,installer.PROVIDER),future.REVIEWED_INSTALLATIONS)
        self.assertTrue(future.artifact_name(old.name))
        self.assertFalse(future.artifact_name('codex-99.0.0-precompact-v1-'+'0'*64))
        future.verify_installed(old,old_receipt)
        self.assertEqual(old,future.active_installation(self.root/'providers'))
        for change in ({'source_commit':'0'*40},{'patch_sha256':'0'*64},
                       {'patch_sha256':installer.LEGACY_PATCH_SHA256},{'source_commit':[]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                future.verify_installed(old,dict(old_receipt,**change))

        self.binary.write_text(self.binary.read_text().replace(installer.AGENT_PREFIX,future.AGENT_PREFIX))
        identity=dict(source_commit=future.SOURCE,patch_sha256=future.PATCH_SHA256,profile='dev-small',toolchain='1.99.1')
        helper=dict(identity,product='codex-code-mode-host',v8_inputs={'synthetic':'reviewed'})
        for name,data in [('build.json',dict(identity,binary_sha256=future.digest(self.binary))),
                          ('code-mode-host-build.json',dict(helper,binary_sha256=future.digest(self.helper)))]:
            path=self.root/name; path.write_text(json.dumps(data)); path.chmod(0o600)
        verified=future.verified_artifacts(self.root,self.binary,self.helper,helper)
        self.assertEqual(future.digest(future.PATCH),verified['patch_sha256'])
        self.assertEqual(future.SOURCE,verified['source_commit'])
        rollback=self.root/'skipped-release-rollback.json'
        installed=future.install(self.binary,self.root/'providers',Path('/usr/bin/true'),self.launcher,
                                 self.helper,helper,verified,rollback_receipt=rollback)
        pointer=self.root/'providers/codex-current'
        self.assertEqual(installed.parent,pointer.resolve())
        self.assertEqual(old_bytes,{file.name:file.read_bytes() for file in old.iterdir()})
        future.rollback_publication(rollback)
        self.assertEqual(old,pointer.resolve())
        self.assertEqual(old_bytes,{file.name:file.read_bytes() for file in old.iterdir()})

        rollback2=self.root/'corrupt-old-rollback.json'
        future.install(self.binary,self.root/'providers',Path('/usr/bin/true'),self.launcher,
                       self.helper,helper,verified,rollback_receipt=rollback2)
        (old/'codex-code-mode-host-payload').write_bytes(b'corrupt older reviewed helper')
        with self.assertRaisesRegex(ValueError,'artifact differs'):
            future.rollback_publication(rollback2)
        self.assertEqual(installed.parent,pointer.resolve())

    def test_reviewed_registry_is_bounded_and_rejects_malformed_or_ambiguous_identities(self):
        for history in ([('a'*40,'b'*64,'codex-0.162.0-precompact-v1')],
                        (('a'*40,'bad','codex-0.162.0-precompact-v1'),),
                        ((installer.SOURCE,installer.PATCH_SHA256,'codex-99.0.0-precompact-v1'),),
                        tuple(('a'*40,f'{i:064x}','codex-0.162.0-precompact-v1') for i in range(257))):
            with self.subTest(history_length=len(history)), patch.object(installer,'REVIEWED_INSTALLATIONS',history):
                with self.assertRaises(ValueError):
                    installer.reviewed_installations()

    def test_cargo_mode_is_accepted_only_for_verified_build_outputs(self):
        identity = {"source_commit": installer.SOURCE, "patch_sha256": installer.PATCH_SHA256,
                    "profile": "dev-small", "toolchain": "1.99.1"}
        helper_identity = dict(identity, product="codex-code-mode-host", v8_inputs={"fixture": "reviewed"})
        for executable in (self.binary, self.helper):
            executable.chmod(0o755)  # Real Cargo outputs use this mode.
        os.link(self.binary, self.root / "cached-server-link")
        os.link(self.helper, self.root / "cached-helper-link")
        self.assertEqual(self.binary.stat().st_nlink, 2)
        (self.root / "build.json").write_text(json.dumps(dict(identity, binary_sha256=installer.digest(self.binary))))
        (self.root / "code-mode-host-build.json").write_text(json.dumps(dict(helper_identity, binary_sha256=installer.digest(self.helper))))
        for name in ("build.json", "code-mode-host-build.json"):
            (self.root / name).chmod(0o600)

        verified = installer.verified_artifacts(self.root, self.binary, self.helper, helper_identity)
        installed = installer.install(self.binary, self.root / "providers", Path("/usr/bin/true"),
                                      self.launcher, self.helper, helper_identity, verified).parent
        for name in ("codex-app-server", "codex", "codex-code-mode-host", "codex-code-mode-host-payload"):
            self.assertEqual((installed / name).stat().st_mode & 0o777, 0o700)
        self.assertEqual((installed / "receipt.json").stat().st_mode & 0o777, 0o600)

        (installed / "codex-app-server").chmod(0o755)
        with self.assertRaisesRegex(ValueError, "installed provider file"):
            installer.install(self.binary, self.root / "providers", Path("/usr/bin/true"),
                              self.launcher, self.helper, helper_identity, verified)
        (installed / "codex-app-server").chmod(0o700)
        self.helper.chmod(0o775)
        with self.assertRaisesRegex(ValueError, "cached Cargo output"):
            installer.verified_artifacts(self.root, self.binary, self.helper, helper_identity)
        self.helper.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "cached Cargo output"):
            installer.verified_artifacts(self.root, self.binary, self.helper, helper_identity)

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
        with self.assertRaisesRegex(ValueError, "both be Rust 1.99.1"):
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

    def test_old_toolchain_receipt_is_bound_to_reviewed_source_and_patch(self):
        self.install()
        receipt = json.loads((self.destination / "receipt.json").read_text())
        source = "83086188ae0614bd882818ff1aa23f3194962012"
        patch_sha = "3a7b8c09b1f95bc523611202005aa71e6ba14d23f26a3833249487385e013e9c"
        old = dict(receipt, source_commit=source, patch_sha256=patch_sha,
                   code_mode_host_source_commit=source, toolchain="1.95.0")
        installer.verify_installed(self.destination, old)
        # Compiler provenance cannot be relabelled in either direction.
        with self.assertRaisesRegex(ValueError, "provenance"):
            installer.verify_installed(self.destination, dict(old, toolchain="1.99.1"))
        with self.assertRaisesRegex(ValueError, "provenance"):
            installer.verify_installed(self.destination, dict(receipt, toolchain="1.95.0"))
        with self.assertRaisesRegex(ValueError, "reviewed release identity"):
            installer.verify_installed(self.destination, dict(old, patch_sha256="0" * 64))

    def test_v8_inputs_verify_source_manifest_and_archive_binding_bytes(self):
        target = "x86_64-unknown-linux-gnu"
        source = self.root / "v8-source"
        (source / "codex-rs").mkdir(parents=True)
        (source / "third_party/v8").mkdir(parents=True)
        (source / "codex-rs/Cargo.lock").write_text('[[package]]\nname="v8"\nversion="152.2.0"\n')
        archive = f"librusty_v8_ptrcomp_sandbox_release_{target}.a.gz"
        binding = f"src_binding_ptrcomp_sandbox_release_{target}.rs"
        name = f"rusty_v8_ptrcomp_sandbox_release_{target}.sha256"
        artifacts = {archive: b"approved archive fixture", binding: b"approved binding fixture"}
        manifest = "".join(f"{hashlib.sha256(data).hexdigest()}  {artifact}\n" for artifact, data in artifacts.items()).encode()
        artifacts[name] = manifest
        (source / "third_party/v8/rusty_v8_152_2_0_release_manifests.sha256").write_text(f"{hashlib.sha256(manifest).hexdigest()}  {name}\n")
        def response(url, **_):
            self.assertTrue(url.startswith("https://github.com/openai/codex/releases/download/rusty-v8-v152.2.0/"))
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
        rustup.write_text("#!/bin/sh\nname=${0##*/}\necho \"$name 1.99.1 (fixture)\"\n")
        rustup.chmod(0o700)
        for name in ("cargo", "rustc"):
            (self.root / name).symlink_to(rustup)
        cargo, environment = installer.toolchain(self.root, str(self.root / "cargo"))
        self.assertEqual(str(self.root / "cargo"), cargo)
        self.assertEqual(str(self.root / "rustc"), environment["RUSTC"])

    def test_upstream_upgrade_uses_separate_pinned_checkout_and_preserves_old_cache(self):
        source = self.root / "source"
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.name", "Fixture"], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.email", "fixture@example.invalid"], check=True)
        gate = source / "codex-rs/core/src/doxa_precompact.rs"
        gate.parent.mkdir(parents=True)
        gate.write_text("original\n")
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-qm", "test: fixture source"], check=True)
        subprocess.run(["git", "-C", str(source), "tag", "fixture-release"], check=True)
        head = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        gate.write_text("new reviewed patch\n")
        reviewed = self.root / "fixture.patch"
        reviewed.write_bytes(subprocess.check_output(["git", "-C", str(source), "diff", "HEAD", "--binary"]))
        gate.write_text("retained previous patch\n")
        (source / "untracked").write_text("retained user edit\n")
        before = subprocess.check_output(["git", "-C", str(source), "diff", "HEAD", "--binary"])
        def local_clone(arguments, **kwargs):
            if arguments[:2] == ["git", "clone"]:
                self.assertEqual(arguments[4:6], ["--branch", "fixture-release"])
                arguments = [*arguments[:-2], str(source), arguments[-1]]
            subprocess.run(arguments, check=True, **kwargs)
        with patch.multiple(installer, SOURCE=head, SOURCE_TAG="fixture-release", PATCH=reviewed,
                            PATCH_SHA256=installer.digest(reviewed)), patch.object(installer, "run", side_effect=local_clone):
            updated = installer.prepare_source(self.root)
            self.assertEqual(self.root / ("source-" + head + "-" + installer.digest(reviewed)), updated)
            self.assertEqual("new reviewed patch\n", (updated / gate.relative_to(source)).read_text())
            self.assertEqual(before, subprocess.check_output(["git", "-C", str(source), "diff", "HEAD", "--binary"]))
            self.assertEqual("retained user edit\n", (source / "untracked").read_text())
            self.assertEqual(updated, installer.prepare_source(self.root))
            (updated / gate.relative_to(source)).write_text("unreviewed edit\n")
            with self.assertRaisesRegex(ValueError, "unrelated changes"):
                installer.prepare_source(self.root)
            self.assertEqual("retained previous patch\n", gate.read_text())

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
            versioned = self.root / ("source-" + head + "-" + installer.digest(patch_file))
            source.rename(versioned)
            source = versioned
            unrelated = source / "Cargo.toml"
            self.assertEqual(source, installer.prepare_source(self.root))
            unrelated.write_text("unreviewed staged package\n")
            subprocess.run(["git", "-C", str(source), "add", "Cargo.toml"], check=True)
            with self.assertRaisesRegex(ValueError, "do not match the reviewed patch"):
                installer.prepare_source(self.root)


if __name__ == "__main__":
    unittest.main()
