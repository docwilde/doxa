"""doxa.images -- terminal image rendering with a strict fallback ladder.

Wraps ``textual-image``'s widgets behind one detection ladder:

    KGP (Kitty graphics protocol / TGP)  ->  sixel  ->  half-block cells
    ->  text fallback ("[image: <desc>]")

Images are polish, never load-bearing: every render site calls
:func:`widget_for`, which returns EITHER an image widget OR a plain
``Static`` carrying :func:`fallback_line` -- so a site cannot forget the
text fallback, it is the function's own failure mode. Any construction
error, a missing file, an unsupported terminal, a broken ``textual-image``
install: all of them degrade to the text line, never to an exception.

Detection discipline (the part that can bite): probing the terminal for
TGP/sixel support writes escape sequences and reads the reply from stdin --
textual-image documents that this "will not work anymore once Textual is
started" (Textual's stdin reader thread eats the response). So the probe
runs AT MOST ONCE, cached module-wide, and doxa.app imports this module at
import time -- i.e. before ``App.run()`` -- so a real TTY is probed while we
still own it. Under pytest / headless use stdout is a pipe, ``_is_tty()``
is False, and the probe short-circuits to "text" without writing a byte.

``DOXA_IMAGE_MODE`` (kgp | sixel | halfblock | text) overrides detection
entirely -- checked per call, so tests force each tier without touching the
cache, and a user whose terminal lies about its support has a way out.
"""

from __future__ import annotations

import os
from typing import Any

MODES = ("kgp", "sixel", "halfblock", "text")
ENV_VAR = "DOXA_IMAGE_MODE"

# Ladder result, settled at most once per process (see module docstring).
_detected: str | None = None


def _is_tty() -> bool:
    """Probe seam: True only when the REAL stdout is an interactive
    terminal (sys.__stdout__, not a pytest/pipe wrapper)."""
    import sys

    out = sys.__stdout__
    try:
        return bool(out is not None and out.isatty())
    except Exception:
        return False


def _kgp_support() -> bool:
    """Probe seam: Kitty graphics protocol (textual-image calls it TGP)."""
    from textual_image.renderable import tgp

    return bool(tgp.query_terminal_support())


def _sixel_support() -> bool:
    """Probe seam: sixel graphics."""
    from textual_image.renderable import sixel

    return bool(sixel.query_terminal_support())


def _probe() -> str:
    """One walk down the ladder. Never raises -- an import/probe failure is
    a "text" terminal, not an error."""
    try:
        if not _is_tty():
            return "text"
        if _kgp_support():
            return "kgp"
        if _sixel_support():
            return "sixel"
        return "halfblock"
    except Exception:
        return "text"


def detect_mode() -> str:
    """The effective render mode: the DOXA_IMAGE_MODE override when set to a
    known mode, else the (once-)probed ladder result."""
    forced = (os.environ.get(ENV_VAR) or "").strip().lower()
    if forced in MODES:
        return forced
    global _detected
    if _detected is None:
        _detected = _probe()
    return _detected


def fallback_line(desc: str) -> str:
    """The text fallback every render site ends up with when pixels are off
    the table."""
    return f"[image: {desc}]"


def widget_for(source: Any, desc: str, mode: str | None = None):
    """An image widget for `source` (path / bytes-stream / PIL image), or a
    ``Static`` with the text fallback -- ALWAYS a mountable widget, never
    None, never an exception. `desc` is the human line used by the fallback
    (typically the path or the tool name that produced the image)."""
    from textual.widgets import Static

    mode = mode if mode in MODES else detect_mode()
    if mode == "text":
        return Static(fallback_line(desc), classes="image-fallback")
    try:
        from textual_image.widget import HalfcellImage, SixelImage, TGPImage

        cls = {"kgp": TGPImage, "sixel": SixelImage, "halfblock": HalfcellImage}[mode]
        return cls(source, classes="image-widget")
    except Exception:
        return Static(fallback_line(desc), classes="image-fallback")


# Extensions the engine treats as "this tool result is an image path".
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


def looks_like_image_path(text: str) -> bool:
    """True when a tool result's text is nothing but a path to an existing
    image file -- the cheap end of the EngineEvent image convention."""
    from pathlib import Path

    candidate = text.strip()
    if not candidate or "\n" in candidate or len(candidate) > 1024:
        return False
    if not candidate.lower().endswith(IMAGE_SUFFIXES):
        return False
    try:
        return Path(candidate).is_file()
    except OSError:
        return False
