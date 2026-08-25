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

v0.49.0 added the containment half, from a crash report, and it is three
separate defences against the same fact -- **this library runs code inside
DOXA's render loop and on DOXA's terminal, and DOXA does not get to review
what it does there**:

* :func:`_mute_library_logging` -- it may not WRITE on the screen. Its
  warnings go to stderr, and stderr is the screen.
* :func:`_seed_library_cache` -- it may not ASK the terminal anything
  after ``App.run()``, because Textual owns stdin by then and the answer
  can never arrive.
* :func:`_guarded` -- it may not RAISE while measuring or painting, where
  there is no caller of ours left to catch it.
"""

from __future__ import annotations

from typing import Any

from . import config as config_mod

MODES = ("kgp", "sixel", "halfblock", "text")
ENV_VAR = "DOXA_IMAGE_MODE"

# Ladder result, settled at most once per process (see module docstring).
_detected: str | None = None


def _mute_library_logging() -> None:
    """Stop textual-image writing on DOXA's screen (v0.49.0, from a crash
    report). Run at import, once, before anything can log.

    **The reported failure, exactly.** ``get_cell_size`` probes with
    ``ESC[16t``, and a terminal that does not implement that window-op --
    VTE, which is GNOME Terminal and so Linux Mint's default -- never
    answers. Upstream CATCHES its own timeout, which is correct, and then
    reports it with ``logger.warning(..., exc_info=e)``. With no logging
    configured, Python's last-resort handler writes WARNING and above to
    **stderr**, so the message and a full traceback land on the terminal
    at the exact moment the TUI is taking it over. The user reported that
    as a crash, and from where they were sitting it is indistinguishable
    from one:

        doxa: restoring 1 tab(s) in /home/fabian/repos/doxa…
        Failed to get cell size via escape sequence, assuming VT340 sizes
        Traceback (most recent call last): ...
        TimeoutError: Timeout waiting for data

    Nothing there is an error -- it is a library narrating a handled
    fallback. But in a full-screen app **stderr is the screen**, so a
    library's idea of a warning is DOXA's idea of corrupted output.

    A ``NullHandler`` plus ``propagate=False`` is the documented way to
    say "this library's records are not mine to print". It is deliberately
    the whole logger and not one message: any record from this package
    would land in the same place with the same result. What the user
    actually needs from that probe is a *reported* value, and ``/img``
    already prints it -- labelled as defaulted when it was defaulted."""
    import logging

    logger = logging.getLogger("textual_image")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False


_mute_library_logging()


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
        from textual_image._terminal import get_cell_size
    except Exception:  # noqa: BLE001 -- no library, nothing to settle
        return None
    if _is_tty():
        try:
            measured = get_cell_size()
            _cell_size = (int(measured.width), int(measured.height))
        except Exception:  # noqa: BLE001 -- an unmeasurable terminal is not
            # an error. Measured, on a pty reporting zero columns:
            # get_cell_size divides by that zero and raises
            # ZeroDivisionError straight out of its own except clause.
            _cell_size = None
    _seed_library_cache(get_cell_size)
    return _cell_size


def _seed_library_cache(get_cell_size) -> None:
    """Make sure textual-image never asks the terminal again (v0.49.0).

    ``get_cell_size`` memoises onto its own ``_result`` attribute, but only
    on the path that RETURNS -- if it raised, or if we never called it,
    the attribute is absent and the next caller re-probes. Every other
    caller is a widget measuring or painting itself, which happens after
    ``App.run()``, where Textual's reader thread owns stdin and the
    terminal's reply to ``ESC[16t`` can never arrive. That probe is
    therefore guaranteed to burn its timeout and guaranteed to fail, and
    it does so from inside a render, where a raise is not a degraded
    picture but a dead app.

    So whatever happened above, the cache is left populated: with what we
    measured, or with the same VT340 constant upstream would have fallen
    back to anyway. Nothing is claimed by this that
    :func:`diagnostics` does not already label as defaulted."""
    try:
        if hasattr(get_cell_size, "_result"):
            return
        from textual_image._terminal import CellSize

        setattr(get_cell_size, "_result", CellSize(*(_cell_size or VT340_DEFAULT_CELL)))
    except Exception:  # noqa: BLE001 -- an upstream rename must not be fatal
        pass


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


from functools import lru_cache


class _GuardedRenderable:
    """The last few inches of the paint path.

    ``Widget.render`` only BUILDS a Rich renderable; the library's actual
    drawing -- and its ``get_cell_size`` call -- happens later, when Rich
    asks that object to produce segments. That is past the widget method,
    inside the compositor, with nothing of DOXA's on the stack. So the
    renderable is wrapped too, and a raise becomes the text fallback
    rather than a dead session. Segments already yielded before a failure
    are kept: a half-drawn image plus one honest line beats losing the
    frame."""

    def __init__(self, inner: Any, fallback: str) -> None:
        self._inner = inner
        self._fallback = fallback

    def __rich_console__(self, console, options):
        from rich.text import Text

        try:
            yield from console.render(self._inner, options)
        except Exception:  # noqa: BLE001 -- see the class docstring
            yield Text(self._fallback)

    def __rich_measure__(self, console, options):
        from rich.measure import Measurement

        try:
            return Measurement.get(console, options, self._inner)
        except Exception:  # noqa: BLE001
            width = len(self._fallback)
            return Measurement(width, width)


@lru_cache(maxsize=None)
def _guarded(cls):
    """`cls` with its measure and paint methods wrapped so a raise degrades
    the picture instead of the session (v0.49.0).

    :func:`widget_for` has always promised "never an exception", and until
    now that promise covered CONSTRUCTION only -- which is the easy half.
    A Textual widget is also asked for its width, its height and its
    content long after it was built, from inside the compositor, where
    there is no caller left to catch anything: an exception there takes
    the app down. That is not hypothetical for this library. Its
    ``get_cell_size`` divides by a terminal-reported column count and
    raises ``ZeroDivisionError`` when that count is zero, and every one of
    the three methods below calls it.

    The seeded cache in :func:`_seed_library_cache` closes that particular
    door before it can open. This closes the doorway. Both, because the
    library gets to decide what it does inside a render and DOXA does not.

    The height fallback is deliberately 1 and not 0: a widget measuring to
    zero rows is present in the DOM, invisible on screen, and passes every
    structural assertion -- the v0.28.0 defect, and the one failure mode
    images make cheap to reproduce."""
    def _desc(self) -> str:
        return fallback_line(getattr(self, "_doxa_desc", "image"))

    def render(self):
        try:
            return _GuardedRenderable(cls.render(self), _desc(self))
        except Exception:  # noqa: BLE001 -- see this function's docstring
            return _desc(self)

    def get_content_width(self, container, viewport) -> int:
        try:
            return cls.get_content_width(self, container, viewport)
        except Exception:  # noqa: BLE001
            return len(_desc(self))

    def get_content_height(self, container, viewport, width) -> int:
        try:
            return cls.get_content_height(self, container, viewport, width)
        except Exception:  # noqa: BLE001
            return 1

    return type(
        cls.__name__,
        (cls,),
        {
            "render": render,
            "get_content_width": get_content_width,
            "get_content_height": get_content_height,
        },
        # textual_image.widget.Image.__init_subclass__ REQUIRES this
        # keyword; passing the parent's own renderable keeps the subclass
        # painting exactly what the parent would have painted.
        Renderable=cls._Renderable,
    )


def widget_for(source: Any, desc: str, mode: str | None = None):
    """An image widget for `source` (path / bytes-stream / PIL image), or a
    ``Static`` with the text fallback -- ALWAYS a mountable widget, never
    None, never an exception, and since v0.49.0 never an exception while
    PAINTING either (see :func:`_guarded`). `desc` is the human line used
    by the fallback (typically the path or the tool name that produced the
    image)."""
    from textual.widgets import Static

    mode = mode if mode in MODES else detect_mode()
    if mode == "text":
        return Static(fallback_line(desc), classes="image-fallback")
    try:
        from textual_image.widget import HalfcellImage, SixelImage, TGPImage

        cls = {"kgp": TGPImage, "sixel": SixelImage, "halfblock": HalfcellImage}[mode]
        widget = _guarded(cls)(source, classes="image-widget")
        widget._doxa_desc = desc
        return widget
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
