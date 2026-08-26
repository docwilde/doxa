# SPDX-License-Identifier: AGPL-3.0-only
"""Opening banner + /img showcase (v0.41.0).

Every assertion here is a USER-VISIBLE outcome, on the v0.28.0 rule: the
invisible-button defect passed every structural check for a full release
because "the widget is in the DOM" and "the user can see it" are different
claims. Images make that failure mode cheap to hit -- a textual-image
widget that measures to zero rows mounts perfectly happily -- so the
banner tests assert REGION HEIGHT, not membership.

The suite runs with DOXA_IMAGE_MODE=text (conftest), so the pixel-tier
tests force their tier the way tests/test_images.py already does.
"""

from __future__ import annotations

import pytest

from doxa import banner, images
from doxa.app import _DrawnMark, BootBanner, DoxaApp, ImageShowcaseBlock, SystemBlock

# A realistic terminal size for scenes that just need room -- the default
# run_test size (80x24) is fine for the banner itself (v0.70.0: it is the
# same drawn form at every width above DRAWN_FULL_COLUMNS), but several
# /img showcase scenes want more vertical room than 24 rows gives.
WIDE = (120, 34)


async def _settle(pilot, tries: int = 60) -> None:
    for _ in range(tries):
        await pilot.pause(0.02)


def _banner(app):
    found = list(app.query(BootBanner))
    return found[0] if found else None


# -- geometry ----------------------------------------------------------


def test_the_drawn_mark_uses_full_blocks_and_nothing_else():
    """The user's constraint, reached by looking at rendered candidates:
    **"No, use the full block"**. One codepoint plus space -- true of the
    triangle AND (v0.72.0) of ΔΟΞΑ, drawn in the same alphabet rather than
    printed as Unicode Greek text.

    Every glyph excluded here was excluded for a reason that survives the
    font it was tested in, so this asserts the codepoints rather than the
    appearance:

    * ``▀``/``▄`` (U+2580/U+2584) -- *"do not use half-blocks / it leaves
      gaps"*: drawn against the font's own baseline and leading, so a
      column of them seams instead of reading as one stroke.
    * ``◢``/``◣`` (U+25E2/U+25E3) -- Geometric Shapes, not Block Elements.
      A font can cover one and not the other, and the failure is tofu.
    * The Greek letters THEMSELVES (``Δ``, ``Ο``, ``Ξ``, ``Α``) -- printing
      them as text would trade the tofu risk above for an equivalent one:
      Greek coverage in a monospace font is not guaranteed the way Block
      Elements coverage is. Drawing them from ``█`` sidesteps it instead
      of reintroducing it under a different name.
    """
    assert banner.MARK_ROWS
    assert banner.GREEK_ROWS
    used = set("".join(banner.MARK_ROWS) + "".join(banner.GREEK_ROWS))
    assert used <= {"█", " "}, (
        f"mark uses {sorted(f'U+{ord(c):04X}' for c in used)}; "
        "only U+2588 FULL BLOCK and space are allowed"
    )
    assert "█" in used, "a mark of pure whitespace is not a mark"
    for row in banner.MARK_ROWS:
        assert len(row) == banner.MARK_COLUMNS, "the triangle must be rectangular"
    for row in banner.GREEK_ROWS:
        assert len(row) == banner.GREEK_COLUMNS, "the Greek word must be rectangular"
    assert len(banner.MARK_ROWS) == len(banner.GREEK_ROWS), (
        "triangle and word must share a row count to sit beside each other"
    )
    # Unicode Greek does not appear anywhere in the drawn output -- it is
    # ALL block art, per the constraint above.
    assert not (used & set("ΔΟΞΑ"))
    # The Latin fallback is PLAIN TEXT, not glyph art -- v0.41.0's own call,
    # now the MID-width degrade rather than the full-width form.
    assert banner.WORDMARK == "DOXA"
    assert set(banner.WORDMARK).isdisjoint("▀▄█▌▐")


def test_the_mark_is_a_solid_triangle_with_no_ring():
    """v0.72.0 dropped the ring: the brief asked for "a simple, broader
    triangle... not a narrow spike", and there is nothing left inside the
    mark for a ring to frame. Every row is exactly ONE run of ink (no
    ring/gap/triangle/gap/ring split any more), and the triangle widens
    monotonically from a single-cell apex to a full-width base."""
    rows = banner.MARK_ROWS
    widths = []
    for row in rows:
        stripped = row.strip()
        assert stripped, "a blank row in the triangle"
        assert row.count("█") == len(stripped), (
            f"row {row!r} is not one contiguous run of ink -- the ring is back"
        )
        widths.append(len(stripped))
    assert widths[0] == 1, "the apex must be a single cell"
    assert widths[-1] == banner.MARK_COLUMNS, "the base must span the full width"
    assert widths == sorted(widths), f"the triangle does not widen monotonically: {widths}"
    # "Broader... not a narrow spike": base width relative to row count,
    # against the ~2:1 height-to-width a terminal cell actually draws at
    # (CELL_ASPECT). A visually-square triangle needs base ~= 2*rows; this
    # one clears that rather than merely approaching it.
    assert banner.MARK_COLUMNS >= 2 * len(rows), (
        f"base {banner.MARK_COLUMNS} over {len(rows)} rows reads as a spike, "
        "not a broad triangle, once cell aspect is accounted for"
    )


def test_delta_is_the_hollow_outline_of_the_same_triangle():
    """Design intent, pinned as shape: Δ echoes :data:`MARK_ROWS`' own
    triangle -- same apex-to-base widening -- but hollow (edges only, plus
    a solid base), the way a real capital delta is drawn, rather than a
    second filled triangle sitting redundantly next to the icon."""
    rows = banner._DELTA_ROWS
    assert len(rows) == len(banner.MARK_ROWS)
    for row in rows[:-1]:
        assert 1 <= row.count("█") <= 2, (
            f"row {row!r} of Δ is not an outline (edges only) above the base"
        )
    assert rows[-1] == "█" * len(rows[-1]), "Δ's base must be solid, closing the shape"
    apex_col = rows[0].index("█")
    assert rows[0].count("█") == 1, "Δ's apex must be a single cell"
    # The two edges spread outward from the apex column on every row above
    # the base.
    for row in rows[1:-1]:
        cols = [i for i, c in enumerate(row) if c == "█"]
        assert len(cols) == 2, f"row {row!r} does not have exactly two edge cells"
        assert cols[0] <= apex_col <= cols[1]


def test_xi_is_three_separated_bars():
    """The letter named as hard in the brief: Ξ is three horizontal bars
    and nothing else, and the risk at block resolution is them fusing
    into one dark rectangle. Pinned directly: exactly three ink rows, each
    a single unbroken run, each separated from its neighbour by at least
    one fully blank row."""
    rows = banner._XI_ROWS
    ink_rows = [i for i, row in enumerate(rows) if row.strip()]
    assert len(ink_rows) == 3, f"Ξ must be exactly three bars, found ink on rows {ink_rows}"
    for i in ink_rows:
        stripped = rows[i].strip()
        assert rows[i].count("█") == len(stripped), f"row {rows[i]!r} is not one unbroken bar"
    gaps = [b - a for a, b in zip(ink_rows, ink_rows[1:])]
    assert all(g >= 2 for g in gaps), (
        f"bars are only {gaps} rows apart -- not enough separation to read as three"
    )
    # Top and bottom bars run the letter's full width; a middle bar equal
    # to the outer two would be indistinguishable from a solid block at
    # this resolution, so it is deliberately narrower and centred.
    top, mid, bottom = (rows[i] for i in ink_rows)
    assert len(top.strip()) == len(bottom.strip()) == len(top)
    assert len(mid.strip()) < len(top)
    assert mid == mid.strip().center(len(mid))


def test_alpha_has_a_full_width_crossbar():
    """The other letter named as hard: Α needs a crossbar that reads as a
    crossbar, not a floating dash. Pinned as shape: there is a row whose
    ink is one unbroken run spanning at least half the letter's width (not
    a short run centred under the apex), with two legs above it (the apex
    splitting into a V) and two legs below it running to the base."""
    rows = banner._ALPHA_ROWS
    run_lengths = [row.count("█") for row in rows]
    crossbar_index = max(range(len(rows)), key=lambda i: run_lengths[i])
    crossbar = rows[crossbar_index]
    stripped = crossbar.strip()
    assert crossbar.count("█") == len(stripped), f"crossbar row {crossbar!r} has a gap in it"
    assert len(stripped) >= len(crossbar) // 2, "the crossbar must span real width, not a dash"
    for row in rows[:crossbar_index]:
        assert row.count("█") in (1, 2), f"row {row!r} above the crossbar is not legs/apex"
    for row in rows[crossbar_index + 1:]:
        assert row.count("█") == 2, f"row {row!r} below the crossbar is not two legs"


def test_omicron_is_round_with_the_same_single_cell_stroke_as_its_neighbours():
    """A review pass caught this one directly: an earlier Ο drew its sides
    two columns wide (``██   ██``) to make the curve read as a curve,
    which left three letters at one stroke weight and a fourth at double
    it -- uneven type, not a rounder O, and at wordmark scale that
    unevenness is exactly what stops four letters reading as one word.

    Pinned on the side rows specifically (top/bottom caps are a short
    horizontal run, same idea as Δ's solid base, and not what the
    original defect was about): each is exactly two ink cells, one column
    wide apiece -- never a doubled-width ``██`` edge -- and the two are
    on OPPOSITE sides of the letter, not adjacent. Roundness comes from
    the caps being narrower than the body instead, checked directly."""
    rows = banner._OMICRON_ROWS
    assert len(rows) == len(banner.MARK_ROWS)
    sides = rows[1:-1]
    for row in sides:
        cols = [i for i, c in enumerate(row) if c == "█"]
        assert len(cols) == 2, (
            f"row {row!r} of Ο is not two single-cell edges -- got {len(cols)} ink cells"
        )
        assert cols[1] - cols[0] > 1, (
            f"row {row!r} of Ο has adjacent ink -- a doubled-width stroke, not two edges"
        )
    # The caps (rows 0 and -1) are narrower than the body -- that
    # narrowing, not stroke thickness, is where the roundness lives.
    cap_width = rows[0].count("█")
    body_width = max(cols[-1] - cols[0] + 1 for row in sides for cols in [[i for i, c in enumerate(row) if c == "█"]])
    assert 0 < cap_width < body_width, "Ο's cap must be narrower than its body -- that is its roundness"
    assert rows[-1] == rows[0], "Ο must be symmetric top to bottom"


def test_drawn_lines_fit_the_width_they_are_given():
    """Three shapes, widest first. Nothing may overflow its column: art
    that wraps is mush, which is what this whole release is about."""
    import re

    def plain(line):
        return re.sub(r"\[/?(?:b )?#?[0-9A-Fa-f]{0,6}\]", "", line)

    full = banner.drawn_lines(banner.DRAWN_FULL_COLUMNS)
    assert len(full) == banner.FULL_ROWS
    joined = " ".join(plain(l) for l in full)
    for row in banner.GREEK_ROWS:
        assert row in joined, "the Greek word is missing from the full-width form"
    assert banner.TAGLINE in joined
    assert banner.WORDMARK not in plain(full[0]), (
        "the full-width form draws ΔΟΞΑ in blocks, not the Latin fallback"
    )

    mid = banner.drawn_lines(banner.DRAWN_MARK_COLUMNS)
    assert len(mid) == len(banner.MARK_ROWS)
    joined = " ".join(plain(l) for l in mid)
    assert banner.WORDMARK in joined and banner.TAGLINE not in joined
    assert banner.GREEK_ROWS[0] not in joined, (
        "the mid-width form must not draw the wider Greek glyphs"
    )

    tiny = banner.drawn_lines(banner.DRAWN_MARK_COLUMNS - 1)
    assert [plain(l) for l in tiny] == [banner.WORDMARK]

    for width in range(4, 90):
        for line in banner.drawn_lines(width):
            assert len(plain(line)) <= max(width, len(banner.WORDMARK)), (
                f"width {width}: {plain(line)!r} overflows"
            )


def test_full_rows_accounts_for_the_blank_and_tagline_rows():
    """FULL_ROWS is the triangle's own row count plus a blank separator
    plus the tagline row -- derived, not a second number to keep in sync
    by hand."""
    assert banner.FULL_ROWS == len(banner.MARK_ROWS) + 2
    full = banner.drawn_lines(banner.DRAWN_FULL_COLUMNS)
    assert len(full) == banner.FULL_ROWS
    assert full[len(banner.MARK_ROWS)] == "", "no blank separator before the tagline"
    assert banner.TAGLINE in full[-1]


def test_full_and_mark_columns_are_derived_from_the_art():
    """The two thresholds are arithmetic over the glyphs' own measured
    widths, not chosen freehand -- change a glyph and these move with it,
    same discipline the old ring-era constants held to."""
    assert banner.DRAWN_FULL_COLUMNS == (
        banner.MARK_COLUMNS + banner.MARK_GAP + banner.GREEK_COLUMNS
    )
    assert banner.DRAWN_MARK_COLUMNS == (
        banner.MARK_COLUMNS + banner.MARK_GAP + len(banner.WORDMARK)
    )
    assert banner.DRAWN_FULL_COLUMNS > banner.DRAWN_MARK_COLUMNS


def test_geometry_is_derived_from_the_row_budget():
    """47 cells is not a magic number -- it is 6 rows spent through the
    cell aspect and the INKED aspect of the asset, and the docstring's
    arithmetic has to be the code's. /img's showcase only, since v0.70.0
    -- the boot banner itself never spends this budget any more."""
    assert banner.COLUMNS == round(
        banner.ROWS * banner.CELL_ASPECT * banner.CONTENT_ASPECT
    )
    assert banner.COLUMNS == 47


def test_asset_is_the_readme_banner_and_is_on_disk():
    path = banner.asset_path()
    assert path is not None and path.is_file()
    assert path.name == "logo.png"


def test_content_aspect_still_describes_the_real_asset():
    """CONTENT_ASPECT is a constant so COLUMNS needs no import-time file
    read -- which only stays honest if something re-measures the file."""
    from PIL import Image

    box = Image.open(banner.asset_path()).convert("RGBA").getchannel("A").getbbox()
    measured = (box[2] - box[0]) / (box[3] - box[1])
    assert abs(measured - banner.CONTENT_ASPECT) / banner.CONTENT_ASPECT < 0.02


def test_prepared_image_is_cropped_and_opaque():
    """The two defects a screenshot caught: a transparent background that
    PIL's convert("RGB") turns into a white slab, and 26% of the rows
    spent on empty canvas."""
    prepared = banner.image_source()
    assert prepared is not None
    assert prepared.mode == "RGB", "alpha survived -- it will flatten to white"
    # Cropped to the ink, so no fully-empty border row/column remains.
    assert prepared.size == (928, 238)
    # Every corner is the theme base, not white.
    w, h = prepared.size
    for point in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        assert prepared.getpixel(point) == banner.BASE_COLOR


def test_each_caller_gets_its_own_image():
    """The showcase mounts several widgets over one asset; handing them a
    shared PIL object makes one widget's cleanup another's bug."""
    assert banner.image_source() is not banner.image_source()


def test_old_multi_value_settings_still_read_as_on(monkeypatch):
    """v0.70.0 collapsed boot_banner from a four-way choice
    (auto/blocks/image/off) to plain on/off -- the raster form auto and
    image used to reach for is gone, so there is no longer a distinction
    between them worth keeping. A config.toml written by the OLD settings
    modal still holds one of those words, and none of them may error or
    silently start meaning off: every spelling but a recognised OFF one
    reads as on, the same permissive rule the pre-collapse form() used to
    fall back to for a value it did not recognise at all."""
    for value in ("auto", "blocks", "image", "AUTO", " image ", "sideways"):
        monkeypatch.setenv("DOXA_BOOT_BANNER", value)
        assert banner.enabled() is True, f"{value!r} must still mean on"
    for value in ("off", "OFF", "0", "false", "no"):
        monkeypatch.setenv("DOXA_BOOT_BANNER", value)
        assert banner.enabled() is False, f"{value!r} must still mean off"


# -- the banner a user actually sees -----------------------------------


@pytest.mark.asyncio
async def test_every_tier_gets_the_drawn_mark_never_a_raster(tmp_path, monkeypatch):
    """v0.70.0: there is no raster tier for the banner any more, on any
    terminal. kgp/sixel used to earn a raster ``logo.png`` here
    (``use_image``'s old ``auto`` rule, now removed), and ``image`` could
    force it on every tier including half-block. Both are gone -- the
    drawn mark, VISIBLE (the v0.28.0 guard: mounted is not the claim), is
    what every terminal gets now, unconditionally on the image ladder."""
    monkeypatch.delenv("DOXA_BOOT_BANNER", raising=False)
    for mode in ("kgp", "sixel", "halfblock", "text"):
        _unforced(monkeypatch, mode)
        app = DoxaApp(cwd=str(tmp_path))
        async with app.run_test(size=WIDE) as pilot:
            await _settle(pilot)
            block = _banner(app)
            assert block is not None
            assert not block.query(".banner-image"), f"{mode} drew the raster"
            drawn = block.query_one(".banner-wordmark")
            # WIDE is comfortably past DRAWN_FULL_COLUMNS, so this is the
            # full-width form: triangle + GREEK_ROWS + tagline.
            assert drawn.region.height == banner.FULL_ROWS, (
                f"{mode}: banner mounted at zero rows -- invisible"
            )


@pytest.mark.asyncio
async def test_banner_sits_above_the_identity_block(tmp_path):
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        identity = app.query_one("#identity-block", SystemBlock)
        block = _banner(app)
        assert block is not None
        assert block.region.y < identity.region.y


@pytest.mark.asyncio
async def test_text_tier_shows_the_wordmark_and_never_the_fallback_line(tmp_path):
    """conftest forces the text tier suite-wide, so this is the default
    headless path. `[image: doxa logo]` as the first line of every session
    is the thing this feature must not ship."""
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        block = _banner(app)
        assert block is not None
        assert block.region.height > 0
        drawn = block.query_one(".banner-wordmark")
        assert drawn.region.height == banner.FULL_ROWS
        rendered = str(drawn.renderable)
        assert "[image:" not in rendered
        assert banner.TAGLINE in rendered
        # v0.72.0: the full-width form draws ΔΟΞΑ in blocks, not the plain
        # Latin WORDMARK -- that is now the mid-width degrade only (see
        # test_a_mid_width_terminal_shows_mark_and_wordmark_never_an_image).
        # Triangle and Greek word share one colour tag per row now (no
        # ring to keep apart from the triangle any more), so a bare row of
        # MARK_ROWS/GREEK_ROWS IS still a literal substring once markup is
        # stripped.
        plain = "\n".join(_plain_lines(drawn))
        for row in banner.MARK_ROWS:
            assert row in plain
        for row in banner.GREEK_ROWS:
            assert row in plain
        assert not block.query(".banner-image")


@pytest.mark.asyncio
async def test_a_mid_width_terminal_shows_mark_and_wordmark_never_an_image(
    tmp_path, monkeypatch
):
    """Between DRAWN_MARK_COLUMNS and DRAWN_FULL_COLUMNS: wide enough for
    the mark plus the wordmark, too narrow for the tagline too -- and,
    unconditionally now (v0.70.0), never the raster."""
    monkeypatch.setenv("DOXA_IMAGE_MODE", "halfblock")
    width = (banner.DRAWN_MARK_COLUMNS + banner.DRAWN_FULL_COLUMNS) // 2 + 4
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=(width, 24)) as pilot:
        await _settle(pilot)
        block = _banner(app)
        assert block is not None
        drawn = block.query_one(".banner-wordmark")
        assert drawn.region.height == len(banner.MARK_ROWS)
        rendered = str(drawn.renderable)
        assert banner.WORDMARK in rendered
        assert banner.TAGLINE not in rendered
        assert not block.query(".banner-image")


async def _identity_top(tmp_path) -> int:
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        return app.query_one("#identity-block", SystemBlock).region.y


@pytest.mark.asyncio
async def test_the_setting_genuinely_removes_it(tmp_path, monkeypatch):
    """Off is off: no widget, AND the rows it was costing come back --
    a setting that only hides something is a setting that still pays for
    it."""
    monkeypatch.setenv("DOXA_IMAGE_MODE", "halfblock")
    monkeypatch.setenv("DOXA_BOOT_BANNER", "1")
    with_banner = await _identity_top(tmp_path)

    monkeypatch.setenv("DOXA_BOOT_BANNER", "0")
    assert banner.enabled() is False
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        assert not app.query(BootBanner)
        identity = app.query_one("#identity-block", SystemBlock)
        block_list = app.query_one("#block-list")
        assert block_list.children[0] is identity
        assert identity.region.y < with_banner


def test_setting_defaults_on_and_has_a_settings_row(monkeypatch):
    """v0.70.0 collapsed this from a 4-way ``choice`` to a plain
    ``bool_on`` -- there is only one form to turn on or off now."""
    from doxa import config as config_mod

    monkeypatch.delenv("DOXA_BOOT_BANNER", raising=False)
    assert banner.enabled() is True
    row = config_mod.SETTINGS_BY_ENV["DOXA_BOOT_BANNER"]
    assert row.key == "boot_banner"
    assert row.kind == "bool_on"
    assert row.default == "1"
    assert row.category == "Appearance"


# -- the showcase ------------------------------------------------------


def test_diagnostics_report_the_real_detected_mode(monkeypatch):
    monkeypatch.setenv("DOXA_IMAGE_MODE", "sixel")
    rows = dict(images.diagnostics())
    assert rows["mode"] == "sixel — forced via DOXA_IMAGE_MODE"
    # A forced mode is not a measurement, and the report must not dress it
    # up as one.
    assert "not measured" in rows["kgp (kitty graphics)"]

    monkeypatch.delenv("DOXA_IMAGE_MODE", raising=False)
    monkeypatch.setattr(images, "_detected", "kgp")
    rows = dict(images.diagnostics())
    assert rows["mode"] == "kgp — probed"
    assert rows["kgp (kitty graphics)"].startswith("supported")
    # The ladder short-circuits, so sixel was never asked -- saying "not
    # supported" here would be an invention.
    assert "not probed" in rows["sixel"]

    monkeypatch.setattr(images, "_detected", "halfblock")
    rows = dict(images.diagnostics())
    assert rows["kgp (kitty graphics)"].startswith("not supported")
    assert rows["sixel"].startswith("not supported")

    # A settled "text" is silence, not a terminal that said no: the probe
    # short-circuits before writing a byte when stdout is not a tty, so
    # every rung below has to read as unmeasured.
    monkeypatch.setattr(images, "_detected", "text")
    rows = dict(images.diagnostics())
    assert "no answer" in rows["probe"]
    assert "not measured" in rows["kgp (kitty graphics)"]
    assert "not measured" in rows["sixel"]


def test_renderable_modes_never_include_an_unmeasured_tier(monkeypatch):
    monkeypatch.delenv("DOXA_IMAGE_MODE", raising=False)
    monkeypatch.setattr(images, "_detected", "kgp")
    assert images.renderable_modes() == ("kgp", "halfblock", "text")
    # sixel was never probed under kgp, so it is never drawn.
    monkeypatch.setattr(images, "_detected", "halfblock")
    assert images.renderable_modes() == ("halfblock", "text")
    monkeypatch.setattr(images, "_detected", "text")
    assert images.renderable_modes() == ("halfblock", "text")


def test_a_defaulted_cell_size_is_never_reported_as_measured(monkeypatch):
    """textual-image returns its VT340 constant indistinguishably from a
    real measurement when nothing answered. Reprinting that as though the
    terminal had said it is the exact kind of confident wrong answer this
    report exists to avoid."""
    monkeypatch.setattr(images, "_cell_size_settled", True)
    monkeypatch.setattr(images, "_cell_size", images.VT340_DEFAULT_CELL)
    assert "VT340 default" in dict(images.diagnostics())["cell size"]

    monkeypatch.setattr(images, "_cell_size", (9, 19))
    assert dict(images.diagnostics())["cell size"] == "9 × 19 px"


def test_cell_size_is_none_headless_and_settles_once(monkeypatch):
    """It must never write a byte into a captured stream, and must never
    ask twice."""
    monkeypatch.setattr(images, "_cell_size", None)
    monkeypatch.setattr(images, "_cell_size_settled", False)
    monkeypatch.setattr(images, "_is_tty", lambda: False)
    assert images.cell_size() is None
    calls = []
    monkeypatch.setattr(images, "_is_tty", lambda: calls.append(1) or True)
    assert images.cell_size() is None
    assert calls == [], "cell_size re-asked after it had settled"


@pytest.mark.asyncio
async def test_bare_img_renders_the_showcase_with_visible_samples(tmp_path, monkeypatch):
    monkeypatch.setenv("DOXA_IMAGE_MODE", "halfblock")
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=(120, 60)) as pilot:
        await _settle(pilot)
        app.query_one("#prompt-input").value = "/img"
        await pilot.press("enter")
        await _settle(pilot, 100)
        showcase = app.query_one(ImageShowcaseBlock)
        assert showcase.region.height > 0
        report = str(showcase.query_one(".image-diagnostics").renderable)
        assert "halfblock" in report
        assert "cell size" in report
        # One label per tier it may honestly draw, and no more.
        labels = [str(w.renderable) for w in showcase.query(".image-mode-label")]
        assert labels == ["── halfblock ──", "── text ──"]
        # And the half-block sample is a picture the user can see.
        sample = showcase.query(".banner-image")[0]
        assert sample.region.height > 0


@pytest.mark.asyncio
async def test_img_with_a_path_is_unchanged(tmp_path, monkeypatch):
    from doxa.app import ImageBlock

    monkeypatch.setenv("DOXA_IMAGE_MODE", "halfblock")
    target = tmp_path / "pic.png"
    target.write_bytes(banner.asset_path().read_bytes())
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        app.query_one("#prompt-input").value = f"/img {target}"
        await pilot.press("enter")
        await _settle(pilot, 100)
        assert app.query(ImageBlock)
        assert not app.query(ImageShowcaseBlock)


def test_img_is_registered_as_taking_an_optional_path():
    from doxa import commands as commands_mod

    row = next(c for c in commands_mod.REGISTRY if c.name == "/img")
    assert row.usage == "/img [path]"


# -- v0.58.0: "the logo image is not rendering" ------------------------
#
# The report was one sentence and the banner had no way to answer it,
# because every degrade path was correct AND silent -- which from the
# outside is indistinguishable from being wrong. These tests pin the two
# halves of the fix: the opening block no longer bets six blank rows on an
# escape payload nothing can confirm arrived, and when the logo does not
# draw it says so in place.


def _unforced(monkeypatch, detected: str) -> None:
    """Detection settled on `detected`, with nothing forcing it -- the
    state conftest's suite-wide DOXA_IMAGE_MODE=text otherwise prevents."""
    monkeypatch.delenv("DOXA_IMAGE_MODE", raising=False)
    monkeypatch.setattr(images, "_detected", detected)


def test_the_legacy_bool_spelling_still_means_what_it_meant(monkeypatch):
    """v0.41.0 shipped this knob as a bool. A config.toml written by that
    settings modal says 1 or 0 and must not start meaning something else,
    even after v0.70.0 collapsed the auto/blocks/image middle ground that
    sat between v0.41.0's bool and today."""
    monkeypatch.setenv("DOXA_BOOT_BANNER", "1")
    assert banner.enabled() is True
    monkeypatch.setenv("DOXA_BOOT_BANNER", "0")
    assert banner.enabled() is False
    # Anything unrecognised reads as on rather than off -- see
    # test_old_multi_value_settings_still_read_as_on for the values that
    # actually matter here (this module's own former choices).
    monkeypatch.setenv("DOXA_BOOT_BANNER", "sideways")
    assert banner.enabled() is True


@pytest.mark.asyncio
async def test_half_block_terminal_gets_the_wordmark_not_the_raster(
    tmp_path, monkeypatch
):
    """What the user actually sees on the terminal they filed from."""
    monkeypatch.delenv("DOXA_BOOT_BANNER", raising=False)
    _unforced(monkeypatch, "halfblock")
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        block = _banner(app)
        assert block is not None
        assert not block.query(".banner-image"), "raster drawn on half-block"
        drawn = block.query_one(".banner-wordmark")
        assert drawn.region.height > 0
        # WIDE is comfortably past DRAWN_FULL_COLUMNS: triangle + Greek
        # word + tagline, one colour tag per row now that there is no
        # ring to keep apart from the triangle -- markup-stripped text
        # still contains a bare MARK_ROWS/GREEK_ROWS row as a literal
        # substring, same pattern as
        # test_text_tier_shows_the_wordmark_and_never_the_fallback_line.
        plain = "\n".join(_plain_lines(drawn))
        assert banner.MARK_ROWS[1] in plain, "the drawn triangle is missing"
        assert banner.GREEK_ROWS[1] in plain, "the drawn Greek word is missing"


@pytest.mark.asyncio
async def test_the_drawn_banner_is_legible_at_eighty_columns(tmp_path, monkeypatch):
    """80 columns is where the user is. Legible means: the triangle, the
    Greek word and the strapline are all present, and no row overflows."""
    monkeypatch.delenv("DOXA_BOOT_BANNER", raising=False)
    _unforced(monkeypatch, "halfblock")
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=(80, 24)) as pilot:
        await _settle(pilot)
        block = _banner(app)
        drawn = block.query_one(".banner-wordmark")
        lines = _plain_lines(drawn)
        assert len(lines) == banner.FULL_ROWS
        for row, mark_row in zip(lines, banner.MARK_ROWS):
            assert row.startswith(mark_row), f"{row!r} is not the drawn triangle"
        joined = " ".join(lines)
        for row in banner.GREEK_ROWS:
            assert row in joined
        assert banner.TAGLINE in joined
        available = block.content_size.width
        assert all(len(line) <= available for line in lines)
        assert drawn.region.height == banner.FULL_ROWS


def _plain_lines(widget) -> "list[str]":
    """The rendered rows with the color markup taken back off."""
    import re

    text = re.sub(r"\[/?(?:b )?#?[0-9A-Fa-f]{0,6}\]", "", str(widget.renderable))
    return text.splitlines()


@pytest.mark.asyncio
async def test_narrow_terminal_never_overflows_the_glyph_art(tmp_path, monkeypatch):
    """Prose may wrap; art may not.

    Asserting HEIGHT cannot catch this, and did not: the old CSS pinned
    the wordmark at three rows, so content too wide for its column was
    CLIPPED to exactly the height a passing test expected. The invariant
    that actually holds the line is that no rendered row is wider than the
    column it goes into. Measured content widths (v0.72.0's triangle +
    ΔΟΞΑ geometry) are 8, 20, 28, 44, 68 and 108 cells for terminals of
    20, 30, 40, 56, 80 and 120 -- narrow enough to cross every tier
    boundary (:data:`DRAWN_MARK_COLUMNS` 22, :data:`DRAWN_FULL_COLUMNS`
    58) at least once."""
    _unforced(monkeypatch, "text")
    for width in (20, 30, 40, 56, 80, 120):
        app = DoxaApp(cwd=str(tmp_path))
        async with app.run_test(size=(width, 24)) as pilot:
            await _settle(pilot)
            block = _banner(app)
            drawn = block.query_one(".banner-wordmark")
            assert drawn.region.height > 0
            available = block.content_size.width
            for line in _plain_lines(drawn):
                assert len(line) <= max(available, len(banner.WORDMARK)), (
                    f"at {width} columns the drawn row {line!r} "
                    f"({len(line)} cells) overflows the {available} available"
                )


@pytest.mark.asyncio
async def test_a_terminal_too_narrow_for_the_glyphs_drops_to_the_name(
    tmp_path, monkeypatch
):
    _unforced(monkeypatch, "text")
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=(20, 24)) as pilot:
        await _settle(pilot)
        block = _banner(app)
        drawn = block.query_one(".banner-wordmark")
        assert drawn.region.height == 1
        assert banner.WORDMARK in str(drawn.renderable)
        # v0.70.0: there is no fallback-reason line left to stay silent --
        # the raster it explained was never given IS the thing that is
        # gone, so the class itself no longer exists anywhere in the DOM.
        assert not block.query(".banner-reason")


def test_the_showcase_path_never_raises_without_pillow(monkeypatch):
    """doxa.images.widget_for is documented never to raise, but the
    crop/flatten step is DOXA's own code on the near side of that
    guarantee -- and it runs inside ``ImageShowcaseBlock.compose`` (the
    only caller left since v0.70.0 retired the banner's own raster path),
    where an exception does not degrade a decoration, it breaks a debug
    command."""
    import builtins

    banner._prepared.cache_clear()
    real = builtins.__import__

    def no_pillow(name, *args, **kwargs):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("no Pillow in this install")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pillow)
    assert banner.image_source() is None
    banner._prepared.cache_clear()


def test_the_showcase_path_never_raises_without_an_asset(monkeypatch):
    banner._prepared.cache_clear()
    monkeypatch.setattr(banner, "asset_path", lambda: None)
    assert banner.image_source() is None
    banner._prepared.cache_clear()


# -- v0.58.0: the crash report ------------------------------------------
#
# "doxa crashed while using it with: ... TimeoutError: Timeout waiting for
# data", on Linux Mint's default terminal, in a session restoring a tab.
# Measured cause: textual-image probes cell size with ESC[16t, VTE never
# answers it, upstream CATCHES its own timeout and then reports it with
# logger.warning(..., exc_info=e) -- which, unconfigured, Python prints to
# stderr. In a full-screen app stderr IS the screen.


def test_the_image_library_can_never_write_on_doxas_screen():
    """A library's idea of a warning is a TUI's idea of corrupted output.
    Nothing from textual_image may reach a handler DOXA did not choose."""
    import logging

    logger = logging.getLogger("textual_image")
    assert logger.propagate is False, "records would reach the root handler"
    assert any(
        isinstance(h, logging.NullHandler) for h in logger.handlers
    ), "no handler of its own means Python's last resort, which is stderr"


def test_the_reported_warning_reaches_no_handler_of_ours():
    """The exact record the user's traceback came from, emitted for real,
    and asserted to reach nothing downstream.

    A spy on the ROOT logger rather than capsys: pytest installs its own
    root handler, so stderr is quiet during tests either way and asserting
    on it would pass with or without the fix."""
    import logging

    seen = []

    class Spy(logging.Handler):
        def emit(self, record):
            seen.append(record)

    root = logging.getLogger()
    spy = Spy()
    root.addHandler(spy)
    try:
        logging.getLogger("textual_image").warning(
            "Failed to get cell size via escape sequence, assuming VT340 sizes",
            exc_info=TimeoutError("Timeout waiting for data"),
        )
    finally:
        root.removeHandler(spy)
    assert seen == [], "the record still propagates to whatever root is doing"


def test_the_library_cache_is_always_seeded(monkeypatch):
    """Whatever happens to our probe, textual-image must never ask the
    terminal again: every later caller is a widget painting itself, after
    App.run(), where Textual owns stdin and the reply cannot arrive."""
    import textual_image._terminal as terminal_mod

    monkeypatch.delattr(terminal_mod.get_cell_size, "_result", raising=False)
    monkeypatch.setattr(images, "_cell_size", None)
    monkeypatch.setattr(images, "_cell_size_settled", False)

    def explode():
        raise ZeroDivisionError("terminal reported zero columns")

    monkeypatch.setattr(images, "_is_tty", lambda: True)
    monkeypatch.setattr(terminal_mod, "get_cell_size", explode)
    assert images.cell_size() is None
    # The object textual-image will call NEXT is the one that must be
    # primed -- whatever our probe did or failed to do.
    seeded = getattr(terminal_mod.get_cell_size, "_result", None)
    assert seeded is not None, "a later render would re-probe the terminal"
    assert tuple(seeded) == images.VT340_DEFAULT_CELL


def test_the_cache_is_seeded_even_with_no_terminal(monkeypatch):
    import textual_image._terminal as terminal_mod

    monkeypatch.delattr(terminal_mod.get_cell_size, "_result", raising=False)
    monkeypatch.setattr(images, "_cell_size", None)
    monkeypatch.setattr(images, "_cell_size_settled", False)
    monkeypatch.setattr(images, "_is_tty", lambda: False)
    assert images.cell_size() is None
    assert hasattr(terminal_mod.get_cell_size, "_result")


def test_widget_for_never_raises_while_measuring_or_painting(monkeypatch):
    """widget_for has always promised "never an exception". Until v0.58.0
    that covered construction only -- the easy half. These are the calls
    Textual makes later, from the compositor, with no caller left to catch
    anything."""
    from textual.geometry import Size

    monkeypatch.setenv("DOXA_IMAGE_MODE", "halfblock")
    widget = images.widget_for(banner.image_source(), "doxa logo", mode="halfblock")

    def explode(*_args, **_kwargs):
        raise ZeroDivisionError("terminal reported zero columns")

    monkeypatch.setattr("textual_image.widget._base.get_cell_size", explode)
    size = Size(80, 24)
    assert widget.get_content_width(size, size) > 0
    # 1, never 0: a widget measuring to zero rows is in the DOM, invisible
    # on screen, and passes every structural assertion (the v0.28.0 defect).
    assert widget.get_content_height(size, size, 40) == 1

    monkeypatch.setattr("textual_image.renderable.halfcell.get_cell_size", explode)
    from rich.console import Console
    import io

    console = Console(width=50, file=io.StringIO())
    painted = "".join(s.text for s in console.render(widget.render()))
    assert images.fallback_line("doxa logo") in painted


def test_the_guarded_widget_is_still_the_real_widget(monkeypatch):
    """The guard must not change what a render site sees, or the ladder
    tests above are asserting something that no longer ships."""
    from textual_image.widget import HalfcellImage, TGPImage

    monkeypatch.setenv("DOXA_IMAGE_MODE", "halfblock")
    widget = images.widget_for(banner.image_source(), "d", mode="halfblock")
    assert isinstance(widget, HalfcellImage)
    assert not isinstance(widget, TGPImage)
    assert type(widget).__name__ == "HalfcellImage"
    # One subclass per tier, not one per call.
    assert images._guarded(HalfcellImage) is images._guarded(HalfcellImage)


def test_no_real_width_draws_the_name_and_never_the_terminal_width():
    """The CI-only defect of v0.58.0/v0.59.0, made deterministic.

    v0.59.0's ``_lay_out`` fitted the art on ``on_mount``/``on_resize``
    against ``self.content_size.width or self.columns`` -- the fallback is
    the TERMINAL's width, wider than this widget's own content box, and a
    guess from it could build a row too wide for the column it goes into.
    v0.60.0 moved the fit into ``_DrawnMark.render`` (paint time, against
    this widget's OWN content_size, never ``columns``) because that guess
    was not the whole defect: even a correct fit computed only on resize
    could go stale when a scrollbar appeared later without a Resize
    message reaching this widget -- see ``_DrawnMark``'s docstring for the
    measurement. Testing at the ``_DrawnMark`` level rather than through
    ``BootBanner`` is what pins the RIGHT layer down: with no measured
    width, the mark must draw the bare name, which fits any column, no
    matter what a container around it happens to be."""
    from textual.geometry import Size

    class _Zero(_DrawnMark):
        @property
        def content_size(self) -> Size:  # type: ignore[override]
            return Size(0, 0)

    mark = _Zero("", classes="banner-wordmark")
    mark.render()
    drawn = _plain_lines(mark)
    assert drawn == [banner.WORDMARK], (
        "with no measured width the mark must draw the name, which fits "
        f"any column -- got {drawn!r}"
    )


def test_a_measured_width_is_used_verbatim():
    """The other half: once a real content width exists it is the number
    fitted against -- read straight from :attr:`content_size` at paint
    time, not a value some earlier resize handler cached."""
    from textual.geometry import Size

    width = banner.DRAWN_MARK_COLUMNS

    class _Sized(_DrawnMark):
        @property
        def content_size(self) -> Size:  # type: ignore[override]
            return Size(width, len(banner.MARK_ROWS))

    mark = _Sized("", classes="banner-wordmark")
    mark.render()
    rows = _plain_lines(mark)
    assert len(rows) > 1, f"{width} columns is wide enough for the mark"
    for line in rows:
        assert len(line) <= width


def test_a_scrollbar_appearing_after_first_layout_cannot_leave_a_stale_fit():
    """Pins the exact CI sequence down, deterministically -- no app, no
    asyncio scheduling to get lucky or unlucky on.

    Measured against the real app (drive DoxaApp at 30 columns, log every
    ``BootBanner._lay_out`` call against the transcript's
    ``VerticalScroll.show_vertical_scrollbar``): laying the banner out
    fits it against a 20-cell box -- no scrollbar yet. The identity block
    mounts right after it (``PaneRuntimeMixin._boot``), and once the
    transcript outgrows the 24-row viewport the scrollbar that appears
    narrows every child's content box by two, to 18 -- but a follow-up
    ``_lay_out``, the only thing that could re-fit a cached string, fired
    on just 1 of 3 runs. 20 was ``DRAWN_MARK_COLUMNS`` at the time (the
    ring-era mark; v0.72.0's triangle+ΔΟΞΑ geometry moved that constant,
    but the mechanism this test pins -- a fit computed for a wider box
    surviving unrefitted into a narrower one -- does not depend on which
    width that was), so a row fitted there and never refitted overflowed
    an 18-cell box by exactly the scrollbar's width -- the CI failure
    verbatim.

    Reproduced here by fitting once at 20, then narrowing the box to 18
    with NOTHING telling the widget to refit -- no resize, no second
    ``_lay_out`` call. Only the next paint (``render()``) is asked for
    anything, which is the same thing the compositor asks for, and it
    must still be correct."""
    from textual.geometry import Size

    class _Narrowing(_DrawnMark):
        width = 20

        @property
        def content_size(self) -> Size:  # type: ignore[override]
            return Size(self.width, 7)

    mark = _Narrowing("", classes="banner-wordmark")
    mark.render()  # first layout pass: the box is 20 wide, no scrollbar
    first = _plain_lines(mark)
    assert max(len(line) for line in first) <= 20, "sanity: 20 fits 20"

    mark.width = 18  # the scrollbar appears; nothing else is told
    mark.render()  # the next paint -- not a resize, not a second _lay_out
    for line in _plain_lines(mark):
        assert len(line) <= 18, (
            f"row {line!r} ({len(line)} cells) overflows the 18-cell box "
            "the scrollbar left behind"
        )
