# SPDX-License-Identifier: AGPL-3.0-only
"""The diff chip, the auto-open setting, and the diff's own colours
(v1.0.1).

Three concerns, one release, and they share a fixture because they share
a subject: the live diff (v0.92.0) opened on ``F2``/``/diff`` and
``_tick_diff`` deliberately did nothing when no diff pane was open --
"costs nothing when nobody is looking". The measured cost of that thrift
was that you could not tell there were changes without opening it.

What each section pins:

* **the chip** -- hidden at zero, the real counts when there are
  changes, a click that opens the pane, and (the part that is not a
  preference) each of the base states rendering DISTINCTLY. "cannot
  determine a base" must never be spelt the way "no changes" is spelt,
  which here means: never by being absent;
* **auto_diff** -- off by default, once per session when on, no re-open
  after a close, no focus theft, and a refusal rather than a sliver on a
  window too narrow to split;
* **the colours** -- asserted on the rendered ``Text``'s spans, never on
  a screenshot, including a hunk whose body contains ``[`` so the markup
  trap v0.28.0 paid for stays pinned.

Local ``_repo``/``_app`` helpers rather than imports from
tests/test_live_diff.py: a test module is not a library (see
tests/helpers.py's own docstring on what happened the last time one was
treated as one).
"""

from __future__ import annotations

import json
import subprocess

import pytest
from textual.content import Content

from doxa import config as config_mod
from doxa import diff as diff_mod
from doxa.app import DoxaApp
from doxa.ui.diffview import (
    DiffPane,
    FileSection,
    HunkView,
    _file_title,
    _hunk_text,
    _side_by_side_text,
)
from doxa.ui.labels import (
    DIFF_ADD_BG,
    DIFF_ADD_NUM,
    DIFF_CHIP_MIN_COLS,
    DIFF_DEL_BG,
    DIFF_DEL_NUM,
)
from doxa.ui.statusline import StatusBar
from tests.fakes import FakeEngine


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    config_mod.invalidate()
    yield
    config_mod.invalidate()


# -- a real repo, and a real app around it ------------------------------


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )


_BODY = [f"line{i}" for i in range(1, 31)]


def _repo(root, *, base_is_branch=False, dirty=True, sidecar=True):
    """A worktree on ``doxa/abc12345``, cut from ``main``, with two edits
    ten lines apart and one created file -- the same shape
    tests/test_live_diff.py builds, minus the binary blob (nothing here
    asserts on binary rendering).

    ``sidecar=False`` is the no-worktree-base case (``BASE_HEAD``);
    ``base_is_branch=True`` is v0.33.0's trap (``STATUS_NO_BASE``);
    ``dirty=False`` is a clean tree, which is what "hidden at zero"
    means."""
    # The sidecar is keyed on the worktree's DIRECTORY NAME
    # (worktrees.meta_file_path), and one test builds four repos under
    # one DOXA_HOME -- so the name has to be unique per repo or the
    # fourth reads the third's recorded base.
    work = root / f"work-{root.name}"
    work.mkdir(parents=True)
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@example.invalid")
    _git(work, "config", "user.name", "t")
    (work / "f.py").write_text("\n".join(_BODY) + "\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "base")
    _git(work, "checkout", "-qb", "doxa/abc12345")
    if dirty:
        edited = list(_BODY)
        edited[2] = "CHANGED_TOP"
        edited[24] = "CHANGED_BOTTOM"
        (work / "f.py").write_text("\n".join(edited) + "\n")
        (work / "new.txt").write_text("created by the agent\n")
    if sidecar:
        _sidecar(
            work,
            base_ref="doxa/abc12345" if base_is_branch else "main",
            branch="doxa/abc12345",
        )
    return work


def _sidecar(work, **fields):
    from doxa import worktrees as worktrees_mod

    meta = worktrees_mod.worktrees_root() / ".meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / f"{work.name}.json").write_text(
        json.dumps({"main_root": str(work), "session_id": "abc12345", **fields})
    )


def _app(cwd):
    engines: "list[FakeEngine]" = []

    def make() -> FakeEngine:
        engine = FakeEngine([], cwd=str(cwd))
        engine.session_id = "abc12345"
        engines.append(engine)
        return engine

    return DoxaApp(
        cwd=str(cwd), engine_factory=make, new_session_factory=make
    ), engines


async def _wait(pilot, cond, tries=300):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(0.02)
    return cond()


def _bar_markup(app) -> str:
    return str(app.query_one("#status-bar", StatusBar).renderable)


def _bar_plain(app) -> str:
    return Content.from_markup(_bar_markup(app)).plain


async def _settled(pilot, app):
    """Wait for the pane's FIRST diff-counts reading to have landed.

    The chip is painted from a cached record written by a worker
    (``PaneRuntimeMixin._refresh_diff_counts``), so "the bar has been
    refreshed" is not the same event as "the counts are in"."""
    pane = app.active_pane
    return await _wait(pilot, lambda: pane._diff_counts is not None)


def _tick(app, name="Edit"):
    """What a finished tool call does -- the production seam
    (``_render_tool_result`` calls exactly this)."""
    app.active_pane._tick_diff(name, {"file_path": "f.py"})


# -- the chip: hidden at zero, real counts when there are changes -------


@pytest.mark.asyncio
async def test_a_clean_worktree_paints_no_diff_chip(tmp_path):
    """Hide-at-zero, the house rule every chip on this row follows. A
    measured, empty diff earns no columns: `diff 0 files` would be a
    permanent reminder of nothing on the most contended row in the UI."""
    work = _repo(tmp_path, dirty=False)
    app, _ = _app(work)
    async with app.run_test(size=(160, 48)) as pilot:
        assert await _settled(pilot, app)
        counts = app.active_pane._diff_counts
        assert counts.status == diff_mod.STATUS_OK and counts.files == 0
        assert "diff " not in _bar_plain(app)
        assert "open_diff" not in _bar_markup(app)


@pytest.mark.asyncio
async def test_the_chip_shows_the_real_counts_not_a_placeholder(tmp_path):
    """The numbers on the bar are the numbers git reports, and the same
    ones the pane's own head line is built from -- asserted against
    ``compute()``'s independent full-diff path rather than against the
    chip's own arithmetic."""
    work = _repo(tmp_path)
    app, _ = _app(work)
    async with app.run_test(size=(160, 48)) as pilot:
        assert await _settled(pilot, app)
        assert await _wait(pilot, lambda: "diff " in _bar_plain(app))
        full = diff_mod.compute(str(work)).counts()
        assert full.files == 2 and full.added == 3 and full.removed == 2
        assert f"diff {full.files} files +{full.added} −{full.removed}" in (
            _bar_plain(app)
        )


@pytest.mark.asyncio
async def test_the_cheap_counts_agree_with_the_full_diff(tmp_path):
    """One repo, two paths to the same four numbers: ``--numstat`` (the
    chip) and the parsed unified diff (the pane). A chip that disagrees
    with the surface it summarises is worse than no chip."""
    work = _repo(tmp_path)
    assert diff_mod.counts(str(work)) == diff_mod.compute(str(work)).counts()


def test_the_chip_counts_past_the_caps_the_pane_stops_rendering_at(
    tmp_path, monkeypatch,
):
    """The one place the two paths are MEANT to differ. ``compute`` stops
    rendering at MAX_FILES and says how many it left out; a count has no
    page to end, and `diff 1 file` on a tree with three changed would be
    exactly the short-answer-as-whole-answer the caps exist to
    prevent."""
    work = _repo(tmp_path)
    (work / "second.py").write_text("one\n")
    monkeypatch.setattr(diff_mod, "MAX_FILES", 1)
    rendered = diff_mod.compute(str(work))
    assert len(rendered.files) == 1 and rendered.dropped_files == 2
    assert diff_mod.counts(str(work)).files == 3


@pytest.mark.asyncio
async def test_clicking_the_chip_opens_the_diff_pane(tmp_path):
    """The operator's own wording: clickable to open the diff pane, the
    same action F2 fires. Asserted through a real click at a real screen
    column, and against a PAINTED rectangle rather than a mounted
    widget (v0.28.0's lesson)."""
    work = _repo(tmp_path)
    app, _ = _app(work)
    async with app.run_test(size=(160, 48)) as pilot:
        assert await _settled(pilot, app)
        text = app.active_pane._diff_counts.chip()
        assert await _wait(pilot, lambda: text in _bar_plain(app))
        assert not list(app.query(DiffPane))
        # +2 for the bar's own `padding: 0 2`, the same offset
        # StatusBar._tooltip_for_x subtracts.
        await pilot.click("#status-bar", offset=(_bar_plain(app).index(text) + 2, 0))
        assert await _wait(
            pilot,
            lambda: bool(list(app.query(DiffPane)))
            and list(app.query(DiffPane))[0].region.width > 0,
        ), "the click did not open a painted diff pane"


# -- the four base states, each rendered distinctly ---------------------


def test_cannot_determine_a_base_is_never_rendered_as_no_changes(tmp_path):
    """v0.33.0's trap, inherited by the chip. ``base_ref == branch``
    makes the diff structurally unmeasurable, and the chip's ONLY
    hide-at-zero rendering is a MEASURED zero -- so this state paints a
    chip that says so, at every width, rather than the absence that
    means "nothing changed"."""
    work = _repo(tmp_path, base_is_branch=True)
    unmeasurable = diff_mod.counts(str(work))
    clean = diff_mod.counts(str(_repo(tmp_path / "other", dirty=False)))

    assert unmeasurable.status == diff_mod.STATUS_NO_BASE
    assert not unmeasurable.measurable
    assert unmeasurable.chip() == "diff ⚠ no base"
    assert unmeasurable.chip(short=True) == "diff ⚠ no base"

    assert clean.status == diff_mod.STATUS_OK
    assert clean.chip() is None  # the absence that means "no changes"
    assert unmeasurable.chip() != clean.chip()
    assert "no changes" not in unmeasurable.headline()
    assert "cannot determine a base" in unmeasurable.headline()
    assert "no changes" in clean.headline()


def test_each_of_the_four_base_states_renders_differently(tmp_path):
    """The closed set, on screen. Four states, four renderings, and no
    two of them the same string."""
    changed = diff_mod.counts(str(_repo(tmp_path / "a")))
    clean = diff_mod.counts(str(_repo(tmp_path / "b", dirty=False)))
    no_base = diff_mod.counts(str(_repo(tmp_path / "c", base_is_branch=True)))
    head = diff_mod.counts(str(_repo(tmp_path / "d", sidecar=False)))
    broken = diff_mod.DiffCounts(
        status=diff_mod.STATUS_ERROR, detail="fatal: bad revision"
    )

    assert changed.chip() == "diff 2 files +3 −2"
    assert clean.chip() is None
    assert no_base.chip() == "diff ⚠ no base"
    assert head.chip() == "diff 2 files +3 −2 vs HEAD"
    assert broken.chip() == "diff ⚠ unreadable"

    rendered = [c.chip() for c in (changed, clean, no_base, head, broken)]
    assert len(set(rendered)) == len(rendered), rendered
    # The HEAD case says which claim it is making, in the chip AND in the
    # tooltip -- "against the current commit" is a SMALLER claim than
    # "against this session's branch point".
    assert "no worktree base recorded" in head.chip_hint()
    assert "no worktree base recorded" not in changed.chip_hint()


def test_the_tooltip_is_the_same_sentence_the_pane_prints(tmp_path):
    """One wording for one state (``doxa.diff.headline``): the chip is a
    summary of the pane and the two must not be able to disagree about
    which state the tree is in."""
    work = _repo(tmp_path)
    assert diff_mod.compute(str(work)).headline() in diff_mod.counts(
        str(work)
    ).chip_hint()


def test_the_chip_shortens_on_a_narrow_bar_but_the_warnings_never_do(tmp_path):
    """The mode chip's width discipline, inherited: the noun goes below
    DIFF_CHIP_MIN_COLS, and the state that is the only place a fact
    appears keeps every column it needs."""
    changed = diff_mod.counts(str(_repo(tmp_path / "a")))
    assert changed.chip(short=False) == "diff 2 files +3 −2"
    assert changed.chip(short=True) == "diff 2f +3 −2"
    assert len(changed.chip(short=True)) < len(changed.chip(short=False))
    no_base = diff_mod.counts(str(_repo(tmp_path / "b", base_is_branch=True)))
    assert no_base.chip(short=True) == no_base.chip(short=False)


@pytest.mark.asyncio
async def test_a_narrow_window_paints_the_short_form(tmp_path):
    """The width gate, through the real bar rather than the helper."""
    work = _repo(tmp_path)
    app, _ = _app(work)
    async with app.run_test(size=(DIFF_CHIP_MIN_COLS - 10, 24)) as pilot:
        assert await _settled(pilot, app)
        assert await _wait(pilot, lambda: "diff 2f " in _bar_plain(app))
        assert "2 files" not in _bar_plain(app)


@pytest.mark.asyncio
async def test_the_chip_paints_added_green_and_removed_red(tmp_path):
    """One vocabulary across both surfaces: the same two colours the
    pane's line numbers and file folds use. And the chip's KEY stays the
    PLAIN text -- a key carrying colour markup matches nothing in the
    bar's stripped string and the tooltip silently vanishes (v0.35.0)."""
    work = _repo(tmp_path)
    app, _ = _app(work)
    async with app.run_test(size=(160, 48)) as pilot:
        assert await _settled(pilot, app)
        assert await _wait(pilot, lambda: "diff 2 files" in _bar_plain(app))
        markup = _bar_markup(app)
        assert f"[{DIFF_ADD_NUM}]+3[/]" in markup
        assert f"[{DIFF_DEL_NUM}] −2[/]" in markup
        bar = app.query_one("#status-bar", StatusBar)
        hints = dict(bar._chip_hints)
        assert "diff 2 files +3 −2" in hints
        assert "click to open the live diff" in hints["diff 2 files +3 −2"]


# -- auto_diff: off by default, once per session when on ----------------


@pytest.mark.asyncio
async def test_by_default_an_edit_opens_nothing(tmp_path):
    """The default IS the feature's main claim: an app nobody configured
    behaves exactly as v0.92.0 did."""
    work = _repo(tmp_path)
    app, _ = _app(work)
    async with app.run_test(size=(160, 48)) as pilot:
        assert await _settled(pilot, app)
        assert not diff_mod.auto_open_enabled()
        _tick(app)
        for _ in range(20):
            await pilot.pause(0.02)
        assert not list(app.query(DiffPane))
        # ...and the chip still updated, which is the whole point of the
        # tick no longer returning early when no pane is open.
        assert "diff " in _bar_plain(app)


@pytest.mark.asyncio
async def test_the_first_edit_opens_the_diff_and_the_second_does_not(
    tmp_path, monkeypatch,
):
    """Once per session. The second tick must find the allowance spent,
    not merely find a pane already open -- so the flag is asserted
    directly as well as the pane count."""
    monkeypatch.setenv("DOXA_AUTO_DIFF", "1")
    config_mod.invalidate()
    work = _repo(tmp_path)
    app, _ = _app(work)
    async with app.run_test(size=(160, 48)) as pilot:
        pane = app.active_pane
        assert await _settled(pilot, app)
        assert not pane._auto_diff_done
        _tick(app)
        assert await _wait(
            pilot,
            lambda: bool(list(app.query(DiffPane)))
            and list(app.query(DiffPane))[0].region.width > 0,
        ), "the first edit did not open a painted diff"
        assert pane._auto_diff_done
        _tick(app)
        for _ in range(20):
            await pilot.pause(0.02)
        assert len(list(app.query(DiffPane))) == 1


@pytest.mark.asyncio
async def test_closing_the_diff_does_not_make_it_come_back(
    tmp_path, monkeypatch,
):
    """The reason the flag lives on the SESSION pane and not on the diff
    pane: a user who closes the diff has closed it, and state living on
    the closed widget would come back False with the next tick and fight
    them."""
    monkeypatch.setenv("DOXA_AUTO_DIFF", "1")
    config_mod.invalidate()
    work = _repo(tmp_path)
    app, _ = _app(work)
    async with app.run_test(size=(160, 48)) as pilot:
        assert await _settled(pilot, app)
        _tick(app)
        assert await _wait(pilot, lambda: bool(list(app.query(DiffPane))))
        note = await app.toggle_diff_pane()
        assert note is None, note
        assert await _wait(pilot, lambda: not list(app.query(DiffPane)))
        _tick(app)
        for _ in range(20):
            await pilot.pause(0.02)
        assert not list(app.query(DiffPane)), "the diff re-opened behind the user"


@pytest.mark.asyncio
async def test_auto_open_never_takes_the_keyboard(tmp_path, monkeypatch):
    """v0.38.0 made focus explicit: a new surface mounts unfocused and
    whatever creates it says where the keyboard goes. For an open the
    user did not ask for, mid-sentence, that is load-bearing -- the
    prompt keeps focus and keeps receiving keys."""
    monkeypatch.setenv("DOXA_AUTO_DIFF", "1")
    config_mod.invalidate()
    work = _repo(tmp_path)
    app, _ = _app(work)
    async with app.run_test(size=(160, 48)) as pilot:
        pane = app.active_pane
        assert await _settled(pilot, app)
        prompt = pane.query_one("#prompt-input")
        prompt.focus()
        await pilot.pause()
        assert app.focused is prompt
        _tick(app)
        assert await _wait(pilot, lambda: bool(list(app.query(DiffPane))))
        await pilot.pause()
        assert app.focused is prompt, f"focus moved to {app.focused}"
        await pilot.press("x")
        await pilot.pause()
        assert "x" in prompt.text


@pytest.mark.asyncio
async def test_auto_open_refuses_a_window_too_narrow_to_split(
    tmp_path, monkeypatch,
):
    """Refuse rather than mangle -- and through the SAME
    ``layout.split_refusal`` floor a hand-driven split hits, not a second
    rule invented for this path. The refusal is SHOWN, and the allowance
    is spent rather than re-asked on every subsequent edit."""
    monkeypatch.setenv("DOXA_AUTO_DIFF", "1")
    config_mod.invalidate()
    work = _repo(tmp_path)
    app, _ = _app(work)
    notes: "list[str]" = []
    async with app.run_test(size=(60, 24)) as pilot:
        pane = app.active_pane
        assert await _settled(pilot, app)
        app.notify = lambda msg, **kw: notes.append(str(msg))  # type: ignore[assignment]
        _tick(app)
        assert await _wait(pilot, lambda: bool(notes)), "the refusal was swallowed"
        assert not list(app.query(DiffPane)), "a sliver was created anyway"
        assert "not enough width to split" in notes[0]
        assert "F2" in notes[0]
        assert pane._auto_diff_done


@pytest.mark.asyncio
async def test_a_restored_diff_spends_the_allowance_rather_than_queueing(
    tmp_path, monkeypatch,
):
    """A diff that is already open IS this session's one open. Without
    this the first tick after a restore would find the flag False, and
    the auto-open would be racing a pane that is already on screen."""
    monkeypatch.setenv("DOXA_AUTO_DIFF", "1")
    config_mod.invalidate()
    work = _repo(tmp_path)
    app, _ = _app(work)
    async with app.run_test(size=(160, 48)) as pilot:
        pane = app.active_pane
        assert await _settled(pilot, app)
        assert await app.toggle_diff_pane() is None  # as a restore leaves it
        assert await _wait(pilot, lambda: bool(list(app.query(DiffPane))))
        assert not pane._auto_diff_done
        _tick(app)
        await pilot.pause()
        assert pane._auto_diff_done
        assert len(list(app.query(DiffPane))) == 1


@pytest.mark.asyncio
async def test_the_setting_is_a_real_registry_row_that_is_off_by_default():
    """No placeholder settings (doxa/config.py's own rule): the row
    exists, it is a bool, its default is empty (off), and the function
    that reads it agrees."""
    row = next(s for s in config_mod.SETTINGS if s.key == "auto_diff")
    assert row.env == "DOXA_AUTO_DIFF"
    assert row.kind == "bool" and row.default == ""
    assert row.category == "Session"
    assert not diff_mod.auto_open_enabled()


# -- the diff's colours -------------------------------------------------


def _hunk(*lines, old=10, new=10):
    return diff_mod.Hunk(
        header="@@", old_start=old, old_count=len(lines),
        new_start=new, new_count=len(lines), lines=tuple(lines),
    )


def _style_at(text, needle):
    """The style covering the first character of ``needle``."""
    idx = text.plain.index(needle)
    return next(
        (str(s.style) for s in text.spans if s.start <= idx < s.end), ""
    )


def test_a_removed_row_is_red_and_an_added_row_is_green(tmp_path):
    """Backgrounds, not just foregrounds -- v0.92.0 set the foreground
    only. Asserted on the rendered Text's spans, never on a screenshot."""
    text = _hunk_text(_hunk(" keep", "-gone", "+here"), width=80)
    assert DIFF_DEL_BG in _style_at(text, "-gone")
    assert DIFF_ADD_BG in _style_at(text, "+here")
    # A context row carries NEITHER background: the wash is the signal,
    # and a signal on every row is not one.
    keep = _style_at(text, " keep")
    assert DIFF_DEL_BG not in keep and DIFF_ADD_BG not in keep


def test_the_line_numbers_are_coloured_by_kind_and_sit_outside_the_wash():
    """An added line's number green, a removed line's red -- and on the
    pane's own ramp rather than on the wash, because a green number
    painted on the green background is the one part of this that cannot
    be read."""
    text = _hunk_text(_hunk(" keep", "-gone", "+here"), width=80)
    lines = text.plain.splitlines()
    del_num = _style_at(text, lines[1][:8])
    add_num = _style_at(text, lines[2][:8])
    assert del_num == DIFF_DEL_NUM
    assert add_num == DIFF_ADD_NUM
    assert "on " not in del_num and "on " not in add_num


def test_the_line_numbers_are_the_hunks_own_ranges():
    """Walked against the ``@@`` header: a ``-`` advances the old
    counter only, a ``+`` the new only, a context line both. Nothing is
    re-parsed and nothing is guessed."""
    text = _hunk_text(
        _hunk(" a", "-b", "+c", " d", old=10, new=20), width=0
    )
    rows = text.plain.splitlines()
    assert rows[0] == " 10  20  a"       # context: both
    assert rows[1] == " 11     -b"       # removed: the old number only
    assert rows[2] == "     21 +c"       # added: the new number only
    assert rows[3] == " 12  22  d"       # context: both advanced


def test_a_hunk_body_containing_a_bracket_stays_literal():
    """The markup trap, pinned. A diff body is arbitrary source and
    source contains ``[``; this renders through Rich ``Text`` and
    ``style=`` arguments, never through console markup, so the bracket
    is text."""
    body = "+items[0] = [red]not markup[/]"
    text = _hunk_text(_hunk(body), width=80)
    assert body in text.plain
    assert "[red]" in text.plain
    title = _file_title(diff_mod.FileDiff(path="src/[id]/page.py"))
    assert "src/[id]/page.py" in title.plain


def test_the_no_newline_note_and_the_truncation_note_are_unchanged():
    """Two things this release must not have disturbed."""
    text = _hunk_text(_hunk(" a", "\\ No newline at end of file"), width=80)
    assert "\\ No newline at end of file" in text.plain
    assert "italic" in _style_at(text, "\\ No newline")
    from doxa.ui.diffview import MAX_HUNK_ROWS

    long = _hunk(*[f"+line{i}" for i in range(MAX_HUNK_ROWS + 5)])
    rendered = _hunk_text(long, width=80)
    assert "5 more lines not shown" in rendered.plain
    assert rendered.plain.count("\n") == MAX_HUNK_ROWS + 1


def test_side_by_side_numbers_each_side_from_its_own_file():
    """One number per side, because each side IS one file: the left
    column is the old file and the number in front of a left-hand line
    can only be an old line number."""
    text = _side_by_side_text(_hunk(" a", "-b", "+c", old=10, new=20), 160)
    rows = text.plain.splitlines()
    assert rows[0].startswith(" 10 a")
    left, _, right = rows[1].partition("│")
    assert left.startswith(" 11 b")
    assert right.startswith(" 21 c")
    assert DIFF_DEL_BG in _style_at(text, "b   ")
    assert DIFF_ADD_BG in _style_at(text, "c   ")


def test_the_file_fold_carries_the_counts_in_the_same_two_colours():
    """The vocabulary reads across both surfaces: the fold's ``+42 −7``
    wears exactly what the chip's does, and the WORDING is still the
    model's (``FileDiff.summary_parts``)."""
    fd = diff_mod.FileDiff(
        path="f.py",
        hunks=(_hunk("+a", "+b", "-c"),),
    )
    title = _file_title(fd)
    assert fd.summary() in title.plain.removeprefix("◈ ")
    assert _style_at(title, "+2") == DIFF_ADD_NUM
    assert _style_at(title, "−1") == DIFF_DEL_NUM


@pytest.mark.asyncio
async def test_the_pane_paints_the_wash_at_its_real_width(tmp_path):
    """The rendering, in the real widget, at a real rectangle -- and the
    row padded to the width it is actually given (six columns inside the
    pane: Collapsible's padding plus its Contents'), because a row padded
    too far wraps and puts an empty coloured line under every change."""
    work = _repo(tmp_path)
    app, _ = _app(work)
    async with app.run_test(size=(160, 48)) as pilot:
        pane = app.active_pane
        assert await _wait(pilot, lambda: bool(pane._session_id))
        assert await app.toggle_diff_pane() is None
        diff = next(iter(app.query(DiffPane)))
        assert await _wait(
            pilot,
            lambda: diff.region.width > 0
            and any(s.file_diff.path == "f.py" for s in diff.query(FileSection)),
        )
        section = next(
            s for s in diff.query(FileSection) if s.file_diff.path == "f.py"
        )
        section.collapsed = False
        section.build(diff.size.width)
        assert await _wait(pilot, lambda: bool(list(diff.query(HunkView))))
        view = next(iter(diff.query(HunkView)))
        assert await _wait(pilot, lambda: view._body.size.width > 0)
        body = view._body.renderable
        rows = [r for r in body.plain.splitlines() if r.strip()]
        changed = [r for r in rows if r[8:9] in "+-"]
        assert changed, rows
        for row in changed:
            assert len(row) == view._body.size.width, (len(row), row)
