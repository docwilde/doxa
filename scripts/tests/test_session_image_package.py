"""The image context must carry only verified binaries, never host secrets."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "build-session-image.py"
spec = importlib.util.spec_from_file_location("session_image", SCRIPT)
image = importlib.util.module_from_spec(spec)
spec.loader.exec_module(image)
installer_spec = importlib.util.spec_from_file_location(
    "protected_installer", SCRIPT.parent / "install_codex_protected.py")
installer = importlib.util.module_from_spec(installer_spec)
installer_spec.loader.exec_module(installer)
ACTUAL_PATCH_SHA256 = hashlib.sha256(installer.PATCH.read_bytes()).hexdigest()
NOBLE = "ubuntu-noble"


def elf_needing(*versions, soname=b"libc.so.6"):
    """Minimal little-endian ELF64 whose .gnu.version_r needs GLIBC versions."""
    strings = b"\0"
    offsets = {}
    for name in [soname, *(f"GLIBC_{version}".encode() for version in versions)]:
        offsets[name] = len(strings)
        strings += name + b"\0"
    aux = b"".join(struct.pack("<IHHII", 0, 0, index + 2, offsets[f"GLIBC_{version}".encode()],
                               16 if index + 1 < len(versions) else 0)
                   for index, version in enumerate(versions))
    needed = struct.pack("<HHIII", 1, len(versions), offsets[soname], 16, 0) + aux
    strings_at = 64
    needed_at = strings_at + len(strings)
    padding = (-(needed_at + len(needed))) % 8
    sections_at = needed_at + len(needed) + padding
    sections = [struct.pack("<IIQQQQIIQQ", *([0] * 10)),
                struct.pack("<IIQQQQIIQQ", 0, 3, 0, 0, strings_at, len(strings), 0, 0, 1, 0),
                struct.pack("<IIQQQQIIQQ", 0, 0x6FFFFFFE, 0, 0, needed_at, len(needed), 1, 1, 8, 0)]
    header = b"\x7fELF" + bytes([2, 1, 1]) + bytes(9) + struct.pack(
        "<HHIQQQIHHHHHH", 2, 62, 1, 0, 0, sections_at, 0, 64, 0, 0, 64, len(sections), 0)
    return header + strings + needed + bytes(padding) + b"".join(sections)


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
                "ripgrep": "13.0.0-4+b2",
            },
            "ubuntu-noble": {
                "bash": "5.2.21-2ubuntu4", "ca-certificates": "20260601~24.04.1",
                "git": "1:2.43.0-1ubuntu7.3", "python3": "3.12.3-0ubuntu2.1",
                "ripgrep": "14.1.0-1",
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

    def test_snapshots_use_verified_https_with_the_pinned_ca_bundle(self):
        for family in sorted(image.APT_FAMILIES):
            context = self.root / f"https-{family}"
            context.mkdir(mode=0o700)
            manifest = image.copy_apt_inputs(context, family)
            entries = [row for row in (context / "apt" / f"{family}.sources").read_text().splitlines()
                       if row.startswith("deb ")]
            self.assertEqual(len(entries), 3)
            self.assertTrue(all(" https://snapshot." in row for row in entries), entries)
            bundle = (context / "apt" / image.APT_CA_BUNDLE).read_bytes()
            self.assertEqual(hashlib.sha256(bundle).hexdigest(), image.APT_CA_SHA256)
            self.assertEqual(manifest["apt_ca_sha256"], image.APT_CA_SHA256)
            self.assertEqual(bundle.count(b"-----BEGIN CERTIFICATE-----"), 2)
        dockerfile = (SCRIPT.parents[1] / "containers/session/Dockerfile").read_text()
        self.assertIn('Acquire::https::CAInfo "/opt/doxa-apt/snapshot-ca.pem";', dockerfile)
        self.assertIn('Acquire::https::Verify-Peer "true";', dockerfile)
        self.assertNotIn("Verify-Peer \"false\"", dockerfile)

    def test_plain_http_sources_or_a_changed_ca_bundle_are_refused(self):
        fake = self.root / "repo"
        shutil.copytree(SCRIPT.parents[1] / "containers/session", fake / "containers/session")
        sources = fake / "containers/session/apt/ubuntu-noble.sources"
        sources.write_text(sources.read_text().replace("https://", "http://"))
        with mock.patch.object(image, "REPO", fake):
            target = self.root / "http-context"
            target.mkdir(mode=0o700)
            with self.assertRaisesRegex(ValueError, "verified HTTPS"):
                image.copy_apt_inputs(target, NOBLE)
            bundle = fake / "containers/session/apt" / image.APT_CA_BUNDLE
            bundle.write_bytes(bundle.read_bytes() + b"\n")
            target = self.root / "ca-context"
            target.mkdir(mode=0o700)
            with self.assertRaisesRegex(ValueError, "CA bundle differs"):
                image.copy_apt_inputs(target, "debian-bookworm")

    def test_codex_provider_is_refused_on_a_bookworm_glibc_base(self):
        context = self.context()
        with self.assertRaisesRegex(ValueError, r"GLIBC_2\.39.*debian-bookworm ships glibc 2\.36.*ubuntu-noble"):
            image.populate_context(context, self.worker, None, self.provider, "debian-bookworm")
        self.assertFalse((context / "codex-provider").exists(), "refusal must precede copying")
        with self.assertRaisesRegex(ValueError, "glibc 2.36"):
            image.copy_codex_package(self.provider, self.context_dir("direct"), "debian-bookworm")
        # Bookworm stays valid without the protected Codex provider.
        evidence = image.populate_context(self.context_dir("fixture"), self.worker, None, None, "debian-bookworm")
        self.assertEqual(evidence["base_glibc"], "2.36")
        self.assertNotIn("codex", evidence)

    def test_noble_codex_records_the_glibc_floor(self):
        evidence = image.populate_context(self.context(), self.worker, None, self.provider, NOBLE)
        self.assertEqual(evidence["codex"]["glibc_required"], "2.39")
        self.assertEqual(evidence["base_glibc"], "2.39")

    def test_codex_payload_needing_newer_glibc_is_refused_even_on_noble(self):
        payload = elf_needing("2.34", "2.41", "2.17")
        (self.provider / "codex-code-mode-host-payload").write_bytes(payload)
        self.receipt["code_mode_host_sha256"] = hashlib.sha256(payload).hexdigest()
        self.save_receipt()
        with self.assertRaisesRegex(ValueError, r"codex-code-mode-host-payload requires GLIBC_2\.41"):
            image.populate_context(self.context(), self.worker, None, self.provider, NOBLE)

    def test_elf_glibc_requirement_reads_version_needs(self):
        self.assertEqual(image.glibc_requirement(elf_needing("2.2.5", "2.39", "2.34")), (2, 39))
        self.assertIsNone(image.glibc_requirement(b"not an executable"))
        self.assertIsNone(image.glibc_requirement(elf_needing("2.39")[:80]))
        self.assertIsNone(image.glibc_requirement(elf_needing("2.39", soname=b"libother.so")
                                                  .replace(b"GLIBC_2.39", b"OTHER_2.39")))

    def test_cli_refuses_codex_on_bookworm_before_contacting_docker(self):
        result = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), "--base-image", "debian@sha256:" + "0" * 64,
             "--base-family", "debian-bookworm", "--worker", str(self.worker),
             "--codex-provider", str(self.provider), "--docker-host", "unix:///nonexistent/docker.sock"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("requires GLIBC_2.39", result.stderr)
        self.assertNotIn("rootless", result.stderr)

    def test_default_cache_is_a_user_cache_outside_the_checkout(self):
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "xdg")}):
            self.assertEqual(image.default_cache(), self.root / "xdg/doxa/session-image")
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": "relative/cache", "HOME": str(self.root / "home")}):
            self.assertEqual(image.default_cache(), self.root / "home/.cache/doxa/session-image")
        with mock.patch.dict(os.environ, {"HOME": str(self.root / "home")}):
            os.environ.pop("XDG_CACHE_HOME", None)
            cache = image.default_cache()
        self.assertEqual(cache, self.root / "home/.cache/doxa/session-image")
        self.assertFalse(cache.is_relative_to(SCRIPT.parents[1]))

    def context_dir(self, name):
        path = self.root / name
        path.mkdir(mode=0o700)
        return path

    def test_rejects_legacy_protected_patch_receipt(self):
        self.receipt["patch_sha256"] = installer.LEGACY_PATCH_SHA256
        self.save_receipt()
        with self.assertRaisesRegex(ValueError, "reviewed provider contract"):
            image.populate_context(self.context(), self.worker, None, self.provider, NOBLE)

    def test_preserves_verified_receipt_and_excludes_unlisted_secret(self):
        (self.provider / "auth.json").write_text("never copy this")
        context = self.context()
        image.populate_context(context, self.worker, None, self.provider, NOBLE)
        package = context / "codex-provider"
        self.assertEqual((package / "receipt.json").read_bytes(),
                         (self.provider / "receipt.json").read_bytes())
        self.assertEqual({path.name for path in package.iterdir()},
                         set(image.CODEX_FILES) | {"receipt.json"})
        self.assertFalse((package / "auth.json").exists())

    def test_rejects_changed_payload_even_with_valid_contract(self):
        (self.provider / "codex-app-server").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "differs from receipt"):
            image.populate_context(self.context(), self.worker, None, self.provider, NOBLE)

    def test_rejects_symlinked_package_payload(self):
        payload = self.provider / "codex-app-server"
        payload.unlink()
        payload.symlink_to(self.worker)
        with self.assertRaises(OSError):
            image.populate_context(self.context(), self.worker, None, self.provider, NOBLE)

    def test_rejects_foreign_contract_before_copying(self):
        self.receipt["contract"] = "stock provider"
        self.save_receipt()
        with self.assertRaisesRegex(ValueError, "reviewed provider contract"):
            image.populate_context(self.context(), self.worker, None, self.provider, NOBLE)

    def test_rejects_shared_or_public_package(self):
        (self.provider / "receipt.json").chmod(0o644)
        with self.assertRaisesRegex(ValueError, "unsafe provider artifact"):
            image.populate_context(self.context(), self.worker, None, self.provider, NOBLE)

    def test_cargo_hardlinked_worker_is_copied_without_linking_context(self):
        os.link(self.worker, self.root / "cargo-deps-worker")
        context = self.context()
        image.populate_context(context, self.worker, None, None)
        self.assertEqual((context / "worker").read_bytes(), self.worker.read_bytes())
        self.assertEqual((context / "worker").stat().st_nlink, 1)

    def test_protected_package_hardlink_is_rejected(self):
        os.link(self.provider / "codex-app-server", self.root / "shared-payload")
        with self.assertRaisesRegex(ValueError, "unsafe provider artifact"):
            image.populate_context(self.context(), self.worker, None, self.provider, NOBLE)

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
