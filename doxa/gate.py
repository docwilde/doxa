# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.gate -- the containment layer at the PreToolUse choke point.

docs/phase0-findings.md redesign item 3: tool allowlisting is session-scoped in
ClaudeAgentOptions, not swappable per call -- so "this session may only use
these tools" is enforced HERE, per tool call, at the one choke point the SDK
guarantees (the PreToolUse hook) plus the executor every DOXA-native tool
already routes through. The same discipline as the DeepSeek-harness
reference (finch/serving/ask.py's _run_operator + two-strikes tracker +
executor permission gate), ported to the SDK's hook calling convention.

Four contracts, all session-scoped state on one ToolGate:

1. Allowed-set check (hook side, ALL tools): when a policy set is given, a
   tool call whose name is not in it -- SDK built-in, plugin/MCP tool, or
   DOXA-native -- gets a graceful hook DENY the model sees as an ordinary
   refused result, never an exception. No policy set (None) means no
   filtering: Phase 1 has exactly one stage and allows everything; the choke
   point exists so a future stage model plugs in without changing the
   calling convention.
2. Total graceful degradation (executor side, DOXA-native only): unknown
   name, TypeError from bad args, any backend exception -- every failure
   becomes an ordinary {"error": ...} tool result the model reads and
   recovers from. ToolGate.execute never raises.
3. Two-strikes containment: a conservative classifier counts only "not
   configured" results and the "<name> failed:" shape execute()'s own
   exception catch produces (under-counting is the safe direction -- a tool
   that still sometimes works must stay available). The SECOND hard failure
   removes the tool from the offered surface for the rest of the session --
   realized as hook-deny + executor-refusal, since SDK tool registration is
   fixed at connect() -- and fires the on_disable callback so the engine can
   emit a tool_disabled event for the TUI. Never persisted: the next session
   gets a clean slate.
4. OperatorContext sidecar: per-session trusted values (session_id, cwd,
   repo_root, belief-store handle) ride as their OWN kwarg into the
   operators that declare it (operators.OP_CTX_OPERATORS) -- NEVER inside
   the model-writable args dict, and a model-supplied "op_ctx" key is
   stripped unconditionally before dispatch. args is the one namespace the
   model writes to; trusting a principal-shaped value there would defeat
   the point (see the harness's OperatorContext docstring, mirrored here).
"""

from __future__ import annotations

import inspect
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable

from .operators import OP_CTX_OPERATORS, OPERATORS, WRITE_OPERATORS, registry_name
from .session_ops import SESSION_OP_CTX_OPERATORS, SESSION_OPERATORS

TOOL_DISABLE_AFTER = 2

# Every registry a DOXA-native tool can be defined in, in lookup order.
# doxa.session_ops is a SIBLING of doxa.operators, not a part of it (see
# its module docstring for the charter argument), and contract 2 above is
# why that costs nothing: containment does not care which module an
# Operator came from, only that every call flows through this one
# executor. Adding a registry here is the only wiring a new one needs.
_REGISTRIES: "tuple[dict[str, Any], ...]" = (
    OPERATORS, WRITE_OPERATORS, SESSION_OPERATORS,
)

# The union of every registry's own "declares the op_ctx sidecar" set.
# One rule, applied across both modules -- see contract 4.
_OP_CTX_NAMES = frozenset(OP_CTX_OPERATORS) | SESSION_OP_CTX_OPERATORS


def _registry_for(name: str) -> "dict[str, Any] | None":
    """Which registry defines ``name``, or None for an unknown tool."""
    for registry in _REGISTRIES:
        if name in registry:
            return registry
    return None


def _known(name: str) -> bool:
    return _registry_for(name) is not None


@dataclass(frozen=True)
class OperatorContext:
    """Per-session dispatch context, built once by doxa.engine from values
    the HOST resolved (never from anything model-supplied) and threaded to
    declaring operators as an explicit op_ctx kwarg -- its own channel,
    exactly like the reference implementation, so it can never collide with
    or leak into the model-suppliable args namespace.

    ``belief_store``: zero-arg handle returning a lore_core DB connection
    (lore_store.db_connect in production; a recording fake in the read-only
    guarantee test). None means "use lore_core's own default" -- operators
    treat an absent seam as today's behavior, unchanged."""

    session_id: str
    cwd: str
    repo_root: str
    belief_store: "Callable[[], Any] | None" = None

    spawn_depth: int = 0
    """How deep this session already sits in a spawn chain -- 0 for one a
    human started. Carried on the SIDECAR rather than passed as a tool
    argument for the obvious reason: a depth the model could write is not
    a depth limit. Its value reaches this process on its own command line
    (``doxa.daemon``'s ``--spawn-depth``) and never from the registry, so
    reaping an ancestor's entry cannot lose it. See
    ``doxa.session_ops.MAX_SPAWN_DEPTH``."""

    spawn_confirm: "Callable[[dict], Any] | None" = None
    """Async seam for "ask the human, and wait for a real answer" --
    ``SessionEngine._confirm_spawn``, which parks the call on the same
    out-of-band needs_input queue ``AskUserQuestion`` already uses and
    resolves it with ``{"decision": "allow"|"deny"}``.

    A seam rather than a direct engine call for the same reason
    ``belief_store`` is one: an operator must not import the engine, and a
    test must be able to answer without a TUI. None means this session has
    no channel to ask on at all -- ``doxa.session_ops`` treats that as a
    refusal, never as an implied yes."""


def repo_root_of(cwd: str) -> str:
    """Git toplevel when inside a repo, the cwd itself otherwise -- same
    project-identity rule as lore_core.config.project_slug, kept as a path
    instead of a slug."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except OSError:
        pass
    return str(cwd)


def is_hard_failure(name: str, result: Any) -> bool:
    """Conservative classifier for the two-strikes tracker: True ONLY for an
    {"error": ...} result that says the TOOL ITSELF is broken -- a "not
    configured" backend, or the "<name> failed: ..." shape ToolGate.
    execute's generic exception catch produces (so a raised exception is
    caught by this same check, no separate did-it-raise flag).

    Deliberately False for everything else: empty results ("no matching
    beliefs" is an answer, not a failure), bad-args results ("bad arguments
    for <name>: ..." -- the model's mistake, retryable), and single-colon
    validation messages ("<name>: empty query"). Under-counting is the safe
    direction."""
    if not (isinstance(result, dict) and isinstance(result.get("error"), str)):
        return False
    err = result["error"]
    if "not configured" in err:
        return True
    return bool(re.match(rf"^{re.escape(name)} failed:", err))


def _deny(reason: str) -> dict:
    """A graceful PreToolUse deny -- the CLI turns it into an ordinary
    refused tool result the model sees; the session continues."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


@dataclass
class ToolGate:
    """One session's containment state. ``allowed`` is the session tool
    policy over REGISTRY-side names (None = allow everything);``op_ctx`` is
    the trusted sidecar injected into declaring operators; ``on_disable``
    fires once per tool the two-strikes tracker removes, with (name,
    reason) -- doxa.engine turns it into a tool_disabled EngineEvent."""

    allowed: "set[str] | None" = None
    op_ctx: "OperatorContext | None" = None
    on_disable: "Callable[[str, str], None] | None" = None
    failures: dict = field(default_factory=dict)
    disabled: set = field(default_factory=set)

    # -- hook side (ALL tools) ----------------------------------------

    def pre_tool_use(self, input_data: dict) -> dict:
        """The PreToolUse decision for ANY tool call -- SDK built-ins pass
        through untouched unless the allowed-set policy or the two-strikes
        disable says otherwise; DOXA-native calls that pass here still
        execute via execute() below, which re-checks (defence in depth --
        never trust a single layer at a choke point)."""
        name = str((input_data or {}).get("tool_name") or "")
        base = registry_name(name)
        if base in self.disabled:
            return _deny(
                f"{base} is disabled for the rest of this session after repeated failures"
            )
        if self.allowed is not None and base not in self.allowed:
            if _known(base):
                # A repeatedly-refused known tool is the strongest "stop
                # calling this" signal there is -- it feeds the same
                # two-strikes counter as any other hard failure (harness
                # executor-gate parity).
                self._note_hard(base, f"tool not permitted: {base!r}")
            return _deny(f"tool not permitted: {base!r}")
        return {}

    # -- executor side (DOXA-native operators) ------------------------

    def execute(self, name: str, args: dict) -> Any:
        """Run one DOXA-native tool call against the registry. NEVER raises:
        every failure is an ordinary {"error": ...} result the model sees
        (contract 2 in the module docstring). Hard failures feed the
        two-strikes tracker.

        Usually returns a dict. An operator whose ``fn`` returns an
        AWAITABLE (``doxa.session_ops.spawn_session``: it has to park on a
        human's answer and then hand a 60-second subprocess poll to a
        worker thread) gets a coroutine back instead, which settles
        through the SAME classifier and the SAME two-strikes tracker the
        moment it is awaited -- ``to_sdk_tools``' handler already awaits
        exactly this. The alternative, a second async executor, would put
        containment in two places, which is the one thing this module
        exists to prevent."""
        base = registry_name(name)
        result = self._execute_inner(base, args)
        if inspect.isawaitable(result):
            return self._settle_async(base, result)
        return self._settle(base, result)

    def _settle(self, name: str, result: Any) -> Any:
        if is_hard_failure(name, result):
            self._note_hard(name, result["error"])
        return result

    async def _settle_async(self, name: str, awaitable: Any) -> dict:
        """The awaitable half of :meth:`execute`'s contract -- including
        its never-raises half: an exception escaping the coroutine becomes
        the same ``"<name> failed: ..."`` result (and therefore the same
        strike) a synchronous one would have."""
        try:
            result = await awaitable
        except Exception as exc:  # noqa: BLE001 -- parity with the sync path
            result = {"error": f"{name} failed: {type(exc).__name__}: {exc}"}
        return self._settle(name, result)

    def _execute_inner(self, name: str, args: dict) -> Any:
        if name in self.disabled:
            return {"error": (
                f"{name} is disabled for the rest of this session after "
                "repeated failures -- stop calling it")}
        registry = _registry_for(name)
        if registry is None:
            return {"error": f"unknown tool: {name!r}. Available tools: {sorted(OPERATORS)}"}
        if self.allowed is not None and name not in self.allowed:
            # Defence in depth behind the hook deny: server-side enforced,
            # never trust the tool call. Counts as hard (see pre_tool_use).
            self._note_hard(name, f"tool not permitted: {name!r}")
            return {"error": f"tool not permitted: {name!r}"}
        # Contract 4: a model-supplied op_ctx is stripped ALWAYS; the real
        # sidecar goes into a NEW dict, never a mutation of the caller's
        # args, and only for operators that declare it.
        args = {k: v for k, v in dict(args or {}).items() if k != "op_ctx"}
        kwargs = args
        if self.op_ctx is not None and name in _OP_CTX_NAMES:
            kwargs = dict(args)
            kwargs["op_ctx"] = self.op_ctx
        try:
            return registry[name].fn(**kwargs)
        except TypeError as exc:
            # Almost always the model passing wrong/extra kwargs -- a
            # RECOVERABLE bad-arguments result, not a server fault, and
            # deliberately NOT a hard failure for the tracker.
            return {"error": f"bad arguments for {name}: {exc}"}
        except Exception as exc:  # noqa: BLE001 -- never let a tool crash the session
            return {"error": f"{name} failed: {type(exc).__name__}: {exc}"}

    # -- two-strikes tracker ------------------------------------------

    def _note_hard(self, name: str, reason: str) -> None:
        self.failures[name] = self.failures.get(name, 0) + 1
        if self.failures[name] >= TOOL_DISABLE_AFTER and name not in self.disabled:
            self.disabled.add(name)
            if self.on_disable is not None:
                self.on_disable(name, reason)

    def disabled_tools(self) -> list[str]:
        return sorted(self.disabled)
