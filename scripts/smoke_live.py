"""Live smoke test -- NOT part of `uv run pytest` (no test_*.py name, not
under tests/), because it spends real Claude subscription quota on a real
`claude` CLI subprocess. Run manually: `uv run python scripts/smoke_live.py`.

Proves the whole stack end to end, headlessly, the same way
spike/03_textual_marriage.py proved the SDK+Textual coexistence claim in
Phase 0: a real ClaudeSDKClient session, driven by the real SessionEngine,
rendered live into a real DoxaApp turn block -- via Textual's run_test()
Pilot harness, no terminal needed, no `ANTHROPIC_API_KEY` set (subscription
OAuth only, asserted below same as every Phase 0 spike did).

Cheapest model, one short prompt, LORE review disabled for the smoke run
(LORE_DISABLE_REVIEW=1) so finalize() doesn't spend a second headless
`claude -p` call reviewing a two-message transcript that would fall under
REVIEW_MIN_MESSAGES anyway -- belt and suspenders on the "keep spend
minimal" constraint.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("LORE_DISABLE_REVIEW", "1")

# Isolate LORE state for this run, same reasoning as tests/conftest.py: must
# happen before doxa (and therefore lore_core) is imported anywhere.
_tmp = Path(tempfile.mkdtemp(prefix="doxa-smoke-lore-"))
os.environ.setdefault("LORE_ROOT", str(_tmp / "lore"))
os.environ.setdefault("LORE_PROJECTS_DIR", str(_tmp / "projects"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from doxa.app import DoxaApp, TurnBlock  # noqa: E402


async def main() -> None:
    assert "ANTHROPIC_API_KEY" not in os.environ, (
        "ANTHROPIC_API_KEY is set -- this smoke test is supposed to prove "
        "subscription-OAuth-only auth, per PHASE0_FINDINGS.md SS3. Unset it."
    )

    cwd = str(Path(__file__).resolve().parent.parent)
    app = DoxaApp(cwd=cwd, model="claude-haiku-4-5")

    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        for _ in range(200):  # up to ~20s for the engine to connect
            if app.engine is not None and app.engine.model:
                break
            await pilot.pause(0.1)

        prompt_input = app.query_one("#prompt-input")
        prompt_input.value = "Say 'ok' and nothing else."
        await pilot.press("enter")

        block = None
        for _ in range(600):  # up to ~60s for a real turn
            blocks = list(app.query(TurnBlock))
            if blocks and blocks[0].assistant_text:
                block = blocks[0]
                break
            await pilot.pause(0.1)

        assert block is not None, "no turn block ever received assistant text"
        print(f"[smoke] assistant text: {block.assistant_text!r}")
        print(f"[smoke] session cost so far: ${app.engine.total_cost_usd:.4f}")

        status = app.query_one("#status-bar").renderable
        print(f"[smoke] status bar: {status}")

        await app.action_quit()

    print("[smoke] PASS -- real SDK turn rendered live in a headless DoxaApp")


if __name__ == "__main__":
    asyncio.run(main())
