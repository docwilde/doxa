# SPDX-License-Identifier: AGPL-3.0-only
"""Streaming-deriver tests (DOXA_DERIVE_SECS): the debounced mid-session
review that reuses the finalize/PreCompact deriver path.

Contracts pinned here: default OFF; at most one review per interval
(debounce); never more than one in flight; NEVER concurrent with finalize
(the review lock serializes them, and a finalize-first race makes the
derive bail); staged proposals surface as the out-of-band derive_done
event. The review runner itself is monkeypatched throughout -- lore_core's
machinery is reused, not reimplemented, so these tests assert WHEN it
runs, never WHAT it derives.

v0.31.0 added the half a user actually experiences. The event now carries
the staged proposal TEXTS (scrubbed, ellipsized, and bounded so an
oversize batch can never degrade to the frame-cap truncation marker), and
it drives THREE surfaces: a transcript block that quotes them and clicks
through to the list, a calm steady ``-staged`` tab tint (never the
needs-input blink), and a focus-gated desktop notification. The list is
DOXA's own ``/pending``; the old block pointed at ``/lore:pending``, a
Claude Code plugin command that does not exist inside DOXA. Everything
here is READ-ONLY by design -- approving and rejecting stay with LORE
while the plugin write path is under security review.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from claude_agent_sdk import ResultMessage
from textual.content import Content
from textual.widgets import TabbedContent

from doxa import commands
from doxa.app import ChipPicker, DoxaApp, SystemBlock
from doxa.daemon import encode_frame
from doxa.engine import (
    DERIVE_EVENT_TEXTS,
    DERIVE_TEXT_CHARS,
    EngineEvent,
    SessionEngine,
    derive_interval,
    staged_event_payload,
)
from doxa.peers import MAX_FRAME_BYTES
from tests.fakes import FakeEngine, factory_with_script


def _result_script() -> list:
    return [
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s", total_cost_usd=0.0,
        ),
    ]


async def _engine(tmp_path) -> SessionEngine:
    factory, _created = factory_with_script(_result_script())
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()
    return engine


async def _turn(engine: SessionEngine) -> None:
    async for _ in engine.send("hi"):
        pass


def test_derive_interval_parsing(monkeypatch):
    from doxa.engine import DERIVE_SECS_DEFAULT

    # v0.98.0 turned the streaming deriver ON by default. Unset and empty
    # now mean "take the default", the way every other knob in the
    # registry reads them -- and GARBAGE takes it too, deliberately:
    # silently disabling a feature the operator believes is on is the
    # worse failure of the two.
    monkeypatch.delenv("DOXA_DERIVE_SECS", raising=False)
    assert derive_interval() == DERIVE_SECS_DEFAULT == 900.0
    monkeypatch.setenv("DOXA_DERIVE_SECS", "")
    assert derive_interval() == DERIVE_SECS_DEFAULT
    monkeypatch.setenv("DOXA_DERIVE_SECS", "banana")
    assert derive_interval() == DERIVE_SECS_DEFAULT
    # Off must be sayable, and in the spellings a person actually types.
    for off in ("0", "off", "no", "false", "OFF"):
        monkeypatch.setenv("DOXA_DERIVE_SECS", off)
        assert derive_interval() is None, off
    monkeypatch.setenv("DOXA_DERIVE_SECS", "-5")
    assert derive_interval() is None
    monkeypatch.setenv("DOXA_DERIVE_SECS", "90")
    assert derive_interval() == 90.0
    monkeypatch.setenv("DOXA_DERIVE_SECS", "0.5")
    assert derive_interval() == 0.5


@pytest.mark.asyncio
async def test_derive_can_be_turned_off_explicitly(tmp_path, monkeypatch):
    """v0.98.0 turned the streaming deriver ON by default, so "unset" no
    longer means off and this test says `off` outright.

    It was `test_derive_off_by_default`, and after the default flipped it
    still PASSED -- for the wrong reason: `_last_derive` is stamped at
    construction, so the first turn of a session is inside the 900s
    debounce whether the feature is on or off. A test that cannot fail
    when the thing it names breaks is worse than no test, which is why
    the sibling below asserts the on-by-default path from the other side.
    """
    monkeypatch.setenv("DOXA_DERIVE_SECS", "off")
    engine = await _engine(tmp_path)
    runs: list[bool] = []
    monkeypatch.setattr(engine, "_run_review_sync", lambda older: runs.append(older))
    await _turn(engine)
    assert engine._derive_task is None
    assert runs == []
    await engine.finalize()
    # finalize's own review still ran -- the deriver being off only
    # disables the MID-session cadence.
    assert runs == [False]


@pytest.mark.asyncio
async def test_derive_is_on_by_default_once_the_interval_has_passed(
    tmp_path, monkeypatch,
):
    """The other half of the flip: with nothing set, a turn that lands
    after the debounce window DOES schedule a review. Ages `_last_derive`
    rather than sleeping 900 seconds."""
    monkeypatch.delenv("DOXA_DERIVE_SECS", raising=False)
    engine = await _engine(tmp_path)
    runs: list[bool] = []
    monkeypatch.setattr(engine, "_run_review_sync", lambda older: runs.append(older))
    engine._last_derive -= 901          # the session has been going a while
    await _turn(engine)
    assert engine._derive_task is not None
    await engine._derive_task
    # `older=False` -- the same argument finalize and PreCompact pass.
    # A streaming review is not a different KIND of review, it is the
    # same job run earlier, which is why _derive_once goes through
    # _run_review_sync rather than reimplementing anything.
    assert runs == [False]


@pytest.mark.asyncio
async def test_derive_debounce_at_most_every_interval(tmp_path, monkeypatch):
    monkeypatch.setenv("DOXA_DERIVE_SECS", "3600")
    engine = await _engine(tmp_path)
    runs: list[float] = []
    monkeypatch.setattr(
        engine, "_run_review_sync", lambda older: runs.append(time.monotonic())
    )

    # Interval not yet elapsed since session start: debounced.
    await _turn(engine)
    assert engine._derive_task is None and runs == []

    # Pretend a full interval passed: exactly one derive fires...
    engine._last_derive = time.monotonic() - 7200
    await _turn(engine)
    assert engine._derive_task is not None
    await engine._derive_task
    assert len(runs) == 1

    # ...and the very next turn is debounced again.
    await _turn(engine)
    await asyncio.sleep(0)
    assert len(runs) == 1
    await engine.finalize()


@pytest.mark.asyncio
async def test_derive_never_more_than_one_in_flight(tmp_path, monkeypatch):
    monkeypatch.setenv("DOXA_DERIVE_SECS", "0.001")
    engine = await _engine(tmp_path)
    release = threading.Event()
    starts: list[float] = []

    def slow_review(older: bool) -> None:
        starts.append(time.monotonic())
        release.wait(5)

    monkeypatch.setattr(engine, "_run_review_sync", slow_review)
    engine._last_derive = time.monotonic() - 60
    await _turn(engine)
    first = engine._derive_task
    assert first is not None
    for _ in range(100):  # the executor job has actually started
        if starts:
            break
        await asyncio.sleep(0.01)
    assert len(starts) == 1

    # Debounce window long gone, but the first derive is still running:
    # no second task may be scheduled.
    engine._last_derive = time.monotonic() - 60
    await _turn(engine)
    assert engine._derive_task is first
    assert len(starts) == 1

    release.set()
    await first
    await engine.finalize()


@pytest.mark.asyncio
async def test_finalize_waits_for_inflight_derive(tmp_path, monkeypatch):
    """Never concurrent with finalize: a running derive holds the review
    lock and finalize awaits the whole task before (then serialized via the
    same lock) running its own last review."""
    monkeypatch.setenv("DOXA_DERIVE_SECS", "0.001")
    engine = await _engine(tmp_path)
    release = threading.Event()
    spans: list[tuple[float, float, str]] = []

    def slow_review(older: bool) -> None:
        t0 = time.monotonic()
        release.wait(5)
        spans.append((t0, time.monotonic(), "derive"))

    monkeypatch.setattr(engine, "_run_review_sync", slow_review)
    engine._last_derive = time.monotonic() - 60
    await _turn(engine)
    assert engine._derive_task is not None

    def final_review(older: bool) -> None:
        t0 = time.monotonic()
        spans.append((t0, time.monotonic(), "final"))

    finalize_task = asyncio.create_task(engine.finalize())
    await asyncio.sleep(0.1)
    assert not finalize_task.done()  # blocked behind the in-flight derive
    monkeypatch.setattr(engine, "_run_review_sync", final_review)
    release.set()
    await asyncio.wait_for(finalize_task, 5)

    assert [kind for *_ts, kind in spans] == ["derive", "final"]
    derive_end = spans[0][1]
    final_start = spans[1][0]
    assert final_start >= derive_end  # serialized, never overlapped


@pytest.mark.asyncio
async def test_queued_derive_bails_when_finalized_first(tmp_path, monkeypatch):
    monkeypatch.setenv("DOXA_DERIVE_SECS", "0.001")
    engine = await _engine(tmp_path)
    runs: list[bool] = []
    monkeypatch.setattr(engine, "_run_review_sync", lambda older: runs.append(older))
    engine._last_derive = time.monotonic() - 60
    # Schedule the derive but finalize BEFORE its task first runs.
    engine._maybe_schedule_derive()
    task = engine._derive_task
    assert task is not None
    engine._finalized = True
    await task
    assert runs == []  # the queued derive saw _finalized and bailed


def _staged_texts(engine, monkeypatch, before, after) -> None:
    """Script the pending list the derive sees before and after its review
    -- the same before/after pair ``_derive_once`` diffs."""
    lists = iter([before, after])
    monkeypatch.setattr(engine, "_pending_texts", lambda: list(next(lists)))
    monkeypatch.setattr(engine, "_run_review_sync", lambda older: None)


@pytest.mark.asyncio
async def test_staged_proposals_surface_as_derive_done_event(tmp_path, monkeypatch):
    monkeypatch.setenv("DOXA_DERIVE_SECS", "0.001")
    engine = await _engine(tmp_path)
    _staged_texts(engine, monkeypatch, ["old one"], ["old one", "alpha", "beta"])
    engine._last_derive = time.monotonic() - 60
    await _turn(engine)
    await engine._derive_task
    ev = engine._peer_queue.get_nowait()
    assert ev.type == "derive_done"
    assert ev.data["staged"] == 2
    # The count was never the point: the event has to say WHAT was staged.
    assert ev.data["texts"] == ["alpha", "beta"]
    assert ev.data["omitted"] == 0
    await engine.finalize()


@pytest.mark.asyncio
async def test_the_event_carries_only_what_this_review_added(tmp_path, monkeypatch):
    """The preview must show the proposals THIS review staged, not the
    tail of a queue that is mostly weeks old -- and a duplicate text in
    the old list may not swallow a genuinely new one (multiset diff)."""
    monkeypatch.setenv("DOXA_DERIVE_SECS", "0.001")
    engine = await _engine(tmp_path)
    _staged_texts(
        engine, monkeypatch,
        ["dup", "stale a", "stale b"],
        ["dup", "stale a", "stale b", "dup", "fresh"],
    )
    engine._last_derive = time.monotonic() - 60
    await _turn(engine)
    await engine._derive_task
    ev = engine._peer_queue.get_nowait()
    assert ev.data["texts"] == ["dup", "fresh"]
    await engine.finalize()


@pytest.mark.asyncio
async def test_no_event_when_nothing_new_staged(tmp_path, monkeypatch):
    monkeypatch.setenv("DOXA_DERIVE_SECS", "0.001")
    engine = await _engine(tmp_path)
    monkeypatch.setattr(engine, "_pending_texts", lambda: ["a", "b", "c", "d"])
    monkeypatch.setattr(engine, "_run_review_sync", lambda older: None)
    engine._last_derive = time.monotonic() - 60
    await _turn(engine)
    await engine._derive_task
    assert engine._peer_queue.empty()
    await engine.finalize()


# -- what rides on the event, and what cannot ---------------------------


def test_proposal_texts_are_scrubbed_on_the_way_out():
    """Staged proposals are derived from transcripts, so they are
    model-adjacent text and route through lore_core.scrub.scrub_secrets
    like everything else leaving the engine (doxa/engine.py's secret-scrub
    choke point)."""
    secret = "sk-ant-api03-" + "A" * 40
    payload = staged_event_payload(1, [f"the operator's key is {secret}"])
    assert secret not in payload["texts"][0]


def test_proposal_texts_are_one_line_and_ellipsized():
    payload = staged_event_payload(1, ["first line\n\nsecond line   spaced"])
    assert payload["texts"][0] == "first line second line spaced"
    # Ordinary prose, not one long opaque token: the scrubber redacts what
    # looks like a base64 blob, which would replace the string wholesale
    # and prove nothing about the width cap.
    verbose = " ".join(["the operator prefers uv over pip"] * 40)
    long = staged_event_payload(1, [verbose])["texts"][0]
    assert len(long) == DERIVE_TEXT_CHARS and long.endswith("…")


def test_a_big_batch_is_capped_and_says_how_many_it_left_out():
    payload = staged_event_payload(40, [f"proposal {i}" for i in range(40)])
    assert len(payload["texts"]) == DERIVE_EVENT_TEXTS
    assert payload["staged"] == 40
    assert payload["omitted"] == 40 - DERIVE_EVENT_TEXTS


def test_an_oversize_batch_never_degrades_to_the_truncation_marker():
    """The defect this cap exists to prevent: doxa.daemon.encode_frame
    answers an EVENT frame over MAX_FRAME_BYTES by replacing its whole
    payload with {"truncated": True}, which the TUI would render as
    nothing at all. A pathological batch -- hundreds of proposals, each far
    longer than a frame allows, in characters that escape wide -- has to
    come out UNDER the cap with the overflow counted, never swallowed."""
    monstrous = [("€" * 4000) + f" {i}" for i in range(400)]
    payload = staged_event_payload(len(monstrous), monstrous)
    frame = {"type": "event", "seq": 1,
             "event": {"type": "derive_done", "data": payload}}
    encoded = encode_frame(frame)
    assert len(encoded) <= MAX_FRAME_BYTES
    assert b'"truncated"' not in encoded
    assert payload["omitted"] == len(monstrous) - len(payload["texts"])
    assert json.loads(encoded)["event"]["data"]["staged"] == len(monstrous)


# -- the three surfaces one event drives --------------------------------


DERIVE_EVENT = EngineEvent("derive_done", {
    "staged": 3,
    "texts": ["the operator prefers uv over pip", "doxa lives in ~/doxa"],
    "omitted": 1,
})


def _system_blocks(app):
    return [b for b in app.query(SystemBlock) if b.id != "identity-block"]


def _plain(widget) -> str:
    return Content.from_markup(str(widget.renderable)).plain


async def _wait(pilot, cond, tries=150):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(0.02)
    return cond()


@pytest.mark.asyncio
async def test_the_notification_renders_with_real_height_and_quotes_the_texts(
    monkeypatch, tmp_path
):
    """A user-visible outcome, not a structural one: the block is actually
    on screen (non-zero height), and it says WHAT was staged."""
    monkeypatch.setattr("doxa.app.notify_mod.notify", lambda *a, **k: None)
    fake = FakeEngine([])
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        fake.push_peer_event(DERIVE_EVENT)
        assert await _wait(pilot, lambda: _system_blocks(app))
        await pilot.pause()
        block = _system_blocks(app)[0]
        assert block.size.height > 0 and block.size.width > 0
        rendered = _plain(block)
        assert "3 proposals staged" in rendered
        assert "the operator prefers uv over pip" in rendered
        assert "doxa lives in ~/doxa" in rendered
        assert "and 1 more" in rendered
        # The dead end is gone: /lore:pending is a Claude Code PLUGIN
        # command, not a DOXA one, so this block must not send anyone to it.
        assert "/lore:pending" not in rendered
        assert "/pending" in rendered


@pytest.mark.asyncio
async def test_the_tab_changes_and_does_not_blink(monkeypatch, tmp_path):
    """The tab affordance actually changes -- and it is the CALM one:
    -staged is steady, so unlike -attention it needs no timer and does not
    toggle itself off half a second later."""
    monkeypatch.setattr("doxa.app.notify_mod.notify", lambda *a, **k: None)
    fake = FakeEngine([])
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        tab = app.query_one("#session-tabs", TabbedContent).get_tab(pane.tab_id)
        assert not tab.has_class("-staged")
        fake.push_peer_event(DERIVE_EVENT)
        assert await _wait(pilot, lambda: tab.has_class("-staged"))
        # Blinking stays reserved for needs-input: no attention class, no
        # timer, and the tint is still there a full blink period later.
        assert not tab.has_class("-attention")
        assert pane._attention_timer is None
        await pilot.pause(0.6)
        assert tab.has_class("-staged")


@pytest.mark.asyncio
async def test_looking_at_the_tab_clears_the_staged_tint(monkeypatch, tmp_path):
    monkeypatch.setattr("doxa.app.notify_mod.notify", lambda *a, **k: None)
    first_engine = FakeEngine([])
    engines = iter([first_engine, FakeEngine([]), FakeEngine([])])
    monkeypatch.setattr(
        "doxa.app.SessionEngine", lambda cwd, model=None: next(engines)
    )
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        first_pane = app.active_pane
        tabs = app.query_one("#session-tabs", TabbedContent)
        first_tab = tabs.get_tab(first_pane.tab_id)
        await app.action_new_tab()
        await pilot.pause()
        assert app.active_pane is not first_pane
        first_engine.push_peer_event(DERIVE_EVENT)
        assert await _wait(pilot, lambda: first_tab.has_class("-staged"))
        tabs.active = first_pane.tab_id
        assert await _wait(pilot, lambda: not first_tab.has_class("-staged"))


@pytest.mark.asyncio
@pytest.mark.parametrize("focused,expected_calls", [(True, 0), (False, 1)])
async def test_the_desktop_notification_respects_the_focus_rule(
    monkeypatch, tmp_path, focused, expected_calls
):
    """The standing rule every DOXA trigger obeys: on the default `auto`
    mode a banner fires only while the window is NOT the one being looked
    at."""
    sent: "list[tuple[str, str]]" = []
    monkeypatch.setattr(
        "doxa.app.notify_mod.notify", lambda title, body: sent.append((title, body))
    )
    fake = FakeEngine([])
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.app_has_focus = focused
        fake.push_peer_event(DERIVE_EVENT)
        assert await _wait(pilot, lambda: _system_blocks(app))
        await pilot.pause()
        assert len(sent) == expected_calls
        if sent:
            # And the banner is informative, not a bare number.
            assert "3 proposals staged" in sent[0][1]
            assert "the operator prefers uv over pip" in sent[0][1]


# -- /pending: the native surface the notification points at ------------


@pytest.mark.asyncio
async def test_pending_is_a_real_doxa_command_and_lists_the_proposals(
    monkeypatch, tmp_path
):
    """The old hint named /lore:pending, a Claude Code PLUGIN command
    doxa.commands has never known about -- typing it inside DOXA reached
    the model, not a list. /pending is DOXA's own."""
    assert "/pending" in commands.interactive_names()
    fake = FakeEngine([])
    fake.list_pending_result = ["remember uv, not pip", "the repo is at ~/doxa"]
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        # Exactly ONE call, and it is _boot's: v0.56.0 puts the staged
        # count on the opening block's `lore` line, which is drawn once
        # per session boot. The discipline this line has always pinned is
        # unchanged, and re-asserted right below it -- an ordinary STATUS
        # REFRESH must never reach for staged-proposal text, because
        # _refresh_status runs on every peer event under the no-timer,
        # no-per-frame rule GitLine documents.
        assert fake.list_pending_calls == 1
        for _ in range(5):
            pane._refresh_status()
        await pilot.pause()
        assert fake.list_pending_calls == 1  # never on refresh
        pane.query_one("#prompt-input").value = "/pending"
        await pilot.press("enter")
        picker = pane.query_one("#chip-picker", ChipPicker)
        assert await _wait(pilot, lambda: picker.is_open)
        labels = [label for _rid, label in picker._rows]
        assert any("remember uv, not pip" in label for label in labels)
        assert any("the repo is at ~/doxa" in label for label in labels)


@pytest.mark.asyncio
async def test_no_proposal_row_in_the_dropdown_acts_on_a_proposal(
    monkeypatch, tmp_path
):
    """Scope boundary, restated for v0.57.0 rather than dropped.

    v0.31.0 pinned "DOXA does not write at all"; v0.40.0 "not from this
    dropdown"; v0.57.0 gave proposals their own chip and put approve and
    reject behind their rows; v0.67.0 put approve/reject on the row's own
    inline action span too. The property underneath has never moved and
    is what this asserts directly: SELECTING a proposal row outright never
    itself acts as approve or reject -- it opens that proposal's own named
    verbs, because a dropdown row is one Enter from whatever the highlight
    is sitting on. (v0.69.0 removed the door row this test used to check
    named its destination, "approve or reject", along with the browser it
    led to -- there is no second surface to name a way into any more.)"""
    fake = FakeEngine([])
    fake.list_pending_result = [
        {"pid": "20260824-00", "kind": "memory", "action": "add",
         "scope": "user", "text": "one proposal"},
    ]
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        await pane.open_pending_picker()
        picker = pane.query_one("#chip-picker", ChipPicker)
        assert await _wait(pilot, lambda: picker.is_open)

        proposal_labels = " ".join(
            label for rid, label in picker._rows
            if rid.startswith("pending:")).lower()
        assert "approve" not in proposal_labels
        assert "reject" not in proposal_labels

        row = next(rid for rid, _l in picker._rows if rid.startswith("pending:"))
        index = next(i for i, (r, _l) in enumerate(picker._rows) if r == row)
        picker.select_row(index)
        assert await _wait(
            pilot, lambda: any(r == "act:show" for r, _l in picker._rows))
        assert fake.approved == [] and fake.rejected == []

        show = next(i for i, (r, _l) in enumerate(picker._rows) if r == "act:show")
        picker.select_row(show)
        assert await _wait(pilot, lambda: _system_blocks(app))
        await pilot.pause()
        assert "one proposal" in _plain(_system_blocks(app)[0])
        assert fake.approved == [] and fake.rejected == []


@pytest.mark.asyncio
async def test_pending_says_so_when_nothing_is_staged(monkeypatch, tmp_path):
    fake = FakeEngine([])
    fake.list_pending_result = []
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        await pane.open_pending_picker()
        assert await _wait(pilot, lambda: _system_blocks(app))
        await pilot.pause()
        assert "nothing staged" in _system_blocks(app)[0].text


@pytest.mark.asyncio
async def test_the_notification_block_is_itself_the_door(monkeypatch, tmp_path):
    """The block's trailing line is a live click target onto the same
    list, so an announcement that names a destination can take you there."""
    monkeypatch.setattr("doxa.app.notify_mod.notify", lambda *a, **k: None)
    fake = FakeEngine([])
    fake.list_pending_result = ["remember uv, not pip"]
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        fake.push_peer_event(DERIVE_EVENT)
        assert await _wait(pilot, lambda: _system_blocks(app))
        _system_blocks(app)[0].action_follow_link()
        picker = pane.query_one("#chip-picker", ChipPicker)
        assert await _wait(pilot, lambda: picker.is_open)
        assert any(
            "remember uv, not pip" in label for _rid, label in picker._rows
        )


@pytest.mark.asyncio
async def test_opening_the_list_clears_the_staged_tint(monkeypatch, tmp_path):
    monkeypatch.setattr("doxa.app.notify_mod.notify", lambda *a, **k: None)
    fake = FakeEngine([])
    fake.list_pending_result = ["something"]
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        tab = app.query_one("#session-tabs", TabbedContent).get_tab(pane.tab_id)
        fake.push_peer_event(DERIVE_EVENT)
        assert await _wait(pilot, lambda: pane.staged_pending)
        await pane.open_pending_picker()
        assert not pane.staged_pending
        assert not tab.has_class("-staged")
