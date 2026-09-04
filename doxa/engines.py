# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.engines -- the SESSION seam: ONE Protocol between the TUI and
whatever actually runs a turn, plus a registry mapping an engine id to a
provider.

The sibling of :mod:`doxa.providers`, and the half that module's own
docstring said was missing: "the SESSION half is not here at all -- spawn,
send, interrupt and the event stream are what SessionEngine and
EngineClient already agree on INFORMALLY". This module names that
agreement. It does not invent it.

WHAT THE SEAM ALREADY WAS, MEASURED. Before a line of this was written,
``SessionEngine`` (in-process, Claude Agent SDK) and ``EngineClient``
(daemon socket, has never imported the SDK) were compared member by
member. They share 24 public names, and both already yield
:class:`~doxa.events.EngineEvent` -- DOXA's own type. The TUI has never
seen an SDK object. So the Protocol below is a TRANSCRIPT of that
intersection, and three things were deliberately left OUT of it because
the transcript said so:

* ``stop`` -- ``EngineClient`` has it, ``SessionEngine`` does NOT. It is
  the daemon's "finalize NOW" verb and it is meaningless in-process.
  ``doxa.session.runtime.PaneRuntimeMixin.stop`` already reads it through
  ``getattr(engine, "stop", None)`` and falls back to ``finalize()``.
  Putting it in the Protocol would have been the exact mistake the spec's
  own check was written to catch -- only in the mirror image: a Protocol
  written against ``EngineClient``'s implementation rather than the seam.
* ``lore_write_state`` and ``belief_action_state`` -- present on both, but
  SYNC on ``SessionEngine`` and ASYNC on ``EngineClient``. One Protocol
  signature cannot be honest about both, and the call sites already
  await-or-not by inspection.
* the belief/pending PICKER surface (``list_beliefs``, ``list_pending``,
  ``belief_evidence``, ``record_belief_outcome``, ``retract_belief``,
  ``approve_pending``, ``reject_pending``). Every one of these is a
  lore_core query with no engine in it at all, every call site already
  reaches them through ``getattr``, and they live on ``SessionEngine``
  only because that is where they were written. See
  :class:`EngineCapabilities.lore_pickers` -- a second engine inherits the
  belief store's COUNT (which is one SELECT) but not its pickers, and
  un-coupling that is its own piece of work, not this one's.

WHAT ``supports()`` IS FOR. Capability is not uniform and pretending
otherwise is the trap. Claude Code has MCP tools, hooks, permission modes,
``--plugin-dir``, an init message naming the model it actually chose, and
``get_context_usage``. Codex has some of those, differently, and not
others. :class:`EngineCapabilities` is the honest map, and every caller
handles a ``False`` rather than rendering a zero: ``/context`` already
says so instead of inventing a breakdown, and the ctx chip is now OMITTED
outright for an engine that can never report a window (rather than
painting ``ctx —`` forever, which reads as "not yet" and would be a lie
once "never" is the truth).

NO SDK HERE. Same rule :mod:`doxa.events` established and for the same
measured reason: importing ``claude_agent_sdk`` costs 404 ms, and a module
whose job is to say WHICH engine must not force one of them to load.
Every provider imports its own engine inside :meth:`EngineProvider.
new_session`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Any, Protocol, runtime_checkable

from .events import EngineEvent


# The engine ids DOXA itself ships. Free strings on the wire (see
# peers.PeerInfo.engine -- a peer's self-description is displayed, never
# verified, and decides nothing), but the REGISTRY keys are these two and
# a lookup of anything else is an error, not a guess.
CLAUDE_ENGINE_ID = "claude"
CODEX_ENGINE_ID = "codex"

DEFAULT_ENGINE_ID = CLAUDE_ENGINE_ID


@dataclass(frozen=True)
class EngineCapabilities:
    """What one engine can actually do, as a flat map of facts.

    Every field defaults to the CONSERVATIVE answer (``False``), so a
    provider that forgets to declare something under-promises rather than
    over-promises. ``EngineCapabilities.claude()`` is the full set; a
    second engine starts from ``EngineCapabilities()`` and turns on only
    what it has been observed to do.

    A capability map that lies is worse than no second engine -- so
    nothing here is aspirational and nothing here is inferred. Each field
    below names the surface it gates."""

    #: DOXA's own LORE operators (doxa.operators / doxa.session_ops) reach
    #: the model as callable tools, through the executor doxa.gate.ToolGate
    #: contains. False means the session simply does not have them and says
    #: so -- never that they were offered and silently dropped.
    mcp_tools: bool = False
    #: UserPromptSubmit / PreToolUse / PreCompact hooks -- how DOXA injects
    #: the LORE snapshot and how the graph-context block reaches a turn.
    hooks: bool = False
    #: can_use_tool: DOXA sees each call before it runs, can refuse it, and
    #: the two-strikes tracker can disable a tool (the tool_disabled event).
    tool_gate: bool = False
    #: /mode, Shift+Tab, the mode chip -- an engine with one fixed posture
    #: reports False and those surfaces say so rather than cycling nothing.
    permission_modes: bool = False
    #: --plugin-dir: doxa.claude_plugins can hand this engine adopted
    #: skills and agents.
    plugins: bool = False
    #: The engine reports which model it ACTUALLY resolved (Claude's init
    #: SystemMessage). False means self.model is only ever what was asked
    #: for, and an unasked-for default publishes as absent, never guessed.
    resolved_model: bool = False
    #: The engine can be asked what is in its context window: the
    #: percentage, the size, the per-category breakdown /context renders.
    #: False hides the ctx chip and makes /context say so.
    context_window: bool = False
    #: Per-turn token counts (in / out / cached). Independent of
    #: context_window: an engine can count tokens and still never report a
    #: window size, which is exactly Codex.
    token_usage: bool = False
    #: A dollar figure for the session. False omits the cost chip rather
    #: than painting $0.0000, which reads as "free" and not as "unknown".
    cost: bool = False
    #: The model's own reasoning reaches the transcript (reasoning_delta).
    reasoning: bool = False
    #: Assistant text arrives incrementally. False means it lands in whole
    #: messages -- still a text_delta, just one of them.
    streaming_text: bool = False
    #: set_model takes effect on this session (live, or from the next turn).
    live_model_switch: bool = False
    #: The conversation can be continued after the process running it exits.
    resume: bool = False
    #: The session can be detached from and reattached to -- the daemon
    #: split. An in-process engine reports False and Ctrl+Q stops it.
    detachable: bool = False
    #: DOXA's peer layer (the rail, /msg, the registry). Engine-agnostic by
    #: construction -- doxa.peers has no model in it -- so both engines
    #: report True, and it is a field rather than an assumption because the
    #: next engine may not be able to host a PeerHost at all.
    peer_messaging: bool = False
    #: spawn_session (doxa.session_ops). Same-engine only, per the spec.
    spawn_sessions: bool = False
    #: The belief/pending PICKERS (list_beliefs, list_pending, the approve
    #: and outcome paths). See the module docstring: this is a property of
    #: where the code happens to live, not of the engine, and it is
    #: reported honestly rather than quietly returning empty lists.
    lore_pickers: bool = False

    @classmethod
    def claude(cls) -> "EngineCapabilities":
        """Everything, because SessionEngine is where every one of these
        surfaces was built. Written as an explicit all-True rather than a
        default flip so that adding a field forces a decision here."""
        return cls(
            mcp_tools=True, hooks=True, tool_gate=True, permission_modes=True,
            plugins=True, resolved_model=True, context_window=True,
            token_usage=True, cost=True, reasoning=True, streaming_text=True,
            live_model_switch=True, resume=True, detachable=True,
            peer_messaging=True, spawn_sessions=True, lore_pickers=True,
        )

    def without(self, **flags: bool) -> "EngineCapabilities":
        """A copy with some flags overridden -- used by a provider that is
        the same engine in a narrower posture (an in-process Claude session
        is not detachable)."""
        return replace(self, **flags)


#: What a caller assumes when an engine handle carries no declaration of
#: its own. Claude's full set, deliberately: every engine handle that
#: existed before this module (SessionEngine, EngineClient) is a Claude
#: session, and the default has to reproduce the behaviour those already
#: had rather than start hiding chips for them.
DEFAULT_CAPABILITIES = EngineCapabilities.claude()


def capabilities_of(engine: Any) -> EngineCapabilities:
    """The capability map for a live engine handle.

    Read off an optional ``engine_capabilities`` attribute -- the same
    duck-typed, strictly-additive convention ``detachable``, ``account``
    and ``backlog_skipped`` already follow on this seam. Absent means
    :data:`DEFAULT_CAPABILITIES`, so nothing about a Claude session changes
    by this module existing, and a handle that DOES declare (CodexEngine)
    is believed about itself.

    Note what is NOT done here: no lookup of ``engine.engine_id`` in the
    registry. ``EngineClient`` fronts a daemon and cannot know from its own
    side what that daemon hosts; asking the registry would turn a socket's
    silence into a confident answer."""
    declared = getattr(engine, "engine_capabilities", None)
    return declared if isinstance(declared, EngineCapabilities) else DEFAULT_CAPABILITIES


@runtime_checkable
class Engine(Protocol):
    """What a DOXA session pane drives, and nothing more.

    This is the intersection ``SessionEngine`` and ``EngineClient`` ALREADY
    satisfied before it was written -- see the module docstring for the
    three things the measurement kept out. Both classes satisfy it
    unchanged; that was the check the spec owed itself, and it passed.

    ``runtime_checkable`` so the suite can assert that in one line. The
    check is structural (names, not signatures), which is the honest limit
    of what a Protocol can promise at runtime -- the signatures are held by
    the tests that drive both classes through the same pane."""

    # -- attributes the status bar reads mid-render, unguarded -----------
    cwd: "str | None"
    model: "str | None"
    session_id: "str | None"
    total_cost_usd: float
    last_ctx_percentage: "float | None"

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> EngineEvent:
        """Begin. Returns the session_started event."""
        ...

    async def finalize(self) -> EngineEvent:
        """End this handle's involvement. Returns session_done. On the
        daemon side this DETACHES rather than stops -- see EngineClient."""
        ...

    # -- turns -----------------------------------------------------------
    def send(self, prompt: str) -> "AsyncIterator[EngineEvent]":
        """One turn, as a stream of EngineEvents."""
        ...

    def peer_events(self) -> "AsyncIterator[EngineEvent]":
        """The out-of-band stream: peer traffic, tool_disabled,
        needs_input. Never waits for a turn generator's yield point."""
        ...

    async def set_model(self, model: "str | None") -> str: ...

    async def set_permission_mode(self, mode: str) -> str: ...

    async def switch_branch(self, target: "str | None") -> dict: ...

    async def answer_needs_input(self, req_id: str, answer: dict) -> bool: ...

    # -- what the surfaces read ------------------------------------------
    async def context_usage(self) -> "dict[str, Any] | None":
        """/context's breakdown, or None when this session cannot be asked.
        None is the honest answer and the command prints it as one."""
        ...

    def usage_summary(self) -> "dict[str, Any]": ...

    def belief_count(self) -> int: ...

    def disabled_tools(self) -> "list[str]": ...

    # -- peers -----------------------------------------------------------
    async def send_peer_message(self, target_prefix: str, text: str) -> Any: ...

    def list_peers(self) -> list: ...

    def peer_count(self) -> int: ...


class EngineProvider(Protocol):
    """Which engine, and what it can do. The session half of what
    :class:`doxa.providers.ModelProvider` does for the catalog half.

    ``new_session`` takes keyword arguments only, and a provider ignores
    the ones it has no use for: the argument list is DOXA's session
    vocabulary (cwd, model, session_id, resume, spawn_depth,
    parent_session_id), not any one SDK's."""

    def engine_id(self) -> str: ...

    def engine_display_name(self) -> str: ...

    def supports(self) -> EngineCapabilities: ...

    def new_session(self, **kwargs: Any) -> Engine: ...


class ClaudeEngineProvider:
    """The engine DOXA has always been. Imports ``doxa.engine`` -- and with
    it the 404 ms of ``claude_agent_sdk`` -- inside ``new_session`` and
    nowhere else."""

    def engine_id(self) -> str:
        return CLAUDE_ENGINE_ID

    def engine_display_name(self) -> str:
        return "Claude Code (Anthropic)"

    def supports(self) -> EngineCapabilities:
        return EngineCapabilities.claude()

    def new_session(self, **kwargs: Any) -> Engine:
        # Resolved through sys.modules, not a direct import, for the reason
        # doxa.app._in_process states: the suite substitutes a fake engine
        # with monkeypatch.setattr(doxa.app, "SessionEngine", ...) and a
        # direct import here would walk past every such patch.
        import sys

        from . import engine as engine_mod  # noqa: F401 -- ensures it is loaded

        return getattr(sys.modules["doxa.engine"], "SessionEngine")(**kwargs)


# -- the registry ------------------------------------------------------
#
# An explicit dict with explicit registration calls, the same discipline
# doxa.operators states for its own tool registry: nothing is discovered,
# nothing registers as an import side effect of being on the path. Adding
# an engine is a deliberate, reviewed act, and the closure test in
# tests/test_engines.py lists every id literally.

_REGISTRY: "dict[str, EngineProvider]" = {}


def register(provider: EngineProvider) -> EngineProvider:
    """Put a provider in the registry under its own id. Re-registering the
    same id REPLACES -- the suite builds a fresh registry per test and a
    raise here would make that an error instead of a setup step."""
    _REGISTRY[provider.engine_id()] = provider
    return provider


def get(engine_id: "str | None") -> EngineProvider:
    """The provider for an id. ``None``/empty means
    :data:`DEFAULT_ENGINE_ID`; an unknown id RAISES with the list of real
    ones, because silently falling back to Claude would start a session on
    an engine the operator did not ask for and never say so."""
    _ensure_builtins()
    # Strip BEFORE the default, not after: `--engine "  "` and `--engine ""`
    # are the same request (the operator named nothing), and a whitespace
    # id that fell through to the KeyError would report an "unknown engine
    # ''" that nobody typed.
    key = (engine_id or "").strip().lower() or DEFAULT_ENGINE_ID
    try:
        return _REGISTRY[key]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "(none registered)"
        raise KeyError(f"unknown engine {key!r} -- known engines: {known}") from None


def available() -> "tuple[str, ...]":
    """Every registered engine id, sorted. The picker's list, and the
    error message's."""
    _ensure_builtins()
    return tuple(sorted(_REGISTRY))


def is_known(engine_id: "str | None") -> bool:
    _ensure_builtins()
    return (engine_id or DEFAULT_ENGINE_ID).strip().lower() in _REGISTRY


register(ClaudeEngineProvider())

_codex_registered = False


def _ensure_builtins() -> None:
    """Register Codex on first LOOKUP, not at import.

    ``doxa.codex`` imports this module for the Protocol and the capability
    dataclass, so registering it at the bottom of this one is a cycle. It
    is also the wrong time: nothing that merely imports ``doxa.engines``
    (the ctx chip asking a handle what it supports, for one) needs Codex's
    module loaded. Every registry reader below calls this first, so the
    laziness is invisible from the outside -- ``available()`` lists both
    engines on a cold import."""
    global _codex_registered
    if _codex_registered:
        return
    # Set BEFORE the import: a failure here must not make every later
    # lookup retry a broken import, and Codex missing from the registry is
    # already the honest outcome (``get("codex")`` then raises with the
    # list of engines that DO exist).
    _codex_registered = True
    try:
        from .codex import CodexEngineProvider
    except Exception:  # noqa: BLE001 -- an engine that cannot import is absent
        return
    register(CodexEngineProvider())
