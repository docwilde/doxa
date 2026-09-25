"""Capture the production Rust TUI through Ratatui's TestBackend.

The example uses only fixture sessions and daemon frames. Pillow rasterizes
its styled terminal cells; no account, daemon, provider, or terminal recording
is involved. Run: python3 scripts/rust_gallery.py [scene ...]
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
SCENES = ("hero", "split-panes", "tool-activity", "needs-input", "permissions", "history")
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
CELL_W, CELL_H = 14, 26


def capture(name: str) -> None:
    raw = subprocess.check_output(
        ["cargo", "run", "--quiet", "--manifest-path", "rust/doxa-tui/Cargo.toml",
         "--example", "gallery", "--", name], cwd=ROOT
    )
    frame = json.loads(raw)
    width, height = frame["width"], frame["height"]
    image = Image.new("RGB", (width * CELL_W, height * CELL_H))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(FONT, 19)
    for i, (symbol, fg, bg, _modifier) in enumerate(frame["cells"]):
        x, y = i % width, i // width
        left, top = x * CELL_W, y * CELL_H
        draw.rectangle((left, top, left + CELL_W - 1, top + CELL_H - 1), fill=tuple(bg))
        if symbol.strip():
            draw.text((left, top + 1), symbol, font=font, fill=tuple(fg), stroke_width=0)
    dest = ROOT / "assets" / "shots" / f"rust-{name}.png"
    image.save(dest, optimize=True)
    print(f"{name}: {width}x{height} cells -> {dest.relative_to(ROOT)}")


if __name__ == "__main__":
    names = sys.argv[1:] or SCENES
    for name in names:
        if name not in SCENES:
            raise SystemExit(f"unknown scene: {name}")
        capture(name)
