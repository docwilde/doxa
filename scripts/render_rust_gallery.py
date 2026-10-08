#!/usr/bin/env python3
"""Render deterministic production App gallery scenes to reviewable PNGs.

The Rust example drives the real App through Ratatui's TestBackend. This script
only paints its styled cell buffer with a fixed local font; it never starts a
provider, daemon, network connection, or user store.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import tomllib

from PIL import Image, ImageDraw, ImageFont, PngImagePlugin

ROOT = Path(__file__).resolve().parent.parent
SIZE = (3068, 1734)
GRID = (126, 31)
CELL = (24, 54)
ORIGIN = ((SIZE[0] - GRID[0] * CELL[0]) // 2, (SIZE[1] - GRID[1] * CELL[1]) // 2)
SCENES = (
    "hero", "image-preview", "isolation", "fleet-review", "fleet-dependency", "fleet-release-review", "fleet-view",
    "beliefs", "tool-entries", "memory-management", "commands", "help",
)
FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")
FONT_BOLD = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf")


def capture(binary: Path, scene: str, destination: Path, version: str) -> None:
    raw = subprocess.check_output([str(binary), scene], cwd=ROOT, timeout=30)
    frame = json.loads(raw)
    if (frame["width"], frame["height"]) != GRID or len(frame["cells"]) != GRID[0] * GRID[1]:
        raise ValueError(f"{scene}: unexpected terminal grid")
    rows = ["".join(frame["cells"][y * GRID[0] + x][0] for x in range(GRID[0]))
            for y in range(GRID[1])]
    if scene == "hero" and version not in "\n".join(rows):
        raise ValueError(f"{scene}: binary version {version} is not visible")

    canvas = Image.new("RGB", SIZE, (23, 20, 28))
    draw = ImageDraw.Draw(canvas)
    regular = ImageFont.truetype(FONT, 38)
    bold = ImageFont.truetype(FONT_BOLD, 38)
    ox, oy = ORIGIN
    cw, ch = CELL
    for index, (symbol, foreground, background, modifier) in enumerate(frame["cells"]):
        x, y = index % GRID[0], index // GRID[0]
        left, top = ox + x * cw, oy + y * ch
        draw.rectangle((left, top, left + cw - 1, top + ch - 1), fill=tuple(background))
        if not symbol or symbol == " ":
            continue
        font = bold if modifier & 1 else regular
        bbox = font.getbbox(symbol)
        if bbox is None:
            continue
        glyph_height = bbox[3] - bbox[1]
        draw.text((left - bbox[0], top + (ch - glyph_height) // 2 - bbox[1]),
                  symbol, font=font, fill=tuple(foreground))

    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("DOXA version", version)
    metadata.add_text("Scene", scene)
    metadata.add_text("Capture", "production App, Ratatui TestBackend, deterministic fixture")
    destination.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(destination, pnginfo=metadata, optimize=True)
    if Image.open(destination).size != SIZE:
        raise ValueError(f"{scene}: wrong image dimensions")
    print(f"{scene}: {destination} ({SIZE[0]} x {SIZE[1]})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / "target/debug/examples/gallery")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "assets/shots")
    parser.add_argument("scenes", nargs="*", choices=SCENES, default=SCENES)
    args = parser.parse_args()
    binary = args.binary.resolve()
    if not binary.is_file():
        parser.error(f"gallery executable missing: {binary}")
    version = tomllib.loads((ROOT / "rust/doxa-tui/Cargo.toml").read_text())["package"]["version"]
    for scene in args.scenes:
        capture(binary, scene, args.output_dir / f"rust-{version}-{scene}.png", version)


if __name__ == "__main__":
    main()
