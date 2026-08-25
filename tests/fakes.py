# SPDX-License-Identifier: AGPL-3.0-only
"""Test doubles shared by test_engine.py and test_app.py.

FakeClient stands in for claude_agent_sdk.ClaudeSDKClient: no subprocess, no
network, no `claude` CLI on PATH required. FakeEngine stands in for
doxa.engine.SessionEngine at the doxa.app layer, so the Textual pilot test
can drive a scripted turn without a real engine (and therefore without a
real SDK client) underneath it -- app.py only ever touches the small surface
reproduced here (start/send/finalize/model/total_cost_usd/
last_ctx_percentage/effort/belief_count/list_beliefs/list_pending), so the fake is a
narrow, honest stand-in rather than a reimplementation of the engine.

reasoning_delta (v0.25.0) needs NO change here: FakeEngine.send() replays
whatever EngineEvent script it was given verbatim, so a script that
includes EngineEvent("reasoning_delta", {...}) already exercises
doxa.app's handling of it exactly like any other event type -- the parity
this docstring promises is in the REPLAY, not in a per-type branch that
would need one more case for every new engine event.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from doxa.engine import EngineEvent
from doxa.peers import PeerInfo, resolve_peer


class FakeClient:
    """Stands in for ClaudeSDKClient. `script` is a list of already-built
    claude_agent_sdk message dataclasses that receive_response() replays
    verbatim -- real message types, fake transport."""

    def __init__(
        self,
        options: Any,
        script: list[Any] | None = None,
        ctx_usage: dict | None = None,
        server_info: dict | None = None,
    ) -> None:
        self.options = options
        self.script = script or []
        self.ctx_usage = ctx_usage
        self.server_info = server_info
        self.entered = False
        self.exited = False
        self.queried: list[tuple[str, str]] = []
        # v0.42.0: every mode handed to set_permission_mode, in order.
        # This is the SDK seam itself -- a test asserting "the mode
        # actually reached the SDK" reads THIS list, not an engine
        # attribute the engine set on itself.
        self.permission_modes: list[str] = []

    async def __aenter__(self) -> "FakeClient":
        self.entered = True
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.exited = True
        return False

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self.queried.append((prompt, session_id))

    async def receive_response(self) -> AsyncIterator[Any]:
        for message in self.script:
            yield message

    async def set_permission_mode(self, mode: str) -> None:
        """The SDK control request DOXA's /mode drives
        (``ClaudeSDKClient.set_permission_mode``)."""
        self.permission_modes.append(mode)

    async def get_context_usage(self) -> dict:
        if self.ctx_usage is None:
            raise RuntimeError("no context usage scripted for this FakeClient")
        return self.ctx_usage

    async def get_server_info(self) -> "dict | None":
        return self.server_info


def factory_with_script(
    script: list[Any],
    ctx_usage: dict | None = None,
    server_info: dict | None = None,
) -> tuple[Any, list[FakeClient]]:
    """Returns (factory, created); created[0] is the FakeClient instance
    SessionEngine.start() built, once it has run -- for post-hoc assertions
    on what options/prompts it was given."""
    created: list[FakeClient] = []

    def factory(options: Any) -> FakeClient:
        client = FakeClient(
            options, script=script, ctx_usage=ctx_usage, server_info=server_info
        )
        created.append(client)
        return client

    return factory, created


class FakeEngine:
    """The doxa.app.DoxaApp-facing surface of SessionEngine, scripted.

    Peer surface: `peers` is the static list list_peers() returns (real
    PeerInfo objects, so /peers formatting and /msg resolution exercise the
    same doxa.peers.resolve_peer the real engine uses); push_peer_event()
    feeds the app's peer pump exactly like the real out-of-band queue."""

    def __init__(
        self,
        script: list[EngineEvent],
        model: str = "claude-haiku-4-5",
        peers: list[PeerInfo] | None = None,
        effort: "str | None" = None,
        cwd: str = "",
        permission_mode: str = "default",
        bypass_armed: bool = False,
    ) -> None:
        self._script = script
        self.model = model
        # Engine parity for the surfaces that ask an engine where its
        # session actually lives (/search, and item Q's `!`, which must run
        # in the session's own worktree). Empty by default so every
        # pre-existing test keeps falling through to the pane's own cwd,
        # exactly as it did before this attribute existed.
        self.cwd = cwd
        self.total_cost_usd = 0.0
        self.last_ctx_percentage: float | None = None
        # Engine parity (item X): the absolute halves of the same context
        # reading. None by default -- an engine that has never finished a
        # turn has measured nothing, and the chip/tooltip must survive that
        # rather than assume a window size.
        self.last_ctx_tokens: "int | None" = None
        self.last_ctx_max_tokens: "int | None" = None
        # Engine parity (item T): connect-time effort, asserted once and
        # never mutated for the life of a session -- same shape as the real
        # engine's self.effort.
        self.effort = effort
        # Engine parity (v0.42.0): the session's CURRENT permission mode.
        # Unlike effort directly above, this one MOVES -- the SDK has a
        # live setter for it -- so the fake records every switch the way it
        # already records model_switches, which is exactly what a test
        # asserting "the mode actually reached the engine" reads.
        self.permission_mode = permission_mode
        # Engine parity (v0.58.0): whether this session's CLI was spawned
        # able to reach bypassPermissions at all. Default False, matching
        # the shipped default, so every pre-existing test exercises the
        # UNARMED session -- which is the one a user actually gets.
        self.bypass_armed = bypass_armed
        self.permission_mode_switches: list[str] = []
        # Set to an exception to make set_permission_mode refuse, the same
        # way the real engine refuses an unknown mode or a disconnected
        # client -- the pane has to SHOW that rather than swallow it.
        self.permission_mode_error: "Exception | None" = None
        self.started = False
        self.finalized = False
        self._peers = peers or []
        self._peer_queue: asyncio.Queue[EngineEvent] = asyncio.Queue()
        self.sent_peer_messages: list[tuple[str, str]] = []
        self.disabled: list[str] = []  # two-strikes containment, scripted
        # Identity surface (engine parity): tests set account fields to
        # exercise the subscription-aware display / identity block.
        self.account: dict = {}
        self.lore_root: "str | None" = None
        self.model_switches: list = []
        self.num_turns = 0
        # Every prompt string actually handed to send() -- what item N's
        # tests check to prove a submitted turn carried the FULL resolved
        # text (pending pastes expanded), not a collapsed placeholder.
        self.received_prompts: list[str] = []
        # Queue item 5: every (id, answer) handed to answer_needs_input --
        # what the UI-level tests check to prove a key/click resolved the
        # RIGHT request with the RIGHT payload, engine-parity style (the
        # same surface SessionEngine.answer_needs_input exposes).
        self.needs_input_answers: list[tuple[str, dict]] = []
        # Item S (/branch): every target handed to switch_branch (None for
        # a bare listing), and the canned results a test sets before
        # calling it -- see switch_branch's own docstring below.
        self.branch_calls: list["str | None"] = []
        self.branch_list_result: dict = {
            "branches": [], "base": None, "checked_out": None,
        }
        self.branch_switch_result: dict = {
            "ok": True, "base": None, "message": "switched",
        }
        # Item 3 (beliefs picker): scriptable list_beliefs() result, plus a
        # call counter -- what the cost-discipline test asserts stays at 0
        # across ordinary status refreshes and only grows once the picker
        # itself is opened.
        self.list_beliefs_result: list[dict] = []
        self.list_beliefs_calls = 0
        # v0.31.0 (/pending): scriptable list_pending() result, the same
        # shape and the same call counter as the beliefs pair above. The
        # picker is click-only, so the counter is what proves an ordinary
        # status refresh never reaches for staged-proposal text.
        self.list_pending_result: list = []
        self.list_pending_calls = 0
        self.list_pending_error: "Exception | None" = None
        # Item V (the beliefs browser): the evidence trail a belief row
        # expands to, keyed by belief id, plus the lore_core write-state
        # the browser asks for before it renders any approve/reject
        # control at all -- scripting it False is how the read-only
        # degradation gets exercised without an old lore_core on disk.
        self.belief_evidence_result: "dict[Any, list[dict]]" = {}
        self.belief_evidence_calls: list = []
        self.lore_write_state_result: dict = {
            "capable": True, "version": "0.36.0", "source": "package",
            "location": "/fake", "reason": "",
        }
        # THE SECURITY LEDGER. Every approve/reject that reached the engine,
        # in order, one entry per call. A test asserting that nothing is
        # approved without an explicit per-item action asserts against this
        # list, not against the UI -- the UI is what is on trial.
        self.approved: list[str] = []
        self.rejected: list[str] = []
        self.approve_error: "str | None" = None
        self.reject_error: "str | None" = None
        # v0.48.0 (belief actions): the ledger for outcomes and retracts,
        # same discipline as approved/rejected above -- a test asserting
        # that nothing fires without an explicit per-belief action asserts
        # against these lists, not against the UI.
        self.outcomes_recorded: list = []
        self.retracted: list = []
        self.outcome_error: "str | None" = None
        self.retract_error: "str | None" = None
        self.belief_action_state_result: dict = {
            "capable": True, "version": "0.36.0", "source": "package",
            "reason": "",
        }
        # Item K (/context): what context_usage() hands back, plus a call
        # counter. None is the REAL absence case (a session whose handle
        # cannot report a breakdown), which is exactly what the "nothing is
        # estimated" assertion needs to be able to script.
        self.context_usage_result: "dict | None" = None
        self.context_usage_calls = 0
        self.context_usage_error: "Exception | None" = None

    async def context_usage(self) -> "dict | None":
        """Engine parity for /context. Both real engines normalize through
        doxa.engine.context_breakdown before returning, so the fake returns
        an already-normalized dict too -- the pane never sees the raw SDK
        shape from either of them."""
        self.context_usage_calls += 1
        if self.context_usage_error is not None:
            raise self.context_usage_error
        return self.context_usage_result

    async def list_pending(self, limit: int = 500, offset: int = 0) -> list[str]:
        self.list_pending_calls += 1
        if self.list_pending_error is not None:
            raise self.list_pending_error
        return list(self.list_pending_result)[offset : offset + limit]

    async def belief_evidence(self, belief_id, limit: int = 40) -> list[dict]:
        """Engine parity for item V's lazy evidence fetch."""
        self.belief_evidence_calls.append(belief_id)
        return list(self.belief_evidence_result.get(belief_id, []))

    def belief_action_state(self) -> dict:
        """Engine parity for v0.48.0's narrower capability check -- what
        gates recording an outcome and retracting, which need only the
        outcome ledger rather than 0.36.0's provenance columns."""
        return dict(self.belief_action_state_result)

    async def record_belief_outcome(self, belief_id, event, note=None) -> "str | None":
        """ONE belief, ONE verdict -- no list form on either real engine."""
        self.outcomes_recorded.append((belief_id, event))
        return self.outcome_error

    async def retract_belief(self, belief_id, reason="retracted") -> "str | None":
        self.retracted.append(belief_id)
        return self.retract_error

    def lore_write_state(self) -> dict:
        """Engine parity for item V's read-only degradation check.
        Sync here, like SessionEngine's; EngineClient's is async and the
        browser awaits whichever it got."""
        return dict(self.lore_write_state_result)

    async def approve_pending(self, pid: str) -> "str | None":
        """Engine parity for item V's write half. ONE id -- there is no
        list form on either real engine and there is none here either, so
        a test cannot accidentally prove a bulk path works."""
        self.approved.append(str(pid))
        return self.approve_error

    async def reject_pending(self, pid: str) -> "str | None":
        self.rejected.append(str(pid))
        return self.reject_error

    async def answer_needs_input(self, req_id: str, answer: dict) -> bool:
        self.needs_input_answers.append((req_id, dict(answer or {})))
        return True

    def disabled_tools(self) -> list[str]:
        return list(self.disabled)

    def list_peers(self) -> list[PeerInfo]:
        return list(self._peers)

    def peer_count(self) -> int:
        return len(self._peers)

    def push_peer_event(self, ev: EngineEvent) -> None:
        self._peer_queue.put_nowait(ev)

    async def peer_events(self) -> AsyncIterator[EngineEvent]:
        while True:
            yield await self._peer_queue.get()

    async def send_peer_message(self, target_prefix: str, text: str) -> PeerInfo:
        peer = resolve_peer(self._peers, target_prefix)  # may raise PeerSendError
        self.sent_peer_messages.append((peer.session_id, text))
        return peer

    async def start(self) -> EngineEvent:
        self.started = True
        return EngineEvent("session_started", {"session_id": "fake", "model": self.model})

    async def send(self, prompt: str) -> AsyncIterator[EngineEvent]:
        self.received_prompts.append(prompt)
        for ev in self._script:
            if ev.type == "turn_done":
                self.total_cost_usd += ev.data.get("cost_usd") or 0.0
                self.last_ctx_percentage = ev.data.get("ctx_percentage")
                if ev.data.get("ctx_tokens") is not None:
                    self.last_ctx_tokens = ev.data["ctx_tokens"]
                if ev.data.get("ctx_max_tokens") is not None:
                    self.last_ctx_max_tokens = ev.data["ctx_max_tokens"]
            yield ev

    async def set_model(self, model: "str | None") -> str:
        """Engine parity for /model. The real engine issues an SDK control
        request; the fake just records the switch, which is exactly the
        surface app.py touches."""
        self.model = model
        self.model_switches.append(model)
        return model or "default"

    async def set_permission_mode(self, mode: str) -> str:
        """Engine parity for /mode (v0.42.0). The real engines issue an SDK
        control request / a daemon RPC; the fake records the switch, which
        is the whole surface the pane touches.

        v0.58.0: refuses a mode this session is not armed for, exactly as
        SessionEngine does, so a test cannot accidentally prove a UI path
        works against a fake more permissive than the real thing."""
        if self.permission_mode_error is not None:
            raise self.permission_mode_error
        from doxa import engine as _engine_mod

        if mode not in _engine_mod.available_modes(self.bypass_armed):
            raise RuntimeError(
                f"{mode} needs a session started with "
                f"--{_engine_mod.BYPASS_ARM_FLAG}; this one was not"
            )
        self.permission_mode = mode
        self.permission_mode_switches.append(mode)
        return mode

    async def switch_branch(self, target: "str | None") -> dict:
        """Engine parity for /branch (item S). Scriptable via
        ``branch_list_result``/``branch_switch_result`` -- the real
        engines both delegate to doxa.worktrees, which is exercised with
        real git in tests/test_worktrees.py; this fake only needs to prove
        app.py's command handler dispatches and displays correctly."""
        self.branch_calls.append(target)
        return dict(self.branch_list_result if target is None
                    else self.branch_switch_result)

    def usage_summary(self) -> dict:
        return {
            "session_id": "fake-session-id",
            "model": self.model,
            "num_turns": self.num_turns,
            "total_cost_usd": self.total_cost_usd,
            "ctx_percentage": self.last_ctx_percentage,
            "ctx_tokens": self.last_ctx_tokens,
            "ctx_max_tokens": self.last_ctx_max_tokens,
            "input_tokens": 1200,
            "output_tokens": 340,
            "cache_read_input_tokens": 8000,
            "cache_creation_input_tokens": 150,
        }

    def belief_count(self) -> int:
        return 3

    async def list_beliefs(self) -> list[dict]:
        """Engine parity for item 3's beliefs picker. ``list_beliefs_result``
        is what a test scripts (defaults to empty); every call is recorded
        in ``list_beliefs_calls`` so a test can assert this is NEVER called
        by a status refresh, only by the picker's own open_beliefs_picker."""
        self.list_beliefs_calls += 1
        return list(self.list_beliefs_result)

    async def finalize(self) -> EngineEvent:
        self.finalized = True
        return EngineEvent("session_done", {"indexed": 0, "belief_count": self.belief_count()})
