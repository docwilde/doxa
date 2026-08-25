# SPDX-License-Identifier: AGPL-3.0-only
"""background (v0.29.0): the setting that stops DOXA painting its own
base -- $doxa-base in theme.tcss, resolved by DoxaApp.get_theme_variable_
defaults -- so an already-transparent terminal shows through.

What this file pins down, in order:

  1. default is opaque, and opaque is BYTE-IDENTICAL to every release
     before it -- #171512 on the base, no ansi_color.
  2. "transparent" flips the base (and only the base) to the real ANSI
     "default background" color, and turns on App.ansi_color -- required
     for that color to reach the terminal as ESC[49m instead of being
     rewritten into an approximated opaque RGB by Textual's own
     ANSIToTruecolor filter (see doxa/app.py's _apply_background).
  3. the surface ramp's OTHER rungs (status bar, tool-calls section,
     ToolChip, the settings/picker/palette panels) stay their own literal,
     opaque hex regardless -- role tints keep reading as distinct steps,
     and every popup keeps an opaque backdrop.
  4. the five modal washes keep their literal `#171512 60%` unconditionally
     -- Rich's "default" color has no RGB for Textual to blend a
     percentage against, so ansi_default+alpha would silently drop the
     dimming veil.
  5. an invalid/garbage value falls back to opaque without crashing, same
     as every other `choice` knob.
  6. the settings modal round-trip flips it live, no restart -- the same
     "saving re-reads the affected surfaces immediately" contract the
     clock chip already has (see test_chrome.py's clock-save test).
  7. the escape sequence that actually leaves the widget, byte for byte --
     the only check that proves the terminal ever sees anything different.
  8. the two surfaces this branch had to be reconciled against while it sat
     unmerged for four releases: v0.25.0's reasoning fold (a base-tinted
     surface that did not exist when the branch was cut) and v0.28.0's
     `height: auto` confirm-button rows (a hand-reported defect fix in the
     same file this feature rewrites).
  9. the setting survives v0.23.0's session-restore launch path.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console
from textual.geometry import Region

from doxa import config
from doxa.app import DoxaApp
from doxa.settings import SettingsScreen, field_id
from tests.fakes import FakeEngine

ESC = "\x1b"
DEFAULT_BG = f"{ESC}[49m"          # SGR "default background" -- the reset
PAINTED_BASE = f"{ESC}[48;2;23;21;18m"  # the literal #171512 DOXA has always painted


def _rendered_bytes(widget) -> str:
    """The widget's styled, FILTERED lines, run out through a real
    truecolor Rich console -- i.e. the bytes a terminal would actually
    receive.

    ``Widget.render_lines`` (not ``render_line``, which is content only) is
    the path that reaches ``StylesCache.render_widget`` with
    ``filters=widget.app._filters``, and that is where only filters whose
    ``.enabled`` is True get applied (textual/_styles_cache.py). That
    distinction is the whole point of measuring here: ``App.ansi_color``
    does nothing to the CSS -- it flips ``ANSIToTruecolor.enabled``, and
    only this path can see the difference."""
    widget._styles_cache.clear()
    segments = [
        segment
        for strip in widget.render_lines(Region(0, 0, widget.size.width, widget.size.height))
        for segment in strip
    ]
    buffer = io.StringIO()
    console = Console(
        file=buffer, force_terminal=True, color_system="truecolor", width=300
    )
    console._buffer.extend(segments)
    console._check_buffer()
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.delenv("DOXA_BACKGROUND", raising=False)
    config.invalidate()
    yield
    config.invalidate()


def _app(tmp_path) -> DoxaApp:
    return DoxaApp(cwd=str(tmp_path), engine_factory=lambda: FakeEngine([]))


# -- doxa.config.background_mode() -----------------------------------------


def test_background_mode_defaults_to_opaque():
    assert config.background_mode() == "opaque"


def test_background_mode_reads_the_env(monkeypatch):
    monkeypatch.setenv("DOXA_BACKGROUND", "transparent")
    assert config.background_mode() == "transparent"


def test_background_mode_reads_the_config_file(monkeypatch):
    monkeypatch.delenv("DOXA_BACKGROUND", raising=False)
    config.save({"background": "transparent"})
    assert config.background_mode() == "transparent"


def test_background_mode_falls_back_to_opaque_on_garbage(monkeypatch):
    """A hand-edited config, an old build's leftover value, a typo'd env
    var -- none of them may crash the app; all of them mean "opaque"."""
    monkeypatch.setenv("DOXA_BACKGROUND", "translucent")
    assert config.background_mode() == "opaque"


def test_invalid_value_is_not_stored_by_the_modal():
    """_coerce's choices gate, exercised through config.save the same way
    the settings modal calls it."""
    config.save({"background": "translucent"})
    assert "background" not in config.load()


# -- default rendering is unchanged (pin it) --------------------------------


@pytest.mark.asyncio
async def test_default_is_opaque_and_byte_identical_to_todays_look(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.ansi_color is False
        screen = app.screen
        assert str(screen.styles.background) == "Color(23, 21, 18)"
        assert screen.styles.background.ansi is None
        block_list = app.query_one("#block-list")
        assert block_list.styles.background == screen.styles.background


@pytest.mark.asyncio
async def test_explicit_opaque_matches_the_default(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_BACKGROUND", "opaque")
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.ansi_color is False
        assert str(app.screen.styles.background) == "Color(23, 21, 18)"


# -- transparent flips the base, and only the base --------------------------


@pytest.mark.asyncio
async def test_transparent_paints_nothing_on_the_base(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_BACKGROUND", "transparent")
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.ansi_color is True  # required for ansi_default to reach the terminal
        screen = app.screen
        assert screen.styles.background.ansi == -1
        # Rich's actual "default background" marker -- the ESC[49m proof.
        assert screen.styles.background.rich_color.name == "default"
        block_list = app.query_one("#block-list")
        assert block_list.styles.background.ansi == -1


@pytest.mark.asyncio
async def test_transparent_still_disables_the_ansi_truecolor_filter_correctly(
    monkeypatch, tmp_path
):
    """The finding that makes this feature work at all: ansi_color=False
    (the default) would have Textual's own ANSIToTruecolor filter rewrite
    ansi_default into an approximated OPAQUE rgb -- the opposite of what
    "transparent" promises. Confirmed at the filter level, not just the
    reactive's value."""
    from textual.filter import ANSIToTruecolor

    monkeypatch.setenv("DOXA_BACKGROUND", "transparent")
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        truecolor_filters = [
            f for f in app._filters if isinstance(f, ANSIToTruecolor)
        ]
        assert truecolor_filters
        assert truecolor_filters[0].enabled is False


@pytest.mark.asyncio
async def test_role_tints_still_differ_from_each_other_in_transparent_mode(
    monkeypatch, tmp_path
):
    """The v0.13.0 restyle's whole point -- background tint carries role --
    must survive: status bar, the tool-calls dimmer step and a raised chip
    stay their OWN literal, opaque colors, distinct from the now-transparent
    base and from each other."""
    from doxa.app import ToolCallsSection, ToolChip, TurnBlock
    from textual.containers import VerticalScroll

    monkeypatch.setenv("DOXA_BACKGROUND", "transparent")
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        block_list = app.active_pane.query_one("#block-list", VerticalScroll)
        block = TurnBlock("hi")
        await block_list.mount(block)
        chip = ToolChip("t1", "grep", {"pattern": "x"})
        await block.add_tool_chip(chip)

        base_bg = app.screen.styles.background
        status_bg = app.query_one("#status-bar").styles.background
        tools_bg = app.query_one(ToolCallsSection).styles.background
        chip_bg = chip.styles.background

        # Base is transparent...
        assert base_bg.ansi == -1
        # ...but every role step is its own PAINTED, opaque hex, and no
        # two of them collide.
        for tint in (status_bg, tools_bg, chip_bg):
            assert tint.ansi is None
            assert tint.a == 1
        assert len({str(status_bg), str(tools_bg), str(chip_bg)}) == 3


@pytest.mark.asyncio
async def test_modal_washes_stay_painted_regardless_of_background_mode(
    monkeypatch, tmp_path
):
    """ansi_default ignores alpha entirely (Textual's CSS parser keeps the
    ansi marker and DISCARDS the percentage: `background: ansi_default 60%`
    parses to Color(0, 0, 0, ansi=-1), whose rich_color is still plain
    "default") -- pairing it with a percentage would silently drop the
    dimming veil. The five scrims keep their literal #171512 60% on
    purpose, transparent mode or not."""
    monkeypatch.setenv("DOXA_BACKGROUND", "transparent")
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+comma")
        for _ in range(100):
            if isinstance(app.screen, SettingsScreen):
                break
            await pilot.pause(0.02)
        wash = app.screen.styles.background
        assert wash.ansi is None
        assert 0 < wash.a < 1  # still a genuine, blendable alpha wash
        assert (wash.r, wash.g, wash.b) == (23, 21, 18)


@pytest.mark.asyncio
async def test_popups_keep_an_opaque_backdrop_in_transparent_mode(
    monkeypatch, tmp_path
):
    """A floating dropdown over a transparent body still needs its own
    solid backing -- checked on the settings panel itself (behind its own
    wash) and the live slash-autocomplete dropdown (no wash behind it at
    all, mounted directly over the transparent transcript)."""
    monkeypatch.setenv("DOXA_BACKGROUND", "transparent")
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()

        await pilot.press("ctrl+comma")
        for _ in range(100):
            if isinstance(app.screen, SettingsScreen):
                break
            await pilot.pause(0.02)
        panel = app.screen.query_one("#settings-panel")
        assert panel.styles.background.ansi is None
        assert panel.styles.background.a == 1
        await pilot.press("escape")
        for _ in range(100):
            if not isinstance(app.screen, SettingsScreen):
                break
            await pilot.pause(0.02)

        await pilot.press("slash")
        dropdown = app.query_one("#slash-complete")
        assert dropdown.styles.background.ansi is None
        assert dropdown.styles.background.a == 1
        await pilot.press("escape")


# -- the settings modal flips it live ----------------------------------------


@pytest.mark.asyncio
async def test_settings_save_flips_background_live(monkeypatch, tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.ansi_color is False
        await pilot.press("ctrl+comma")
        for _ in range(100):
            if isinstance(app.screen, SettingsScreen):
                break
            await pilot.pause(0.02)
        screen = app.screen
        screen.query_one(f"#{field_id('background')}").value = "transparent"
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.press("escape")
        for _ in range(100):
            if not isinstance(app.screen, SettingsScreen):
                break
            await pilot.pause(0.02)
        assert app.ansi_color is True
        assert app.screen.styles.background.ansi == -1
    config.invalidate()


@pytest.mark.asyncio
async def test_settings_row_carries_the_terminal_side_caveat(monkeypatch, tmp_path):
    """The honesty requirement, checked where a user actually reads it:
    the modal's own help/note text must say DOXA only stops painting --
    making the terminal itself see-through is the terminal's job."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+comma")
        for _ in range(100):
            if isinstance(app.screen, SettingsScreen):
                break
            await pilot.pause(0.02)
        notes = " ".join(
            str(n.renderable) for n in app.screen.query(".setting-note")
        )
        assert "terminal" in notes.lower()
        assert "background_opacity" in notes


# -- the escape sequence that actually leaves the widget ---------------------


@pytest.mark.asyncio
async def test_opaque_paints_the_literal_base_rgb_at_the_byte_level(tmp_path):
    """The default's half of the byte-level proof: every rendered line of a
    base-tinted widget carries the explicit truecolor SGR for #171512, and
    never the "default background" reset. This is what "byte-identical to
    every release before it" means, measured rather than asserted."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        block_list = app.query_one("#block-list")
        out = _rendered_bytes(block_list)
        assert out.count(PAINTED_BASE) >= block_list.size.height
        assert DEFAULT_BG not in out


@pytest.mark.asyncio
async def test_transparent_emits_the_terminal_default_background_escape(
    monkeypatch, tmp_path
):
    """The whole feature, end to end and at the only layer that can settle
    it: what bytes reach the terminal. In transparent mode every rendered
    line of the base surface carries ESC[49m -- the SGR "default
    background" reset, which paints nothing and lets an already-transparent
    terminal show through -- and carries no RGB paint at all.

    Both halves are load-bearing here and this test fails if either is
    missing: drop `$doxa-base`'s ansi_default and the RGB comes back; drop
    App.ansi_color = True and Textual's ANSIToTruecolor filter (still
    enabled, still in App._filters) rewrites the ansi color into an
    approximated OPAQUE rgb before it ever reaches this console."""
    monkeypatch.setenv("DOXA_BACKGROUND", "transparent")
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        block_list = app.query_one("#block-list")
        out = _rendered_bytes(block_list)
        assert out.count(DEFAULT_BG) >= block_list.size.height
        assert PAINTED_BASE not in out


# -- reconciliation with what landed while this branch sat unmerged ---------


@pytest.mark.asyncio
async def test_the_reasoning_fold_follows_the_base_not_a_stale_literal(
    monkeypatch, tmp_path
):
    """v0.25.0's `.turn-reasoning` was added to theme.tcss AFTER this branch
    was cut, hardcoding the same #171512 the branch had just replaced
    everywhere else. Left alone it would paint an opaque patch across the
    reasoning fold while the turn body around it went transparent -- a
    visible seam, not a subtle one. It reads $doxa-base like every other
    base-level surface, so it tracks the setting."""
    from doxa.app import TurnBlock
    from textual.containers import VerticalScroll

    monkeypatch.setenv("DOXA_BACKGROUND", "transparent")
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        block_list = app.active_pane.query_one("#block-list", VerticalScroll)
        block = TurnBlock("hi")
        await block_list.mount(block)
        await block.append_reasoning("thinking about it")
        await pilot.pause()
        fold = block.query_one(".turn-reasoning")
        assert fold.styles.background.ansi == -1
        assert fold.styles.background == app.screen.styles.background


@pytest.mark.asyncio
async def test_confirm_button_rows_keep_v0_28_0s_real_height_in_transparent_mode(
    monkeypatch, tmp_path
):
    """v0.28.0 fixed a hand-reported defect in this exact file: the confirm
    dialogs' `#compact-confirm-buttons` / `#close-confirm-buttons` spent
    their whole declared `height: 1` on padding under Textual's border-box
    model, laying the buttons out at zero rows and drawing nothing. This
    branch rewrites theme.tcss underneath that fix, so the geometry is
    re-checked HERE, under the new setting, rather than trusted to the
    merge: `height: auto` still resolves to real rows, in the mode most
    likely to disturb it."""
    from doxa.app import CloseWithTurnRunning, CompactConfirm

    monkeypatch.setenv("DOXA_BACKGROUND", "transparent")
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        for screen, row_id in (
            (CompactConfirm(42.0), "#compact-confirm-buttons"),
            (CloseWithTurnRunning(), "#close-confirm-buttons"),
        ):
            screen_cls = type(screen)
            app.push_screen(screen)
            for _ in range(100):
                if isinstance(app.screen, screen_cls):
                    break
                await pilot.pause(0.02)
            await pilot.pause()
            row = app.screen.query_one(row_id)
            assert row.size.height > 0, f"{row_id} collapsed: {row.size}"
            for button in row.children:
                assert button.size.height > 0, f"{button.id} collapsed in {row_id}"
                assert button.size.width > 0, f"{button.id} collapsed in {row_id}"
            app.pop_screen()
            await pilot.pause()


@pytest.mark.asyncio
async def test_background_applies_on_the_v0_23_0_restore_path(monkeypatch, tmp_path):
    """Tab restore (v0.23.0) boots the app down a different compose branch
    -- N panes mounted from RestoreTabSpecs instead of the single fresh
    session -- and landed after this branch was cut. _apply_background runs
    off on_mount, before either branch, so the setting has to hold for a
    restored window too: checked on the restored pane's own transcript, not
    just the Screen."""
    from doxa.app import RestoreTabSpec
    from textual.containers import VerticalScroll

    def _factory(session_id):
        def make():
            engine = FakeEngine([])
            engine.session_id = session_id
            return engine

        return make

    where = tmp_path / "scratch"
    where.mkdir()
    monkeypatch.setenv("DOXA_BACKGROUND", "transparent")
    app = DoxaApp(
        cwd=str(where),
        restore_tabs=[
            RestoreTabSpec("sid-1", _factory("sid-1"), pinned_name="alpha"),
            RestoreTabSpec("sid-2", _factory("sid-2"), pinned_name=None),
        ],
        restore_active_id="sid-2",
        restore_report="tab restore: restored 2 tabs.",
    )
    async with app.run_test() as pilot:
        for _ in range(100):
            panes = app.panes()
            if len(panes) == 2 and all(p._session_id for p in panes):
                break
            await pilot.pause(0.02)
        await pilot.pause()
        assert [p._session_id for p in app.panes()] == ["sid-1", "sid-2"]
        assert app.ansi_color is True
        assert app.screen.styles.background.ansi == -1
        for pane in app.panes():
            block_list = pane.query_one("#block-list", VerticalScroll)
            assert block_list.styles.background.ansi == -1
