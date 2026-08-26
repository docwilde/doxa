# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.banner -- the DOXA mark at the top of a session's opening block.

**One form, on every terminal.** :data:`MARK_ROWS` (a ring around a
triangle, authored cell by cell out of ``█`` and spaces and NOTHING ELSE)
with :data:`WORDMARK` and :data:`TAGLINE` beside it as plain text,
assembled by :func:`drawn_lines`. That is the whole banner now --
:func:`enabled` is the only decision left, on or off.

**v0.66.0 removed the raster ``logo.png`` form this module used to draw
on ``kgp``/``sixel`` terminals** (``boot_banner=auto``/``image``), and
with it the three-way choice between that raster, the drawn mark and a
plain wordmark that v0.58.0 built. The owner's call: the drawn form reads
better than a downscaled photograph even where a terminal COULD carry
real pixels, so there is no longer a case where the raster is the right
answer -- see v0.58.0's own CHANGELOG entry (and this module's git
history) for the arithmetic that first made that true for half-block
terminals; it generalises. ``boot_banner`` is a plain on/off knob now
(:func:`enabled`); a ``config.toml`` still holding ``auto``, ``blocks``
or ``image`` from before this collapse keeps meaning "on" -- every value
but a recognised OFF spelling does.

**``/img`` still shows the raster.** ``assets/logo.png`` through
:mod:`doxa.images`' own ladder is not gone, only the BANNER's use of it
-- :func:`asset_path`, :func:`_prepared` and :func:`image_source` back
``ImageShowcaseBlock`` (:mod:`doxa.ui.transcript`), which demonstrates
every tier a terminal answers for, boot banner setting or not. Nothing
here ever renders ``[image: doxa logo]`` on the banner path; that line
was for a raster the user ASKED for and did not get, and asking is no
longer a thing the banner does.
"""

from __future__ import annotations

import importlib.resources
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import config as config_mod

ENV_VAR = "DOXA_BOOT_BANNER"

# The row budget and the two aspect ratios it is spent against -- ONLY
# for /img's showcase now (ImageShowcaseBlock), which still renders
# assets/logo.png at real resolution on every tier a terminal answers
# for. The boot banner itself never reads these (v0.66.0). CELL_ASPECT
# is the height-to-width ratio of a terminal cell, ~2 everywhere, and the
# same constant scripts/screenshot.py derives from its own measured SVG
# export (24.375 / 12.2).
ROWS = 6
CELL_ASPECT = 2.0

#: Aspect of the logo's INKED AREA -- 928 x 238, measured off the alpha
#: channel's bounding box, not the 1100 x 320 canvas. The asset's margin
#: is page layout for a README; inside a six-row budget every row is dear,
#: so :func:`prepared_image` crops it and this is the shape that survives.
#: ``test_banner`` re-measures the real file against this number, so the
#: constant cannot quietly drift away from the asset it describes.
CONTENT_ASPECT = 928 / 238

#: /img's demonstration width in CELLS -- what each rendered tier's
#: widget ``width`` style is set to (ImageShowcaseBlock).
COLUMNS = round(ROWS * CELL_ASPECT * CONTENT_ASPECT)  # 47

#: The background the logo's transparency is flattened onto -- theme.tcss's
#: ``$doxa-base``, stated here as a literal for the same reason the theme
#: states it: it is the ramp's own floor. With the `background` setting on
#: "transparent" the screen behind is the terminal's own, so this leaves a
#: faint dark rectangle; that is a far smaller wrong than the alternative,
#: which is what PIL's RGBA->RGB conversion does unaided -- see
#: :func:`prepared_image`.
BASE_COLOR = (0x17, 0x15, 0x12)

#: The DOXA mark -- ring and triangle -- DRAWN, not downscaled.
#:
#: **One codepoint and one space.** ``█`` (U+2588 FULL BLOCK) and ``" "``,
#: nothing else. That constraint is the user's, arrived at by looking at
#: rendered candidates rather than by argument, and each rejection closed
#: off a whole family of solutions:
#:
#: * Half blocks (``▀`` U+2580, ``▄`` U+2584) -- *"do not use half-blocks
#:   / it leaves gaps"*. They are drawn against the font's own baseline
#:   and leading, so a column of them seams horizontally instead of
#:   reading as one stroke.
#: * Quadrant triangles (``◢``/``◣`` U+25E2/U+25E3), which an earlier
#:   revision used for a sloped edge -- they live in Geometric Shapes
#:   rather than Block Elements, so a font covering one need not cover the
#:   other, and the mark degrades to tofu rather than to something plainer.
#: * Dropping the mark for a wordmark-only banner, which was offered when
#:   even ``█`` was observed to render short, and was overruled: **"No,
#:   use the full block"**.
#:
#: **The construction**, so this is tunable rather than magic. A circle of
#: radius ``(rows - 1) / 2`` rasterised on a grid twice as wide as it is
#: tall -- terminal cells run about 1:2, so widening the grid is what makes
#: the ring round instead of an ellipse -- with a one-cell stroke, and a
#: triangle whose apex sits at ``cy - R*0.55``, whose base sits at
#: ``cy + R*0.62``, and whose half-width is ``t * R * 0.60``.
#:
#: **Nine rows, not seven, and that is v0.58.0's own escape hatch used.**
#: Reported against the 7-row mark: the ring and the triangle inside it
#: TOUCH -- the triangle's widest row sits directly against the ring's
#: inner face with only the ordinary word-spacing between them, and at
#: this size that reads as one blob rather than two shapes. Widening the
#: ring's inner radius while holding it to seven rows only shrank the
#: triangle to a sliver; there was nowhere left to put a moat. v0.58.0's
#: own docstring already named the way out -- *"A 9x17 version reads well
#: too, ... if seven rows ever feels tight beside the text"* -- and a
#: gap the mark did not have before is exactly that. Two more rows buys a
#: full blank cell of separation on every row where ring and triangle
#: share a line (rows 2-6 below), checked by rendering through the real
#: SVG exporter at true 2:1 cell metrics, not guessed at from the source.
#:
#: **The rows below are hand-tightened, same as v0.58.0's were, and do not
#: come out of the formula verbatim.** The moat itself is the hand
#: adjustment: the formula's own triangle radius left rows 2 and 6
#: touching the ring at the width this mark actually ships. Every row is
#: still exactly one of two shapes -- a single run of ink (the cap and
#: shoulder rows) or ring/gap/triangle/gap/ring (three runs) -- which is
#: what lets :func:`drawn_lines` colour the two apart without a second
#: hand-authored grid; see ``_mark_markup``.
#:
#: **The accepted caveat.** Some monospace fonts render Block Elements at
#: reduced height, and some terminals add leading, so stacked full blocks
#: can show faint horizontal banding. That is the terminal drawing the
#: glyph, not DOXA drawing the mark, and there is no cell-level fix for it
#: from this side. Shown to the user, accepted, and written down in the
#: CHANGELOG so a reader who hits it does not think the mark is broken.
MARK_ROWS: tuple[str, ...] = (
    "       █████████       ",
    "    ███████████████    ",
    "  ██████   █   ██████  ",
    " ████     ███     ████ ",
    "█████     ███     █████",
    " ████    █████    ████ ",
    "  ██████ █████ ██████  ",
    "    ███████████████    ",
    "       █████████       ",
)

#: Width of :data:`MARK_ROWS`, and the blank gutter between mark and text.
MARK_COLUMNS = max(len(row) for row in MARK_ROWS)
MARK_GAP = 3

#: The wordmark, as PLAIN TEXT. v0.41.0 drew it in block glyphs too; the
#: user's own resolution of that was "the wordmark as plain text", and
#: they are right -- stylised letters at two rows are something to squint
#: at, where four ordinary capitals are simply legible. The font already
#: knows how to draw an A.
WORDMARK = "DOXA"

#: The strapline set into the asset, as real text for the same reason.
TAGLINE = "doxa · belief earning knowledge"

#: Widths at which the drawn form can show mark + tagline, and mark +
#: wordmark. Below the second it is the bare wordmark: a mark with nothing
#: to name it is a shape, not a banner.
DRAWN_FULL_COLUMNS = MARK_COLUMNS + MARK_GAP + len(TAGLINE)
DRAWN_MARK_COLUMNS = MARK_COLUMNS + MARK_GAP + len(WORDMARK)

# Row indices the text sits on (0-based): rows 3 and 5 of the nine, with a
# blank row between them. That straddles the ring's vertical centre -- row
# 4 -- so the two lines sit either side of the middle rather than crowding
# the top, and the block of text has the same optical centre as the mark
# it stands beside.
_WORDMARK_ROW = 3
_TAGLINE_ROW = 5

MARK_COLOR = "#D97757"
MUTED_COLOR = "#8A8073"

#: The ring's colour -- distinct from :data:`MARK_COLOR`, which is now the
#: TRIANGLE's alone. A single flat colour was the other half of why ring
#: and triangle read as one blob: even with a moat between them, the same
#: colour on both sides of a one-cell gap reads as one shape with a
#: notch, not two shapes. Reuses :data:`MUTED_COLOR` rather than
#: introducing a third constant -- it is already the mark's own quiet
#: tone (the tagline wears it beside the wordmark), so the ring becomes
#: the mark's frame and the triangle stays the one accent-coloured thing
#: in it, same relationship the wordmark/tagline pair already has.
RING_COLOR = MUTED_COLOR


def _runs(row: str) -> "list[tuple[int, int]]":
    """``[(start, end)]`` for each maximal run of ``█`` in ``row`` (``end``
    exclusive) -- how :func:`_mark_markup` tells the ring from the
    triangle without a second hand-authored grid: see :data:`MARK_ROWS`."""
    spans: "list[tuple[int, int]]" = []
    start = None
    for i, cell in enumerate(row + " "):
        if cell == "█" and start is None:
            start = i
        elif cell != "█" and start is not None:
            spans.append((start, i))
            start = None
    return spans


def _mark_markup(index: int) -> str:
    """Row ``index`` of :data:`MARK_ROWS` as Textual markup, the ring in
    :data:`RING_COLOR` and the triangle in :data:`MARK_COLOR`.

    Classified by RUN COUNT, not a second grid to keep in sync by hand:
    every row of this mark is either pure ring -- one run of ink, the cap
    and shoulder rows -- or ring/gap/triangle/gap/ring -- three runs, the
    moat being what makes this unambiguous.
    ``test_the_mark_is_a_ring_around_a_triangle`` pins that every row is
    one of those two shapes, so an edit that breaks the pattern fails
    loudly there rather than mis-colouring silently here."""
    row = MARK_ROWS[index]
    spans = _runs(row)
    colors = (RING_COLOR,) if len(spans) == 1 else (RING_COLOR, MARK_COLOR, RING_COLOR)
    parts = []
    pos = 0
    for (start, end), color in zip(spans, colors):
        if start > pos:
            parts.append(row[pos:start])
        parts.append(f"[{color}]{row[start:end]}[/]")
        pos = end
    if pos < len(row):
        parts.append(row[pos:])
    return "".join(parts)


def drawn_lines(content_columns: int) -> "list[str]":
    """The drawn banner as Textual markup rows, fitted to the width it has.

    THIS IS THE ONLY PATH (v0.66.0 retired the raster alternative
    v0.58.0-0.65.0 drew on kgp/sixel terminals) -- every terminal, every
    tier, gets these rows.

    Three shapes, widest first, so what survives at any width is the part
    that still reads: mark + wordmark + tagline, mark + wordmark, then the
    bare wordmark. A drawn mark that has shrunk past legibility is dropped
    rather than crushed, on the same rule the rest of this module follows.
    Dropped is ALL OF IT, ring included -- the two-colour mark has nothing
    left to colour once it is gone, so there is no separate case to keep
    in sync with this one."""
    if content_columns >= DRAWN_FULL_COLUMNS:
        beside = {_WORDMARK_ROW: f"[b {MARK_COLOR}]{WORDMARK}[/]",
                  _TAGLINE_ROW: f"[{MUTED_COLOR}]{TAGLINE}[/]"}
    elif content_columns >= DRAWN_MARK_COLUMNS:
        beside = {_WORDMARK_ROW: f"[b {MARK_COLOR}]{WORDMARK}[/]"}
    else:
        return [f"[b {MARK_COLOR}]{WORDMARK}[/]"]
    gap = " " * MARK_GAP
    lines = []
    for index in range(len(MARK_ROWS)):
        text = beside.get(index)
        lines.append(_mark_markup(index) + (gap + text if text else ""))
    return lines


# Every spelling this knob has ever shipped that means OFF: the plain
# "off", and the bool spelling it launched with in v0.41.0 ("0"/"false"/
# "no", back when 1/0 read as on/off). v0.58.0 added "auto"/"blocks"/
# "image" as ON spellings on top of that; v0.66.0 retired the three-way
# choice those named, and rather than add a matching _LEGACY_ON tuple to
# keep recognising them, every string that is not a recognised OFF
# spelling now reads as on -- the same permissive rule form() used to
# fall back to for a value it did not recognise at all. A config.toml
# still holding "auto", "blocks" or "image" from before this collapse
# therefore keeps meaning exactly what it always meant here: draw it.
_LEGACY_OFF = ("0", "false", "no", "off")


def enabled() -> bool:
    """Is the opening banner drawn at all? Default yes.

    ``boot_banner`` is a plain on/off knob (a ``bool_on`` Setting,
    default on) -- see :data:`_LEGACY_OFF` for exactly what still reads
    as off, everything else reads as on, and there is no longer a
    choice of WHAT gets drawn: :func:`drawn_lines` is the only form
    (v0.66.0 dropped the raster ``logo.png`` alternative). ON by default
    is a judgment call and it rests on the drawn form rather than on a
    picture: there is no terminal and no width at which this costs more
    than a few rows of something legible."""
    raw = config_mod.raw(ENV_VAR).strip().lower()
    return raw not in _LEGACY_OFF


def asset_path() -> "Path | None":
    """The logo file, or None when neither copy is on disk.

    Only :func:`_prepared` (in turn only ``/img``'s
    :class:`~doxa.ui.transcript.ImageShowcaseBlock`, since v0.66.0
    retired the banner's own raster form) calls this now, but the
    lookup itself is unchanged: installed wheel --
    ``pyproject.toml``'s force-include put it at ``doxa/assets/logo.png``,
    the arrangement ``doxa.launcher`` already uses for ``icon.png`` -- one
    file in git, no duplicate under ``doxa/``. Source checkout: it is
    still only at the repo's own ``assets/``. Neither present is not an
    error; it is a showcase that says so (``ImageShowcaseBlock``'s own
    fallback line) -- and neither is a loader that cannot answer the
    question, which is why the whole lookup is guarded rather than just
    the packaged half."""
    try:
        packaged = importlib.resources.files("doxa") / "assets" / "logo.png"
        if packaged.is_file():
            return Path(str(packaged))
    except Exception:  # noqa: BLE001 -- a zipped/frozen/odd loader is a
        # showcase that falls through to the checkout copy, never a crash
        # on the boot path (see _prepared on why that matters here).
        pass
    try:
        checkout = Path(__file__).resolve().parent.parent / "assets" / "logo.png"
        return checkout if checkout.is_file() else None
    except OSError:
        return None


@lru_cache(maxsize=1)
def _prepared() -> Any:
    """The logo, cropped to its ink and flattened onto :data:`BASE_COLOR`.

    **Why this is not just the file path.** ``logo.png`` is RGBA with a
    fully transparent background, and textual-image normalizes to RGB with
    ``PIL.Image.convert("RGB")``, which DISCARDS alpha rather than
    compositing it -- so every transparent pixel renders as whatever RGB
    was hiding underneath, which in this asset is white. On DOXA's dark
    theme that is a glaring white slab around the mark, and it is exactly
    the defect a screenshot catches and a green test suite does not.
    Compositing here, before the widget ever sees it, is the fix.

    The crop is the second half of the same pass: cropping to the alpha
    bounding box hands the six-row budget 238 rows of ink instead of 320
    rows of canvas, which is a third more resolution for the wordmark at
    no cost in rows.

    Cached: one decode and one composite per process, and the result is
    copied per caller so no widget can mutate another's image.

    **Everything is inside the try, including the import** (v0.58.0).
    ``doxa.images.widget_for`` is documented never to raise and always to
    return a mountable widget, but this function is DOXA's own code on
    the near side of that guarantee: it runs during
    ``ImageShowcaseBlock.compose`` (``/img``'s -- see this module's own
    docstring for why that is the only caller left since v0.66.0), and an
    exception there does not degrade a decoration, it breaks a debug
    command. Pillow is a declared dependency now, so the import "cannot"
    fail -- which is exactly the class of assumption that produces a bug
    report, and it costs one indent to not make it."""
    try:
        from PIL import Image

        path = asset_path()
        if path is None:
            return None
        source = Image.open(path).convert("RGBA")
        box = source.getchannel("A").getbbox()
        if box is not None:
            source = source.crop(box)
        flat = Image.new("RGB", source.size, BASE_COLOR)
        flat.paste(source, (0, 0), source)
        return flat
    except Exception:  # noqa: BLE001 -- a showcase that cannot prepare the
        # logo says so (ImageShowcaseBlock's own fallback line) rather
        # than crash the command that asked for it.
        return None


def image_source() -> Any:
    """What to hand :func:`doxa.images.widget_for` -- a fresh copy of the
    prepared image, or None when there is nothing to prepare. ``/img``'s
    showcase is the only caller since v0.66.0 retired the banner's own
    raster form."""
    prepared = _prepared()
    return prepared.copy() if prepared is not None else None
