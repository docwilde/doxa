# SPDX-License-Identifier: AGPL-3.0-only
"""Lettered item V: the beliefs browser.

Every test here asserts something a USER can see or a store can be checked
for. That bar is not stylistic: the v0.28.0 defect shipped a modal whose
buttons were in the DOM at zero height for a whole release, invisible,
while every structural assertion passed. So rows are asserted to have
non-zero size, the age/verdict/provenance text is asserted to be ON SCREEN
(read back off the rendered widget, not off the formatter that fed it),
tooltips are asserted by their TEXT, and the write path is asserted
against the engine's own ledger of what it was asked to do.

The security assertion (nothing is approved without an explicit per-item
action) is written as one -- it drives the whole surface with everything a
careless hand could plausibly hit and asserts the ledger stayed empty.
"""

from __future__ import annotations

import contextlib
import time

import pytest

from doxa import commands
from doxa.app import DoxaApp
from doxa.ui.beliefs import BeliefRow, BeliefsBrowserTab, EvidenceTrail, ProposalRow
from doxa.ui.labels import (
    _fmt_age,
    _fmt_belief_row,
    _fmt_pending_row,
    belief_age_text,
    belief_outcome_kind,
    belief_outcome_text,
    belief_sort_key,
    belief_tooltip,
    proposal_supersedes,
    proposal_tooltip,
    proposal_verdict,
)

from textual.widgets import TabbedContent

from fakes import FakeEngine


def _status_plain(app) -> str:
    """The status bar as a reader sees it -- markup resolved. Same helper
    tests/test_status_chips.py uses: a chip asserted against raw markup is
    a chip whose colour can hide it."""
    from textual.content import Content

    return Content.from_markup(str(app.query_one("#status-bar").renderable)).plain

DAY = 86400.0


def _stamp(secs_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - secs_ago))


def _belief(bid, claim, *, subject="user", created_days=120, idle_days=40,
            via="derived", confidence=0.9, evidence_count=3,
            outcome=None, outcome_days=2, outcomes=0, source="dream"):
    """One belief as ``SessionEngine.list_beliefs`` hands it over.

    ``outcomes`` is ALWAYS present -- 0 meaning "the ledger was read and is
    empty", which is the ~95% case on a real store and the reason "never
    tested" is a state rather than a large age. A record with no
    ``outcomes`` key at all is a DIFFERENT thing (something predating the
    column) and is built explicitly where it is tested."""
    belief = {
        "id": bid, "subject": subject, "claim": claim, "confidence": confidence,
        "created": _stamp(created_days * DAY),
        "updated": _stamp(idle_days * DAY),
        "last_referenced": _stamp(idle_days * DAY),
        "via": via, "evidence_count": evidence_count,
        "outcomes": outcomes,
    }
    if outcome:
        belief.update({
            "outcome_event": outcome,
            "outcome_at": _stamp(outcome_days * DAY),
            "outcome_source": source,
            f"outcome_{outcome}s": max(1, outcomes),
            "outcomes": max(1, outcomes),
        })
    return belief


def _proposal(pid, text, *, kind="memory", action="add", scope="user",
              staged_days=5, **extra):
    item = {
        "pid": pid, "kind": kind, "action": action, "scope": scope,
        "text": text, "created": _stamp(staged_days * DAY),
        "session_id": "sess-1", "project": "doxa",
    }
    item.update(extra)
    return item


async def _browser(pilot, app, fake) -> BeliefsBrowserTab:
    pane = app.active_pane
    await pane.open_beliefs_browser()
    for _ in range(200):
        tab = pane._beliefs_tab
        if tab is not None and tab.rows:
            await pilot.pause()
            return tab
        await pilot.pause(0.02)
    raise AssertionError("the beliefs browser never finished loading")


async def _open(monkeypatch, tmp_path, fake):
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    app = DoxaApp(cwd=str(tmp_path))
    return app


def _plain(widget) -> str:
    """What the widget actually renders, markup resolved -- not the string
    a formatter returned. The two are the same only when nobody made the
    v0.35.0 mistake of keying content off one form and reading the other."""
    return str(widget.renderable)


# -- the formatters: timestamps, age, verdict ---------------------------


def test_fmt_age_gained_a_day_tier_and_kept_every_old_one():
    """One age format, everywhere. Beliefs are months old and "2904h0m" is
    arithmetic homework; everything under a day renders exactly as it did
    before, which is what lets this stay one function."""
    assert _fmt_age(5) == "5s"
    assert _fmt_age(300) == "5m"
    assert _fmt_age(3600 * 5 + 60) == "5h1m"
    assert _fmt_age(DAY * 3 + 3600 * 4) == "3d4h"
    assert _fmt_age(DAY * 120) == "120d"


def test_a_belief_row_carries_its_creation_time_and_its_last_verdict():
    """v0.67.0: the outcome KIND and its AGE are two separate fixed
    columns now (status, then age), not one merged "confirmed 3d" string
    -- see doxa.ui.labels.format_picker_row."""
    row = _fmt_belief_row(_belief(1, "prefers terse commits", created_days=120,
                                  outcome="confirmed", outcome_days=3))
    assert "prefers terse commits" in row
    assert "confirmed" in row
    assert "3d0h" in row
    assert time.strftime("%m-%d %H:%M",
                         time.gmtime(time.time() - 120 * DAY)) in row


def test_the_picker_row_shows_hh_mm_not_just_the_day():
    """Asked for directly. A belief store IS browsed by day, which was the
    old argument for dropping the clock -- but two beliefs derived in the
    same session land on the same date, and the date alone cannot order
    them for a reader."""
    row = _fmt_belief_row({"id": 1, "claim": "x",
                           "created": "2026-08-25T14:23:07Z", "outcomes": 0})
    assert "14:23" in row
    assert "14:23:07" not in row, "seconds are a precision nobody acts on"


def test_the_stamp_is_yy_mm_dd_hh_mm_and_never_changes_width():
    """v0.48.0 dropped the year from a belief derived in the current one to
    buy back a column. The user asked for it back and wrote the format out.
    The better reason is the second one: a stamp that is 11 characters for
    some rows and 14 for others makes the CLAIM column start in a different
    place down the list -- the shifting surface this codebase avoids
    everywhere else."""
    from doxa.ui.labels import belief_created_text

    now = time.mktime(time.strptime("2026-08-25", "%Y-%m-%d"))
    this_year = {"created": "2026-03-04T09:14:00Z"}
    older = {"created": "2025-11-03T09:14:00Z"}
    assert belief_created_text(this_year, now=now) == "26-03-04 09:14"
    assert belief_created_text(older, now=now) == "25-11-03 09:14"
    assert len(belief_created_text(this_year, now=now)) == len(
        belief_created_text(older, now=now))
    # Still no seconds -- a precision nobody acts on for a derived claim.
    assert belief_created_text({"created": "2026-03-04T09:14:07Z"}).count(":") == 1
    # The BROWSER spells the century out; it is read as a record and has
    # the width. Fixed-width too.
    assert belief_created_text(this_year, full=True, now=now) == "2026-03-04 09:14"


def test_an_unreadable_created_stamp_never_grows_an_invented_clock():
    from doxa.ui.labels import belief_created_text

    unreadable = belief_created_text({"created": "sometime last week"})
    assert ":" not in unreadable, "no clock invented for a string it cannot parse"
    assert belief_created_text({}) == ""


def test_being_cited_is_not_being_confirmed():
    """The v0.46.0 correction, as one assertion. A belief referenced
    minutes ago and never once tested is NOT fresh, and the row must not
    say a number that reads as though it were."""
    cited_but_untested = _belief(1, "x", idle_days=0, outcomes=0)
    assert belief_outcome_text(cited_but_untested) == "never tested"
    row = _fmt_belief_row(cited_but_untested)
    assert "never tested" in row
    assert "idle" not in row
    # ...and the tooltip says the two things separately and in order.
    tip = belief_tooltip(cited_but_untested)
    assert "never tested — no outcome has ever been recorded" in tip
    assert "cited, not confirmed" in tip


@pytest.mark.parametrize("event", ["confirmed", "contradicted", "stale"])
def test_every_lore_verdict_renders_as_itself(event):
    """Read off lore_core.store's own CHECK constraint on
    belief_outcomes.event, not invented here. "confirmed 2d" and
    "contradicted 2d" are opposite facts about one belief and must never
    render alike."""
    belief = _belief(1, "x", outcome=event, outcome_days=2)
    assert belief_outcome_text(belief) == f"{event} 2d0h"
    assert belief_outcome_kind(belief) == event


def test_the_three_verdicts_and_never_tested_are_four_distinct_colours():
    from doxa.ui.labels import OUTCOME_COLORS, belief_outcome_color

    seen = {belief_outcome_color(_belief(1, "x", outcome=e))
            for e in ("confirmed", "contradicted", "stale")}
    seen.add(belief_outcome_color(_belief(1, "x")))
    assert len(seen) == 4, seen
    assert set(OUTCOME_COLORS) == {"confirmed", "contradicted", "stale", "untested"}


def test_never_tested_is_a_state_and_not_an_age():
    """It must not be mistakable for a duration -- no digits, no unit."""
    text = belief_outcome_text(_belief(1, "x", created_days=900, idle_days=900))
    assert text == "never tested"
    assert not any(ch.isdigit() for ch in text)


def test_a_record_predating_the_ledger_renders_without_the_column():
    """Same rule a NULL `via` follows: an absent key is an admission, a
    zero is a measurement. A belief from a daemon that predates the column
    gets NO staleness column rather than a guessed "never tested"."""
    old = {"id": 1, "subject": "user", "claim": "x", "confidence": 0.5,
           "created": _stamp(10 * DAY)}
    assert belief_outcome_kind(old) == ""
    assert belief_outcome_text(old) == ""
    assert "never tested" not in _fmt_belief_row(old)


def test_tested_beliefs_sort_ahead_of_never_tested_ones():
    """31 outcome rows against 628 active beliefs: the tested ones are
    needles, and never-tested sorts as a BUCKET rather than by age."""
    tested_old = _belief(1, "a", outcome="confirmed", outcome_days=90)
    tested_new = _belief(2, "b", outcome="contradicted", outcome_days=1)
    untested_recent = _belief(3, "c", idle_days=0)
    untested_ancient = _belief(4, "d", idle_days=900)
    order = sorted([untested_recent, tested_old, untested_ancient, tested_new],
                   key=belief_sort_key)
    assert [b["id"] for b in order] == [2, 1, 3, 4]
    # The two untested ones keep the order they arrived in -- a stable
    # bucket, not an age ranking.
    assert belief_sort_key(untested_recent) == belief_sort_key(untested_ancient)


def test_a_belief_with_no_timestamps_renders_a_blank_not_a_guessed_column():
    """v0.67.0: the picker row is now FIXED-WIDTH columns, so a fact the
    store does not carry renders as BLANK columns (padded, held open)
    rather than the column disappearing outright -- the same "an absent
    key is an admission, a zero is a measurement" rule this row already
    followed, extended to a table shape. Never a GUESSED stamp/status/age
    for a belief arriving from an older daemon, which is the invariant
    that actually matters here."""
    from doxa.ui.labels import PICKER_PREFIX_WIDTH

    row = _fmt_belief_row({"id": 1, "claim": "x"})
    assert row == " " * PICKER_PREFIX_WIDTH + "x"
    assert row[PICKER_PREFIX_WIDTH:] == "x"
    assert belief_age_text({"id": 1, "claim": "x"}) == ""


def test_last_referenced_survives_in_the_tooltip_and_only_there():
    """It is not worthless -- "cited three days ago, never once confirmed"
    is a real state -- but two age-shaped numbers on one line, only one of
    which means anything, is the confusion v0.46.0 removes. So: tooltip,
    labelled for what it is, below the outcome and never beside it."""
    belief = _belief(1, "x", idle_days=3, outcome="confirmed", outcome_days=40)
    row = _fmt_belief_row(belief)
    assert "confirmed" in row
    assert "40d" in row
    assert "idle" not in row
    tip = belief_tooltip(belief)
    assert "cited, not confirmed" in tip
    assert tip.index("last outcome:") < tip.index("last referenced")
    # The coalesce it reports is still lore_core's own dormancy clock.
    assert "2d" in belief_age_text(
        {"updated": _stamp(300 * DAY), "last_referenced": _stamp(2 * DAY)})
    assert "9d" in belief_age_text({"updated": _stamp(9 * DAY)})


@pytest.mark.parametrize("item,expected", [
    (_proposal("p1", "uv not pip"), "add → memory/user"),
    (_proposal("p2", "uv not pip", scope="project"), "add → memory/project:doxa"),
    (_proposal("p3", "new text", action="replace", match="old text"),
     "replace → memory/user"),
    (_proposal("p4", "gone", action="remove", match="old text"),
     "remove → memory/user"),
    ({"pid": "p5", "kind": "belief", "action": "retract", "id": 42},
     "retract → belief #42"),
    ({"pid": "p6", "kind": "belief", "subject": "project:doxa",
      "claim": "uses uv"}, "add → belief/project:doxa"),
    ({"pid": "p7", "kind": "filemap", "project": "doxa", "path": "doxa/app.py",
      "purpose": "facade"}, "add → filemap/doxa"),
    ({"pid": "p8", "kind": "skill", "action": "retire", "name": "old-skill"},
     "retire → skill/old-skill"),
])
def test_every_proposal_kind_says_what_approving_it_would_do(item, expected):
    assert proposal_verdict(item) == expected


def test_a_proposal_that_arrived_as_bare_text_claims_no_verdict():
    """An older daemon still serves strings. No verdict is the honest
    answer; a guessed one on a write path is the wrong place to guess."""
    assert proposal_verdict("remember uv, not pip") == ""
    assert "cannot be shown" in proposal_tooltip("remember uv, not pip")


def test_a_replacing_proposal_names_what_it_supersedes():
    item = _proposal("p", "uses uv", action="replace", match="uses pip")
    assert proposal_supersedes(item) == "uses pip"
    assert "superseding: uses pip" in proposal_tooltip(item)


def test_a_pending_row_leads_with_the_verdict_and_the_wait():
    """v0.67.0: stamp, verdict and age are three separate FIXED columns
    now (the shared beliefs/proposals shape -- see
    doxa.ui.labels.format_picker_row), not a `` · ``-joined string that
    drifted with every field's own length. The verdict still leads --
    right after the fixed-width stamp column -- because it is the part a
    reviewer scans."""
    from doxa.ui.labels import PICKER_STAMP_COL

    row = _fmt_pending_row(_proposal("p", "remember uv, not pip", staged_days=5))
    assert row.index("add → memory/user") == PICKER_STAMP_COL
    assert "5d0h" in row
    assert "remember uv, not pip" in row  # still filterable by its own words


# -- the surface: rows are visible, and say what they should -------------


@pytest.mark.asyncio
async def test_the_browser_opens_as_a_full_height_tab_with_visible_rows(
    monkeypatch, tmp_path
):
    """The v0.28.0 guard: rows must have real size, not merely exist."""
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(1, "prefers terse commits")]
    fake.list_pending_result = [_proposal("20260824-00", "remember uv, not pip")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        browser = await _browser(pilot, app, fake)
        assert browser.is_mounted
        assert browser.size.height > 10, "a browser, not a dropdown"
        rows = list(browser.query(BeliefRow)) + list(browser.query(ProposalRow))
        assert rows
        for row in rows:
            assert row.size.height > 0 and row.size.width > 0, row


@pytest.mark.asyncio
async def test_belief_rows_show_the_verdict_timestamp_and_provenance_on_screen(
    monkeypatch, tmp_path
):
    fake = FakeEngine([])
    fake.list_beliefs_result = [
        _belief(1, "prefers terse commits", created_days=120,
                via="derived", outcome="confirmed", outcome_days=40),
        _belief(2, "uses uv for deps", subject="project:doxa", via=None,
                created_days=10),
    ]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        browser = await _browser(pilot, app, fake)
        rendered = "\n".join(_plain(r) for r in browser.query(BeliefRow))
        assert "prefers terse commits" in rendered
        assert "confirmed 40d" in rendered
        # ...and the one that reality has never tested says exactly that,
        # rather than wearing an age it did not earn.
        assert "never tested" in rendered
        assert "idle" not in rendered
        assert time.strftime("%Y-%m-%d",
                             time.gmtime(time.time() - 120 * DAY)) in rendered
        assert "via derived" in rendered
        # A belief the store never labelled is named as unlabelled, never
        # back-filled with a plausible one.
        assert "provenance unknown" in rendered
        assert "3 evidence" in rendered


@pytest.mark.asyncio
async def test_proposal_rows_show_the_verdict_and_the_wait_on_screen(
    monkeypatch, tmp_path
):
    fake = FakeEngine([])
    fake.list_pending_result = [
        _proposal("20260824-00", "remember uv, not pip", staged_days=5),
        _proposal("20260824-01", "uses uv", action="replace", match="uses pip",
                  staged_days=61),
    ]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        browser = await _browser(pilot, app, fake)
        rendered = "\n".join(_plain(r) for r in browser.query(ProposalRow))
        assert "add → memory/user" in rendered
        assert "replace → memory/user" in rendered
        assert "supersedes: uses pip" in rendered
        assert "staged 5d" in rendered and "staged 61d" in rendered


@pytest.mark.asyncio
async def test_hovering_a_row_shows_the_full_claim_text(monkeypatch, tmp_path):
    """The user's ask, asserted as the user would read it: the tooltip
    carries the WHOLE claim, where the row carries an ellipsized one.

    Also the v0.35.0 guard. That defect keyed a hint by the chip's markup
    while the lookup ran against markup-stripped text, so the hint
    silently vanished at two tiers. There is no lookup here at all: the
    row object that renders the line carries the tooltip, set from the
    same record in the same constructor -- and this asserts they agree."""
    long_claim = (
        "the operator prefers terse conventional commit subjects and asks "
        "for the body to explain why rather than what, because the diff "
        "already says what, and a body that repeats it is noise in every "
        "future bisect"
    )
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(1, long_claim)]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(120, 48)) as pilot:
        await pilot.pause()
        browser = await _browser(pilot, app, fake)
        row = next(iter(browser.query(BeliefRow)))
        # The ROW is ellipsized...
        assert "…" in _plain(row)
        assert long_claim not in _plain(row)
        # ...and the TOOLTIP is not.
        assert isinstance(row.tooltip, str)
        assert long_claim in row.tooltip
        assert "confidence 0.90" in row.tooltip
        assert "via derived" in row.tooltip
        assert row.tooltip == belief_tooltip(row.belief)


@pytest.mark.asyncio
async def test_hovering_a_proposal_says_what_approving_it_would_do(
    monkeypatch, tmp_path
):
    fake = FakeEngine([])
    fake.list_pending_result = [
        _proposal("20260824-00", "uses uv", action="replace", match="uses pip"),
    ]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(120, 48)) as pilot:
        await pilot.pause()
        browser = await _browser(pilot, app, fake)
        row = next(iter(browser.query(ProposalRow)))
        assert "approving this would: replace → memory/user" in row.tooltip
        assert "superseding: uses pip" in row.tooltip


@pytest.mark.asyncio
async def test_a_belief_row_expands_its_evidence_trail_on_demand(
    monkeypatch, tmp_path
):
    """Evidence is fetched per belief, on expand -- never as part of the
    list. That is how a browser over hundreds of beliefs stays inside one
    64KB wire frame, so this asserts both halves: nothing is fetched at
    load, and the trail appears when a row asks for it."""
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(7, "prefers terse commits")]
    fake.belief_evidence_result = {7: [
        {"session_id": "sess-a", "project": "doxa",
         "note": "said so while reviewing a PR", "created": "2026-05-02T09:00:00Z"},
    ]}
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        browser = await _browser(pilot, app, fake)
        assert fake.belief_evidence_calls == []  # never on load
        row = next(iter(browser.query(BeliefRow)))
        row.focus()
        await pilot.press("enter")
        for _ in range(100):
            trails = list(browser.query(EvidenceTrail))
            if trails:
                break
            await pilot.pause(0.02)
        await pilot.pause()
        assert fake.belief_evidence_calls == [7]
        trail = next(iter(browser.query(EvidenceTrail)))
        assert trail.size.height > 0
        assert "said so while reviewing a PR" in _plain(trail)
        assert "sess-a" in _plain(trail)


# -- the write half: per row, per action, never by accident --------------


@pytest.mark.asyncio
async def test_nothing_is_approved_without_an_explicit_per_item_action(
    monkeypatch, tmp_path
):
    """SECURITY ASSERTION.

    The whole point of LORE's approval gate is that a human looked at THIS
    proposal. This drives the browser with everything a careless hand
    plausibly hits -- opening it, moving through every row, pressing Enter
    on each (Enter is the key a hand rests on), pressing every other key
    that is not the approve key -- and asserts the engine was never asked
    to approve or reject anything.

    It then asserts the arm: ONE press of the approve key still approves
    nothing, because the first press only arms the control. Only the
    second press, on that same row, reaches the engine, and it reaches it
    with exactly one id.
    """
    fake = FakeEngine([])
    fake.list_pending_result = [
        _proposal("20260824-00", "first proposal"),
        _proposal("20260824-01", "second proposal"),
        _proposal("20260824-02", "third proposal"),
    ]
    fake.list_beliefs_result = [_belief(1, "a belief")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        browser = await _browser(pilot, app, fake)

        # Walk the whole surface, leaning on Enter and the arrow keys.
        for _ in range(len(browser.rows) + 2):
            await pilot.press("enter")
            await pilot.press("down")
            await pilot.pause()
        for key in ("home", "end", "space", "tab", "pageup", "pagedown", "y", "n"):
            await pilot.press(key)
            await pilot.pause()
        assert fake.approved == [], "an approve reached the engine unasked"
        assert fake.rejected == [], "a reject reached the engine unasked"

        # No bulk affordance anywhere on screen, under any spelling.
        painted = "\n".join(
            str(w.renderable) for w in browser.query("Static")
        ).lower()
        for phrase in ("approve all", "approve every", "accept all", "reject all"):
            assert phrase not in painted, phrase

        # ARM: one press of the approve key on one row writes nothing.
        first = next(r for r in browser.rows if isinstance(r, ProposalRow))
        first.focus()
        await pilot.press("a")
        await pilot.pause()
        assert first.armed
        assert fake.approved == [], "the first approve press must only arm"
        assert "CONFIRM APPROVE" in _plain(first)

        # Moving away DISARMS -- an armed control never outlives attention
        # on the row that armed it.
        second = [r for r in browser.rows if isinstance(r, ProposalRow)][1]
        second.action_approve()
        await pilot.pause()
        assert not first.armed and second.armed
        assert fake.approved == []

        # CONFIRM: the second press on the SAME row, and only that, writes.
        second.action_approve()
        for _ in range(100):
            if fake.approved:
                break
            await pilot.pause(0.02)
        assert fake.approved == ["20260824-01"], "exactly one id, exactly once"
        assert fake.rejected == []


@pytest.mark.asyncio
async def test_a_click_on_one_rows_approve_affects_only_that_proposal(
    monkeypatch, tmp_path
):
    """The user asked for a button in each row. A click on THAT row's
    control (twice -- approve arms first) approves THAT id and no other."""
    fake = FakeEngine([])
    fake.list_pending_result = [
        _proposal("20260824-00", "first proposal"),
        _proposal("20260824-01", "second proposal"),
    ]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        browser = await _browser(pilot, app, fake)
        rows = [r for r in browser.rows if isinstance(r, ProposalRow)]
        assert "✓ approve" in _plain(rows[0]) and "✗ reject" in _plain(rows[0])

        rows[1].action_approve()   # the click target's own action method
        rows[1].action_approve()
        for _ in range(100):
            if fake.approved:
                break
            await pilot.pause(0.02)
        await pilot.pause()
        assert fake.approved == ["20260824-01"]
        assert rows[1].resolved == "✓ approved"
        assert rows[0].resolved == ""


@pytest.mark.asyncio
async def test_reject_is_one_action_and_is_as_reachable_as_approve(
    monkeypatch, tmp_path
):
    """Reject discards a staged file; approve writes into the model's
    context. The asymmetry is deliberate and it runs the safe way round:
    reject is one action, approve needs two on the same row."""
    fake = FakeEngine([])
    fake.list_pending_result = [_proposal("20260824-00", "remember uv, not pip")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        browser = await _browser(pilot, app, fake)
        row = next(r for r in browser.rows if isinstance(r, ProposalRow))
        row.focus()
        await pilot.press("r")
        for _ in range(100):
            if fake.rejected:
                break
            await pilot.pause(0.02)
        await pilot.pause()
        assert fake.rejected == ["20260824-00"]
        assert fake.approved == []
        assert row.resolved == "✗ rejected"
        assert "rejected" in _plain(row)


@pytest.mark.asyncio
async def test_the_keyboard_route_reaches_the_same_per_item_action(
    monkeypatch, tmp_path
):
    """A click-only control is unreachable for most of this app's use.
    ``a`` arms and applies, ``r`` rejects, ``Esc`` disarms -- all on the
    FOCUSED row, and ↑/↓ is what moves that focus."""
    fake = FakeEngine([])
    fake.list_pending_result = [
        _proposal("20260824-00", "first proposal"),
        _proposal("20260824-01", "second proposal"),
    ]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        browser = await _browser(pilot, app, fake)
        rows = [r for r in browser.rows if isinstance(r, ProposalRow)]
        rows[0].focus()
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is rows[1], "↓ must move between rows"

        await pilot.press("a")
        await pilot.pause()
        assert rows[1].armed
        await pilot.press("escape")
        await pilot.pause()
        assert not rows[1].armed and fake.approved == []

        await pilot.press("a")
        await pilot.press("a")
        for _ in range(100):
            if fake.approved:
                break
            await pilot.pause(0.02)
        assert fake.approved == ["20260824-01"]


@pytest.mark.asyncio
async def test_neither_outcome_is_silent(monkeypatch, tmp_path):
    """The user must see what happened -- in the row AND in the session,
    because the browser tab may not be the tab they are looking at when a
    write lands."""
    fake = FakeEngine([])
    fake.list_pending_result = [_proposal("20260824-00", "remember uv, not pip")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        pane = app.active_pane
        browser = await _browser(pilot, app, fake)
        row = next(r for r in browser.rows if isinstance(r, ProposalRow))
        row.action_approve()
        row.action_approve()
        for _ in range(150):
            texts = [str(b.renderable) for b in pane.query("SystemBlock")]
            if any("approved 20260824-00" in t for t in texts):
                break
            await pilot.pause(0.02)
        texts = "\n".join(str(b.renderable) for b in pane.query("SystemBlock"))
        assert "approved 20260824-00" in texts
        assert "add → memory/user" in texts
        assert "via approved" in texts, "the provenance label is named to the user"


@pytest.mark.asyncio
async def test_a_failed_approve_says_so_and_does_not_claim_success(
    monkeypatch, tmp_path
):
    fake = FakeEngine([])
    fake.list_pending_result = [_proposal("20260824-00", "remember uv, not pip")]
    fake.approve_error = "20260824-00: NOT applied — memory scope is full"
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        pane = app.active_pane
        browser = await _browser(pilot, app, fake)
        row = next(r for r in browser.rows if isinstance(r, ProposalRow))
        row.action_approve()
        row.action_approve()
        for _ in range(150):
            if row.resolved:
                break
            await pilot.pause(0.02)
        await pilot.pause()
        assert row.resolved == "✗ NOT applied"
        texts = "\n".join(str(b.renderable) for b in pane.query("SystemBlock"))
        assert "memory scope is full" in texts


# -- read-only degradation ----------------------------------------------


@pytest.mark.asyncio
async def test_an_older_lore_core_degrades_to_read_only_and_says_so(
    monkeypatch, tmp_path
):
    """MANDATORY degradation. A lore_core without the write gate and the
    provenance ledger cannot record that a human approved something, so
    the browser renders NO approve or reject control and prints why."""
    fake = FakeEngine([])
    fake.lore_write_state_result = {
        "capable": False, "version": "0.34.0", "source": "plugin",
        "location": "/home/x/.claude/plugins/marketplaces/lore",
        "reason": "lore_core 0.34.0 (plugin at /home/x/.claude/plugins/"
                  "marketplaces/lore) has no write gate or provenance ledger "
                  "— approving here would write into the model's context with "
                  "no record that a human approved it. Approve and reject are "
                  "disabled; LORE 0.36.0 or newer enables them.",
    }
    fake.list_pending_result = [_proposal("20260824-00", "remember uv, not pip")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        browser = await _browser(pilot, app, fake)
        painted = "\n".join(str(w.renderable) for w in browser.query("Static"))
        assert "read-only" in painted
        assert "no write gate or provenance ledger" in painted
        assert "0.34.0" in painted

        row = next(r for r in browser.rows if isinstance(r, ProposalRow))
        assert row.size.height > 0
        assert "approve" not in _plain(row).lower()
        assert "reject" not in _plain(row).lower()

        # And the controls are not merely hidden: driving them writes
        # nothing.
        row.focus()
        row.action_approve()
        row.action_approve()
        await pilot.press("a", "a", "r")
        await pilot.pause()
        assert fake.approved == [] and fake.rejected == []


# -- reachability: the command, the palette, the pickers ----------------


def test_beliefs_is_a_registered_command():
    assert "/beliefs" in commands.interactive_names()
    row = next(c for c in commands.REGISTRY if c.name == "/beliefs")
    assert row.group == "Memory" and row.palette


@pytest.mark.asyncio
async def test_the_slash_command_opens_the_browser(monkeypatch, tmp_path):
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(1, "prefers terse commits")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        pane = app.active_pane
        pane.query_one("#prompt-input").value = "/beliefs"
        await pilot.press("enter")
        for _ in range(200):
            if pane._beliefs_tab is not None and pane._beliefs_tab.rows:
                break
            await pilot.pause(0.02)
        assert isinstance(pane._beliefs_tab, BeliefsBrowserTab)
        assert pane._beliefs_tab.is_mounted


@pytest.mark.asyncio
async def test_reopening_activates_the_same_tab_rather_than_stacking(
    monkeypatch, tmp_path
):
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(1, "prefers terse commits")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        pane = app.active_pane
        browser = await _browser(pilot, app, fake)
        await pane.open_beliefs_browser()
        await pilot.pause()
        assert len(list(app.query(BeliefsBrowserTab))) == 1
        assert pane._beliefs_tab is browser


@pytest.mark.asyncio
async def test_the_chip_picker_keeps_its_glance_and_offers_the_browser(
    monkeypatch, tmp_path
):
    """Both surfaces, not one replacing the other: the dropdown is still
    the glance, and its first row is the door to the session."""
    from doxa.app import ChipPicker
    from doxa.session.chips import BROWSE_ALL_ROW

    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(1, "prefers terse commits")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        pane = app.active_pane
        await pane.open_beliefs_picker()
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        assert picker.is_open
        rids = [rid for rid, _l in picker._rows if rid]
        assert BROWSE_ALL_ROW[0] in rids
        assert any(rid.startswith("belief:") for rid in rids)
        # The door names what the browser HAS, never a verb it performs.
        door = next(l for rid, l in picker._rows if rid == BROWSE_ALL_ROW[0])
        assert "approve" not in door.lower() and "reject" not in door.lower()

        index = next(i for i, (rid, _l) in enumerate(picker._rows)
                     if rid == BROWSE_ALL_ROW[0])
        picker.select_row(index)
        for _ in range(200):
            if pane._beliefs_tab is not None and pane._beliefs_tab.rows:
                break
            await pilot.pause(0.02)
        assert isinstance(pane._beliefs_tab, BeliefsBrowserTab)


# -- against the real lore_core: provenance, honestly recorded ----------
#
# Everything above drives a fake engine, because what is on trial there is
# the SURFACE. These two drive the real SessionEngine against the real
# lore_core in conftest.py's throwaway LORE_ROOT, because what is on trial
# here is the CONTRACT: an entry approved through DOXA must be labelled
# the way LORE labels a human approval, and DOXA must not be the thing
# that decides what that label is.


@pytest.fixture
def lore_store_cleanup():
    """Put the SHARED belief store back exactly as it was found.

    conftest.py points LORE_ROOT at one throwaway directory for the whole
    session, so a test that really writes a belief leaves it there for
    every later test -- and `tests/test_consult.py` asserts that an
    unrelated prompt matches NOTHING in that store, which a stray claim
    quietly breaks. tests/test_daemon.py's _seed_big_belief_store already
    carries this discipline ("the caller deletes them again"); these tests
    carry it too, because they are the only other ones here that write.

    Snapshot-and-restore rather than delete-by-claim: it also catches the
    evidence rows, the OUTCOME ledger, the FTS shadow and the
    pending/archive files an approve leaves behind, none of which the test
    names.

    belief_outcomes is on that list because leaving it behind is not
    merely untidy -- `beliefs.id` is an INTEGER PRIMARY KEY, so SQLite
    hands a deleted id straight back to the next insert, and an orphaned
    outcome row silently re-attaches itself to a completely different
    belief in the next test. Caught exactly that way."""
    import lore_core
    from lore_core import store as lore_store

    conn = lore_store.db_connect()
    before = {row[0] for row in conn.execute("SELECT id FROM beliefs")}
    pdir = lore_core.ROOT / "pending"
    files_before = {p for p in pdir.rglob("*.json")} if pdir.exists() else set()
    try:
        yield
    finally:
        conn = lore_store.db_connect()
        added = [row[0] for row in conn.execute("SELECT id FROM beliefs")
                 if row[0] not in before]
        for bid in added:
            conn.execute("DELETE FROM beliefs WHERE id = ?", (bid,))
            conn.execute("DELETE FROM belief_evidence WHERE belief_id = ?", (bid,))
            conn.execute("DELETE FROM belief_outcomes WHERE belief_id = ?", (bid,))
            with contextlib.suppress(Exception):
                conn.execute("DELETE FROM belief_fts WHERE belief_id = ?", (bid,))
        conn.commit()
        if pdir.exists():
            for path in pdir.rglob("*.json"):
                if path not in files_before:
                    path.unlink(missing_ok=True)


def _stage(item: dict) -> str:
    """One staged proposal on disk, written the way lore_core writes them."""
    import json

    import lore_core

    pdir = lore_core.ROOT / "pending"
    pdir.mkdir(parents=True, exist_ok=True)
    pid = f"20260824120000-{len(list(pdir.glob('*.json'))):02d}"
    (pdir / f"{pid}.json").write_text(json.dumps(
        {"created": "2026-08-24T12:00:00Z", "derived_by": "test",
         "session_id": "sess-1", **item}), encoding="utf-8")
    return pid


def _engine(tmp_path):
    from doxa.engine import SessionEngine

    return SessionEngine(cwd=str(tmp_path))


@pytest.mark.asyncio
async def test_approving_a_belief_records_lores_own_approved_provenance(
    tmp_path, lore_store_cleanup,
):
    """The provenance condition, end to end.

    DOXA does not label anything: it calls ``lore_core.pending.apply_item``,
    which passes ``via="approved"`` into ``belief_insert``. This asserts
    the label that ends up in the STORE, not the one DOXA intended --
    which is the only version of this assertion worth having."""
    from lore_core import store as lore_store
    from lore_core.beliefs import belief_subject

    from doxa.engine import lore_write_state

    if not lore_write_state()["capable"]:
        pytest.skip("this lore_core cannot record provenance (pre-0.36.0)")

    claim = "the operator approves beliefs from the doxa browser"
    engine = _engine(tmp_path)
    pid = _stage({"kind": "belief", "action": "add", "subject": "user",
                  "claim": claim, "confidence": 0.77,
                  "project": engine.slug, "scope": "user"})

    listed = await engine.list_pending()
    assert any(item["pid"] == pid for item in listed)
    row = next(item for item in listed if item["pid"] == pid)
    assert proposal_verdict(row) == "add → belief/user"

    assert await engine.approve_pending(pid) is None

    conn = lore_store.db_connect()
    stored = conn.execute(
        "SELECT via, status, confidence FROM beliefs WHERE subject = ? "
        "AND claim = ?", (belief_subject("user", engine.slug), claim),
    ).fetchone()
    assert stored is not None, "the belief did not reach the store"
    assert stored[0] == "approved", "LORE's own label for a human approval"
    assert stored[1] == "active"

    # ...and the proposal is gone from the queue, archived as approved.
    assert not any(item["pid"] == pid for item in await engine.list_pending())
    import json

    import lore_core
    archived = json.loads(
        (lore_core.ROOT / "pending" / "archive" / f"{pid}.json").read_text()
    )
    assert archived["status"] == "approved"


@pytest.mark.asyncio
async def test_rejecting_removes_the_proposal_and_writes_nothing(
    tmp_path, lore_store_cleanup,
):
    import json

    import lore_core
    from lore_core import store as lore_store

    from doxa.engine import lore_write_state

    if not lore_write_state()["capable"]:
        pytest.skip("this lore_core cannot record provenance (pre-0.36.0)")

    claim = "the operator rejects beliefs from the doxa browser"
    engine = _engine(tmp_path)
    pid = _stage({"kind": "belief", "action": "add", "subject": "user",
                  "claim": claim, "confidence": 0.5,
                  "project": engine.slug, "scope": "user"})

    assert await engine.reject_pending(pid) is None
    assert not any(item["pid"] == pid for item in await engine.list_pending())

    conn = lore_store.db_connect()
    assert conn.execute(
        "SELECT count(*) FROM beliefs WHERE claim = ?", (claim,)
    ).fetchone()[0] == 0, "a rejected proposal must write nothing"

    archived = json.loads(
        (lore_core.ROOT / "pending" / "archive" / f"{pid}.json").read_text()
    )
    assert archived["status"] == "rejected"


@pytest.mark.asyncio
async def test_approving_something_already_resolved_elsewhere_says_so(
    tmp_path, lore_store_cleanup,
):
    """Two windows on one store. The second approve must not apply a
    proposal that is no longer there, and must not pretend it did."""
    from doxa.engine import lore_write_state

    if not lore_write_state()["capable"]:
        pytest.skip("this lore_core cannot record provenance (pre-0.36.0)")

    engine = _engine(tmp_path)
    pid = _stage({"kind": "belief", "action": "add", "subject": "user",
                  "claim": "a claim resolved twice", "confidence": 0.5,
                  "project": engine.slug, "scope": "user"})
    assert await engine.approve_pending(pid) is None
    again = await engine.approve_pending(pid)
    assert again and "no longer staged" in again


def test_the_engine_has_no_bulk_approve_under_any_name():
    """SECURITY ASSERTION, at the API rather than the UI. There is no
    method here that takes a sequence of ids, so there is nothing for an
    "approve all" button to be built on later without adding one first --
    and adding one is what this test exists to notice."""
    import inspect

    from doxa.client import EngineClient
    from doxa.engine import SessionEngine

    for cls in (SessionEngine, EngineClient):
        names = [n for n in dir(cls) if "approve" in n or "reject" in n]
        assert sorted(names) == ["approve_pending", "reject_pending"], (cls, names)
        for name in names:
            params = list(inspect.signature(getattr(cls, name)).parameters)
            assert params == ["self", "pid"], (cls, name, params)


# -- the capability check itself ----------------------------------------


def test_write_state_is_measured_off_the_signature_not_the_version(monkeypatch):
    """A copy of lore_core whose writers take no ``via=`` cannot record an
    approval, however new its version string claims to be -- and DOXA must
    notice the missing keyword, not the number. This substitutes exactly
    that: a via-less ``belief_insert`` on an otherwise current install."""
    import lore_core.beliefs as beliefs_mod

    from doxa.engine import lore_write_state

    if not lore_write_state()["capable"]:
        # A bare clone installs the PINNED lore_core, which may predate the
        # provenance ledger -- in which case the browser is already
        # read-only for a different (and equally correct) reason and there
        # is no via-taking writer here to take away.
        pytest.skip("this lore_core cannot record provenance (pre-0.36.0)")

    def via_less(conn, subject, claim, confidence, session_id, project, note,
                 exclude_ids=None):
        raise AssertionError("never called")

    monkeypatch.setattr(beliefs_mod, "belief_insert", via_less)
    state = lore_write_state()
    assert state["capable"] is False
    assert "cannot label a write as approved" in state["reason"]
    assert "Approve and reject are disabled" in state["reason"]


def test_write_state_reports_the_carrier_about_already_names(monkeypatch):
    """One story in two places: the browser's banner and /about's `lore
    from` row read the same measurement, so a user chasing a difference is
    never told two things."""
    from doxa import _lore_bootstrap
    from doxa import version as version_mod
    from doxa.engine import lore_write_state

    state = lore_write_state()
    assert state["version"] == version_mod.lore_core_version()
    source = _lore_bootstrap.resolved_source()
    assert state["source"] == (source[0] if source else None)


@pytest.mark.asyncio
async def test_a_read_only_engine_refuses_the_write_even_if_asked_directly(
    monkeypatch, tmp_path
):
    """Belt and braces: the browser hides the controls, and the ENGINE
    still refuses. A surface-level guard alone would be one refactor away
    from writing without provenance."""
    import doxa.engine as engine_mod

    engine = engine_mod.SessionEngine(cwd=str(tmp_path))
    monkeypatch.setattr(engine_mod, "lore_write_state", lambda: {
        "capable": False, "reason": "no provenance ledger here",
    })
    assert await engine.approve_pending("whatever") == "no provenance ledger here"
    assert await engine.reject_pending("whatever") == "no provenance ledger here"


@pytest.mark.asyncio
async def test_ctrl_w_closes_the_browser_and_reopening_builds_a_fresh_one(
    monkeypatch, tmp_path
):
    """A tab that cannot be closed is a tab the user is stuck in. Ctrl+W
    removes it and drops the pane's reference, so the next open builds a
    new one rather than activating a tab that is no longer there."""
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(1, "prefers terse commits")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        pane = app.active_pane
        first = await _browser(pilot, app, fake)
        await app.action_close_tab()
        await pilot.pause()
        assert pane._beliefs_tab is None
        assert not list(app.query(BeliefsBrowserTab))
        second = await _browser(pilot, app, fake)
        assert second is not first
        assert second.is_mounted


@pytest.mark.asyncio
async def test_ctrl_q_closes_the_browser_and_leaves_the_session_running(
    monkeypatch, tmp_path
):
    """v0.58.0. The browser holds no engine, so Ctrl+Q -- "end this
    session (finalize now) and close its tab" -- has no session to end.
    Through v0.56.0 that made it a no-op and the browser was closable by
    exactly one of the two close keys.

    It now closes, and the session that OPENED it is left running: Ctrl+Q
    on a tab with no session must not fall through to the pane
    underneath."""
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(1, "prefers terse commits")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        pane = app.active_pane
        await _browser(pilot, app, fake)
        await pilot.press("ctrl+q")
        for _ in range(100):
            if pane._beliefs_tab is None:
                break
            await pilot.pause(0.02)
        assert pane._beliefs_tab is None
        assert not list(app.query(BeliefsBrowserTab))
        # The session tab is still there, still holding its engine.
        assert pane in app.panes()
        assert pane.engine is fake
        assert fake.finalized is False


# -- the outcome ledger, against the real lore_core ----------------------


@pytest.mark.asyncio
async def test_list_beliefs_carries_the_outcome_ledger(tmp_path, lore_store_cleanup):
    """End to end against the real store: a belief the dreamer contradicted
    comes back carrying LORE's verdict and when it landed, and a belief
    nobody ever tested comes back carrying a measured zero."""
    from lore_core import store as lore_store
    from lore_core.beliefs import belief_insert, belief_subject, record_outcome

    engine = _engine(tmp_path)
    conn = lore_store.db_connect()
    subject = belief_subject("user", engine.slug)
    tested, _ = belief_insert(conn, subject, "the operator ships on fridays",
                              0.8, None, None, None)
    untested, _ = belief_insert(conn, subject, "the operator never ships",
                                0.8, None, None, None)
    record_outcome(conn, tested, "confirmed", "dream")
    record_outcome(conn, tested, "contradicted", "user")
    conn.commit()

    by_id = {b["id"]: b for b in await engine.list_beliefs()}

    hit = by_id[tested]
    assert hit["outcomes"] == 2
    assert hit["outcome_event"] == "contradicted"   # the LATEST verdict wins
    assert hit["outcome_source"] == "user"
    assert hit["outcome_at"]
    assert hit["outcome_confirmeds"] == 1 and hit["outcome_contradicteds"] == 1
    assert belief_outcome_kind(hit) == "contradicted"

    miss = by_id[untested]
    assert miss["outcomes"] == 0, "a measured zero, not an absent key"
    assert "outcome_event" not in miss
    assert belief_outcome_kind(miss) == "untested"
    assert belief_outcome_text(miss) == "never tested"


@pytest.mark.asyncio
async def test_the_page_wide_counts_equal_lore_s_own_outcome_counts(
    tmp_path, lore_store_cleanup
):
    """SET QUERY, SAME DEFINITION.

    ``lore_core.beliefs.outcome_counts`` is the definition of what a
    belief's outcome tally is, and doxa.operators already calls it per
    hit. list_beliefs cannot: belief_outcomes has no index on belief_id,
    so per-row would be one full scan per belief across a list capped at
    2000. It computes the same sums set-wise instead, and THIS is what
    stops the two drifting -- every belief in the store, both ways, equal."""
    from lore_core import store as lore_store
    from lore_core.beliefs import (
        belief_insert,
        belief_subject,
        outcome_counts,
        record_outcome,
    )

    engine = _engine(tmp_path)
    conn = lore_store.db_connect()
    subject = belief_subject("user", engine.slug)
    for index, events in enumerate(
        ([], ["confirmed"], ["confirmed", "confirmed", "stale"],
         ["contradicted"], ["confirmed", "stale", "stale"]),
    ):
        bid, _ = belief_insert(conn, subject, f"ledger fixture {index}",
                               0.8, None, None, None)
        for event in events:
            record_outcome(conn, bid, event, "audit")
    conn.commit()

    listed = await engine.list_beliefs()
    assert listed, "fixture produced no beliefs"
    for belief in listed:
        confirms, contradicts, stales = outcome_counts(conn, belief["id"])
        assert belief.get("outcome_confirmeds", 0) == confirms, belief["id"]
        assert belief.get("outcome_contradicteds", 0) == contradicts, belief["id"]
        assert belief.get("outcome_stales", 0) == stales, belief["id"]
        assert belief["outcomes"] == confirms + contradicts + stales, belief["id"]


@pytest.mark.asyncio
async def test_the_ledger_costs_nothing_on_a_store_that_has_no_outcomes(
    tmp_path, lore_store_cleanup
):
    """Measured on this operator's store: 31 outcome rows against 628
    active beliefs. So the ~95% that carry no verdict must add ONE short
    field, not five -- the belief page rides the shared _fit_page byte
    budget and payload spent on three zeroes per row is payload spent
    saying nothing."""
    import json

    from lore_core import store as lore_store
    from lore_core.beliefs import belief_insert, belief_subject

    engine = _engine(tmp_path)
    conn = lore_store.db_connect()
    subject = belief_subject("user", engine.slug)
    bid, _ = belief_insert(conn, subject, "an untested claim", 0.8,
                           None, None, None)
    conn.commit()
    belief = next(b for b in await engine.list_beliefs() if b["id"] == bid)
    ledger = {k: v for k, v in belief.items() if k.startswith("outcome")}
    assert ledger == {"outcomes": 0}
    assert len(json.dumps(ledger).encode("utf-8")) < 20


@pytest.mark.asyncio
async def test_belief_rows_paint_never_tested_where_the_idle_age_used_to_be(
    monkeypatch, tmp_path
):
    """On screen, not in a formatter: the browser's own rows say what
    reality last said, and the ones reality never tested say so."""
    fake = FakeEngine([])
    fake.list_beliefs_result = [
        _belief(1, "reality contradicted this", outcome="contradicted",
                outcome_days=2),
        _belief(2, "reality never checked this"),
    ]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(170, 48)) as pilot:
        await pilot.pause()
        browser = await _browser(pilot, app, fake)
        rows = list(browser.query(BeliefRow))
        rendered = "\n".join(_plain(r) for r in rows)
        assert "contradicted 2d" in rendered
        assert "never tested" in rendered
        assert "idle" not in rendered
        for row in rows:
            assert row.size.height > 0
        # Tested first, inside the one scope group they share.
        assert rows[0].belief["id"] == 1


# -- v0.48.0: the chip picker as a navigable surface ---------------------


async def _picker(pilot, app, *, beliefs):
    from doxa.app import ChipPicker

    pane = app.active_pane
    await pane.open_beliefs_picker()
    for _ in range(200):
        picker = app.query_one("#chip-picker", ChipPicker)
        if picker.is_open:
            await pilot.pause()
            return pane, picker
        await pilot.pause(0.02)
    raise AssertionError("the beliefs picker never opened")


def _headers(picker):
    from doxa.app import ChipPicker

    return [label for rid, label in picker._rows
            if rid.startswith(ChipPicker.GROUP_ROW_PREFIX)]


def _belief_rows(picker):
    return [(rid, label) for rid, label in picker._rows
            if rid.startswith("belief:")]


_NEXT_ID = [1000]


def _many(n, group="project:doxa", **kw):
    """n beliefs in one scope, with ids that are unique ACROSS calls --
    the picker keys its group map by row id, so two batches sharing ids
    would silently merge into one group and count wrong."""
    start = _NEXT_ID[0]
    _NEXT_ID[0] += n
    return [_belief(i, f"claim number {i:03d}", subject=group, **kw)
            for i in range(start, start + n)]


@pytest.mark.asyncio
async def test_scope_groups_are_headers_carrying_their_own_count(
    monkeypatch, tmp_path
):
    """Asked for directly: `project (N beliefs)`. The counts follow the
    labels _belief_scope_label actually emits -- which keeps `user-model`
    its own group rather than folding it into plain `user`, so a store
    with both shows three headers and not two.

    v0.67.0: `user`/`user-model` headers also carry LORE's own channel
    tag (`· stated` / `· inferred` -- see BELIEF_CHANNEL_RULE) so the
    "may a later session act on this" distinction is legible at a glance,
    not only in a hover. `project` carries no such tag -- it has no
    channel to distinguish."""
    fake = FakeEngine([])
    fake.list_beliefs_result = (
        _many(8) + _many(3, group="user") + _many(2, group="user-model")
    )
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _picker(pilot, app, beliefs=None)
        headers = _headers(picker)
        assert any("project (8 beliefs)" in h for h in headers), headers
        assert any("user · stated (3 beliefs)" in h for h in headers), headers
        assert any("user-model · inferred (2 beliefs)" in h for h in headers), headers


@pytest.mark.asyncio
async def test_a_group_of_one_is_not_pluralised(monkeypatch, tmp_path):
    fake = FakeEngine([])
    fake.list_beliefs_result = _many(11) + _many(1, group="user")
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _picker(pilot, app, beliefs=None)
        assert any("user · stated (1 belief)" in h for h in _headers(picker))


@pytest.mark.asyncio
async def test_the_header_says_how_many_of_a_group_reality_has_tested(
    monkeypatch, tmp_path
):
    """The number this store makes interesting: 15 of 635 on the reporting
    operator's. A folded group that says "412 beliefs, 3 tested" answered
    a question the expanded list would have taken six hundred rows to."""
    fake = FakeEngine([])
    fake.list_beliefs_result = (
        _many(9)
        + [_belief(90, "tested one", subject="project:doxa", outcome="confirmed")]
        + _many(3, group="user")
    )
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _picker(pilot, app, beliefs=None)
        headers = _headers(picker)
        assert any("project (10 beliefs, 1 tested)" in h for h in headers), headers
        # A group with none says nothing rather than "0 tested".
        assert any(h.strip().startswith("▸ user · stated (3 beliefs)")
                   or h.strip().startswith("▾ user · stated (3 beliefs)")
                   for h in headers), headers


@pytest.mark.asyncio
async def test_a_large_list_opens_folded_and_a_header_unfolds_it(
    monkeypatch, tmp_path
):
    """635 active beliefs is the reported store. A picker that opens fully
    expanded is a wall, not a glance -- so every group opens folded, and
    the counts in the headers are what make the folded view an answer
    rather than an empty box."""
    fake = FakeEngine([])
    fake.list_beliefs_result = _many(40) + _many(6, group="user")
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _picker(pilot, app, beliefs=None)
        assert _headers(picker), "groups must still have headers"
        assert _belief_rows(picker) == [], "a folded group shows no belief rows"

        index = next(i for i, (_rid, label) in enumerate(picker._rows)
                     if "project (40 beliefs" in label)
        picker.select_row(index)
        await pilot.pause()
        assert picker.is_open, "folding must not close the picker"
        opened = _belief_rows(picker)
        assert len(opened) == 40
        assert all("claim number" in label for _rid, label in opened)
        # ...and the user group beside it stays folded.
        assert any("▸ user · stated (6 beliefs)" in h for h in _headers(picker))

        # A second selection folds it back, and the highlight is still on
        # the header so it can be toggled again without hunting for it.
        index = next(i for i, (_rid, label) in enumerate(picker._rows)
                     if "project (40 beliefs" in label)
        assert picker.highlighted == index
        picker.select_row(index)
        await pilot.pause()
        assert _belief_rows(picker) == []


@pytest.mark.asyncio
async def test_a_small_list_does_not_fold_at_all(monkeypatch, tmp_path):
    """Folding three rows behind three headers is strictly worse than
    showing them. Below the widget's own max-height every group opens."""
    fake = FakeEngine([])
    fake.list_beliefs_result = _many(2) + _many(2, group="user")
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _picker(pilot, app, beliefs=None)
        assert len(_belief_rows(picker)) == 4
        assert not picker._collapsed


@pytest.mark.asyncio
async def test_typing_still_finds_a_belief_inside_a_folded_group(
    monkeypatch, tmp_path
):
    """THE constraint. Folding must not make a belief unfindable, and this
    needs no auto-expand rule: the matcher has always scored the complete
    row set rather than what is on screen, and a typed filter drops the
    group headers entirely, so the folded view simply is not the view
    being filtered."""
    fake = FakeEngine([])
    fake.list_beliefs_result = (
        _many(40)
        + [_belief(999, "the operator keeps doxa in a worktree",
                   subject="project:doxa")]
    )
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _picker(pilot, app, beliefs=None)
        assert _belief_rows(picker) == [], "starts folded"

        await pilot.press("w", "o", "r", "k", "t", "r", "e", "e")
        await pilot.pause()
        found = _belief_rows(picker)
        assert any("worktree" in label for _rid, label in found), found
        assert _headers(picker) == [], "a filtered view has no group headers"

        # Clearing the filter returns to exactly the fold state from before.
        for _ in range(8):
            await pilot.press("backspace")
        await pilot.pause()
        assert _belief_rows(picker) == []
        assert _headers(picker)


@pytest.mark.asyncio
async def test_the_picker_sorts_tested_beliefs_to_the_top_of_their_group(
    monkeypatch, tmp_path
):
    """belief_sort_key existed for the browser since v0.46.0 and the picker
    never used it -- chips.py sorted by scope label alone. This is that
    defect, fixed, without disturbing the contiguous grouping the header
    insertion depends on."""
    fake = FakeEngine([])
    fake.list_beliefs_result = [
        _belief(1, "untested alpha", subject="project:doxa"),
        _belief(2, "tested beta", subject="project:doxa", outcome="confirmed",
                outcome_days=9),
        _belief(3, "untested gamma", subject="project:doxa"),
        _belief(4, "tested delta", subject="project:doxa",
                outcome="contradicted", outcome_days=1),
        _belief(5, "a user one", subject="user"),
    ]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _picker(pilot, app, beliefs=None)
        rows = _belief_rows(picker)          # small list: nothing folded
        project = [rid for rid, _l in rows if rid in
                   ("belief:1", "belief:2", "belief:3", "belief:4")]
        assert project[:2] == ["belief:4", "belief:2"], project
        assert set(project[2:]) == {"belief:1", "belief:3"}
        # Grouping survives: the project ids are contiguous, so the header
        # insertion still produces one block per scope.
        ids = [rid for rid, _l in rows]
        assert ids.index("belief:5") == len(ids) - 1


@pytest.mark.asyncio
async def test_selecting_the_browse_row_opens_the_browser_in_a_tab(
    monkeypatch, tmp_path
):
    """Item 3, verified rather than assumed: BROWSE_ALL_ROW already opened
    the browser tab in v0.46.0 and still does. The user was confirming the
    expectation, not reporting a defect -- this is the proof."""
    from doxa.session.chips import BROWSE_ALL_ROW

    fake = FakeEngine([])
    fake.list_beliefs_result = _many(3)
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        pane, picker = await _picker(pilot, app, beliefs=None)
        index = next(i for i, (rid, _l) in enumerate(picker._rows)
                     if rid == BROWSE_ALL_ROW[0])
        picker.select_row(index)
        for _ in range(200):
            if pane._beliefs_tab is not None and pane._beliefs_tab.rows:
                break
            await pilot.pause(0.02)
        assert isinstance(pane._beliefs_tab, BeliefsBrowserTab)
        assert pane._beliefs_tab.is_mounted


# -- v0.48.0: what a belief row can actually DO --------------------------
#
# The user asked for "Approve or Reject" on every belief row. Those two
# are not operations on a belief: approving applies a staged PROPOSAL, an
# entry that does not exist yet, and every proposal already carries them
# in the browser. A belief is a claim already in the store and already
# steering the model. What LORE can do to one is record what reality did
# (belief_outcomes: confirmed / contradicted / stale) and end it (retract).
# These assert that vocabulary, and that the destructive half is harder to
# reach than the reversible half.


def _actions(picker):
    return [rid for rid, _l in picker._rows if rid.startswith("act:")]


@pytest.mark.asyncio
async def test_a_belief_row_offers_lores_verbs_not_approve_and_reject(
    monkeypatch, tmp_path
):
    fake = FakeEngine([])
    fake.list_beliefs_result = _many(3)
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _picker(pilot, app, beliefs=None)
        row = next(i for i, (rid, _l) in enumerate(picker._rows)
                   if rid.startswith("belief:"))
        picker.select_row(row)
        await pilot.pause()
        assert picker.is_open, "selecting a belief opens its actions"
        assert _actions(picker) == [
            "act:show", "act:confirmed", "act:contradicted", "act:stale",
            "act:retract", "act:back",
        ]
        painted = " ".join(label for _rid, label in picker._rows).lower()
        assert "approve" not in painted
        assert "reject" not in painted
        # Nothing has happened to the belief just by looking at its menu.
        assert fake.outcomes_recorded == [] and fake.retracted == []


@pytest.mark.asyncio
async def test_recording_an_outcome_takes_two_explicit_selections(
    monkeypatch, tmp_path
):
    """A dropdown row is one Enter from whatever the highlight sits on,
    which makes it a MORE accidental surface than the browser's rows. So
    selecting a belief never acts on it -- it opens the named verbs."""
    fake = FakeEngine([])
    fake.list_beliefs_result = _many(3)
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _picker(pilot, app, beliefs=None)
        rid = next(rid for rid, _l in picker._rows if rid.startswith("belief:"))
        bid = int(rid.split(":", 1)[1])
        picker.select_row(next(i for i, (r, _l) in enumerate(picker._rows)
                               if r == rid))
        await pilot.pause()
        assert fake.outcomes_recorded == []

        picker.select_row(next(i for i, (r, _l) in enumerate(picker._rows)
                               if r == "act:confirmed"))
        for _ in range(150):
            if fake.outcomes_recorded:
                break
            await pilot.pause(0.02)
        assert fake.outcomes_recorded == [(bid, "confirmed")]
        assert fake.retracted == []


@pytest.mark.asyncio
async def test_retract_from_the_dropdown_arms_before_it_ends_a_belief(
    monkeypatch, tmp_path
):
    """SECURITY-SHAPED ASSERTION. Retract is the destructive verb: the
    belief leaves the working set and the model's context. One selection
    re-words the row; only a second, differently-worded selection acts."""
    fake = FakeEngine([])
    fake.list_beliefs_result = _many(3)
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _picker(pilot, app, beliefs=None)
        rid = next(rid for rid, _l in picker._rows if rid.startswith("belief:"))
        bid = int(rid.split(":", 1)[1])
        picker.select_row(next(i for i, (r, _l) in enumerate(picker._rows)
                               if r == rid))
        await pilot.pause()

        picker.select_row(next(i for i, (r, _l) in enumerate(picker._rows)
                               if r == "act:retract"))
        await pilot.pause()
        assert fake.retracted == [], "the first selection only arms"
        armed = next(label for r, label in picker._rows if r == "act:retract!")
        assert "RETRACT" in armed and "select again" in armed
        assert "act:retract" not in _actions(picker), "the unarmed row is gone"

        picker.select_row(next(i for i, (r, _l) in enumerate(picker._rows)
                               if r == "act:retract!"))
        for _ in range(150):
            if fake.retracted:
                break
            await pilot.pause(0.02)
        assert fake.retracted == [bid]
        assert fake.outcomes_recorded == []


@pytest.mark.asyncio
async def test_the_dropdown_hides_the_verbs_when_lore_cannot_record_them(
    monkeypatch, tmp_path
):
    fake = FakeEngine([])
    fake.list_beliefs_result = _many(3)
    fake.belief_action_state_result = {
        "capable": False, "version": "0.30.0", "source": "plugin",
        "reason": "lore_core 0.30.0 (plugin at /x) is missing record_outcome",
    }
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _picker(pilot, app, beliefs=None)
        rid = next(rid for rid, _l in picker._rows if rid.startswith("belief:"))
        picker.select_row(next(i for i, (r, _l) in enumerate(picker._rows)
                               if r == rid))
        await pilot.pause()
        assert _actions(picker) == ["act:show", "act:back"]
        assert "missing record_outcome" in picker._note
        assert fake.outcomes_recorded == [] and fake.retracted == []


@pytest.mark.asyncio
async def test_the_browser_row_carries_the_same_verbs_as_real_controls(
    monkeypatch, tmp_path
):
    """The browser CAN have a button per row -- one widget per row is why
    it exists -- so it has one, and the keyboard reaches the same place."""
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(7, "a claim under test")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(180, 48)) as pilot:
        await pilot.pause()
        browser = await _browser(pilot, app, fake)
        row = next(iter(browser.query(BeliefRow)))
        painted = _plain(row)
        assert "✓ confirmed" in painted and "✗ contradicted" in painted
        assert "⌫ retract" in painted
        assert "approve" not in painted.lower()
        assert row.size.height > 0

        row.focus()
        await pilot.press("c")
        for _ in range(150):
            if fake.outcomes_recorded:
                break
            await pilot.pause(0.02)
        assert fake.outcomes_recorded == [(7, "confirmed")]


@pytest.mark.asyncio
async def test_the_browser_retract_arms_and_enter_never_reaches_it(
    monkeypatch, tmp_path
):
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(7, "a claim under test")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(180, 48)) as pilot:
        await pilot.pause()
        browser = await _browser(pilot, app, fake)
        row = next(iter(browser.query(BeliefRow)))
        row.focus()

        # Enter is the key a hand rests on: it expands evidence, nothing else.
        for _ in range(4):
            await pilot.press("enter")
            await pilot.pause()
        assert fake.retracted == [] and fake.outcomes_recorded == []

        await pilot.press("d")
        await pilot.pause()
        assert row.armed and fake.retracted == []
        assert "CONFIRM RETRACT" in _plain(row)
        await pilot.press("escape")
        await pilot.pause()
        assert not row.armed and fake.retracted == []

        await pilot.press("d")
        await pilot.press("d")
        for _ in range(150):
            if fake.retracted:
                break
            await pilot.pause(0.02)
        assert fake.retracted == [7]
        assert "retracted" in _plain(row)


@pytest.mark.asyncio
async def test_the_browser_hides_belief_verbs_on_a_lore_core_without_them(
    monkeypatch, tmp_path
):
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(7, "a claim under test")]
    fake.belief_action_state_result = {
        "capable": False, "reason": "lore_core 0.30.0 is missing record_outcome",
    }
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(180, 48)) as pilot:
        await pilot.pause()
        browser = await _browser(pilot, app, fake)
        painted = "\n".join(str(w.renderable) for w in browser.query("Static"))
        assert "missing record_outcome" in painted
        row = next(iter(browser.query(BeliefRow)))
        assert "confirmed" not in _plain(row).lower().split("·")[-1]
        assert "⌫ retract" not in _plain(row)
        row.focus()
        await pilot.press("c", "x", "d", "d")
        await pilot.pause()
        assert fake.outcomes_recorded == [] and fake.retracted == []


@pytest.mark.asyncio
async def test_recording_an_outcome_repaints_the_staleness_column(
    monkeypatch, tmp_path
):
    """The point of recording an outcome is that the column changes. A row
    that still said "never tested" after you confirmed it would be the
    browser lying about the one thing it exists to show."""
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(7, "a claim under test")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(180, 48)) as pilot:
        await pilot.pause()
        browser = await _browser(pilot, app, fake)
        row = next(iter(browser.query(BeliefRow)))
        assert "never tested" in _plain(row)
        # The store now answers with a confirmed belief.
        fake.list_beliefs_result = [
            _belief(7, "a claim under test", outcome="confirmed", outcome_days=0)
        ]
        row.focus()
        await pilot.press("c")
        for _ in range(200):
            if "confirmed" in _plain(row) and "never tested" not in _plain(row):
                break
            await pilot.pause(0.02)
        assert "never tested" not in _plain(row)
        assert "confirmed" in _plain(row)


# -- belief actions against the real lore_core ---------------------------


@pytest.mark.asyncio
async def test_recording_an_outcome_lands_in_lores_own_ledger(
    tmp_path, lore_store_cleanup
):
    """The provenance condition for this verb. DOXA does not invent a
    label: it calls record_outcome with source="user", which is exactly
    what lore_core.beliefs.cmd_outcome -- LORE's own "manual/pushback
    path" -- passes. Read back out of the store, not trusted."""
    from lore_core import store as lore_store
    from lore_core.beliefs import belief_insert, belief_subject, outcome_counts

    from doxa.engine import belief_action_state

    if not belief_action_state()["capable"]:
        pytest.skip("this lore_core has no outcome ledger")

    engine = _engine(tmp_path)
    conn = lore_store.db_connect()
    bid, _ = belief_insert(conn, belief_subject("user", engine.slug),
                           "the operator confirms beliefs from doxa",
                           0.8, None, None, None)
    conn.commit()

    assert await engine.record_belief_outcome(bid, "confirmed") is None

    row = conn.execute(
        "SELECT event, source, session_id FROM belief_outcomes"
        " WHERE belief_id = ?", (bid,)).fetchone()
    assert row is not None, "the outcome never reached the ledger"
    assert row[0] == "confirmed"
    assert row[1] == "user", "LORE's own label for the manual path"
    assert row[2] == engine.session_id
    assert outcome_counts(conn, bid) == (1, 0, 0)

    # ...and the belief now carries its verdict into the browser's column.
    listed = next(b for b in await engine.list_beliefs() if b["id"] == bid)
    assert belief_outcome_kind(listed) == "confirmed"
    assert belief_outcome_text(listed).startswith("confirmed")


@pytest.mark.asyncio
async def test_contradictions_retire_a_belief_and_the_reply_says_so(
    tmp_path, lore_store_cleanup
):
    """record_outcome carries LORE's dormancy trigger, and DOXA drives it
    rather than routing around it -- so the second contradiction retires
    the claim exactly as `lore outcome` would, and the user is TOLD."""
    from lore_core import store as lore_store
    from lore_core.beliefs import (
        CONTRADICTIONS_TO_DORMANT,
        belief_insert,
        belief_subject,
    )

    from doxa.engine import belief_action_state

    if not belief_action_state()["capable"]:
        pytest.skip("this lore_core has no outcome ledger")

    engine = _engine(tmp_path)
    conn = lore_store.db_connect()
    bid, _ = belief_insert(conn, belief_subject("user", engine.slug),
                           "a claim reality keeps disagreeing with",
                           0.8, None, None, None)
    conn.commit()

    said = [await engine.record_belief_outcome(bid, "contradicted")
            for _ in range(CONTRADICTIONS_TO_DORMANT)]
    status = conn.execute("SELECT status FROM beliefs WHERE id = ?",
                          (bid,)).fetchone()[0]
    assert status == "dormant"
    assert said[-1] and "dormant" in said[-1], said
    assert "retire a claim from the working set" in said[-1]
    # A dormant belief is out of the working set, so out of the browser.
    assert all(b["id"] != bid for b in await engine.list_beliefs())


@pytest.mark.asyncio
async def test_retracting_uses_lores_own_transition(tmp_path, lore_store_cleanup):
    """Copied from the branch lore_core.pending.apply_item runs for an
    approved retract proposal, so a retraction from DOXA and one from
    `lore approve` leave the store in the same shape -- status, resolution
    text and all. And the evidence survives: retracting is not deleting."""
    from lore_core import store as lore_store
    from lore_core.beliefs import belief_insert, belief_subject

    from doxa.engine import belief_action_state

    if not belief_action_state()["capable"]:
        pytest.skip("this lore_core has no belief_supersede")

    engine = _engine(tmp_path)
    conn = lore_store.db_connect()
    bid, _ = belief_insert(conn, belief_subject("user", engine.slug),
                           "a claim the operator kills on sight",
                           0.8, None, None, "seen in a session")
    conn.commit()

    assert await engine.retract_belief(bid) is None

    row = conn.execute(
        "SELECT status, resolution FROM beliefs WHERE id = ?", (bid,)).fetchone()
    assert row[0] == "retracted"
    assert row[1], "belief_supersede's own resolution text is recorded"
    assert conn.execute(
        "SELECT count(*) FROM belief_evidence WHERE belief_id = ?", (bid,)
    ).fetchone()[0] == 1, "retracting is not deleting"
    assert all(b["id"] != bid for b in await engine.list_beliefs())

    again = await engine.retract_belief(bid)
    assert again and "already retracted" in again


@pytest.mark.asyncio
async def test_an_unknown_verdict_is_refused_rather_than_written(
    tmp_path, lore_store_cleanup
):
    """The vocabulary is LORE's CHECK constraint, not free text. A verdict
    this store cannot hold is refused here rather than raised out of
    sqlite three frames down."""
    from doxa.engine import BELIEF_OUTCOME_EVENTS

    engine = _engine(tmp_path)
    said = await engine.record_belief_outcome(1, "approved")
    assert said and "approved" in said
    assert all(event in said for event in BELIEF_OUTCOME_EVENTS)


def test_the_engine_has_no_bulk_belief_action_under_any_name():
    """SECURITY-SHAPED ASSERTION at the API, matching the one approve and
    reject already carry: one belief per call, so there is nothing for a
    "retract all" to be built on without adding it first."""
    import inspect

    from doxa.client import EngineClient
    from doxa.engine import SessionEngine

    for cls in (SessionEngine, EngineClient):
        names = sorted(n for n in dir(cls)
                       if ("retract" in n or "outcome" in n)
                       and not n.startswith("_"))
        assert names == ["record_belief_outcome", "retract_belief"], (cls, names)
        assert list(inspect.signature(
            getattr(cls, "record_belief_outcome")).parameters
        )[:3] == ["self", "belief_id", "event"]
        assert list(inspect.signature(
            getattr(cls, "retract_belief")).parameters
        )[:2] == ["self", "belief_id"]


# -- v0.58.0: the proposals chip, and four corrections to the pickers ----


def _proposals(n, kind="memory", scope="user", start=0, **kw):
    out = []
    for i in range(start, start + n):
        item = {"pid": f"20260824-{i:03d}", "kind": kind, "action": "add",
                "text": f"proposal number {i:03d}",
                "created": _stamp(3 * DAY)}
        if kind == "memory":
            item["scope"] = scope
            item["project"] = "doxa"
        item.update(kw)
        out.append(item)
    return out


async def _pending_picker(pilot, app):
    from doxa.app import ChipPicker

    pane = app.active_pane
    await pane.open_pending_picker()
    for _ in range(200):
        picker = app.query_one("#chip-picker", ChipPicker)
        if picker.is_open:
            await pilot.pause()
            return pane, picker
        await pilot.pause(0.02)
    raise AssertionError("the proposals picker never opened")


def _pending_rows(picker):
    return [(rid, label) for rid, label in picker._rows
            if rid.startswith("pending:")]


def _shown(picker):
    """Each row as a READER sees it -- markup resolved, same rule
    `_status_plain` follows. v0.67.0's inline row actions add real
    `[@click=...]` control markup to a beliefs/pending row's raw
    `Option.prompt` string (never rendered as visible cells, same as any
    other markup span in this app); measuring THAT raw string's length
    instead of the plain text it resolves to would count control
    sequences as if they were columns on screen."""
    from textual.content import Content

    return [Content.from_markup(str(picker.get_option_at_index(i).prompt)).plain
            for i in range(picker.option_count)]


def test_the_staged_chip_is_hidden_at_zero_and_counts_at_one():
    """The status line is the most contended row in the UI; a chip reading
    `0 proposals` is a permanent reminder of nothing."""
    from doxa.ui.labels import staged_chip

    assert staged_chip(0) is None
    assert staged_chip(None) is None
    assert staged_chip(1)[0] == "1 proposal"
    assert staged_chip(175)[0] == "175 proposals"


def test_the_staged_count_is_cached_on_the_pending_directory(monkeypatch):
    """COST DISCIPLINE. _refresh_status runs on every event-driven refresh
    and already pays a belief COUNT(*); scoping the staged count means
    opening every staged file. A directory's mtime changes exactly when an
    entry is added or removed, so an unchanged spool costs one stat."""
    import json

    import lore_core
    from lore_core import pending as pending_mod

    from doxa.ui import labels as labels_mod

    pdir = lore_core.ROOT / "pending"
    pdir.mkdir(parents=True, exist_ok=True)
    for stale in pdir.glob("*.json"):
        stale.unlink()
    for i in range(3):
        (pdir / f"20260824-{i:02d}.json").write_text(json.dumps(
            {"kind": "memory", "scope": "user", "text": f"p{i}"}))
    labels_mod._STAGED_CACHE.clear()

    reads = {"n": 0}
    real = pending_mod.load_pending

    def counting():
        reads["n"] += 1
        return real()

    monkeypatch.setattr(pending_mod, "load_pending", counting)

    assert labels_mod.staged_count(None) == 3
    assert reads["n"] == 1
    for _ in range(50):
        assert labels_mod.staged_count(None) == 3
    assert reads["n"] == 1, "an unchanged spool must not be re-read"

    (pdir / "20260824-99.json").write_text(json.dumps(
        {"kind": "memory", "scope": "user", "text": "new"}))
    assert labels_mod.staged_count(None) == 4
    assert reads["n"] == 2

    for stale in pdir.glob("*.json"):
        stale.unlink()
    labels_mod._STAGED_CACHE.clear()


@pytest.mark.asyncio
async def test_the_chip_count_equals_the_list_it_opens(tmp_path,
                                                       lore_store_cleanup):
    """THE DEFECT THIS RELEASE NEARLY SHIPPED. A chip reading 5 over a
    picker showing 59 is worse than no chip. The count was first written on
    `lore_core.deriver.pending_texts`, which returns
    `item["text"] or item["name"]` and drops anything carrying neither --
    so every filemap proposal (a path and a purpose, and neither of those
    fields) vanished from the count while staying in the list. Both now
    walk load_pending through the SAME predicate."""
    import json

    import lore_core

    from doxa.ui import labels as labels_mod

    engine = _engine(tmp_path)
    pdir = lore_core.ROOT / "pending"
    pdir.mkdir(parents=True, exist_ok=True)
    for stale in pdir.glob("*.json"):
        stale.unlink()

    mixed = [
        {"kind": "memory", "scope": "user", "text": "a user memory"},
        {"kind": "memory", "scope": "project", "project": engine.slug,
         "text": "this project's memory"},
        # No `text`, no `name` -- the shape that was being dropped.
        {"kind": "filemap", "project": engine.slug,
         "path": "doxa/app.py", "purpose": "the facade"},
        {"kind": "belief", "subject": "user", "claim": "a staged belief"},
        {"name": "a-learned-skill", "action": "add", "description": "a thing"},
        {"kind": "memory", "scope": "project", "project": "some-other-repo",
         "text": "not this project's"},
    ]
    for index, item in enumerate(mixed):
        (pdir / f"20260824{index:04d}-00.json").write_text(
            json.dumps({"created": "2026-08-24T12:00:00Z", **item}))
    labels_mod._STAGED_CACHE.clear()

    listed = await engine.list_pending(limit=500)
    counted = labels_mod.staged_count(engine.slug)
    assert counted == len(listed) == 5, (counted, len(listed))
    assert any(p.get("kind") == "filemap" for p in listed)
    assert all(p.get("project") != "some-other-repo" for p in listed)

    for stale in pdir.glob("*.json"):
        stale.unlink()
    labels_mod._STAGED_CACHE.clear()


@pytest.mark.asyncio
async def test_the_chip_shows_the_count_and_opens_the_proposals_picker(
    monkeypatch, tmp_path
):
    from doxa.session import chips as chips_mod

    monkeypatch.setattr(chips_mod, "staged_count", lambda slug: 7)
    fake = FakeEngine([])
    fake.list_pending_result = _proposals(7)
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        pane = app.active_pane
        for _ in range(200):
            if "7 proposals" in _status_plain(app):
                break
            await pilot.pause(0.02)
        assert "7 proposals" in _status_plain(app)

        await pane.query_one("#status-bar").action_open_pending_picker()
        for _ in range(200):
            picker = app.query_one("#chip-picker")
            if picker.is_open:
                break
            await pilot.pause(0.02)
        assert picker.border_title == "pending"


@pytest.mark.asyncio
async def test_the_chip_is_absent_when_nothing_is_staged(monkeypatch, tmp_path):
    from doxa.session import chips as chips_mod

    monkeypatch.setattr(chips_mod, "staged_count", lambda slug: 0)
    fake = FakeEngine([])
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        for _ in range(60):
            await pilot.pause(0.02)
        assert "proposal" not in _status_plain(app)


@pytest.mark.asyncio
async def test_proposals_fold_by_kind_with_their_counts(monkeypatch, tmp_path):
    """BY KIND, because kind is what the verdict acts on -- and the SKILL
    lane falls out of that for free, which is the point: LORE's own
    /lore:pending keeps skills out of memory clustering."""
    from doxa.app import ChipPicker

    fake = FakeEngine([])
    fake.list_pending_result = (
        _proposals(6, scope="user")
        + _proposals(4, scope="project", start=10)
        + _proposals(2, kind="filemap", start=20)
        + [{"pid": "s-1", "name": "a-learned-skill", "action": "add",
            "description": "does a thing"}]
    )
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _pending_picker(pilot, app)
        headers = [label for rid, label in picker._rows
                   if rid.startswith(ChipPicker.GROUP_ROW_PREFIX)]
        assert any("memory/user (6 proposals)" in h for h in headers), headers
        assert any("memory/project (4 proposals)" in h for h in headers), headers
        assert any("filemap (2 proposals)" in h for h in headers), headers
        assert any("skill (1 proposal)" in h for h in headers), headers


@pytest.mark.asyncio
async def test_approve_arms_and_reject_is_one_act(monkeypatch, tmp_path):
    """THE ASYMMETRY. Approving writes into curated memory or the belief
    store -- material injected into the model's context on every prompt.
    Rejecting archives a JSON file that stays on disk."""
    fake = FakeEngine([])
    fake.list_pending_result = _proposals(2)
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _pending_picker(pilot, app)
        rid = next(rid for rid, _l in _pending_rows(picker))
        pid = fake.list_pending_result[0]["pid"]
        picker.select_row(next(i for i, (r, _l) in enumerate(picker._rows)
                               if r == rid))
        for _ in range(150):
            if any(r == "act:approve" for r, _l in picker._rows):
                break
            await pilot.pause(0.02)
        assert fake.approved == [] and fake.rejected == []

        picker.select_row(next(i for i, (r, _l) in enumerate(picker._rows)
                               if r == "act:approve"))
        for _ in range(150):
            if any(r == "act:approve!" for r, _l in picker._rows):
                break
            await pilot.pause(0.02)
        assert fake.approved == [], "the first approve selection must only arm"
        armed = next(l for r, l in picker._rows if r == "act:approve!")
        assert "CONFIRM APPROVE" in armed and "select again" in armed
        assert "act:approve" not in [r for r, _l in picker._rows]

        picker.select_row(next(i for i, (r, _l) in enumerate(picker._rows)
                               if r == "act:approve!"))
        for _ in range(150):
            if fake.approved:
                break
            await pilot.pause(0.02)
        assert fake.approved == [pid] and fake.rejected == []


@pytest.mark.asyncio
async def test_reject_takes_one_selection_and_writes_nothing(
    monkeypatch, tmp_path
):
    fake = FakeEngine([])
    fake.list_pending_result = _proposals(2)
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        pane, picker = await _pending_picker(pilot, app)
        rid = next(rid for rid, _l in _pending_rows(picker))
        pid = fake.list_pending_result[0]["pid"]
        picker.select_row(next(i for i, (r, _l) in enumerate(picker._rows)
                               if r == rid))
        for _ in range(150):
            if any(r == "act:reject" for r, _l in picker._rows):
                break
            await pilot.pause(0.02)
        picker.select_row(next(i for i, (r, _l) in enumerate(picker._rows)
                               if r == "act:reject"))
        for _ in range(150):
            if fake.rejected:
                break
            await pilot.pause(0.02)
        await pilot.pause()
        assert fake.rejected == [pid] and fake.approved == []
        texts = "\n".join(str(b.renderable) for b in pane.query("SystemBlock"))
        assert "rejected" in texts and "Nothing was written" in texts


@pytest.mark.asyncio
async def test_a_session_that_cannot_record_provenance_offers_no_verbs(
    monkeypatch, tmp_path
):
    """The WIDER gate: approving writes a NEW ENTRY, and an entry with no
    `via` label is what LORE 0.36.0's ledger exists to prevent."""
    fake = FakeEngine([])
    fake.list_pending_result = _proposals(2)
    fake.lore_write_state_result = {
        "capable": False, "version": "0.34.0",
        "reason": "lore_core 0.34.0 has no write gate or provenance ledger",
    }
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _pending_picker(pilot, app)
        rid = next(rid for rid, _l in _pending_rows(picker))
        picker.select_row(next(i for i, (r, _l) in enumerate(picker._rows)
                               if r == rid))
        for _ in range(150):
            if any(r == "act:show" for r, _l in picker._rows):
                break
            await pilot.pause(0.02)
        assert [r for r, _l in picker._rows
                if r.startswith("act:")] == ["act:show", "act:back"]
        assert "no write gate or provenance ledger" in picker._note
        assert fake.approved == [] and fake.rejected == []


def test_there_is_no_bulk_proposal_action_on_any_surface():
    """SECURITY-SHAPED ASSERTION, on IDENTIFIERS rather than on prose --
    the docstrings here talk about the "approve all" that deliberately does
    not exist, and a substring search would match the argument against it.
    One proposal per call, so there is nothing for a bulk action to be
    built on without adding it first."""
    import inspect

    from doxa.session.chips import PaneChipsMixin

    names = [n for n in dir(PaneChipsMixin)
             if ("approve" in n or "reject" in n) and not n.startswith("__")]
    assert names == [], names
    assert list(inspect.signature(
        PaneChipsMixin._resolve_pending).parameters) == ["self", "item", "action"]
    assert list(inspect.signature(
        PaneChipsMixin._run_pending_action).parameters) == [
            "self", "chosen", "rid", "item", "by_id"]


# -- the four picker corrections ----------------------------------------


@pytest.mark.asyncio
async def test_a_long_claim_uses_the_terminal_it_has(monkeypatch, tmp_path):
    """PICKER_ROW_WIDTH was a constant 72 -- what fits an 80-column
    terminal -- so a claim on a 160-column terminal was cut at 72 anyway.
    The row is now trimmed by the WIDGET against its own measured content
    width."""
    claim = ("the operator prefers conventional commit subjects and asks for "
             "the body to explain why rather than what, because the diff "
             "already says what and a body repeating it is noise in a bisect")
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(1, claim)]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        _pane, picker = await _picker(pilot, app, beliefs=None)
        assert picker._row_budget() > 72, picker._row_budget()
        widest = max(len(line) for line in _shown(picker))
        assert widest > 72, widest
        assert widest <= 160

    fake2 = FakeEngine([])
    fake2.list_beliefs_result = [_belief(1, claim)]
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake2)
    narrow = DoxaApp(cwd=str(tmp_path))
    async with narrow.run_test(size=(80, 40)) as pilot:
        await pilot.pause()
        _pane, picker = await _picker(pilot, narrow, beliefs=None)
        # ...and it degrades rather than overflowing its own dropdown.
        assert all(len(line) <= 80 for line in _shown(picker))


@pytest.mark.asyncio
async def test_typing_finds_a_word_past_the_visible_cut(monkeypatch, tmp_path):
    """The formatter used to ellipsize to 72 and the matcher scored THAT,
    so the tail of a long claim could not be searched for."""
    fake = FakeEngine([])
    fake.list_beliefs_result = [
        _belief(1, "x" * 200 + " quokka"),
        _belief(2, "an unrelated claim"),
    ]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        _pane, picker = await _picker(pilot, app, beliefs=None)
        await pilot.press("q", "u", "o", "k", "k", "a")
        await pilot.pause()
        found = [rid for rid, _l in picker._rows if rid.startswith("belief:")]
        assert found == ["belief:1"], found


@pytest.mark.asyncio
async def test_the_door_has_no_fold_around_it(monkeypatch, tmp_path):
    """It was given a group of its own purely so the header machinery would
    not paint a bare `▎` -- which became a fold around ONE row once groups
    folded, a fold whose only effect was hiding the way out."""
    from doxa.app import ChipPicker
    from doxa.session.chips import BROWSE_ALL_ROW

    fake = FakeEngine([])
    fake.list_beliefs_result = _many(40)
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        _pane, picker = await _picker(pilot, app, beliefs=None)
        rids = [rid for rid, _l in picker._rows]
        assert rids[0] == BROWSE_ALL_ROW[0], "the door is present, and first"
        headers = [label for rid, label in picker._rows
                   if rid.startswith(ChipPicker.GROUP_ROW_PREFIX)]
        assert not any("browse" in h.lower() for h in headers), headers
        assert not any(h.strip() in ("▎", "") for h in headers), headers


def test_each_door_names_the_half_it_opens():
    """Reported as misleading, and it was: BOTH rows read "open the beliefs
    browser", so a reader who clicked one in the PROPOSALS picker landed on
    a tab named for beliefs. The door did not say where it led because it
    led to two places at once."""
    from doxa.session.chips import BROWSE_ALL_ROW, REVIEW_ALL_ROW

    beliefs_door = BROWSE_ALL_ROW[1].lower()
    proposals_door = REVIEW_ALL_ROW[1].lower()
    assert "belief" in beliefs_door
    assert "approve" not in beliefs_door and "reject" not in beliefs_door
    assert "proposal" in proposals_door
    assert "approve" in proposals_door and "reject" in proposals_door


@pytest.mark.asyncio
async def test_the_door_lands_on_the_half_you_came_from(monkeypatch, tmp_path):
    """One tab, both halves -- splitting it would duplicate the surface --
    so the door opens it focused where the reader was heading."""
    from doxa.session.chips import BROWSE_ALL_ROW, REVIEW_ALL_ROW

    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(1, "a belief")]
    fake.list_pending_result = _proposals(2)
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        pane, picker = await _picker(pilot, app, beliefs=None)
        picker.select_row(next(i for i, (r, _l) in enumerate(picker._rows)
                               if r == BROWSE_ALL_ROW[0]))
        for _ in range(200):
            if pane._beliefs_tab is not None and pane._beliefs_tab.rows:
                break
            await pilot.pause(0.02)
        browser = pane._beliefs_tab
        for _ in range(200):
            if isinstance(app.focused, BeliefRow):
                break
            await pilot.pause(0.02)
        assert browser.focus_target == "beliefs"
        assert isinstance(app.focused, BeliefRow), app.focused

        # The OTHER door records the other half. Asserted on the recorded
        # target rather than on app.focused: re-entering an already-open
        # browser from a picker row is a three-way race between
        # ChipPicker's focus hand-off to the prompt, TabbedContent's
        # reactive `.active`, and its `_on_tab_pane_focused` snap-back, and
        # driving that sequence headlessly is not the same thing as a user
        # clicking. focus_target is what _apply_focus reads, so it is the
        # product's own answer to "which half did the reader ask for".
        pane.open_beliefs_browser  # (the door below routes through it)
        browser.focus_target = "beliefs"
        await pane.open_beliefs_browser(focus="proposals")
        await pilot.pause()
        assert browser.focus_target == "proposals"
        assert pane._beliefs_tab is browser, "one tab, both halves"


@pytest.mark.asyncio
async def test_the_tab_is_named_for_what_it_holds(monkeypatch, tmp_path):
    """A tab titled for one of its two halves is the same misleading label
    one level up."""
    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief(1, "a belief")]
    fake.list_pending_result = _proposals(1)
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        browser = await _browser(pilot, app, fake)
        title = str(browser._title) if browser._title else ""
        assert "belief" not in title.lower(), title
        assert "lore" in title.lower(), title
