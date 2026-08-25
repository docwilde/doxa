# SPDX-License-Identifier: AGPL-3.0-only
"""Containment tests for doxa.gate.ToolGate: total graceful degradation
(execute never raises), the conservative two-strikes classifier + disable,
allowed-set denial at both the hook and the executor, and the
OperatorContext sidecar contract (stripped from model args always, injected
only for declaring operators)."""

from __future__ import annotations

import dataclasses

from doxa import operators as ops
from doxa.gate import OperatorContext, ToolGate, is_hard_failure


def _ctx(tmp_path, belief_store=None) -> OperatorContext:
    return OperatorContext(
        session_id="sess-gate", cwd=str(tmp_path), repo_root=str(tmp_path),
        belief_store=belief_store,
    )


def _broken_store():
    raise RuntimeError("belief db unavailable")


# --------------------------------------------------------------------------
# Total graceful degradation
# --------------------------------------------------------------------------

def test_unknown_tool_is_an_ordinary_error_result(tmp_path):
    gate = ToolGate(op_ctx=_ctx(tmp_path))
    out = gate.execute("mcp__doxa__no_such_tool", {})
    assert "unknown tool" in out["error"]
    assert "lore_belief_search" in out["error"]  # the available list helps recovery


def test_bad_args_become_a_recoverable_result_and_never_a_strike(tmp_path):
    gate = ToolGate(op_ctx=_ctx(tmp_path))
    for _ in range(3):
        out = gate.execute("lore_belief_show", {"belief_id": 1, "bogus": 2})
        assert out["error"].startswith("bad arguments for lore_belief_show:")
    assert gate.disabled_tools() == []


def test_backend_exception_becomes_the_name_failed_shape(tmp_path):
    gate = ToolGate(op_ctx=_ctx(tmp_path, belief_store=_broken_store))
    out = gate.execute("lore_belief_search", {"query": "anything"})
    assert out["error"].startswith("lore_belief_search failed: RuntimeError:")


# --------------------------------------------------------------------------
# Two-strikes containment
# --------------------------------------------------------------------------

def test_second_hard_failure_disables_and_fires_event_once(tmp_path):
    events: list[tuple[str, str]] = []
    gate = ToolGate(op_ctx=_ctx(tmp_path, belief_store=_broken_store),
                    on_disable=lambda n, r: events.append((n, r)))

    first = gate.execute("mcp__doxa__lore_belief_search", {"query": "x"})
    assert "failed:" in first["error"]
    assert gate.disabled_tools() == []  # one failure alone never disables

    gate.execute("mcp__doxa__lore_belief_search", {"query": "y"})
    assert gate.disabled_tools() == ["lore_belief_search"]
    assert len(events) == 1 and events[0][0] == "lore_belief_search"

    # From now on: executor refuses, hook denies -- and no more events.
    third = gate.execute("mcp__doxa__lore_belief_search", {"query": "z"})
    assert "disabled" in third["error"]
    hook = gate.pre_tool_use({"tool_name": "mcp__doxa__lore_belief_search"})
    assert hook["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert len(events) == 1


def test_soft_errors_never_count_toward_disable(tmp_path):
    gate = ToolGate(op_ctx=_ctx(tmp_path))
    for _ in range(3):
        out = gate.execute("lore_belief_search", {"query": "   "})
        assert out["error"] == "lore_belief_search: empty query"  # single colon
        miss = gate.execute("lore_belief_show", {"belief_id": 987654321})
        assert miss["error"].startswith("lore_belief_show: no belief")
    assert gate.disabled_tools() == []


def test_classifier_is_conservative():
    assert is_hard_failure("t", {"error": "t failed: RuntimeError: x"}) is True
    assert is_hard_failure("t", {"error": "t is not configured on this host"}) is True
    # everything else is the model's mistake or a valid empty answer:
    assert is_hard_failure("t", {"error": "bad arguments for t: unexpected"}) is False
    assert is_hard_failure("t", {"error": "t: empty query"}) is False
    assert is_hard_failure("t", {"error": "other_tool failed: x"}) is False
    assert is_hard_failure("t", {"beliefs": [], "count": 0}) is False
    assert is_hard_failure("t", "not a dict") is False


# --------------------------------------------------------------------------
# Allowed-set policy (hook + executor, defence in depth)
# --------------------------------------------------------------------------

def test_no_policy_passes_everything_through(tmp_path):
    gate = ToolGate(op_ctx=_ctx(tmp_path))
    assert gate.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": "ls"}}) == {}
    assert gate.pre_tool_use({"tool_name": "mcp__doxa__lore_memory_list"}) == {}


def test_allowed_set_denies_builtins_and_native_alike(tmp_path):
    gate = ToolGate(allowed={"lore_memory_list", "Read"}, op_ctx=_ctx(tmp_path))
    assert gate.pre_tool_use({"tool_name": "Read"}) == {}
    assert gate.pre_tool_use({"tool_name": "mcp__doxa__lore_memory_list"}) == {}

    denied = gate.pre_tool_use({"tool_name": "Bash"})
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    # An unknown built-in is denied every time but never enters the
    # two-strikes ledger -- only known operators do.
    gate.pre_tool_use({"tool_name": "Bash"})
    assert gate.disabled_tools() == []

    # A known operator outside the set: executor refuses gracefully, and a
    # tool the caller can NEVER use is the strongest stop signal -- its
    # second refusal disables it.
    out = gate.execute("lore_belief_search", {"query": "x"})
    assert out["error"] == "tool not permitted: 'lore_belief_search'"
    gate.pre_tool_use({"tool_name": "mcp__doxa__lore_belief_search"})
    assert gate.disabled_tools() == ["lore_belief_search"]


# --------------------------------------------------------------------------
# OperatorContext sidecar
# --------------------------------------------------------------------------

def test_op_ctx_is_stripped_from_model_args_and_injected_for_declaring(tmp_path, monkeypatch):
    seen: dict = {}

    def spy(**kwargs):
        seen.clear()
        seen.update(kwargs)
        return {"ok": True}

    real = ops.OPERATORS["lore_belief_search"]
    monkeypatch.setitem(ops.OPERATORS, "lore_belief_search",
                        dataclasses.replace(real, fn=spy))
    ctx = _ctx(tmp_path)
    gate = ToolGate(op_ctx=ctx)

    # Model-supplied op_ctx is stripped ALWAYS; the gate's own sidecar is
    # what the declaring operator receives.
    out = gate.execute("lore_belief_search", {"query": "q", "op_ctx": "evil"})
    assert out == {"ok": True}
    assert seen["query"] == "q"
    assert seen["op_ctx"] is ctx

    # A non-declaring operator never sees an op_ctx at all -- even with the
    # sidecar present on the gate and a model trying to smuggle one in.
    real_show = ops.OPERATORS["lore_belief_show"]
    monkeypatch.setitem(ops.OPERATORS, "lore_belief_show",
                        dataclasses.replace(real_show, fn=spy))
    out2 = gate.execute("lore_belief_show", {"belief_id": 1, "op_ctx": "evil"})
    assert out2 == {"ok": True}
    assert seen == {"belief_id": 1}


def test_op_ctx_absent_means_operators_run_without_it(tmp_path, monkeypatch):
    seen: dict = {}

    def spy(**kwargs):
        seen.update(kwargs)
        return {"ok": True}

    real = ops.OPERATORS["lore_belief_search"]
    monkeypatch.setitem(ops.OPERATORS, "lore_belief_search",
                        dataclasses.replace(real, fn=spy))
    gate = ToolGate(op_ctx=None)
    gate.execute("lore_belief_search", {"query": "q"})
    assert "op_ctx" not in seen
