"""Render a README screenshot of the DOXA shell — no live SDK, no spend.

Drives DoxaApp headlessly with a scripted FakeEngine (the pilot-test
fixture), plays a richer multi-turn session than the test uses, and saves
Textual's SVG screenshot to assets/screenshot.svg. Convert to PNG with
inkscape (CI does not need to; the SVG is committed too).

    uv run python scripts/screenshot.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from doxa.engine import EngineEvent  # noqa: E402
from tests.fakes import FakeEngine  # noqa: E402

SCRIPT = [
    EngineEvent("peer_joined", {"session_id": "b3f2a1c9", "title": "paper draft session"}),
    EngineEvent("turn_started", {}),
    EngineEvent("text_delta", {"text": "Two beliefs about this repo are relevant — one is "}),
    EngineEvent("text_delta", {"text": "calibrated (STEER), one is cite-only until it earns a track record."}),
    EngineEvent("tool_call", {"id": "t1", "name": "lore_belief_search",
                              "input": {"query": "deploy checklist", "scope": "project"}}),
    EngineEvent("tool_result", {"id": "t1", "name": "lore_belief_search",
                                "result_summary": "2 beliefs: #184 STEER (0.91 · 12 outcomes) · #201 CITE",
                                "is_error": False, "duration_ms": 45}),
    EngineEvent("turn_done", {"cost_usd": 0.0031, "duration_ms": 1840, "is_error": False,
                              "session_cost_usd": 0.0031, "ctx_percentage": 11.0}),
]


async def main() -> None:
    import doxa.app as app_mod
    orig = app_mod.SessionEngine
    app_mod.SessionEngine = lambda cwd, model=None: FakeEngine(SCRIPT)
    try:
        app = app_mod.DoxaApp(cwd=".")
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.query_one("#prompt-input").value = "what do we believe about deploys here?"
            await pilot.press("enter")
            for _ in range(200):
                blocks = list(app.query(app_mod.TurnBlock))
                if blocks and "earns a track record" in blocks[0].assistant_text:
                    break
                await pilot.pause(0.02)
            await pilot.pause(0.2)
            out = Path(__file__).resolve().parents[1] / "assets" / "screenshot.svg"
            app.save_screenshot(str(out))
            print(f"saved {out}")
    finally:
        app_mod.SessionEngine = orig


if __name__ == "__main__":
    asyncio.run(main())
