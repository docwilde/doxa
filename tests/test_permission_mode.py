"""Permission mode (v0.42.0): the status chip, the Shift+Tab cycle, /mode.

The operator asked for "an indicator to switch from manual to auto mode …
add it to the status line and add a hotkey CTRL + Tab cycling through the
modes, and a command /mode for it". Three surfaces, one piece of session
state, and one thing that is not a preference at all but a safety
property: which tool calls still stop and ask.

Every test here asserts a USER-VISIBLE outcome rather than that a method
was reachable -- rendered geometry, a real hit test, the plain text of the
status row, a key pressed through the pilot with the prompt focused, the
list of modes the SDK seam actually received. That posture is v0.28.0's
lesson, written down in tests/test_status_chips.py: a dialog can pass
every ``query_one`` in the suite and still occupy no screen.

``test_cycling_can_never_reach_a_dangerous_mode`` is a SECURITY assertion
and is written like one: it does not check the three-step happy path, it
exhausts the reachable set from every possible starting point (including
states the UI is not supposed to be able to produce) and asserts a
forbidden set is disjoint from it.
"""

from __future__ import annotations

import asyncio

import pytest
from textual.content import Content

from doxa import config as config_mod
from doxa import engine as engine_mod
from doxa.app import ChipPicker, DoxaApp, PermissionModeConfirm, StatusBar, SystemBlock
from doxa.engine import SessionEngine
from doxa.ui.labels import mode_chip, mode_text
from tests.fakes import FakeEngine, factory_with_script


# -- harness (the same shape tests/test_status_chips.py uses) -------------


async def _app(monkeypatch, cwd, fake=None):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(cwd) + "-rt")
    engines: list[FakeEngine] = []

    def make():
        engines.append(fake or FakeEngine([]))
        return engines[-1]

    app = DoxaApp(cwd=str(cwd), engine_factory=make, new_session_factory=make)
    return app, engines


def _status_plain(app) -> str:
    return Content.from_markup(str(app.query_one("#status-bar").renderable)).plain


def _status_markup(app) -> str:
    return str(app.query_one("#status-bar").renderable)


def _offset_of(app, needle: str) -> tuple[int, int]:
    idx = _status_plain(app).index(needle)
    return (2 + idx, 0)


async def _wait_status(pilot, app, needle: str, tries=200) -> bool:
    for _ in range(tries):
        if needle in _status_plain(app):
            return True
        await pilot.pause(0.02)
    return needle in _status_plain(app)


async def _wait_for(pilot, predicate, tries=200):
    for _ in range(tries):
        if predicate():
            return True
        await pilot.pause(0.02)
    return bool(predicate())


def _system_texts(app) -> list[str]:
    return [b.text for b in app.query(SystemBlock) if b.id != "identity-block"]


def _hit(app, widget):
    """The widget the SCREEN reports at this widget's own centre -- the hit
    test a mouse actually performs. A zero-height button passes every
    query_one() in the suite and fails this one."""
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


# =======================================================================
# THE SECURITY ASSERTION
# =======================================================================


def test_cycling_can_never_reach_a_dangerous_mode():
    """The hotkey's reachable set is exactly CYCLE_MODES, from anywhere.

    Written as an exhaustive reachability proof rather than as a walk of
    the intended path, because the question is not "does the happy path
    work" but "is there ANY sequence of key presses that lands on
    bypassPermissions". Closure is computed from every mode in the SDK's
    vocabulary, plus None, plus strings no code path should ever produce
    -- if a future edit adds a branch that steps "onward" from a gated
    mode into the ring, this fails.

    ``bypassPermissions`` disables the approval gate outright; ``auto``
    replaces the human at it with a model classifier; ``dontAsk`` denies
    silently instead of asking. A key that a user taps to move between
    conveniences must not be able to arrive at any of them."""
    seeds = list(engine_mod.PERMISSION_MODES) + [
        None, "", "garbage", "Default", "BYPASSPERMISSIONS", "plan ",
    ]
    reachable: set[str] = set()
    frontier = list(seeds)
    while frontier:
        current = frontier.pop()
        nxt = engine_mod.next_cycle_mode(current)
        if nxt not in reachable:
            reachable.add(nxt)
            frontier.append(nxt)

    assert reachable == set(engine_mod.CYCLE_MODES)
    assert not (reachable & set(engine_mod.GATED_MODES))
    assert "bypassPermissions" not in reachable
    # And the same claim stated as the property the function must hold,
    # not as a fact about one computed set.
    for seed in seeds:
        assert engine_mod.next_cycle_mode(seed) in engine_mod.CYCLE_MODES


def test_cycle_walks_the_three_modes_in_order_and_wraps():
    assert engine_mod.next_cycle_mode("default") == "acceptEdits"
    assert engine_mod.next_cycle_mode("acceptEdits") == "plan"
    assert engine_mod.next_cycle_mode("plan") == "default"


def test_a_session_parked_on_a_gated_mode_cycles_home_not_onward():
    """One press from bypassPermissions goes to the SAFE default, not to
    the "next" element of some larger ring. A user reaching for a key to
    get out of a mode that asks nothing wants out, in one press."""
    # Named literally rather than looped over GATED_MODES: a test whose
    # body iterates a constant passes vacuously the moment that constant
    # is empty, which is exactly the shape of an implementation that
    # forgot to gate anything.
    assert engine_mod.next_cycle_mode("bypassPermissions") == "default"
    assert engine_mod.next_cycle_mode("auto") == "default"
    assert engine_mod.next_cycle_mode("dontAsk") == "default"
    assert set(engine_mod.GATED_MODES) == {"bypassPermissions", "auto", "dontAsk"}


def test_no_configured_value_can_persist_a_gated_mode(monkeypatch):
    """A settings file or an env var can seed the three cycle-safe modes
    and cannot seed the other three. A standing bypassPermissions would
    disarm the approval gate of every future session -- including ones
    opened in repositories the user has not read, and ones opened by
    somebody who never set it."""
    for mode in engine_mod.CYCLE_MODES:
        monkeypatch.setenv("DOXA_PERMISSION_MODE", mode)
        assert engine_mod.permission_mode_default() == mode
    for mode in list(engine_mod.GATED_MODES) + ["nonsense", " "]:
        monkeypatch.setenv("DOXA_PERMISSION_MODE", mode)
        assert engine_mod.permission_mode_default() == "default"


def test_the_settings_row_offers_only_the_cycle_safe_modes():
    """The modal is a surface too: it must not present a gated mode as a
    persistable choice."""
    row = next(s for s in config_mod.SETTINGS if s.key == "permission_mode")
    assert set(row.choices) == {"", *engine_mod.CYCLE_MODES}
    for mode in engine_mod.GATED_MODES:
        assert mode not in row.choices


# =======================================================================
# The mode actually reaches the SDK
# =======================================================================


@pytest.mark.asyncio
async def test_connect_asserts_the_permission_mode_as_an_option(monkeypatch):
    """The connect-time half: ClaudeAgentOptions.permission_mode carries
    what the session thinks it is in, so the chip and the CLI cannot
    disagree about it from the very first turn."""
    monkeypatch.setenv("DOXA_PERMISSION_MODE", "acceptEdits")
    factory, created = factory_with_script([])
    engine = SessionEngine(cwd=".", client_factory=factory)
    assert engine.permission_mode == "acceptEdits"
    await engine.start()
    assert created[0].options.permission_mode == "acceptEdits"


@pytest.mark.asyncio
async def test_set_permission_mode_issues_the_sdk_control_request(monkeypatch):
    """The live half, and the reason /mode is a real command rather than
    /effort's honest apology: ClaudeSDKClient.set_permission_mode is a
    control request, so the running session moves. Asserted at the SDK
    SEAM (the client's own call log), not at the engine attribute the
    engine sets on itself."""
    monkeypatch.delenv("DOXA_PERMISSION_MODE", raising=False)
    factory, created = factory_with_script([])
    engine = SessionEngine(cwd=".", client_factory=factory)
    await engine.start()
    assert created[0].permission_modes == []

    assert await engine.set_permission_mode("plan") == "plan"
    assert created[0].permission_modes == ["plan"]
    assert engine.permission_mode == "plan"

    await engine.set_permission_mode("bypassPermissions")
    assert created[0].permission_modes == ["plan", "bypassPermissions"]


@pytest.mark.asyncio
async def test_an_unknown_mode_never_reaches_the_sdk(monkeypatch):
    factory, created = factory_with_script([])
    engine = SessionEngine(cwd=".", client_factory=factory)
    await engine.start()
    with pytest.raises(RuntimeError):
        await engine.set_permission_mode("yolo")
    assert created[0].permission_modes == []


# =======================================================================
# The status chip
# =======================================================================


@pytest.mark.asyncio
async def test_chip_renders_with_real_height_and_shows_the_mode(
    monkeypatch, tmp_path,
):
    """The chip occupies screen and says which mode. Height is asserted
    directly, for v0.28.0's reason: a widget can satisfy every query in
    the suite and lay out at zero rows."""
    monkeypatch.delenv("DOXA_PERMISSION_MODE", raising=False)
    fake = FakeEngine([], permission_mode="acceptEdits")
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(140, 40)) as pilot:
        assert await _wait_status(pilot, app, "mode:acceptEdits")
        bar = app.query_one("#status-bar", StatusBar)
        assert bar.size.height > 0, f"status bar collapsed: {bar.size}"
        assert bar.size.width > 0
        assert _hit(app, bar) is bar


@pytest.mark.asyncio
async def test_chip_is_visibly_distinct_once_the_session_stops_asking(
    monkeypatch, tmp_path,
):
    """Three tiers, and the point is that a glance is enough: default
    carries no escalation color, acceptEdits/plan carry the amber that
    means "not the posture you started in", and a mode that stops asking
    carries the red the ctx chip uses for unrecoverable pressure AND a
    warning glyph, so the signal survives a monochrome terminal too."""
    from doxa.ui.labels import CTX_AMBER, CTX_RED, MODE_WARN_GLYPH

    assert CTX_RED not in mode_chip("default")
    assert CTX_AMBER not in mode_chip("default")
    assert MODE_WARN_GLYPH not in mode_text("default")

    for mode in ("acceptEdits", "plan"):
        assert CTX_AMBER in mode_chip(mode)
        assert MODE_WARN_GLYPH not in mode_text(mode)

    for mode in engine_mod.GATED_MODES:
        assert CTX_RED in mode_chip(mode), mode
        assert MODE_WARN_GLYPH in mode_text(mode), mode
        # …and in the SHORT form too: width is the last thing a warning
        # gives way to, never the first.
        assert MODE_WARN_GLYPH in mode_text(mode, short=True), mode

    fake = FakeEngine([], permission_mode="bypassPermissions")
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(140, 40)) as pilot:
        assert await _wait_status(pilot, app, "mode:bypassPermissions")
        assert MODE_WARN_GLYPH in _status_plain(app)
        assert CTX_RED in _status_markup(app)


@pytest.mark.asyncio
async def test_chip_tooltip_survives_the_colored_tiers(monkeypatch, tmp_path):
    """v0.35.0's defect, pinned on the new chip before it can repeat it:
    the tooltip is looked up by finding the chip's text inside the bar's
    markup-STRIPPED string, so a chip whose KEY still carried its color
    tags silently lost its hint at exactly the tier that mattered. Checked
    at the red tier, which is where that bug bit last time."""
    fake = FakeEngine([], permission_mode="bypassPermissions")
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(140, 40)) as pilot:
        assert await _wait_status(pilot, app, "mode:bypassPermissions")
        bar = app.query_one("#status-bar", StatusBar)
        x, _y = _offset_of(app, "mode:bypassPermissions")
        hint = bar._tooltip_for_x(x) or ""
        assert "permission mode" in hint.lower()
        assert "bypassPermissions" in hint
        assert "nothing asks you" in hint


@pytest.mark.asyncio
async def test_narrow_terminal_keeps_the_warning_and_drops_only_the_default(
    monkeypatch, tmp_path,
):
    """Graceful degradation, in the direction that matters. The status row
    does not truncate -- a chip that does not fit is simply gone -- so on a
    narrow terminal the chip yields when it would only have said
    "default", and shrinks rather than vanishing when it has something the
    user cannot learn anywhere else."""
    monkeypatch.delenv("DOXA_PERMISSION_MODE", raising=False)
    fake = FakeEngine([])
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(80, 24)) as pilot:
        assert await _wait_status(pilot, app, "beliefs")
        assert "mode:" not in _status_plain(app)

    danger = FakeEngine([], permission_mode="bypassPermissions")
    app2, _e2 = await _app(monkeypatch, tmp_path, danger)
    async with app2.run_test(size=(80, 24)) as pilot:
        assert await _wait_status(pilot, app2, "mode:bypass")
        plain = _status_plain(app2)
        assert "⚠" in plain
        # SHORT form: the full SDK spelling would cost 22 columns here.
        assert "mode:bypassPermissions" not in plain


def test_every_short_label_is_one_a_user_can_actually_reach():
    """No abbreviation exists for a mode that never renders short.

    `default` is the only mode a cramped row drops rather than shrinks, so
    a short form for it would be a label nobody can ever see — the same
    "present, documented, dead" shape the Ctrl+Tab measurement rejected.
    Every OTHER mode must have an entry, and none may be LONGER than the
    name it stands in for. Not strictly shorter: `plan` is already four
    characters and has no honest abbreviation, and inventing one to
    satisfy a test would be the tail wagging the chip."""
    from doxa.ui.labels import MODE_SHORT

    assert "default" not in MODE_SHORT
    assert set(MODE_SHORT) == set(engine_mod.PERMISSION_MODES) - {"default"}
    for mode, short in MODE_SHORT.items():
        assert len(mode_text(mode, short=True)) <= len(mode_text(mode)), mode
    # …and the one that actually has to fit does shrink, a lot.
    assert len(mode_text("bypassPermissions", short=True)) == 13
    assert len(mode_text("bypassPermissions")) == 24


@pytest.mark.asyncio
async def test_wide_terminal_shows_the_default_mode_too(monkeypatch, tmp_path):
    """With room on the row, DOXA says which mode unconditionally -- a
    permission mode is always in force, and `default` is a mode with
    behavior rather than the absence of one."""
    monkeypatch.delenv("DOXA_PERMISSION_MODE", raising=False)
    fake = FakeEngine([])
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(140, 40)) as pilot:
        assert await _wait_status(pilot, app, "mode:default")


@pytest.mark.asyncio
async def test_clicking_the_chip_opens_a_picker_of_all_six_modes(
    monkeypatch, tmp_path,
):
    """The gated three are LISTED, not hidden: a capability the CLI has
    and DOXA refuses to mention is how a user ends up hand-editing config
    files to reach it. The confirmation, not the concealment, is what
    makes reaching one deliberate."""
    fake = FakeEngine([])
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(140, 40)) as pilot:
        assert await _wait_status(pilot, app, "mode:default")
        picker = app.query_one("#chip-picker", ChipPicker)
        await pilot.click("#status-bar", offset=_offset_of(app, "mode:default"))
        assert await _wait_for(pilot, lambda: picker.is_open)
        listed = "\n".join(label for _rid, label in picker._rows)
        for mode in engine_mod.PERMISSION_MODES:
            assert mode in listed, mode


# =======================================================================
# The hotkey -- pressed for real, with the prompt focused
# =======================================================================


@pytest.mark.asyncio
async def test_shift_tab_reaches_the_handler_with_the_prompt_focused(
    monkeypatch, tmp_path,
):
    """The failure mode the Ctrl+Tab measurement was protecting against is
    a binding that tests green and does nothing in the terminal. This
    presses the key through the pilot's real key pipeline, with focus
    where it actually lives (PromptInput is a TextArea and holds it almost
    always), and asserts the SESSION moved -- not that an action method
    exists."""
    monkeypatch.delenv("DOXA_PERMISSION_MODE", raising=False)
    fake = FakeEngine([])
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(140, 40)) as pilot:
        assert await _wait_status(pilot, app, "mode:default")
        assert isinstance(app.focused, type(app.query_one("#prompt-input")))

        await pilot.press("shift+tab")
        assert await _wait_for(pilot, lambda: fake.permission_mode == "acceptEdits")
        assert await _wait_status(pilot, app, "mode:acceptEdits")

        await pilot.press("shift+tab")
        assert await _wait_for(pilot, lambda: fake.permission_mode == "plan")
        assert await _wait_status(pilot, app, "mode:plan")

        await pilot.press("shift+tab")
        assert await _wait_for(pilot, lambda: fake.permission_mode == "default")
        # Exactly the ring, in order, and nothing else touched the engine.
        assert fake.permission_mode_switches == ["acceptEdits", "plan", "default"]


@pytest.mark.asyncio
async def test_shift_tab_from_a_gated_mode_returns_to_default(
    monkeypatch, tmp_path,
):
    """The escape hatch, pressed for real: a session someone left in
    bypassPermissions comes home on one keystroke, with no confirmation in
    the way -- narrowing permissions never asks."""
    fake = FakeEngine([], permission_mode="bypassPermissions")
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(140, 40)) as pilot:
        assert await _wait_status(pilot, app, "mode:bypassPermissions")
        await pilot.press("shift+tab")
        assert await _wait_for(pilot, lambda: fake.permission_mode == "default")
        assert not isinstance(app.screen, PermissionModeConfirm)


@pytest.mark.asyncio
async def test_forward_tab_traversal_still_works(monkeypatch, tmp_path):
    """What taking Shift+Tab app-level COSTS, measured rather than
    assumed. Textual's Screen binds shift+tab to ``app.focus_previous``;
    binding it here with priority=True removes reverse traversal. Forward
    traversal is untouched and wraps, so no focusable widget becomes
    unreachable -- a user goes the long way round, they are not stranded.
    This test is the evidence for that claim."""
    fake = FakeEngine([])
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(140, 40)) as pilot:
        assert await _wait_status(pilot, app, "mode:default")
        start = app.focused
        seen = []
        for _ in range(12):
            await pilot.press("tab")
            await pilot.pause()
            seen.append(app.focused)
            if app.focused is start and len(seen) > 1:
                break
        assert any(widget is not start for widget in seen), (
            "Tab moved focus nowhere: forward traversal is broken"
        )
        assert start in seen, "Tab traversal never wrapped back around"


def test_both_keycaps_are_bound_and_the_unsendable_one_is_labelled(monkeypatch):
    """The binding decision, pinned. The operator asked for Ctrl+Tab;
    doxa.keyboard measures it unsendable under the legacy encoding, so
    Shift+Tab (deliverable everywhere -- back-tab, CSI Z) is primary and
    Ctrl+Tab rides beside it for kitty-protocol terminals. The second
    binding is only defensible because /help SAYS where it does not work,
    which is exactly what v0.39.0 built."""
    from doxa import keyboard as keyboard_mod
    from doxa.ui.labels import help_text

    assert keyboard_mod.unreachable_under_legacy("ctrl+tab") is True
    assert keyboard_mod.unreachable_under_legacy("shift+tab") is False

    keys = {
        binding.key: binding.action for binding in DoxaApp.BINDINGS
        if not isinstance(binding, tuple)
    }
    assert keys.get("shift+tab") == "cycle_permission_mode"
    assert keys.get("ctrl+tab") == "cycle_permission_mode"

    monkeypatch.setenv(keyboard_mod.ENV_VAR, keyboard_mod.LEGACY)
    text = help_text()
    assert "Ctrl+Tab✗" in text
    assert "Shift+Tab✗" not in text
    assert "cannot send that combination" in text


def test_the_bytes_a_terminal_actually_sends_decode_to_the_bound_key():
    """The leg a pilot keypress cannot reach: TERMINAL BYTES -> key name.

    ``pilot.press("shift+tab")`` proves the binding dispatches once Textual
    has already decided a key arrived. It says nothing about whether a
    terminal can produce that key in the first place, which is the entire
    Ctrl+Tab question. So this feeds Textual's own parser the real
    sequences:

      ``ESC [ Z``      back-tab, what a legacy terminal sends for Shift+Tab
      ``ESC [ 9;2u``   the same key under the kitty protocol
      ``ESC [ 9;5u``   Ctrl+Tab -- which exists ONLY in the CSI u form

    The third line is the measurement stated positively: Ctrl+Tab has a
    kitty encoding and no legacy one, which is exactly what
    ``unreachable_under_legacy('ctrl+tab')`` claims and why it is not the
    primary binding."""
    from textual import events
    from textual._xterm_parser import XTermParser

    def decode(sequence: str) -> list[str]:
        return [
            e.key for e in XTermParser().feed(sequence)
            if isinstance(e, events.Key)
        ]

    assert decode("\x1b[Z") == ["shift+tab"]
    assert decode("\x1b[9;2u") == ["shift+tab"]
    assert decode("\x1b[9;5u") == ["ctrl+tab"]


def test_mode_is_in_the_registry_and_reaches_help_and_the_palette():
    from doxa import commands as commands_mod

    row = commands_mod.find("/mode")
    assert row is not None
    assert row.binding == "shift+tab"
    assert row.palette
    assert "/mode" in commands_mod.interactive_names()


# =======================================================================
# /mode -- the command, and the confirmation
# =======================================================================


async def _run(pilot, app, text: str):
    pane = app.active_pane
    prompt = app.query_one("#prompt-input")
    prompt.value = text
    await pilot.press("enter")
    await pilot.pause()
    return pane


@pytest.mark.asyncio
async def test_bare_mode_shows_the_current_mode_and_the_choices(
    monkeypatch, tmp_path,
):
    monkeypatch.delenv("DOXA_PERMISSION_MODE", raising=False)
    fake = FakeEngine([], permission_mode="plan")
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(140, 40)) as pilot:
        assert await _wait_status(pilot, app, "mode:plan")
        await _run(pilot, app, "/mode")
        assert await _wait_for(
            pilot, lambda: any("mode: plan" in t for t in _system_texts(app))
        )
        text = next(t for t in _system_texts(app) if t.startswith("mode: plan"))
        for mode in engine_mod.PERMISSION_MODES:
            assert mode in text, mode
        assert "▸ plan" in text
        assert "Shift+Tab" in text
        assert "asks first" in text


@pytest.mark.asyncio
async def test_bare_mode_names_an_ignored_configured_value(monkeypatch, tmp_path):
    """A settings row that cannot take effect must not sit there silently
    looking like it did. This is the visible half of
    permission_mode_default()'s narrowing."""
    monkeypatch.setenv("DOXA_PERMISSION_MODE", "bypassPermissions")
    fake = FakeEngine([])
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(140, 40)) as pilot:
        assert await _wait_status(pilot, app, "mode:")
        await _run(pilot, app, "/mode")
        assert await _wait_for(
            pilot, lambda: any("IGNORED" in t for t in _system_texts(app))
        )
        text = next(t for t in _system_texts(app) if "IGNORED" in t)
        assert "bypassPermissions" in text


@pytest.mark.asyncio
async def test_mode_switches_a_safe_mode_without_asking(monkeypatch, tmp_path):
    monkeypatch.delenv("DOXA_PERMISSION_MODE", raising=False)
    fake = FakeEngine([])
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(140, 40)) as pilot:
        assert await _wait_status(pilot, app, "mode:default")
        await _run(pilot, app, "/mode acceptEdits")
        assert await _wait_for(pilot, lambda: fake.permission_mode == "acceptEdits")
        assert not isinstance(app.screen, PermissionModeConfirm)
        assert await _wait_status(pilot, app, "mode:acceptEdits")


@pytest.mark.asyncio
async def test_mode_bypass_requires_confirmation(monkeypatch, tmp_path):
    """The dialog appears, states what STOPS happening rather than asking
    "are you sure?", and nothing has reached the engine yet while it is
    up."""
    fake = FakeEngine([])
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(140, 40)) as pilot:
        assert await _wait_status(pilot, app, "mode:default")
        await _run(pilot, app, "/mode bypassPermissions")
        assert await _wait_for(
            pilot, lambda: isinstance(app.screen, PermissionModeConfirm)
        )
        await pilot.pause()

        body = str(app.screen.query_one("#mode-confirm-body").renderable)
        assert "unapproved" in body
        assert "nothing will ask" in body
        assert "this session only" in body.lower()
        # Nothing has been switched merely by asking.
        assert fake.permission_mode_switches == []


@pytest.mark.asyncio
async def test_declining_the_confirmation_changes_nothing(monkeypatch, tmp_path):
    """Esc: no control request, the engine's mode untouched, the status
    line where it was. The same nothing-happened contract the compact
    confirm's decline path keeps."""
    monkeypatch.delenv("DOXA_PERMISSION_MODE", raising=False)
    fake = FakeEngine([])
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(140, 40)) as pilot:
        assert await _wait_status(pilot, app, "mode:default")
        await _run(pilot, app, "/mode bypassPermissions")
        assert await _wait_for(
            pilot, lambda: isinstance(app.screen, PermissionModeConfirm)
        )
        await pilot.press("escape")
        assert await _wait_for(
            pilot, lambda: not isinstance(app.screen, PermissionModeConfirm)
        )
        assert fake.permission_mode_switches == []
        assert fake.permission_mode == "default"
        assert "mode:default" in _status_plain(app)
        assert any("unchanged" in t for t in _system_texts(app))


@pytest.mark.asyncio
async def test_enter_does_not_accept_the_permission_confirmation(
    monkeypatch, tmp_path,
):
    """Deliberately NOT CompactConfirm's Enter-accepts shape. There, Enter
    completes an action the user's own click already asked for; here the
    dialog is the last thing between a keystroke and an unattended agent,
    so the reflex key must not be the one that disarms the gate."""
    fake = FakeEngine([])
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(140, 40)) as pilot:
        assert await _wait_status(pilot, app, "mode:default")
        await _run(pilot, app, "/mode bypassPermissions")
        assert await _wait_for(
            pilot, lambda: isinstance(app.screen, PermissionModeConfirm)
        )
        await pilot.press("enter")
        assert await _wait_for(
            pilot, lambda: not isinstance(app.screen, PermissionModeConfirm)
        )
        assert fake.permission_mode_switches == []


@pytest.mark.asyncio
async def test_accepting_the_confirmation_switches_the_session(
    monkeypatch, tmp_path,
):
    fake = FakeEngine([])
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(140, 40)) as pilot:
        assert await _wait_status(pilot, app, "mode:default")
        await _run(pilot, app, "/mode bypassPermissions")
        assert await _wait_for(
            pilot, lambda: isinstance(app.screen, PermissionModeConfirm)
        )
        await pilot.press("y")
        assert await _wait_for(
            pilot, lambda: fake.permission_mode == "bypassPermissions"
        )
        assert fake.permission_mode_switches == ["bypassPermissions"]
        assert await _wait_status(pilot, app, "mode:bypassPermissions")


@pytest.mark.asyncio
async def test_confirm_buttons_have_real_height_and_are_hittable(
    monkeypatch, tmp_path,
):
    """v0.28.0's reported defect, pinned on the fourth member of this
    modal family before it can happen a fourth time: `height: 1` plus
    `padding-top: 1` under a border-box model leaves a zero-row content
    box and draws no buttons at all."""
    fake = FakeEngine([])
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(140, 40)) as pilot:
        assert await _wait_status(pilot, app, "mode:default")
        await _run(pilot, app, "/mode auto")
        assert await _wait_for(
            pilot, lambda: isinstance(app.screen, PermissionModeConfirm)
        )
        await pilot.pause()

        row = app.screen.query_one("#mode-confirm-buttons")
        yes = app.screen.query_one("#mode-confirm-yes")
        no = app.screen.query_one("#mode-confirm-no")
        assert row.size.height > 0, f"button row collapsed: {row.size}"
        for button in (yes, no):
            assert button.size.height > 0, f"{button.id} collapsed: {button.size}"
            assert button.size.width > 0, f"{button.id} collapsed: {button.size}"
            assert _hit(app, button) is button, f"{button.id} is not hittable"
        # Self-describing: each door names its own key.
        assert "y" in str(yes.renderable).lower()
        assert "esc" in str(no.renderable).lower()


@pytest.mark.asyncio
async def test_the_picker_routes_a_gated_choice_through_the_same_confirm(
    monkeypatch, tmp_path,
):
    """One switch path, three doors. The chip's picker does not carry its
    own copy of the confirmation rule -- it calls the same handler /mode
    does, which is where the rule lives."""
    fake = FakeEngine([])
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(140, 40)) as pilot:
        assert await _wait_status(pilot, app, "mode:default")
        pane = app.active_pane
        pane.run_worker(pane._cmd_mode("dontAsk"), group="command")
        assert await _wait_for(
            pilot, lambda: isinstance(app.screen, PermissionModeConfirm)
        )
        await pilot.press("escape")
        assert await _wait_for(
            pilot, lambda: not isinstance(app.screen, PermissionModeConfirm)
        )
        assert fake.permission_mode_switches == []


@pytest.mark.asyncio
async def test_an_unknown_mode_is_refused_in_words(monkeypatch, tmp_path):
    fake = FakeEngine([])
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(140, 40)) as pilot:
        assert await _wait_status(pilot, app, "mode:default")
        await _run(pilot, app, "/mode yolo")
        assert await _wait_for(
            pilot, lambda: any("unknown mode" in t for t in _system_texts(app))
        )
        assert fake.permission_mode_switches == []


@pytest.mark.asyncio
async def test_mode_never_writes_the_settings_file(monkeypatch, tmp_path):
    """The persist-or-reset answer, asserted. /model saves because a model
    is a preference; a permission mode is a posture adopted for a piece of
    work, and one Shift+Tab tap must not silently rewrite the default for
    every future session."""
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DOXA_PERMISSION_MODE", raising=False)
    saved: list[dict] = []
    monkeypatch.setattr(
        config_mod, "save", lambda values: saved.append(dict(values))
    )
    fake = FakeEngine([])
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(140, 40)) as pilot:
        assert await _wait_status(pilot, app, "mode:default")
        await _run(pilot, app, "/mode plan")
        assert await _wait_for(pilot, lambda: fake.permission_mode == "plan")
        assert saved == []


@pytest.mark.asyncio
async def test_a_new_session_starts_from_the_persisted_safe_default(monkeypatch):
    """Reset, not inherit. /clear and Ctrl+T build a fresh SessionEngine,
    which re-reads the config -- so a gated mode cannot be carried into a
    session the user did not choose it for."""
    monkeypatch.setenv("DOXA_PERMISSION_MODE", "acceptEdits")
    factory, _created = factory_with_script([])
    first = SessionEngine(cwd=".", client_factory=factory)
    await first.start()
    await first.set_permission_mode("bypassPermissions")
    assert first.permission_mode == "bypassPermissions"

    second = SessionEngine(cwd=".", client_factory=factory)
    assert second.permission_mode == "acceptEdits"


# =======================================================================
# Daemon parity -- a detached session, and a client that comes back
# =======================================================================


@pytest.mark.asyncio
async def test_daemon_owns_the_switch_and_every_client_learns_it(
    tmp_path, monkeypatch,
):
    """/mode over the socket: the daemon issues the control request (it
    holds the SDK client) and BROADCASTS the result, so a second tab on
    the same daemon cannot go on painting "default" for a session that
    stopped asking. That matters more here than model_changed's version of
    it does -- a status line that misreports a safety property is worse
    than one that misreports a model name."""
    from tests.test_daemon import _drain_oob, running_daemon
    from doxa.client import EngineClient

    monkeypatch.delenv("DOXA_PERMISSION_MODE", raising=False)
    async with running_daemon(tmp_path, monkeypatch) as (daemon, created, _):
        one = EngineClient(str(daemon.socket_path))
        await one.start()
        two = EngineClient(str(daemon.socket_path))
        await two.start()
        assert one.permission_mode == "default"
        assert two.permission_mode == "default"

        watcher = asyncio.create_task(
            _drain_oob(two, "permission_mode_changed")
        )
        await asyncio.sleep(0.05)
        assert await one.set_permission_mode("plan") == "plan"

        # The SDK seam actually received it, daemon-side.
        assert created[0].permission_modes == ["plan"]
        events = await watcher
        assert events[-1].data["mode"] == "plan"
        assert two.permission_mode == "plan"

        await one.finalize()
        await two.finalize()


@pytest.mark.asyncio
async def test_a_reattaching_client_sees_the_real_mode(tmp_path, monkeypatch):
    """The whole point of putting the mode in the daemon's status and hello
    frames: someone coming back to a session they left running has to be
    told what it is actually doing, before anything is painted. A fresh
    client that defaulted its chip to "default" over a session running in
    bypassPermissions would be lying to the person checking on it."""
    from tests.test_daemon import running_daemon
    from doxa.client import EngineClient

    monkeypatch.delenv("DOXA_PERMISSION_MODE", raising=False)
    async with running_daemon(tmp_path, monkeypatch) as (daemon, _created, _):
        first = EngineClient(str(daemon.socket_path))
        await first.start()
        await first.set_permission_mode("bypassPermissions")
        await first.finalize()

        # A NEW client, no shared state with the one above.
        fresh = EngineClient(str(daemon.socket_path))
        await fresh.start()
        assert fresh.permission_mode == "bypassPermissions"
        status = await fresh.refresh_status()
        assert status["permission_mode"] == "bypassPermissions"
        # And the chip it would paint says so, in red, with the glyph.
        assert "⚠" in mode_text(fresh.permission_mode)
        await fresh.finalize()


@pytest.mark.asyncio
async def test_the_hello_frame_carries_the_mode(tmp_path, monkeypatch):
    """Asserted on the RAW frame, because EngineClient.attach runs its
    first status refresh under contextlib.suppress -- a refresh that fails
    must still leave the chip showing the daemon's fact rather than the
    client's seeded guess."""
    from tests.test_daemon import running_daemon
    import json

    async with running_daemon(tmp_path, monkeypatch) as (daemon, _created, _):
        daemon.engine.permission_mode = "plan"
        reader, writer = await asyncio.open_unix_connection(str(daemon.socket_path))
        hello = json.loads(await asyncio.wait_for(reader.readline(), 5))
        assert hello["type"] == "hello"
        assert hello["permission_mode"] == "plan"
        writer.close()


@pytest.mark.asyncio
async def test_the_daemon_refuses_an_unknown_mode_over_the_socket(
    tmp_path, monkeypatch,
):
    """A socket is reachable by something that is not this TUI, so the
    daemon validates rather than forwarding whatever it is handed."""
    from tests.test_daemon import running_daemon
    from doxa.client import EngineClient, EngineClientError

    async with running_daemon(tmp_path, monkeypatch) as (daemon, created, _):
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        with pytest.raises(EngineClientError):
            await client.set_permission_mode("bypassEverything")
        assert created[0].permission_modes == []
        await client.finalize()
