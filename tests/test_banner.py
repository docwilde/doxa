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


def test_geometry_is_derived_from_the_row_budget():
    """47 cells is not a magic number -- it is 6 rows spent through the
    cell aspect and the INKED aspect of the asset, and the docstring's
    arithmetic has to be the code's."""
    assert banner.COLUMNS == round(
        banner.ROWS * banner.CELL_ASPECT * banner.CONTENT_ASPECT
    )
    assert banner.COLUMNS == 47
    # The wordmark has to fit where the image does not.
    assert banner.WORDMARK_COLUMNS < banner.MIN_COLUMNS


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


def test_use_image_degrades_on_the_text_tier_and_on_narrow_terminals():
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
        wordmark = block.query_one(".banner-wordmark")
        assert wordmark.region.height == 3
        rendered = str(wordmark.renderable)
        assert "[image:" not in rendered
        assert banner.TAGLINE in rendered
        for row in banner.WORDMARK_ROWS:
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
        assert block.query_one(".banner-wordmark").region.height == 3
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
    assert row.kind == "bool_on"
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
