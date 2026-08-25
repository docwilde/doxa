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

v0.41.0 added the reporting half (:func:`diagnostics`,
:func:`renderable_modes`, :func:`cell_size`), which is what ``/img`` with
no argument renders. It measures nothing new: the ladder probe is spent
before ``App.run()`` and cannot honestly be repeated, so every row is
either a settled value or an explicit "not measured", and the tiers /img
DRAWS are only the ones this terminal answered for.
"""

from __future__ import annotations

from typing import Any

from . import config as config_mod

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
    forced = config_mod.raw(ENV_VAR).strip().lower()
    if forced in MODES:
        return forced
    global _detected
    if _detected is None:
        _detected = _probe()
    return _detected


# -- what we MEASURED, for /img's showcase --------------------------------
#
# Everything below reports; nothing below probes a second time. The ladder
# probe above is the only measurement, it is spent before App.run(), and
# re-running any part of it afterwards would read a reply Textual's stdin
# thread already ate. So the showcase distinguishes three states by
# construction -- measured, inferred from a measurement, and never asked --
# and never collapses the third into either of the first two.

# Cell geometry, settled at most once per process, same as _detected.
_cell_size: "tuple[int, int] | None" = None
_cell_size_settled = False

#: textual-image's own last-resort constant when neither ``TIOCGWINSZ`` nor
#: the ``ESC[16t`` query answers ("assuming VT340 sizes", _terminal.py). It
#: returns this indistinguishably from a real measurement, so the showcase
#: labels it rather than reprinting it as one. A terminal whose cells
#: genuinely are 10x20 gets under-claimed by one line of text, which is the
#: side of that trade doxa.keyboard already argued for.
VT340_DEFAULT_CELL = (10, 20)


def cell_size() -> "tuple[int, int] | None":
    """This terminal's cell size in pixels, or None when nobody could say.

    textual-image needs this to turn a cell box into a pixel box, and asks
    for it with ``TIOCGWINSZ`` first and an ``ESC[16t`` query second -- the
    second of which writes escapes and reads stdin, so it is bound by the
    same before-``App.run()`` discipline as the ladder probe and is settled
    from ``DoxaApp.__init__`` beside it. Not a tty: None, without writing a
    byte. ``textual_image._terminal`` is private, hence the guard -- an
    upstream rename degrades this to "not measured", which is a row the
    showcase already knows how to print. That guard is not theoretical:
    on a pty reporting zero columns, ``get_cell_size`` divides by that
    zero and raises ``ZeroDivisionError`` straight out of its own except
    clause."""
    global _cell_size, _cell_size_settled
    if _cell_size_settled:
        return _cell_size
    _cell_size_settled = True
    try:
        if not _is_tty():
            return None
        from textual_image._terminal import get_cell_size

        measured = get_cell_size()
        _cell_size = (int(measured.width), int(measured.height))
    except Exception:  # noqa: BLE001 -- an unmeasurable terminal is not an error
        _cell_size = None
    return _cell_size


def probe_answered() -> bool:
    """Did a terminal actually ANSWER the ladder probe this process?

    Not "did detect_mode() get called": ``_probe`` returns "text" both when
    ``_is_tty()`` short-circuited it and when it blew up, and in neither
    case did anything out there say a word to us. So a settled "text" is
    read as silence, which is what it is -- and every rung below is
    reported as unmeasured rather than as a terminal that said no."""
    return _detected is not None and _detected != "text"


def library_version() -> str:
    """textual-image's own version, or "" when it cannot be imported at all
    (in which case every tier collapses to the text line, and the showcase
    should say so rather than list four modes it cannot draw)."""
    try:
        from importlib.metadata import version

        return str(version("textual-image"))
    except Exception:  # noqa: BLE001
        return ""


def renderable_modes() -> "tuple[str, ...]":
    """The tiers /img may actually DRAW right now, ladder order.

    ``halfblock`` and ``text`` are unconditional: they are ordinary cell
    output and need nothing from the terminal. ``kgp``/``sixel`` appear
    only when THIS terminal is the one that answered for them -- rendering
    a tier we merely hope is supported sprays escape bytes across the
    transcript, and a showcase that does that has claimed support it never
    measured."""
    detected = detect_mode()
    top = (detected,) if detected in ("kgp", "sixel") else ()
    return (*top, "halfblock", "text")


def _support_rows() -> "list[tuple[str, str]]":
    """The four ladder rungs, each labelled with HOW we know."""
    forced = config_mod.raw(ENV_VAR).strip().lower() in MODES
    detected = detect_mode()
    unconditional = "always available — plain cell output, needs no terminal support"
    if forced:
        unasked = f"not measured — DOXA_IMAGE_MODE forces {detected}; unset it to probe"
        kgp = sixel = unasked
    elif not probe_answered():
        unasked = "not measured — no interactive terminal to ask (headless, or a pipe)"
        kgp = sixel = unasked
    elif detected == "kgp":
        kgp = "supported — this terminal answered the graphics query"
        sixel = "not probed — the ladder stopped at kgp, so sixel was never asked"
    elif detected == "sixel":
        kgp = "not supported — this terminal declined the graphics query"
        sixel = "supported — this terminal answered the sixel query"
    else:
        kgp = "not supported — this terminal declined the graphics query"
        sixel = "not supported — this terminal declined the sixel query"
    return [
        ("kgp (kitty graphics)", kgp),
        ("sixel", sixel),
        ("halfblock", unconditional),
        ("text", unconditional + ' — the "[image: …]" line'),
    ]


def diagnostics() -> "list[tuple[str, str]]":
    """``/img``'s report: label/value rows, in reading order.

    Pure report. Every row is either a value already settled by a probe
    that ran once before ``App.run()``, or an explicit statement that
    nothing was measured. There is no row here whose value this function
    goes and finds out."""
    detected = detect_mode()
    forced = config_mod.raw(ENV_VAR).strip().lower() in MODES
    rows = [(
        "mode",
        f"{detected} — forced via DOXA_IMAGE_MODE" if forced
        else f"{detected} — probed",
    )]
    rows.append((
        "probe",
        "answered once at startup, before the TUI took stdin"
        if probe_answered()
        else "no answer — stdout is not an interactive terminal we could ask",
    ))
    version = library_version()
    rows.append((
        "textual-image",
        version if version else "not importable — every tier falls to the text line",
    ))
    measured_cells = cell_size()
    if measured_cells is None:
        cells = "not measured — no interactive terminal to ask"
    elif measured_cells == VT340_DEFAULT_CELL:
        cells = (
            f"{measured_cells[0]} × {measured_cells[1]} px — textual-image's "
            "VT340 default; this terminal reported no size of its own"
        )
    else:
        cells = f"{measured_cells[0]} × {measured_cells[1]} px"
    rows.append(("cell size", cells))
    rows.extend(_support_rows())
    return rows


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
