# SPDX-License-Identifier: AGPL-3.0-only
"""Item N -- clipboard paste, the Pilot half.

Pure collapse/normalize rules are pinned in tests/test_paste.py; this file
proves the prompt widget itself: a multi-line bracketed paste is ONE edit
and can never spuriously submit, CRLF/CR both normalize, a paste over the
collapse threshold lands as an expandable placeholder and still resolves
to its full text at submit time, the box grows to a cap then stops, Enter
submits while Shift+Enter/Alt+Enter insert a literal newline, and Ctrl+V
is a deliberate no-op (never Textual's own stale in-app-clipboard paste).
"""

from __future__ import annotations

import pytest
from textual import events

from doxa import paste as paste_mod
from doxa.app import DoxaApp, PromptInput, SystemBlock, TurnBlock
from doxa.engine import EngineEvent
from tests.fakes import FakeEngine

SCRIPT = [
    EngineEvent("turn_started", {}),
    EngineEvent("text_delta", {"text": "ok"}),
    EngineEvent("turn_done", {"cost_usd": 0.0, "duration_ms": 5, "is_error": False}),
]


async def _app(monkeypatch, tmp_path, peers=None):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    fake = FakeEngine(SCRIPT, peers=peers or [])
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    return DoxaApp(cwd=str(tmp_path)), fake


def _turn_blocks(app):
    return list(app.query(TurnBlock))


@pytest.mark.asyncio
async def test_small_paste_lands_inline_not_collapsed(monkeypatch, tmp_path):
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt-input", PromptInput)
        prompt.focus()
        prompt.post_message(events.Paste("one\ntwo\nthree"))
        await pilot.pause()
        assert prompt.value == "one\ntwo\nthree"
        assert prompt._pending_pastes == []


@pytest.mark.asyncio
async def test_multiline_paste_is_one_prompt_and_never_submits(monkeypatch, tmp_path):
    """The headline claim of item N.1: a bracketed paste containing N
    newlines must never be mistaken for N presses of Enter."""
    app, fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt-input", PromptInput)
        prompt.focus()
        text = "\n".join(f"line {i}" for i in range(20))
        prompt.post_message(events.Paste(text))
        await pilot.pause()
        # Collapsed (20 lines is well past COLLAPSE_LINES) -- one line in
        # the document, not twenty.
        assert prompt.value.count("\n") == 0
        assert prompt.value.startswith("⧉ pasted 20 lines")
        assert _turn_blocks(app) == []
        assert fake.received_prompts == []


@pytest.mark.asyncio
async def test_crlf_and_lone_cr_normalize_on_paste(monkeypatch, tmp_path):
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt-input", PromptInput)
        prompt.focus()
        prompt.post_message(events.Paste("a\r\nb\rc"))
        await pilot.pause()
        assert prompt.value == "a\nb\nc"
        assert "\r" not in prompt.value


@pytest.mark.asyncio
async def test_collapsed_paste_resolves_to_full_text_on_submit_without_expanding(
    monkeypatch, tmp_path
):
    """Item N.3: "full content on submit" -- even if the operator never
    looked at (expanded) the placeholder."""
    app, fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt-input", PromptInput)
        prompt.focus()
        text = "\n".join(f"line {i}" for i in range(10))
        prompt.post_message(events.Paste(text))
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        for _ in range(50):
            if fake.received_prompts:
                break
            await pilot.pause(0.02)
        assert fake.received_prompts == [text]
        assert prompt.value == ""  # cleared, and pending pastes forgotten
        assert prompt._pending_pastes == []


@pytest.mark.asyncio
async def test_ctrl_g_expands_a_pending_placeholder_in_place(monkeypatch, tmp_path):
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt-input", PromptInput)
        prompt.focus()
        text = "\n".join(f"line {i}" for i in range(6))
        prompt.post_message(events.Paste(text))
        await pilot.pause()
        assert prompt.value.startswith("⧉ pasted 6 lines")
        await pilot.press("ctrl+g")
        await pilot.pause()
        assert prompt.value == text
        assert prompt._pending_pastes == []


@pytest.mark.asyncio
async def test_enter_submits_shift_and_alt_enter_insert_newline(monkeypatch, tmp_path):
    app, fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt-input", PromptInput)
        prompt.focus()
        for ch in "line1":
            await pilot.press(ch)
        await pilot.press("shift+enter")
        for ch in "line2":
            await pilot.press(ch)
        await pilot.press("alt+enter")
        for ch in "line3":
            await pilot.press(ch)
        await pilot.pause()
        assert prompt.value == "line1\nline2\nline3"
        assert _turn_blocks(app) == []  # neither newline key submitted

        await pilot.press("enter")
        await pilot.pause()
        for _ in range(50):
            if fake.received_prompts:
                break
            await pilot.pause(0.02)
        assert fake.received_prompts == ["line1\nline2\nline3"]


@pytest.mark.asyncio
async def test_box_grows_to_a_cap_then_stops(monkeypatch, tmp_path):
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt-input", PromptInput)
        prompt.focus()
        assert prompt.styles.height.value == PromptInput.MIN_ROWS + 2

        for _ in range(3):
            await pilot.press("x")
            await pilot.press("shift+enter")
        await pilot.pause()
        # "x", "x", "x", "" -- three shift+enters make four lines.
        assert prompt.styles.height.value == 4 + 2

        # Now blow well past the cap.
        for _ in range(30):
            await pilot.press("x")
            await pilot.press("shift+enter")
        await pilot.pause()
        assert prompt.styles.height.value == PromptInput.MAX_ROWS + 2


@pytest.mark.asyncio
async def test_ctrl_v_is_a_deliberate_noop_not_the_stale_app_clipboard(
    monkeypatch, tmp_path
):
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._clipboard = "stale in-app text, not the real clipboard"
        prompt = app.query_one("#prompt-input", PromptInput)
        prompt.focus()
        await pilot.press("ctrl+v")
        await pilot.pause()
        assert prompt.value == ""


@pytest.mark.asyncio
async def test_empty_paste_with_clipboard_image_reports_a_stub_notice(
    monkeypatch, tmp_path
):
    """Item N.4: a terminal can never forward binary clipboard content
    through bracketed paste -- an EMPTY paste plus a clipboard that turns
    out to hold an image is the only signal available, and there is no
    attachment plumbing in the engine yet, so the honest response is a
    reported stub, not a silent no-op or a fake attachment."""
    monkeypatch.setattr(paste_mod, "detect_clipboard_image_mime", lambda: "image/png")
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt-input", PromptInput)
        prompt.focus()
        prompt.post_message(events.Paste(""))
        for _ in range(50):
            blocks = [b for b in app.query(SystemBlock) if b.id != "identity-block"]
            if blocks:
                break
            await pilot.pause(0.02)
        else:
            blocks = []
        assert blocks, "no clipboard-image notice was ever mounted"
        assert "image/png" in str(blocks[-1].renderable)
        assert prompt.value == ""


@pytest.mark.asyncio
async def test_empty_paste_with_text_clipboard_stays_silent(monkeypatch, tmp_path):
    monkeypatch.setattr(paste_mod, "detect_clipboard_image_mime", lambda: None)
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt-input", PromptInput)
        prompt.focus()
        prompt.post_message(events.Paste(""))
        await pilot.pause(0.1)
        blocks = [b for b in app.query(SystemBlock) if b.id != "identity-block"]
        assert blocks == []
