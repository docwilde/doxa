#!/usr/bin/env python3
"""Package the single-frame gallery and its selected screenshots for Pages."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil

from gallery_capture import load_gallery, validate_screenshot

ROOT = Path(__file__).resolve().parents[1]


def build(output: Path) -> int:
    source = ROOT / "docs/gallery"
    manifest, images, record_bytes = load_gallery(ROOT)
    if output.exists() and (output.is_symlink() or not output.is_dir() or any(output.iterdir())):
        raise ValueError("gallery output must be an empty directory")
    (output / "gallery").mkdir(parents=True, exist_ok=True)
    (output / "assets/shots").mkdir(parents=True)
    for name in ("index.html", "scenes.json"):
        shutil.copyfile(source / name, output / "gallery" / name)
    for image, checksum in images:
        packaged = output / "assets/shots" / image.name
        shutil.copyfile(image, packaged)
        validate_screenshot(packaged, checksum)
    (output / "assets/shots" / f"rust-{manifest['capture_version']}-capture.json").write_bytes(record_bytes)
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
