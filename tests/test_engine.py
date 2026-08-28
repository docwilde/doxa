# SPDX-License-Identifier: AGPL-3.0-only
"""Engine event-stream unit tests: a fake claude_agent_sdk client, no
subprocess, no network. Covers: typed events yielded in order, the LORE
snapshot landing in system_prompt at start(), the secret-scrub choke point
applied before anything touches disk, and finalize() running exactly once.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

import pytest

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from doxa import claude_plugins as claude_plugins_mod
from doxa import cli_isolation as cli_isolation_mod
from doxa import worktrees as worktrees_mod
from doxa.engine import SessionEngine
from tests.fakes import factory_with_script


def _repo(tmp_path, name="repo", branch="trunk"):
    """Same minimal real-git fixture tests/test_worktrees.py and tests/
    test_branch_command.py each keep their own copy of -- the [SESSION
    WORKTREE] block tests below need a REAL sidecar (doxa.worktrees.
    read_meta reads an actual file worktrees.create wrote), so a fake
    engine/fake git would test nothing."""
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", branch, str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "f.txt").write_text("one", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "one"], check=True)
    return repo


def _script_one_turn_with_tool_call() -> list:
    return [
        StreamEvent(
            uuid="stream-1", session_id="s",
            event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello"}},
        ),
        AssistantMessage(content=[TextBlock(text="Hello")], model="claude-haiku-4-5"),
        AssistantMessage(
            content=[ToolUseBlock(id="tool-1", name="calculator_add", input={"a": 1, "b": 2})],
            model="claude-haiku-4-5",
        ),
        UserMessage(content=[ToolResultBlock(tool_use_id="tool-1", content="3", is_error=False)]),
        ResultMessage(
            subtype="success", duration_ms=42, duration_api_ms=40, is_error=False,
            num_turns=1, session_id="s", total_cost_usd=0.001,
        ),
    ]


@pytest.mark.asyncio
async def test_start_captures_account_and_init_names_the_model(tmp_path):
    """Identity surface: start() captures the CLI's connect-time account
    block via get_server_info (only the fields it actually reports), and the
    first turn's init SystemMessage names the ACTUAL session model when the
    engine rode the CLI default (model=None)."""
    from claude_agent_sdk import SystemMessage

    script = [
        SystemMessage(subtype="init", data={"model": "claude-haiku-4-5"}),
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s", total_cost_usd=0.0,
        ),
    ]
    factory, created = factory_with_script(script, server_info={
        "account": {
            "email": "doc@example.org", "organization": "Doc's Org",
            "subscriptionType": "Claude Max", "apiProvider": "firstParty",
        },
        "output_style": "default",
    })
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()
    assert engine.account["email"] == "doc@example.org"
    assert engine.account["subscriptionType"] == "Claude Max"
    assert engine.server_info["output_style"] == "default"
    assert engine.lore_root  # LORE store path, for the identity block
    assert engine.model is None  # CLI default until init says otherwise

    events = [ev async for ev in engine.send("hi")]
    assert events[-1].type == "turn_done"
    assert engine.model == "claude-haiku-4-5"
    await engine.finalize()


def test_start_captures_effort_asserted_at_connect(monkeypatch, tmp_path):
    """Item T's status-bar chip needs THIS session's actual connect-time
    effort, not a re-read of live config (which /effort can change after
    connect without touching the running session -- see its own docstring).
    _build_options() is what asserts effort onto ClaudeAgentOptions; self.
    effort must capture the SAME value it asserted, once, right there."""
    monkeypatch.setenv("DOXA_EFFORT", "xhigh")
    eng = SessionEngine(cwd=str(tmp_path))
    assert eng.effort is None  # nothing asserted before connect
    options = eng._build_options()
    assert options.effort == "xhigh"
    assert eng.effort == "xhigh"

    monkeypatch.delenv("DOXA_EFFORT", raising=False)
    import doxa.config as config_mod

    config_mod.invalidate()
    unset = SessionEngine(cwd=str(tmp_path))
    unset._build_options()
    assert unset.effort is None  # CLI default -- the chip hides itself


@pytest.mark.asyncio
async def test_list_beliefs_returns_active_belief_bodies(tmp_path):
    """Item 3's beliefs picker: list_beliefs() is a SEPARATE call from
    belief_count() (the status bar's own cheap COUNT(*)) -- this pins its
    shape (id/subject/claim/confidence per active belief) against a real
    lore_core store, no engine start() required (it never touches
    self._client)."""
    from lore_core import beliefs as beliefs_mod
    from lore_core import store as lore_store

    conn = lore_store.db_connect()
    beliefs_mod.belief_insert(
        conn, "user", "prefers terse commits", 0.9, None, None, None,
    )
    conn.commit()

    engine = SessionEngine(cwd=str(tmp_path))
    result = await engine.list_beliefs()
    match = next((b for b in result if b["claim"] == "prefers terse commits"), None)
    assert match is not None
    assert match["subject"] == "user"
    assert 0.0 <= match["confidence"] <= 1.0
    assert isinstance(match["id"], int)


@pytest.mark.asyncio
async def test_start_without_server_info_leaves_identity_empty(tmp_path):
    """No initialize payload (fakes, older SDKs, API-key non-streaming):
    the identity surface stays empty -- never guessed."""
    factory, _created = factory_with_script([])
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()
    assert engine.account == {}
    assert engine.server_info is None
    await engine.finalize()


@pytest.mark.asyncio
async def test_start_injects_lore_snapshot_into_system_prompt(tmp_path):
    factory, created = factory_with_script([])
    engine = SessionEngine(cwd=str(tmp_path), model="claude-haiku-4-5", client_factory=factory)

    event = await engine.start()

    assert event.type == "session_started"
    assert created[0].entered is True
    system_prompt = created[0].options.system_prompt
    assert system_prompt["type"] == "preset"
    assert system_prompt["preset"] == "claude_code"
    # PHASE0 redesign item 2: snapshot injection is the system_prompt
    # append, not a SessionStart hook -- this is the assertion that matters.
    assert "LORE SNAPSHOT" in system_prompt["append"]
    assert "hooks" in vars(created[0].options) or created[0].options.hooks
    assert set(created[0].options.hooks) == {"UserPromptSubmit", "PreCompact", "PreToolUse"}


# -- [SESSION WORKTREE]: worktree-awareness in the system prompt ----------
#
# The gap this closes: a session running in its own doxa/<id> worktree
# (v0.17.0+) was never told, and would push its private branch, try to
# switch to main, or burn a turn on git archaeology working out its own
# base. Every test below asserts on the COMPOSED options a real client
# factory receives -- system_prompt["append"] and engine.
# worktree_notice_chars -- never on _session_worktree_block's internals.


@pytest.mark.asyncio
async def test_session_worktree_block_carries_the_real_branch_base_and_root(
    tmp_path,
):
    """Inside a real doxa worktree, the appended block names the ACTUAL
    branch/base/root the sidecar recorded -- not a guess, not a template
    with placeholders."""
    repo = _repo(tmp_path)
    path = worktrees_mod.create(str(repo), "e2eid01")
    assert path is not None
    meta = worktrees_mod.read_meta(path)

    factory, created = factory_with_script([])
    engine = SessionEngine(cwd=path, client_factory=factory)
    await engine.start()

    append = created[0].options.system_prompt["append"]
    assert "[SESSION WORKTREE]" in append
    assert meta["branch"] in append
    assert meta["base_ref"] in append
    assert meta["main_root"] in append
    assert "removed with no trace" in append  # the finalize rule, verbatim
    assert "do not push this branch" in append.lower()
    assert "do not switch off it" in append.lower()
    # LORE snapshot is still there too -- this is a SECOND appendix, not a
    # replacement.
    assert "[LORE SNAPSHOT]" in append
    assert append.index("[LORE SNAPSHOT]") < append.index("[SESSION WORKTREE]")

    assert engine.worktree_notice_chars is not None
    assert engine.worktree_notice_chars > 0
    await engine.finalize()


@pytest.mark.asyncio
async def test_non_worktree_session_gets_a_byte_identical_prompt(tmp_path):
    """The hide-at-zero pin (item 2 of the ask): a session with no doxa
    worktree sidecar at all -- no git repo here, worktrees never touched
    this cwd -- must produce EXACTLY the append string a pre-feature
    session produced: "[LORE SNAPSHOT]\\n" + the snapshot, nothing more."""
    from lore_core import context as lore_context_mod

    plain = tmp_path / "not-a-worktree"
    plain.mkdir()

    factory, created = factory_with_script([])
    engine = SessionEngine(cwd=str(plain), client_factory=factory)
    await engine.start()

    expected = "[LORE SNAPSHOT]\n" + lore_context_mod.build_context(str(plain))
    assert created[0].options.system_prompt["append"] == expected
    assert "[SESSION WORKTREE]" not in created[0].options.system_prompt["append"]
    assert engine.worktree_notice_chars is None
    await engine.finalize()


@pytest.mark.asyncio
async def test_worktree_disabled_by_setting_gets_no_block(tmp_path, monkeypatch):
    """Same hide-at-zero, the 'disabled by setting' path named explicitly:
    DOXA_WORKTREE=0 means worktrees.create never wrote a sidecar for this
    cwd in the first place, so read_meta finds nothing and the prompt is
    unaffected -- even though this cwd IS a real repo."""
    monkeypatch.setenv("DOXA_WORKTREE", "0")
    repo = _repo(tmp_path)
    assert worktrees_mod.create(str(repo), "offid001") is None  # setting off

    factory, created = factory_with_script([])
    engine = SessionEngine(cwd=str(repo), client_factory=factory)
    await engine.start()

    assert "[SESSION WORKTREE]" not in created[0].options.system_prompt["append"]
    assert engine.worktree_notice_chars is None
    await engine.finalize()


@pytest.mark.asyncio
async def test_missing_sidecar_gets_no_block(tmp_path):
    """A cwd that LOOKS like a doxa worktree dir name but has no sidecar
    at all (crashed create, or the .meta file was lost) -- read_meta
    returns None, and a wrong claim about the base is worse than silence,
    so nothing is appended."""
    repo = _repo(tmp_path)
    path = worktrees_mod.create(str(repo), "nosidecr")
    assert path is not None
    worktrees_mod.meta_file_path(path).unlink()  # sidecar gone, worktree stays

    factory, created = factory_with_script([])
    engine = SessionEngine(cwd=path, client_factory=factory)
    await engine.start()

    assert "[SESSION WORKTREE]" not in created[0].options.system_prompt["append"]
    assert engine.worktree_notice_chars is None
    await engine.finalize()


@pytest.mark.asyncio
async def test_corrupt_sidecar_gets_no_block(tmp_path):
    """Same refusal for a sidecar that exists but is not valid JSON --
    read_meta's own contract (OSError/ValueError -> None), exercised
    through the engine rather than assumed."""
    repo = _repo(tmp_path)
    path = worktrees_mod.create(str(repo), "corrupti")
    assert path is not None
    worktrees_mod.meta_file_path(path).write_text("{not json", encoding="utf-8")

    factory, created = factory_with_script([])
    engine = SessionEngine(cwd=path, client_factory=factory)
    await engine.start()

    assert "[SESSION WORKTREE]" not in created[0].options.system_prompt["append"]
    assert engine.worktree_notice_chars is None
    await engine.finalize()


@pytest.mark.asyncio
async def test_incomplete_sidecar_gets_no_block(tmp_path):
    """A sidecar that parses as JSON but is missing a field the block
    needs (branch/base_ref/main_root) -- still refused rather than
    printing a half-true sentence with a blank where the base should be."""
    repo = _repo(tmp_path)
    path = worktrees_mod.create(str(repo), "incomplt")
    assert path is not None
    meta = worktrees_mod.read_meta(path)
    del meta["base_ref"]
    worktrees_mod.meta_file_path(path).write_text(
        json.dumps(meta), encoding="utf-8",
    )

    factory, created = factory_with_script([])
    engine = SessionEngine(cwd=path, client_factory=factory)
    await engine.start()

    assert "[SESSION WORKTREE]" not in created[0].options.system_prompt["append"]
    await engine.finalize()


def test_session_worktree_block_reflects_the_current_base_at_reconnect(tmp_path):
    """v0.56.0's resume re-runs _build_options on reconnect (SessionEngine.
    start() is exactly the same call for a fresh connect and a resume) --
    this pins that the block is rebuilt from the sidecar's CURRENT state
    each time, not cached from the first call. Exercised directly against
    _build_options (sync, no client needed) so the test can rewrite the
    sidecar BETWEEN two calls and assert the second one sees the change,
    the way a real switch_base rewrite (doxa.worktrees.update_base) would
    land before a later reconnect."""
    repo = _repo(tmp_path)
    path = worktrees_mod.create(str(repo), "reconnid")
    assert path is not None

    engine = SessionEngine(cwd=path)
    first = engine._build_options()
    first_append = first.system_prompt["append"]
    assert "trunk" in first_append

    subprocess.run(["git", "-C", str(repo), "checkout", "-qb", "other"], check=True)
    assert worktrees_mod.update_base(path, "other") is True

    second = engine._build_options()
    second_append = second.system_prompt["append"]
    assert "other" in second_append
    assert second_append != first_append


# -- [BELIEF GRAPH]: telling a session lore_belief_neighbours exists ------
#
# The gap this closes: the LORE snapshot's own retrieval ladder (verbatim
# inside [LORE SNAPSHOT]) lists five steps and none of them is "the store
# also carries typed relations between beliefs; traverse them" -- a session
# has the tool (advertised by its schema, like any operator) with no
# instruction that reaching for it is ever the right move. HIDE AT ZERO:
# the block only appears once the store has an ASSERTED relation (one of
# the deriver's five verbs) between two ACTIVE beliefs -- the exact view
# lore_belief_neighbours itself traverses. A store with only structural
# `supersedes` edges or projected `co_derived` pairs (no deriver has ever
# asserted a real relation) gets nothing: pointing an agent at pure
# coincidence would teach it to read co-occurrence as structure.

def _assert_active(slug, claim, session_id):
    from lore_core import store as lore_store
    from lore_core.beliefs import belief_insert

    conn = lore_store.db_connect()
    bid, _created = belief_insert(
        conn, f"project:{slug}", claim, 0.6, session_id, slug, "seeded")
    conn.commit()
    return bid


@pytest.fixture
def _belief_graph_engine_cleanup():
    """Same snapshot-and-restore discipline as tests/test_operators.py's
    _belief_graph_cleanup -- conftest.py's LORE_ROOT is one throwaway
    store for the whole session, and these tests write real beliefs and
    real belief_edges rows into it."""
    from lore_core import store as lore_store

    conn = lore_store.db_connect()
    before = {row[0] for row in conn.execute("SELECT id FROM beliefs")}
    try:
        yield
    finally:
        conn = lore_store.db_connect()
        added = [row[0] for row in conn.execute("SELECT id FROM beliefs")
                 if row[0] not in before]
        for bid in added:
            conn.execute("DELETE FROM beliefs WHERE id = ?", (bid,))
            conn.execute("DELETE FROM belief_evidence WHERE belief_id = ?", (bid,))
            conn.execute("DELETE FROM belief_edges WHERE src = ? OR dst = ?", (bid, bid))
            conn.execute(
                "DELETE FROM belief_edge_assertions WHERE src = ? OR dst = ?", (bid, bid))
            import contextlib
            with contextlib.suppress(Exception):
                conn.execute("DELETE FROM belief_fts WHERE belief_id = ?", (bid,))
        conn.commit()


@pytest.mark.asyncio
async def test_belief_graph_block_appears_once_an_asserted_relation_exists(
    tmp_path, _belief_graph_engine_cleanup,
):
    """The presence case: one asserted relation (depends_on) between two
    active beliefs is enough to earn the block, and it names the real
    tool by name."""
    from lore_core import store as lore_store
    from lore_core.beliefs import edge_insert
    from lore_core.config import project_slug

    slug = project_slug(str(tmp_path))
    a = _assert_active(slug, "belief-graph-fixture A depends on B", "sess-bg-a")
    b = _assert_active(slug, "belief-graph-fixture B is the dependency", "sess-bg-b")
    conn = lore_store.db_connect()
    assert edge_insert(conn, a, b, "depends_on", "derived", session_id="sess-bg-edge") is True
    conn.commit()

    factory, created = factory_with_script([])
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()

    append = created[0].options.system_prompt["append"]
    assert "[BELIEF GRAPH]" in append
    assert "lore_belief_neighbours" in append
    assert "CITE-only" in append
    assert "[LORE SNAPSHOT]" in append
    assert append.index("[LORE SNAPSHOT]") < append.index("[BELIEF GRAPH]")

    assert engine.graph_awareness_chars is not None
    assert engine.graph_awareness_chars > 0
    await engine.finalize()


@pytest.mark.asyncio
async def test_belief_graph_block_absent_with_no_asserted_relation_at_all(
    tmp_path, _belief_graph_engine_cleanup,
):
    """Hide-at-zero: a belief with no relation of any kind gets nothing --
    the prompt is unaffected."""
    from lore_core.config import project_slug

    slug = project_slug(str(tmp_path))
    bid = _assert_active(
        slug, "belief-graph-fixture lonely belief, no relation at all", "sess-bg-lonely")
    assert bid  # seeded, just needs to exist

    factory, created = factory_with_script([])
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()

    assert "[BELIEF GRAPH]" not in created[0].options.system_prompt["append"]
    assert engine.graph_awareness_chars is None
    await engine.finalize()


@pytest.mark.asyncio
async def test_belief_graph_block_absent_with_only_structural_and_projected_edges(
    tmp_path, _belief_graph_engine_cleanup,
):
    """The deliberate exclusions, both at once: a `supersedes` edge
    (structural) and a `co_derived` pair (projected, same small session)
    exist, but NEITHER is an asserted relation between two ACTIVE beliefs
    -- supersedes touches a now-superseded (non-active) belief by
    definition, and co_derived is coincidence, not an assertion. No block."""
    from lore_core import store as lore_store
    from lore_core.beliefs import belief_insert, belief_supersede
    from lore_core.config import project_slug

    slug = project_slug(str(tmp_path))
    conn = lore_store.db_connect()

    # supersedes: old -> new, backfilled the way belief_supersede does it.
    old, _created = belief_insert(
        conn, f"project:{slug}", "belief-graph-fixture superseded claim",
        0.6, "sess-bg-old", slug, "seeded")
    new, _created = belief_insert(
        conn, f"project:{slug}", "belief-graph-fixture superseding claim",
        0.6, "sess-bg-new", slug, "seeded")
    conn.commit()
    belief_supersede(conn, old, new, "superseded by a newer claim")
    conn.commit()

    # co_derived: two beliefs from the SAME small session, projected at
    # read time, never a belief_edges row.
    c1, _created = belief_insert(
        conn, f"project:{slug}", "belief-graph-fixture co-derived claim one",
        0.6, "sess-bg-coderived", slug, "seeded")
    c2, _created = belief_insert(
        conn, f"project:{slug}", "belief-graph-fixture co-derived claim two",
        0.6, "sess-bg-coderived", slug, "seeded")
    conn.commit()

    factory, created = factory_with_script([])
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()

    assert "[BELIEF GRAPH]" not in created[0].options.system_prompt["append"]
    assert engine.graph_awareness_chars is None
    await engine.finalize()
    # old/new/c1/c2 cleanup: handled by _belief_graph_engine_cleanup's own
    # before/after id diff -- belief_supersede changes old's status but
    # leaves its row in `beliefs`, so it (and new, c1, c2) are all caught
    # by the generic sweep same as a plain insert would be.


@pytest.mark.asyncio
async def test_start_isolates_the_spawned_cli_from_the_real_claude_config(tmp_path):
    """Item AA: every spawned CLI gets its OWN CLAUDE_CONFIG_DIR (never
    DOXA's own process env / the operator's real ~/.claude, where the LORE
    plugin and every other installed plugin live) via
    ClaudeAgentOptions.env, plus LORE_SKIP=1 belt-and-braces."""
    factory, created = factory_with_script([])
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)

    await engine.start()

    env = created[0].options.env
    assert env["CLAUDE_CONFIG_DIR"] == str(cli_isolation_mod.cli_config_dir())
    assert env["CLAUDE_CONFIG_DIR"] != os.environ.get("CLAUDE_CONFIG_DIR")
    assert env["LORE_SKIP"] == "1"
    # The mechanism actually provisioned the directory, not just named it.
    settings_path = cli_isolation_mod.cli_config_dir() / cli_isolation_mod.SETTINGS_NAME
    assert settings_path.exists()
    await engine.finalize()


@pytest.mark.asyncio
async def test_start_adopts_no_plugins_by_default(tmp_path):
    """docs/plans/plugins.md: adoption is opt-in, default OFF -- a session
    started with nothing configured gets ClaudeAgentOptions.plugins == [],
    byte-identical to before this feature existed."""
    factory, created = factory_with_script([])
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)

    await engine.start()

    assert created[0].options.plugins == []
    await engine.finalize()


@pytest.mark.asyncio
async def test_start_wires_adopted_plugins_through_to_the_sdk_options(tmp_path, monkeypatch):
    """The other half: when doxa.claude_plugins.adopt() has something to
    say, _build_options passes it straight through -- this is the ONE
    place ClaudeAgentOptions.plugins gets set, so proving the wiring here
    means every session, not just this test's fake, gets it."""
    sentinel = [{"type": "local", "path": str(tmp_path / "staged" / "caveman@caveman")}]
    monkeypatch.setattr(claude_plugins_mod, "adopt", lambda *a, **k: sentinel)
    factory, created = factory_with_script([])
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)

    await engine.start()

    assert created[0].options.plugins == sentinel
    await engine.finalize()


@pytest.mark.asyncio
async def test_start_retries_once_with_forced_credential_resync_on_connect_failure(tmp_path, monkeypatch):
    """The one retry item AA calls for: a connect failure gets ONE forced
    credential resync + a fresh client, not a retry loop -- and if there is
    nothing to resync (no source credentials at all), the original failure
    is what the caller sees."""
    attempts: list[Any] = []

    class _FlakyOnceClient:
        def __init__(self, options: Any) -> None:
            self.options = options
            attempts.append(self)

        async def __aenter__(self):
            if len(attempts) == 1:
                raise RuntimeError("simulated auth failure")
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def get_server_info(self):
            return None

    monkeypatch.setattr(
        cli_isolation_mod, "sync_credentials",
        lambda force=False: True if force else False,
    )
    engine = SessionEngine(cwd=str(tmp_path), client_factory=_FlakyOnceClient)

    event = await engine.start()

    assert event.type == "session_started"
    assert len(attempts) == 2  # the failed attempt, then one retry


@pytest.mark.asyncio
async def test_start_reraises_when_nothing_to_resync(tmp_path, monkeypatch):
    class _AlwaysFailsClient:
        def __init__(self, options: Any) -> None:
            pass

        async def __aenter__(self):
            raise RuntimeError("simulated auth failure")

        async def __aexit__(self, *exc: Any) -> bool:
            return False

    monkeypatch.setattr(cli_isolation_mod, "sync_credentials", lambda force=False: False)
    engine = SessionEngine(cwd=str(tmp_path), client_factory=_AlwaysFailsClient)

    with pytest.raises(RuntimeError, match="simulated auth failure"):
        await engine.start()


@pytest.mark.asyncio
async def test_lore_snapshot_is_scoped_per_project_not_shared_across_tabs(tmp_path):
    """Item AA.3: a tab in one repo and a tab in another must get DIFFERENT
    project memory -- exercising the real lore_core (conftest.py already
    points LORE_ROOT/LORE_PROJECTS_DIR at a throwaway dir), not a fake, so
    this proves the actual cwd -> project_slug -> MEMORY.md path
    doxa.engine._build_options rides via lore_context.build_context(self.cwd)."""
    import lore_core
    from lore_core.config import project_slug

    project_a = tmp_path / "repo-a"
    project_b = tmp_path / "repo-b"
    project_a.mkdir()
    project_b.mkdir()

    def _write_memory(cwd, text):
        slug = project_slug(str(cwd))
        memory_file = lore_core.ROOT / "projects" / slug / "MEMORY.md"
        memory_file.parent.mkdir(parents=True, exist_ok=True)
        memory_file.write_text(f"- {text}\n", encoding="utf-8")

    _write_memory(project_a, "REPO-A-ONLY-MARKER")
    _write_memory(project_b, "REPO-B-ONLY-MARKER")

    factory_a, created_a = factory_with_script([])
    engine_a = SessionEngine(cwd=str(project_a), client_factory=factory_a)
    await engine_a.start()

    factory_b, created_b = factory_with_script([])
    engine_b = SessionEngine(cwd=str(project_b), client_factory=factory_b)
    await engine_b.start()

    snapshot_a = created_a[0].options.system_prompt["append"]
    snapshot_b = created_b[0].options.system_prompt["append"]

    assert "REPO-A-ONLY-MARKER" in snapshot_a
    assert "REPO-B-ONLY-MARKER" not in snapshot_a
    assert "REPO-B-ONLY-MARKER" in snapshot_b
    assert "REPO-A-ONLY-MARKER" not in snapshot_b


@pytest.mark.asyncio
async def test_send_yields_typed_events_in_order(tmp_path):
    factory, created = factory_with_script(
        _script_one_turn_with_tool_call(), ctx_usage={"percentage": 12.5}
    )
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()

    events = [ev async for ev in engine.send("what is 1+2?")]

    assert [e.type for e in events] == [
        "turn_started", "text_delta", "tool_call", "tool_result", "turn_done",
    ]
    assert created[0].queried == [("what is 1+2?", engine.session_id)]

    tool_call = next(e for e in events if e.type == "tool_call")
    assert tool_call.data["name"] == "calculator_add"
    assert tool_call.data["input"] == {"a": 1, "b": 2}

    tool_result = next(e for e in events if e.type == "tool_result")
    assert tool_result.data["result_summary"] == "3"
    assert tool_result.data["is_error"] is False
    assert tool_result.data["duration_ms"] is not None

    turn_done = next(e for e in events if e.type == "turn_done")
    assert turn_done.data["cost_usd"] == pytest.approx(0.001)
    assert turn_done.data["ctx_percentage"] == pytest.approx(12.5)
    assert engine.total_cost_usd == pytest.approx(0.001)


@pytest.mark.asyncio
async def test_secret_scrub_applied_before_persistence(tmp_path):
    factory, created = factory_with_script([
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s", total_cost_usd=0.0,
        ),
    ])
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()

    secret_prompt = "my key is AKIAABCDEFGHIJKLMNOP, do not leak it"
    async for _ in engine.send(secret_prompt):
        pass

    transcript = engine.transcript_path.read_text(encoding="utf-8")
    assert "AKIAABCDEFGHIJKLMNOP" not in transcript
    assert "[REDACTED:aws]" in transcript


@pytest.mark.asyncio
async def test_secret_scrub_applied_to_tool_input_and_result(tmp_path):
    script = [
        AssistantMessage(
            content=[ToolUseBlock(
                id="tool-1", name="Bash",
                input={"command": "curl -H 'Authorization: Bearer AKIAABCDEFGHIJKLMNOP' https://x"},
            )],
            model="claude-haiku-4-5",
        ),
        UserMessage(content=[ToolResultBlock(
            tool_use_id="tool-1", content="token AKIAABCDEFGHIJKLMNOP accepted", is_error=False,
        )]),
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s", total_cost_usd=0.0,
        ),
    ]
    factory, created = factory_with_script(script)
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()

    events = [ev async for ev in engine.send("run the curl")]

    tool_call = next(e for e in events if e.type == "tool_call")
    assert "AKIAABCDEFGHIJKLMNOP" not in tool_call.data["input"]["command"]

    tool_result = next(e for e in events if e.type == "tool_result")
    assert "AKIAABCDEFGHIJKLMNOP" not in tool_result.data["result_summary"]

    transcript = engine.transcript_path.read_text(encoding="utf-8")
    assert "AKIAABCDEFGHIJKLMNOP" not in transcript


@pytest.mark.asyncio
async def test_finalize_runs_once_and_disconnects_client(tmp_path):
    factory, created = factory_with_script([])
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()

    first = await engine.finalize()
    assert first.type == "session_done"
    assert "already_finalized" not in first.data
    assert created[0].exited is True

    second = await engine.finalize()
    assert second.data.get("already_finalized") is True
