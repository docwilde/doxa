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
from doxa.app import BootBanner, DoxaApp, ImageShowcaseBlock, SystemBlock

# Wide enough for banner.use_image's width test: the default run_test size
# (80x24) is over MIN_COLUMNS but leaves the 47-cell banner most of the
# line, so the pixel scenes ask for a realistic terminal instead.
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
    **"No, use the full block"**. One codepoint plus space.

    Every glyph excluded here was excluded for a reason that survives the
    font it was tested in, so this asserts the codepoints rather than the
    appearance:

    * ``▀``/``▄`` (U+2580/U+2584) -- *"do not use half-blocks / it leaves
      gaps"*: drawn against the font's baseline and leading, so a column
      of them seams instead of reading as one stroke.
    * ``◢``/``◣`` (U+25E2/U+25E3) -- Geometric Shapes, not Block Elements.
      A font can cover one and not the other, and the failure is tofu.
    """
    assert banner.MARK_ROWS
    used = set("".join(banner.MARK_ROWS))
    assert used <= {"█", " "}, (
        f"mark uses {sorted(f'U+{ord(c):04X}' for c in used)}; "
        "only U+2588 FULL BLOCK and space are allowed"
    )
    assert "█" in used, "a mark of pure whitespace is not a mark"
    for row in banner.MARK_ROWS:
        assert len(row) == banner.MARK_COLUMNS, "the mark must be rectangular"
    # The wordmark is PLAIN TEXT, not glyph art -- the user's own call.
    assert banner.WORDMARK == "DOXA"
    assert set(banner.WORDMARK).isdisjoint("▀▄█▌▐")


def test_the_mark_is_a_ring_around_a_triangle():
    """Shape, not just palette. The ring must be closed and the triangle
    must widen downward -- a regression that broke either would still pass
    the codepoint test above."""
    rows = banner.MARK_ROWS
    assert len(rows) == 7, "seven rows: the approved geometry"
    # Closed ring: every row has ink, and the outer edges bow in at the
    # poles rather than running straight down a rectangle.
    first_ink = [r.index("█") for r in rows]
    assert all(r.strip() for r in rows), "a gap in the ring"
    assert first_ink[0] > first_ink[3], "the top does not curve inward"
    assert first_ink[-1] > first_ink[3], "the bottom does not curve inward"
    # Triangle: apex a single cell, widening on each row below it.
    middles = [r[banner.MARK_COLUMNS // 2 - 3 : banner.MARK_COLUMNS // 2 + 4] for r in rows[2:5]]
    widths = [m.count("█") for m in middles]
    assert widths == sorted(widths) and widths[0] < widths[-1], (
        f"the inner triangle does not widen downward: {widths}"
    )


def test_drawn_lines_fit_the_width_they_are_given():
    """Three shapes, widest first. Nothing may overflow its column: art
    that wraps is mush, which is what this whole release is about."""
    import re

    def plain(line):
        return re.sub(r"\[/?(?:b )?#?[0-9A-Fa-f]{0,6}\]", "", line)

    full = banner.drawn_lines(banner.DRAWN_FULL_COLUMNS)
    assert len(full) == len(banner.MARK_ROWS)
    joined = " ".join(plain(l) for l in full)
    assert banner.WORDMARK in joined and banner.TAGLINE in joined

    mid = banner.drawn_lines(banner.DRAWN_MARK_COLUMNS)
    joined = " ".join(plain(l) for l in mid)
    assert banner.WORDMARK in joined and banner.TAGLINE not in joined

    tiny = banner.drawn_lines(banner.DRAWN_MARK_COLUMNS - 1)
    assert [plain(l) for l in tiny] == [banner.WORDMARK]

    for width in range(4, 90):
        for line in banner.drawn_lines(width):
            assert len(plain(line)) <= max(width, len(banner.WORDMARK)), (
                f"width {width}: {plain(line)!r} overflows"
            )


def test_geometry_is_derived_from_the_row_budget():
    """47 cells is not a magic number -- it is 6 rows spent through the
    cell aspect and the INKED aspect of the asset, and the docstring's
    arithmetic has to be the code's."""
    assert banner.COLUMNS == round(
        banner.ROWS * banner.CELL_ASPECT * banner.CONTENT_ASPECT
    )
    assert banner.COLUMNS == 47
    # The drawn form has to fit where the raster does not.
    assert banner.DRAWN_FULL_COLUMNS < banner.MIN_COLUMNS


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


def test_use_image_degrades_on_the_text_tier_and_on_narrow_terminals(monkeypatch):
    # Pinned to `image` so this stays a test of the two IMPOSSIBILITIES
    # (no pixels, no room) rather than of the auto rule, which
    # test_auto_draws_blocks_where_a_raster_would_only_be_a_downscale owns.
    monkeypatch.setenv("DOXA_BOOT_BANNER", "image")
    assert banner.use_image("kgp", 120) is True
    assert banner.use_image("halfblock", 120) is True
    # No pixels at any width...
    assert banner.use_image("text", 200) is False
    # ...and pixels with nowhere to put them.
    assert banner.use_image("kgp", banner.MIN_COLUMNS - 1) is False


# -- the banner a user actually sees -----------------------------------


@pytest.mark.asyncio
async def test_banner_has_non_zero_height_on_a_pixel_tier(tmp_path, monkeypatch):
    """The v0.28.0 guard: mounted is not the claim, VISIBLE is. Half-block
    is the tier every terminal can draw, so it is the one that must hold up
    headlessly."""
    monkeypatch.setenv("DOXA_IMAGE_MODE", "halfblock")
    monkeypatch.setenv("DOXA_BOOT_BANNER", "image")
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        block = _banner(app)
        assert block is not None
        assert block.region.height > 0
        assert block.region.width > 0
        image = block.query_one(".banner-image")
        assert image.region.height > 0, "banner mounted at zero rows -- invisible"
        # The row budget is a promise, not a hope.
        assert image.region.height <= banner.ROWS + 3
        assert image.region.width == banner.COLUMNS


@pytest.mark.asyncio
async def test_banner_sits_above_the_identity_block(tmp_path, monkeypatch):
    monkeypatch.setenv("DOXA_IMAGE_MODE", "halfblock")
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
        assert drawn.region.height == len(banner.MARK_ROWS)
        rendered = str(drawn.renderable)
        assert "[image:" not in rendered
        assert banner.TAGLINE in rendered
        assert banner.WORDMARK in rendered
        for row in banner.MARK_ROWS:
            assert row in rendered
        assert not block.query(".banner-image")


@pytest.mark.asyncio
async def test_narrow_terminal_degrades_to_the_wordmark(tmp_path, monkeypatch):
    monkeypatch.setenv("DOXA_IMAGE_MODE", "halfblock")
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=(banner.MIN_COLUMNS - 6, 24)) as pilot:
        await _settle(pilot)
        block = _banner(app)
        assert block is not None
        assert block.query_one(".banner-wordmark").region.height == len(banner.MARK_ROWS)
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
    from doxa import config as config_mod

    monkeypatch.delenv("DOXA_BOOT_BANNER", raising=False)
    assert banner.enabled() is True
    row = config_mod.SETTINGS_BY_ENV["DOXA_BOOT_BANNER"]
    assert row.key == "boot_banner"
    assert row.kind == "choice"
    assert row.default == "auto"
    assert set(banner.FORMS) <= set(row.choices)
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


def test_auto_draws_blocks_where_a_raster_would_only_be_a_downscale(monkeypatch):
    """The v0.58.0 rule, from a user looking at a half-block render and
    calling it "quite pixelated". Six rows of half-block is twelve
    vertical samples for a 238-row image; a drawn glyph wins there."""
    monkeypatch.delenv("DOXA_BOOT_BANNER", raising=False)
    assert banner.form() == "auto"
    for tier in ("kgp", "sixel"):
        assert banner.use_image(tier, 120) is True, f"{tier} carries real pixels"
    for tier in ("halfblock", "text"):
        assert banner.use_image(tier, 120) is False, f"{tier} is a downscale"


def test_the_form_setting_overrides_the_rule_both_ways(monkeypatch):
    monkeypatch.setenv("DOXA_BOOT_BANNER", "blocks")
    assert banner.form() == "blocks"
    assert banner.use_image("kgp", 120) is False, "blocks must never raster"

    monkeypatch.setenv("DOXA_BOOT_BANNER", "image")
    assert banner.form() == "image"
    assert banner.use_image("halfblock", 120) is True, "image is v0.41.0's look"
    # ...but the two genuine impossibilities still hold.
    assert banner.use_image("text", 120) is False
    assert banner.use_image("halfblock", banner.MIN_COLUMNS - 1) is False

    monkeypatch.setenv("DOXA_BOOT_BANNER", "off")
    assert banner.enabled() is False


def test_the_legacy_bool_spelling_still_means_what_it_meant(monkeypatch):
    """v0.41.0 shipped this knob as a bool. A config.toml written by that
    settings modal says 1 or 0 and must not start meaning something else."""
    monkeypatch.setenv("DOXA_BOOT_BANNER", "1")
    assert banner.form() == "auto" and banner.enabled() is True
    monkeypatch.setenv("DOXA_BOOT_BANNER", "0")
    assert banner.form() == "off" and banner.enabled() is False
    # Anything unrecognised falls back to the rule rather than to nothing.
    monkeypatch.setenv("DOXA_BOOT_BANNER", "sideways")
    assert banner.form() == "auto"


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
        rendered = str(drawn.renderable)
        assert banner.MARK_ROWS[1] in rendered, "the drawn mark is missing"
        assert banner.WORDMARK in rendered, "the plain wordmark is missing"


@pytest.mark.asyncio
async def test_the_drawn_banner_is_legible_at_eighty_columns(tmp_path, monkeypatch):
    """80 columns is where the user is. Legible means: the whole mark, the
    wordmark and the strapline are all present, and no row overflows."""
    monkeypatch.delenv("DOXA_BOOT_BANNER", raising=False)
    _unforced(monkeypatch, "halfblock")
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=(80, 24)) as pilot:
        await _settle(pilot)
        block = _banner(app)
        drawn = block.query_one(".banner-wordmark")
        lines = _plain_lines(drawn)
        assert len(lines) == len(banner.MARK_ROWS)
        for row, mark_row in zip(lines, banner.MARK_ROWS):
            assert row.startswith(mark_row), f"{row!r} is not the drawn mark"
        joined = " ".join(lines)
        assert banner.WORDMARK in joined
        assert banner.TAGLINE in joined
        available = block.content_size.width
        assert all(len(line) <= available for line in lines)
        assert drawn.region.height == len(banner.MARK_ROWS)


@pytest.mark.asyncio
async def test_a_pixel_tier_still_gets_the_raster(tmp_path, monkeypatch):
    """kgp/sixel carry a real bitmap, so the logo keeps earning its place."""
    monkeypatch.delenv("DOXA_BOOT_BANNER", raising=False)
    monkeypatch.setenv("DOXA_IMAGE_MODE", "kgp")
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        image = _banner(app).query_one(".banner-image")
        assert image.region.height > 0


def test_fallback_reason_speaks_only_when_the_raster_was_asked_for(monkeypatch):
    """Under `auto` the wordmark is the INTENDED output, so announcing
    "logo not drawn" over it would be noise on most sessions."""
    monkeypatch.delenv("DOXA_BOOT_BANNER", raising=False)
    assert banner.fallback_reason("halfblock", 120) == ""
    assert banner.fallback_reason("text", 120) == ""
    assert banner.fallback_reason("kgp", 120) == ""

    monkeypatch.setenv("DOXA_BOOT_BANNER", "blocks")
    assert banner.fallback_reason("kgp", 120) == ""

    # Pinned to the raster and unable to deliver it: now it must explain.
    monkeypatch.setenv("DOXA_BOOT_BANNER", "image")
    narrow = banner.fallback_reason("halfblock", 50)
    assert "50 columns" in narrow and str(banner.MIN_COLUMNS) in narrow
    assert "/img" in narrow
    no_pixels = banner.fallback_reason("text", 120)
    assert "no pixel mode" in no_pixels and "/img" in no_pixels


@pytest.mark.asyncio
async def test_a_degraded_banner_says_why_on_screen(tmp_path, monkeypatch):
    """When the raster WAS asked for and could not be drawn, the banner
    says so in place rather than leaving the user to discover /img."""
    monkeypatch.setenv("DOXA_BOOT_BANNER", "image")
    _unforced(monkeypatch, "text")
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        block = _banner(app)
        reason = block.query_one(".banner-reason")
        assert reason.region.height > 0, "the explanation mounted invisible"
        rendered = str(reason.renderable)
        assert "logo not drawn" in rendered
        assert "/img" in rendered


@pytest.mark.asyncio
async def test_a_drawn_logo_is_told_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("DOXA_BOOT_BANNER", "image")
    monkeypatch.setenv("DOXA_IMAGE_MODE", "halfblock")
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        assert not _banner(app).query(".banner-reason")


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
    column it goes into. Measured content widths are 8, 20, 30, 44 and 110
    cells for terminals of 20, 30, 40, 56 and 120."""
    _unforced(monkeypatch, "text")
    for width in (20, 30, 40, 56, 120):
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
        # The reason is prose and would have wrapped to seven rows here.
        assert not any(w.display for w in block.query(".banner-reason"))


def test_the_banner_path_never_raises_without_pillow(monkeypatch):
    """doxa.images.widget_for is documented never to raise, but the
    crop/flatten step is DOXA's own code on the near side of that
    guarantee -- and it runs inside BootBanner.compose, where an exception
    does not degrade a decoration, it takes the pane boot with it."""
    import builtins

    banner._prepared.cache_clear()
    real = builtins.__import__

    def no_pillow(name, *args, **kwargs):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("no Pillow in this install")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pillow)
    assert banner.use_image("halfblock", 120) is False
    assert banner.image_source() is None
    reason = banner.fallback_reason("halfblock", 120)
    assert "could not be decoded" in reason
    banner._prepared.cache_clear()


def test_the_banner_path_never_raises_without_an_asset(monkeypatch):
    banner._prepared.cache_clear()
    monkeypatch.setattr(banner, "asset_path", lambda: None)
    assert banner.use_image("halfblock", 120) is False
    assert "missing from this install" in banner.fallback_reason("halfblock", 120)
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
    """The CI-only defect of v0.58.0, made deterministic.

    ``_lay_out`` used ``self.content_size.width or self.columns``. The
    fallback is the TERMINAL's width; this widget's content box is
    narrower by chrome whose size is not a constant (v0.55.0 measured the
    scrollbar alone moving it by two). At 30 columns the guess built a
    20-cell row into an 18-cell box -- and nothing corrected it, because
    no resize follows a widget whose own size never changed.

    A local run passed and three CI jobs did not, which is the signature
    of a fallback that happens to be right on one machine.
    """
    block = BootBanner(columns=30)
    block._drawn = _Recorder()
    block._lay_out()
    drawn = block._drawn.text
    assert drawn == banner.WORDMARK, (
        "with no measured width the banner must draw the name, which fits "
        f"any column -- got {drawn!r}"
    )
    for line in drawn.splitlines():
        assert len(line) <= len(banner.WORDMARK)


def test_a_measured_width_is_used_verbatim():
    """The other half: once a real content width exists it is the number
    fitted against, not the terminal's."""
    from textual.geometry import Size

    class _Sized(BootBanner):
        @property
        def content_size(self) -> Size:  # type: ignore[override]
            return Size(20, 7)

    block = _Sized(columns=120)
    block._drawn = _Recorder()
    block._lay_out()
    rows = block._drawn.text.splitlines()
    assert len(rows) > 1, "20 columns is wide enough for the mark"
    for line in rows:
        assert len(line) <= 20


class _Recorder:
    """A stand-in for the Static the banner writes into, so the fit can be
    read without mounting an app -- the mounted path is covered by
    test_narrow_terminal_never_overflows_the_glyph_art above, and this one
    is about the branch that runs BEFORE a layout exists."""

    def __init__(self) -> None:
        self.text = ""

    def update(self, text: str) -> None:
        import re

        self.text = re.sub(r"\[[^]]*\]", "", text)
