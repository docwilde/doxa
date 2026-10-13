"""Declared historical captures remain verified across package version bumps."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_gallery_site as builder
import generate_gallery_pages as generator


def png(width=3068, height=1734):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes((width + 1) * height))) + chunk(b"IEND", b""))


class GalleryCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="gallery-contract-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.gallery = self.root / "docs/gallery"
        self.shots = self.root / "assets/shots"
        self.gallery.mkdir(parents=True)
        self.shots.mkdir(parents=True)
        cargo = self.root / "rust/doxa-tui/Cargo.toml"
        cargo.parent.mkdir(parents=True)
        cargo.write_text('[package]\nversion = "2.0.0-beta.51"\n')
        self.image = self.shots / "rust-2.0.0-beta.50-hero.png"
        self.image.write_bytes(png())
        self.record_path = self.shots / "rust-2.0.0-beta.50-capture.json"
        self.record = {"version": "2.0.0-beta.50", "size": [3068, 1734], "captures": [
            {"scene": "hero", "image": self.image.name,
             "sha256": hashlib.sha256(self.image.read_bytes()).hexdigest()}]}
        self.manifest_path = self.gallery / "scenes.json"
        self.manifest = {"capture_version": "2.0.0-beta.50", "default": "hero", "scenes": [
            {"id": "hero", "title": "Workspace", "caption": "Illustrative beta.50 capture.",
             "alt": "Fixture workspace", "image": "../assets/shots/" + self.image.name}]}
        self.write_metadata()
        (self.gallery / "index.html").write_text("<title>Fixture gallery</title>")
        self.readme = self.root / "README.md"
        self.readme.write_text(generator.START + "\nold frame\n" + generator.END)
        for target, name, value in ((builder, "ROOT", self.root), (generator, "ROOT", self.root),
                                    (generator, "SCENE_DIR", self.gallery / "scenes"),
                                    (generator, "README", self.readme)):
            replacement = patch.object(target, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)

    def write_metadata(self):
        self.record_path.write_text(json.dumps(self.record))
        self.manifest_path.write_text(json.dumps(self.manifest))

    def refused_by_both(self, error=ValueError):
        for operation in (lambda: builder.build(self.root / "site"), generator.read_scenes):
            with self.subTest(operation=operation), self.assertRaises(error):
                operation()
        self.assertFalse((self.root / "site").exists())

    def test_beta51_package_uses_exact_declared_beta50_capture(self):
        original = self.image.read_bytes()
        self.assertEqual(builder.build(self.root / "site"), 1)
        self.assertEqual((self.root / "site/assets/shots" / self.image.name).read_bytes(), original)
        self.assertEqual((self.root / "site/assets/shots" / self.record_path.name).read_bytes(),
                         self.record_path.read_bytes())
        generator.main()
        page = (self.gallery / "scenes/hero.md").read_text()
        self.assertIn("rust-2.0.0-beta.50-hero.png", page)
        self.assertIn(self.manifest["scenes"][0]["caption"], page)
        self.assertNotIn("beta.51", page)
        self.assertEqual(self.image.read_bytes(), original)

    def test_declared_version_must_have_its_own_capture_record(self):
        self.manifest["capture_version"] = "2.0.0-beta.51"
        self.write_metadata()
        self.refused_by_both(FileNotFoundError)

    def test_capture_record_version_must_match_declaration(self):
        self.record["version"] = "2.0.0-beta.49"
        self.write_metadata()
        self.refused_by_both()

    def test_uri_must_match_exact_declared_version_and_scene(self):
        for uri in ("../assets/shots/rust-2.0.0-beta.51-hero.png",
                    "https://example.test/hero.png", "../assets/shots/../hero.png"):
            self.manifest["scenes"][0]["image"] = uri
            self.write_metadata()
            self.refused_by_both()

    def test_wrong_hash_and_changed_image_bytes_are_refused(self):
        self.record["captures"][0]["sha256"] = "0" * 64
        self.write_metadata()
        self.refused_by_both()
        self.record["captures"][0]["sha256"] = hashlib.sha256(self.image.read_bytes()).hexdigest()
        self.write_metadata()
        self.image.write_bytes(self.image.read_bytes() + b"changed")
        self.refused_by_both()

    def test_capture_filename_and_selected_scene_membership_are_required(self):
        self.record["captures"][0]["image"] = "other.png"
        self.write_metadata()
        self.refused_by_both()
        self.record["captures"][0].update(scene="other", image="rust-2.0.0-beta.50-other.png")
        self.write_metadata()
        self.refused_by_both()

    def test_png_and_record_dimensions_are_checked(self):
        self.record["size"] = [3068, 1733]
        self.write_metadata()
        self.refused_by_both()
        self.record["size"] = [3068, 1734]
        self.image.write_bytes(png(3068, 1733))
        self.record["captures"][0]["sha256"] = hashlib.sha256(self.image.read_bytes()).hexdigest()
        self.write_metadata()
        self.refused_by_both()

    def test_invalid_png_with_matching_hash_is_refused(self):
        self.image.write_bytes(b"not a PNG")
        self.record["captures"][0]["sha256"] = hashlib.sha256(self.image.read_bytes()).hexdigest()
        self.write_metadata()
        self.refused_by_both()

    def test_symlink_record_and_image_are_refused_without_target_mutation(self):
        for path in (self.record_path, self.image):
            target = path.with_suffix(path.suffix + ".original")
            path.rename(target)
            original = target.read_bytes()
            path.symlink_to(target)
            self.refused_by_both()
            self.assertEqual(target.read_bytes(), original)
            path.unlink()
            target.rename(path)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO regular-file guard requires Unix")
    def test_nonregular_record_and_image_are_refused_without_blocking(self):
        for path in (self.record_path, self.image):
            original = path.read_bytes()
            path.unlink()
            os.mkfifo(path)
            self.refused_by_both()
            path.unlink()
            path.write_bytes(original)

    def test_record_and_image_reads_are_bounded(self):
        for path, limit in ((self.record_path, 1024 * 1024), (self.image, 32 * 1024 * 1024)):
            original = path.read_bytes()
            with path.open("r+b") as handle:
                handle.truncate(limit + 1)
            self.refused_by_both()
            path.write_bytes(original)

    def test_duplicate_record_scene_and_missing_capture_version_are_refused(self):
        self.record["captures"].append(dict(self.record["captures"][0]))
        self.write_metadata()
        self.refused_by_both()
        self.record["captures"].pop()
        del self.manifest["capture_version"]
        self.write_metadata()
        self.refused_by_both()

    def test_packaged_copy_is_verified_against_original_capture_hash(self):
        copyfile = shutil.copyfile
        def changed_copy(source, destination):
            result = copyfile(source, destination)
            if Path(source).suffix == ".png":
                destination.write_bytes(destination.read_bytes() + b"changed after validation")
            return result
        with patch.object(builder.shutil, "copyfile", changed_copy):
            with self.assertRaisesRegex(ValueError, "SHA256 differs"):
                builder.build(self.root / "site")
        self.assertEqual(self.image.read_bytes(), png())

    def test_manifest_replacement_publishes_only_exact_validated_bytes(self):
        original = self.manifest_path.read_bytes()
        copyfile = shutil.copyfile
        changed = False
        def changing_copy(source, destination):
            nonlocal changed
            # The first copy happens after validation and before publication.
            # Change the same URI as the independent source-recopy repro.
            if Path(source).name == "index.html":
                self.manifest["scenes"][0]["image"] = "../assets/shots/rust-2.0.0-beta.51-hero.png"
                self.manifest_path.write_text(json.dumps(self.manifest))
                changed = True
            return copyfile(source, destination)
        with patch.object(builder.shutil, "copyfile", changing_copy):
            self.assertEqual(builder.build(self.root / "site"), 1)
        self.assertTrue(changed)
        self.assertNotEqual(self.manifest_path.read_bytes(), original)
        self.assertEqual((self.root / "site/gallery/scenes.json").read_bytes(), original)
        self.assertEqual((self.root / "site/assets/shots" / self.record_path.name).read_bytes(),
                         self.record_path.read_bytes())
        self.assertTrue((self.root / "site/assets/shots" / self.image.name).is_file())


if __name__ == "__main__":
    unittest.main()
