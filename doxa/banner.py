"""doxa.banner -- the DOXA mark at the top of a session's opening block.

**What it is.** The same `assets/logo.png` the README opens with, rendered
through :mod:`doxa.images`' existing ladder into the first thing a session
puts on screen, above the identity block. Nothing here is a second render
path: this module decides WHICH asset, HOW BIG, and WHETHER AT ALL, and
then hands the answer to :func:`doxa.images.widget_for` like every other
render site does.

**Which asset.** ``logo.png`` (1100x320) rather than ``icon.png``
(512x512): a banner is a wide thing, and the square mark alone drops the
wordmark that makes it read as DOXA rather than as a triangle. The logo's
own tagline line ("doxa - belief earning knowledge") is set small enough
that it degrades to a soft grey rule at terminal sizes -- that is the
asset's design rather than damage we introduced, and the wordmark above it
stays legible all the way down to the half-block tier (measured by
down-sampling to the exact cell grid and looking at it).

The file is not handed to the renderer as-is; see :func:`prepared_image`
for the two things done to it first, both of which a screenshot found and
a passing test suite did not.

**How big, and why in CELLS.** A 1100px-wide image means nothing to an
80-column terminal; what it occupies is cells. So the geometry is derived
from a declared ROW BUDGET rather than from the pixel size:

    columns = rows x cell_aspect x content_aspect = 6 x 2 x 3.899 ~= 47

Six rows is the budget, and it is the number this module actually defends.
It is a quarter of a classic 24-row terminal, and about the height of the
identity block's own field list directly beneath it -- so the banner never
outweighs the information it introduces, which is the thing that turns a
logo into a nuisance by the third session of the day. 47 columns is then
under 40% of a 120-column terminal, which reads as a deliberate size at
every width wide enough to draw it at all.

Only WIDTH is pinned. Height is left to the image widget, which derives it
from the terminal's OWN cell aspect -- 6 rows on the usual 2:1 cell, and
whatever is actually right on a terminal whose cells are not. Pinning both
would letterbox or distort on exactly the terminals that got it wrong.

**Degrading rather than smearing.** Below :data:`MIN_COLUMNS` a 47-cell
banner is most of the line and the identity block under it starts wrapping,
so the image is replaced by :data:`WORDMARK` -- three rows of half-block
glyphs spelling DOXA, hand-rolled here (small, one place, no dependency).
The same wordmark is what the ``text`` tier gets, because
``[image: doxa logo]`` as the first line of every session is honest and
unusable: the fallback line exists to tell you an image you asked for
could not be drawn, not to be the permanent state of a decoration.
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

#: Three rows: two of half-block glyphs, then the logo's own tagline. The
#: same glyph vocabulary the half-block tier draws with, so the text tier
#: looks like a smaller sibling of the pixel one rather than a different
#: program. Colors are applied by the caller (accent, then muted).
WORDMARK_ROWS: tuple[str, ...] = (
    "█▀▄ █▀█ ▀▄▀ ▄▀▄",
    "█▄▀ █▄█ ▄▀▄ █▀█",
)

#: The tagline set into the asset itself, repeated as real text because at
#: 6 rows the drawn one is a grey rule and nothing more.
TAGLINE = "doxa · belief earning knowledge"

#: Width of :data:`WORDMARK_ROWS` / :data:`TAGLINE`, whichever is wider --
#: what a caller needs to know before deciding it does not fit either.
WORDMARK_COLUMNS = max(len(r) for r in (*WORDMARK_ROWS, TAGLINE))


def _bool(env_name: str, default: bool) -> bool:
    """Same four lines doxa.worktrees / doxa.clock / doxa.notify each keep
    for themselves -- the house convention for a default-ON knob, kept
    local rather than imported for one helper."""
    raw = config_mod.raw(env_name).strip()
    if not raw:
        return default
    return raw.lower() not in ("0", "false", "no", "off")


def enabled() -> bool:
    """Is the opening banner on? Default ON, ``DOXA_BOOT_BANNER=0`` (or the
    ``boot_banner`` row in the settings modal) turns it off.

    ON by default is a judgment call, and it rests on the degrade path
    rather than on the picture: there is no terminal and no width at which
    this costs more than three rows of something legible, and the tier that
    would otherwise show ``[image: ...]`` shows the wordmark instead. A
    default-off banner would also mean the image renderer ships untested on
    every machine that never finds the switch."""
    return _bool(ENV_VAR, True)


def asset_path() -> "Path | None":
    """The logo file, or None when neither copy is on disk.

    Installed wheel: ``pyproject.toml``'s force-include put it at
    ``doxa/assets/logo.png``, the arrangement ``doxa.launcher`` already
    uses for ``icon.png`` -- one file in git, no duplicate under ``doxa/``.
    Source checkout: it is still only at the repo's own ``assets/``.
    Neither present is not an error; it is a banner that does not draw."""
    packaged = importlib.resources.files("doxa") / "assets" / "logo.png"
    try:
        if packaged.is_file():
            return Path(str(packaged))
    except (OSError, TypeError):  # noqa: BLE001 -- a zipped/odd loader
        pass
    checkout = Path(__file__).resolve().parent.parent / "assets" / "logo.png"
    return checkout if checkout.is_file() else None


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
    copied per caller so no widget can mutate another's image."""
    from PIL import Image

    path = asset_path()
    if path is None:
        return None
    try:
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
    """Should the banner be the real asset rather than the wordmark?

    Two ways to answer no, and they are different failures: the ``text``
    tier has no pixels to spend at any size, and a narrow terminal has
    pixels but nowhere to put them. Both land on the wordmark, which is
    why this returns a bool rather than a mode."""
    if mode == "text":
        return False
    if columns < MIN_COLUMNS:
        return False
    return _prepared() is not None
