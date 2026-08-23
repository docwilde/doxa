"""Tab labels: `Model@repo:branch`.

A tab has room for the two things that differ between tabs -- which model
is answering and where it is working -- and for nothing else. What is
pinned here: the short-model rule, the git half (including the linked
worktree case), the no-repo fallback, that a live /model switch moves the
label, and that the label is derived from the SAME event-driven GitLine
the status bar reads rather than from any polling of its own.
"""

from __future__ import annotations

import subprocess

import pytest

from doxa.app import (
    DoxaApp,
    GitLine,
    TAB_LABEL_MAX,
    ellipsize,
    short_model,
)
from tests.fakes import FakeEngine


def _repo(tmp_path, branch="trunk", name="myrepo"):
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", branch, str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "f.txt").write_text("one", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "one"], check=True)
    return repo


# -- the short-model rule -------------------------------------------------


def test_short_model_is_the_tier_word():
    assert short_model("claude-sonnet-4-5") == "Sonnet"
    assert short_model("claude-3-5-haiku-20241022") == "Haiku"
    assert short_model("claude-opus-4-1") == "Opus"
    assert short_model("fable") == "Fable"
    # Unknown vendors keep their first dash-segment rather than a truncated
    # word, and an unset model is the same "default" the status bar says.
    assert short_model("deepseek-chat") == "deepseek"
    assert short_model("") == "default"
    assert short_model(None) == "default"


def test_labels_are_ellipsis_truncated():
    long = "x" * (TAB_LABEL_MAX + 20)
    out = ellipsize(long)
    assert len(out) == TAB_LABEL_MAX
    assert out.endswith("…")
    assert ellipsize("short") == "short"


# -- the git half ---------------------------------------------------------


def test_branch_label_is_the_plain_branch_in_a_normal_checkout(tmp_path):
    repo = _repo(tmp_path)
    line = GitLine(str(repo))
    assert line.worktree is None
    assert line.branch_label() == "trunk"


def test_branch_label_names_a_linked_worktree(tmp_path):
    """`main@featureX` -- a worktree is a different place with the same
    repo name, and the branch alone does not say which one you are in."""
    repo = _repo(tmp_path)
    work = tmp_path / "elsewhere"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "feature",
         str(work)],
        check=True,
    )
    line = GitLine(str(work))
    assert line.worktree == "elsewhere"     # git's own name for it
    assert line.repo == "elsewhere"
    # The worktree name here repeats the repo slot, so it is not appended:
    # `elsewhere ⎇ feature@elsewhere` says one fact three times.
    assert line.branch_label() == "feature"

    # A worktree directory whose name differs from both DOES get the
    # suffix -- that is the case where it carries information.
    line.worktree = "spike"
    assert line.branch_label() == "feature@spike"
    line.worktree = "feature"  # same as the branch: nothing to add
    assert line.branch_label() == "feature"


# -- the label on a live tab ----------------------------------------------


async def _app(monkeypatch, cwd, model="claude-sonnet-4-5"):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(cwd) + "-rt")
    engines: list[FakeEngine] = []

    def make():
        engines.append(FakeEngine([], model=model))
        return engines[-1]

    app = DoxaApp(cwd=str(cwd), engine_factory=make, new_session_factory=make)
    return app, engines


def _tab_label(app, pane) -> str:
    from textual.widgets import TabbedContent

    return app.query_one("#session-tabs", TabbedContent).get_tab(pane.id).label.plain


async def _settled(pilot, app, pane, tries=200):
    for _ in range(tries):
        if pane._tab_label:
            return True
        await pilot.pause(0.02)
    return bool(pane._tab_label)


@pytest.mark.asyncio
async def test_tab_label_is_model_repo_branch(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    app, _engines = await _app(monkeypatch, repo)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, app, pane)
        assert pane.auto_label() == "Sonnet@myrepo:trunk"
        assert _tab_label(app, pane) == "Sonnet@myrepo:trunk"


@pytest.mark.asyncio
async def test_tab_label_outside_a_repo_is_model_and_dirname(
    monkeypatch, tmp_path
):
    where = tmp_path / "loose-files"
    where.mkdir()
    app, _engines = await _app(monkeypatch, where, model="claude-haiku-4-5")
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, app, pane)
        assert pane.auto_label() == "Haiku@loose-files"
        assert ":" not in _tab_label(app, pane)  # never a dangling colon


@pytest.mark.asyncio
async def test_a_model_switch_moves_the_label(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    app, _engines = await _app(monkeypatch, repo)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, app, pane)
        assert _tab_label(app, pane).startswith("Sonnet")

        await pane._cmd_model("haiku")
        await pilot.pause()
        assert _tab_label(app, pane) == "Haiku@myrepo:trunk"


@pytest.mark.asyncio
async def test_the_label_follows_a_branch_switch_without_polling(
    monkeypatch, tmp_path
):
    """Same discipline as the git chip: the branch is re-read when HEAD's
    mtime moves, on the next event-driven status refresh -- there is no
    timer anywhere in this path."""
    repo = _repo(tmp_path)
    app, _engines = await _app(monkeypatch, repo)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, app, pane)
        assert pane.auto_label().endswith(":trunk")

        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "side"],
                       check=True)
        pane._git._mtime = None  # defeat same-second mtime granularity
        pane._refresh_status()   # what a finished turn or a peer event calls
        await pilot.pause()
        assert pane.auto_label() == "Sonnet@myrepo:side"
        assert _tab_label(app, pane) == "Sonnet@myrepo:side"


@pytest.mark.asyncio
async def test_each_tab_carries_its_own_label(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    app, _engines = await _app(monkeypatch, repo)
    async with app.run_test() as pilot:
        first = app.active_pane
        assert await _settled(pilot, app, first)

        await pilot.press("ctrl+t")
        for _ in range(200):
            if len(app.panes()) == 2:
                break
            await pilot.pause(0.02)
        second = app.panes()[1]
        assert await _settled(pilot, app, second)

        await second._cmd_model("opus")
        await pilot.pause()
        assert _tab_label(app, first) == "Sonnet@myrepo:trunk"
        assert _tab_label(app, second) == "Opus@myrepo:trunk"


# -- renaming a tab -------------------------------------------------------


async def _rename_editor(app, pilot, tries=100):
    from doxa.app import TabRename

    for _ in range(tries):
        editors = list(app.query(TabRename))
        if editors:
            return editors[0]
        await pilot.pause(0.02)
    return None


@pytest.mark.asyncio
async def test_double_click_opens_an_inline_field(monkeypatch, tmp_path):
    from textual.widgets import Tab

    repo = _repo(tmp_path)
    app, _engines = await _app(monkeypatch, repo)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, app, pane)
        tab = app.query_one("#session-tabs").get_tab(pane.id)

        await pilot.click(Tab)  # one click is still just "switch to me"
        await pilot.pause()
        assert not list(app.query("#tab-rename"))

        await pilot.click(Tab, times=2)
        editor = await _rename_editor(app, pilot)
        assert editor is not None
        assert editor.value == "Sonnet@myrepo:trunk"  # seeded with the label
        assert tab.display is False  # the field sits in the tab's own slot
        assert app.focused is editor


@pytest.mark.asyncio
async def test_enter_commits_and_pins_the_name(monkeypatch, tmp_path):
    from textual.widgets import Tab

    repo = _repo(tmp_path)
    app, _engines = await _app(monkeypatch, repo)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, app, pane)

        await pilot.click(Tab, times=2)
        editor = await _rename_editor(app, pilot)
        editor.value = "graph importer"
        await pilot.press("enter")
        await pilot.pause()

        assert pane.custom_name == "graph importer"
        assert _tab_label(app, pane) == "graph importer"
        assert not list(app.query("#tab-rename"))
        assert app.query_one("#session-tabs").get_tab(pane.id).display is True

        # PINNED: neither a model switch nor a branch change rewrites it.
        await pane._cmd_model("haiku")
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "other"],
                       check=True)
        pane._git._mtime = None
        pane._refresh_status()
        await pilot.pause()
        assert _tab_label(app, pane) == "graph importer"
        # ...while the STATUS bar still tracks both, as it always did.
        status = str(pane.query_one("#status-bar").renderable)
        assert "other" in status and "haiku" in status


@pytest.mark.asyncio
async def test_escape_cancels_the_rename(monkeypatch, tmp_path):
    from textual.widgets import Tab

    repo = _repo(tmp_path)
    app, _engines = await _app(monkeypatch, repo)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, app, pane)
        before = _tab_label(app, pane)

        await pilot.click(Tab, times=2)
        editor = await _rename_editor(app, pilot)
        editor.value = "discarded"
        await pilot.press("escape")
        await pilot.pause()

        assert pane.custom_name is None
        assert _tab_label(app, pane) == before
        assert not list(app.query("#tab-rename"))


@pytest.mark.asyncio
async def test_an_emptied_name_returns_the_automatic_label(monkeypatch, tmp_path):
    from textual.widgets import Tab

    repo = _repo(tmp_path)
    app, _engines = await _app(monkeypatch, repo)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, app, pane)
        pane.set_custom_name("pinned name")
        assert _tab_label(app, pane) == "pinned name"

        await pilot.click(Tab, times=2)
        editor = await _rename_editor(app, pilot)
        editor.value = "   "
        await pilot.press("enter")
        await pilot.pause()

        assert pane.custom_name is None
        assert _tab_label(app, pane) == "Sonnet@myrepo:trunk"
        # ...and it tracks again, because un-pinning is what emptying means.
        await pane._cmd_model("opus")
        await pilot.pause()
        assert _tab_label(app, pane) == "Opus@myrepo:trunk"


@pytest.mark.asyncio
async def test_rename_command_is_the_keyboard_door(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    app, _engines = await _app(monkeypatch, repo)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, app, pane)

        await pane._cmd_rename("importer work")
        await pilot.pause()
        assert _tab_label(app, pane) == "importer work"

        await pane._cmd_rename("")
        await pilot.pause()
        assert pane.custom_name is None
        assert _tab_label(app, pane) == "Sonnet@myrepo:trunk"


# -- truncation order ------------------------------------------------------


def test_truncation_sacrifices_model_first_then_repo_and_protects_branch():
    """Across open tabs the model is usually identical and the branch is
    usually what differs -- so the branch is the last thing to give."""
    from doxa.app import TAB_MODEL_MIN, TAB_REPO_MIN, compose_tab_label

    label = compose_tab_label("Sonnet", "reasonably-long-repo", "kg-stats")
    assert len(label) <= TAB_LABEL_MAX
    model, rest = label.split("@", 1)
    repo, branch = rest.split(":", 1)
    assert branch == "kg-stats"            # untouched
    assert model != "Sonnet" and model.endswith("…")   # trimmed first
    assert len(model) >= TAB_MODEL_MIN

    # Longer still: the repo starts giving ground too, the branch still not.
    label = compose_tab_label("Sonnet", "an-extremely-long-repository-name",
                              "kg-stats")
    model, rest = label.split("@", 1)
    repo, branch = rest.split(":", 1)
    assert branch == "kg-stats"
    assert len(model) == TAB_MODEL_MIN
    assert repo.endswith("…") and len(repo) >= TAB_REPO_MIN

    # Only when both are at their floors does the whole label get cut.
    label = compose_tab_label("Sonnet", "repo", "a-branch-name-so-long-it-alone-overflows")
    assert len(label) == TAB_LABEL_MAX and label.endswith("…")

    # And a short label is left exactly alone.
    assert compose_tab_label("Opus", "doxa", "main") == "Opus@doxa:main"
    assert compose_tab_label("Opus", "notes") == "Opus@notes"


# -- the Haiku namer, outside a repo --------------------------------------


def test_the_sanitizer_is_the_guarantee_not_the_prompt():
    from doxa.naming import NAME_MAX, sanitize

    assert sanitize("Graph importer review") == "Graph importer review"
    # Control characters, newlines and markup are stripped, not trusted.
    assert sanitize("evil\x1b[31m\nname\x00here") == "evil 31m name here"
    assert sanitize('"Quoted: title!"') == "Quoted title"
    long = sanitize("word " * 40)
    assert len(long) <= NAME_MAX


@pytest.mark.asyncio
async def test_out_of_repo_tab_is_named_from_the_first_turn(
    monkeypatch, tmp_path
):
    from doxa.engine import EngineEvent
    from doxa import naming as naming_mod

    where = tmp_path / "scratch-dir"
    where.mkdir()
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "home"))
    calls: list[str] = []

    def fake_generate(first_message, model=naming_mod.NAMER_MODEL):
        calls.append(first_message)
        return "Flux capacitor rewire"

    monkeypatch.setattr(naming_mod, "generate_name", fake_generate)

    script = [
        EngineEvent("turn_started", {}),
        EngineEvent("text_delta", {"text": "ok"}),
        EngineEvent("turn_done", {"cost_usd": 0.0, "duration_ms": 1,
                                  "is_error": False}),
    ]
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    engine = FakeEngine(script, model="claude-opus-4-1")
    engine.session_id = "abc12345-0000"
    app = DoxaApp(cwd=str(where), engine_factory=lambda: engine,
                  new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, app, pane)
        # Provisional: the dirname, never blank and never spinning.
        assert _tab_label(app, pane) == "Opus@scratch-dir"

        app.query_one("#prompt-input").value = "help me rewire the flux capacitor"
        await pilot.press("enter")
        for _ in range(200):
            if pane.generated_name:
                break
            await pilot.pause(0.02)
        assert pane.generated_name == "Flux capacitor rewire"
        assert _tab_label(app, pane) == "Opus@Flux capacitor rewire"
        assert calls == ["help me rewire the flux capacitor"]

        # Cached with the session: a restore reuses it rather than paying
        # for a second call.
        assert naming_mod.cached_name("abc12345-0000") == "Flux capacitor rewire"

        # Once, ever -- a second turn does not re-name.
        app.query_one("#prompt-input").value = "and now something else"
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_failing_namer_keeps_the_dirname_and_does_not_retry(
    monkeypatch, tmp_path
):
    from doxa.engine import EngineEvent
    from doxa import naming as naming_mod

    where = tmp_path / "unnamed-dir"
    where.mkdir()
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    calls: list[str] = []

    def failing(first_message, model=naming_mod.NAMER_MODEL):
        calls.append(first_message)
        return None  # offline, timed out, non-zero exit -- all the same

    monkeypatch.setattr(naming_mod, "generate_name", failing)
    script = [
        EngineEvent("turn_started", {}),
        EngineEvent("turn_done", {"cost_usd": 0.0, "duration_ms": 1,
                                  "is_error": False}),
    ]
    engine = FakeEngine(script, model="claude-haiku-4-5")
    app = DoxaApp(cwd=str(where), engine_factory=lambda: engine,
                  new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, app, pane)
        for turn in ("first", "second"):
            app.query_one("#prompt-input").value = turn
            await pilot.press("enter")
            await pilot.pause(0.2)
        assert pane.generated_name is None
        assert _tab_label(app, pane) == "Haiku@unnamed-dir"
        assert len(calls) == 1  # no retry loop


@pytest.mark.asyncio
async def test_a_repo_tab_is_never_named_by_the_model(monkeypatch, tmp_path):
    from doxa.engine import EngineEvent
    from doxa import naming as naming_mod

    repo = _repo(tmp_path)
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "home"))
    calls: list[str] = []
    monkeypatch.setattr(
        naming_mod, "generate_name",
        lambda first_message, model=None: calls.append(first_message) or "nope",
    )
    script = [
        EngineEvent("turn_started", {}),
        EngineEvent("turn_done", {"cost_usd": 0.0, "duration_ms": 1,
                                  "is_error": False}),
    ]
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    engine = FakeEngine(script, model="claude-sonnet-4-5")
    app = DoxaApp(cwd=str(repo), engine_factory=lambda: engine,
                  new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, app, pane)
        app.query_one("#prompt-input").value = "anything"
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert calls == []
        assert _tab_label(app, pane) == "Sonnet@myrepo:trunk"


@pytest.mark.asyncio
async def test_a_restored_session_reuses_its_cached_name(monkeypatch, tmp_path):
    from doxa import naming as naming_mod

    where = tmp_path / "some-dir"
    where.mkdir()
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    naming_mod.remember_name("kept-session", "Earlier work here")
    calls: list[str] = []
    monkeypatch.setattr(
        naming_mod, "generate_name",
        lambda first_message, model=None: calls.append(first_message) or "new",
    )
    engine = FakeEngine([], model="claude-opus-4-1")
    engine.session_id = "kept-session"
    app = DoxaApp(cwd=str(where), engine_factory=lambda: engine,
                  new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        pane = app.active_pane
        for _ in range(200):
            if pane.generated_name:
                break
            await pilot.pause(0.02)
        assert pane.generated_name == "Earlier work here"
        assert _tab_label(app, pane) == "Opus@Earlier work here"
        assert calls == []  # no call was spent


@pytest.mark.asyncio
async def test_a_renamed_tab_is_never_overwritten_by_the_namer(
    monkeypatch, tmp_path
):
    from doxa.engine import EngineEvent
    from doxa import naming as naming_mod

    where = tmp_path / "pinned-dir"
    where.mkdir()
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    calls: list[str] = []
    monkeypatch.setattr(
        naming_mod, "generate_name",
        lambda first_message, model=None: calls.append(first_message) or "generated",
    )
    script = [
        EngineEvent("turn_started", {}),
        EngineEvent("turn_done", {"cost_usd": 0.0, "duration_ms": 1,
                                  "is_error": False}),
    ]
    engine = FakeEngine(script)
    app = DoxaApp(cwd=str(where), engine_factory=lambda: engine,
                  new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, app, pane)
        pane.set_custom_name("my own name")
        app.query_one("#prompt-input").value = "anything"
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert calls == []
        assert _tab_label(app, pane) == "my own name"
