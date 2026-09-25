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
SCENES = ("hero", "tool-activity", "tool-expanded", "processing", "reasoning", "needs-input", "permissions", "effort", "history", "queue", "memory")
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
CELL_W, CELL_H = 14, 26
BOX_STROKES = {
    "─": "lr", "│": "ud", "┌": "rd", "┐": "ld", "└": "ur", "┘": "ul",
    "├": "urd", "┤": "uld", "┬": "lrd", "┴": "lru", "┼": "lrud",
}


def draw_box_stroke(draw: ImageDraw.ImageDraw, symbol: str, left: int, top: int,
                    color: tuple[int, int, int]) -> None:
    """Join box edges at cell boundaries instead of using a narrow font glyph."""
    edges = BOX_STROKES[symbol]
    center_x, center_y = left + CELL_W // 2, top + CELL_H // 2
    if "l" in edges:
        draw.line((left, center_y, center_x, center_y), fill=color, width=2)
    if "r" in edges:
        draw.line((center_x, center_y, left + CELL_W, center_y), fill=color, width=2)
    if "u" in edges:
        draw.line((center_x, top, center_x, center_y), fill=color, width=2)
    if "d" in edges:
        draw.line((center_x, center_y, center_x, top + CELL_H), fill=color, width=2)


def draw_branch_symbol(draw: ImageDraw.ImageDraw, left: int, top: int,
                       color: tuple[int, int, int]) -> None:
    """Render U+2387 consistently when the screenshot font lacks its glyph."""
    left_x, right_x = left + 4, left + 10
    draw.line((left_x, top + 6, left_x, top + 19, right_x, top + 13, right_x, top + 7),
              fill=color, width=2)
    for x, y in ((left_x, top + 5), (right_x, top + 6), (left_x, top + 20)):
        draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill=color)


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
        if symbol in BOX_STROKES:
            draw_box_stroke(draw, symbol, left, top, tuple(fg))
        elif symbol == "⎇":
            draw_branch_symbol(draw, left, top, tuple(fg))
        elif symbol.strip():
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
