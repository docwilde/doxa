"""Streaming-deriver tests (DOXA_DERIVE_SECS): the debounced mid-session
review that reuses the finalize/PreCompact deriver path.

Contracts pinned here: default OFF; at most one review per interval
(debounce); never more than one in flight; NEVER concurrent with finalize
(the review lock serializes them, and a finalize-first race makes the
derive bail); staged proposals surface as the out-of-band derive_done
event, which the TUI renders as the '/lore:pending' notification block.
The review runner itself is monkeypatched throughout -- lore_core's
machinery is reused, not reimplemented, so these tests assert WHEN it
runs, never WHAT it derives.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from claude_agent_sdk import ResultMessage

from doxa.app import DoxaApp, SystemBlock
from doxa.engine import EngineEvent, SessionEngine, derive_interval
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
    monkeypatch.delenv("DOXA_DERIVE_SECS", raising=False)
    assert derive_interval() is None  # default OFF
    monkeypatch.setenv("DOXA_DERIVE_SECS", "")
    assert derive_interval() is None
    monkeypatch.setenv("DOXA_DERIVE_SECS", "0")
    assert derive_interval() is None
    monkeypatch.setenv("DOXA_DERIVE_SECS", "-5")
    assert derive_interval() is None
    monkeypatch.setenv("DOXA_DERIVE_SECS", "banana")
    assert derive_interval() is None
    monkeypatch.setenv("DOXA_DERIVE_SECS", "90")
    assert derive_interval() == 90.0
    monkeypatch.setenv("DOXA_DERIVE_SECS", "0.5")
    assert derive_interval() == 0.5


@pytest.mark.asyncio
async def test_derive_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("DOXA_DERIVE_SECS", raising=False)
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


@pytest.mark.asyncio
async def test_staged_proposals_surface_as_derive_done_event(tmp_path, monkeypatch):
    monkeypatch.setenv("DOXA_DERIVE_SECS", "0.001")
    engine = await _engine(tmp_path)
    counts = iter([1, 3])  # before, after: two newly staged proposals
    monkeypatch.setattr(engine, "_pending_count", lambda: next(counts))
    monkeypatch.setattr(engine, "_run_review_sync", lambda older: None)
    engine._last_derive = time.monotonic() - 60
    await _turn(engine)
    await engine._derive_task
    ev = engine._peer_queue.get_nowait()
    assert ev.type == "derive_done"
    assert ev.data == {"staged": 2}
    await engine.finalize()


@pytest.mark.asyncio
async def test_no_event_when_nothing_new_staged(tmp_path, monkeypatch):
    monkeypatch.setenv("DOXA_DERIVE_SECS", "0.001")
    engine = await _engine(tmp_path)
    monkeypatch.setattr(engine, "_pending_count", lambda: 4)
    monkeypatch.setattr(engine, "_run_review_sync", lambda older: None)
    engine._last_derive = time.monotonic() - 60
    await _turn(engine)
    await engine._derive_task
    assert engine._peer_queue.empty()
    await engine.finalize()


@pytest.mark.asyncio
async def test_tui_renders_the_pending_notification(monkeypatch, tmp_path):
    fake = FakeEngine([])
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        fake.push_peer_event(EngineEvent("derive_done", {"staged": 3}))

        def _blocks():
            return [b for b in app.query(SystemBlock) if b.id != "identity-block"]

        for _ in range(100):
            if _blocks():
                break
            await pilot.pause(0.02)
        blocks = _blocks()
        assert len(blocks) == 1
        assert "3 proposals staged — /lore:pending" in blocks[0].text
