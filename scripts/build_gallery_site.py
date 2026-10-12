#!/usr/bin/env python3
"""Package the single-frame gallery and its selected screenshots for Pages."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import struct
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def build(output: Path) -> int:
    version = tomllib.loads((ROOT / "rust/doxa-tui/Cargo.toml").read_text())["package"]["version"]
    source = ROOT / "docs/gallery"
    manifest = json.loads((source / "scenes.json").read_text())
    scenes = manifest.get("scenes")
    if not isinstance(scenes, list) or not 1 <= len(scenes) <= 64:
        raise ValueError("gallery must contain 1–64 scenes")
    identifiers = set()
    images = []
    for scene in scenes:
        identifier = scene.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", identifier):
            raise ValueError("invalid gallery scene identifier")
        if identifier in identifiers:
            raise ValueError("duplicate gallery scene identifier")
        identifiers.add(identifier)
        for key in ("title", "caption", "alt"):
            if not isinstance(scene.get(key), str) or not scene[key].strip():
                raise ValueError("gallery text fields must be nonempty")
        expected = f"../assets/shots/rust-{version}-{identifier}.png"
        if scene.get("image") != expected:
            raise ValueError(f"{identifier}: screenshot URL does not match this release")
        image = ROOT / "assets/shots" / expected.rsplit("/", 1)[1]
        if image.is_symlink() or not image.is_file():
            raise ValueError(f"{identifier}: screenshot is missing or linked")
        with image.open("rb") as handle:
            header = handle.read(24)
        if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
            raise ValueError(f"{identifier}: invalid PNG header")
        if struct.unpack(">II", header[16:24]) != (3068, 1734):
            raise ValueError(f"{identifier}: screenshot dimensions must be 3068 × 1734")
        images.append(image)
    if manifest.get("default") not in identifiers:
        raise ValueError("gallery default must name a scene")
    if output.exists() and (output.is_symlink() or not output.is_dir() or any(output.iterdir())):
        raise ValueError("gallery output must be an empty directory")
    (output / "gallery").mkdir(parents=True, exist_ok=True)
    (output / "assets/shots").mkdir(parents=True)
    for name in ("index.html", "scenes.json"):
        shutil.copyfile(source / name, output / "gallery" / name)
    for image in images:
        shutil.copyfile(image, output / "assets/shots" / image.name)
    (output / ".nojekyll").write_text("")
    (output / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" '
        'content="0;url=gallery/"><title>DOXA gallery</title>'
        '<a href="gallery/">Open the DOXA gallery</a>\n'
    )
    return len(images)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(f"Packaged {build(args.output)} gallery scenes to {args.output}")
