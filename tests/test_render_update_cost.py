"""Regression checks for work skipped on unchanged UI updates."""

from __future__ import annotations

import asyncio
import threading

import pytest

from doxa import diff as diff_mod
from doxa.ui import diffview, statusline, transcript
from doxa.ui.prompt import PromptInput


def test_hunk_reformats_only_when_width_changes(monkeypatch):
    hunk = diff_mod.Hunk("@@ -1,1 +1,1 @@", 1, 1, 1, 1, ("-old", "+new"))
    file_diff = diff_mod.FileDiff("file.txt", hunks=(hunk,))
    view = diffview.HunkView(file_diff, hunk)
    widths = []
    original = diffview._hunk_text

    def counted(hunk, width=0):
        widths.append(width)
        return original(hunk, width)

    monkeypatch.setattr(diffview, "_hunk_text", counted)
    monkeypatch.setattr(view._body, "update", lambda _text: None)
    view.paint(80)
    view.paint(80)
    view.paint(81)
    assert widths == [80, 81]


@pytest.mark.asyncio
async def test_identical_diff_result_does_not_rebuild(monkeypatch):
    pane = diffview.DiffPane("session", "/tmp")
    pane._painted = True
    previous = diff_mod.DiffResult(status=diff_mod.STATUS_OK, base="main")
    pane.result = previous
    calls = []

    async def compute(_fn, _cwd):
        return previous

    async def repaint(_self):
        calls.append(True)

    # to_thread is the only asynchronous boundary in refresh_diff.
    monkeypatch.setattr(diffview.asyncio, "to_thread", compute)
    monkeypatch.setattr(diffview.DiffPane, "_repaint", repaint)
    await pane.refresh_diff()
    assert calls == []

    changed = diff_mod.DiffResult(status=diff_mod.STATUS_OK, base="other")

    async def changed_compute(_fn, _cwd):
        return changed

    monkeypatch.setattr(diffview.asyncio, "to_thread", changed_compute)
    await pane.refresh_diff()
    assert calls == [True]
    assert pane.result == changed


@pytest.mark.asyncio
async def test_diff_refresh_burst_has_one_active_compute_and_one_followup(monkeypatch):
    pane = diffview.DiffPane("session", "/tmp")
    monkeypatch.setattr(diffview.DiffPane, "is_mounted", property(lambda _self: True))
    started = threading.Event()
    release = threading.Event()
    guard = threading.Lock()
    calls = 0
    active = 0
    peak = 0
    painted = []
    workers = []

    def compute(_cwd):
        nonlocal calls, active, peak
        with guard:
            calls += 1
            number = calls
            active += 1
            peak = max(peak, active)
        if number == 1:
            started.set()
            release.wait(timeout=5)
        with guard:
            active -= 1
        return diff_mod.DiffResult(base=f"version-{number}")

    async def repaint(self):
        painted.append(self.result.base)

    def run_worker(coro, **_kwargs):
        workers.append(asyncio.create_task(coro))

    monkeypatch.setattr(diffview.diff_mod, "compute", compute)
    monkeypatch.setattr(diffview.DiffPane, "_repaint", repaint)
    monkeypatch.setattr(pane, "run_worker", run_worker)
    pane.schedule_refresh()
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=2)
        for _ in range(20):
            pane.schedule_refresh()
        release.set()
        await asyncio.wait_for(asyncio.gather(*workers), timeout=5)
    finally:
        release.set()
    assert calls == 2
    assert peak == 1
    assert painted == ["version-2"]


def test_status_tooltip_parses_markup_once_until_it_changes(monkeypatch):
    class Pane:
        pass

    bar = statusline.StatusBar(Pane())
    bar.update("[@click=open_model_picker]model[/]  ·  branch")
    bar.set_chip_hints([("model", "model hint")])
    original = statusline.Content.from_markup
    calls = []

    def counted(markup):
        calls.append(markup)
        return original(markup)

    monkeypatch.setattr(statusline.Content, "from_markup", counted)
    assert bar._tooltip_for_x(2) == "model hint"
    assert bar._tooltip_for_x(3) == "model hint"
    assert len(calls) == 1
    bar.update("[@click=open_model_picker]other[/]  ·  branch")
    bar.set_chip_hints([("other", "other hint")])
    before = len(calls)
    assert bar._tooltip_for_x(2) == "other hint"
    assert len(calls) == before + 1
    assert bar._tooltip_for_x(3) == "other hint"
    assert len(calls) == before + 1


def test_turn_prompt_full_is_written_once_across_resizes(monkeypatch):
    block = transcript.TurnBlock("many words " * 30)
    updates = []
    original = block.prompt_full.update

    def counted(value):
        updates.append(value)
        return original(value)

    monkeypatch.setattr(block.prompt_full, "update", counted)
    block._render_title()
    block._render_title()
    # Construction already installed the full prompt; resize must not
    # write that same content again.
    assert updates == []


def test_prompt_keystrokes_do_not_invalidate_unchanged_height():
    prompt = PromptInput(None, None, None, None)
    before = prompt.styles._updates
    prompt._resize_to_content()
    assert prompt.styles._updates == before
