# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.codex -- a DOXA session driven by the Codex CLI instead of Claude.

The second engine, end to end: its own tab, its own transcript, its own
turns, the status bar, the rail, ``/msg`` and the peer registry. Not the
``codex:rescue`` subagent plugin -- that is a tool a Claude session calls;
this is a session.

MEASURED SURFACE (codex-cli 0.144.4, on the machine this was built on --
every claim below was run, not read):

* ``codex exec --json`` prints a JSONL event stream on stdout: one
  ``thread.started`` with the conversation id, ``turn.started``, a run of
  ``item.started`` / ``item.updated`` / ``item.completed`` frames each
  carrying a typed ``item``, then ``turn.completed`` with a ``usage``
  block. That is the whole protocol, and :func:`map_event` below is the
  whole mapping.
* ``codex exec resume <id> <prompt>`` continues a conversation. A DOXA
  Codex session is therefore ONE ``codex exec`` process PER TURN, the
  first starting a thread and every later one resuming it -- not a
  long-lived process the way ``ClaudeSDKClient`` is. That is the single
  biggest structural difference and it is why :meth:`CodexEngine.send`
  spawns, streams and reaps inside one call.
* the prompt goes in on STDIN (``-`` as the prompt argument), never in
  argv: a pasted prompt can be megabytes and ``ARG_MAX`` is not.

WHAT CODEX DOES NOT REPORT, AND WHAT DOXA THEREFORE SAYS.

* **No context window.** ``turn.completed.usage`` carries
  ``input_tokens`` / ``cached_input_tokens`` / ``output_tokens`` /
  ``reasoning_output_tokens`` and NOTHING about the size of the window
  they sit in. So this engine reports ``token_usage=True`` and
  ``context_window=False``: ``/usage`` prints the real token counts,
  ``context_usage()`` returns ``None`` so ``/context`` prints its own
  "cannot be asked" line, and the ctx chip is OMITTED rather than painting
  ``ctx —`` forever. A percentage would have to be invented from a window
  size nobody reported, and this codebase has refused that once already
  (see ``doxa.ui.labels.ctx_absolute_text``: an unknown limit is ``?``,
  never a substituted 200000).
* **No dollars.** No cost field anywhere in the stream, so
  ``total_cost_usd`` stays 0.0 and ``cost=False`` omits the chip -- a
  ``$0.0000`` chip reads as "this session is free", which is a different
  claim from "nobody said".
* **No streamed text.** ``agent_message`` arrives whole on
  ``item.completed``; there are no content deltas in this stream. It is
  still a ``text_delta`` -- just one of them per message -- because
  ``EngineEvent`` is the boundary type and does not change for an engine.

MCP, AND THE ONE FINDING THAT COST A LIVE PROBE. ``codex mcp add NAME --
COMMAND`` (equivalently ``-c mcp_servers.NAME.command=...``) genuinely
does hand an external stdio MCP server to a ``codex exec --json`` run:
verified end to end against a throwaway one-tool server -- Codex ran
``initialize`` and ``tools/list``, the model chose the tool, and the
tool's result came back in an ``mcp_tool_call`` item. It needs
``mcp_servers.<name>.default_tools_approval_mode = "approve"``; on the
default (and on ``"auto"``/``"writes"``) the call is auto-cancelled with
``error: {"message": "user cancelled MCP tool call"}`` and the model is
told it was refused.

So the answer to the spec's open question -- "does that engine simply lose
DOXA's LORE tools?" -- is **not "it cannot"**; it is **"not through this
release"**. Reaching them needs a stdio MCP server PROCESS built from
:mod:`doxa.operators`, and that process is outside the DOXA process, which
means outside :class:`doxa.gate.ToolGate`: no can_use_tool refusal, no
two-strikes disable, no ``tool_disabled`` event -- the containment
discipline that module's docstring calls "the registry describes tools,
the gate contains them" would be bypassed for exactly the engine that
needs it most. That is the second MCP projection the spec put out of
scope, and it is out of scope for a reason that is now measured rather
than assumed. Until it exists, ``mcp_tools=False`` and the session says so.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, Callable

from . import _lore_bootstrap  # noqa: F401 -- sys.path shim, see that module

from lore_core import store as lore_store
from lore_core.config import PROJECTS_DIR, project_slug
from lore_core.scrub import scrub_secrets

from . import peers as peers_mod
from . import providers as providers_mod
from .engines import (
    CODEX_ENGINE_ID,
    Engine,
    EngineCapabilities,
)
from .events import EngineEvent


#: The executable. Resolved through ``shutil.which`` at start(), never
#: assumed present: a DOXA install has no Codex dependency and a missing
#: CLI has to fail as a session that could not start, with the reason,
#: rather than as a traceback from a spawn.
CODEX_BIN = "codex"

#: Codex's three sandbox policies, verbatim from ``codex exec --help``.
#: An ALLOW-list, not a passthrough: ``self.sandbox`` is interpolated into
#: a ``-c sandbox_mode="..."`` TOML override, and an operator-supplied
#: string reaching that unchecked would be config injection into the very
#: setting that decides what the agent may write.
SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")

#: ``workspace-write`` is what a coding session actually needs, and a
#: DOXA session already runs in a worktree-per-session when that setting
#: is on. A different axis from Claude's permission modes, deliberately
#: not mapped onto them. Overridable per install (DOXA_CODEX_SANDBOX); an
#: unrecognised value falls back HERE rather than being passed through.
DEFAULT_SANDBOX = "workspace-write"

#: How long a turn's process may run before it is killed. A turn that
#: never ends would hold the pane's exclusive worker forever; the number
#: is generous because a real coding turn is minutes, not seconds.
TURN_TIMEOUT_SECS = 3600.0

#: Result text kept per tool chip, matching what SessionEngine keeps for
#: a Claude tool result (the chip shows a summary; the transcript holds
#: the whole thing).
RESULT_SUMMARY_MAX = 280


CODEX_CAPABILITIES = EngineCapabilities(
    # Verified open, deliberately not taken -- see the module docstring.
    mcp_tools=False,
    # No hook surface at all: `codex exec` has no UserPromptSubmit
    # equivalent, so the LORE snapshot cannot be injected mid-session.
    # It rides the first prompt instead (see CodexEngine._preamble).
    hooks=False,
    tool_gate=False,
    permission_modes=False,
    plugins=False,
    # `codex exec -m X` is what was ASKED for; the stream never names the
    # model it actually resolved, so self.model is a request, not a fact.
    resolved_model=False,
    context_window=False,
    token_usage=True,
    cost=False,
    # No reasoning items in `codex exec --json` on this build: the usage
    # block counts reasoning_output_tokens, the stream carries no
    # reasoning content. Declared False rather than "maybe".
    reasoning=False,
    streaming_text=False,
    # Takes effect on the NEXT turn -- each turn is its own process and
    # gets its own -m. Still a live switch from the operator's side.
    live_model_switch=True,
    resume=True,
    # No daemon hosts this engine: a Codex session lives in the TUI
    # process, so Ctrl+Q ends it rather than detaching from it.
    detachable=False,
    # DOXA's own layer, and there is no model in it.
    peer_messaging=True,
    spawn_sessions=False,
    # The belief store is shared and real; its PICKERS live on
    # SessionEngine. belief_count() below is honest and complete; the
    # pickers are absent and every call site already reaches them through
    # getattr. See doxa.engines' module docstring.
    lore_pickers=False,
)


def _truncate(text: str, limit: int = RESULT_SUMMARY_MAX) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tool_name(item: dict) -> str:
    """Codex's OWN name for a call, never a Claude name it resembles.

    A ``command_execution`` is not a ``Bash`` tool call and a
    ``file_change`` is not an ``Edit``; relabelling them would put a
    Claude vocabulary on a Codex transcript, and the transcript is
    evidence. ``doxa.diff.is_tick`` learned these two names instead --
    one predicate, two vocabularies, no translation layer."""
    kind = str(item.get("type") or "item")
    if kind == "mcp_tool_call":
        server = str(item.get("server") or "mcp")
        return f"{server}/{item.get('tool') or 'tool'}"
    return kind


def _tool_input(item: dict) -> dict:
    """The chip's input dict, per item kind. Scrubbed here because this is
    the boundary: everything below goes to a chip, a transcript line, or
    both."""
    kind = str(item.get("type") or "")
    if kind == "command_execution":
        return {"command": scrub_secrets(str(item.get("command") or ""))}
    if kind == "file_change":
        changes = item.get("changes")
        paths = [
            str(change.get("path") or "")
            for change in (changes if isinstance(changes, list) else [])
            if isinstance(change, dict)
        ]
        return {"paths": paths}
    if kind == "mcp_tool_call":
        arguments = item.get("arguments")
        return {"arguments": arguments if isinstance(arguments, dict) else {}}
    if kind == "todo_list":
        items = item.get("items")
        return {"steps": len(items) if isinstance(items, list) else 0}
    if kind == "web_search":
        return {"query": scrub_secrets(str(item.get("query") or ""))}
    return {}


def _tool_result(item: dict) -> "tuple[str, bool]":
    """``(summary, is_error)`` for a finished item."""
    kind = str(item.get("type") or "")
    status = str(item.get("status") or "")
    error = item.get("error")
    if isinstance(error, dict) and error.get("message"):
        return (_truncate(scrub_secrets(str(error["message"]))), True)
    failed = status in ("failed", "error")
    if kind == "command_execution":
        code = item.get("exit_code")
        out = _truncate(scrub_secrets(str(item.get("aggregated_output") or "")))
        failed = failed or (isinstance(code, int) and code != 0)
        return (out or f"exit {code}", failed)
    if kind == "file_change":
        changes = item.get("changes")
        rows = changes if isinstance(changes, list) else []
        return (f"{len(rows)} file(s) changed", failed)
    if kind == "mcp_tool_call":
        result = item.get("result")
        if isinstance(result, dict):
            blocks = result.get("content")
            texts = [
                str(block.get("text") or "")
                for block in (blocks if isinstance(blocks, list) else [])
                if isinstance(block, dict)
            ]
            return (_truncate(scrub_secrets("\n".join(texts))), failed)
        return ("", failed)
    if kind == "todo_list":
        items = item.get("items")
        rows = items if isinstance(items, list) else []
        done = sum(1 for row in rows if isinstance(row, dict) and row.get("completed"))
        return (f"{done}/{len(rows)} done", failed)
    return (_truncate(scrub_secrets(json.dumps(item, ensure_ascii=False))), failed)


#: Item kinds that are TOOL-shaped: they open a chip and close it.
#: ``agent_message`` is not here -- it is prose, and prose is a text_delta.
TOOL_ITEM_KINDS = frozenset({
    "command_execution", "file_change", "mcp_tool_call", "web_search",
    "todo_list", "patch_apply",
})

#: Item kinds that are the model TALKING.
TEXT_ITEM_KINDS = frozenset({"agent_message"})

#: Item kinds that are the model THINKING. Not observed on codex-cli
#: 0.144.4's `exec --json` stream (the usage block counts reasoning
#: tokens; the stream carries no reasoning content) -- kept, and kept
#: EMPTY of assumptions, so a build that starts emitting one lands in
#: reasoning_delta rather than in the unmapped bucket.
REASONING_ITEM_KINDS = frozenset({"reasoning", "agent_reasoning"})


class CodexUnavailable(RuntimeError):
    """The Codex CLI is not installed or not runnable."""


class CodexEngine:
    """One Codex session. Satisfies :class:`doxa.engines.Engine`.

    ``exec_factory(argv, cwd)`` builds the subprocess -- injectable for
    exactly the reason ``SessionEngine.client_factory`` is: the suite
    drives every mapping and lifecycle path here without a Codex install
    and without a network call."""

    #: What this handle says about itself (doxa.engines.capabilities_of).
    engine_capabilities = CODEX_CAPABILITIES

    #: The attach chip's predicate. False, and truthfully: no daemon
    #: hosts a Codex session, so there is nothing to detach from.
    detachable = False

    def __init__(
        self,
        cwd: str,
        model: "str | None" = None,
        session_id: "str | None" = None,
        *,
        resume: "str | None" = None,
        spawn_depth: int = 0,
        parent_session_id: "str | None" = None,
        exec_factory: "Callable[..., Any] | None" = None,
        sandbox: "str | None" = None,
        **_ignored: Any,
    ) -> None:
        # **_ignored, deliberately: EngineProvider.new_session takes DOXA's
        # session vocabulary and a provider ignores what its engine has no
        # use for (daemon_socket, allowed_tools, client_factory). Refusing
        # them would make every caller branch on which engine it is talking
        # to, which is the branch this whole seam exists to remove.
        self.cwd = str(cwd)
        self.model = model
        self.session_id = session_id or str(uuid.uuid4())
        self.resume = resume or None
        self.spawn_depth = max(0, int(spawn_depth or 0))
        self.parent_session_id = parent_session_id or None
        self.slug = project_slug(self.cwd)
        wanted = str(sandbox or os.environ.get("DOXA_CODEX_SANDBOX", "")).strip()
        self.sandbox = wanted if wanted in SANDBOX_MODES else DEFAULT_SANDBOX
        self._exec_factory = exec_factory or asyncio.create_subprocess_exec

        # Codex's own conversation id, learned from the first
        # ``thread.started`` frame. NOT self.session_id: DOXA's session id
        # names the transcript, the registry entry and the /search row and
        # is minted before Codex has ever run. Two ids for two things, and
        # neither is derived from the other.
        self.thread_id: "str | None" = resume or None

        # Status-bar parity with SessionEngine/EngineClient. Every one of
        # these is read UNGUARDED mid-render by doxa.session.chips, so they
        # exist from construction rather than from the first turn.
        self.total_cost_usd = 0.0
        self.last_ctx_percentage: "float | None" = None
        self.last_ctx_tokens: "int | None" = None
        self.last_ctx_max_tokens: "int | None" = None
        self.last_context_usage: "dict[str, Any] | None" = None
        self.permission_mode: str = "default"
        self.bypass_armed: bool = False
        self.account: dict = {}
        self.lore_root: "str | None" = lore_root_path()
        self.effort: "str | None" = None
        self.num_turns = 0
        self.usage_totals: "dict[str, int]" = {}

        self.peer_host: "peers_mod.PeerHost | None" = None
        self.peer_error: "str | None" = None
        self._peer_queue: "asyncio.Queue[EngineEvent]" = asyncio.Queue()
        self._pending_peer_frames: list[dict] = []
        self._disabled: list[str] = []
        self._finalized = False
        self._started = False
        self._proc: Any = None
        self._turn_closed = False
        self._tool_started: "dict[str, float]" = {}

        transcript_dir = PROJECTS_DIR / self.slug
        transcript_dir.mkdir(parents=True, exist_ok=True)
        self.transcript_path = transcript_dir / f"{self.session_id}.jsonl"

    # -- persistence ---------------------------------------------------

    def _persist(self, record: dict) -> None:
        """One LORE-transcript-shaped line, same file shape and same
        contract as SessionEngine's: every text field is already scrubbed
        by the time it arrives here."""
        try:
            with self.transcript_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            # A transcript that cannot be written must not take the turn
            # down: the session is still usable, it just will not be
            # indexed. Same posture SessionEngine takes for its review.
            pass

    def _persist_user_text(self, text: str) -> None:
        self._persist({
            "type": "user",
            "message": {"role": "user", "content": scrub_secrets(text)},
            "cwd": self.cwd,
            "sessionId": self.session_id,
            "timestamp": _iso_now(),
        })

    def _persist_assistant_text(self, text: str) -> None:
        self._persist({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": scrub_secrets(text)}],
            },
            "sessionId": self.session_id,
            "timestamp": _iso_now(),
        })

    # -- lifecycle -----------------------------------------------------

    async def start(self) -> EngineEvent:
        """Check the CLI is there, join the peer registry, and say the
        session started.

        Nothing is spawned here. A Codex turn IS a process, so there is no
        connect step to perform and nothing to hold open between turns --
        which also means this method cannot block the event loop the way
        ``spawn_daemon``'s 60-second poll can, and the v1.2.1 probes that
        assert a factory does not run on the loop thread have nothing to
        catch."""
        if shutil.which(CODEX_BIN) is None:
            raise CodexUnavailable(
                f"{CODEX_BIN!r} is not on PATH -- install the Codex CLI, or "
                "start this session on the claude engine"
            )
        self._started = True
        try:
            self.peer_host = peers_mod.PeerHost(
                session_id=self.session_id,
                cwd=self.cwd,
                on_message=self._on_peer_frame,
                on_peer_joined=self._on_peer_joined,
                on_peer_left=self._on_peer_left,
                daemon_socket=None,
                # A self-description, exactly as v1.0.2 defined it: shown,
                # never verified, and it decides nothing. What is new is
                # that `engine` finally distinguishes two real things.
                provider=providers_mod.CODEX_PROVIDER_ID,
                model=self.model,
                engine=CODEX_ENGINE_ID,
                parent_session_id=self.parent_session_id,
            )
            await self.peer_host.start()
        except Exception as exc:  # noqa: BLE001 -- peers are strictly additive
            self.peer_host = None
            self.peer_error = repr(exc)
        return EngineEvent("session_started", {
            "session_id": self.session_id, "model": self.model, "cwd": self.cwd,
        })

    async def finalize(self) -> EngineEvent:
        """End the session: drop out of the registry, index what was said.

        No deriver review, and that is a declared gap rather than an
        oversight: ``SessionEngine._run_review_sync`` builds its job from a
        transcript whose shape it also wrote, and running it over a Codex
        transcript is work this release did not verify. The transcript IS
        indexed, so ``/search`` and the session index see a Codex session
        like any other."""
        if self._finalized:
            return EngineEvent("session_done", {"already_finalized": True})
        self._finalized = True
        await self._kill_turn()
        if self.peer_host is not None:
            try:
                await self.peer_host.stop()
            except Exception:  # noqa: BLE001
                pass
            self.peer_host = None
        indexed = 0
        try:
            conn = lore_store.db_connect()
            added, _consumed = lore_store.index_live(conn, self.transcript_path)
            indexed = added
        except Exception:  # noqa: BLE001 -- an index failure never blocks quit
            pass
        return EngineEvent("session_done", {
            "indexed": indexed,
            "belief_count": self.belief_count(),
            "review": "skipped -- the LORE review is not wired for this engine",
        })

    async def _kill_turn(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.kill()
            await proc.wait()
        except Exception:  # noqa: BLE001
            pass

    # -- turns ---------------------------------------------------------

    def _argv(self, first_turn: bool) -> list[str]:
        """The command line for one turn. ONE shape for both turns.

        ``--json`` is the whole integration. ``-`` as the prompt makes
        Codex read it from stdin, which is what keeps a pasted prompt off
        argv. ``approval_policy="never"`` because there is no channel for
        an approval prompt in a non-interactive stream -- and DOXA says so
        through ``permission_modes=False`` rather than offering a /mode
        that changes nothing.

        **``-C`` and ``-s`` are NOT used, and that is a measured fix**:
        ``codex exec resume`` accepts neither (``error: unexpected
        argument '-C' found``, caught by a live second turn against the
        real CLI before this shipped, only because the exit-code branch
        below surfaces a non-zero exit instead of rendering it as a turn
        that produced no text). The working directory therefore rides the
        SUBPROCESS's own cwd -- which ``send`` sets -- and the sandbox
        rides ``-c sandbox_mode=``, a config override both subcommands
        take. One argv shape for the first turn and every resume after
        it, rather than two that can drift apart."""
        argv = [CODEX_BIN, "exec"]
        if not first_turn and self.thread_id:
            argv += ["resume", self.thread_id]
        argv += [
            "--json",
            "--skip-git-repo-check",
            "-c", 'approval_policy="never"',
            "-c", f'sandbox_mode="{self.sandbox}"',
        ]
        if self.model:
            argv += ["-m", str(self.model)]
        argv.append("-")  # the prompt arrives on stdin
        return argv

    async def send(self, prompt: str) -> AsyncIterator[EngineEvent]:
        """One turn: spawn ``codex exec``, stream its JSONL, map it.

        The generator OWNS the process for its whole lifetime and reaps it
        in a finally, so a cancelled turn (the pane's exclusive worker
        being replaced) does not leave a Codex process behind --
        ``tests/conftest.py`` reaps leaked agent subprocesses per test and
        would say so if it did."""
        if self._pending_peer_frames:
            frames, self._pending_peer_frames = self._pending_peer_frames, []
            prompt_out = peers_mod.frame_for_model(frames) + "\n\n" + prompt
        else:
            prompt_out = prompt
        if self.num_turns == 0:
            # First turn only: the peer rail's row for this session gets
            # its name from what the operator actually asked for.
            if self.peer_host is not None:
                try:
                    self.peer_host.set_title(_peer_title(prompt))
                except Exception:  # noqa: BLE001
                    pass

        self._persist_user_text(prompt_out)
        yield EngineEvent("turn_started", {
            "prompt": prompt, "peer_context": prompt_out is not prompt,
        })

        first = self.thread_id is None
        started = time.monotonic()
        proc = await self._exec_factory(
            *self._argv(first),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
        )
        self._proc = proc
        self._turn_closed = False
        code: "int | None" = None
        stderr_tail = ""
        try:
            if proc.stdin is not None:
                proc.stdin.write(prompt_out.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()
            if proc.stdout is not None:
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    for event in self._map_line(line):
                        yield event
            code = await proc.wait()
            if code and proc.stderr is not None:
                stderr_tail = _truncate(
                    scrub_secrets((await proc.stderr.read()).decode(
                        "utf-8", "replace"
                    ))
                )
        finally:
            await self._kill_turn()

        self.num_turns += 1
        if self._turn_closed:
            # The stream already said how the turn ended (turn.failed /
            # error, mapped to a turn_done with is_error). One turn, one
            # turn_done: a second would re-mark a block that is already
            # marked and double-count the turn in every status surface.
            return
        failed = bool(code)
        if failed:
            # Same reason as the turn.failed branch in map_event: an exit
            # code with a silent stream is how a missing login, a rejected
            # flag or a killed process arrives, and it has to be readable.
            yield EngineEvent("text_delta", {
                "text": f"codex: {stderr_tail or f'exec exited {code}'}",
            })
        yield EngineEvent("turn_done", {
            "duration_ms": int((time.monotonic() - started) * 1000),
            # No dollars in this stream -- None, never 0.0, because a
            # renderer that gets 0.0 prints "$0.0000" and that is a claim.
            "cost_usd": None,
            "session_cost_usd": None,
            "num_turns": self.num_turns,
            # A non-zero exit with NOTHING on the stream is the shape a
            # missing login or a rejected sandbox takes -- silence would
            # render as a turn that simply produced no text, which is the
            # one reading that sends the operator looking in the wrong
            # place. The stderr tail is carried so the block can say it.
            "is_error": failed,
            **({"error": stderr_tail or f"codex exec exited {code}"}
               if failed else {}),
            # Three Nones, and they are the point: an unreported window is
            # unknown, and every surface downstream already says so.
            "ctx_percentage": None,
            "ctx_tokens": None,
            "ctx_max_tokens": None,
        })

    def _map_line(self, raw: bytes) -> "list[EngineEvent]":
        """One stdout line -> zero or more EngineEvents.

        Split out of :meth:`send` so the whole mapping is testable without
        a subprocess, which is how every kind below was pinned."""
        try:
            frame = json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            return []
        if not isinstance(frame, dict):
            return []
        return self.map_event(frame)

    def map_event(self, frame: dict) -> "list[EngineEvent]":
        """The mapping, and every judgement call in it.

        Four Codex frames have NO EngineEvent kind, and none of them got a
        new one (the spec: a new event kind is a finding, not a field):

        ``thread.started``   the engine's conversation id changed. There is
                             no "the engine renamed itself" event and there
                             should not be -- it is consumed here, into
                             ``self.thread_id``, which is what makes the
                             NEXT turn a resume.
        ``item.updated``     progress on an open call. EngineEvent has no
                             progress kind, so it is emitted as a
                             ``tool_result`` on the SAME id: the chip
                             refreshes in place, which is what a progress
                             event would have done. A todo list ticking
                             its items off is the only producer observed.
        ``todo_list``        a plan. There is no plan kind either, and
                             Claude's own equivalent (TodoWrite) already
                             arrives as a tool call -- so it is one here
                             too, under Codex's own name.
        ``turn.failed`` /    a turn that ended badly. Folded into
        ``error``            ``turn_done`` with ``is_error`` set, which is
                             the field that already exists for it.
        """
        kind = str(frame.get("type") or "")
        if kind == "thread.started":
            thread = frame.get("thread_id")
            if isinstance(thread, str) and thread:
                self.thread_id = thread
            return []
        if kind == "turn.started":
            return []
        if kind == "turn.completed":
            self._absorb_usage(frame.get("usage"))
            return []
        if kind in ("turn.failed", "error"):
            message = frame.get("message") or frame.get("error") or kind
            self._turn_closed = True
            # The reason goes into the TRANSCRIPT, not only onto the
            # turn_done's data: `is_error` alone paints "✗ error" beside a
            # turn with no text in it, which is the reading that sends an
            # operator looking in the wrong place. text_delta is the kind
            # that already exists for "words the turn produced".
            return [EngineEvent("text_delta", {
                "text": f"codex: {_truncate(scrub_secrets(str(message)))}",
            }), EngineEvent("turn_done", {
                "duration_ms": None, "cost_usd": None, "session_cost_usd": None,
                "num_turns": self.num_turns, "is_error": True,
                "error": _truncate(scrub_secrets(str(message))),
                "ctx_percentage": None, "ctx_tokens": None, "ctx_max_tokens": None,
            })]
        if kind not in ("item.started", "item.updated", "item.completed"):
            return []

        item = frame.get("item")
        if not isinstance(item, dict):
            return []
        item_kind = str(item.get("type") or "")
        item_id = str(item.get("id") or "")

        if item_kind in TEXT_ITEM_KINDS:
            if kind != "item.completed":
                return []
            text = str(item.get("text") or "")
            if not text:
                return []
            self._persist_assistant_text(text)
            return [EngineEvent("text_delta", {"text": scrub_secrets(text)})]

        if item_kind in REASONING_ITEM_KINDS:
            if kind != "item.completed":
                return []
            text = str(item.get("text") or item.get("summary") or "")
            if not text:
                return []
            return [EngineEvent("reasoning_delta", {"text": scrub_secrets(text)})]

        if item_kind in TOOL_ITEM_KINDS:
            if kind == "item.started":
                self._tool_started[item_id] = time.monotonic()
                return [EngineEvent("tool_call", {
                    "id": item_id,
                    "name": _tool_name(item),
                    "input": _tool_input(item),
                })]
            summary, is_error = _tool_result(item)
            began = self._tool_started.get(item_id)
            duration = int((time.monotonic() - began) * 1000) if began else None
            if kind == "item.completed":
                self._tool_started.pop(item_id, None)
            return [EngineEvent("tool_result", {
                "id": item_id,
                "name": _tool_name(item),
                "result_summary": summary,
                "is_error": is_error,
                "duration_ms": duration,
            })]

        # An item kind this build has never seen. Dropped, not guessed --
        # the same rule doxa.session.runtime states for an unknown EVENT
        # type ("an engine that learns a new event type must not be able to
        # crash a client that has not learned it yet"), applied one layer
        # down.
        return []

    def _absorb_usage(self, usage: Any) -> None:
        """Accumulate ``turn.completed.usage`` into the session totals.

        Tokens only. Nothing here touches ``last_ctx_*``: input_tokens is
        what one sampling call was charged for, not what is resident in a
        window whose size nobody reported, and reading it as the latter is
        exactly the fabricated percentage this engine refuses to print."""
        if not isinstance(usage, dict):
            return
        for source, target in (
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("cached_input_tokens", "cache_read_input_tokens"),
            ("reasoning_output_tokens", "reasoning_output_tokens"),
        ):
            value = usage.get(source)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                self.usage_totals[target] = self.usage_totals.get(target, 0) + value
        if self.peer_host is not None and self.usage_totals:
            try:
                self.peer_host.update_usage(sum(self.usage_totals.values()))
            except Exception:  # noqa: BLE001
                pass

    # -- the settable surface ------------------------------------------

    async def set_model(self, model: "str | None") -> str:
        """Takes effect on the NEXT turn -- each turn is its own process.
        Reported as what it is, so /model does not claim a live switch this
        engine cannot make."""
        self.model = model or None
        if self.peer_host is not None:
            try:
                self.peer_host.set_model(self.model)
            except Exception:  # noqa: BLE001
                pass
        return f"{model or 'default'} (from the next turn)"

    async def set_permission_mode(self, mode: str) -> str:
        """Refused, by name. Codex has a sandbox policy and an approval
        policy, neither of which is Claude's permission mode, and mapping
        one onto the other would make the chip claim a posture the session
        does not have."""
        raise NotImplementedError(
            "the codex engine has no permission modes -- its posture is the "
            f"sandbox policy ({self.sandbox}), fixed for the session"
        )

    async def switch_branch(self, target: "str | None") -> dict:
        raise NotImplementedError(
            "the codex engine does not manage its own worktree"
        )

    async def answer_needs_input(self, req_id: str, answer: dict) -> bool:
        """Nothing ever asks: there is no can_use_tool callback and no
        AskUserQuestion in this stream, so no needs_input event is ever
        emitted and there is nothing to answer. False, not a raise -- a
        stale dialog from another engine's session must not explode."""
        return False

    # -- what the surfaces read ----------------------------------------

    async def context_usage(self) -> "dict[str, Any] | None":
        """None, always, and honestly: Codex reports no window. ``/context``
        prints its own "cannot be asked" line for exactly this."""
        return None

    def usage_summary(self) -> "dict[str, Any]":
        return {
            "session_id": self.session_id,
            "model": self.model,
            "num_turns": self.num_turns,
            # None, never 0.0 -- /usage omits what is absent.
            "total_cost_usd": None,
            "ctx_percentage": None,
            "ctx_tokens": None,
            "ctx_max_tokens": None,
            **self.usage_totals,
        }

    def belief_count(self) -> int:
        """The same COUNT(*) SessionEngine runs. The belief store is not
        the engine's -- it is the project's -- so a Codex tab's chip shows
        the real number rather than a zero that would read as "this
        session has no memory"."""
        try:
            conn = lore_store.db_connect()
            return conn.execute(
                "SELECT count(*) FROM beliefs WHERE status = 'active'"
            ).fetchone()[0]
        except Exception:  # noqa: BLE001
            return 0

    def disabled_tools(self) -> "list[str]":
        """Always empty, and structurally so: the two-strikes tracker lives
        in doxa.gate.ToolGate, which this engine has no way to reach (see
        ``tool_gate=False``)."""
        return list(self._disabled)

    # -- peers ---------------------------------------------------------

    def _on_peer_frame(self, frame: dict) -> None:
        self._pending_peer_frames.append(dict(frame))
        self._peer_queue.put_nowait(EngineEvent("peer_message", dict(frame)))

    def _on_peer_joined(self, info: "peers_mod.PeerInfo") -> None:
        self._peer_queue.put_nowait(EngineEvent("peer_joined", {
            "session_id": info.session_id, "title": info.title, "cwd": info.cwd,
        }))

    def _on_peer_left(self, session_id: str) -> None:
        self._peer_queue.put_nowait(EngineEvent("peer_left", {"session_id": session_id}))

    async def peer_events(self) -> AsyncIterator[EngineEvent]:
        while True:
            yield await self._peer_queue.get()

    def list_peers(self) -> list:
        return self.peer_host.list_peers() if self.peer_host is not None else []

    def peer_count(self) -> int:
        return len(self.list_peers())

    async def send_peer_message(self, target_prefix: str, text: str) -> Any:
        if self.peer_host is None:
            raise peers_mod.PeerSendError(
                "peer layer is not running in this session"
            )
        peer = peers_mod.resolve_peer(self.peer_host.list_peers(), target_prefix)
        await peers_mod.send_message(
            peer.socket_path,
            from_id=self.session_id,
            from_title=self.peer_host.title,
            body=text,
        )
        return peer


class CodexEngineProvider:
    """The registry entry. Holds no state and imports no CLI -- building
    one costs nothing, which is why doxa.engines can register it eagerly."""

    def engine_id(self) -> str:
        return CODEX_ENGINE_ID

    def engine_display_name(self) -> str:
        return "Codex (OpenAI)"

    def supports(self) -> EngineCapabilities:
        return CODEX_CAPABILITIES

    def new_session(self, **kwargs: Any) -> Engine:
        return CodexEngine(**kwargs)


# -- small shared helpers ----------------------------------------------


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


PEER_TITLE_MAX = 72


def _peer_title(prompt: str) -> str:
    """The peer registry's title for this session, from its first prompt:
    first line, internal whitespace collapsed, capped.

    The same rule ``doxa.engine._peer_title_from_prompt`` states, written
    again rather than imported -- importing it would pull
    ``claude_agent_sdk``'s 404 ms into a session that has no Claude in it,
    which is the whole reason ``doxa.events`` exists."""
    lines = [line for line in str(prompt or "").strip().splitlines() if line.strip()]
    if not lines:
        return "session"
    return " ".join(lines[0].split())[:PEER_TITLE_MAX]


def lore_root_path() -> str:
    """Where LORE keeps its store, for the ``lore_root`` attribute the
    status surfaces read off any engine handle."""
    from lore_core.config import ROOT

    return str(ROOT)
