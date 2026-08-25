"""doxa.banner -- the DOXA mark at the top of a session's opening block.

**Read the ladder in this order, because v0.49.0 inverted it.** The DRAWN
mark is the normal path. The raster ``logo.png`` is the exception, for the
terminals that earn it.

1. **Drawn** -- :data:`MARK_ROWS` (a triangle authored cell by cell out of
   ``◢◣██``) with :data:`WORDMARK` and :data:`TAGLINE` beside it as plain
   text, assembled by :func:`drawn_lines`. Four rows. This is what every
   terminal without a real pixel protocol gets, which is most of them.
2. **Raster** -- ``assets/logo.png`` through :mod:`doxa.images`' existing
   ladder, but ONLY on ``kgp``/``sixel`` (:data:`PIXEL_TIERS`), the
   protocols that carry an actual bitmap. See :func:`use_image`.

**Why that way round, since v0.41.0 shipped it the other way.** A user
ran it and said: "quite pixelated -- then i would prefer to just show it
as unicode/ASCI blocks", and "the real logo png just in terminals that
support it". They were right, and the arithmetic says why: half-block is
not a pixel protocol with fewer pixels, it is a 2x-vertical approximation
made of ``▀``, so six rows of it is twelve vertical samples for a 238-row
image. A downscale AVERAGES, and averaging at that ratio is mush. A drawn
glyph is chosen, cell by cell, and is therefore exactly as sharp as the
font. The ``boot_banner`` setting (:func:`form`) pins either form for
anyone who disagrees.

**The raster half, when it does run.** ``logo.png`` (1100x320) rather than
``icon.png`` (512x512): a banner is a wide thing. The file is not handed
to the renderer as-is -- see :func:`_prepared` for the crop and the
alpha flatten, both of which a screenshot caught and a green suite did
not. Geometry is derived from a declared ROW BUDGET rather than from the
pixel size, because what an image occupies in a terminal is CELLS:

    columns = rows x cell_aspect x content_aspect = 6 x 2 x 3.899 ~= 47

Six rows is that budget -- a quarter of a classic 24-row terminal, about
the height of the identity block it introduces, so the banner never
outweighs the information beneath it. Only WIDTH is pinned; height comes
from the image widget using the terminal's own cell aspect, so a terminal
whose cells are not 2:1 gets the right number of rows rather than a
letterboxed six. Below :data:`MIN_COLUMNS` the raster has nowhere to go
and the drawn form takes over regardless of setting.

Nothing here ever renders ``[image: doxa logo]``. That line exists to tell
you an image you ASKED for could not be drawn; it is not fit to be the
permanent first line of every session.
"""

from __future__ import annotations

import importlib.resources
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import config as config_mod

ENV_VAR = "DOXA_BOOT_BANNER"

# The row budget (see module docstring) and the two aspect ratios it is
# spent against. CELL_ASPECT is the height-to-width ratio of a terminal
# cell, ~2 everywhere, and the same constant scripts/screenshot.py derives
# from its own measured SVG export (24.375 / 12.2).
ROWS = 6
CELL_ASPECT = 2.0

#: Aspect of the logo's INKED AREA -- 928 x 238, measured off the alpha
#: channel's bounding box, not the 1100 x 320 canvas. The asset's margin
#: is page layout for a README; inside a six-row budget every row is dear,
#: so :func:`prepared_image` crops it and this is the shape that survives.
#: ``test_banner`` re-measures the real file against this number, so the
#: constant cannot quietly drift away from the asset it describes.
CONTENT_ASPECT = 928 / 238

#: Banner width in CELLS -- what the widget's ``width`` style is set to.
COLUMNS = round(ROWS * CELL_ASPECT * CONTENT_ASPECT)  # 47

#: The background the logo's transparency is flattened onto -- theme.tcss's
#: ``$doxa-base``, stated here as a literal for the same reason the theme
#: states it: it is the ramp's own floor. With the `background` setting on
#: "transparent" the screen behind is the terminal's own, so this leaves a
#: faint dark rectangle; that is a far smaller wrong than the alternative,
#: which is what PIL's RGBA->RGB conversion does unaided -- see
#: :func:`prepared_image`.
BASE_COLOR = (0x17, 0x15, 0x12)

#: Narrower than this and the image is dropped for the wordmark. A 47-cell
#: banner inside 2 cells of padding needs 51 columns merely to fit; 64 is
#: where it stops being most of the line and the identity block beneath it
#: stops wrapping.
MIN_COLUMNS = 64

#: The DOXA mark -- ring and triangle -- DRAWN, not downscaled.
#:
#: Authored on a 9x8 subpixel grid and folded to half-block cells, so
#: every cell was CHOSEN: ``█`` where both subpixels are ink, ``▀``/``▄``
#: where one is, a space where neither. That is the whole difference from
#: the raster path and the whole of the complaint that produced it -- a
#: resampled photograph AVERAGES, and averaging at four rows is what the
#: user was looking at when they wrote "quite pixelated". Nothing here is
#: averaged, so nothing here is mushy: it is as sharp as the font.
#: Two shapes were drawn, rendered in a real monospace font at true cell
#: metrics, and LOOKED AT before this one was kept -- which is the only
#: way any of this gets decided. A ring proved impossible at this size: a
#: one-subpixel outline renders as horizontal bars, not as a circle. A
#: triangle built from ``▀▄█`` alone renders as a stepped pyramid, because
#: every row is a solid rectangle and the eye reads three stacked bars.
#: The quadrant triangles ``◢``/``◣`` are what give the edge a real slope.
#:
#: The triangle is the logo's distinctive element and it survives the
#: reduction intact. Four rows, two fewer than the raster it replaces.
#:
#: **Two limitations, both shown to the user and both accepted -- this
#: shape is SETTLED, and is not to be iterated on again without them.**
#: At four rows it reads as a stepped, stacked shape rather than as the
#: smooth triangle-in-a-ring of the PNG: there are not enough rows for the
#: ring and the tiers are visible. Dropping the mark for the wordmark
#: alone was offered and declined. And ``◢``/``◣`` are U+25E2/U+25E3, in
#: Geometric Shapes rather than the Block Elements the rest of this uses,
#: so a font without that coverage shows tofu where ``▀▄█`` alone would
#: not -- the real price of the sloped edge, paid knowingly.
MARK_ROWS: tuple[str, ...] = (
    "   ◢◣   ",
    "  ◢██◣  ",
    " ◢████◣ ",
    "◢██████◣",
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

# Row indices the text sits on: the mark's two middle rows, which centres
# it against the triangle instead of hanging it off the apex or the base.
_WORDMARK_ROW = 1
_TAGLINE_ROW = 2

MARK_COLOR = "#D97757"
MUTED_COLOR = "#8A8073"


def drawn_lines(content_columns: int) -> "list[str]":
    """The drawn banner as Textual markup rows, fitted to the width it has.

    THIS IS THE NORMAL PATH, not a fallback. Since v0.49.0 the raster is
    the exception -- see :func:`use_image` -- and every terminal without a
    real pixel protocol gets these rows.

    Three shapes, widest first, so what survives at any width is the part
    that still reads: mark + wordmark + tagline, mark + wordmark, then the
    bare wordmark. A drawn mark that has shrunk past legibility is dropped
    rather than crushed, on the same rule the rest of this module follows.
    """
    if content_columns >= DRAWN_FULL_COLUMNS:
        beside = {_WORDMARK_ROW: f"[b {MARK_COLOR}]{WORDMARK}[/]",
                  _TAGLINE_ROW: f"[{MUTED_COLOR}]{TAGLINE}[/]"}
    elif content_columns >= DRAWN_MARK_COLUMNS:
        beside = {_WORDMARK_ROW: f"[b {MARK_COLOR}]{WORDMARK}[/]"}
    else:
        return [f"[b {MARK_COLOR}]{WORDMARK}[/]"]
    gap = " " * MARK_GAP
    lines = []
    for index, row in enumerate(MARK_ROWS):
        text = beside.get(index)
        lines.append(f"[{MARK_COLOR}]{row}[/]" + (gap + text if text else ""))
    return lines


#: What the ``boot_banner`` setting can say. ``auto`` is the rule in
#: :func:`use_image`; the other three take the decision away from it.
FORMS = ("auto", "blocks", "image", "off")

#: Tiers carrying REAL pixels at real resolution. Half-block is not one of
#: them: it is a 2x-vertical approximation built out of ``▀``, and at the
#: banner's six rows that is twelve vertical samples for a 238-row image.
#: This tuple is the whole of :func:`use_image`'s ``auto`` rule.
PIXEL_TIERS = ("kgp", "sixel")

# The bool spelling this knob shipped with in v0.41.0. A config.toml
# written by that settings modal still says 1 or 0, and it has to keep
# meaning what it meant.
_LEGACY_OFF = ("0", "false", "no", "off")
_LEGACY_ON = ("1", "true", "yes", "on")


def form() -> str:
    """How the opening banner should be drawn: one of :data:`FORMS`.

    ``auto`` (default) is the v0.49.0 rule -- drawn blocks where a raster
    would only be a downscale, the raster where the terminal has real
    pixels. ``blocks`` and ``image`` pin it either way; ``off`` removes the
    banner. Legacy ``1``/``0`` read as ``auto``/``off``."""
    raw = config_mod.raw(ENV_VAR).strip().lower()
    if not raw:
        return "auto"
    if raw in _LEGACY_OFF:
        return "off"
    if raw in _LEGACY_ON:
        return "auto"
    return raw if raw in FORMS else "auto"


def enabled() -> bool:
    """Is the opening banner drawn at all? Default yes.

    ON by default is a judgment call and it rests on the fallback rather
    than on the picture: there is no terminal and no width at which this
    costs more than three rows of something legible."""
    return form() != "off"


def asset_path() -> "Path | None":
    """The logo file, or None when neither copy is on disk.

    Installed wheel: ``pyproject.toml``'s force-include put it at
    ``doxa/assets/logo.png``, the arrangement ``doxa.launcher`` already
    uses for ``icon.png`` -- one file in git, no duplicate under ``doxa/``.
    Source checkout: it is still only at the repo's own ``assets/``.
    Neither present is not an error; it is a banner that does not draw --
    and neither is a loader that cannot answer the question, which is why
    the whole lookup is guarded rather than just the packaged half."""
    try:
        packaged = importlib.resources.files("doxa") / "assets" / "logo.png"
        if packaged.is_file():
            return Path(str(packaged))
    except Exception:  # noqa: BLE001 -- a zipped/frozen/odd loader is a
        # banner that falls through to the checkout copy, never a crash
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

    **Everything is inside the try, including the import** (v0.49.0).
    ``doxa.images.widget_for`` is documented never to raise and always to
    return a mountable widget, but this function is DOXA's own code on the
    near side of that guarantee: it runs during ``BootBanner.compose``,
    and an exception there does not degrade a decoration, it takes the
    pane boot down with it. Pillow is a declared dependency now, so the
    import "cannot" fail -- which is exactly the class of assumption that
    produces a bug report, and it costs one indent to not make it."""
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
    except Exception:  # noqa: BLE001 -- a banner that cannot be prepared is
        # a banner that does not draw; use_image's caller falls to the
        # wordmark, which needs no file at all.
        return None


def image_source() -> Any:
    """What to hand :func:`doxa.images.widget_for` -- a fresh copy of the
    prepared image, or None when there is nothing to prepare."""
    prepared = _prepared()
    return prepared.copy() if prepared is not None else None


def use_image(mode: str, columns: int) -> bool:
    """Should the banner be the RASTER logo rather than the drawn mark?

    **The v0.49.0 rule, and it came from a user looking at the thing.**
    The report was "quite pixelated -- then i would prefer to just show it
    as unicode/ASCI blocks", against a half-block render on an ordinary
    Linux terminal. That is not a bug; it is arithmetic. Six rows of
    half-block is twelve vertical samples for a 238-row image, and the
    tagline inside the asset gets under one sample per stroke. A drawn
    glyph beats a resampled photograph at those dimensions, so under
    ``auto`` the raster has to EARN its place by having real pixels to
    spend -- :data:`PIXEL_TIERS`, the protocols that carry an actual
    bitmap. ``halfblock`` does not qualify, and that is the whole change.

    The other forms take the decision away: ``blocks`` never draws the
    raster, ``image`` draws it wherever any pixel tier exists at all
    (half-block included -- the way back to v0.41.0's look), ``off`` never
    reaches here.

    The remaining two nos are unchanged, and are failures rather than
    choices: the ``text`` tier has no pixels to spend at any size, and a
    terminal under :data:`MIN_COLUMNS` has pixels but nowhere to put
    them."""
    chosen = form()
    if chosen in ("off", "blocks"):
        return False
    if mode == "text":
        return False
    if columns < MIN_COLUMNS:
        return False
    if chosen == "auto" and mode not in PIXEL_TIERS:
        return False
    return _prepared() is not None


def fallback_reason(mode: str, columns: int) -> str:
    """One line naming why the raster was not drawn, or "" when there is
    nothing worth saying -- which is now the common case.

    **Narrower than the version written a day earlier, on purpose.** While
    the wordmark was only ever a fallback, reaching it always meant
    something had gone wrong and saying so was a service. Under ``auto``
    the wordmark is the INTENDED output on a half-block terminal, and
    announcing "logo not drawn" over a banner drawing exactly as designed
    would be noise on most sessions -- trading the report this line was
    written for against a worse one.

    So it speaks only where the user asked for the raster and did not get
    it: ``image`` pinned and not honorable, or the asset missing or
    unreadable. Everything else is ``/img``'s to answer, which is what
    this line points at when it does appear."""
    if use_image(mode, columns) or form() in ("off", "blocks"):
        return ""
    if asset_path() is None:
        return "logo not drawn: assets/logo.png is missing from this install — /img"
    if _prepared() is None:
        return "logo not drawn: the logo file could not be decoded here — /img"
    if form() != "image":
        # auto, and this tier simply has no real pixels: the wordmark IS
        # the answer here, not a consolation for one.
        return ""
    if mode == "text":
        return (
            "logo not drawn: this terminal has no pixel mode — "
            "headless, a pipe, or a remote relay — /img"
        )
    return (
        f"logo not drawn: terminal is {columns} columns, the logo needs "
        f"{MIN_COLUMNS} — /img"
    )
