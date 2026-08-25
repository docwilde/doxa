"""v0.32.0 -- restore brings back the VIEW, not just the tab list.

The reported defect, verbatim: "i meant to restore the view, also any
prior vertical or horizontal panel split and amount of opened tabs and
their content, after it was closed". Splits are out of scope (DOXA has no
split layout at all -- see CHANGELOG.md's 0.32.0 entry); TAB CONTENT is
what this file is about.

What was measured against pre-fix code, with a real daemon over a real
Unix socket, before anything was changed:

* Order and pinned names already survived a restart (v0.23.0), and
  tests/test_tabsets.py already asserted them.
* The saved ACTIVE tab did NOT, once there were three of them: a pane
  focusing its prompt on mount activates its own tab, so the LAST pane to
  mount always won. The existing two-tab test passed only because its
  saved active tab happened to be the last one.
* A SHORT session's transcript came back too, by accident of the daemon
  replaying its event ring to the reattaching client.
* A session longer than the ring did NOT. One ``text_delta`` is one frame
  and the ring holds 512, so a single 700-delta answer pushed
  ``turn_started`` off the far end; ``_peer_pump`` had no TurnBlock to
  render the survivors into and dropped every one. Measured: ring
  next_seq 702, oldest buffered seq 190, restored tab rendered **zero**
  turn blocks, and said nothing about it.
* A session whose daemon had finalized did not come back at all -- one
  line of arithmetic ("skipped 1 session no longer running") in place of
  the tab and everything in it.

Every test here asserts what is ON SCREEN -- a TurnBlock exists, and its
prompt and answer text are the ones the session had. Asserting that a
spec object was persisted is what let v0.27.0 ship invisible buttons.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
)

from doxa import config as config_mod
from doxa import tabsets
from doxa import transcript as transcript_mod
from doxa.app import (
    ArchivedSessionTab,
    DoxaApp,
    RestoreTabSpec,
    SystemBlock,
    ToolChip,
    TurnBlock,
)
from doxa.client import EngineClient
from doxa.daemon import SessionDaemon
from doxa.engine import EngineEvent, SessionEngine
from tests.fakes import FakeEngine, factory_with_script


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    config_mod.invalidate()
    yield
    config_mod.invalidate()


async def _wait(pilot, cond, tries=200):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(0.02)
    return cond()


def _write_transcript(session_id: str, cwd: str, turns: "list[dict]") -> None:
    """Persist a conversation through SessionEngine's OWN writers.

    Not a hand-rolled JSONL: the point of reading the transcript back is
    that it is the file the engine really writes, so the fixture goes
    through ``_persist_user_text`` / ``_persist_assistant_blocks`` /
    ``_persist_tool_results``. A format change in engine.py that this
    module failed to follow therefore breaks these tests, which is
    exactly what should happen."""
    engine = SessionEngine(cwd=cwd, session_id=session_id)
    for turn in turns:
        engine._persist_user_text(turn["prompt"])
        blocks: "list[dict]" = []
        if turn.get("text"):
            blocks.append({"type": "text", "text": turn["text"]})
        for tool in turn.get("tools") or []:
            blocks.append({
                "type": "tool_use", "id": tool["id"],
                "name": tool["name"], "input": tool.get("input") or {},
            })
        if blocks:
            engine._persist_assistant_blocks(blocks)
        results = [
            {"type": "tool_result", "tool_use_id": tool["id"],
             "content": tool["result"], "is_error": bool(tool.get("is_error"))}
            for tool in turn.get("tools") or []
            if "result" in tool
        ]
        if results:
            engine._persist_tool_results(results)


def _restore_engine(session_id: str, script=None):
    """A FakeEngine wearing the one EngineClient attribute the restore
    path reads: ``backlog_skipped``, the daemon ring head this client
    attached at instead of replaying. Not None means "the backlog is my
    job, not the ring's" -- which is precisely the condition under which
    the pane renders the transcript from disk."""

    def make() -> FakeEngine:
        engine = FakeEngine(list(script or []))
        engine.session_id = session_id
        engine.backlog_skipped = 0
        return engine

    return make


def _turn_blocks(node) -> "list[TurnBlock]":
    return list(node.query(TurnBlock))


def _rendered(node) -> "list[tuple[str, str]]":
    """What the user can actually read: (prompt, answer) per turn block."""
    return [(t.prompt_text, t.assistant_text) for t in _turn_blocks(node)]


# -- doxa.transcript: the reader itself ---------------------------------


def test_reads_back_the_conversation_the_engine_persisted(tmp_path):
    _write_transcript("sid-1", str(tmp_path), [
        {"prompt": "what is 1+2?", "text": "The answer is 3.",
         "tools": [{"id": "t1", "name": "calculator_add",
                    "input": {"a": 1, "b": 2}, "result": "3"}]},
        {"prompt": "and 2+2?", "text": "Four."},
    ])
    snapshot = transcript_mod.read("sid-1", str(tmp_path))
    assert [t.prompt for t in snapshot.turns] == ["what is 1+2?", "and 2+2?"]
    assert snapshot.turns[0].text == "The answer is 3."
    assert snapshot.turns[1].text == "Four."
    assert snapshot.dropped_turns == 0
    tool = snapshot.turns[0].tools[0]
    assert (tool.name, tool.result, tool.is_error) == ("calculator_add", "3", False)
    assert tool.tool_input == {"a": 1, "b": 2}


def test_missing_transcript_is_empty_not_an_error(tmp_path):
    snapshot = transcript_mod.read("never-existed", str(tmp_path))
    assert snapshot.turns == []
    assert not snapshot
    assert transcript_mod.exists("never-existed", str(tmp_path)) is False


def test_a_torn_final_line_costs_one_record_not_the_restore(tmp_path):
    _write_transcript("sid-torn", str(tmp_path), [
        {"prompt": "first", "text": "one"},
    ])
    path = transcript_mod.transcript_path("sid-torn", str(tmp_path))
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"type": "user", "message": {"role": "user", "conte')
    snapshot = transcript_mod.read("sid-torn", str(tmp_path))
    assert [t.prompt for t in snapshot.turns] == ["first"]


def test_turn_limit_keeps_the_TAIL_and_says_how_much_it_dropped(tmp_path):
    _write_transcript("sid-long", str(tmp_path), [
        {"prompt": f"turn {i}", "text": f"answer {i}"} for i in range(10)
    ])
    snapshot = transcript_mod.read("sid-long", str(tmp_path), limit=3)
    assert [t.prompt for t in snapshot.turns] == ["turn 7", "turn 8", "turn 9"]
    assert snapshot.dropped_turns == 7


def test_an_over_long_answer_is_cut_and_MARKED_never_silently(tmp_path):
    huge = "x" * (transcript_mod.MAX_TEXT_CHARS + 500)
    _write_transcript("sid-huge", str(tmp_path), [{"prompt": "dump", "text": huge}])
    turn = transcript_mod.read("sid-huge", str(tmp_path)).turns[0]
    assert len(turn.text) == transcript_mod.MAX_TEXT_CHARS
    assert turn.text_truncated is True


# -- the headline: a restored tab RENDERS its prior conversation --------


@pytest.mark.asyncio
async def test_a_restored_tab_renders_the_conversation_it_had(tmp_path):
    """Pre-fix this rendered NOTHING: the pane came up with only its
    identity block, bound to a live session whose whole transcript was on
    disk and (partly) in the daemon's ring."""
    _write_transcript("sid-restored", str(tmp_path), [
        {"prompt": "MEASURED-PROMPT-ONE", "text": "MEASURED-ANSWER-ONE"},
        {"prompt": "MEASURED-PROMPT-TWO", "text": "MEASURED-ANSWER-TWO",
         "tools": [{"id": "call-1", "name": "Read", "input": {"path": "/x"},
                    "result": "file contents"}]},
    ])
    app = DoxaApp(
        cwd=str(tmp_path),
        restore_tabs=[RestoreTabSpec(
            session_id="sid-restored",
            engine_factory=_restore_engine("sid-restored"),
            cwd=str(tmp_path),
        )],
        restore_active_id="sid-restored",
    )
    async with app.run_test() as pilot:
        pane = app.panes()[0]
        assert await _wait(pilot, lambda: _rendered(pane) == [
            ("MEASURED-PROMPT-ONE", "MEASURED-ANSWER-ONE"),
            ("MEASURED-PROMPT-TWO", "MEASURED-ANSWER-TWO"),
        ])
        assert await _wait(
            pilot, lambda: [c.tool_result for c in pane.query(ToolChip)]
            == ["file contents"]
        )
        assert [c.tool_name for c in pane.query(ToolChip)] == ["Read"]


@pytest.mark.asyncio
async def test_order_active_tab_and_names_survive_WITH_their_content(tmp_path):
    """The whole user-visible contract in one assertion set: three tabs
    come back in saved order, the saved active one is active, a pinned
    name is still pinned -- and each tab shows ITS OWN conversation, not
    an empty pane and not another tab's."""
    for index in ("a", "b", "c"):
        _write_transcript(f"sid-{index}", str(tmp_path), [
            {"prompt": f"prompt-{index}", "text": f"answer-{index}"},
        ])
    app = DoxaApp(
        cwd=str(tmp_path),
        restore_tabs=[
            RestoreTabSpec("sid-a", _restore_engine("sid-a"),
                           pinned_name="alpha", cwd=str(tmp_path)),
            RestoreTabSpec("sid-b", _restore_engine("sid-b"), cwd=str(tmp_path)),
            RestoreTabSpec("sid-c", _restore_engine("sid-c"), cwd=str(tmp_path)),
        ],
        restore_active_id="sid-b",
    )
    async with app.run_test() as pilot:
        assert await _wait(
            pilot, lambda: all(
                _rendered(p) == [(f"prompt-{i}", f"answer-{i}")]
                for p, i in zip(app.panes(), "abc")
            )
        )
        panes = app.panes()
        assert [p._session_id for p in panes] == ["sid-a", "sid-b", "sid-c"]
        assert app.active_pane is panes[1]
        assert panes[0].custom_name == "alpha"
        assert _rendered(panes[0]) == [("prompt-a", "answer-a")]
        assert _rendered(panes[1]) == [("prompt-b", "answer-b")]
        assert _rendered(panes[2]) == [("prompt-c", "answer-c")]


@pytest.mark.asyncio
async def test_the_saved_active_tab_wins_over_the_last_one_to_mount(tmp_path):
    """A v0.23.0 defect this work measured and fixed on the way past.

    A SessionPane focuses its prompt when it mounts, and focusing a widget
    inside a TabPane ACTIVATES that pane -- so every restored pane doing
    it left whichever mounted LAST active, whatever the record said. The
    existing two-tab test never caught it because its saved active tab
    WAS the last one; three tabs and a middle active id expose it. Uses no
    transcripts at all, so it fails on pre-fix code for the one reason it
    is about."""
    app = DoxaApp(
        cwd=str(tmp_path),
        restore_tabs=[
            RestoreTabSpec("sid-a", _restore_engine("sid-a")),
            RestoreTabSpec("sid-b", _restore_engine("sid-b")),
            RestoreTabSpec("sid-c", _restore_engine("sid-c")),
        ],
        restore_active_id="sid-b",
    )
    async with app.run_test() as pilot:
        assert await _wait(
            pilot, lambda: all(p._session_id for p in app.panes())
        )
        assert app.active_pane.id == "restore-sid-b"


@pytest.mark.asyncio
async def test_a_dropped_earlier_turn_is_announced_never_silently_missing(tmp_path):
    _write_transcript("sid-cap", str(tmp_path), [
        {"prompt": f"turn {i}", "text": f"answer {i}"} for i in range(6)
    ])
    limit = transcript_mod.DEFAULT_TURN_LIMIT
    try:
        transcript_mod.DEFAULT_TURN_LIMIT = 2
        app = DoxaApp(
            cwd=str(tmp_path),
            restore_tabs=[RestoreTabSpec(
                "sid-cap", _restore_engine("sid-cap"), cwd=str(tmp_path))],
        )
        async with app.run_test() as pilot:
            pane = app.panes()[0]
            assert await _wait(pilot, lambda: len(_turn_blocks(pane)) == 2)
            texts = [b.text for b in pane.query(SystemBlock)]
            assert any("4 earlier turns not shown" in t for t in texts)
    finally:
        transcript_mod.DEFAULT_TURN_LIMIT = limit


@pytest.mark.asyncio
async def test_an_ordinary_launch_renders_no_transcript_at_all(tmp_path):
    """Restore is the only thing that reads a transcript back. A plain
    `doxa` in a directory that HAS one must still open an empty pane --
    the setting gates the launch, and a fresh session is a fresh session."""
    _write_transcript("sid-old", str(tmp_path), [
        {"prompt": "should not appear", "text": "nor this"},
    ])
    factory = _restore_engine("sid-old")
    app = DoxaApp(cwd=str(tmp_path), engine_factory=factory,
                  new_session_factory=factory)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _wait(pilot, lambda: pane._session_id)
        await pilot.pause()
        assert _turn_blocks(pane) == []


# -- the ring-truncation defect, at the daemon boundary -----------------


@pytest.mark.asyncio
async def test_a_replayed_turn_whose_start_fell_off_the_ring_still_renders(tmp_path):
    """The exact shape of the measured defect, isolated: turn events with
    no ``turn_started`` in front of them. Pre-fix ``_peer_pump`` dropped
    every one on the floor; now they open an unattributed turn block that
    says so rather than vanishing."""
    engine = FakeEngine([])
    engine.session_id = "sid-orphan"
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: engine,
                  new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        pane = app.active_pane
        # Fully composed, not merely booted: the pump refreshes the status
        # bar after every event it handles.
        assert await _wait(
            pilot, lambda: pane._session_id and pane.query("#status-bar")
        )
        # No turn_started -- exactly what a ring that has wrapped delivers.
        engine.push_peer_event(EngineEvent("text_delta", {"text": "ORPHANED-TEXT"}))
        assert await _wait(pilot, lambda: any(
            "ORPHANED-TEXT" in b.assistant_text for b in _turn_blocks(pane)
        ))
        blocks = _turn_blocks(pane)
        assert len(blocks) == 1
        assert "in progress" in blocks[0].prompt_text


@pytest.mark.asyncio
async def test_a_restoring_client_skips_the_ring_so_nothing_renders_twice(tmp_path):
    """End to end over a real socket: the daemon holds a turn in its ring,
    a restoring client attaches with skip_backlog, and the ONLY copy of
    that turn on screen is the one read from disk."""
    script = [
        StreamEvent(uuid="s1", session_id="s",
                    event={"type": "content_block_delta",
                           "delta": {"type": "text_delta", "text": "RING-TEXT"}}),
        AssistantMessage(content=[TextBlock(text="RING-TEXT")],
                         model="claude-haiku-4-5"),
        ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                      is_error=False, num_turns=1, session_id="s",
                      total_cost_usd=0.0),
    ]
    factory, _created = factory_with_script(script)
    daemon = SessionDaemon(
        cwd=str(tmp_path), linger_secs=300.0,
        engine_factory=lambda cwd, sid, dsock: SessionEngine(
            cwd=cwd, session_id=sid, client_factory=factory, daemon_socket=dsock,
        ),
    )
    serve = asyncio.create_task(daemon.serve())
    await asyncio.wait_for(daemon.ready.wait(), 10)
    try:
        driver = EngineClient(str(daemon.socket_path))
        await driver.start()
        async for _ev in driver.send("RING-PROMPT"):
            pass
        session_id = str(driver.session_id)
        await driver.finalize()
        assert daemon.ring.next_seq > 0  # the turn IS in the ring

        # The disk transcript the restore will read is the daemon engine's
        # own -- written by the real turn above, not by this test.
        assert transcript_mod.exists(session_id, str(tmp_path))

        app = DoxaApp(
            cwd=str(tmp_path),
            restore_tabs=[RestoreTabSpec(
                session_id=session_id,
                engine_factory=(lambda: EngineClient(
                    str(daemon.socket_path), skip_backlog=True)),
                cwd=str(tmp_path),
            )],
        )
        async with app.run_test() as pilot:
            pane = app.panes()[0]
            assert await _wait(pilot, lambda: _turn_blocks(pane))
            for _ in range(25):
                await pilot.pause(0.02)
            rendered = _rendered(pane)
            assert rendered == [("RING-PROMPT", "RING-TEXT")]  # ONCE, not twice
    finally:
        # Same teardown discipline tests/test_daemon.py's own fixture uses:
        # shut the daemon down rather than cancelling serve() out from
        # under it, so the linger timer never outlives the test loop.
        if not serve.done():
            with contextlib.suppress(Exception):
                await daemon._shutdown("test teardown")
                await asyncio.wait_for(serve, 5)


@pytest.mark.asyncio
async def test_a_turn_longer_than_the_ring_comes_back_WHOLE(tmp_path):
    """THE measured defect, reproduced end to end at 1/32 scale.

    A 60-delta turn into a 16-frame ring: ``turn_started`` and all but the
    last handful of deltas are gone from the replay buffer, which is
    exactly the shape a 700-delta answer makes of the real 512-frame one.
    Pre-fix the restored tab rendered ZERO turn blocks. It now renders the
    turn complete, prompt included, because the content comes from the
    session's transcript rather than from the ring."""
    script = [
        StreamEvent(uuid=f"s{i}", session_id="s",
                    event={"type": "content_block_delta",
                           "delta": {"type": "text_delta", "text": f"D{i} "}})
        for i in range(60)
    ]
    script += [
        AssistantMessage(content=[TextBlock(text="".join(f"D{i} " for i in range(60)))],
                         model="claude-haiku-4-5"),
        ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                      is_error=False, num_turns=1, session_id="s",
                      total_cost_usd=0.0),
    ]
    factory, _created = factory_with_script(script)
    daemon = SessionDaemon(
        cwd=str(tmp_path), linger_secs=300.0, ring_capacity=16,
        engine_factory=lambda cwd, sid, dsock: SessionEngine(
            cwd=cwd, session_id=sid, client_factory=factory, daemon_socket=dsock,
        ),
    )
    serve = asyncio.create_task(daemon.serve())
    await asyncio.wait_for(daemon.ready.wait(), 10)
    try:
        driver = EngineClient(str(daemon.socket_path))
        await driver.start()
        async for _ev in driver.send("LONG-PROMPT"):
            pass
        session_id = str(driver.session_id)
        await driver.finalize()
        # The ring really has lost the start of the turn.
        assert daemon.ring._frames[0]["seq"] > 0

        app = DoxaApp(
            cwd=str(tmp_path),
            restore_tabs=[RestoreTabSpec(
                session_id=session_id,
                engine_factory=(lambda: EngineClient(
                    str(daemon.socket_path), skip_backlog=True)),
                cwd=str(tmp_path),
            )],
        )
        async with app.run_test() as pilot:
            pane = app.panes()[0]
            assert await _wait(pilot, lambda: any(
                b.prompt_text == "LONG-PROMPT" and b.assistant_text
                for b in _turn_blocks(pane)
            ))
            block = next(b for b in _turn_blocks(pane)
                         if b.prompt_text == "LONG-PROMPT")
            # The WHOLE answer, including the deltas the ring dropped.
            assert "D0 " in block.assistant_text
            assert "D59 " in block.assistant_text
    finally:
        if not serve.done():
            with contextlib.suppress(Exception):
                await daemon._shutdown("test teardown")
                await asyncio.wait_for(serve, 5)


# -- terminated sessions: the archived tab -------------------------------


def test_a_dead_session_with_a_transcript_resolves_as_archived(tmp_path):
    scope = str(tmp_path)
    _write_transcript("sid-dead", scope, [{"prompt": "p", "text": "a"}])
    tabsets.save(scope, [
        tabsets.TabRecord("sid-dead", "kept name", scope),
        tabsets.TabRecord("sid-vanished", None, scope),
    ], "sid-dead")
    resolved = tabsets.resolve(scope)
    assert [t.session_id for t in resolved.archived] == ["sid-dead"]
    assert resolved.archived[0].pinned_name == "kept name"
    assert resolved.skipped == 1  # no daemon AND no transcript
    assert resolved.active_session_id == "sid-dead"  # archived counts as survived
    assert [t.session_id for t, _e in resolved.ordered()] == ["sid-dead"]


@pytest.mark.asyncio
async def test_an_archived_tab_shows_its_transcript_read_only(tmp_path):
    _write_transcript("sid-gone", str(tmp_path), [
        {"prompt": "ARCHIVED-PROMPT", "text": "ARCHIVED-ANSWER"},
    ])
    factory = _restore_engine("sid-live")
    app = DoxaApp(
        cwd=str(tmp_path),
        engine_factory=factory,
        new_session_factory=factory,
        restore_tabs=[RestoreTabSpec(
            "sid-gone", pinned_name="old work", cwd=str(tmp_path), archived=True,
        )],
        restore_report="tab restore: 1 read-only transcript (session ended).",
    )
    async with app.run_test() as pilot:
        assert await _wait(pilot, lambda: app.archived_tabs())
        tab = app.archived_tabs()[0]
        assert await _wait(
            pilot, lambda: _rendered(tab) == [
                ("ARCHIVED-PROMPT", "ARCHIVED-ANSWER")]
        )
        # Read-only: no engine, no prompt box anywhere in this tab.
        assert not tab.query("#prompt-input")
        assert any("this session has ended" in b.text
                   for b in tab.query(SystemBlock))
        # And a usable session came up beside it -- an all-archived
        # restore must never leave the window with nothing to type into.
        assert len(app.panes()) == 1


@pytest.mark.asyncio
async def test_an_archived_tab_stays_in_the_persisted_set(tmp_path):
    """It survived one restart; it must survive the next. Dropping it from
    the record on the first save would make an archived tab a one-launch
    curiosity instead of a restored tab."""
    _write_transcript("sid-gone", str(tmp_path), [{"prompt": "p", "text": "a"}])
    factory = _restore_engine("sid-live")
    app = DoxaApp(
        cwd=str(tmp_path),
        engine_factory=factory,
        new_session_factory=factory,
        restore_tabs=[RestoreTabSpec(
            "sid-gone", pinned_name="old work", cwd=str(tmp_path), archived=True,
        )],
    )
    async with app.run_test() as pilot:
        assert await _wait(pilot, lambda: app.panes() and app.panes()[0]._session_id)
        await pilot.pause()
    record = tabsets.load(str(tmp_path))
    assert record is not None
    saved = {t.session_id: t for t in record.tabs}
    assert "sid-gone" in saved
    assert saved["sid-gone"].pinned_name == "old work"
    assert saved["sid-gone"].cwd == str(tmp_path)


@pytest.mark.asyncio
async def test_closing_an_archived_tab_takes_it_out_of_the_record(tmp_path):
    _write_transcript("sid-gone", str(tmp_path), [{"prompt": "p", "text": "a"}])
    factory = _restore_engine("sid-live")
    app = DoxaApp(
        cwd=str(tmp_path),
        engine_factory=factory,
        new_session_factory=factory,
        restore_tabs=[RestoreTabSpec(
            "sid-gone", cwd=str(tmp_path), archived=True)],
    )
    async with app.run_test() as pilot:
        assert await _wait(pilot, lambda: app.archived_tabs())
        assert await _wait(pilot, lambda: app.panes() and app.panes()[0]._session_id)
        tabbed = app.query_one("#session-tabs")
        archived_id = app.archived_tabs()[0].id
        tabbed.active = archived_id
        assert await _wait(pilot, lambda: tabbed.active == archived_id)
        await app.action_close_tab()
        assert await _wait(pilot, lambda: not app.archived_tabs())
        await pilot.pause()
    record = tabsets.load(str(tmp_path))
    assert record is None or "sid-gone" not in {t.session_id for t in record.tabs}


@pytest.mark.asyncio
async def test_ctrl_q_closes_an_archived_tab_and_leaves_the_live_one_alone(tmp_path):
    """v0.57.0. An archived tab's session ended before this window opened,
    so Ctrl+Q -- "end this session (finalize now) and close its tab" -- has
    nothing to finalize. Through v0.56.0 it therefore did nothing, and a
    read-only transcript was a tab the close key would not close.

    It now closes, taking the archive out of the persisted set exactly as
    Ctrl+W does -- and the LIVE session in the neighbouring tab keeps
    running, which is what says the key stayed scoped to the visible
    tab."""
    _write_transcript("sid-gone", str(tmp_path), [{"prompt": "p", "text": "a"}])
    factory = _restore_engine("sid-live")
    app = DoxaApp(
        cwd=str(tmp_path),
        engine_factory=factory,
        new_session_factory=factory,
        restore_tabs=[RestoreTabSpec(
            "sid-gone", cwd=str(tmp_path), archived=True)],
    )
    async with app.run_test() as pilot:
        assert await _wait(pilot, lambda: app.archived_tabs())
        assert await _wait(pilot, lambda: app.panes() and app.panes()[0]._session_id)
        tabbed = app.query_one("#session-tabs")
        archived_id = app.archived_tabs()[0].id
        tabbed.active = archived_id
        assert await _wait(pilot, lambda: tabbed.active == archived_id)

        await pilot.press("ctrl+q")
        assert await _wait(pilot, lambda: not app.archived_tabs())
        live = app.panes()
        assert len(live) == 1 and live[0].is_mounted
        assert live[0].engine is not None  # not detached, not finalized
        await pilot.pause()
    record = tabsets.load(str(tmp_path))
    assert record is None or "sid-gone" not in {t.session_id for t in record.tabs}


def test_the_report_names_read_only_tabs_separately():
    """"restored" and "read-only" must never read as the same thing: one
    is a session you can type into, the other is a transcript. The user
    learns which they got when the window opens, not when they type."""
    from doxa import cli as cli_mod

    assert cli_mod._restore_report_text(2, 0, 0) == "tab restore: restored 2 tabs."
    assert cli_mod._restore_report_text(0, 0, 1) == (
        "tab restore: 1 read-only transcript (session ended)."
    )
    assert cli_mod._restore_report_text(2, 1, 3) == (
        "tab restore: restored 2 tabs, 3 read-only transcripts (session ended), "
        "skipped 1 session no longer running."
    )
    assert cli_mod._restore_report_text(0, 0, 0) is None


# -- the record format ---------------------------------------------------


def test_the_record_carries_a_layout_node_beside_the_flat_tab_list(tmp_path):
    """Forward compatibility for a split tree that does not exist yet: the
    node is written, the flat list stays authoritative, and an older DOXA
    reading only ``tabs`` is unaffected."""
    scope = str(tmp_path)
    tabsets.save(scope, [tabsets.TabRecord("sid-1", "n", "/some/cwd")], "sid-1")
    data = json.loads(tabsets._file_for(scope).read_text(encoding="utf-8"))
    assert data["layout"] == {"kind": "tabs", "tabs": data["tabs"]}
    assert data["tabs"][0] == {
        "session_id": "sid-1", "pinned_name": "n", "cwd": "/some/cwd",
    }


def test_a_record_with_only_a_layout_node_still_loads(tmp_path):
    scope = str(tmp_path)
    path = tabsets._file_for(scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "scope_key": scope,
        "active_session_id": "sid-2",
        "layout": {"kind": "tabs", "tabs": [
            {"session_id": "sid-1", "pinned_name": None, "cwd": None},
            {"session_id": "sid-2", "pinned_name": "two", "cwd": "/w"},
        ]},
    }), encoding="utf-8")
    record = tabsets.load(scope)
    assert [t.session_id for t in record.tabs] == ["sid-1", "sid-2"]
    assert record.tabs[1].cwd == "/w"


def test_a_pre_v030_record_without_cwd_still_loads(tmp_path):
    scope = str(tmp_path)
    path = tabsets._file_for(scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "scope_key": scope,
        "active_session_id": "sid-1",
        "tabs": [{"session_id": "sid-1", "pinned_name": "old"}],
    }), encoding="utf-8")
    record = tabsets.load(scope)
    assert record.tabs[0] == tabsets.TabRecord("sid-1", "old", None)


def test_a_layout_kind_this_version_cannot_render_reads_as_nothing(tmp_path):
    """The day splits exist, an older DOXA meeting a split record must
    show no restore rather than a split silently flattened into tabs."""
    scope = str(tmp_path)
    path = tabsets._file_for(scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "scope_key": scope,
        "layout": {"kind": "split", "orientation": "vertical", "children": []},
    }), encoding="utf-8")
    assert tabsets.load(scope) is None
