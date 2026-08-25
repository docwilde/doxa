"""Registry-discipline tests for doxa.operators: explicit closure, valid
hand-written JSON Schemas, the three composing projection filters
(allowed / include_write / configuredness), the read-only guarantee via a
recording fake store, and lore_remember's stage-never-write contract.
lore_core state lives in the throwaway LORE_ROOT conftest.py points at."""

from __future__ import annotations

import json

import jsonschema
import pytest

import doxa._lore_bootstrap  # noqa: F401 -- sys.path shim: makes lore_core importable
import lore_core
from lore_core import store as lore_store
from lore_core.beliefs import belief_insert
from lore_core.config import project_slug
from lore_core.memory import memory_add, memory_path, read_entries
from lore_core.pending import load_pending

from doxa import operators as ops
from doxa.gate import OperatorContext


@pytest.fixture(autouse=True)
def _no_staged_leak():
    """Leave the shared pending spool as it was found.

    conftest.py points LORE_ROOT at ONE throwaway directory for the whole
    session, and `lore_remember` below stages real proposals into it. That
    was invisible until v0.57.0 gave staged proposals a status-bar chip:
    every leaked proposal then widened the status bar in every LATER test,
    pushing the last two clickable chips past the click offsets
    tests/test_status_chips.py computes -- two failures in a module that
    passes cleanly on its own, three hundred tests away from the cause.

    Same discipline `_seed_big_belief_store` and `lore_store_cleanup`
    already follow: a test that really writes into the shared store puts it
    back."""
    pdir = lore_core.ROOT / "pending"
    before = {p for p in pdir.glob("*.json")} if pdir.exists() else set()
    try:
        yield
    finally:
        if pdir.exists():
            for path in pdir.glob("*.json"):
                if path not in before:
                    path.unlink(missing_ok=True)


def _ctx(tmp_path, belief_store=None, session_id="sess-test") -> OperatorContext:
    return OperatorContext(
        session_id=session_id, cwd=str(tmp_path), repo_root=str(tmp_path),
        belief_store=belief_store,
    )


# --------------------------------------------------------------------------
# Registry closure + schema validity
# --------------------------------------------------------------------------

def test_registry_has_exactly_the_read_operators():
    # Explicit literal set (harness closure-test style): adding an operator
    # is a deliberate act that must touch this test, never an import side
    # effect that slips a tool into the model's hands unreviewed.
    assert set(ops.OPERATORS) == {
        "lore_belief_search", "lore_belief_show",
        "lore_memory_list", "lore_session_search",
    }
    assert all(op.read_only for op in ops.OPERATORS.values())


def test_write_operators_are_separate_and_excluded_by_default():
    """lore_remember is the ONE write-capable tool: never in OPERATORS,
    never in the default projection -- it joins only on an explicit
    include_write=True, and even then it only stages a pending proposal."""
    assert set(ops.WRITE_OPERATORS) == {"lore_remember"}
    assert ops.WRITE_OPERATORS["lore_remember"].read_only is False
    executor = lambda name, args: {}  # noqa: E731
    default_names = {t.name for t in ops.to_sdk_tools(executor)}
    assert "lore_remember" not in default_names
    gated_names = {t.name for t in ops.to_sdk_tools(executor, include_write=True)}
    assert "lore_remember" in gated_names


def test_every_parameters_dict_is_a_valid_json_schema_object():
    for name, op in {**ops.OPERATORS, **ops.WRITE_OPERATORS}.items():
        params = op.parameters
        jsonschema.Draft202012Validator.check_schema(params)
        assert params["type"] == "object", name
        assert params["additionalProperties"] is False, name
        assert isinstance(params["properties"], dict) and params["properties"], name
        for req in params.get("required", []):
            assert req in params["properties"], f"{name}: required {req!r} not in properties"
        assert op.cost in ("low", "medium", "high"), name


def test_projection_filters_compose_and_descriptions_carry_cost():
    executor = lambda name, args: {}  # noqa: E731
    allowed = {"lore_memory_list", "lore_remember"}
    tools = ops.to_sdk_tools(executor, allowed=allowed, include_write=True)
    assert {t.name for t in tools} == {"lore_memory_list", "lore_remember"}
    by_name = {t.name: t for t in tools}
    assert by_name["lore_memory_list"].description.endswith("[cost: low]")
    assert "[write: staged for review]" in by_name["lore_remember"].description


def test_unconfigured_operators_never_appear():
    executor = lambda name, args: {}  # noqa: E731
    # No belief_store seam wired: the DB-backed operators are invisible;
    # a tool the model can see but never successfully call just burns a step.
    names = {t.name for t in ops.to_sdk_tools(
        executor, include_write=True, ctx={"lore_root": "/x"})}
    assert names == {"lore_belief_show", "lore_memory_list", "lore_remember"}
    # ctx=None means "don't gate on configuredness", never "nothing is
    # configured" -- every registered name appears.
    names_none = {t.name for t in ops.to_sdk_tools(executor, include_write=True, ctx=None)}
    assert names_none == set(ops.OPERATORS) | set(ops.WRITE_OPERATORS)


@pytest.mark.asyncio
async def test_projected_handler_wraps_executor_result_as_mcp_content():
    tools = ops.to_sdk_tools(lambda name, args: {"error": f"{name} failed: nope"})
    out = await tools[0].handler({"query": "q"})
    assert out["is_error"] is True
    assert "failed: nope" in out["content"][0]["text"]

    ok_tools = ops.to_sdk_tools(lambda name, args: {"beliefs": [], "count": 0})
    out_ok = await ok_tools[0].handler({"query": "q"})
    assert out_ok["is_error"] is False
    assert json.loads(out_ok["content"][0]["text"]) == {"beliefs": [], "count": 0}


# --------------------------------------------------------------------------
# Read-only guarantee -- recording fake store (harness FakeGraph idea)
# --------------------------------------------------------------------------

class RecordingConnection:
    """Proxies a real lore_core connection, recording every SQL statement
    so the test can scan for write patterns."""

    def __init__(self, conn, log: list[str]) -> None:
        self._conn = conn
        self._log = log

    def execute(self, sql, params=()):
        self._log.append(str(sql))
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_read_operators_never_write(tmp_path, monkeypatch):
    slug = project_slug(str(tmp_path))
    # Seed real lore state (the test's own writes, before recording starts).
    conn = lore_store.db_connect()
    belief_insert(conn, "user", "doxa quokka telemetry belief for readonly test",
                  0.7, "sess-a", slug, "seeded")
    conn.execute(
        "INSERT INTO msg(session_id, project, ts, role, content) VALUES(?,?,?,?,?)",
        ("sess-a", slug, "2026-08-23T00:00:00Z", "user",
         "doxa quokka telemetry session line"))
    conn.commit()
    memory_add("project", slug, "doxa quokka readonly memory entry")

    memory_before = {
        sc: read_entries(memory_path(sc, slug)) for sc in ("user", "project")
    }
    pending_before = sorted(p.name for p in (lore_core.ROOT / "pending").glob("*.json")) \
        if (lore_core.ROOT / "pending").exists() else []

    log: list[str] = []
    real_db_connect = lore_store.db_connect

    def recording_store():
        return RecordingConnection(real_db_connect(), log)

    # lore_belief_show takes no op_ctx (deliberately -- the non-declaring
    # case); route its module-level db_connect through the recorder too.
    monkeypatch.setattr(lore_store, "db_connect", recording_store)

    ctx = _ctx(tmp_path, belief_store=recording_store)
    out_search = ops.OPERATORS["lore_belief_search"].fn(query="quokka telemetry", op_ctx=ctx)
    assert out_search["count"] >= 1
    bid = out_search["beliefs"][0]["id"]
    out_show = ops.OPERATORS["lore_belief_show"].fn(belief_id=bid)
    assert out_show["belief"]["id"] == bid
    out_mem = ops.OPERATORS["lore_memory_list"].fn(op_ctx=ctx)
    assert "doxa quokka readonly memory entry" in out_mem["project"]["entries"]
    out_sess = ops.OPERATORS["lore_session_search"].fn(query="quokka telemetry", op_ctx=ctx)
    assert out_sess["count"] >= 1 and out_sess["scope"] == "project"

    # The write-pattern scan: every statement a read operator issued is a
    # SELECT -- no INSERT/UPDATE/DELETE/CREATE/ALTER/DROP, ever.
    assert log, "recording store was never used"
    for sql in log:
        assert sql.lstrip().upper().startswith("SELECT"), f"non-SELECT from a read operator: {sql}"

    memory_after = {sc: read_entries(memory_path(sc, slug)) for sc in ("user", "project")}
    assert memory_after == memory_before
    pending_after = sorted(p.name for p in (lore_core.ROOT / "pending").glob("*.json")) \
        if (lore_core.ROOT / "pending").exists() else []
    assert pending_after == pending_before


# --------------------------------------------------------------------------
# lore_remember -- stages a pending proposal, never writes memory
# --------------------------------------------------------------------------

def test_remember_stages_pending_proposal_and_never_writes_memory(tmp_path):
    slug = project_slug(str(tmp_path))
    memory_before = {sc: read_entries(memory_path(sc, slug)) for sc in ("user", "project")}

    ctx = _ctx(tmp_path, session_id="sess-remember")
    out = ops.WRITE_OPERATORS["lore_remember"].fn(
        text="doxa staged xylograph fact for the remember test",
        scope="project", op_ctx=ctx,
    )
    assert out["staged"], out
    assert "approv" in out["note"]  # the review gate is named, not implied

    # The proposal exists in ROOT/pending with the deriver-compatible shape.
    staged = dict(load_pending())
    item = staged[out["staged"]]
    assert item["kind"] == "memory" and item["action"] == "add"
    assert item["scope"] == "project" and item["project"] == slug
    assert item["session_id"] == "sess-remember"
    assert item["derived_by"] == "doxa-tool"
    assert item["text"] == "doxa staged xylograph fact for the remember test"

    # Curated memory itself is byte-identical: staging is not writing.
    memory_after = {sc: read_entries(memory_path(sc, slug)) for sc in ("user", "project")}
    assert memory_after == memory_before

    # Idempotent: the same text again stages nothing.
    again = ops.WRITE_OPERATORS["lore_remember"].fn(
        text="doxa staged xylograph fact for the remember test",
        scope="project", op_ctx=ctx,
    )
    assert again["staged"] is None


def test_remember_scrubs_secrets_and_validates(tmp_path):
    ctx = _ctx(tmp_path)
    out = ops.WRITE_OPERATORS["lore_remember"].fn(
        text="deploy key is AKIAABCDEFGHIJKLMNOP for the scrub test",
        scope="user", op_ctx=ctx,
    )
    assert out["staged"]
    item = dict(load_pending())[out["staged"]]
    assert "AKIAABCDEFGHIJKLMNOP" not in item["text"]
    assert "[REDACTED:aws]" in item["text"]

    bad_scope = ops.WRITE_OPERATORS["lore_remember"].fn(
        text="x", scope="global", op_ctx=ctx)
    assert bad_scope["error"].startswith("lore_remember:")
    empty = ops.WRITE_OPERATORS["lore_remember"].fn(text="   ", op_ctx=ctx)
    assert empty["error"] == "lore_remember: empty text"


# --------------------------------------------------------------------------
# Session search widens project -> all
# --------------------------------------------------------------------------

def test_session_search_widens_to_all_projects(tmp_path):
    conn = lore_store.db_connect()
    conn.execute(
        "INSERT INTO msg(session_id, project, ts, role, content) VALUES(?,?,?,?,?)",
        ("sess-other", "-some-other-project", "2026-08-23T00:00:00Z", "assistant",
         "zeugma flotilla only exists elsewhere"))
    conn.commit()
    out = ops.OPERATORS["lore_session_search"].fn(
        query="zeugma flotilla", op_ctx=_ctx(tmp_path))
    assert out["scope"] == "all" and out["count"] >= 1
    assert out["hits"][0]["session_id"] == "sess-other"


def test_registry_name_strips_only_the_doxa_prefix():
    assert ops.registry_name("mcp__doxa__lore_belief_search") == "lore_belief_search"
    assert ops.registry_name("Bash") == "Bash"
    assert ops.registry_name("mcp__github__search_code") == "mcp__github__search_code"
