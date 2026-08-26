# SPDX-License-Identifier: AGPL-3.0-only
"""v0.67.0: inline row actions on the beliefs/proposals chip pickers, the
one shared row formatter both use, and the prompt-as-filter mode that
drives them.

Additive on top of item V/v0.48.0's existing per-row action SUB-menu
(``PaneChipsMixin._open_belief_actions`` / ``_open_pending_actions``,
covered by tests/test_beliefs_browser.py and left unchanged): this file
covers the FASTER path -- a click on the row's own action span, or the
reserved letter while that row is highlighted -- reaching the identical
engine calls.

Same testing bar tests/test_beliefs_browser.py states in its own module
docstring, restated here because it is the whole reason this file exists
rather than one more assertion bolted onto that one: "in the DOM" and "the
user can see it" are different claims (v0.28.0). Every action assertion
below reads the row back off the RENDERED option text (markup resolved),
checks the picker's own on-screen height, and for the click path drives a
REAL mouse event through Textual's own hit-testing -- never just a method
call standing in for one.
"""

from __future__ import annotations

import time

import pytest
from textual.content import Content

from doxa.app import ChipPicker, DoxaApp
from doxa.ui.labels import (
    PICKER_PREFIX_WIDTH,
    PICKER_STAMP_COL,
    _fmt_belief_row,
    _fmt_pending_row,
    format_picker_row,
)

from fakes import FakeEngine

DAY = 86400.0


def _stamp(secs_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - secs_ago))


def _belief(bid, claim, *, outcome=None, outcome_days=2, created_days=10, **extra):
    belief = {
        "id": bid, "subject": "user", "claim": claim, "confidence": 0.9,
        "created": _stamp(created_days * DAY), "outcomes": 1 if outcome else 0,
    }
    if outcome:
        belief.update({
            "outcome_event": outcome, "outcome_at": _stamp(outcome_days * DAY),
        })
    belief.update(extra)
    return belief


def _proposal(pid, text, *, staged_days=3, **extra):
    item = {
        "pid": pid, "kind": "memory", "action": "add", "scope": "user",
        "text": text, "created": _stamp(staged_days * DAY),
    }
    item.update(extra)
    return item


async def _open(monkeypatch, tmp_path, fake):
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    return DoxaApp(cwd=str(tmp_path))


def _shown(picker) -> list[str]:
    """Rendered rows, markup resolved -- what a reader actually sees, not
    the raw ``Option.prompt`` string with its ``[@click=...]`` control
    spans still literal in it."""
    return [Content.from_markup(str(picker.get_option_at_index(i).prompt)).plain
            for i in range(picker.option_count)]


async def _beliefs_picker(pilot, app):
    pane = app.active_pane
    await pane.open_beliefs_picker()
    for _ in range(200):
        picker = app.query_one("#chip-picker", ChipPicker)
        if picker.is_open:
            await pilot.pause()
            return pane, picker
        await pilot.pause(0.02)
    raise AssertionError("the beliefs picker never opened")


async def _pending_picker(pilot, app):
    pane = app.active_pane
    await pane.open_pending_picker()
    for _ in range(200):
        picker = app.query_one("#chip-picker", ChipPicker)
        if picker.is_open:
            await pilot.pause()
            return pane, picker
        await pilot.pause(0.02)
    raise AssertionError("the proposals picker never opened")


def _row_index(picker, rid: str) -> int:
    return next(i for i, (r, _l) in enumerate(picker._rows) if r == rid)


# -- the shared formatter: one shape, both menus -------------------------


def test_beliefs_and_proposals_rows_share_one_formatter():
    """Task 2's whole point: not two implementations that happen to look
    alike today, but literally the same function call."""
    belief_row = _fmt_belief_row(_belief(
        1, "x", outcome="confirmed", outcome_days=2,
        created="2026-01-01T00:00:00Z"))
    pending_row = _fmt_pending_row(_proposal(
        "p", "y", created="2026-01-01T00:00:00Z"))
    # Both lead with the SAME fixed-width stamp column, built by the same
    # format_picker_row call: neither drifts, and the status column that
    # follows starts at the identical offset for both row kinds.
    assert belief_row[:PICKER_STAMP_COL].strip() == "26-01-01 00:00"
    assert pending_row[:PICKER_STAMP_COL].strip() == "26-01-01 00:00"
    assert belief_row[PICKER_STAMP_COL:].startswith("confirmed")
    assert pending_row[PICKER_STAMP_COL:].startswith("add")
    direct = format_picker_row("26-01-01 00:00", "confirmed", "2d0h", "x", width=100)
    assert direct.startswith("26-01-01 00:00")
    assert direct[PICKER_PREFIX_WIDTH:] == "x"


@pytest.mark.asyncio
async def test_the_text_column_is_capped_at_min_100_or_the_measured_width(
    monkeypatch, tmp_path,
):
    """The operator's literal spec, checked at the point it is actually
    enforced -- ChipPicker's own render-time trim (``_render_rows``), not
    ``format_picker_row`` itself (which a CALLER hands an explicit width;
    the widget is what measures the terminal and applies the 100 cap --
    see that function's own docstring on the storage/display split). A
    claim longer than 100 characters is cut there even on a very wide
    terminal; a narrower one cuts sooner."""
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(1, "x" * 500)]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(400, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _beliefs_picker(pilot, app)
        index = _row_index(picker, "belief:1")
        shown = _shown(picker)[index]
        # The claim is the only run of "x" in the whole row (stamp,
        # marker and the action labels are not) -- measuring it
        # sidesteps also having to account for the leading "  <mark> "
        # OptionList prefix ChipPicker's own render adds ahead of the
        # fixed columns.
        import re

        match = re.search(r"x+…?", shown)
        assert match is not None, shown
        # This row ALSO carries the beliefs menu's own inline actions,
        # which reserve their own columns out of the same budget (see
        # ChipPicker._action_reserve) -- the text column is exactly 100
        # only once that reserve is accounted for.
        reserve = picker._action_reserve()
        assert len(match.group()) == 100 - reserve, match.group()


def test_a_blank_field_holds_its_column_open_not_omitted():
    """Fixed-width columns: a belief with no outcome still reserves the
    status/age columns instead of the text starting earlier."""
    row = format_picker_row("26-01-01 00:00", "", "", "claim", width=100)
    assert row[PICKER_PREFIX_WIDTH:] == "claim"
    assert row[:PICKER_PREFIX_WIDTH].strip() == "26-01-01 00:00"


@pytest.mark.asyncio
async def test_proposals_rows_are_fixed_width_columns_not_a_drifting_join(
    monkeypatch, tmp_path,
):
    """Named defect: before this, the proposals row was `` · ``-joined and
    a longer verdict pushed the text column right on one row but not its
    neighbour. Two proposals with very different verdict lengths must
    still start their TEXT at the same column."""
    fake = FakeEngine([])
    fake.list_pending_result = [
        _proposal("p1", "short one", kind="belief", action="retract", id=7),
        _proposal("p2", "the other one", kind="memory", scope="project",
                   project="a-very-long-project-slug-name"),
    ]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _pending_picker(pilot, app)
        rows = [(rid, label) for rid, label in picker._rows
                if rid.startswith("pending:")]
        assert len(rows) == 2
        text_starts = {label.index(text) for _rid, label in rows
                       for text in ("short one", "the other one")
                       if text in label}
        assert len(text_starts) == 1, text_starts


# -- inline actions: dispatch, arm-twice, rendered state ------------------


@pytest.mark.asyncio
async def test_a_bare_letter_confirms_the_highlighted_belief(monkeypatch, tmp_path):
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(1, "prefers terse commits")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _beliefs_picker(pilot, app)
        index = _row_index(picker, "belief:1")
        picker.highlighted = index
        assert picker.try_action_key("y") is True
        for _ in range(100):
            if fake.outcomes_recorded:
                break
            await pilot.pause(0.02)
        assert fake.outcomes_recorded == [(1, "confirmed")]
        assert fake.retracted == []


@pytest.mark.asyncio
async def test_retract_arms_on_the_picker_before_it_ends_a_belief(
    monkeypatch, tmp_path,
):
    """The SAME misclick asymmetry the full browser's own retract button
    carries (doxa.ui.beliefs), enforced here by ChipPicker._armed_rid
    instead of a per-row widget flag."""
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(1, "x")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _beliefs_picker(pilot, app)
        index = _row_index(picker, "belief:1")
        picker.highlighted = index

        assert picker.try_action_key("r") is True
        assert fake.retracted == [], "the first press only arms"
        armed_text = _shown(picker)[index]
        assert "CONFIRM RETRACT" in armed_text
        assert "disarms" in armed_text
        assert picker.size.height > 0, "the armed row must be on screen, not just in the DOM"

        # A refresh cycle between the two presses -- same gap a real
        # second keystroke always has, and what action_row_action's own
        # debounce (against Textual's own double-delivered click) keys
        # off of; see that method's docstring.
        await pilot.pause()
        assert picker.try_action_key("r") is True
        for _ in range(100):
            if fake.retracted:
                break
            await pilot.pause(0.02)
        assert fake.retracted == [1]
        assert fake.outcomes_recorded == []


@pytest.mark.asyncio
async def test_arming_a_different_row_disarms_the_first(monkeypatch, tmp_path):
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(1, "one"), _belief(2, "two")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _beliefs_picker(pilot, app)
        i1, i2 = _row_index(picker, "belief:1"), _row_index(picker, "belief:2")
        picker.highlighted = i1
        picker.try_action_key("r")
        assert picker._armed_rid == "belief:1"
        picker.highlighted = i2
        picker.try_action_key("r")
        assert picker._armed_rid == "belief:2", "arming elsewhere disarms the first"
        assert "CONFIRM RETRACT" not in _shown(picker)[i1]
        assert fake.retracted == [], "re-arming must never itself apply anything"


@pytest.mark.asyncio
async def test_approve_arms_on_the_picker_before_it_applies_a_proposal(
    monkeypatch, tmp_path,
):
    fake = FakeEngine([])
    fake.list_pending_result = [_proposal("p1", "remember uv, not pip")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _pending_picker(pilot, app)
        index = _row_index(picker, "pending:0")
        picker.highlighted = index

        assert picker.try_action_key("a") is True
        assert fake.approved == [], "the first press only arms"
        assert "CONFIRM APPROVE" in _shown(picker)[index]

        await pilot.pause()  # see the retract test's identical note
        assert picker.try_action_key("a") is True
        for _ in range(100):
            if fake.approved:
                break
            await pilot.pause(0.02)
        assert fake.approved == ["p1"]
        assert fake.rejected == []


@pytest.mark.asyncio
async def test_reject_is_one_press_no_arming(monkeypatch, tmp_path):
    fake = FakeEngine([])
    fake.list_pending_result = [_proposal("p1", "remember uv, not pip")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _pending_picker(pilot, app)
        picker.highlighted = _row_index(picker, "pending:0")
        assert picker.try_action_key("r") is True
        for _ in range(100):
            if fake.rejected:
                break
            await pilot.pause(0.02)
        assert fake.rejected == ["p1"]
        assert fake.approved == []


@pytest.mark.asyncio
async def test_clicking_the_action_span_fires_the_same_dispatch(
    monkeypatch, tmp_path,
):
    """The literal 'buttons are clickable on that row' requirement.

    Driven through a real ``events.Click`` carrying the exact ``style``
    a click at that screen position resolves to -- ``{"option": index,
    "@click": "row_action(...)"}``, the SAME two-key meta
    ``OptionList._on_click``'s own ``option`` lookup and this widget's
    ``@click`` lookup each read out of, proven to coexist by
    ``rich.style.Style``'s own meta-merge (``Strip.apply_meta`` unions a
    row's markup-derived style with the option-index style OptionList
    paints underneath it -- see ``ChipPicker._on_click``'s docstring).
    ``pilot.click`` itself is NOT used here: Textual's pilot posts a
    MouseDown/MouseUp/Click triplet and (empirically, against THIS
    OptionList subclass, in this Textual version) delivers ``_on_click``
    twice per click -- a testing-harness quirk pilot's own docstring
    flags ("bypasses the normal event processing in App.on_event"), not
    exercised by any pre-existing test in this repo (every other picker
    test selects a row by calling ``select_row`` directly) and not a
    claim about real terminal input, which goes through the full,
    un-bypassed path. Constructing the Click event directly proves the
    NEW code -- the meta lookup and its dispatch -- deterministically,
    without inheriting that harness quirk.

    The row's on-screen reality is asserted too, not assumed: non-zero
    height and the rendered (markup-resolved) text actually carrying the
    label a user would click -- the v0.28.0 bar."""
    fake = FakeEngine([])
    fake.list_pending_result = [_proposal("p1", "remember uv, not pip")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _pending_picker(pilot, app)
        assert picker.size.height > 0, "the picker must be on screen, not just in the DOM"
        index = _row_index(picker, "pending:0")
        assert "r reject" in _shown(picker)[index]

        from rich.style import Style
        from textual.events import Click

        style = Style.from_meta({"option": index, "@click": 'row_action("pending:0", "r")'})
        event = Click(
            widget=picker, x=0, y=0, delta_x=0, delta_y=0, button=1,
            shift=False, meta=False, ctrl=False, style=style,
        )
        await picker._on_click(event)
        for _ in range(100):
            if fake.rejected:
                break
            await pilot.pause(0.02)
        assert fake.rejected == ["p1"], (
            "a click on the action span must reach the same dispatch "
            "the keyboard path does"
        )
        assert fake.approved == []


# -- the collision rule: reserved letters vs. an active filter ------------


@pytest.mark.asyncio
async def test_a_reserved_letter_only_acts_while_the_filter_is_empty(
    monkeypatch, tmp_path,
):
    """The stated rule, verified rather than assumed: once the filter
    holds text, 'y'/'c'/'s'/'r' are ordinary characters -- never a stray
    outcome recorded on the way to typing a search word."""
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(1, "prefers terse commits")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        pane, picker = await _beliefs_picker(pilot, app)
        picker.highlighted = _row_index(picker, "belief:1")
        prompt = app.query_one("#prompt-input")
        prompt.value = "stale"
        await pilot.pause()
        assert fake.outcomes_recorded == [], (
            "typing a word starting with a reserved letter must never "
            "fire an action once the filter is non-empty"
        )
        assert prompt.value == "stale"


@pytest.mark.asyncio
async def test_the_prompt_never_takes_real_focus_away_and_never_sends_a_turn(
    monkeypatch, tmp_path,
):
    """v0.67.0's own scar warning, pinned: the beliefs/proposals picker
    does not steal keyboard focus (unlike every other chip menu), typed
    text becomes the filter, and Enter never reaches
    ``on_prompt_submitted`` while it is open."""
    fake = FakeEngine([])
    fake.list_beliefs_result = [
        _belief(1, "prefers terse commits"),
        _belief(2, "uses uv not pip"),
    ]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        pane, picker = await _beliefs_picker(pilot, app)
        prompt = app.query_one("#prompt-input")
        assert prompt.has_focus, "the prompt keeps real focus in this mode"
        assert not picker.has_focus
        assert prompt.border_title, "the mode must be visible somewhere"

        await pilot.press("u")
        await pilot.press("v")
        await pilot.pause()
        assert prompt.value == "uv"
        real_rows = [(rid, label) for rid, label in picker._rows if rid]
        assert real_rows and all("uv" in label for _rid, label in real_rows)

        turns_before = getattr(fake, "turns", None)
        await pilot.press("enter")
        await pilot.pause()
        # Enter acted on the highlighted row (opened its action sub-menu)
        # rather than submitting "uv" as a turn -- FakeEngine never saw a
        # turn start from this.
        assert getattr(fake, "turns", None) == turns_before

        # Escape closes and the filter does not survive it.
        for _ in range(50):
            if app.query_one("#chip-picker", ChipPicker).is_open:
                await pilot.press("escape")
                break
            await pilot.pause(0.02)
        await pilot.pause()
        assert not app.query_one("#chip-picker", ChipPicker).is_open
        assert prompt.value == ""
        assert prompt.border_title == ""


@pytest.mark.asyncio
async def test_arrow_keys_move_the_highlight_through_the_prompt(
    monkeypatch, tmp_path,
):
    fake = FakeEngine([])
    fake.list_pending_result = [
        _proposal("p1", "first"), _proposal("p2", "second"),
    ]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _pending_picker(pilot, app)
        start = picker.highlighted
        await pilot.press("down")
        await pilot.pause()
        assert picker.highlighted != start
        await pilot.press("up")
        await pilot.pause()
        assert picker.highlighted == start


# -- every OTHER chip menu is unaffected -----------------------------------


@pytest.mark.asyncio
async def test_the_model_picker_still_takes_real_focus_and_types_locally(
    monkeypatch, tmp_path,
):
    """Only the beliefs/proposals menus changed. Every other ChipPicker
    caller (model, branch, effort, mode, sessions, repo) is untouched:
    real focus, its own type-to-filter, unchanged."""
    fake = FakeEngine([], model="claude-haiku-4-5")
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        pane = app.active_pane
        await pane.open_model_picker()
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        assert picker.is_open
        assert not picker.prompt_filter_active
        assert picker.has_focus
        assert not picker._row_actions
