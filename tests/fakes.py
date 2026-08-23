"""Test doubles shared by test_engine.py and test_app.py.

FakeClient stands in for claude_agent_sdk.ClaudeSDKClient: no subprocess, no
network, no `claude` CLI on PATH required. FakeEngine stands in for
doxa.engine.SessionEngine at the doxa.app layer, so the Textual pilot test
can drive a scripted turn without a real engine (and therefore without a
real SDK client) underneath it -- app.py only ever touches the small surface
reproduced here (start/send/finalize/model/total_cost_usd/
last_ctx_percentage/belief_count), so the fake is a narrow, honest stand-in
rather than a reimplementation of the engine.
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
    ) -> None:
        self.options = options
        self.script = script or []
        self.ctx_usage = ctx_usage
        self.entered = False
        self.exited = False
        self.queried: list[tuple[str, str]] = []

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

    async def get_context_usage(self) -> dict:
        if self.ctx_usage is None:
            raise RuntimeError("no context usage scripted for this FakeClient")
        return self.ctx_usage


def factory_with_script(
    script: list[Any], ctx_usage: dict | None = None
) -> tuple[Any, list[FakeClient]]:
    """Returns (factory, created); created[0] is the FakeClient instance
    SessionEngine.start() built, once it has run -- for post-hoc assertions
    on what options/prompts it was given."""
    created: list[FakeClient] = []

    def factory(options: Any) -> FakeClient:
        client = FakeClient(options, script=script, ctx_usage=ctx_usage)
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
    ) -> None:
        self._script = script
        self.model = model
        self.total_cost_usd = 0.0
        self.last_ctx_percentage: float | None = None
        self.started = False
        self.finalized = False
        self._peers = peers or []
        self._peer_queue: asyncio.Queue[EngineEvent] = asyncio.Queue()
        self.sent_peer_messages: list[tuple[str, str]] = []
        self.disabled: list[str] = []  # two-strikes containment, scripted

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
        for ev in self._script:
            if ev.type == "turn_done":
                self.total_cost_usd += ev.data.get("cost_usd") or 0.0
                self.last_ctx_percentage = ev.data.get("ctx_percentage")
            yield ev

    def belief_count(self) -> int:
        return 3

    async def finalize(self) -> EngineEvent:
        self.finalized = True
        return EngineEvent("session_done", {"indexed": 0, "belief_count": self.belief_count()})
