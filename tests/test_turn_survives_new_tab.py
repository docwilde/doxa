# SPDX-License-Identifier: AGPL-3.0-only
"""Reported live: *"when a request is running in one tab and i open
another, and then i switch back, the old request seem to have been
interrupted and i dont see its result"*.

Two failure modes fit that sentence and they need opposite fixes, so
every test here separates them explicitly rather than asserting one
compound thing:

1. the turn really was CANCELLED -- the agent stopped, the answer was
   never produced. ``HalfwayEngine.cancelled`` is the direct evidence:
   the generator ``_run_turn`` is iterating records whether it was closed
   from outside instead of running to its own end.
2. the turn completed and its output was never SHOWN. Asserted against
   the composited screen -- what the terminal actually receives -- and
   never against ``TurnBlock.assistant_text``, which is the model behind
   the widget and says "the answer is here" in both worlds.

It is the second one. The turn survives Ctrl+T untouched; what did not
survive was the scroll (see ``SessionPane.scroll_transcript_to_end``),
so the answer landed in the pane and sat below the fold.

The engine below is the only thing this file adds to the FakeEngine
vocabulary: a ``send()`` that stops halfway on an ``asyncio.Event`` the
test holds. Every other fake in this suite replays its whole script
inside one loop turn, which is exactly the shape that CANNOT reproduce
this -- the turn is over before the second keystroke is pressed.
"""

from __future__ import annotations

import asyncio

import pytest
from textual.containers import VerticalScroll

from doxa.app import DoxaApp, TurnBlock
from doxa.engine import EngineEvent
from tests.fakes import FakeEngine

#: Deliberately long enough that the reply cannot fit the viewport: the
#: whole defect is about content that has scrolled past the bottom, and a
#: turn that fits on screen has no bottom to fall off.
FILLER = [f"line {i} of the reply" for i in range(40)]
ANSWER = "the-answer-the-user-is-waiting-for"


class HalfwayEngine(FakeEngine):
    """A FakeEngine whose turn stops in the middle and waits to be let go.

    ``opened`` is set once send() has yielded its first delta -- the point
    at which the turn is unambiguously in flight -- and ``release`` is
    what the test sets to let the rest of the answer arrive.
    ``cancelled`` records that the generator was closed from outside
    rather than running to its own end, which is the fact that tells the
    two candidate failure modes apart."""

    def __init__(self) -> None:
        super().__init__([])
        self.opened = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        self.completed = False

    def _tail(self) -> "list[EngineEvent]":
        events = [
            EngineEvent("text_delta", {"text": f"{line}\n\n"}) for line in FILLER
        ]
        events.append(EngineEvent("text_delta", {"text": ANSWER}))
        events.append(EngineEvent("turn_done", {
            "cost_usd": 0.002, "duration_ms": 20, "is_error": False,
            "session_cost_usd": 0.002, "ctx_percentage": 3.0,
        }))
        return events

    async def send(self, prompt: str):  # type: ignore[override]
        self.received_prompts.append(prompt)
        try:
            yield EngineEvent("turn_started", {})
            yield EngineEvent("text_delta", {"text": "thinking… "})
            self.opened.set()
            await self.release.wait()
            for event in self._tail():
                yield event
            self.completed = True
        except (GeneratorExit, asyncio.CancelledError):
            self.cancelled = True
            raise


def _app(tmp_path):
    engines: list[HalfwayEngine] = []

    def make() -> HalfwayEngine:
        engines.append(HalfwayEngine())
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
    """Everything the terminal actually receives, as plain text.

    Composited, never ``TurnBlock.assistant_text``: the whole question
    this file exists to answer is whether the answer REACHED THE USER,
    and the widget's own model says yes in both the broken and the fixed
    world. Same ``render_strips`` route tests/test_transcript_density.py
    already takes, for the same reason."""
    return "\n".join(
        "".join(segment.text for segment in strip)
        for strip in app.screen._compositor.render_strips()
    )


async def _submit(pilot, pane, text="the question"):
    pane.query_one("#prompt-input").value = text
    await pilot.press("enter")


@pytest.mark.asyncio
async def test_a_turn_in_flight_is_not_cancelled_by_opening_a_tab(tmp_path):
    """Failure mode 1, ruled out by measurement rather than by reading.

    Textual's exclusivity groups ARE node-scoped -- ``WorkerManager.
    cancel_group`` filters on ``worker.node == node`` (textual 5.3), and
    ``SessionPane.on_prompt_submitted`` runs its worker on the PANE -- so
    a second pane starting anything in group ``"turn"`` cannot touch the
    first pane's. This pins that, because if it ever stops being true the
    report comes back with a completely different cause."""
    app, engines = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert await _wait(pilot, lambda: engines and engines[0].started)
        first = app.panes()[0]
        await _submit(pilot, first)
        assert await _wait(pilot, lambda: engines[0].opened.is_set())
        assert first.turn_in_flight

        await pilot.press("ctrl+t")
        assert await _wait(pilot, lambda: len(engines) == 2 and engines[1].started)

        engines[0].release.set()
        assert await _wait(
            pilot, lambda: engines[0].completed or engines[0].cancelled
        )
        assert not engines[0].cancelled, (
            "opening a second tab CANCELLED the running turn -- the "
            "engine's send() generator was closed mid-stream"
        )
        assert engines[0].completed
        assert ANSWER in "".join(b.assistant_text for b in first.query(TurnBlock))


@pytest.mark.asyncio
async def test_the_answer_is_on_screen_after_ctrl_t_and_back(tmp_path):
    """THE REPORT, keystroke for keystroke, asserted on the screen.

    Submit in tab A, Ctrl+T while it runs, let the whole answer land while
    tab A is hidden, Ctrl+Left back. Before the fix this failed exactly
    here: the turn had completed, its text was in the widget, and the
    transcript was still sitting at the offset it held when the user left
    -- ``scroll_y == 0`` against ``max_scroll_y == 78`` -- so the screen
    showed the boot banner and the first two lines of a forty-line reply,
    and nothing on it said the answer had arrived."""
    app, engines = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert await _wait(pilot, lambda: engines and engines[0].started)
        first = app.panes()[0]
        await _submit(pilot, first)
        assert await _wait(pilot, lambda: engines[0].opened.is_set())

        await pilot.press("ctrl+t")
        assert await _wait(pilot, lambda: len(engines) == 2 and engines[1].started)
        assert app.active_pane is not first

        engines[0].release.set()
        assert await _wait(pilot, lambda: engines[0].completed)

        await pilot.press("ctrl+left")
        assert await _wait(pilot, lambda: app.active_pane is first)

        assert await _wait(pilot, lambda: ANSWER in _painted(app)), (
            "the turn finished but its answer is not on screen — the "
            "transcript is at "
            f"{first.query_one('#block-list', VerticalScroll).scroll_y} of "
            f"{first.query_one('#block-list', VerticalScroll).max_scroll_y}"
        )


@pytest.mark.asyncio
async def test_a_peer_driven_turn_in_a_background_tab_is_on_screen_too(tmp_path):
    """The SAME mechanism through the other renderer.

    ``_peer_pump`` draws turns this client did not drive -- replayed
    history after a reattach, or a turn another attached client of the
    same daemon is running -- and it scrolled to the tail the same way,
    with the same nothing happening in a background tab. The report named
    Ctrl+T because that is what the user did; this is the same answer lost
    without anyone pressing anything."""
    app, engines = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert await _wait(pilot, lambda: engines and engines[0].started)
        first = app.panes()[0]

        await pilot.press("ctrl+t")
        assert await _wait(pilot, lambda: len(engines) == 2 and engines[1].started)
        assert app.active_pane is not first

        # A turn arrives on the BACKGROUND pane's out-of-band stream.
        engines[0].push_peer_event(
            EngineEvent("turn_started", {"prompt": "asked from another client"})
        )
        for event in engines[0]._tail():
            engines[0].push_peer_event(event)
        assert await _wait(
            pilot,
            lambda: ANSWER in "".join(b.assistant_text for b in first.query(TurnBlock)),
        )

        await pilot.press("ctrl+left")
        assert await _wait(pilot, lambda: app.active_pane is first)
        assert await _wait(pilot, lambda: ANSWER in _painted(app))


@pytest.mark.asyncio
async def test_a_short_transcript_is_not_pushed_down_when_a_tab_is_shown(tmp_path):
    """The regression the obvious fix would have introduced.

    ``Widget.anchor()`` is textual's own "stay at the bottom" property and
    is the wrong tool here: its compositor branch writes the scroll offset
    with ``set_reactive``, bypassing ``validate_scroll_y``, so a
    transcript SHORTER than its container gets a large NEGATIVE
    ``scroll_y`` -- measured at ``-20`` on a 100x45 pane, with the boot
    banner shoved off the top under a screenful of blank rows. An idle
    pane that has never run a turn must come back looking exactly like
    itself."""
    app, engines = _app(tmp_path)
    async with app.run_test(size=(100, 45)) as pilot:
        await pilot.pause()
        assert await _wait(pilot, lambda: engines and engines[0].started)
        first = app.panes()[0]
        assert await _wait(pilot, lambda: first.query("#identity-block"))

        await pilot.press("ctrl+t")
        assert await _wait(pilot, lambda: len(engines) == 2 and engines[1].started)
        await pilot.press("ctrl+left")
        assert await _wait(pilot, lambda: app.active_pane is first)
        await pilot.pause()

        block_list = first.query_one("#block-list", VerticalScroll)
        assert block_list.scroll_y >= 0, (
            "a transcript that fits its pane was scrolled to a negative "
            f"offset ({block_list.scroll_y}) — the opening blocks are "
            "pushed down off the top of the pane"
        )
        assert "DOXA" in _painted(app)
