"""doxa.engine -- the session engine.

Wraps ``claude_agent_sdk.ClaudeSDKClient`` and exposes one thing to the TUI:
an async generator of typed :class:`EngineEvent` objects per turn. Every
LORE integration point below is wired at the boundary PHASE0_FINDINGS.md
validated for it, not at the boundary the original plan assumed -- each site
below cites the finding/redesign item it follows
(``/home/docwilde/Schreibtisch/doxa/PHASE0_FINDINGS.md``).

Boundaries used, and why:

* Snapshot injection -- ``ClaudeAgentOptions.system_prompt`` (preset +
  ``append``), evaluated once at ``connect()``. PHASE0 redesign item 2:
  SessionStart is undocumented/unreliable, so the snapshot must not wait for
  it -- system_prompt append is the one connection-time injection point that
  is guaranteed to be present at turn 1.
* Mid-session refresh -- ``UserPromptSubmit`` hook, honoring
  ``LORE_REFRESH_SECS`` the same way ``lore_core.context.cmd_refresh``
  does for the plugin. PHASE0 §2: "Exists natively, confirmed firing... the
  reliable 'per-turn refresh' boundary."
* Transcript-so-far review -- ``PreCompact`` hook. PHASE0 §2: "Exists
  natively, confirmed firing" via the literal ``"/compact"`` prompt-text
  convention (§6 compaction-control note) -- the harness is about to summarize
  the transcript away, so the deriver reviews it first, same as the LORE
  plugin's own SessionEnd-adjacent PreCompact wiring in ``deriver.cmd_review``.
* Tool gating -- ``PreToolUse`` hook. PHASE0 redesign item 3: tool
  allowlisting is session-scoped in ``ClaudeAgentOptions``, not swappable
  per call, so "this stage may only use these tools" has to become "gate
  individual tool calls via a PreToolUse hook" instead.
* Session-end finalization -- host-driven, not hook-driven. PHASE0 redesign
  item 1: there is no SessionEnd hook at all (confirmed by grep across the
  installed package). ``SessionEngine.finalize()`` is called from the
  Textual app's own teardown path and runs the review + index
  deterministically -- "deterministic beats hoping a hook fires."

Secret-scrub choke point: every transcript-derived string this module
persists to the LORE session-index-compatible JSONL (user prompts, assistant
text, tool inputs/results) routes through ``lore_core.scrub.scrub_secrets``
before it touches disk -- see ``_scrub_text``/``_scrub_json`` below. Nothing
downstream (the FTS index, a deriver digest) can be trusted to scrub on its
own; this is the one place doxa's own ingestion path is required to.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import _lore_bootstrap  # noqa: F401 -- sys.path shim, see module docstring

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

import lore_core
from lore_core import context as lore_context
from lore_core import deriver as lore_deriver
from lore_core import store as lore_store
from lore_core.config import PROJECTS_DIR, project_slug, stage_disabled
from lore_core.scrub import scrub_secrets

DEFAULT_MODEL: str | None = None  # None = whatever the CLI/session default is


@dataclass
class EngineEvent:
    """One typed event out of :meth:`SessionEngine.send` /
    :meth:`SessionEngine.start` / :meth:`SessionEngine.finalize`.

    ``type`` is one of: turn_started, text_delta, tool_call, tool_result,
    turn_done, session_done -- the six event kinds the TUI (doxa/app.py)
    switches on to build/update blocks.
    """

    type: str
    data: dict[str, Any] = field(default_factory=dict)


def _scrub_text(text: Any) -> str:
    """The one place a bare string becomes disk- or model-bound. See module
    docstring's "Secret-scrub choke point" -- callers must not persist a
    transcript-derived string without going through this (or _scrub_json for
    structured payloads)."""
    return scrub_secrets(str(text) if text is not None else "")


def _scrub_json(value: Any) -> Any:
    """Recursively scrub string leaves of a tool-call input/result payload,
    preserving structure -- so ``lore_core.store.tool_line`` (which reads
    ``inp.get("command")``/``inp.get("file_path")`` etc.) still works on the
    persisted transcript, unlike collapsing the whole payload to one scrubbed
    string."""
    if isinstance(value, str):
        return scrub_secrets(value)
    if isinstance(value, dict):
        return {k: _scrub_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_json(v) for v in value]
    return value


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
        )
    return ""


class SessionEngine:
    """One session, one Claude Agent SDK client, one LORE-compatible
    transcript. ``client_factory`` is injectable so the test suite can hand
    in a fake client that never shells out (see tests/test_engine.py)."""

    def __init__(
        self,
        cwd: str,
        model: str | None = DEFAULT_MODEL,
        session_id: str | None = None,
        client_factory: Callable[[ClaudeAgentOptions], Any] = ClaudeSDKClient,
    ) -> None:
        self.cwd = cwd
        self.model = model
        self.session_id = session_id or str(uuid.uuid4())
        self.slug = project_slug(cwd)
        self._client_factory = client_factory
        self._client: Any = None
        self._connected = False
        self._finalized = False
        self._last_refresh = time.monotonic()
        self._tool_names: dict[str, str] = {}  # tool_use_id -> name
        self._tool_started: dict[str, float] = {}  # tool_use_id -> monotonic start
        self.total_cost_usd = 0.0
        self.last_ctx_percentage: float | None = None

        transcript_dir = PROJECTS_DIR / self.slug
        transcript_dir.mkdir(parents=True, exist_ok=True)
        self.transcript_path = transcript_dir / f"{self.session_id}.jsonl"

    # -- persistence ---------------------------------------------------

    def _persist(self, record: dict) -> None:
        """Append one LORE-transcript-shaped line. Every text field on
        ``record`` must already have passed through _scrub_text/_scrub_json
        by the time it gets here -- this method does not scrub, it only
        writes, so every call site above is the one that is accountable."""
        with self.transcript_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _persist_user_text(self, text: str) -> None:
        self._persist({
            "type": "user",
            "message": {"role": "user", "content": _scrub_text(text)},
            "cwd": self.cwd,
            "sessionId": self.session_id,
            "timestamp": _iso_now(),
        })

    def _persist_assistant_blocks(self, blocks: list[dict]) -> None:
        self._persist({
            "type": "assistant",
            "message": {"role": "assistant", "content": blocks},
            "sessionId": self.session_id,
            "timestamp": _iso_now(),
        })

    def _persist_tool_results(self, blocks: list[dict]) -> None:
        self._persist({
            "type": "user",
            "message": {"role": "user", "content": blocks},
            "sessionId": self.session_id,
            "timestamp": _iso_now(),
        })

    # -- LORE hooks ------------------------------------------------------

    async def _on_user_prompt_submit(self, input_data: dict, tool_use_id, context) -> dict:
        """UserPromptSubmit -- the mid-session refresh boundary (see module
        docstring). Mirrors lore_core.context.cmd_refresh's own throttle
        logic (LORE_REFRESH_SECS), but in-memory: one long-lived process
        owns the whole session here, so a monotonic timestamp on self
        replaces cmd_refresh's per-session stamp file."""
        interval = lore_context.refresh_interval()
        if interval is None:
            return {}
        now = time.monotonic()
        if now - self._last_refresh < interval:
            return {}
        self._last_refresh = now
        snapshot = lore_context.build_context(self.cwd)
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": (
                    "LORE MEMORY REFRESH -- current as of now; supersedes any "
                    "earlier lore snapshot in this conversation.\n\n" + snapshot
                ),
            }
        }

    async def _on_pre_compact(self, input_data: dict, tool_use_id, context) -> dict:
        """PreCompact -- review the transcript-so-far before the harness
        summarizes it away (see module docstring). Fire-and-forget on a
        thread executor: worker_run() shells out to a headless `claude -p`
        call, which must not block the compaction handshake."""
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, self._run_review_sync, True)
        return {}

    async def _on_pre_tool_use(self, input_data: dict, tool_use_id, context) -> dict:
        """PreToolUse -- the tool-gating choke point (PHASE0 redesign item
        3: tool allowlisting is session-scoped, not per-call, so per-stage
        gating has to live here instead of swapping ClaudeAgentOptions
        mid-session). Phase 1 slice 2 has exactly one stage and allows
        everything; the hook exists so a future stage model has a single
        place to plug a deny/ask decision in, without changing the calling
        convention."""
        return {}

    def _run_review_sync(self, older: bool) -> None:
        """Blocking: build the deriver job for the transcript so far and run
        it. Called off the event loop (see _on_pre_compact / finalize).

        Both call sites here are automatic paths (PreCompact hook,
        host-driven finalize) -- the equivalent of a hook firing in
        lore_core.deriver.cmd_review, not an explicit `lore review` command
        -- so this honors LORE_DISABLE_REVIEW the same way cmd_review's hook
        branch does: skip silently, never block the session over it."""
        if stage_disabled("review"):
            return
        try:
            job = lore_deriver.build_review_job(
                self.transcript_path, self.slug, cwd_hint=self.cwd, older=older,
            )
            if job is None:
                return
            tmp = lore_core.ROOT / "tmp"
            tmp.mkdir(parents=True, exist_ok=True)
            jobfile = tmp / f"review-{job['session_id']}.json"
            jobfile.write_text(json.dumps(job), encoding="utf-8")
            lore_deriver.worker_run(jobfile)
        except Exception:
            # A review failure must never take the session down with it --
            # same posture as cmd_review's hook path ("never block session
            # end"/"never block the prompt loop").
            pass

    # -- lifecycle ---------------------------------------------------

    def _build_options(self) -> ClaudeAgentOptions:
        snapshot = lore_context.build_context(self.cwd)
        return ClaudeAgentOptions(
            model=self.model,
            cwd=self.cwd,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": "[LORE SNAPSHOT]\n" + snapshot,
            },
            hooks={
                "UserPromptSubmit": [HookMatcher(hooks=[self._on_user_prompt_submit])],
                "PreCompact": [HookMatcher(hooks=[self._on_pre_compact])],
                "PreToolUse": [HookMatcher(hooks=[self._on_pre_tool_use])],
            },
            include_partial_messages=True,
        )

    async def start(self) -> EngineEvent:
        """Connect the client and return the session_started event. Snapshot
        injection happens here, inside _build_options() -- see module
        docstring."""
        self._client = self._client_factory(self._build_options())
        await self._client.__aenter__()
        self._connected = True
        return EngineEvent("session_started", {
            "session_id": self.session_id, "model": self.model, "cwd": self.cwd,
        })

    async def send(self, prompt: str) -> AsyncIterator[EngineEvent]:
        """One turn: send `prompt`, stream back typed events until the
        ResultMessage. Every transcript-derived string is scrubbed before
        persistence (see module docstring)."""
        if not self._connected:
            raise RuntimeError("SessionEngine.start() must run before send()")

        self._persist_user_text(prompt)
        yield EngineEvent("turn_started", {"prompt": prompt})

        await self._client.query(prompt, session_id=self.session_id)

        pending_assistant_blocks: list[dict] = []

        async for message in self._client.receive_response():
            if isinstance(message, StreamEvent):
                ev = message.event
                if ev.get("type") == "content_block_delta":
                    delta = ev.get("delta", {})
                    text = delta.get("text") or ""
                    if text:
                        yield EngineEvent("text_delta", {"text": text})

            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        pending_assistant_blocks.append(
                            {"type": "text", "text": _scrub_text(block.text)}
                        )
                    elif isinstance(block, ToolUseBlock):
                        scrubbed_input = _scrub_json(block.input)
                        pending_assistant_blocks.append({
                            "type": "tool_use", "id": block.id,
                            "name": block.name, "input": scrubbed_input,
                        })
                        self._tool_names[block.id] = block.name
                        self._tool_started[block.id] = time.monotonic()
                        yield EngineEvent("tool_call", {
                            "id": block.id, "name": block.name, "input": scrubbed_input,
                        })
                if pending_assistant_blocks:
                    self._persist_assistant_blocks(pending_assistant_blocks)
                    pending_assistant_blocks = []

            elif isinstance(message, UserMessage):
                content = message.content if isinstance(message.content, list) else []
                tool_result_blocks = []
                for block in content:
                    if isinstance(block, ToolResultBlock):
                        started = self._tool_started.pop(block.tool_use_id, None)
                        duration_ms = int((time.monotonic() - started) * 1000) if started else None
                        result_text = _scrub_text(_tool_result_text(block.content))
                        tool_result_blocks.append({
                            "type": "tool_result", "tool_use_id": block.tool_use_id,
                            "content": result_text, "is_error": bool(block.is_error),
                        })
                        yield EngineEvent("tool_result", {
                            "id": block.tool_use_id,
                            "name": self._tool_names.get(block.tool_use_id),
                            "result_summary": result_text[:280],
                            "is_error": bool(block.is_error),
                            "duration_ms": duration_ms,
                        })
                if tool_result_blocks:
                    self._persist_tool_results(tool_result_blocks)

            elif isinstance(message, SystemMessage):
                continue  # not surfaced as a block; informational only

            elif isinstance(message, ResultMessage):
                if message.total_cost_usd:
                    self.total_cost_usd += message.total_cost_usd
                ctx_percentage = await self._safe_ctx_percentage()
                self.last_ctx_percentage = ctx_percentage
                yield EngineEvent("turn_done", {
                    "duration_ms": message.duration_ms,
                    "cost_usd": message.total_cost_usd,
                    "session_cost_usd": self.total_cost_usd,
                    "num_turns": message.num_turns,
                    "is_error": message.is_error,
                    "ctx_percentage": ctx_percentage,
                })

    async def _safe_ctx_percentage(self) -> float | None:
        get_usage = getattr(self._client, "get_context_usage", None)
        if get_usage is None:
            return None
        try:
            usage = await get_usage()
            return usage.get("percentage")
        except Exception:
            return None

    def belief_count(self) -> int:
        """Active belief count for the status bar -- same query
        lore_core.context.build_context uses to decide whether to mention
        the belief store."""
        try:
            conn = lore_store.db_connect()
            return conn.execute(
                "SELECT count(*) FROM beliefs WHERE status = 'active'"
            ).fetchone()[0]
        except Exception:
            return 0

    async def finalize(self) -> EngineEvent:
        """Host-driven session-end finalization (PHASE0 redesign item 1 --
        no SessionEnd hook exists; the host's own teardown path is the only
        deterministic place this can run). Indexes the transcript this
        session just wrote, then runs the same deriver review PreCompact
        would have -- idempotent: dedupe against already-staged proposals is
        the deriver prompt's job (see lore_core.deriver.pending_texts), and
        this only ever runs once per SessionEngine (self._finalized guards
        a second call from a double teardown path)."""
        if self._finalized:
            return EngineEvent("session_done", {"already_finalized": True})
        self._finalized = True

        indexed = 0
        try:
            conn = lore_store.db_connect()
            added, _consumed = lore_store.index_live(conn, self.transcript_path)
            indexed = added
        except Exception:
            pass

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._run_review_sync, False)

        if self._client is not None and self._connected:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception:
                pass
            self._connected = False

        return EngineEvent("session_done", {
            "indexed": indexed,
            "belief_count": self.belief_count(),
        })
