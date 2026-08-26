# SPDX-License-Identifier: AGPL-3.0-only
"""v0.67.0: inline row actions on the beliefs/proposals chip pickers, the
one shared row formatter both use, and the prompt-as-filter mode that
drives them.

Additive on top of item V/v0.48.0's existing per-row action SUB-menu
(``PaneChipsMixin._open_belief_actions`` / ``_open_pending_actions``,
covered by tests/test_beliefs_picker.py and left unchanged): this file
covers the FASTER path -- a click on the row's own action span, or the
reserved letter while that row is highlighted -- reaching the identical
engine calls.

Same testing bar tests/test_beliefs_picker.py states in its own module
docstring, restated here because it is the whole reason this file exists
rather than one more assertion bolted onto that one: "in the DOM" and "the
user can see it" are different claims (v0.28.0). Every action assertion
below reads the row back off the RENDERED option text (markup resolved),
checks the picker's own on-screen height, and for the click path drives a
REAL mouse event through Textual's own hit-testing -- never just a method
call standing in for one.

v0.69.0 added the evidence-expand section near the bottom: Right on a
highlighted belief row inserts its evidence trail as real rows directly
beneath it (one row per evidence event, never one joined blob -- the
picker's own fold mechanism, the same one a folded group's child rows
already use), Left removes them again. The section proves the shape the
removed beliefs browser's ``EvidenceTrail`` widget used to carry: fetched
lazily, capped, honest about an empty trail, and never reachable by an
action key -- the highlight cannot land on an evidence row at all, so
``y``/``c``/``s``/``r`` always act on the belief that owns the trail.

The same release also added three more sections: the column-name header
(shown once at the top, hidden under a typed filter, never reachable by
the highlight); the filter's own debounce and its in-flight marker
(measured, not assumed -- see the section's own lead comment for what a
600-row rebuild actually costs); and the underline/click-target fix on
``approve``/``retract``, whose armed labels used to pad their resting
column from INSIDE the clickable span -- the fix, and the shortened
armed labels that go with it, are both covered directly against the
markup spans :func:`ChipPicker._action_suffix` produces, not just the
rendered string.
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


def _belief(bid, claim, *, outcome=None, outcome_days=2, created_days=10,
            evidence_count=3, **extra):
    belief = {
        "id": bid, "subject": "user", "claim": claim, "confidence": 0.9,
        "created": _stamp(created_days * DAY), "outcomes": 1 if outcome else 0,
        # Nonzero by default (matching tests/test_beliefs_picker.py's own
        # `_belief` helper): most fixtures here are testing something OTHER
        # than the evidence-availability gate itself, and a belief with an
        # unset count reads to ChipPicker.expand_available as "known
        # empty" -- silently making Right a no-op for every belief in this
        # file would have broken every evidence test at once. Tests of the
        # gate itself (an actually-empty trail, or a belief known to carry
        # none) pass `evidence_count=` explicitly.
    }
    if outcome:
        belief.update({
            "outcome_event": outcome, "outcome_at": _stamp(outcome_days * DAY),
        })
    belief.update(extra)
    belief.setdefault("evidence_count", evidence_count)
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
        assert "RETRACT" in armed_text
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
        assert "⌫ RETRACT" not in _shown(picker)[i1]
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
        assert "✓ APPROVE" in _shown(picker)[index]

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


# -- v0.69.0: the underline/click-target must end where the word does ----


def test_the_armed_labels_no_longer_out_run_their_resting_ones():
    """Reported defect, root-caused: RowAction.column_width sizes the
    resting label's padded column to the WIDER of the resting and armed
    label, and retract/approve are the only two verbs that arm. Before
    this, "r retract…" (10) vs "⌫ CONFIRM RETRACT" (17) and "a approve"
    (9) vs "✓ CONFIRM APPROVE" (17) forced 7-8 columns of padding onto
    the resting label. Shortening the armed labels to match their own
    resting label's length is HALF the fix (see the rendering test below
    for the other half) -- checked here directly against the real
    production RowAction table, not a description of it."""
    from doxa.session.chips import BELIEF_ROW_ACTIONS, PENDING_ROW_ACTIONS

    for spec in BELIEF_ROW_ACTIONS + PENDING_ROW_ACTIONS:
        if spec.arms:
            assert spec.column_width == len(spec.label), (
                spec.key, spec.label, spec.armed_label,
                "an arming verb's resting column must not be padded to "
                "fit its own armed wording",
            )


def test_padding_lives_outside_the_clickable_underlined_span():
    """The other half, and the one that actually matters: even if a
    FUTURE RowAction's armed label runs longer than its resting one
    again, the padding that closes the gap must never be inside the
    `[@click=...][color]...[/][/]` span -- that span is both what Textual
    underlines and what the click hit-test keys off (`ChipPicker._on_click`
    reads `@click` out of the clicked cell's own style meta). A synthetic
    RowAction with a deliberately mismatched armed label proves the
    MECHANISM independent of today's production table, which (per the
    test above) no longer exercises any padding at all.

    Measured against `Content.from_markup`'s own parsed spans -- the same
    parser Textual's renderer uses, and the same one `_shown()` already
    relies on elsewhere in this file -- not against the raw markup string,
    because a span's (start, end) is the actual claim: "these character
    OFFSETS are click-and-underline", not "this substring looks right"."""
    from doxa.ui.dialogs import ChipPicker, RowAction

    picker = object.__new__(ChipPicker)
    picker._row_actions = [
        RowAction("z", "zap", "z zap", armed_label="⌫ CONFIRM ZAP THE THING",
                  color="#ff0000", armed_color="#ffffff", arms=True),
    ]
    picker._armed_rid = None
    suffix = picker._action_suffix("row:1")

    content = Content.from_markup(suffix)
    assert content.plain.endswith("z zap" + " " * (len("⌫ CONFIRM ZAP THE THING") - len("z zap")))
    click_span = next(s for s in content.spans if s.style == "@click=row_action(\"row:1\", \"z\")")
    word_start = content.plain.index("z zap")
    word_end = word_start + len("z zap")
    assert (click_span.start, click_span.end) == (word_start, word_end), (
        "the clickable/underlined span must end exactly where the word "
        "does, never inside the padding that follows it"
    )
    # And the padding itself carries NO style at all -- plain text, not
    # part of any span (colour or click).
    assert not any(s.start >= word_end for s in content.spans if "@click" in s.style)


@pytest.mark.asyncio
async def test_a_click_just_past_the_word_does_not_arm_it(monkeypatch, tmp_path):
    """The hazard named directly: with a mismatched-width RowAction (the
    shape production no longer has, but the mechanism must still hold for
    whichever caller reaches for one next), a click landing in the
    padding cell just past the resting word must fall through to an
    ordinary row select -- never reach the row action. Driven through the
    real style-meta lookup `_on_click` performs, at the exact offset the
    OLD (buggy) padded-inside-the-span rendering would have made
    clickable."""
    from doxa.ui.dialogs import ChipPicker
    from rich.style import Style
    from textual.events import Click

    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(1, "prefers terse commits")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        pane = app.active_pane
        await pane.open_beliefs_picker()
        picker = app.query_one("#chip-picker", ChipPicker)
        for _ in range(100):
            if picker.is_open:
                break
            await pilot.pause(0.02)
        index = _row_index(picker, "belief:1")

        # A click whose meta carries NO @click key at all -- the honest
        # simulation of landing in the (now-unstyled) padding, since the
        # real renderer no longer tags those cells with one. `_on_click`
        # must fall through to OptionList's own click handling rather
        # than firing a row action.
        style = Style.from_meta({"option": index})
        event = Click(
            widget=picker, x=0, y=0, delta_x=0, delta_y=0, button=1,
            shift=False, meta=False, ctrl=False, style=style,
        )
        await picker._on_click(event)
        await pilot.pause()
        assert fake.retracted == [] and fake.outcomes_recorded == []
        assert picker._armed_rid is None, (
            "a click with no @click in its style meta must never arm a row"
        )


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
        picker.flush_filter()  # v0.69.0: the filter itself now debounces
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


# -- v0.69.0: the column-name header -----------------------------------


@pytest.mark.asyncio
async def test_the_column_header_shows_once_at_the_top_not_per_group(
    monkeypatch, tmp_path,
):
    fake = FakeEngine([])
    fake.list_beliefs_result = [
        _belief(1, "prefers terse commits", subject="user"),
        _belief(2, "uses uv not pip", subject="project:doxa"),
    ]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _beliefs_picker(pilot, app)
        assert picker.size.height > 0, "the picker must be on screen, not just in the DOM"
        headers = [label for rid, label in picker._rows
                   if rid == "" and "date" in label and "status" in label]
        assert len(headers) == 1, headers
        # On screen, at the top -- before either group header.
        shown = _shown(picker)
        header_index = next(i for i, row in enumerate(shown)
                             if "date" in row and "status" in row)
        group_indices = [i for i, (rid, _l) in enumerate(picker._rows)
                          if rid.startswith(picker.GROUP_ROW_PREFIX)]
        assert group_indices and header_index < min(group_indices)


@pytest.mark.asyncio
async def test_the_column_header_names_the_columns_on_screen(
    monkeypatch, tmp_path,
):
    """Words the operator can pick, but they must be ON SCREEN and they
    must be the SAME words for both menus (the header is shared)."""
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(1, "prefers terse commits")]
    fake.list_pending_result = [_proposal("p1", "remember uv, not pip")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, beliefs_picker = await _beliefs_picker(pilot, app)
        beliefs_header = next(row for row in _shown(beliefs_picker)
                               if "date" in row and "status" in row)
        assert "age" in beliefs_header

        _pane, pending_picker = await _pending_picker(pilot, app)
        pending_header = next(row for row in _shown(pending_picker)
                               if "date" in row and "status" in row)
        assert "age" in pending_header
        # Same shared header, not two independently-worded ones.
        assert beliefs_header.strip() == pending_header.strip()


@pytest.mark.asyncio
async def test_the_header_is_never_the_initial_highlight(
    monkeypatch, tmp_path,
):
    """The opening highlight lands on the first SELECTABLE row -- which,
    with a single group, is that group's own (selectable, foldable)
    header, exactly as it always was before this file's own changes.
    What matters here is narrower and unaffected by that: the DISABLED
    column-name header two rows up the same list must never be it."""
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(1, "prefers terse commits")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _beliefs_picker(pilot, app)
        rid, label = picker._rows[picker.highlighted]
        assert rid != "", "the header must never be what opens highlighted"
        assert not ("date" in label and "status" in label)


@pytest.mark.asyncio
async def test_the_header_is_unreachable_by_action_keys_and_enter(
    monkeypatch, tmp_path,
):
    """Cursor movement already skips every disabled Option (this widget's
    own documented OptionList behaviour) -- checked here as the
    CONSEQUENCE that matters: walking the cursor through every row in the
    list, in both directions, never once lands the highlight on the
    header, so no action key or Enter routed through the highlight can
    reach it either."""
    fake = FakeEngine([])
    fake.list_beliefs_result = [
        _belief(1, "prefers terse commits"), _belief(2, "uses uv not pip"),
    ]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _beliefs_picker(pilot, app)
        seen_rids = set()
        for _ in range(2 * len(picker._rows) + 2):
            rid, _label = picker._rows[picker.highlighted]
            seen_rids.add(rid)
            picker.action_cursor_down()
        for _ in range(2 * len(picker._rows) + 2):
            rid, _label = picker._rows[picker.highlighted]
            seen_rids.add(rid)
            picker.action_cursor_up()
        assert "" not in seen_rids, (
            "the highlight must never land on the header, moving either way"
        )


@pytest.mark.asyncio
async def test_the_header_is_not_counted_as_a_belief(monkeypatch, tmp_path):
    """The header is ChipPicker's own synthetic row, never part of the
    caller's `rows` -- so it must not be able to shift ANYTHING that
    counts rows: `_all_rows` (what chips.py handed over, and what cap-note
    math like `_belief_cap_note`'s v0.69.0 fix reads `len(rows)` off of)
    stays exactly N; the rendered `_rows`/on-screen option count is one
    MORE than that, the header itself, never folded into either."""
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(i, f"claim {i}") for i in range(4)]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _beliefs_picker(pilot, app)
        belief_rows = [rid for rid, _l in picker._all_rows
                       if rid.startswith("belief:")]
        assert len(belief_rows) == 4, "the header must not appear in _all_rows"
        assert picker._note == "", (
            "a complete list must carry no cap caveat -- the header row "
            "must not have been counted as a 5th belief"
        )


@pytest.mark.asyncio
async def test_the_header_hides_under_a_filter_without_breaking_alignment(
    monkeypatch, tmp_path,
):
    """Hidden the same way a folded GROUP header already is the moment a
    filter is typed -- and safe to hide, because every row's own columns
    are fixed-width by construction (format_picker_prefix), so losing the
    header costs a LABEL, never the alignment under it."""
    fake = FakeEngine([])
    fake.list_beliefs_result = [
        _belief(1, "prefers terse commits", outcome="confirmed", outcome_days=2),
        _belief(2, "an unrelated claim"),
    ]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _beliefs_picker(pilot, app)
        assert any("date" in row and "status" in row for row in _shown(picker))
        index = _row_index(picker, "belief:1")
        offset_with_header = _shown(picker)[index].index("prefers terse commits")

        picker.sync_filter("terse")
        picker.flush_filter()
        assert not any("date" in row and "status" in row for row in _shown(picker)), (
            "the header must not survive a typed filter"
        )
        # The claim starts at the EXACT SAME column with the header
        # hidden as it did with the header shown -- the row's own columns
        # never moved, only the header row's presence changed.
        index = _row_index(picker, "belief:1")
        offset_without_header = _shown(picker)[index].index("prefers terse commits")
        assert offset_without_header == offset_with_header

        picker.sync_filter("")
        picker.flush_filter()
        assert any("date" in row and "status" in row for row in _shown(picker)), (
            "clearing the filter restores the header"
        )


# -- v0.69.0: the filter debounces, and says so while it is working -------


@pytest.mark.asyncio
async def test_a_keystroke_does_not_rebuild_rows_before_the_debounce_fires(
    monkeypatch, tmp_path,
):
    """The row rebuild is DEFERRED -- a keystroke changes what is typed
    immediately (proving the widget is not hung at the input end) without
    immediately re-scoring and repainting the whole candidate set."""
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(1, "prefers terse commits"),
                                 _belief(2, "uses uv not pip")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _beliefs_picker(pilot, app)
        before = [rid for rid, _l in picker._rows if rid.startswith("belief:")]
        assert len(before) == 2

        await pilot.press("u")
        await pilot.press("v")
        # No pause long enough for the debounce -- the row list must
        # still be the UNFILTERED one, and the border must already show
        # the in-flight marker even though the rows have not moved yet.
        after_keystroke = [rid for rid, _l in picker._rows if rid.startswith("belief:")]
        assert after_keystroke == before, (
            "the rebuild must not run synchronously with the keystroke"
        )
        assert picker.border_subtitle == "/uv …", picker.border_subtitle


@pytest.mark.asyncio
async def test_the_in_flight_marker_clears_once_the_debounce_settles(
    monkeypatch, tmp_path,
):
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(1, "prefers terse commits"),
                                 _belief(2, "uses uv not pip")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _beliefs_picker(pilot, app)

        await pilot.press("u")
        await pilot.press("v")
        assert picker.border_subtitle == "/uv …"
        for _ in range(100):
            if picker.border_subtitle == "/uv":
                break
            await pilot.pause(0.02)
        assert picker.border_subtitle == "/uv", (
            "the marker must clear once the debounced rebuild actually runs"
        )
        found = [rid for rid, _l in picker._rows if rid.startswith("belief:")]
        assert found == ["belief:2"]


@pytest.mark.asyncio
async def test_flush_filter_skips_the_wait_for_callers_that_want_it_now(
    monkeypatch, tmp_path,
):
    """The escape hatch `SessionSearch.launch` already offers its own
    callers, mirrored here: run the pending filter immediately rather
    than sleeping out DEBOUNCE_SECS."""
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(1, "prefers terse commits"),
                                 _belief(2, "uses uv not pip")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _beliefs_picker(pilot, app)
        await pilot.press("u")
        await pilot.press("v")
        picker.flush_filter()
        found = [rid for rid, _l in picker._rows if rid.startswith("belief:")]
        assert found == ["belief:2"]
        assert picker.border_subtitle == "/uv"


@pytest.mark.asyncio
async def test_a_fast_retype_never_paints_the_stale_intermediate_query(
    monkeypatch, tmp_path,
):
    """Race discipline, the SAME two-part rule `doxa.history.SessionSearch`
    documents: a keystroke arriving before the previous one's debounce
    fired must cancel it outright, so an intermediate query's rows are
    NEVER painted even for one frame on the way to the settled one.

    Three beliefs, chosen so the UNFILTERED start, the INTERMEDIATE
    query's own result and the FINAL query's own result are three
    genuinely different sets -- "uses terse commits" and "uses uv not
    pip" both fuzzy-match a bare "u" (matching {1, 2}), only "uses uv not
    pip" matches the ordered subsequence "uv" (matching {2}), and "an
    unrelated claim" matches neither. If the intermediate timer were not
    actually cancelled, `_rows` would visibly pass through {1, 2} on the
    way to the settled {2} -- this asserts that state is never observed,
    not merely that the FINAL one is correct (which a leaked intermediate
    render, later overwritten, would also satisfy)."""
    fake = FakeEngine([])
    fake.list_beliefs_result = [
        _belief(1, "uses terse commits"),
        _belief(2, "uses uv not pip"),
        _belief(3, "an unrelated claim"),
    ]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _beliefs_picker(pilot, app)

        picker.sync_filter("u")     # intermediate: would match {1, 2}
        picker.sync_filter("uv")    # arrives before "u"'s debounce fires
        for _ in range(100):
            found = {rid for rid, _l in picker._rows if rid.startswith("belief:")}
            if found == {"belief:2"}:
                break
            assert found != {"belief:1", "belief:2"}, (
                "the stale intermediate query's rows must never paint"
            )
            await pilot.pause(0.02)
        assert {rid for rid, _l in picker._rows if rid.startswith("belief:")} == {"belief:2"}


# -- v0.69.0: evidence, expanded in place ----------------------------------


def _evidence(session_id, note="", **extra):
    return {"session_id": session_id, "project": "doxa", "note": note,
            "created": "2026-05-02T09:00:00Z", **extra}


@pytest.mark.asyncio
async def test_evidence_is_never_fetched_on_load_only_on_expand(
    monkeypatch, tmp_path,
):
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(7, "prefers terse commits")]
    fake.belief_evidence_result = {7: [_evidence("sess-a", "said so in a PR")]}
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _beliefs_picker(pilot, app)
        assert fake.belief_evidence_calls == [], "evidence must not be fetched at load"
        assert not any("sess-a" in row for row in _shown(picker))


@pytest.mark.asyncio
async def test_right_expands_evidence_as_real_rows_under_the_belief(
    monkeypatch, tmp_path,
):
    """The headline requirement: evidence survives in the picker, as rows
    inserted directly beneath the belief that owns them -- not a popup, not
    a second pane, not one joined blob. Rendered height and on-screen text
    are asserted, not just membership in ``picker._rows`` (v0.28.0)."""
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(7, "prefers terse commits")]
    fake.belief_evidence_result = {7: [
        _evidence("sess-a", "said so while reviewing a PR"),
        _evidence("sess-b", "repeated it in a later session"),
    ]}
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _beliefs_picker(pilot, app)
        index = _row_index(picker, "belief:7")
        picker.highlighted = index
        before = picker.option_count

        picker.expand_current()
        for _ in range(100):
            if fake.belief_evidence_calls:
                break
            await pilot.pause(0.02)
        await pilot.pause()

        assert fake.belief_evidence_calls == [7]
        assert picker.size.height > 0, "the picker must be on screen, not just in the DOM"
        # TWO evidence events -> two extra rows, each its own Option --
        # never one row holding both joined together.
        assert picker.option_count == before + 2
        shown = _shown(picker)
        assert any("sess-a" in row and "said so while reviewing a PR" in row
                   for row in shown)
        assert any("sess-b" in row and "repeated it in a later session" in row
                   for row in shown)
        # The two evidence rows are SEPARATE Options -- neither line
        # carries the other's session id.
        sess_a_row = next(row for row in shown if "sess-a" in row)
        sess_b_row = next(row for row in shown if "sess-b" in row)
        assert "sess-b" not in sess_a_row
        assert "sess-a" not in sess_b_row


@pytest.mark.asyncio
async def test_the_highlight_cannot_land_on_an_evidence_row(
    monkeypatch, tmp_path,
):
    """The owner's own stated risk: an action key must act on the BELIEF,
    never on one of its evidence rows. OptionList skips disabled options
    on cursor movement, so this asserts the CONSEQUENCE of that -- moving
    down from the expanded belief skips straight over its evidence rows to
    the next real candidate, and a reserved letter pressed right after
    expanding still resolves against the belief's own id."""
    fake = FakeEngine([])
    fake.list_beliefs_result = [
        _belief(7, "prefers terse commits"),
        _belief(8, "uses uv not pip"),
    ]
    fake.belief_evidence_result = {7: [_evidence("sess-a", "a note")]}
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _beliefs_picker(pilot, app)
        index7 = _row_index(picker, "belief:7")
        picker.highlighted = index7
        picker.expand_current()
        for _ in range(100):
            if fake.belief_evidence_calls:
                break
            await pilot.pause(0.02)
        await pilot.pause()

        # Moving down from the (still highlighted) belief lands on the
        # NEXT real candidate, never on one of its own evidence rows.
        picker.highlighted = index7
        picker.action_cursor_down()
        rid, _label = picker._rows[picker.highlighted]
        assert rid == "belief:8", (
            "the highlight must skip straight over evidence rows"
        )

        # A reserved letter pressed while the ORIGINAL belief is
        # highlighted still resolves against ITS id, unaffected by the
        # rows now sitting under it.
        picker.highlighted = index7
        assert picker.try_action_key("y") is True
        for _ in range(100):
            if fake.outcomes_recorded:
                break
            await pilot.pause(0.02)
        assert fake.outcomes_recorded == [(7, "confirmed")]


@pytest.mark.asyncio
async def test_left_folds_the_evidence_away_again(monkeypatch, tmp_path):
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(7, "prefers terse commits")]
    fake.belief_evidence_result = {7: [_evidence("sess-a", "a note")]}
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _beliefs_picker(pilot, app)
        index = _row_index(picker, "belief:7")
        picker.highlighted = index
        picker.expand_current()
        for _ in range(100):
            if fake.belief_evidence_calls:
                break
            await pilot.pause(0.02)
        await pilot.pause()
        assert any("sess-a" in row for row in _shown(picker))

        picker.highlighted = index
        picker.collapse_current()
        await pilot.pause()
        assert not any("sess-a" in row for row in _shown(picker))

        # Expanding again re-fetches -- collapsing forgot the cached
        # rows, it did not just hide them.
        picker.highlighted = index
        picker.expand_current()
        for _ in range(100):
            if len(fake.belief_evidence_calls) == 2:
                break
            await pilot.pause(0.02)
        assert fake.belief_evidence_calls == [7, 7]


@pytest.mark.asyncio
async def test_a_belief_with_no_evidence_says_so_distinctly_from_loading(
    monkeypatch, tmp_path,
):
    """"No evidence" and "not fetched yet" are different states and must
    look different -- zero rows would look like nothing happened.

    This is the SURPRISE case: the belief's own row said it carried
    evidence (a nonzero ``evidence_count``, the ``_belief`` default), so
    the gate lets the fetch through, and the store turns out to hold
    nothing anyway -- a real mismatch (a stale count, evidence pruned out
    from under it) worth surfacing, not hiding. The other case -- a
    belief whose OWN count already says zero, so Right never even asks --
    is `test_a_belief_known_to_have_no_evidence_never_fetches` below."""
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(7, "prefers terse commits")]
    fake.belief_evidence_result = {7: []}
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _beliefs_picker(pilot, app)
        index = _row_index(picker, "belief:7")
        picker.highlighted = index
        before = picker.option_count

        picker.expand_current()
        # Checked SYNCHRONOUSLY, with no pause: the loading row is painted
        # by expand_current() itself, before the fetch worker even starts
        # (see ChipPicker.expand_current's own `_render_rows()` call,
        # ahead of `run_worker`) -- a THIRD distinct state from either "no
        # evidence" or a populated trail, and one a fake engine with no
        # real latency resolves out of within a single event-loop turn if
        # this is not checked before yielding to it.
        assert any("loading" in row for row in _shown(picker))

        for _ in range(100):
            if fake.belief_evidence_calls:
                break
            await pilot.pause(0.02)
        await pilot.pause()
        assert picker.option_count == before + 1, "one row, not zero"
        shown = _shown(picker)
        assert any("no evidence" in row for row in shown)
        assert not any("loading" in row for row in shown)


@pytest.mark.asyncio
async def test_a_belief_known_to_have_no_evidence_never_fetches(
    monkeypatch, tmp_path,
):
    """v0.69.0: a belief whose OWN row already carries
    ``evidence_count == 0`` is known, cheaply and synchronously, to have
    nothing -- Right on it must stay silent exactly like it already does
    on a proposal row (no expand capability at all), never spend a round
    trip to learn what the count already said, and never flash a loading
    row that resolves to "no evidence" a moment later."""
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(7, "prefers terse commits", evidence_count=0)]
    # If this fired, it would prove the gate failed to stop it -- present
    # so a wrongly-reached fetch is caught by CONTENT, not just absence.
    fake.belief_evidence_result = {7: [_evidence("sess-a", "should never be seen")]}
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _beliefs_picker(pilot, app)
        index = _row_index(picker, "belief:7")
        picker.highlighted = index
        before_count = picker.option_count
        before_shown = _shown(picker)[index]

        picker.expand_current()
        await pilot.pause()
        await pilot.pause()

        assert fake.belief_evidence_calls == [], "a known-empty belief must never be fetched"
        assert picker.option_count == before_count, "no row inserted, not even a loading one"
        assert _shown(picker)[index] == before_shown, "the row itself must not repaint either"


@pytest.mark.asyncio
async def test_a_trail_past_the_cap_says_so_in_its_own_row(
    monkeypatch, tmp_path,
):
    """A trail past :data:`doxa.events.BELIEF_EVIDENCE_LIMIT` must not be
    shown as though it were complete -- the same honesty rule the beliefs
    picker's own cap note already follows for the belief LIST."""
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(7, "prefers terse commits")]
    fake.belief_evidence_result = {7: [
        _evidence("sess-a", "first", trail_truncated=True),
    ]}
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _beliefs_picker(pilot, app)
        index = _row_index(picker, "belief:7")
        picker.highlighted = index
        picker.expand_current()
        for _ in range(100):
            if fake.belief_evidence_calls:
                break
            await pilot.pause(0.02)
        await pilot.pause()
        shown = _shown(picker)
        assert any("trail continues" in row for row in shown)


@pytest.mark.asyncio
async def test_typing_a_filter_hides_expanded_evidence_without_forgetting_it(
    monkeypatch, tmp_path,
):
    """Evidence text is not itself searchable -- the fuzzy matcher only
    ever scored the belief's own label -- so a typed filter would show
    rows a reader's search term cannot explain. Suppressed rather than
    left in, the same rule a folded GROUP already follows the moment a
    filter is typed; the fetched trail itself is not forgotten, so
    clearing the filter restores it without a second fetch."""
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(7, "prefers terse commits")]
    fake.belief_evidence_result = {7: [_evidence("sess-a", "a note")]}
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        pane, picker = await _beliefs_picker(pilot, app)
        index = _row_index(picker, "belief:7")
        picker.highlighted = index
        picker.expand_current()
        for _ in range(100):
            if fake.belief_evidence_calls:
                break
            await pilot.pause(0.02)
        await pilot.pause()
        assert any("sess-a" in row for row in _shown(picker))

        prompt = app.query_one("#prompt-input")
        prompt.value = "terse"
        await pilot.pause()
        picker.flush_filter()  # v0.69.0: the filter itself now debounces
        assert not any("sess-a" in row for row in _shown(picker)), (
            "evidence rows must not survive a typed filter"
        )

        prompt.value = ""
        await pilot.pause()
        picker.flush_filter()
        assert any("sess-a" in row for row in _shown(picker)), (
            "clearing the filter restores the expansion without a re-fetch"
        )
        assert fake.belief_evidence_calls == [7], "no second fetch on re-show"


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
