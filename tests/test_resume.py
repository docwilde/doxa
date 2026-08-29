# SPDX-License-Identifier: AGPL-3.0-only
"""Session RESUME (v0.56.0): reopening a finished conversation.

Two halves, and the second is the one with teeth.

The UI half is the ``/search`` popup's new Enter: on a session HEADER it
opens a confirm dialog instead of toggling that header's fold (the fold
kept Right and Left, which is what freed the key); on a HIT row it still
copies the excerpt into the prompt, unchanged, and there is a test here
whose only job is to keep it that way. Every revealed row now carries when
it happened.

The other half is that resume has to actually work, and that turned out to
rest on a fact nothing in this repository could have told us. DOXA minted
its own session uuid and named its LORE transcript -- and therefore every
``/search`` row -- after it; the CLI, given no id of its own, minted a
SECOND uuid and wrote ITS store under that. Measured live against a real
``claude`` under ``cli_isolation.spawn_env`` before any of this was
written: doxa sid ``360a8897…``, CLI sid ``f45bce98…``; ``resume=<CLI
sid>`` replayed the conversation, ``resume=<doxa sid>`` failed with ``No
conversation found with session ID``. A resume keyed on the id the search
list shows would have been broken for every session ever recorded.

v0.56.0 closes that by asking the CLI to USE DOXA's id
(``ClaudeAgentOptions.session_id``, measured: honored exactly), so the two
spaces become one. The tests below pin the options-building that does it,
and pin the honest refusal for every conversation recorded BEFORE it --
those are still readable and searchable, and are not resumable, and the
confirm dialog says so rather than letting the user find out one prompt
in.

Nothing here spawns a CLI or spends a token: the options object is
inspected directly, and the app-level resume runs against an injected
factory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from doxa import cli_isolation as cli_isolation_mod
from doxa import history as history_mod
from doxa import peers as peers_mod
from doxa.app import DoxaApp
from doxa.engine import SessionEngine
from doxa.history import SEARCH_PREFIX, SessionSearch
from doxa.ui.dialogs import ResumeConfirm
from doxa.ui.labels import _fmt_age
from doxa.ui.transcript import TurnBlock
from tests.fakes import FakeEngine

RESUMED_ID = "11111111-2222-3333-4444-555555555555"
OTHER_ID = "99999999-8888-7777-6666-555555555555"


@pytest.fixture(autouse=True)
def _own_doxa_home(monkeypatch, tmp_path):
    """A fresh DOXA_HOME per test, because the isolated CLI store lives
    under it and half these tests turn on whether a given session id is IN
    that store. conftest's suite-wide home is shared, so one test seeding
    a history there would silently make the next test's "the CLI never
    knew this id" case impossible to write."""
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))


# -- the id space, which is the whole feature ------------------------------


def test_a_fresh_session_pins_its_own_id_onto_the_cli(tmp_path):
    """The fix for the measured two-id-spaces defect: DOXA's session id is
    handed to the CLI as ITS session id, so the id ``/search`` shows is the
    id ``--resume`` will accept. Without this key the CLI mints its own and
    nothing in DOXA's index can ever be resumed."""
    engine = SessionEngine(cwd=str(tmp_path), session_id=RESUMED_ID)
    options = engine._build_options()
    assert options.session_id == RESUMED_ID
    assert options.resume is None


def test_a_resuming_session_sends_resume_alone(tmp_path):
    """The SDK: ``session_id`` "cannot be used with continue_conversation or
    resume unless fork_session is also set". A resume therefore sends
    ``resume`` and no ``session_id`` -- which is also what makes it a
    CONTINUATION: measured, the resumed session comes back under the same
    id, so the transcript file, the registry entry and the /search row
    stay one conversation instead of forking into two."""
    engine = SessionEngine(
        cwd=str(tmp_path), session_id=RESUMED_ID, resume=RESUMED_ID,
    )
    options = engine._build_options()
    assert options.resume == RESUMED_ID
    assert options.session_id is None
    assert options.fork_session is False


def test_a_non_uuid_session_id_is_simply_not_pinned(tmp_path):
    """The SDK requires a UUID. A short synthetic id (the suite is full of
    them) must cost one omitted key, never a refused connect."""
    options = SessionEngine(cwd=str(tmp_path), session_id="s1")._build_options()
    assert options.session_id is None
    assert options.resume is None


# -- eligibility: what the dialog knows before it offers anything ----------


def _cli_history(session_id: str) -> Path:
    """Give the isolated CLI store a transcript under ``session_id`` -- the
    on-disk fact ``resume_state`` reads to decide a conversation is one the
    CLI can still continue."""
    path = (
        cli_isolation_mod.cli_config_dir() / "projects" / "-work"
        / f"{session_id}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"type":"user"}\n', encoding="utf-8")
    return path


def test_a_conversation_the_cli_never_knew_is_refused_in_words(tmp_path):
    """Every session recorded before v0.56.0 is in this state, and its
    /search row looks exactly like a resumable one. Saying so at the
    dialog is the entire reason this check exists."""
    state, reason = history_mod.resume_state(OTHER_ID, str(tmp_path))
    assert state == history_mod.RESUME_NO_HISTORY
    assert "v0.56.0" in reason
    assert "searchable" in reason  # says what still WORKS, not just what doesn't


def test_a_vanished_cwd_is_refused_before_anything_is_spawned(tmp_path):
    _cli_history(RESUMED_ID)
    state, reason = history_mod.resume_state(
        RESUMED_ID, str(tmp_path / "gone-for-good")
    )
    assert state == history_mod.RESUME_NO_CWD
    assert "nowhere to reopen it" in reason


def test_a_session_the_cli_knows_and_whose_cwd_exists_is_resumable(tmp_path):
    _cli_history(RESUMED_ID)
    state, reason = history_mod.resume_state(RESUMED_ID, str(tmp_path))
    assert (state, reason) == (history_mod.RESUME_OK, "")


def test_a_live_session_reads_as_running_not_as_resumable(tmp_path, monkeypatch):
    _cli_history(RESUMED_ID)
    entry = peers_mod.PeerInfo(
        session_id=RESUMED_ID, pid=os.getpid(), socket_path="",
        cwd=str(tmp_path), repo_root=None, title="live one",
        started_at="", heartbeat_at="", daemon_socket="/tmp/sock",
    )
    monkeypatch.setattr(peers_mod, "read_registry", lambda *a, **k: [entry])
    state, reason = history_mod.resume_state(RESUMED_ID, str(tmp_path))
    assert state == history_mod.RESUME_RUNNING
    assert "still RUNNING" in reason


# -- timestamps on every row the popup shows -------------------------------


def test_fmt_age_gained_a_day_tier_for_session_history():
    """Session history reaches back weeks, and the hour tier alone
    rendered last Tuesday as "168h0m" -- arithmetic rather than an answer.
    The tier itself came with v0.46.0's beliefs browser; what /search
    depends on is its CEILING, since the excerpt gutter is a fixed six
    columns and an age that overran it would shunt one row's snippet out
    of step with its neighbours."""
    assert _fmt_age(30) == "30s"
    assert _fmt_age(90) == "1m"
    assert _fmt_age(3 * 3600 + 15 * 60) == "3h15m"
    assert _fmt_age(3 * 86400 + 4 * 3600) == "3d4h"
    assert _fmt_age(365 * 86400) == "365d"  # hours dropped past ten days
    # The ceiling the gutter is sized from -- checked, not assumed.
    assert max(
        len(_fmt_age(s)) for s in range(0, 4000 * 86400, 3607)
    ) <= history_mod.AGE_COLUMNS - 1


def test_a_revealed_snippet_row_carries_its_own_age():
    """The user's ask: "every entry that decollapsing reveals should also
    carry a timestamp". The child row's six leading columns used to be
    blank indentation; they now hold that message's own age, so the
    excerpt beside them is not one column narrower than before."""
    hit = {"ts": "2026-08-20T10:00:00.000Z", "snippet": "docking [clamp] ran clean"}
    label = history_mod.child_row_label(hit)
    age = history_mod.hit_age(hit)
    assert age  # a seeded timestamp really does resolve to an age
    assert age in label.plain
    assert "docking clamp ran clean" in label.plain  # excerpt intact, in full
    assert label.plain.index("docking") == history_mod.AGE_COLUMNS


def test_a_session_header_carries_both_the_date_and_the_age():
    """Both clocks on the one row that can afford them: the absolute date
    is what makes a list of conversations orderable and citable by eye, the
    age is what makes it scannable. A header has no excerpt competing for
    the line, which is exactly why the child rows below it get only the
    cheap one."""
    group = {
        "session_id": RESUMED_ID, "title": "delorean wiring",
        "ts": "2026-08-20T10:00:00.000Z", "hits": [{}], "collapsed": True,
    }
    plain = history_mod.group_label(group).plain
    assert "2026-08-20 10:00" in plain
    assert history_mod.hit_age(group) in plain


def test_an_unreadable_timestamp_leaves_an_empty_gutter_not_a_number():
    """``peers.age_secs`` answers ``inf`` for anything it cannot parse.
    Rendering that as a number would be a claim DOXA was never given."""
    assert history_mod.hit_age({"ts": "not a date"}) == ""
    assert history_mod.age_cell({"ts": ""}).strip() == ""


# -- the popup's Enter, before and after ----------------------------------


def _seed(cwd: str = "/work", session_ids=(RESUMED_ID, OTHER_ID)) -> None:
    """Rows into LORE's msg/sessions tables, under the PROJECT SLUG of the
    cwd the test's app runs in.

    The slug matters and cost a full-suite failure to learn: the index is
    shared across the whole run, ``recent_sessions`` is capped at twenty
    and orders by timestamp, and by the time this module runs the suite has
    written dozens of newer sessions. Seeded under a foreign project these
    rows pass alone and vanish in a full run. Seeded under the app's own
    project they come back on ``recent_sessions``' FIRST pass -- the
    this-project-before-everywhere-else scoping that function actually
    implements -- regardless of what else is in the table."""
    from doxa import _lore_bootstrap  # noqa: F401
    from lore_core.config import project_slug
    from lore_core import store as lore_store

    project = project_slug(cwd)
    conn = lore_store.db_connect()
    for sid in session_ids:
        conn.execute("DELETE FROM msg WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
        conn.execute(
            "INSERT INTO msg(session_id, project, ts, role, content)"
            " VALUES(?,?,?,?,?)",
            (sid, project, "2026-08-20T10:00:00Z", "user",
             f"warpcore alignment notes for {sid[:4]}"),
        )
        conn.execute(
            "INSERT INTO sessions(session_id, project, cwd, title, first_ts,"
            " last_ts, messages) VALUES(?,?,?,?,?,?,?)",
            (sid, project, cwd, f"warpcore {sid[:4]}",
             "2026-08-20T10:00:00Z", "2026-08-20T10:00:00Z", 7),
        )
    conn.commit()


async def _app(monkeypatch, tmp_path, resumed=None):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr(
        "doxa.app.SessionEngine", lambda **kw: FakeEngine([], cwd=kw.get("cwd", ""))
    )
    app = DoxaApp(cwd=str(tmp_path))
    if resumed is not None:
        def factory(path: str, session_id: str):
            resumed.append((path, session_id))
            return FakeEngine([], cwd=path)
        app._resume_session_factory = factory
    return app


def _hit(app, widget):
    """The widget the SCREEN reports at this one's own centre -- the hit
    test a mouse actually performs. A zero-height door passes every
    query_one() in the suite and fails this. Same helper, same reason, as
    tests/test_about.py's."""
    region = widget.region
    if not region.area:
        return None
    try:
        found, _region = app.screen.get_widget_at(
            region.x + region.width // 2, region.y + region.height // 2,
        )
    except Exception:
        return None
    return found


async def _open_search(pilot, app, term):
    popup = app.query_one("#session-search", SessionSearch)
    for char in SEARCH_PREFIX + term:
        await pilot.press({"/": "slash", " ": "space"}.get(char, char))
    for _ in range(300):
        if popup.hits and popup.query_text == term:
            break
        await pilot.pause(0.02)
    return popup


@pytest.mark.asyncio
async def test_enter_on_a_conversation_row_opens_a_visible_confirm(
    monkeypatch, tmp_path
):
    """The gesture the user asked for, and the v0.28.0 lesson enforced.

    That release shipped a modal whose buttons were ``height: 1`` plus
    ``padding-top: 1`` -- border-box, so both Statics laid out at zero
    height: present in the DOM, passing every ``query_one``, drawn
    nowhere, for a whole release, because the tests asserted the modal was
    pushed and never that anything was visible. So this asserts REGION
    HEIGHT and on-screen text, not existence."""
    _seed(cwd=str(tmp_path))
    _cli_history(RESUMED_ID)
    _cli_history(OTHER_ID)
    app = await _app(monkeypatch, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        popup = await _open_search(pilot, app, "warpcore")
        assert popup.current_kind() == "header"  # two sessions: a tree

        await pilot.press("enter")
        for _ in range(300):
            if isinstance(app.screen, ResumeConfirm):
                break
            await pilot.pause(0.02)
        dialog = app.screen
        assert isinstance(dialog, ResumeConfirm)
        await pilot.pause()

        row = dialog.query_one("#resume-confirm-buttons")
        assert row.size.height > 0, f"button row collapsed: {row.size}"
        for wid in ("#resume-confirm-yes", "#resume-confirm-no"):
            button = dialog.query_one(wid)
            assert button.size.height > 0, f"{wid} collapsed: {button.size}"
            assert button.size.width > 0, f"{wid} collapsed: {button.size}"
            assert _hit(app, button) is button, f"{wid} is not hittable"
        # Self-describing: each door names its own key, the rule every
        # other confirm in this family follows.
        assert "enter" in str(dialog.query_one("#resume-confirm-yes").renderable)
        assert "esc" in str(dialog.query_one("#resume-confirm-no").renderable)

        body = dialog.query_one("#resume-confirm-body")
        assert body.size.height > 0, f"body collapsed: {body.size}"
        assert _hit(app, body) is body
        rendered = str(body.renderable)
        assert RESUMED_ID in rendered or OTHER_ID in rendered
        # The body STATES WHAT WILL HAPPEN rather than asking "are you
        # sure?" -- including the surprising part, that it opens a new tab.
        assert "NEW TAB" in rendered
        assert "are you sure" not in rendered.lower()


@pytest.mark.asyncio
async def test_an_unresumable_conversation_gets_one_door_and_a_reason(
    monkeypatch, tmp_path
):
    """The pre-v0.56.0 case, which for a while is most of the index. The
    dialog opens on the same key, says WHY in the body, and offers exactly
    one door -- a confirm with a "resume" button that cannot resume is
    worse than no button at all."""
    _seed(cwd=str(tmp_path))  # indexed, but the CLI never knew these ids
    app = await _app(monkeypatch, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _open_search(pilot, app, "warpcore")
        await pilot.press("enter")
        for _ in range(300):
            if isinstance(app.screen, ResumeConfirm):
                break
            await pilot.pause(0.02)
        dialog = app.screen
        assert isinstance(dialog, ResumeConfirm)
        await pilot.pause()
        assert dialog.resumable is False
        assert not dialog.query("#resume-confirm-yes")  # no door that lies
        close = dialog.query_one("#resume-confirm-no")
        assert close.size.height > 0 and _hit(app, close) is close
        body = str(dialog.query_one("#resume-confirm-body").renderable)
        assert "v0.56.0" in body and "searchable" in body


@pytest.mark.asyncio
async def test_declining_the_confirm_resumes_nothing(monkeypatch, tmp_path):
    """Esc is a real answer, not a dismissal that leaves work half-done."""
    _seed(cwd=str(tmp_path))
    _cli_history(RESUMED_ID)
    _cli_history(OTHER_ID)
    resumed: list = []
    app = await _app(monkeypatch, tmp_path, resumed=resumed)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        tabs_before = len(app.panes())
        await _open_search(pilot, app, "warpcore")
        await pilot.press("enter")
        for _ in range(300):
            if isinstance(app.screen, ResumeConfirm):
                break
            await pilot.pause(0.02)
        assert isinstance(app.screen, ResumeConfirm)

        await pilot.press("escape")
        for _ in range(100):
            if not isinstance(app.screen, ResumeConfirm):
                break
            await pilot.pause(0.02)
        await pilot.pause()
        assert resumed == []            # nothing spawned
        assert len(app.panes()) == tabs_before  # no tab appeared


@pytest.mark.asyncio
async def test_enter_on_a_hit_row_still_copies_the_excerpt(monkeypatch, tmp_path):
    """The behaviour the user explicitly asked to keep, pinned so the
    header repurposing cannot quietly take it with it. Enter on a SNIPPET
    replaces the /search line with that excerpt, exactly as before."""
    _seed(cwd=str(tmp_path))
    app = await _app(monkeypatch, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        popup = await _open_search(pilot, app, "warpcore")
        prompt = app.query_one("#prompt-input")
        await pilot.press("right")  # expand: Right still folds, Enter no longer does
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert popup.current_kind() == "hit"

        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(app.screen, ResumeConfirm)  # no dialog on a hit
        assert not prompt.value.startswith(SEARCH_PREFIX)
        assert "lore session" in prompt.value  # the excerpt's provenance line


# -- resuming for real -----------------------------------------------------


def _write_transcript(session_id: str, cwd: str, prompt: str) -> None:
    """A LORE transcript on disk, in the shape SessionEngine._persist
    writes -- what a resumed tab reads to put the prior turns back."""
    from doxa import transcript as transcript_mod

    path = transcript_mod.transcript_path(session_id, cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "user",
            "message": {"role": "user", "content": prompt},
            "sessionId": session_id,
        }) + "\n")
        fh.write(json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "the alignment held at 1.21 gigawatts"},
            ]},
            "sessionId": session_id,
        }) + "\n")


@pytest.mark.asyncio
async def test_a_resumed_session_shows_its_prior_conversation(
    monkeypatch, tmp_path
):
    """The model comes back holding the conversation. If the pane came up
    blank the user would be typing into a context they cannot see and
    cannot audit -- which for a tool whose premise is auditable memory is
    the wrong failure to ship. So the prior turns are re-rendered from the
    transcript on disk, through v0.32.0's own restore machinery."""
    work = tmp_path / "work"
    work.mkdir()
    _cli_history(RESUMED_ID)
    _write_transcript(RESUMED_ID, str(work), "align the warpcore, carefully")
    resumed: list = []
    app = await _app(monkeypatch, tmp_path, resumed=resumed)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        before = len(app.panes())
        note = await app.resume_session({
            "session_id": RESUMED_ID, "cwd": str(work), "title": "warpcore",
        })
        assert note is None  # nothing to explain: it just worked
        for _ in range(400):
            if len(app.panes()) > before:
                break
            await pilot.pause(0.02)
        assert resumed == [(str(work), RESUMED_ID)], (
            "the resume factory is asked for the SAME id, at the session's "
            "own cwd -- a resume keeps its id rather than forking one"
        )
        pane = app.panes()[-1]
        for _ in range(400):
            if any("warpcore, carefully" in b.prompt_text for b in pane.query(TurnBlock)):
                break
            await pilot.pause(0.02)
        prompts = [b.prompt_text for b in pane.query(TurnBlock)]
        assert any("align the warpcore, carefully" in p for p in prompts), prompts


@pytest.mark.asyncio
async def test_resume_opens_a_new_tab_and_leaves_this_one_alone(
    monkeypatch, tmp_path
):
    """A resumed conversation is a DIFFERENT conversation from the one the
    active pane holds. Taking the pane over would end or orphan a live
    session on a keystroke whose stated subject was some other session --
    and unlike a new tab, it would have no undo."""
    work = tmp_path / "work"
    work.mkdir()
    _cli_history(RESUMED_ID)
    _write_transcript(RESUMED_ID, str(work), "prior prompt")
    app = await _app(monkeypatch, tmp_path, resumed=[])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        original = app.panes()[0]
        original_engine = original.engine
        before = len(app.panes())
        await app.resume_session({
            "session_id": RESUMED_ID, "cwd": str(work), "title": "warpcore",
        })
        for _ in range(400):
            if len(app.panes()) > before:
                break
            await pilot.pause(0.02)
        assert len(app.panes()) == before + 1
        assert original in app.panes()
        assert original.engine is original_engine  # untouched, still running


@pytest.mark.asyncio
async def test_resuming_a_running_session_attaches_instead_of_forking(
    monkeypatch, tmp_path
):
    """A live conversation has one writer. Handing --resume to a second CLI
    while the first is still on it means two writers on one transcript and
    two daemons under one registry id -- so this does the thing the user
    actually wanted (attach) and SAYS it did, rather than forking
    silently."""
    work = tmp_path / "work"
    work.mkdir()
    _cli_history(RESUMED_ID)
    entry = peers_mod.PeerInfo(
        session_id=RESUMED_ID, pid=os.getpid(), socket_path="",
        cwd=str(work), repo_root=None, title="live",
        started_at="", heartbeat_at="", daemon_socket=str(tmp_path / "d.sock"),
    )
    monkeypatch.setattr(peers_mod, "read_registry", lambda *a, **k: [entry])
    monkeypatch.setattr(
        "doxa.client.EngineClient",
        lambda sock, **kw: FakeEngine([], cwd=str(work)),
    )
    resumed: list = []
    app = await _app(monkeypatch, tmp_path, resumed=resumed)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        before = len(app.panes())
        note = await app.resume_session({
            "session_id": RESUMED_ID, "cwd": str(work), "title": "live",
        })
        assert note is not None
        assert "still running" in note
        assert "attached to it in a new tab" in note
        assert resumed == [], "a running session must never be resumed/forked"
        for _ in range(300):
            if len(app.panes()) > before:
                break
            await pilot.pause(0.02)
        assert len(app.panes()) == before + 1  # attached, in its own tab


@pytest.mark.asyncio
async def test_a_running_in_process_session_is_refused_in_words(
    monkeypatch, tmp_path
):
    """No daemon socket means nothing to attach to -- and resuming it
    anyway is exactly the double-writer this branch exists to prevent. It
    refuses, and says which window owns the session instead."""
    work = tmp_path / "work"
    work.mkdir()
    _cli_history(RESUMED_ID)
    entry = peers_mod.PeerInfo(
        session_id=RESUMED_ID, pid=os.getpid(), socket_path="",
        cwd=str(work), repo_root=None, title="live",
        started_at="", heartbeat_at="", daemon_socket="",
    )
    monkeypatch.setattr(peers_mod, "read_registry", lambda *a, **k: [entry])
    resumed: list = []
    app = await _app(monkeypatch, tmp_path, resumed=resumed)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        before = len(app.panes())
        note = await app.resume_session({
            "session_id": RESUMED_ID, "cwd": str(work), "title": "live",
        })
        assert "not resumable while it runs" in (note or "")
        assert resumed == []
        assert len(app.panes()) == before


@pytest.mark.asyncio
async def test_resuming_an_unresumable_session_spawns_nothing(
    monkeypatch, tmp_path
):
    """A pre-v0.56.0 conversation reached through /resume by id, rather
    than through the dialog: same refusal, same words, no half-created
    tab."""
    work = tmp_path / "work"
    work.mkdir()
    resumed: list = []
    app = await _app(monkeypatch, tmp_path, resumed=resumed)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        before = len(app.panes())
        note = await app.resume_session({
            "session_id": OTHER_ID, "cwd": str(work), "title": "old one",
        })
        assert "cannot resume" in (note or "")
        assert resumed == []
        assert len(app.panes()) == before


# -- /resume, the command --------------------------------------------------


def test_resume_reaches_help_the_palette_and_autocomplete():
    """One registry row, every surface -- the discipline doxa/commands.py
    exists to enforce. A command reachable only by the key that opens it
    is a command nobody finds."""
    from doxa import commands as commands_mod
    from doxa.ui.labels import help_text

    assert "/resume" in commands_mod.interactive_names()
    row = commands_mod.lookup("/resume")
    assert row is not None and row.palette  # offered on Ctrl+P
    assert "/resume" in help_text()
    assert any(c.name == "/resume" for c in commands_mod.matches("/res"))


@pytest.mark.asyncio
async def test_bare_resume_offers_the_recent_conversations(monkeypatch, tmp_path):
    """Bare /resume lists what there is to resume, in the same ChipPicker
    every other list in this app uses -- and the rows carry the same age
    the /search popup dates a conversation with."""
    _seed(cwd=str(tmp_path))
    app = await _app(monkeypatch, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        pane = app.panes()[0]
        await pane._cmd_resume("")
        await pilot.pause()
        picker = app.query_one("#chip-picker")
        assert picker.is_open
        labels = "\n".join(label for _rid, label in picker._all_rows)
        assert "warpcore" in labels
        assert "7 msg" in labels


@pytest.mark.asyncio
async def test_resume_with_an_ambiguous_prefix_refuses_and_lists(
    monkeypatch, tmp_path
):
    """Resuming the WRONG conversation is not a mistake anyone notices
    quickly, so an ambiguous prefix is answered with the candidates rather
    than by taking the first."""
    _seed(cwd=str(tmp_path),
          session_ids=("aaaa1111-0000-0000-0000-000000000001",
                       "aaaa1111-0000-0000-0000-000000000002"))
    app = await _app(monkeypatch, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        pane = app.panes()[0]
        await pane._cmd_resume("aaaa1111")
        await pilot.pause()
        text = "\n".join(
            str(b.renderable) for b in pane.query("SystemBlock")
        )
        assert "matches 2 conversations" in text
        assert "give more of the id" in text


# -- restore: a tab that was open comes back CONTINUING, not read-only ----
#
# The reported gap, verbatim: "as long as a tab was open, when DOXA is
# started again, the tab should be resumed automatically, not via hotkey".
# v0.32.0 gave a restored-but-ended tab a read-only transcript and a dead
# end, and a daemon finalizing on its linger timer while the window is shut
# is the ORDINARY way a session ends -- so that dead end was the ordinary
# outcome of a restart. Restore meant *display*.


def _tab(session_id: str, cwd: str):
    from doxa.tabsets import TabRecord

    return TabRecord(session_id, None, cwd)


def test_an_ended_tab_that_can_be_resumed_comes_back_live(tmp_path):
    """No gesture, no key: the decision happens at restore, and the tab
    that was open comes back as a real session continuing its
    conversation."""
    from doxa.cli import ended_tab_spec

    work = tmp_path / "work"; work.mkdir()
    _cli_history(RESUMED_ID)
    calls: list = []
    spec = ended_tab_spec(
        _tab(RESUMED_ID, str(work)), str(tmp_path),
        lambda cwd, sid: calls.append((cwd, sid)),
    )
    assert spec.resume is True
    assert spec.archived is False
    assert spec.cwd == str(work)
    # The factory continues THIS conversation, at its own cwd -- a resume
    # keeps its id rather than forking one.
    spec.engine_factory()
    assert calls == [(str(work), RESUMED_ID)]


def test_an_unresumable_ended_tab_falls_back_to_read_only_and_says_why(tmp_path):
    """Requirement: a resume that cannot happen degrades to exactly
    v0.32.0's tab, never to an error or an empty pane -- plus a line
    saying why, because a read-only tab with no reason is
    indistinguishable from the feature not existing."""
    from doxa.cli import ended_tab_spec

    work = tmp_path / "work"; work.mkdir()
    spec = ended_tab_spec(  # no CLI history: a pre-v0.56.0 conversation
        _tab(OTHER_ID, str(work)), str(tmp_path), lambda cwd, sid: None,
    )
    assert spec.archived is True
    assert spec.resume is False
    assert spec.engine_factory is None       # nothing spawns
    assert "not resumed" in spec.resume_note
    assert "v0.56.0" in spec.resume_note     # names the reason, not just the state


def test_resume_restored_off_is_exactly_the_old_behaviour(tmp_path, monkeypatch):
    """Its own switch, and off means v0.32.0 byte for byte -- including
    NO reason line: the setting doing what it says is not a failure, and
    explaining it would be explaining the user's own choice back to them."""
    from doxa.cli import ended_tab_spec

    work = tmp_path / "work"; work.mkdir()
    _cli_history(RESUMED_ID)  # resumable in every other respect
    monkeypatch.setenv("DOXA_RESUME_RESTORED", "0")
    spec = ended_tab_spec(
        _tab(RESUMED_ID, str(work)), str(tmp_path), lambda cwd, sid: None,
    )
    assert spec.archived is True
    assert spec.resume is False
    assert spec.resume_note == ""


def test_a_running_session_is_never_resumed_by_restore(tmp_path, monkeypatch):
    """Two writers on one transcript is the thing this must never do, and
    restore is the path most likely to try: a saved tab whose daemon the
    registry still knows about."""
    from doxa.cli import ended_tab_spec

    work = tmp_path / "work"; work.mkdir()
    _cli_history(RESUMED_ID)
    entry = peers_mod.PeerInfo(
        session_id=RESUMED_ID, pid=os.getpid(), socket_path="",
        cwd=str(work), repo_root=None, title="live",
        started_at="", heartbeat_at="", daemon_socket="/tmp/x.sock",
    )
    monkeypatch.setattr(peers_mod, "read_registry", lambda *a, **k: [entry])
    spec = ended_tab_spec(
        _tab(RESUMED_ID, str(work)), str(tmp_path), lambda cwd, sid: None,
    )
    assert spec.resume is False and spec.archived is True
    assert "still RUNNING" in spec.resume_note


def test_the_restore_report_counts_resumed_separately():
    """"resumed" is a strictly bigger claim than "restored" and must not
    hide inside it: those tabs are live sessions continuing conversations
    that had ended."""
    from doxa.cli import _restore_report_text

    text = _restore_report_text(2, 0, 1, 3)
    assert "restored 2 tabs" in text
    assert "resumed 3 ended conversations" in text
    assert "1 read-only transcript" in text
    assert _restore_report_text(0, 0, 0, 1) == (
        "tab restore: resumed 1 ended conversation."
    )
    assert _restore_report_text(0, 0, 0, 0) is None


@pytest.mark.asyncio
async def test_a_resumed_restored_tab_shows_its_conversation_and_takes_a_prompt(
    monkeypatch, tmp_path
):
    """End to end at the app layer: a restore spec marked ``resume`` opens
    a real SessionPane (a prompt, not a read-only archive) whose scrollback
    is the conversation it is continuing -- mounted ONCE, from the same
    transcript file v0.32.0's restore reads."""
    from doxa.app import RestoreTabSpec
    from doxa.ui.transcript import ArchivedSessionTab

    work = tmp_path / "work"; work.mkdir()
    _cli_history(RESUMED_ID)
    _write_transcript(RESUMED_ID, str(work), "keep going with the warpcore")
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr(
        "doxa.app.SessionEngine", lambda **kw: FakeEngine([], cwd=kw.get("cwd", ""))
    )
    app = DoxaApp(
        cwd=str(tmp_path),
        restore_tabs=[RestoreTabSpec(
            session_id=RESUMED_ID,
            engine_factory=lambda: FakeEngine([], cwd=str(work)),
            cwd=str(work),
            resume=True,
        )],
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert not list(app.query(ArchivedSessionTab)), "read-only was the FALLBACK"
        pane = next(
            p for p in app.panes()
            if p.id == f"restore-{RESUMED_ID}-leaf"
        )
        for _ in range(400):
            if any(
                "keep going with the warpcore" in b.prompt_text
                for b in pane.query(TurnBlock)
            ):
                break
            await pilot.pause(0.02)
        prompts = [b.prompt_text for b in pane.query(TurnBlock)]
        assert any("keep going with the warpcore" in p for p in prompts), prompts
        # Mounted once, not doubled -- the archived tab's own render and
        # this one must not both run over the same file.
        assert sum(
            1 for p in prompts if "keep going with the warpcore" in p
        ) == 1
        # And it is a real session: there is a prompt to type into.
        assert pane.query_one("#prompt-input") is not None


@pytest.mark.asyncio
async def test_an_unresumable_restored_tab_still_explains_itself_on_screen(
    monkeypatch, tmp_path
):
    """The fallback, rendered: read-only exactly as v0.32.0 drew it, and
    the reason in the block the user is already reading."""
    from doxa.app import RestoreTabSpec
    from doxa.ui.transcript import ArchivedSessionTab

    work = tmp_path / "work"; work.mkdir()
    _write_transcript(OTHER_ID, str(work), "an older conversation")
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr(
        "doxa.app.SessionEngine", lambda **kw: FakeEngine([], cwd=kw.get("cwd", ""))
    )
    app = DoxaApp(
        cwd=str(tmp_path),
        restore_tabs=[RestoreTabSpec(
            session_id=OTHER_ID, cwd=str(work), archived=True,
            resume_note="not resumed — the claude CLI has no history under "
                        "this session id",
        )],
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        tab = app.query_one(ArchivedSessionTab)
        for _ in range(300):
            blocks = list(tab.query("SystemBlock"))
            if blocks and "not resumed" in str(blocks[0].renderable):
                break
            await pilot.pause(0.02)
        head = str(list(tab.query("SystemBlock"))[0].renderable)
        assert "read-only" in head              # v0.32.0's own words, kept
        assert "not resumed" in head            # and why, which is new
