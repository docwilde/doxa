# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.banner -- the DOXA mark at the top of a session's opening block.

**One form, on every terminal.** A broad, solid triangle (:data:`MARK_ROWS`)
beside the Greek word it names the app for -- ΔΟΞΑ, drawn in blocks
(:data:`GREEK_ROWS`) the same colour as the triangle -- with
``belief earns knowledge`` (:data:`TAGLINE`) beneath both as ordinary text.
:func:`drawn_lines` assembles whichever of the three degrades the width in
front of it can hold; :func:`enabled` is the only decision left, on or off.

**v0.74.0 dropped the ring.** Through v0.70.0 the mark was a grey ring
around the triangle, with ``DOXA`` as plain Latin text beside it -- the
owner's call there was that stylised letters were "something to squint
at, where four ordinary capitals are simply legible" (v0.41.0's own
resolution). The Greek original changes that calculus: ΔΟΞΑ *is* the
word this app is named for, and doxa means belief -- spelling it out in
the alphabet it is actually spelled in, rather than transliterating it
to four Latin capitals, is worth the block art four plain letters did
not need. See :data:`GREEK_ROWS` for the letterforms and why full blocks
were the only safe way to draw them.

**v0.70.0 removed the raster ``logo.png`` form this module used to draw
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
# for. The boot banner itself never reads these (v0.70.0). CELL_ASPECT
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

MARK_COLOR = "#D97757"
MUTED_COLOR = "#8A8073"

#: The DOXA mark -- a solid triangle, DRAWN, not downscaled, no ring.
#:
#: **One codepoint and one space, still.** ``█`` (U+2588 FULL BLOCK) and
#: ``" "``, nothing else -- the user's own constraint from the ring-era
#: mark, reached by looking at rendered candidates rather than by
#: argument, and it still holds here with nothing new to reconsider:
#:
#: * Half blocks (``▀`` U+2580, ``▄`` U+2584) -- *"do not use half-blocks
#:   / it leaves gaps"*. They are drawn against the font's own baseline
#:   and leading, so a column of them seams horizontally instead of
#:   reading as one stroke, and a triangle this size has no slack to
#:   spend on a seam.
#: * Quadrant triangles (``◢``/``◣`` U+25E2/U+25E3), which an earlier
#:   revision used for a sloped edge -- they live in Geometric Shapes
#:   rather than Block Elements, so a font covering one need not cover the
#:   other, and the mark degrades to tofu rather than to something plainer.
#:
#: **No ring.** The ring existed to frame a narrow, spiky triangle and
#: needed a hand-tightened moat (see this module's git history) to keep
#: from reading as one blob with it. Dropping it does not reopen that
#: problem: there is nothing left inside the mark for the triangle to
#: touch. What replaces the ring's job of "something else in the shot"
#: is :data:`GREEK_ROWS` beside it -- two shapes again, but two shapes
#: that both carry meaning instead of one carrying a frame.
#:
#: **Broader, on purpose.** The ring-era triangle spanned about 5 columns
#: over its 7 inner rows -- a spike, not a triangle, and only that narrow
#: because the ring's moat left it nowhere to grow. Unframed, this one
#: is free to be the shape the brief asked for: apex a single cell,
#: widening by two columns a row, base the full 15-column width -- a
#: rise of 15 columns over 7 rows against a terminal cell's own ~2:1
#: height-to-width, i.e. visually almost as wide as it is tall rather
#: than the old spike's ~0.7:2 sliver. Solid fill, not an outline --
#: :data:`GREEK_ROWS`' own Δ is the outline version of the same shape,
#: right beside it, and the contrast between solid icon and hollow
#: letter is doing work: the same triangle, read two ways, is the reason
#: the two sit next to each other at all.
MARK_ROWS: tuple[str, ...] = (
    "       █       ",
    "      ███      ",
    "    ███████    ",
    "   █████████   ",
    "  ███████████  ",
    " █████████████ ",
    "███████████████",
)

#: Width of :data:`MARK_ROWS`, and the height every row of :data:`GREEK_ROWS`
#: has to match to sit beside it without a seam.
MARK_COLUMNS = max(len(row) for row in MARK_ROWS)
#: The blank gutter between the triangle and whatever sits beside it --
#: the Greek word at full width, the plain Latin :data:`WORDMARK` at mid
#: width.
MARK_GAP = 3

#: The Greek word this app is named for, drawn in the same block alphabet
#: as the triangle, and the same colour (:data:`MARK_COLOR`) -- the
#: brief's own instruction, and also the thing that makes wrapping every
#: row in one colour tag (:func:`drawn_lines`) enough: there is no second
#: colour left to keep apart from the first, the way the old ring and
#: triangle needed to be.
#:
#: **Why blocks and not the Unicode letters themselves.** ``Δ``, ``Ο``,
#: ``Ξ`` and ``Α`` are ordinary characters DOXA could just print --
#: cheaper than authoring a 7-row grid for each. It is not done that way
#: for the same reason ``◢``/``◣`` were rejected above: Greek coverage in
#: a monospace font is not guaranteed the way Block Elements coverage is
#: (``█`` is used constantly across this whole UI, everywhere, and has
#: never once been the tofu), so printing the letters as text risks the
#: exact failure this module already rejected a glyph family for once.
#: Drawing them from ``█`` sidesteps that risk entirely rather than
#: trading it for a new one -- one constraint, paying for two decisions.
#:
#: **Ξ and Α were the hard letters, named as such in the brief.**
#:
#: * Ξ (xi) is three horizontal bars and nothing else. The failure mode
#:   at block resolution is the bars fusing into one dark rectangle, so
#:   the fix was rows, not cleverness: two full-blank rows between each
#:   bar (rows 0/3/6 are ink, 1-2 and 4-5 are not) rather than the one row
#:   a 5-row letter would have been forced to spend. Reads as three bars
#:   at every width this module still draws it at -- checked by rendering
#:   through the real Textual SVG exporter at true 2:1 cell metrics, the
#:   same way the ring/triangle mark's own geometry was chosen, not
#:   guessed at from the source grid.
#: * Α (alpha) needs a crossbar that reads as a crossbar and not a
#:   floating dash. Two diagonal legs spread by one column a row from a
#:   single-cell apex, a crossbar filling the FULL span between the legs
#:   at the row they reach the letter's half-width (not a short dash
#:   centred under the apex), then the legs run straight down to a flat
#:   base. The crossbar's width is what reads: a short one is a Latin
#:   `n` with a hat, not an Α.
#:
#: Both are legible at 7 rows with the letter widths below; neither
#: needed to grow past that to get there, which is why the row budget
#: did not have to grow past what :data:`MARK_ROWS` already spends (see
#: :data:`TAGLINE`'s own note on the total).
_DELTA_ROWS: tuple[str, ...] = (
    "    █    ",
    "   █ █   ",
    "  █   █  ",
    " █     █ ",
    "█       █",
    "█       █",
    "█████████",
)
_OMICRON_ROWS: tuple[str, ...] = (
    " █████ ",
    "███████",
    "██   ██",
    "██   ██",
    "██   ██",
    "███████",
    " █████ ",
)
_XI_ROWS: tuple[str, ...] = (
    "█████████",
    "         ",
    "         ",
    "  █████  ",
    "         ",
    "         ",
    "█████████",
)
_ALPHA_ROWS: tuple[str, ...] = (
    "    █    ",
    "   █ █   ",
    "  █   █  ",
    " ███████ ",
    "█       █",
    "█       █",
    "█       █",
)
#: Blank columns between one letter and the next -- narrower than
#: :data:`MARK_GAP` (the triangle needs more air around it, standing
#: alone as the icon; letters of one word read as a word with less).
_LETTER_GAP = "  "

assert len({len(_DELTA_ROWS), len(_OMICRON_ROWS), len(_XI_ROWS), len(_ALPHA_ROWS),
            len(MARK_ROWS)}) == 1, "every letter and the triangle must share one row count"

#: ΔΟΞΑ, assembled letter by letter with :data:`_LETTER_GAP` between them
#: -- one row per index of :data:`MARK_ROWS`, so ``zip`` against it in
#: :func:`drawn_lines` never runs one out before the other.
GREEK_ROWS: tuple[str, ...] = tuple(
    _LETTER_GAP.join(letters)
    for letters in zip(_DELTA_ROWS, _OMICRON_ROWS, _XI_ROWS, _ALPHA_ROWS)
)
GREEK_COLUMNS = max(len(row) for row in GREEK_ROWS)

#: The wordmark, as PLAIN TEXT -- now the MID-width fallback rather than
#: the full-width form. v0.41.0 drew "DOXA" in block glyphs, and the
#: user's own resolution of that was "the wordmark as plain text": four
#: ordinary capitals are simply legible where stylised letters are
#: something to squint at. That is still true of a terminal too narrow
#: for :data:`GREEK_ROWS`' 40 columns but wide enough for the triangle --
#: it gets the plain Latin name beside the mark instead of nothing, the
#: same shape v0.60.0's full form used to be. Only a terminal too narrow
#: even for the triangle drops to this alone (see :func:`drawn_lines`).
WORDMARK = "DOXA"

#: The strapline, as real text for the same reason -- and BELOW the mark
#: now rather than beside it, the brief's own instruction. Dropped the
#: "doxa · " prefix the old copy carried: ΔΟΞΑ already spells the word
#: out above it, in the alphabet it is actually spelled in, so repeating
#: the transliteration here would be saying the same thing twice.
TAGLINE = "belief earns knowledge"

#: Widths at which the drawn form can show the triangle beside the Greek
#: word, and the triangle beside the plain Latin fallback. Below the
#: second it is the bare :data:`WORDMARK`: a mark with nothing to name it
#: is a shape, not a banner. Both derived from the art's own measured
#: width, not chosen freehand -- change a glyph and these move with it.
DRAWN_FULL_COLUMNS = MARK_COLUMNS + MARK_GAP + GREEK_COLUMNS
DRAWN_MARK_COLUMNS = MARK_COLUMNS + MARK_GAP + len(WORDMARK)

#: Row index (0-based, into :data:`MARK_ROWS`/:data:`GREEK_ROWS`) the
#: plain :data:`WORDMARK` sits on at MID width -- the middle row of the
#: seven, so the Latin fallback keeps the same vertical placement the
#: Greek word has at full width instead of hugging the triangle's apex or
#: its base.
_WORDMARK_ROW = 3

#: Total rows :func:`drawn_lines` returns at FULL width: the triangle's
#: own :data:`MARK_ROWS` (also the row count at MID width, mark alone),
#: plus one blank row and one for :data:`TAGLINE`.
#:
#: **Same nine as the ring-era mark, not more, despite ΔΟΞΑ needing real
#: rows Α and Ξ could be told apart in.** The ring's own moat was two
#: rows of the old nine's budget, spent on keeping the ring from reading
#: as one blob with the triangle it framed; dropping the ring gives those
#: rows back. What used to be nine rows of ring-plus-triangle is now
#: seven of triangle-plus-word (this module's own :data:`MARK_ROWS`
#: height) with two spent on the tagline dropping BELOW instead of being
#: squeezed onto a row already carrying glyph art -- the same total, a
#: different shape, and the mid-width form (triangle alone, seven rows)
#: is now shorter than the old mark ever was at any width.
FULL_ROWS = len(MARK_ROWS) + 2


def drawn_lines(content_columns: int) -> "list[str]":
    """The drawn banner as Textual markup rows, fitted to the width it has.

    THIS IS THE ONLY PATH (v0.70.0 retired the raster alternative
    v0.58.0-0.65.0 drew on kgp/sixel terminals) -- every terminal, every
    tier, gets these rows.

    Three shapes, widest first, so what survives at any width is the part
    that still reads: triangle + ΔΟΞΑ + tagline, triangle + the plain
    name, then the bare name alone. A drawn form that has shrunk past
    legibility is dropped rather than crushed, on the same rule the rest
    of this module follows -- and each drop is a whole tier, not a
    partial one: there is no state where half a letter or the tagline
    without the word it sits under is on screen."""
    if content_columns >= DRAWN_FULL_COLUMNS:
        gap = " " * MARK_GAP
        lines = [
            f"[{MARK_COLOR}]{mark_row}{gap}{greek_row}[/]"
            for mark_row, greek_row in zip(MARK_ROWS, GREEK_ROWS)
        ]
        lines.append("")
        indent = " " * (MARK_COLUMNS + MARK_GAP)
        lines.append(f"{indent}[{MUTED_COLOR}]{TAGLINE}[/]")
        return lines
    if content_columns >= DRAWN_MARK_COLUMNS:
        gap = " " * MARK_GAP
        beside = f"{gap}[b {MARK_COLOR}]{WORDMARK}[/]"
        return [
            f"[{MARK_COLOR}]{row}[/]" + (beside if index == _WORDMARK_ROW else "")
            for index, row in enumerate(MARK_ROWS)
        ]
    return [f"[b {MARK_COLOR}]{WORDMARK}[/]"]


# Every spelling this knob has ever shipped that means OFF: the plain
# "off", and the bool spelling it launched with in v0.41.0 ("0"/"false"/
# "no", back when 1/0 read as on/off). v0.58.0 added "auto"/"blocks"/
# "image" as ON spellings on top of that; v0.70.0 retired the three-way
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
    (v0.70.0 dropped the raster ``logo.png`` alternative). ON by default
    is a judgment call and it rests on the drawn form rather than on a
    picture: there is no terminal and no width at which this costs more
    than a few rows of something legible."""
    raw = config_mod.raw(ENV_VAR).strip().lower()
    return raw not in _LEGACY_OFF


def asset_path() -> "Path | None":
    """The logo file, or None when neither copy is on disk.

    Only :func:`_prepared` (in turn only ``/img``'s
    :class:`~doxa.ui.transcript.ImageShowcaseBlock`, since v0.70.0
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
    docstring for why that is the only caller left since v0.70.0), and an
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
    showcase is the only caller since v0.70.0 retired the banner's own
    raster form."""
    prepared = _prepared()
    return prepared.copy() if prepared is not None else None
