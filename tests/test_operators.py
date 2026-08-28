# SPDX-License-Identifier: AGPL-3.0-only
"""Registry-discipline tests for doxa.operators: explicit closure, valid
hand-written JSON Schemas, the three composing projection filters
(allowed / include_write / configuredness), the read-only guarantee via a
recording fake store, and lore_remember's stage-never-write contract.
lore_core state lives in the throwaway LORE_ROOT conftest.py points at."""

from __future__ import annotations

import contextlib
import json

import jsonschema
import pytest

import doxa._lore_bootstrap  # noqa: F401 -- sys.path shim: makes lore_core importable
import lore_core
from lore_core import store as lore_store
from lore_core.beliefs import (
    belief_insert,
    edge_insert,
    record_outcome,
    support_factor,
)
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
        "lore_belief_search", "lore_belief_show", "lore_belief_neighbours",
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
    assert names == {
        "lore_belief_show", "lore_belief_neighbours", "lore_memory_list", "lore_remember",
    }
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
    assert out_show["edges"] == []
    out_nb = ops.OPERATORS["lore_belief_neighbours"].fn(belief_id=bid)
    assert out_nb["mode"] == "neighbourhood"
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


# --------------------------------------------------------------------------
# lore_belief_show edges + lore_belief_neighbours (v0.84.0, the belief graph)
#
# Fixtures go through lore_core's own write path -- belief_insert /
# edge_insert / record_outcome -- never a hand-rolled INSERT into
# belief_edges or belief_outcomes, so a schema change under either table
# breaks these tests honestly instead of silently drifting from the real
# shape.
# --------------------------------------------------------------------------

@pytest.fixture
def _belief_graph_cleanup():
    """Put the shared belief store back exactly as it was found -- same
    snapshot-and-restore discipline as tests/test_beliefs_picker.py's
    lore_store_cleanup (conftest.py points LORE_ROOT at ONE throwaway
    directory for the whole session, so a stray belief here is a stray
    belief `tests/test_consult.py`'s bm25-floor assertions see too),
    extended to belief_edges/belief_edge_assertions: their PRIMARY KEY is
    (src, dst, rel), not an autoincrement id, so they are cleaned by
    endpoint membership rather than by row id."""
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
            conn.execute("DELETE FROM belief_outcomes WHERE belief_id = ?", (bid,))
            conn.execute("DELETE FROM belief_edges WHERE src = ? OR dst = ?", (bid, bid))
            conn.execute(
                "DELETE FROM belief_edge_assertions WHERE src = ? OR dst = ?", (bid, bid))
            with contextlib.suppress(Exception):
                conn.execute("DELETE FROM belief_fts WHERE belief_id = ?", (bid,))
        conn.commit()


def _belief(slug, claim, session_id, confidence=0.6):
    conn = lore_store.db_connect()
    bid, _created = belief_insert(conn, "project:" + slug, claim, confidence,
                                  session_id, slug, "seeded")
    conn.commit()
    return bid


def _make_steer(bid):
    """Push a belief past the >=3 outcome-ledger rows cmd_consult's STEER
    bar requires, via the real ledger write path."""
    conn = lore_store.db_connect()
    for _ in range(3):
        record_outcome(conn, bid, "confirmed", "test")
    conn.commit()


def test_belief_show_carries_its_edges_present_and_absent(tmp_path, _belief_graph_cleanup):
    slug = project_slug(str(tmp_path))
    a = _belief(slug, "edge-fixture belief A depends on belief B", "sess-edge-1")
    b = _belief(slug, "edge-fixture belief B is the dependency", "sess-edge-2")
    conn = lore_store.db_connect()
    assert edge_insert(conn, a, b, "depends_on", "derived", session_id="sess-edge-1") is True
    conn.commit()

    out_a = ops.OPERATORS["lore_belief_show"].fn(belief_id=a)
    assert len(out_a["edges"]) == 1
    edge = out_a["edges"][0]
    assert edge == {
        "direction": "out", "verb": "depends_on", "belief_id": b,
        "claim": "edge-fixture belief B is the dependency", "status": "active",
        "source": "derived", "support": 1,
    }

    # The other endpoint sees the same edge from the "in" side.
    out_b = ops.OPERATORS["lore_belief_show"].fn(belief_id=b)
    assert len(out_b["edges"]) == 1
    assert out_b["edges"][0]["direction"] == "in"
    assert out_b["edges"][0]["belief_id"] == a

    # A belief nobody has related to anything gets an empty block, not a
    # missing key.
    lonely = _belief(slug, "edge-fixture belief C has no relations at all", "sess-edge-3")
    out_c = ops.OPERATORS["lore_belief_show"].fn(belief_id=lonely)
    assert out_c["edges"] == []


def test_belief_neighbours_one_and_two_hops_with_product_confidence(tmp_path, _belief_graph_cleanup):
    slug = project_slug(str(tmp_path))
    s = _belief(slug, "chain-fixture seed belief S", "sess-chain-s")
    a = _belief(slug, "chain-fixture belief A one hop from S", "sess-chain-a")
    b = _belief(slug, "chain-fixture belief B two hops from S", "sess-chain-b")
    conn = lore_store.db_connect()
    assert edge_insert(conn, s, a, "depends_on", "derived", session_id="sess-chain-e1")
    assert edge_insert(conn, a, b, "depends_on", "derived", session_id="sess-chain-e2")
    conn.commit()

    one_hop = ops.OPERATORS["lore_belief_neighbours"].fn(belief_id=s, hops=1)
    assert one_hop["mode"] == "neighbourhood"
    ids_1 = {n["id"] for n in one_hop["neighbours"]}
    assert ids_1 == {a}
    a_row = one_hop["neighbours"][0]
    assert a_row["hop_distance"] == 1
    assert a_row["via_relation"] == "depends_on"
    assert a_row["relation_projected"] is False
    w1 = support_factor(1)  # single-session support on the S->A edge
    assert a_row["path_confidence"] == pytest.approx(round(w1, 4))

    two_hop = ops.OPERATORS["lore_belief_neighbours"].fn(belief_id=s, hops=2)
    ids_2 = {n["id"] for n in two_hop["neighbours"]}
    assert ids_2 == {a, b}
    b_row = next(n for n in two_hop["neighbours"] if n["id"] == b)
    assert b_row["hop_distance"] == 2
    # PATH CONFIDENCE IS THE PRODUCT OVER HOPS -- two single-session hops
    # multiply, they do not average and they are not just the weaker one.
    expected_product = support_factor(1) * support_factor(1)
    assert b_row["path_confidence"] == pytest.approx(round(expected_product, 4))
    assert b_row["path_confidence"] < a_row["path_confidence"]

    # The same product, reached through the explicit path mode (to_id).
    path_out = ops.OPERATORS["lore_belief_neighbours"].fn(belief_id=s, to_id=b)
    assert path_out["mode"] == "path"
    assert path_out["hop_count"] == 2
    assert path_out["confidence"] == pytest.approx(round(expected_product, 4))
    assert [h["verb"] for h in path_out["path"]] == ["depends_on", "depends_on"]
    assert [belief["id"] for belief in path_out["beliefs"]] == [s, a, b]


def test_belief_neighbours_cap_truncates_visibly(tmp_path, _belief_graph_cleanup):
    slug = project_slug(str(tmp_path))
    s = _belief(slug, "cap-fixture seed belief", "sess-cap-s")
    targets = [
        _belief(slug, f"cap-fixture direct neighbour {i}", f"sess-cap-{i}")
        for i in range(3)
    ]
    conn = lore_store.db_connect()
    for i, t in enumerate(targets):
        assert edge_insert(conn, s, t, "depends_on", "derived", session_id=f"sess-cap-e{i}")
    conn.commit()

    out = ops.OPERATORS["lore_belief_neighbours"].fn(belief_id=s, hops=1, limit=2)
    assert out["count"] == 2
    assert out["reachable_total"] == 3
    assert out["truncated"] is True
    assert "TRUNCATED" in out["note"]

    # A limit above the tool's hard cap is clamped, not honored verbatim.
    from doxa.events import BELIEF_NEIGHBOUR_LIMIT

    clamped = ops.OPERATORS["lore_belief_neighbours"].fn(belief_id=s, hops=1, limit=999)
    assert clamped["limit"] == BELIEF_NEIGHBOUR_LIMIT


def test_belief_neighbours_cite_only_survives_a_steer_seed(tmp_path, _belief_graph_cleanup):
    """A belief reached by traversal is CITE ONLY unless it earned STEER on
    its own -- neither direction launders authority across the edge."""
    slug = project_slug(str(tmp_path))
    steer_belief = _belief(slug, "steer-fixture outcome-calibrated belief", "sess-steer")
    cite_belief = _belief(slug, "steer-fixture uncalibrated neighbour belief", "sess-cite")
    _make_steer(steer_belief)
    conn = lore_store.db_connect()
    # adjacency() is a DIRECTED graph for non-symmetric relations (adj maps
    # src -> dst, matching `lore graph`'s own model) -- an edge asserted one
    # way is not traversable the other way without a second, explicit edge.
    # Two distinct verbs both ways is what makes each direction below an
    # honest, independent traversal rather than an artifact of reversal.
    assert edge_insert(conn, steer_belief, cite_belief, "explains", "derived",
                       session_id="sess-steer-edge")
    assert edge_insert(conn, cite_belief, steer_belief, "specializes", "derived",
                       session_id="sess-cite-edge")
    conn.commit()

    from_steer = ops.OPERATORS["lore_belief_neighbours"].fn(belief_id=steer_belief, hops=1)
    assert from_steer["seed"]["citation_status"] == "steer"
    neighbour = from_steer["neighbours"][0]
    assert neighbour["id"] == cite_belief
    assert neighbour["citation_status"] == "cite_only"

    # And the reverse: reached FROM the cite-only belief, the steer belief
    # keeps its own earned status -- it is not downgraded by the direction
    # of the query either.
    from_cite = ops.OPERATORS["lore_belief_neighbours"].fn(belief_id=cite_belief, hops=1)
    assert from_cite["seed"]["citation_status"] == "cite_only"
    reached = from_cite["neighbours"][0]
    assert reached["id"] == steer_belief
    assert reached["citation_status"] == "steer"


def test_belief_neighbours_labels_co_derived_as_projected(tmp_path, _belief_graph_cleanup):
    slug = project_slug(str(tmp_path))
    conn = lore_store.db_connect()
    # Two beliefs derived in the SAME small session -- co_derived is
    # PROJECTED from belief_evidence at read time, never a belief_edges row.
    a, _ = belief_insert(conn, "project:" + slug, "co-derived-fixture belief A",
                         0.6, "sess-coderived", slug, "seeded")
    b, _ = belief_insert(conn, "project:" + slug, "co-derived-fixture belief B",
                         0.6, "sess-coderived", slug, "seeded")
    conn.commit()

    out = ops.OPERATORS["lore_belief_neighbours"].fn(belief_id=a, hops=1)
    row = next((n for n in out["neighbours"] if n["id"] == b), None)
    assert row is not None, "co_derived projection did not surface the sibling belief"
    assert row["via_relation"] == "co_derived"
    assert row["relation_projected"] is True


def test_belief_neighbours_errors_on_bad_or_inactive_ids(tmp_path, _belief_graph_cleanup):
    slug = project_slug(str(tmp_path))
    active = _belief(slug, "error-fixture active belief", "sess-err-1")
    dormant = _belief(slug, "error-fixture dormant belief", "sess-err-2")
    conn = lore_store.db_connect()
    conn.execute("UPDATE beliefs SET status = 'dormant' WHERE id = ?", (dormant,))
    conn.commit()

    missing = ops.OPERATORS["lore_belief_neighbours"].fn(belief_id=999_999_999)
    assert "error" in missing

    not_active = ops.OPERATORS["lore_belief_neighbours"].fn(belief_id=dormant)
    assert "error" in not_active

    bad_target = ops.OPERATORS["lore_belief_neighbours"].fn(belief_id=active, to_id=999_999_999)
    assert "error" in bad_target
