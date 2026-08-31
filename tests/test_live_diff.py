# SPDX-License-Identifier: AGPL-3.0-only
"""The live diff (v0.92.0) — its model, and its leaf.

Two halves, and the split between them is the point. The MODEL half runs
against a **real git worktree** with real commits, because the claims
that matter there are claims about what git accepted: a reverse patch
that applies, a sibling hunk that survives it, a stale patch that changes
nothing. A mock cannot be wrong about those in the way that matters.

The LEAF half runs against a real ``Pilot``, and v0.28.0's lesson applies
with the same force it does in ``tests/test_split_panes.py``: a widget
present in the DOM and painted nowhere passed every structural assertion
for a whole release. So every claim about the pane is paired with a
rendered rectangle, and the polls are for the PAINTED state, never for
the mount.

What each section pins:

* **the base** — "no changes" and "cannot tell" are different sentences,
  and ``base_ref == branch`` is the second one (v0.33.0's measured trap);
* **parsing** — hunks come out of ``git diff``, and binary/huge files are
  named rather than rendered;
* **reject** — exactly one hunk reverses, the sibling survives, a patch
  that no longer applies changes nothing and says why;
* **the message** — user-authored, down the same door a typed prompt
  uses, and NOT through ``PEER_UNTRUSTED_INTRO``;
* **the leaf** — it paints beside the session, at 80 columns, and keeps
  painting while focus is in the session;
* **the queue** — a rejection clicked mid-turn is visibly pending and
  applies at the end of the turn.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from doxa import config as config_mod
from doxa import diff as diff_mod
from doxa import layout
from doxa import peers as peers_mod
from doxa.app import DoxaApp
from doxa.ui.diffview import DiffPane, FileSection, HunkView
from doxa.ui.split import PaneTab
from tests.fakes import FakeEngine


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    config_mod.invalidate()
    yield
    config_mod.invalidate()


# -- a real repo --------------------------------------------------------


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )


#: Thirty lines, so two edits ten apart land in two SEPARATE hunks under
#: git's default three lines of context. The whole "reject one hunk, keep
#: the other" claim rests on that separation being real, so the file is
#: sized for it rather than hoped at.
_BODY = [f"line{i}" for i in range(1, 31)]


def _repo(root: Path, *, base_is_branch: bool = False) -> Path:
    """A worktree with a committed base, two independent edits, one new
    file and one binary file — and a DOXA sidecar naming its base."""
    work = root / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@example.invalid")
    _git(work, "config", "user.name", "t")
    (work / "f.py").write_text("\n".join(_BODY) + "\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "base")
    _git(work, "checkout", "-qb", "doxa/abc12345")
    edited = list(_BODY)
    edited[2] = "CHANGED_TOP"
    edited[24] = "CHANGED_BOTTOM"
    (work / "f.py").write_text("\n".join(edited) + "\n")
    (work / "new.txt").write_text("created by the agent\n")
    (work / "logo.bin").write_bytes(bytes(range(256)) * 8)
    _sidecar(
        work,
        base_ref="doxa/abc12345" if base_is_branch else "main",
        branch="doxa/abc12345",
    )
    return work


def _sidecar(work: Path, **fields) -> None:
    """The record :func:`doxa.worktrees.read_meta` reads, written where
    it reads it from: ``$DOXA_HOME/worktrees/.meta/<dirname>.json``."""
    from doxa import worktrees as worktrees_mod

    meta = worktrees_mod.worktrees_root() / ".meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / f"{work.name}.json").write_text(
        json.dumps({"main_root": str(work), "session_id": "abc12345", **fields})
    )


# -- the base -----------------------------------------------------------


def test_no_changes_and_cannot_tell_are_different_sentences(tmp_path):
    """The whole of v0.33.0's inheritance, in one assertion pair.

    A base equal to the branch cannot show anything the session
    committed, so an empty diff there means nothing at all — and the
    defect that force-deleted real commits was exactly a zero being read
    as an answer. Both results are "empty"; only one of them is a fact."""
    work = _repo(tmp_path, base_is_branch=True)
    blind = diff_mod.compute(str(work))
    assert blind.status == diff_mod.STATUS_NO_BASE
    assert "cannot determine a base" in blind.headline()
    assert "no changes" not in blind.headline()
    assert not blind.files

    clean = tmp_path / "clean"
    clean.mkdir()
    _git(clean, "init", "-q", "-b", "main")
    _git(clean, "config", "user.email", "t@example.invalid")
    _git(clean, "config", "user.name", "t")
    (clean / "a.txt").write_text("a\n")
    _git(clean, "add", "-A")
    _git(clean, "commit", "-qm", "only")
    _sidecar(clean, base_ref="main", branch="doxa/zzz")
    quiet = diff_mod.compute(str(clean))
    assert quiet.status == diff_mod.STATUS_OK
    assert quiet.headline().startswith("no changes")
    assert "cannot" not in quiet.headline()


def test_a_missing_sidecar_falls_back_to_head_and_says_so(tmp_path):
    """Not the same refusal: worktree-per-session may simply be off, and
    uncommitted work against HEAD is a real, smaller claim. It is
    labelled as the smaller claim rather than passed off as the other."""
    work = _repo(tmp_path)
    from doxa import worktrees as worktrees_mod

    (worktrees_mod.worktrees_root() / ".meta" / f"{work.name}.json").unlink()
    result = diff_mod.compute(str(work))
    assert result.status == diff_mod.STATUS_OK
    assert result.base_source == diff_mod.BASE_HEAD
    assert "no worktree base recorded" in result.headline()


def test_a_directory_that_is_not_a_repo_is_an_error_not_an_empty_diff(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    result = diff_mod.compute(str(plain))
    assert result.status == diff_mod.STATUS_ERROR
    assert "cannot read the diff" in result.headline()


# -- parsing ------------------------------------------------------------


def test_two_edits_ten_lines_apart_parse_as_two_hunks(tmp_path):
    work = _repo(tmp_path)
    result = diff_mod.compute(str(work))
    by_path = {f.path: f for f in result.files}
    assert set(by_path) == {"f.py", "new.txt", "logo.bin"}
    f = by_path["f.py"]
    assert len(f.hunks) == 2
    assert f.added == 2 and f.removed == 2
    assert "+2 −2" in f.summary()
    # The hunk bodies are the raw patch lines, marker byte and all --
    # anything else and hunk_patch cannot rebuild a patch git accepts.
    assert any(ln == "-line3" for ln in f.hunks[0].lines)
    assert any(ln == "+CHANGED_TOP" for ln in f.hunks[0].lines)


def test_a_binary_file_is_named_not_rendered(tmp_path):
    work = _repo(tmp_path)
    result = diff_mod.compute(str(work))
    logo = next(f for f in result.files if f.path == "logo.bin")
    assert logo.binary
    assert logo.skipped == "binary"
    assert logo.hunks == ()
    assert "logo.bin" in logo.summary() and "binary" in logo.summary()


def test_a_created_file_appears_without_touching_the_index(tmp_path):
    """Open question 2, answered — and answered without ``git add
    --intent-to-add``: this is explicitly not ``git add -p``, and a
    review surface that writes the index has changed the thing it was
    reporting on."""
    work = _repo(tmp_path)
    before = _git(work, "diff", "--cached", "--name-only").stdout
    result = diff_mod.compute(str(work))
    new = next(f for f in result.files if f.path == "new.txt")
    assert new.untracked and new.added == 1
    assert "new file" in new.summary()
    assert _git(work, "diff", "--cached", "--name-only").stdout == before


def test_a_huge_diff_is_named_not_rendered_and_the_result_says_so(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(diff_mod, "MAX_HUNK_LINES_PER_FILE", 4)
    work = _repo(tmp_path)
    result = diff_mod.compute(str(work))
    f = next(x for x in result.files if x.path == "f.py")
    assert f.skipped and "too large to render" in f.skipped
    assert f.hunks == ()


def test_a_capped_file_list_says_it_was_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(diff_mod, "MAX_FILES", 1)
    work = _repo(tmp_path)
    result = diff_mod.compute(str(work))
    assert len(result.files) == 1
    assert result.dropped_files == 2
    assert "not shown" in result.truncated


def test_a_diff_body_containing_diff_markers_stays_one_file(tmp_path):
    """The parser is hand-rolled precisely for this: a removed line that
    itself begins ``--- `` is CONTENT, and only "we are inside a hunk"
    tells it apart from a file header."""
    work = _repo(tmp_path)
    (work / "f.py").write_text(
        "\n".join(["--- a/fake", "+++ b/fake", "@@ -1 +1 @@", *_BODY]) + "\n"
    )
    result = diff_mod.compute(str(work))
    assert [f.path for f in result.files if f.path == "f.py"] == ["f.py"]


# -- reject -------------------------------------------------------------


def test_rejecting_one_hunk_leaves_the_other_hunk_in_that_file_intact(tmp_path):
    """The testing bar's first line, against a real worktree."""
    work = _repo(tmp_path)
    result = diff_mod.compute(str(work))
    f = next(x for x in result.files if x.path == "f.py")
    outcome = diff_mod.revert_hunk(str(work), f, f.hunks[0])
    assert outcome.applied, outcome.message
    lines = (work / "f.py").read_text().splitlines()
    assert lines[2] == "line3"           # reverted
    assert lines[24] == "CHANGED_BOTTOM"  # untouched


def test_a_reverse_patch_that_no_longer_applies_changes_nothing(tmp_path):
    """The file moved underneath the recorded hunk — the ordinary case,
    because the agent edits while you read. ``--check`` first, so the
    refusal is reported from a call that could not have written."""
    work = _repo(tmp_path)
    result = diff_mod.compute(str(work))
    f = next(x for x in result.files if x.path == "f.py")
    moved = list(_BODY)
    moved[2] = "SOMETHING_ELSE_ENTIRELY"
    (work / "f.py").write_text("\n".join(moved) + "\n")
    snapshot = (work / "f.py").read_text()

    outcome = diff_mod.revert_hunk(str(work), f, f.hunks[0])
    assert not outcome.applied
    assert "no longer applies" in outcome.message
    assert "f.py" in outcome.message
    assert (work / "f.py").read_text() == snapshot


def test_a_file_shown_by_name_only_has_no_hunk_to_reject(tmp_path):
    work = _repo(tmp_path)
    result = diff_mod.compute(str(work))
    logo = next(f for f in result.files if f.path == "logo.bin")
    outcome = diff_mod.revert_hunk(
        str(work), logo, diff_mod.Hunk(header="@@", old_start=1, old_count=1,
                                       new_start=1, new_count=1)
    )
    assert not outcome.applied
    assert "name only" in outcome.message


# -- the message --------------------------------------------------------


def test_the_rejection_message_names_the_file_the_hunk_and_the_reason(tmp_path):
    work = _repo(tmp_path)
    result = diff_mod.compute(str(work))
    f = next(x for x in result.files if x.path == "f.py")
    text = diff_mod.reject_message(f, f.hunks[0], "we already have a helper")

    assert "f.py" in text
    assert "CHANGED_TOP" in text            # the hunk, quoted
    assert "we already have a helper" in text
    assert "Do not re-apply it" in text


def test_the_rejection_message_is_user_authored_not_untrusted_peer_text(tmp_path):
    """The trust argument, asserted rather than described. A peer message
    is wrapped because ANOTHER AGENT wrote it; a human clicking reject in
    their own session is the user speaking, and framing it as untrusted
    data would tell the agent to weigh its own user's instruction as
    hearsay."""
    work = _repo(tmp_path)
    result = diff_mod.compute(str(work))
    f = next(x for x in result.files if x.path == "f.py")
    text = diff_mod.reject_message(f, f.hunks[0], "")

    assert peers_mod.PEER_UNTRUSTED_INTRO not in text
    assert "UNTRUSTED" not in text
    assert "peer" not in text.lower()
    # No reason given is stated, not omitted: an agent told only "I
    # rejected this" will guess a reason, and a wrong guess is what makes
    # it re-make the edit.
    assert "did not give a reason" in text


# -- the tick -----------------------------------------------------------


def test_edit_write_and_notebookedit_are_ticks_and_read_only_bash_is_not():
    assert diff_mod.is_tick("Edit", {})
    assert diff_mod.is_tick("Write", {})
    assert diff_mod.is_tick("NotebookEdit", {})
    assert diff_mod.is_tick("Task", {})
    assert not diff_mod.is_tick("Read", {"file_path": "x"})
    assert not diff_mod.is_tick("Bash", {"command": "git status --porcelain"})
    assert not diff_mod.is_tick("Bash", {"command": "rg foo | head -20"})


def test_an_unrecognised_bash_command_counts_as_a_write():
    """Over-inclusive on purpose: a false tick costs one ``git diff``, a
    missed one costs a diff that silently disagrees with the disk."""
    assert diff_mod.is_tick("Bash", {"command": "make build"})
    assert diff_mod.is_tick("Bash", {"command": "sed -i s/a/b/ f.py"})
    assert diff_mod.is_tick("Bash", {"command": "echo hi > f.py"})
    assert diff_mod.is_tick("Bash", {"command": "git status && rm f.py"})
    assert diff_mod.is_tick("Bash", {"command": "git commit -am x"})


# -- the width threshold ------------------------------------------------


def test_side_by_side_is_refused_at_the_width_the_spec_names():
    """80 columns, split in half, is 40 per pane and 20 per side. The
    threshold is measured forward from a legible side, not chosen."""
    assert not diff_mod.side_by_side_allowed(40)
    assert not diff_mod.side_by_side_allowed(80)
    assert not diff_mod.side_by_side_allowed(
        diff_mod.SIDE_BY_SIDE_MIN_COLS - 1
    )
    assert diff_mod.side_by_side_allowed(diff_mod.SIDE_BY_SIDE_MIN_COLS)
    assert diff_mod.split_columns(diff_mod.SIDE_BY_SIDE_MIN_COLS) >= (
        diff_mod.SIDE_MIN_COLS
    )


def test_an_unmeasured_width_renders_unified():
    """The opposite of the ctx chip's answer, deliberately: a chip
    appearing late is a flicker, a side-by-side that had to fall back
    after the user read it is a page that changed under them."""
    assert not diff_mod.side_by_side_allowed(0)


# -- the layout leaf ----------------------------------------------------


def test_a_diff_leaf_round_trips_through_the_layout_record():
    """Through v0.91.0 a Leaf was a session and nothing else. Without
    ``view``, ``split._leaf_of`` returns None for the diff, the split
    collapses to one child, and the record says "one pane" for a screen
    showing two."""
    tree = layout.Split(
        layout.ROW,
        (
            layout.Leaf(session_id="s1"),
            layout.Leaf(session_id="s1", view=layout.VIEW_DIFF),
        ),
    )
    back = layout.from_json(json.loads(json.dumps(layout.to_json(tree))))
    assert isinstance(back, layout.Split)
    assert len(back.children) == 2
    assert back.children[1].is_diff
    assert not back.children[0].is_diff


def test_a_session_only_record_is_byte_identical_to_the_v0_91_0_one():
    """``view`` is written only when it is not the default, so nothing
    already on disk changes shape and a v0.91.0 reader sees what it
    always saw."""
    assert "view" not in layout.to_json(layout.Leaf(session_id="s1"))


def test_an_unknown_view_restores_as_a_session_rather_than_vanishing():
    leaf = layout.from_json({"kind": "leaf", "session_id": "s1", "view": "hologram"})
    assert leaf is not None and leaf.view == layout.VIEW_SESSION


def test_a_diff_leaf_is_pruned_with_the_session_it_is_a_diff_of():
    """It carries that session's id for exactly this reason: prune stays
    correct without knowing anything about diffs."""
    tree = layout.Split(
        layout.ROW,
        (
            layout.Leaf(session_id="dead"),
            layout.Leaf(session_id="dead", view=layout.VIEW_DIFF),
            layout.Leaf(session_id="alive"),
        ),
    )
    assert layout.prune(tree, {"alive"}) == layout.Leaf(session_id="alive")


# -- the pane, against a Pilot ------------------------------------------


def _app(cwd):
    engines: list[FakeEngine] = []

    def make() -> FakeEngine:
        engine = FakeEngine([], cwd=str(cwd))
        # Engine parity: SessionPane._boot reads `engine.session_id` on
        # EVERY boot and writes it to `pane._session_id`, so a fake
        # without one boots the pane back to "no session" underneath any
        # value a test wrote by hand. The diff is keyed on that id, so
        # the fake has to carry it like the real engine does.
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


def _diff_of(app) -> "DiffPane | None":
    return next(iter(app.query(DiffPane)), None)


async def _open_diff(pilot, app):
    # Wait for the session to HAVE an id before asking for its diff: the
    # pane learns it from the engine's ``session_started`` event, and a
    # diff opened before that would be a diff of nothing. Polling for the
    # settled state rather than forcing the attribute is the same
    # discipline the rest of the suite follows -- a test that writes the
    # state it is about to assert on is testing itself.
    pane = app.active_pane
    assert await _wait(pilot, lambda: bool(pane._session_id)), "no session id"
    note = await app.toggle_diff_pane()
    assert note is None, note
    ok = await _wait(
        pilot,
        lambda: (
            _diff_of(app) is not None
            and _diff_of(app).region.width > 0
            and "reading the diff" not in str(_diff_of(app)._head.renderable)
        ),
    )
    assert ok, "the diff pane never painted"
    return _diff_of(app)


@pytest.mark.asyncio
async def test_the_diff_paints_beside_the_session_both_with_real_rectangles(
    tmp_path,
):
    """*Session left, diff right, both live* — the design check the spec
    asks of v0.91.0's split, run for real. Both rectangles, both
    non-zero, the diff to the RIGHT of the session."""
    work = _repo(tmp_path)
    app, _ = _app(work)
    async with app.run_test(size=(160, 48)) as pilot:
        pane = app.active_pane
        diff = await _open_diff(pilot, app)

        assert pane.region.width > 0 and pane.region.height > 0
        assert diff.region.width > 0 and diff.region.height > 0
        assert diff.region.x >= pane.region.x + pane.region.width

        tab = pane.tab
        assert isinstance(tab, PaneTab)
        tree = tab.tree()
        assert isinstance(tree, layout.Split)
        assert tree.orientation == layout.ROW
        assert [n.is_diff for n in tree.children] == [False, True]


@pytest.mark.asyncio
async def test_the_diff_pane_renders_with_non_zero_height_at_eighty_columns(
    tmp_path,
):
    """The testing bar's last line, and the v0.28.0 failure mode for any
    new surface: present in the DOM, drawn nowhere. 80 columns is also
    where side-by-side must NOT be in use."""
    work = _repo(tmp_path)
    app, _ = _app(work)
    async with app.run_test(size=(80, 24)) as pilot:
        pane = app.active_pane
        diff = await _open_diff(pilot, app)
        assert diff.region.height > 0 and diff.region.width > 0
        assert not diff_mod.side_by_side_allowed(diff.region.width)


@pytest.mark.asyncio
async def test_the_files_are_collapsed_by_default_with_their_counts(tmp_path):
    """The ``ToolCallsSection`` pattern: a twenty-file diff must not be a
    wall, and the fold has to carry enough to decide whether to open it."""
    work = _repo(tmp_path)
    app, _ = _app(work)
    async with app.run_test(size=(160, 48)) as pilot:
        diff = await _open_diff(pilot, app)
        await _wait(pilot, lambda: len(diff.query(FileSection)) == 3)

        sections = list(diff.query(FileSection))
        assert sections and all(s.collapsed for s in sections)
        titles = " ".join(str(s.title) for s in sections)
        assert "f.py" in titles and "+2 −2" in titles
        assert "logo.bin" in titles and "binary" in titles
        # Collapsed means the hunks were never built at all -- lazy, the
        # way ToolChip.format_body is.
        assert not any(s.query(HunkView) for s in sections)


@pytest.mark.asyncio
async def test_expanding_a_file_builds_its_hunks_once(tmp_path):
    work = _repo(tmp_path)
    app, _ = _app(work)
    async with app.run_test(size=(160, 48)) as pilot:
        diff = await _open_diff(pilot, app)
        await _wait(pilot, lambda: len(diff.query(FileSection)) == 3)
        section = next(
            s for s in diff.query(FileSection) if s.file_diff.path == "f.py"
        )
        section.collapsed = False
        assert await _wait(pilot, lambda: len(section.query(HunkView)) == 2)
        section.collapsed = True
        section.collapsed = False
        await pilot.pause(0.05)
        assert len(section.query(HunkView)) == 2


@pytest.mark.asyncio
async def test_the_diff_keeps_updating_while_focus_is_in_the_session(tmp_path):
    """v0.91.0's "visible and focused are different states", exercised by
    its first concrete consumer. The tick is the tool-result stream, so
    this drives the runtime's own hook rather than a private method."""
    work = _repo(tmp_path)
    app, _ = _app(work)
    async with app.run_test(size=(160, 48)) as pilot:
        pane = app.active_pane
        diff = await _open_diff(pilot, app)
        await _wait(pilot, lambda: len(diff.query(FileSection)) == 3)

        pane.query_one("#prompt-input").focus()
        await pilot.pause(0.05)
        assert app.focused_pane() is pane

        (work / "another.txt").write_text("an edit that landed\n")
        pane._tick_diff("Edit", {"file_path": "another.txt"})
        assert await _wait(
            pilot,
            lambda: any(
                s.file_diff.path == "another.txt" for s in diff.query(FileSection)
            ),
        )
        # Still not focused, still painted, still beside the session.
        assert app.focused_pane() is pane
        assert diff.region.width > 0


@pytest.mark.asyncio
async def test_alt_arrow_widens_the_diff_when_the_keyboard_is_in_it(tmp_path):
    """The "sibling gesture" the spec asks for — and it needed no new
    key: v0.91.0's Alt+arrow already moved the boundary between leaves;
    it only had to stop assuming a leaf was a session."""
    work = _repo(tmp_path)
    app, _ = _app(work)
    async with app.run_test(size=(160, 48)) as pilot:
        diff = await _open_diff(pilot, app)
        diff.focus()
        await pilot.pause(0.05)
        before = diff.region.width
        assert app.grow_pane_towards("left")
        await pilot.pause(0.05)
        assert diff.region.width > before


@pytest.mark.asyncio
async def test_directional_focus_reaches_the_diff_and_comes_back(tmp_path):
    work = _repo(tmp_path)
    app, _ = _app(work)
    async with app.run_test(size=(160, 48)) as pilot:
        pane = app.active_pane
        diff = await _open_diff(pilot, app)
        pane.query_one("#prompt-input").focus()
        await pilot.pause(0.05)

        assert app.focus_pane_towards("right")
        await pilot.pause(0.05)
        assert app.focused_surface() is diff
        # ...and "which session does this keystroke mean" still answers
        # with the session, because a key aimed at a session pressed while
        # looking at its diff is aimed at that session.
        assert app.active_pane is pane

        assert app.focus_pane_towards("left")
        await pilot.pause(0.05)
        assert app.focused_surface() is pane


# -- the queue ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_rejection_clicked_during_a_turn_is_visibly_pending(tmp_path):
    """The spec weighs three answers and lands on queue-until-turn_done
    because a rejection the user has clicked and CANNOT SEE THE EFFECT OF
    is the worst of the three. So the badge is not decoration — it is the
    half of the decision that makes it defensible."""
    work = _repo(tmp_path)
    app, _ = _app(work)
    async with app.run_test(size=(160, 48)) as pilot:
        pane = app.active_pane
        diff = await _open_diff(pilot, app)
        await _wait(pilot, lambda: len(diff.query(FileSection)) == 3)
        section = next(
            s for s in diff.query(FileSection) if s.file_diff.path == "f.py"
        )
        section.collapsed = False
        assert await _wait(pilot, lambda: len(section.query(HunkView)) == 2)

        pane.turn_in_flight = True
        view = section.query(HunkView)[0]
        await diff.reject(view)
        await pilot.pause(0.05)

        assert len(diff.queued) == 1
        assert view._pending.display is True
        assert "queued" in str(view._pending.renderable)
        assert view._button.disabled
        # Nothing on disk moved: that is what "queued" MEANS.
        assert "CHANGED_TOP" in (work / "f.py").read_text()

        pane.turn_in_flight = False
        await diff.flush_pending()
        await pilot.pause(0.05)
        assert not diff.queued
        lines = (work / "f.py").read_text().splitlines()
        assert lines[2] == "line3"
        assert lines[24] == "CHANGED_BOTTOM"


def test_building_a_section_that_is_not_mounted_yet_raises_nothing(tmp_path):
    """Measured in a full-suite run and in none of the targeted ones: a
    ``Collapsible`` is handed its contents in ``__init__``, so a section's
    hunk container exists from its first line and is mounted only when the
    section composes — and ``_remark_queued`` builds a section it JUST
    mounted, which is exactly that window. It surfaced as ``MountError:
    Can't mount widget(s) before Vertical(classes='diff-hunks') is
    mounted`` from a background task, i.e. as an error block nobody
    claimed. Deferred, not dropped: an unbuilt section is a fold that
    opens onto nothing."""
    work = _repo(tmp_path)
    result = diff_mod.compute(str(work))
    section = FileSection(next(f for f in result.files if f.path == "f.py"))
    section.build(120)  # never mounted, never composed
    assert not section.query(HunkView)


@pytest.mark.asyncio
async def test_a_pending_badge_survives_the_next_edit(tmp_path):
    """The whole argument for queueing over refusing is that the user can
    SEE the pending state — so a badge that expired on the agent's next
    edit would leave the choice with none of its justification. Every
    tick rebuilds the hunk widgets, so the badge has to be put back."""
    work = _repo(tmp_path)
    app, _ = _app(work)
    async with app.run_test(size=(160, 48)) as pilot:
        pane = app.active_pane
        diff = await _open_diff(pilot, app)
        await _wait(pilot, lambda: len(diff.query(FileSection)) == 3)
        section = next(
            s for s in diff.query(FileSection) if s.file_diff.path == "f.py"
        )
        section.collapsed = False
        assert await _wait(pilot, lambda: len(section.query(HunkView)) == 2)

        pane.turn_in_flight = True
        await diff.reject(section.query(HunkView)[0])
        await pilot.pause(0.05)
        assert len(diff.queued) == 1

        (work / "yet_another.txt").write_text("the agent kept working\n")
        pane._tick_diff("Write", {"file_path": "yet_another.txt"})
        assert await _wait(
            pilot,
            lambda: any(
                s.file_diff.path == "yet_another.txt"
                for s in diff.query(FileSection)
            ),
        )
        assert await _wait(
            pilot,
            lambda: any(
                v._pending.display for v in diff.query(HunkView)
            ),
        ), "the pending badge did not survive the rebuild"
        # ...and the file it is in is still open, not folded shut under
        # the user mid-read.
        again = next(
            s for s in diff.query(FileSection) if s.file_diff.path == "f.py"
        )
        assert not again.collapsed


@pytest.mark.asyncio
async def test_rejecting_outside_a_turn_reverts_and_tells_the_agent(tmp_path):
    """Both actions, in that order. The message goes down the door a
    typed prompt uses — ``SessionPane._run_turn`` — which is what makes
    it user-authored rather than peer data."""
    work = _repo(tmp_path)
    app, engines = _app(work)
    async with app.run_test(size=(160, 48)) as pilot:
        pane = app.active_pane
        diff = await _open_diff(pilot, app)
        await _wait(pilot, lambda: len(diff.query(FileSection)) == 3)
        section = next(
            s for s in diff.query(FileSection) if s.file_diff.path == "f.py"
        )
        section.collapsed = False
        assert await _wait(pilot, lambda: len(section.query(HunkView)) == 2)

        sent: list[str] = []
        pane._run_turn = lambda text: _record(sent, text)  # type: ignore
        await diff.reject(section.query(HunkView)[0])
        assert await _wait(pilot, lambda: bool(sent))

        assert (work / "f.py").read_text().splitlines()[2] == "line3"
        assert "f.py" in sent[0]
        assert peers_mod.PEER_UNTRUSTED_INTRO not in sent[0]


async def _record(sink, text):
    sink.append(text)


@pytest.mark.asyncio
async def test_closing_a_diff_with_queued_rejections_is_refused(tmp_path):
    """Open question 4's near neighbour: a queued rejection is state, and
    the one thing that must not happen is losing it silently."""
    work = _repo(tmp_path)
    app, _ = _app(work)
    async with app.run_test(size=(160, 48)) as pilot:
        pane = app.active_pane
        diff = await _open_diff(pilot, app)
        result = diff.result
        f = next(x for x in result.files if x.path == "f.py")
        diff.queued.append(
            diff_mod.PendingRejection(
                path="f.py", hunk_label=f.hunks[0].label, reason="",
                file_diff=f, hunk=f.hunks[0],
            )
        )
        note = await app.toggle_diff_pane()
        assert note and "still queued" in note
        assert _diff_of(app) is diff


@pytest.mark.asyncio
async def test_a_second_diff_command_closes_the_one_that_is_open(tmp_path):
    """Open question 3, answered: one diff per SESSION, matching the
    isolation model. Two sessions in worktrees off the same branch have
    two different diffs."""
    work = _repo(tmp_path)
    app, _ = _app(work)
    async with app.run_test(size=(160, 48)) as pilot:
        await _open_diff(pilot, app)
        assert await app.toggle_diff_pane() is None
        assert await _wait(pilot, lambda: _diff_of(app) is None)


# -- the keys -----------------------------------------------------------


def test_the_diff_key_is_claimed_by_the_diff_and_by_nothing_else():
    """F2 since v0.95.0, with Alt+G kept beside it for kitty-protocol
    terminals -- where it was the only place it ever worked. See
    tests/test_split_keys.py for the parser measurement that moved it."""
    app = DoxaApp(cwd=".")
    resolved = dict(app._bindings.key_to_bindings)
    for key in ("f2", "alt+g"):
        assert [b.action for b in resolved[key]] == ["toggle_diff"], key
        assert all(b.priority for b in resolved[key]), key


def test_diff_is_a_real_command_with_a_registry_row_and_a_handler():
    from doxa import commands as commands_mod
    from doxa.session.commands import PANE_COMMANDS

    row = commands_mod.lookup("/diff")
    assert row is not None and not row.passthrough
    assert row.group == "Panes & tabs"
    assert "(F2)" in row.summary
    assert {c.name: c.method for c in PANE_COMMANDS}["/diff"] == "_cmd_diff"
