# SPDX-License-Identifier: AGPL-3.0-only
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
    TAB_ISOLATION_MARKER,
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
    # The repo slot is the MAIN checkout's name, not this worktree
    # directory's -- a session cwd IS a linked worktree since v0.17, and
    # `Path(repo_root).name` there would print the session id twice
    # alongside the branch chip beside it (item S's regression fix).
    assert line.repo == "myrepo"
    # "elsewhere" differs from both the branch and the (correct) repo
    # slot, so it carries real information and IS appended.
    assert line.branch_label() == "feature@elsewhere"

    # A worktree directory whose name repeats the BRANCH has nothing to
    # add...
    line.worktree = "feature"
    assert line.branch_label() == "feature"
    # ...and neither does one that repeats the REPO slot.
    line.worktree = "myrepo"
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


def _raw_tab_label(app, pane) -> str:
    """The tab header exactly as painted -- provider glyph and all."""
    from textual.widgets import TabbedContent

    return app.query_one("#session-tabs", TabbedContent).get_tab(pane.tab_id).label.plain


def _tab_label(app, pane) -> str:
    """The tab header's IDENTITY half -- the provider glyph stripped off.

    Every painted label carries the glyph (see test_the_provider_glyph_*
    below), so stripping it here once keeps the rest of this file reading
    exactly like it did before the glyph shipped: these assertions are
    about what the label SAYS (model, repo, branch, or a pinned name), a
    question the glyph -- constant across every tab -- has no bearing on."""
    from doxa.app import PROVIDER_GLYPHS

    prefix = PROVIDER_GLYPHS["claude"] + " "
    raw = _raw_tab_label(app, pane)
    return raw[len(prefix):] if raw.startswith(prefix) else raw


def _painted(app, pane) -> bool:
    """Has this pane's own label reached the TAB HEADER?

    Not the same question as "has the pane computed one", and the
    difference is this file's most frequent flake. `pane._tab_label` is
    the pane's IDENTITY string, set by the first `_refresh_status` after
    boot; every assertion in this file is about what `_raw_tab_label`
    PAINTS. Those are two moments, not one: `SessionPane.set_tab_label`
    writes the header inside `contextlib.suppress`, so a label computed
    before the tab's `Tab` widget is up records the identity and paints
    nothing. Waiting for the first while asserting on the second is how
    this file came to fail with the BIRTH label (`_tab_title`'s
    `model · dirname`) on a machine one message-pump turn slower than
    usual."""
    label = pane._tab_label
    if not label:
        return False
    try:
        return label in _raw_tab_label(app, pane)
    except Exception:  # noqa: BLE001 -- no tab yet is "not painted"
        return False


async def _settled(pilot, app, pane, tries=200):
    for _ in range(tries):
        if _painted(app, pane):
            return True
        await pilot.pause(0.02)
    return _painted(app, pane)


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


# -- item S: the tab label inside a worktree-per-session session ----------
#
# v0.17's worktree-per-session made every session cwd a linked worktree
# whose OWN checked-out branch is `doxa/<shortid>` -- the session's own
# throwaway handle, not the branch it forked from. The tab label used to
# show that session branch (a regression, reported: the session id ends up
# printed twice, once as the tab's branch half and again as the daemon's
# own handle elsewhere). Fixed: the tab shows the BASE (GitLine.tab_branch,
# via the worktree sidecar's base_ref); the status bar kept the session
# branch + sha, because that IS session identity.
#
# v0.28.0 OVERRIDES that second half, on the operator's own report ("when i
# chose a branch and click on one, it is not changed"). The status bar's
# branch segment is a SELECTOR now, and what it selects is the base: a
# successful switch rebases doxa/<id> onto the new base and rewrites
# base_ref, leaving the checked-out branch name untouched -- so a chip
# rendering the checked-out branch stayed byte-identical across a switch
# that had fully landed. The chip shows the base now (the same string the
# tab shows, which is what GitLine.render's docstring always claimed), and
# the checked-out branch moves into that segment's tooltip.


@pytest.mark.asyncio
async def test_tab_label_inside_a_worktree_session_shows_the_base_not_the_session_branch(
    monkeypatch, tmp_path,
):
    from doxa import worktrees as worktrees_mod

    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    from doxa import config as config_mod
    config_mod.invalidate()
    repo = _repo(tmp_path)
    worktree = worktrees_mod.create(str(repo), "brlabel1")
    assert worktree is not None
    app, _engines = await _app(monkeypatch, worktree)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, app, pane)
        # The base (trunk), not the session's own branch (doxa/brlabel1).
        assert pane.auto_label() == f"Sonnet@myrepo:trunk{TAB_ISOLATION_MARKER}"
        assert _tab_label(app, pane) == f"Sonnet@myrepo:trunk{TAB_ISOLATION_MARKER}"
        assert "brlabel1" not in _tab_label(app, pane)
    config_mod.invalidate()


@pytest.mark.asyncio
async def test_status_bar_shows_the_base_inside_a_worktree_session(
    monkeypatch, tmp_path,
):
    """v0.28.0's override of "the status bar keeps the session branch":
    the branch segment is the thing the branch picker CHANGES, so it has
    to be the base, or a landed switch is invisible (defect 3). The
    session's own branch is not lost -- it moves to the tooltip."""
    from doxa import worktrees as worktrees_mod

    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    from doxa import config as config_mod
    config_mod.invalidate()
    repo = _repo(tmp_path)
    worktree = worktrees_mod.create(str(repo), "brlabel2")
    assert worktree is not None
    app, _engines = await _app(monkeypatch, worktree)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, app, pane)
        # The TAB shows the base...
        assert _tab_label(app, pane) == f"Sonnet@myrepo:trunk{TAB_ISOLATION_MARKER}"
        # ...and so does the STATUS BAR now: one string, one meaning.
        status = str(pane.query_one("#status-bar").renderable)
        assert "myrepo[/][/] ⎇ " in status
        assert "trunk" in status
        assert "doxa/brlabel2" not in status
        # The checked-out branch is still reachable -- in the tooltip that
        # segment carries.
        hints = dict(pane._git.chip_hints())
        assert "doxa/brlabel2" in hints["trunk"]
    config_mod.invalidate()


@pytest.mark.asyncio
async def test_tab_label_follows_a_base_switch_without_rebuilding_gitline(
    monkeypatch, tmp_path,
):
    """GitLine is never reconstructed (item S #5's "verify the path
    updates" instruction): base_branch() re-reads the sidecar mtime-guarded,
    same discipline as the branch/sha fields, so a switch is visible on the
    next event-driven refresh -- here simulated the same way
    test_the_label_follows_a_branch_switch_without_polling defeats the
    HEAD mtime guard."""
    from doxa import worktrees as worktrees_mod

    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    from doxa import config as config_mod
    config_mod.invalidate()
    repo = _repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "branch", "develop"], check=True)
    worktree = worktrees_mod.create(str(repo), "brlabel3")
    assert worktree is not None
    app, _engines = await _app(monkeypatch, worktree)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, app, pane)
        assert pane.auto_label() == f"Sonnet@myrepo:trunk{TAB_ISOLATION_MARKER}"

        result = worktrees_mod.switch_base(worktree, "develop")
        assert result["ok"] is True
        pane._git._base_mtime = None  # defeat same-second mtime granularity
        pane._refresh_status()
        await pilot.pause()
        assert pane.auto_label() == f"Sonnet@myrepo:develop{TAB_ISOLATION_MARKER}"
        assert _tab_label(app, pane) == f"Sonnet@myrepo:develop{TAB_ISOLATION_MARKER}"
    config_mod.invalidate()


@pytest.mark.asyncio
async def test_tab_label_toggle_off_is_unchanged(monkeypatch, tmp_path):
    """worktree_per_session OFF: no sidecar at all, so tab_branch() falls
    back to branch_label() -- the checked-out branch IS the base, exactly
    as every tab label read before this feature existed."""
    monkeypatch.setenv("DOXA_WORKTREE", "0")
    repo = _repo(tmp_path)
    app, _engines = await _app(monkeypatch, repo)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, app, pane)
        assert pane.auto_label() == "Sonnet@myrepo:trunk"


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
        tab = app.query_one("#session-tabs").get_tab(pane.tab_id)

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
        assert app.query_one("#session-tabs").get_tab(pane.tab_id).display is True

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


def test_the_worst_case_label_plus_glyph_still_fits_the_original_budget():
    """TAB_LABEL_MAX was cut from 34 to 32 for exactly one reason: the
    glyph (1 cell) plus its separating space (1 cell) painted ahead of it
    must not blow past the width the tab bar was originally sized for.
    Pin that relationship down as a test, not just a comment, so a future
    edit to either constant has to notice the other."""
    from doxa.app import PROVIDER_GLYPHS, compose_tab_label

    glyph_width = len(PROVIDER_GLYPHS["claude"]) + 1  # glyph + separating space
    ORIGINAL_BUDGET = 34
    assert glyph_width + TAB_LABEL_MAX == ORIGINAL_BUDGET

    worst_case = compose_tab_label(
        "Sonnet", "repo", "a-branch-name-so-long-it-alone-overflows"
    )
    assert glyph_width + len(worst_case) <= ORIGINAL_BUDGET


# -- item S: the worktree-isolation marker ---------------------------------


def test_isolation_marker_appends_when_it_fits():
    from doxa.app import compose_tab_label

    label = compose_tab_label("Opus", "doxa", "main", isolated=True)
    assert label == f"Opus@doxa:main{TAB_ISOLATION_MARKER}"
    assert len(label) <= TAB_LABEL_MAX
    # And plainly absent when the session isn't isolated at all.
    assert compose_tab_label("Opus", "doxa", "main") == "Opus@doxa:main"


def test_isolation_marker_is_dropped_rather_than_shrinking_the_branch():
    """The base branch never gives up a character to make room for the
    marker -- a label already sitting at TAB_LABEL_MAX goes without it."""
    from doxa.app import compose_tab_label

    plain = compose_tab_label(
        "Sonnet", "repo", "a-branch-name-so-long-it-alone-overflows"
    )
    marked = compose_tab_label(
        "Sonnet", "repo", "a-branch-name-so-long-it-alone-overflows",
        isolated=True,
    )
    assert plain == marked  # no room: marker silently dropped
    assert len(marked) <= TAB_LABEL_MAX


def test_isolation_marker_still_fits_the_glyph_plus_limit_budget():
    from doxa.app import PROVIDER_GLYPHS, compose_tab_label

    glyph_width = len(PROVIDER_GLYPHS["claude"]) + 1
    ORIGINAL_BUDGET = 34
    worst_case = compose_tab_label(
        "Sonnet", "repo", "a-branch-name-so-long-it-alone-overflows",
        isolated=True,
    )
    assert glyph_width + len(worst_case) <= ORIGINAL_BUDGET


# -- the provider glyph -----------------------------------------------------


def test_provider_glyphs_table_has_exactly_the_one_live_row():
    """Multi-provider engines are planned, not shipped -- every model DOXA
    drives today is Claude/Anthropic's, so the table has one row, and the
    default lookup resolves to it without the caller naming a provider."""
    from doxa.app import PROVIDER_GLYPHS, provider_glyph

    assert PROVIDER_GLYPHS == {"claude": "✳"}
    assert provider_glyph() == provider_glyph("claude")


def test_provider_glyph_is_anthropic_orange_via_markup():
    """Confirmed empirically (not assumed): Textual 5's Tab renders its
    label through Content.from_markup by default, so a color tag here is
    real color -- not a literal bracket painted into the tab bar. Building
    an actual Tab from the glyph and inspecting its style spans is the
    check that would have caught it if that were NOT true."""
    from textual.widgets._tabs import Tab

    from doxa.app import PROVIDER_GLYPH_COLOR, provider_glyph

    markup = provider_glyph()
    assert markup == f"[{PROVIDER_GLYPH_COLOR}]✳[/]"

    tab = Tab(markup)
    assert tab.label.plain == "✳"                     # no stray brackets
    assert any(
        span.style == PROVIDER_GLYPH_COLOR for span in tab.label.spans
    )


def test_provider_glyph_uncolored_is_a_plain_fallback():
    """The escape hatch this feature would need if a future Textual (or a
    non-ContentTab label site) stopped rendering markup: colored=False
    hands back the bare glyph, no brackets at all."""
    from doxa.app import provider_glyph

    assert provider_glyph(colored=False) == "✳"


def test_an_unknown_provider_degrades_to_no_glyph():
    from doxa.app import provider_glyph

    assert provider_glyph("some-future-vendor") == ""


@pytest.mark.asyncio
async def test_the_glyph_prepends_every_painted_tab_label(monkeypatch, tmp_path):
    """The RAW tab header (not the identity string _tab_label helps read)
    always starts with the glyph -- for an auto label AND for a pinned
    (user-renamed) one, because provider identity is orthogonal to the
    user's name for the tab."""
    repo = _repo(tmp_path)
    app, _engines = await _app(monkeypatch, repo)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, app, pane)
        assert _raw_tab_label(app, pane) == "✳ Sonnet@myrepo:trunk"

        pane.set_custom_name("pinned work")
        await pilot.pause()
        assert _raw_tab_label(app, pane) == "✳ pinned work"
        # ...while the rename field seeds from the plain identity, glyph
        # stripped -- renaming "✳ pinned work" back to itself must not
        # hand the glyph back as part of the name.
        assert pane.display_name() == "pinned work"


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
