"""Validate declared gallery captures independently of the current package version."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct

SIZE = (3068, 1734)
IDENTIFIER = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?")


def regular_bytes(path: Path, limit: int) -> bytes:
    """Read bounded regular files without following links or accepting replacement."""
    if path.is_symlink():
        raise ValueError(f"{path.name}: capture file must not be linked")
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError(f"{path.name}: capture file must be regular and bounded")
        data = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
    current = path.stat(follow_symlinks=False)
    fingerprint = lambda value: (value.st_dev, value.st_ino, value.st_size,
                                 value.st_mode, value.st_mtime_ns, value.st_ctime_ns)
    if len(data) != before.st_size or fingerprint(before) != fingerprint(after) \
            or fingerprint(before) != fingerprint(current):
        raise ValueError(f"{path.name}: capture file changed while reading")
    return data


def validate_screenshot(path: Path, checksum: str) -> None:
    data = regular_bytes(path, 32 * 1024 * 1024)
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError(f"{path.name}: invalid PNG header")
    if struct.unpack(">II", data[16:24]) != SIZE:
        raise ValueError(f"{path.name}: screenshot dimensions must be 3068 × 1734")
    if hashlib.sha256(data).hexdigest() != checksum:
        raise ValueError(f"{path.name}: screenshot SHA256 differs from capture record")


def load_gallery(root: Path) -> tuple[dict, list[tuple[Path, str]], bytes]:
    for relative in ("docs", "docs/gallery", "assets", "assets/shots"):
        directory = root / relative
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError(f"{relative}: gallery directory must not be linked")
    manifest = json.loads(regular_bytes(root / "docs/gallery/scenes.json", 1024 * 1024))
    if not isinstance(manifest, dict):
        raise ValueError("gallery manifest must be an object")
    version = manifest.get("capture_version")
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise ValueError("gallery needs an explicit valid capture_version")
    record_path = root / "assets/shots" / f"rust-{version}-capture.json"
    record_bytes = regular_bytes(record_path, 1024 * 1024)
    record = json.loads(record_bytes)
    if not isinstance(record, dict) or record.get("version") != version:
        raise ValueError("capture record version differs from declared capture_version")
    if record.get("size") != list(SIZE):
        raise ValueError("capture record dimensions must be 3068 × 1734")
    captures = record.get("captures")
    if not isinstance(captures, list) or not 1 <= len(captures) <= 64:
        raise ValueError("capture record must contain 1–64 captures")
    recorded = {}
    for capture in captures:
        if not isinstance(capture, dict):
            raise ValueError("invalid capture entry")
        identifier = capture.get("scene")
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier) \
                or identifier in recorded:
            raise ValueError("invalid or duplicate capture identifier")
        expected = f"rust-{version}-{identifier}.png"
        checksum = capture.get("sha256")
        if capture.get("image") != expected or not isinstance(checksum, str) \
                or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise ValueError(f"{identifier}: invalid capture filename or SHA256")
        recorded[identifier] = capture
    scenes = manifest.get("scenes")
    if not isinstance(scenes, list) or not 1 <= len(scenes) <= 64:
        raise ValueError("gallery must contain 1–64 scenes")
    identifiers = set()
    images = []
    for scene in scenes:
        if not isinstance(scene, dict):
            raise ValueError("invalid gallery scene")
        identifier = scene.get("id")
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier) \
                or identifier in identifiers:
            raise ValueError("invalid or duplicate gallery scene identifier")
        identifiers.add(identifier)
        for key in ("title", "caption", "alt"):
            if not isinstance(scene.get(key), str) or not scene[key].strip():
                raise ValueError("gallery text fields must be nonempty")
        expected = f"rust-{version}-{identifier}.png"
        if scene.get("image") != f"../assets/shots/{expected}":
            raise ValueError(f"{identifier}: screenshot URI differs from declared capture")
        capture = recorded.get(identifier)
        if capture is None or capture["image"] != expected:
            raise ValueError(f"{identifier}: selected scene is absent from capture record")
        image = root / "assets/shots" / expected
        validate_screenshot(image, capture["sha256"])
        images.append((image, capture["sha256"]))
    if manifest.get("default") not in identifiers:
        raise ValueError("gallery default must name a scene")
    return manifest, images, record_bytes
