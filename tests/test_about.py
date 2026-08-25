"""Item Z (/about): the surface a bug report is written from.

DOXA had no single place that said which DOXA this is. The version lived
in the session-start identity block (scrolled away by the second turn), the
SDK/Textual/Python versions lived nowhere at all, and the config path in
force was something a reporter had to work out from the docs. ``/about``
is that one place.

The geometry assertions here are not decoration. v0.28.0 shipped a modal
whose buttons were laid out at zero height for a full release --
``height: 1; padding-top: 1`` under Textual's border-box model spends the
whole declared row on padding -- because every test asserted the screen had
been PUSHED and none asserted anything was visible. So this file measures
rendered height, hit-tests the doors at their own centres, and reads the
text off the widgets that actually drew.
"""

from __future__ import annotations

import pytest

from doxa import commands as commands_mod
from doxa import version as version_mod
from doxa.app import AboutDialog, DoxaApp
from tests.fakes import FakeEngine


async def _app(monkeypatch, cwd):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(cwd) + "-rt")
    engines: list[FakeEngine] = []

    def make():
        engines.append(FakeEngine([]))
        return engines[-1]

    app = DoxaApp(
        cwd=str(cwd), engine_factory=make, new_session_factory=make,
        new_session_factory_at=lambda path: make(),
    )
    return app, engines


async def _wait_for(pilot, predicate, tries=200):
    for _ in range(tries):
        if predicate():
            return True
        await pilot.pause(0.02)
    return bool(predicate())


def _hit(app, widget):
    """The widget the SCREEN reports at this widget's own centre -- the hit
    test a mouse actually performs. A zero-height door passes every
    query_one() in the suite and fails this."""
    region = widget.region
    if not region.area:
        return None
    x = region.x + region.width // 2
    y = region.y + region.height // 2
    try:
        found, _region = app.screen.get_widget_at(x, y)
    except Exception:
        return None
    return found


# -- the rows themselves -------------------------------------------------


def test_about_rows_cover_everything_a_bug_report_has_to_state():
    labels = [label for label, _value in version_mod.about_rows()]
    for required in ("doxa", "python", "platform", "config", "repo", "licence"):
        assert required in labels, f"/about has no {required} row"
    values = dict(version_mod.about_rows())
    assert version_mod.resolve_version() in values["doxa"]
    assert "github.com/docwilde/doxa" in values["repo"]
    # Public repo, NONCOMMERCIAL licence -- stated where a would-be
    # contributor or vendor actually looks, not only in LICENSE.
    assert "Noncommercial" in values["licence"]
    # Dependencies: measured off the imported modules, not hardcoded.
    import textual

    assert values["textual"] == textual.__version__
    assert values["agent sdk"]


def test_about_says_when_an_update_is_available_only_once_someone_looked():
    """Three states, and the third is load-bearing. ``None`` -- nobody has
    run the boot check yet, or it failed the silent way it is designed to
    -- must print NOTHING, because "up to date" and "unchecked" are
    different claims."""
    assert "update available" in dict(
        version_mod.about_rows(update_available=True)
    )["doxa"]
    assert "update available" not in dict(
        version_mod.about_rows(update_available=False)
    )["doxa"]
    assert "update available" not in dict(
        version_mod.about_rows(update_available=None)
    )["doxa"]


def test_about_text_is_the_same_string_the_rows_describe():
    """The dialog's body and its copy door read one builder, so what lands
    in an issue is what the reporter was looking at."""
    text = version_mod.about_text()
    for label, value in version_mod.about_rows():
        assert label in text and value in text


def test_lore_version_degrades_to_nothing_rather_than_guessing(monkeypatch, tmp_path):
    """A machine with no ``lore_core`` at all and no plugin manifest to
    read gets None, and the row is left out -- never a plausible-looking
    constant.

    WHICH of the two carriers the version comes from (the package's own
    ``__version__``, since LORE 0.35.1; the plugin manifest for anything
    older) is tested in ``tests/test_lore_dependency.py``, next to the
    ``lore from`` row that names the source."""
    import sys

    monkeypatch.setitem(sys.modules, "lore_core", None)
    monkeypatch.setenv("DOXA_LORE_CORE_PATH", str(tmp_path))
    assert version_mod.lore_core_version() is None
    manifest = tmp_path / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"name": "lore", "version": "9.9.9"}', encoding="utf-8")
    assert version_mod.lore_core_version() == "9.9.9"


# -- the command reaches every surface -----------------------------------


def test_about_is_registered_and_therefore_reaches_help_and_the_palette():
    command = commands_mod.find("/about")
    assert command is not None, "/about is not in the registry"
    assert command.palette, "/about has no palette entry"
    assert not command.passthrough
    assert "/about" in commands_mod.interactive_names()
    assert "/about" in [c.name for c in commands_mod.matches("/ab")]
    # /help is GENERATED from the registry, so registration is what puts it
    # there -- assert the generated text, not the intention.
    from doxa.ui.labels import help_text

    assert "/about" in help_text()


# -- what is on screen ---------------------------------------------------


@pytest.mark.asyncio
async def test_slash_about_opens_a_modal_whose_text_is_actually_visible(
    monkeypatch, tmp_path,
):
    app, _engines = await _app(monkeypatch, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        pane = app.active_pane
        assert await _wait_for(pilot, lambda: pane.engine is not None)
        await pane._run_command("/about")
        assert await _wait_for(pilot, lambda: isinstance(app.screen, AboutDialog))
        await pilot.pause()

        body = app.screen.query_one("#about-body")
        assert body.size.height > 0, f"about body collapsed: {body.size}"
        assert body.size.width > 0, f"about body collapsed: {body.size}"
        rendered = str(body.renderable)
        assert version_mod.resolve_version() in rendered
        assert "python" in rendered and "licence" in rendered
        # And it is genuinely painted, not merely sized: the screen's own
        # hit test finds it where it says it is.
        assert _hit(app, body) is body


@pytest.mark.asyncio
async def test_about_buttons_have_real_height_and_are_hittable(
    monkeypatch, tmp_path,
):
    """The v0.28.0 defect, pre-empted on the new dialog rather than
    rediscovered on it."""
    app, _engines = await _app(monkeypatch, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.push_screen(AboutDialog())
        assert await _wait_for(pilot, lambda: isinstance(app.screen, AboutDialog))
        await pilot.pause()

        row = app.screen.query_one("#about-buttons")
        assert row.size.height > 0, f"button row collapsed: {row.size}"
        for wid in ("#about-copy", "#about-close"):
            button = app.screen.query_one(wid)
            assert button.size.height > 0, f"{wid} collapsed: {button.size}"
            assert button.size.width > 0, f"{wid} collapsed: {button.size}"
            assert _hit(app, button) is button, f"{wid} is not hittable"
        # Self-describing, the same rule the other two confirms follow.
        assert "c" in str(app.screen.query_one("#about-copy").renderable)
        assert "esc" in str(app.screen.query_one("#about-close").renderable)


@pytest.mark.asyncio
async def test_esc_closes_about(monkeypatch, tmp_path):
    app, _engines = await _app(monkeypatch, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.push_screen(AboutDialog())
        assert await _wait_for(pilot, lambda: isinstance(app.screen, AboutDialog))
        await pilot.press("escape")
        assert await _wait_for(
            pilot, lambda: not isinstance(app.screen, AboutDialog)
        )


@pytest.mark.asyncio
async def test_copy_puts_the_visible_text_on_the_clipboard(monkeypatch, tmp_path):
    """A bug report is pasted, not retyped. What the copy door hands over is
    the SAME string the body drew, byte for byte."""
    app, _engines = await _app(monkeypatch, tmp_path)
    copied: list[str] = []
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(
            type(app), "copy_to_clipboard", lambda self, text: copied.append(text)
        )
        app.push_screen(AboutDialog())
        assert await _wait_for(pilot, lambda: isinstance(app.screen, AboutDialog))
        await pilot.pause()
        body_text = str(app.screen.query_one("#about-body").renderable)
        await pilot.click("#about-copy")
        assert await _wait_for(pilot, lambda: bool(copied))
        assert copied[-1] == body_text
