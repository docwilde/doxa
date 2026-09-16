# SPDX-License-Identifier: AGPL-3.0-only
"""The user-reported bug and the feature it becomes: typing a prompt while
a turn is still running used to (1) error at the daemon
("a turn is already running in this session") AND (2) leave the FIRST
turn's own transcript block frozen mid-thought forever.

Root cause (measured, not assumed): ``SessionPane.on_prompt_submitted``
ran every submitted prompt's ``_run_turn`` worker with
``exclusive=True`` on Textual's ``"turn"`` worker group. Textual cancels
whatever else is BUSY in that group, on that same node, before starting
the new one -- so a second prompt typed in the SAME pane cancelled the
FIRST prompt's own ``_run_turn`` coroutine mid-``async for``, and that
coroutine had no ``try/finally`` to recover in: ``turn_in_flight`` stuck
``True`` and the block's elapsed-time ticker never stopped, which is what
"hangs indefinitely" looks like from the seat in front of the screen.
Meanwhile the SECOND prompt's OWN worker got the daemon's flat refusal,
rendered as a visible "turn failed" line -- the error half of the bug.

This file pins the fix at the ONE layer both the daemon-backed
``EngineClient`` path and the in-process ``SessionEngine`` path share:
``doxa.session.pane``/``doxa.session.runtime``. ``PacedQueueEngine``
below reproduces the mid-turn-queue CONTRACT both real engines now
implement (queue when busy, yield nothing for the ack, auto-advance over
``peer_events()`` once the running turn ends) closely enough to drive
the pane exactly the way a real daemon or in-process session would --
without a subprocess, the SDK, or a socket.
"""

from __future__ import annotations

import asyncio

import pytest
from textual.containers import VerticalScroll

from doxa.app import DoxaApp, TurnBlock
from doxa.engine import EngineEvent
from doxa.promptqueue import PROMPT_QUEUE_MAXLEN, PromptQueue
from tests.fakes import FakeEngine

ANSWER_PREFIX = "the-answer-to-"


class PacedQueueEngine(FakeEngine):
    """A FakeEngine that mimics the real engines' mid-turn queue contract
    (doxa.promptqueue) closely enough to drive on_prompt_submitted/
    _run_turn through it, with pacing (opened/release) borrowed from
    tests/test_turn_survives_new_tab.py's HalfwayEngine for the same
    reason it exists there: FakeEngine.send() replays its whole script
    in one loop turn, which cannot reproduce "a second prompt arrives
    while the first is still running"."""

    def __init__(self) -> None:
        super().__init__([])
        self.opened = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        self.completed_prompts: "list[str]" = []
        self._busy = False
        self._queue = PromptQueue()

    async def send(self, prompt: str):  # type: ignore[override]
        self.received_prompts.append(prompt)
        if self._busy:
            item = self._queue.enqueue(prompt)  # may raise PromptQueueFull
            position = self._queue.position(item.id) or len(self._queue)
            # Yielded directly to THIS caller -- matches the real
            # engines' contract (see doxa.engine.SessionEngine.send's
            # docstring): _run_turn peeks at the first event and treats
            # "prompt_queued" as the whole answer, never mounting a
            # turn block for it.
            yield EngineEvent("prompt_queued", {
                "id": item.id, "text": prompt, "position": position,
            })
            return
        self._busy = True
        try:
            async for ev in self._one_turn(prompt):
                yield ev
        except (GeneratorExit, asyncio.CancelledError):
            self.cancelled = True
            self._busy = False
            raise
        self._busy = False
        self._advance()

    async def _one_turn(self, prompt: str):
        yield EngineEvent("turn_started", {})
        yield EngineEvent("text_delta", {"text": "thinking… "})
        self.opened.set()
        await self.release.wait()
        yield EngineEvent("text_delta", {"text": f"{ANSWER_PREFIX}{prompt}"})
        yield EngineEvent("turn_done", {
            "cost_usd": 0.001, "duration_ms": 5, "is_error": False,
            "session_cost_usd": 0.001, "ctx_percentage": 1.0,
        })
        self.completed_prompts.append(prompt)

    def _advance(self) -> None:
        item = self._queue.pop_next()
        if item is None:
            return
        self.push_peer_event(EngineEvent("prompt_dequeued", {
            "id": item.id, "text": item.text,
        }))
        asyncio.ensure_future(self._run_queued_turn(item.text))

    async def _run_queued_turn(self, prompt: str) -> None:
        self._busy = True
        try:
            async for ev in self._one_turn(prompt):
                self.push_peer_event(ev)
        except (GeneratorExit, asyncio.CancelledError):
            self._busy = False
            raise
        self._busy = False
        self._advance()


def _app(tmp_path):
    engines: "list[PacedQueueEngine]" = []

    def make() -> PacedQueueEngine:
        engines.append(PacedQueueEngine())
        return engines[-1]

    app = DoxaApp(
        cwd=str(tmp_path), engine_factory=make, new_session_factory=make,
    )
    return app, engines


async def _wait(pilot, cond, tries=200):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(0.02)
    return cond()


def _painted(app) -> str:
    """Everything the terminal actually receives, as plain text -- the
    same composited route tests/test_turn_survives_new_tab.py takes, for
    the same reason: what matters is what reaches the screen, not what a
    widget's own model claims."""
    return "\n".join(
        "".join(segment.text for segment in strip)
        for strip in app.screen._compositor.render_strips()
    )


async def _submit(pilot, pane, text: str) -> None:
    pane.query_one("#prompt-input").value = text
    await pilot.press("enter")


@pytest.mark.asyncio
async def test_a_second_prompt_mid_turn_does_not_cancel_or_hang_the_first(tmp_path):
    """THE REGRESSION. Submit "first"; while it is still running (opened
    but not released), submit "second" in the SAME pane. Before the fix:
    Textual's exclusive "turn" worker group cancelled "first"'s own
    _run_turn, engine.cancelled became True, turn_in_flight stuck True
    forever, and the block never reached turn_done. After the fix:
    "first" is untouched -- it is not cancelled, it reaches its own
    turn_done, turn_in_flight returns to False, and its answer lands in
    the transcript, exactly as if "second" had never been typed."""
    app, engines = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert await _wait(pilot, lambda: engines and engines[0].started)
        pane = app.panes()[0]
        engine = engines[0]

        await _submit(pilot, pane, "first")
        assert await _wait(pilot, lambda: engine.opened.is_set())
        assert pane.turn_in_flight

        # Typed WHILE "first" is still running, in the SAME pane -- the
        # exact scenario that used to cancel "first"'s own worker.
        await _submit(pilot, pane, "second")
        await pilot.pause(0.05)

        # "first" is NOT cancelled and NOT stuck: it is still legitimately
        # in flight (this is not the bug -- it just hasn't been released
        # yet), and its generator was never torn down from outside.
        assert not engine.cancelled, (
            "a prompt typed mid-turn CANCELLED the running turn's own "
            "generator -- the exact defect this feature replaces"
        )
        assert pane.turn_in_flight

        engine.release.set()
        assert await _wait(
            pilot, lambda: "first" in engine.completed_prompts or engine.cancelled,
        )
        assert not engine.cancelled
        assert await _wait(pilot, lambda: not pane.turn_in_flight), (
            "turn_in_flight never returned to False after the first "
            "turn's own turn_done -- this is the hang"
        )
        assert await _wait(
            pilot,
            lambda: f"{ANSWER_PREFIX}first" in "".join(
                b.assistant_text for b in pane.query(TurnBlock)
            ),
        )

        # Settle: "second" auto-started the moment "first" finished (its
        # own release is already set) -- let it render out fully before
        # the pilot tears down, so no orphaned mount races the teardown.
        assert await _wait(
            pilot,
            lambda: f"{ANSWER_PREFIX}second" in "".join(
                b.assistant_text for b in pane.query(TurnBlock)
            ),
        )


@pytest.mark.asyncio
async def test_a_mid_turn_prompt_is_acknowledged_as_queued_not_refused(tmp_path):
    """The other half of the fix: "second" is never shown as a failed
    turn (the OLD "a turn is already running" refusal) -- it is
    acknowledged as queued, visibly, in the transcript."""
    app, engines = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert await _wait(pilot, lambda: engines and engines[0].started)
        pane = app.panes()[0]
        engine = engines[0]

        await _submit(pilot, pane, "first")
        assert await _wait(pilot, lambda: engine.opened.is_set())

        await _submit(pilot, pane, "second")
        assert await _wait(pilot, lambda: "queued" in _painted(app))
        assert "turn failed" not in _painted(app)
        assert "already running" not in _painted(app)

        engine.release.set()
        assert await _wait(pilot, lambda: "second" in engine.completed_prompts)
        assert await _wait(
            pilot,
            lambda: f"{ANSWER_PREFIX}second" in "".join(
                b.assistant_text for b in pane.query(TurnBlock)
            ),
        ), "the queued prompt never started automatically after turn_done"


@pytest.mark.asyncio
async def test_several_mid_turn_prompts_start_in_fifo_order(tmp_path):
    app, engines = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert await _wait(pilot, lambda: engines and engines[0].started)
        pane = app.panes()[0]
        engine = engines[0]

        await _submit(pilot, pane, "first")
        assert await _wait(pilot, lambda: engine.opened.is_set())
        for text in ("second", "third", "fourth"):
            await _submit(pilot, pane, text)
            await pilot.pause(0.02)

        engine.release.set()
        assert await _wait(
            pilot,
            lambda: engine.completed_prompts == ["first", "second", "third", "fourth"],
            tries=400,
        ), f"wrong order: {engine.completed_prompts}"
        # Settle: let the last queued turn's own render finish before the
        # pilot tears down (see the regression test's own note on this).
        assert await _wait(
            pilot,
            lambda: f"{ANSWER_PREFIX}fourth" in "".join(
                b.assistant_text for b in pane.query(TurnBlock)
            ),
            tries=400,
        )


@pytest.mark.asyncio
async def test_the_bound_is_enforced_with_a_clear_message_not_a_silent_drop(tmp_path):
    app, engines = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert await _wait(pilot, lambda: engines and engines[0].started)
        pane = app.panes()[0]
        engine = engines[0]

        await _submit(pilot, pane, "first")
        assert await _wait(pilot, lambda: engine.opened.is_set())
        for i in range(PROMPT_QUEUE_MAXLEN):
            await _submit(pilot, pane, f"queued-{i}")
            await pilot.pause(0.01)

        await _submit(pilot, pane, "one too many")
        assert await _wait(pilot, lambda: "queue is full" in _painted(app))

        engine.release.set()
        assert await _wait(
            pilot, lambda: len(engine.completed_prompts) == 1 + PROMPT_QUEUE_MAXLEN,
            tries=400,
        )
        # Settle: let the last queued turn's own render finish before the
        # pilot tears down (see the regression test's own note on this).
        last = f"queued-{PROMPT_QUEUE_MAXLEN - 1}"
        assert await _wait(
            pilot,
            lambda: f"{ANSWER_PREFIX}{last}" in "".join(
                b.assistant_text for b in pane.query(TurnBlock)
            ),
            tries=400,
        )
