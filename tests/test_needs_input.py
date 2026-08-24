"""Queue item 5 -- interactive permission (can_use_tool): engine-level unit
tests, no subprocess, no network, no real SDK client (SessionEngine's
_build_options() is exercised directly, and its can_use_tool callback is
invoked exactly the way claude_agent_sdk's own control-request dispatch
would -- see _internal/query.py in the installed package).

Covers: an ordinary tool call defaults to allow (the zero-regression
assertion -- nothing that flows through silently today gains a new
prompt), AskUserQuestion surfaces as a needs_input event and the answer
becomes updated_input["answers"], a declined question denies gracefully,
a permission-worthy call (title/display_name/decision_reason populated)
surfaces too and an allow/deny answer round-trips, and answer_needs_input
is idempotent/no-op on an unknown or already-resolved id.
"""

from __future__ import annotations

import asyncio

import pytest

from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from doxa.engine import SessionEngine


def _ctx(**kwargs) -> ToolPermissionContext:
    return ToolPermissionContext(**kwargs)


@pytest.mark.asyncio
async def test_ordinary_tool_call_defaults_to_allow_and_never_queues_a_prompt(tmp_path):
    """Zero-regression assertion: a tool call the CLI's own permission
    system has no opinion on (nothing in context populated -- the common
    case, and the ONLY case that existed before this feature) is a bare
    allow, and nothing lands on the out-of-band queue for it."""
    engine = SessionEngine(cwd=str(tmp_path))
    engine._build_options()  # asserts can_use_tool onto the options
    result = await engine._on_can_use_tool("Read", {"file_path": "x.py"}, _ctx())
    assert isinstance(result, PermissionResultAllow)
    assert engine._peer_queue.empty()


@pytest.mark.asyncio
async def test_can_use_tool_is_wired_into_build_options(tmp_path):
    engine = SessionEngine(cwd=str(tmp_path))
    options = engine._build_options()
    assert options.can_use_tool.__func__ is SessionEngine._on_can_use_tool
    assert options.can_use_tool.__self__ is engine


@pytest.mark.asyncio
async def test_ask_user_question_surfaces_as_needs_input_and_answer_round_trips(tmp_path):
    engine = SessionEngine(cwd=str(tmp_path))
    engine._build_options()
    tool_input = {
        "questions": [{
            "question": "which color?", "header": "Pick one",
            "options": [
                {"label": "Red", "description": "warm"},
                {"label": "Blue", "description": "cool"},
            ],
            "multiSelect": False,
        }],
    }
    task = asyncio.ensure_future(
        engine._on_can_use_tool("AskUserQuestion", tool_input, _ctx())
    )
    ev = await engine._peer_queue.get()
    assert ev.type == "needs_input"
    assert ev.data["kind"] == "ask_user"
    assert ev.data["tool_name"] == "AskUserQuestion"
    assert ev.data["questions"][0]["question"] == "which color?"
    req_id = ev.data["id"]

    ok = await engine.answer_needs_input(req_id, {"answers": {"which color?": "Red"}})
    assert ok is True

    result = await task
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input["answers"] == {"which color?": "Red"}
    # The original questions payload rides along untouched -- the tool's
    # own executor still needs it to render/validate.
    assert result.updated_input["questions"] == tool_input["questions"]

    resolved = await engine._peer_queue.get()
    assert resolved.type == "needs_input_resolved"
    assert resolved.data["id"] == req_id
    assert req_id not in engine._pending_needs_input


@pytest.mark.asyncio
async def test_ask_user_question_declined_is_a_graceful_deny(tmp_path):
    engine = SessionEngine(cwd=str(tmp_path))
    engine._build_options()
    task = asyncio.ensure_future(
        engine._on_can_use_tool(
            "AskUserQuestion",
            {"questions": [{"question": "q", "options": [{"label": "A"}]}]},
            _ctx(),
        )
    )
    ev = await engine._peer_queue.get()
    await engine.answer_needs_input(ev.data["id"], {"declined": True})
    result = await task
    assert isinstance(result, PermissionResultDeny)
    await engine._peer_queue.get()  # needs_input_resolved


@pytest.mark.asyncio
async def test_permission_worthy_call_surfaces_and_allow_denies_round_trip(tmp_path):
    """A call the CLI populated title/display_name for -- the signal this
    engine reads as "would have shown a real prompt" -- surfaces as a
    needs_input(kind=permission) event; both an allow and a deny answer
    round-trip to the matching PermissionResult."""
    engine = SessionEngine(cwd=str(tmp_path))
    engine._build_options()

    for decision, expect_cls in (("allow", PermissionResultAllow), ("deny", PermissionResultDeny)):
        task = asyncio.ensure_future(
            engine._on_can_use_tool(
                "Bash", {"command": "rm -rf /tmp/x"},
                _ctx(title="Claude wants to run rm -rf /tmp/x", display_name="Run command"),
            )
        )
        ev = await engine._peer_queue.get()
        assert ev.type == "needs_input"
        assert ev.data["kind"] == "permission"
        assert ev.data["tool_name"] == "Bash"
        assert "rm -rf" in ev.data["input_summary"]
        assert ev.data["title"] == "Claude wants to run rm -rf /tmp/x"

        await engine.answer_needs_input(ev.data["id"], {"decision": decision})
        result = await task
        assert isinstance(result, expect_cls)
        await engine._peer_queue.get()  # needs_input_resolved


@pytest.mark.asyncio
async def test_permission_request_summary_is_scrubbed(tmp_path):
    engine = SessionEngine(cwd=str(tmp_path))
    engine._build_options()
    secret_input = {"command": "curl -H 'Authorization: Bearer AKIAABCDEFGHIJKLMNOP'"}
    task = asyncio.ensure_future(
        engine._on_can_use_tool(
            "Bash", secret_input, _ctx(title="Claude wants to run a command"),
        )
    )
    ev = await engine._peer_queue.get()
    assert "AKIAABCDEFGHIJKLMNOP" not in ev.data["input_summary"]
    await engine.answer_needs_input(ev.data["id"], {"decision": "deny"})
    await task
    await engine._peer_queue.get()


@pytest.mark.asyncio
async def test_answer_needs_input_on_unknown_or_resolved_id_is_a_no_op(tmp_path):
    engine = SessionEngine(cwd=str(tmp_path))
    engine._build_options()
    assert await engine.answer_needs_input("does-not-exist", {}) is False

    task = asyncio.ensure_future(
        engine._on_can_use_tool(
            "Bash", {"command": "ls"}, _ctx(title="Claude wants to run ls"),
        )
    )
    ev = await engine._peer_queue.get()
    req_id = ev.data["id"]
    assert await engine.answer_needs_input(req_id, {"decision": "allow"}) is True
    # Second answer to the SAME id, after it already resolved: no-op.
    assert await engine.answer_needs_input(req_id, {"decision": "deny"}) is False
    await task
    await engine._peer_queue.get()


@pytest.mark.asyncio
async def test_decision_reason_alone_is_enough_to_surface_a_permission_request(tmp_path):
    """A PreToolUse hook that returned permissionDecision "ask" with a
    reason forwards it here (per ToolPermissionContext.decision_reason's
    own docstring) -- doxa's own gate never does this today (deny or no
    opinion only), but the callback honors the contract regardless of
    which hook populated it."""
    engine = SessionEngine(cwd=str(tmp_path))
    engine._build_options()
    task = asyncio.ensure_future(
        engine._on_can_use_tool(
            "WebFetch", {"url": "https://example.com"},
            _ctx(decision_reason="outside the allowed domain list"),
        )
    )
    ev = await engine._peer_queue.get()
    assert ev.type == "needs_input"
    await engine.answer_needs_input(ev.data["id"], {"decision": "deny"})
    await task
    await engine._peer_queue.get()
