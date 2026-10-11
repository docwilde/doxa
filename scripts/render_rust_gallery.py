#!/usr/bin/env python3
"""Render deterministic production App gallery scenes to reviewable PNGs.

The Rust example drives the real App through Ratatui's TestBackend. This script
only paints its styled cell buffer with a fixed local font; it never starts a
provider, daemon, network connection, or user store.
"""

from __future__ import annotations

import argparse
from functools import lru_cache
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib

from PIL import Image, ImageDraw, ImageFont, PngImagePlugin

ROOT = Path(__file__).resolve().parent.parent
SIZE = (3068, 1734)
GRID = (126, 31)
CELL = (24, 54)
ORIGIN = ((SIZE[0] - GRID[0] * CELL[0]) // 2, (SIZE[1] - GRID[1] * CELL[1]) // 2)
SCENES = (
    "hero", "triage", "pane-triage", "project-label", "image-preview", "isolation", "fleet-review", "fleet-dependency", "fleet-release-review", "fleet-view",
    "beliefs", "tool-entries", "memory-management", "commands", "help", "codegraph",
    "settings", "context", "queued-prompts", "peer-messages", "routing-status", "settings-router", "settings-mcp", "glm-account", "setup-docker",
)
FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")
FONT_BOLD = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf")
FONT_EMOJI = Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf")


@lru_cache(maxsize=1)
def robot_glyph() -> Image.Image:
    """Rasterize the App's actual Unicode robot using the installed emoji font."""
    font = ImageFont.truetype(FONT_EMOJI, 109)  # Noto's fixed bitmap strike
    left, top, right, bottom = font.getbbox("🤖")
    glyph = Image.new("RGBA", (right - left, bottom - top))
    ImageDraw.Draw(glyph).text((-left, -top), "🤖", font=font, embedded_color=True)
    scale = min(CELL[0] * 2 / glyph.width, (CELL[1] - 4) / glyph.height)
    return glyph.resize((round(glyph.width * scale), round(glyph.height * scale)), Image.Resampling.LANCZOS)


def capture(binary: Path, scene: str, destination: Path, version: str, env: dict[str, str]) -> None:
    raw = subprocess.check_output([str(binary), scene], cwd=ROOT, env=env, timeout=30)
    frame = json.loads(raw)
    if frame.get("version") != version:
        raise ValueError(f"{scene}: binary version differs from manifest {version}")
    if (frame["width"], frame["height"]) != GRID or len(frame["cells"]) != GRID[0] * GRID[1]:
        raise ValueError(f"{scene}: unexpected terminal grid")
    rows = ["".join(frame["cells"][y * GRID[0] + x][0] for x in range(GRID[0]))
            for y in range(GRID[1])]
    if scene == "hero" and version not in "\n".join(rows):
        raise ValueError(f"{scene}: binary version {version} is not visible")
    required = {
        "settings": ("Appearance", "Ctrl+S save"),
        "settings-router": ("Jev Router (on for new sessions)", "Jev Router config", "/private/reviewed-router.json", "Not checked", "no inference"),
        "settings-mcp": ("API worker MCP", "Tavily API key", "Firecrawl API key", "Exa API key", "Brave Search API key", "no tools"),
        "glm-account": ("GLM account reporting", "owner-declared", "Cash balance: unknown", "bundle remainder: unknown"),
        "setup-docker": ("Check Docker prerequisites", "Edit Docker worker image digest", "Edit rootless Docker socket", "Show rootless Engine and image build steps"),
        "context": ("21.0% in use", "42000 / 200000 tokens", "Conversation", "Available"),
        "routing-status": ("Route auto", "ds-fast", "$0.000004 est", "session cost includes worker calls"),
        "queued-prompts": ("Queued · q-next", "parser API stable"),
        "peer-messages": ("Peer Parser reviewer", "q-peer", "reconnect review"),
    }
    for label in required.get(scene, ()):
        if label not in "\n".join(rows):
            raise ValueError(f"{scene}: expected fixture content not visible: {label}")

    canvas = Image.new("RGB", SIZE, (23, 20, 28))
    draw = ImageDraw.Draw(canvas)
    regular = ImageFont.truetype(FONT, 38)
    bold = ImageFont.truetype(FONT_BOLD, 38)
    ox, oy = ORIGIN
    cw, ch = CELL
    # Fill backgrounds before glyphs, so a wide Unicode glyph is not erased by
    # the following cell's background (Ratatui retains that cell for width).
    for index, (_, _, background, _) in enumerate(frame["cells"]):
        x, y = index % GRID[0], index // GRID[0]
        left, top = ox + x * cw, oy + y * ch
        draw.rectangle((left, top, left + cw - 1, top + ch - 1), fill=tuple(background))
    for index, (symbol, foreground, _, modifier) in enumerate(frame["cells"]):
        x, y = index % GRID[0], index // GRID[0]
        left, top = ox + x * cw, oy + y * ch
        if not symbol or symbol == " ":
            continue
        if symbol == "🤖":
            glyph = robot_glyph()
            canvas.paste(glyph, (left, top + (ch - glyph.height) // 2), glyph)
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
    metadata.add_text("Fonts", "DejaVu Sans Mono; Noto Color Emoji for the robot glyph")
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
    scratch = os.environ.get("TMPDIR")
    if not scratch or not Path(scratch).is_dir():
        parser.error("set TMPDIR to an existing real-disk scratch directory")
    with tempfile.TemporaryDirectory(prefix="doxa-gallery-", dir=scratch) as private:
        home = Path(private) / "home"
        doxa_home = Path(private) / "doxa"
        lore_root = Path(private) / "lore"
        for directory in (home, doxa_home, lore_root):
            directory.mkdir(mode=0o700)
        # Owner settings, key bindings, provider credentials and clock state
        # must not influence deterministic example frames or child processes.
        env = {key: os.environ[key] for key in ("PATH", "TMPDIR") if key in os.environ}
        env.update({"HOME": str(home), "DOXA_HOME": str(doxa_home), "LORE_ROOT": str(lore_root),
                    "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC", "DOXA_CLOCK_SHOW": "0"})
        for scene in args.scenes:
            capture(binary, scene, args.output_dir / f"rust-{version}-{scene}.png", version, env)


if __name__ == "__main__":
    main()
