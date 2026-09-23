# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.daemon -- the engine host process behind a Unix socket.

The Phase 2 daemon split: the ``SessionEngine`` (and with it the PeerHost,
the transcript, the LORE hooks -- everything stateful) moves OUT of the TUI
process into this daemon, and the Textual app becomes a thin client
(doxa/client.py) speaking line-JSON over the daemon's socket. Sessions then
detach and reattach without tmux: closing the TUI leaves the daemon (and the
conversation) running; ``doxa attach`` picks it back up.

Socket idioms are doxa.peers' own, reused deliberately: sockets live in
``peers.runtime_dir()`` (DOXA_RUNTIME_DIR-overridable), are chmod 0600, and
speak one JSON object per line with a hard :data:`peers.MAX_FRAME_BYTES` cap
per frame. Discovery reuses the peer registry too -- the daemon's presence
IS the session's peer presence (PeerHost is owned by the engine, which this
process hosts), extended with the ``daemon_socket`` marker field
(peers.PeerInfo.daemon_socket) so ``doxa attach`` finds sessions through the
one registry that already exists, not a second one.

Protocol (all frames are single JSON lines, <= MAX_FRAME_BYTES):

server -> client
  {"type": "hello", "proto": 1, "doxa": <version>, "session_id", "model",
   "engine", "cwd", "next_seq"}             -- version-stamped, sent on connect
  {"type": "event", "seq": N, "turn": <id|null>,
   "event": {"type": ..., "data": {...}}}   -- one EngineEvent, live or replayed
  {"type": "reply", "id": N, "ok": bool, ...}  -- response to prompt/call

client -> server
  {"type": "attach", "cursor": N | null}    -- replay ring from cursor (null =
                                               everything buffered), then live
  {"type": "prompt", "id": N, "text": ...}  -- run one turn; its events arrive
                                               on the event stream tagged with
                                               the reply's "turn" id
  {"type": "call", "id": N, "method": "status"|"peers"|"msg"|"stop"|
   "set_model"|"set_permission_mode"|"branch"|"answer_needs_input"|
   "beliefs"|"pending"|"context"|"belief_evidence"|"lore_write"|
   "approve_pending"|"reject_pending"|"belief_action_state"|
   "belief_outcome"|"retract_belief",
   "params": {...}}

Interactive permission (queue item 5): a pending ``AskUserQuestion`` or
permission request (``doxa.engine.SessionEngine._on_can_use_tool``) rides
the SAME out-of-band ``needs_input``/``needs_input_resolved`` events every
other peer-layer signal does -- queued through ``engine.peer_events()``,
fanned out by ``_peer_pump`` below like ``tool_disabled`` already is, and
landing in the ring like anything else :meth:`_publish` touches. That
ring is therefore ALSO the parking mechanism for a fully detached session
(no client attached at all when the question is asked): nothing special
has to happen for the question to survive until someone attaches --
``EventRing.since()`` replays it like any other buffered frame -- but
nobody is here to see the tab blink or hear a beep either, so
:meth:`_peer_pump` fires the desktop notification itself (focus is moot
with zero clients -- always the "unfocused" gate) in exactly that one
case, leaving the attached-client case to the TUI's own real
``app_has_focus``-gated call, the same division of labor ``notify_lore``
already has between lore_core and doxa.notify. The client answers with
``{"type": "call", "method": "answer_needs_input", "params": {"id", "answer"}}``;
the engine's own resolution fires ``needs_input_resolved`` back out on the
SAME out-of-band stream (see ``SessionEngine._wait_for_answer``), so every
attached client -- not just whichever one answered -- drops its own copy
of the dialog, same as ``model_changed`` already keeps every tab's cached
model in sync after one of them calls ``set_model``.

Replay ring: every published event gets a monotonically increasing ``seq``
and lands in a bounded ring (the ask_buffers idea reused: seq-numbered ring,
replay-from-cursor, reuse-don't-copy -- stored frames are replayed verbatim).
A reattaching client sends the cursor it last saw; the daemon replays
``seq >= cursor`` from the ring, then the live tail follows on the same
stream. The ring is in-memory only -- nothing here persists; every string
that persists anywhere still goes through the engine's scrub choke point.

Lifecycle: the daemon finalizes the session (LORE review + index, via
``SessionEngine.finalize``) when the LAST client detaches AND ``linger_secs``
passes with nobody reattaching, or immediately on an explicit stop call
(``doxa stop`` / the palette's quit-stop). SIGTERM finalizes too -- the
review gate should never be skipped just because systemd or the user got
impatient.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import sys
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable

from . import __version__
from . import engines as engines_mod
from . import notify as notify_mod
from . import worktrees as worktrees_mod
from .identity import require_session_id
from .engine import (
    BELIEF_EVIDENCE_LIMIT,
    BELIEF_LIST_LIMIT,
    GATED_MODES,
    PERMISSION_MODES,
    PENDING_LIST_LIMIT,
    EngineEvent,
    SessionEngine,
    available_modes,
)
from .peers import MAX_FRAME_BYTES, registry_dir, runtime_dir
from .promptqueue import PromptQueue, PromptQueueFull

from .events import PROTOCOL_VERSION  # noqa: F401 -- re-exported
#: The modes this daemon will not let a socket client ESCALATE into
#: without both of the conditions in
#: :meth:`SessionDaemon._gated_mode_refusal`. Derived, never spelled out:
#: ``GATED_MODES`` is what the TUI puts behind a confirmation dialog, and
#: the difference between every mode and the modes an UNARMED session may
#: hold is what launch-time arming exists to withhold
#: (``bypassPermissions``). A mode added to either set is covered here the
#: day it is added.
GATED_SOCKET_MODES = frozenset(GATED_MODES) | (
    frozenset(PERMISSION_MODES) - frozenset(available_modes(False))
)

#: How many bytes may sit unsent in ONE client's socket buffer before the
#: daemon stops treating it as a client.
#:
#: ``_publish`` writes without ``drain()``, deliberately -- it is called
#: from the turn's own event loop and awaiting one slow reader would stall
#: the turn for every other client and for the engine. The cost of not
#: awaiting is that a client which stops reading has its frames buffered
#: in the daemon's memory forever: an attached TUI whose process is
#: SIGSTOPped, a detached client on a dead network, a script that opened
#: the socket and walked away. Unbounded, that is the daemon's memory as a
#: function of somebody else's inattention.
#:
#: The bound is read off the transport's OWN write buffer rather than kept
#: in a second queue in front of it: the transport already counts exactly
#: this, and a queue of our own would only move the same bytes one layer
#: up while adding a pump task that can reorder them. Past the bound the
#: client is DROPPED (``_drop_client``, which closes it and re-arms the
#: linger) -- a client this far behind has already lost the stream's
#: ordering guarantee, and the replay ring is how it catches up when it
#: reattaches.
CLIENT_WRITE_BUFFER_MAX = 8 * 1024 * 1024

DEFAULT_LINGER_SECS = 120.0
# A freshly spawned daemon that NO client has attached to yet gets this
# claim window (>= spawn_daemon's own wait) before giving up, regardless of
# how short --linger is -- the linger knob times detach-to-finalize, not
# spawn-to-first-attach.
INITIAL_CLAIM_SECS = 120.0
RING_CAPACITY = 512

# Turn-event kinds, as doxa.engine.EngineEvent documents them. Everything
# else (peer_*, tool_disabled) is out-of-band and travels with turn=None.
TURN_EVENT_TYPES = frozenset(
    {"turn_started", "text_delta", "reasoning_delta", "tool_call", "tool_result", "turn_done"}
)

#: The RPCs that need a memory surface only ``doxa.engine.SessionEngine``
#: has, mapped to the engine member each one calls.
#:
#: MEASURED (issue #39), by building a ChatApiEngine and a CodexEngine and
#: asking ``hasattr`` for every member this module calls: these nine are
#: exactly the ones neither has. Everything else the daemon reaches for --
#: including ``belief_count`` and ``last_ctx_percentage``, which the
#: pickers' absence might suggest are missing too -- exists on all three
#: engines, so the RPCs that use them need no guard and have none.
#:
#: doxa.engines' own module docstring explains WHY the gap exists: these
#: are lore_core queries with no engine in them, living on SessionEngine
#: because that is where they were written. ``EngineCapabilities.
#: lore_pickers`` is the declaration; this table is what the daemon does
#: about it -- a typed reply the client turns into a message, never an
#: AttributeError out of a dispatch arm.
MEMORY_RPC_MEMBERS: "dict[str, str]" = {
    "beliefs": "list_beliefs",
    "belief_evidence": "belief_evidence",
    "belief_action_state": "belief_action_state",
    "belief_outcome": "record_belief_outcome",
    "retract_belief": "retract_belief",
    "lore_write": "lore_write_state",
    "approve_pending": "approve_pending",
    "reject_pending": "reject_pending",
    "pending": "list_pending",
}

#: What an engine without that surface answers with. One sentence, naming
#: the engine, so the picker that prints it says which session it asked.
NO_MEMORY_SURFACE = "engine has no memory surface"


class EventRing:
    """Bounded, seq-numbered replay ring. append() stamps the next seq and
    stores the complete wire frame; since(cursor) hands the stored frames
    back for replay -- reuse, not copy. Old frames fall off the far end;
    a cursor older than the ring simply gets everything still buffered."""

    def __init__(self, capacity: int = RING_CAPACITY) -> None:
        self._frames: deque[dict] = deque(maxlen=capacity)
        self._next_seq = 0

    @property
    def next_seq(self) -> int:
        return self._next_seq

    def append(self, turn_id: str | None, event: EngineEvent) -> dict:
        frame = {
            "type": "event",
            "seq": self._next_seq,
            "turn": turn_id,
            "event": {"type": event.type, "data": event.data},
        }
        self._next_seq += 1
        self._frames.append(frame)
        return frame

    def since(self, cursor: int | None) -> list[dict]:
        if cursor is None:
            return list(self._frames)
        return [f for f in self._frames if f["seq"] >= cursor]


def encode_frame(frame: dict) -> bytes:
    """One frame as a wire line, enforcing the peers-style 64KB cap. An
    oversize event frame degrades to a marker carrying the event type --
    the client stays in sync (seq intact) and the model-side data is never
    silently split across frames."""
    payload = (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")
    if len(payload) <= MAX_FRAME_BYTES:
        return payload
    slim = dict(frame)
    if slim.get("type") == "event":
        slim["event"] = {
            "type": slim["event"]["type"],
            "data": {"truncated": True,
                     "note": "event exceeded the frame cap; see the transcript"},
        }
    else:
        slim = {"type": slim.get("type"), "id": slim.get("id"),
                "ok": False, "error": "reply exceeded the frame cap"}
    return (json.dumps(slim, ensure_ascii=False) + "\n").encode("utf-8")


# Room left for the reply envelope around a belief page -- {"type":
# "reply", "id": N, "ok": true, "beliefs": [...], "next_offset": N} plus
# the JSON separators between rows. Generous on purpose: overshooting the
# cap costs the whole reply (encode_frame replaces it with an error), while
# undershooting costs one extra round trip on a click-only call.
BELIEF_PAGE_OVERHEAD_BYTES = 2048
# Row ceiling per page. Deliberately NOT tuned to fill a frame: measured
# against the reporting operator's live store, a full belief row averages
# ~472 bytes (avg claim 201 chars, max 300), so ~139 rows would fit 64KB
# exactly -- 100 leaves real headroom for a store whose claims run longer
# than that one's without ever depending on the byte budget below to save
# it. That budget stays as the backstop for genuinely long claims;
# whichever ceiling binds first ends the page. Cost of a smaller page is
# one more round trip on a local unix socket, on a click-only call.
BELIEF_PAGE_ROWS = 100
# Row ceiling for one page of STAGED PROPOSALS (the `pending` RPC, item 3
# of the v0.31.0 deriver-notification work). Same discipline as
# BELIEF_PAGE_ROWS and a smaller number for the same reason it is smaller
# than the belief cap: a proposal is a whole sentence or three of free
# text, materially longer than a belief claim, so fewer of them fit a
# frame comfortably. The byte backstop below still has the last word.
PENDING_PAGE_ROWS = 50


def _fit_page(
    rows: "list[Any]", offset: int, trim_one: "Callable[[Any, int], Any]"
) -> "tuple[list[Any], int | None]":
    """As many of ``rows`` as fit in one wire frame, and the offset to
    resume from (``None`` when this page is the last one). The BYTE
    backstop shared by every paged RPC here -- beliefs (v0.28.0) and
    staged proposals (v0.31.0).

    It is applied to a slice the caller has already capped by ROW count.
    That row ceiling is what normally ends a page and is set well below
    what a frame holds; this exists because both payloads are free text,
    so no row count alone can promise a fit -- and an oversize reply is
    not degraded gracefully by encode_frame, it is discarded entirely,
    which is the defect this whole mechanism removes.

    A single row bigger than the entire frame budget would otherwise page
    forever without ever emitting anything, so it is emitted ALONE, cut to
    fit by ``trim_one(row, budget)`` -- which is also responsible for
    MARKING the row as cut, because a shortened body must never be shown
    as if it were whole."""
    budget = MAX_FRAME_BYTES - BELIEF_PAGE_OVERHEAD_BYTES
    page: "list[Any]" = []
    used = 0
    for index, row in enumerate(rows):
        size = len(json.dumps(row, ensure_ascii=False).encode("utf-8")) + 1
        if size > budget:
            if page:
                return page, offset + len(page)
            return [trim_one(row, budget)], offset + 1
        if used + size > budget and page:
            return page, offset + index
        page.append(row)
        used += size
    return page, None


def _trim_belief(belief: dict, budget: int) -> dict:
    """One oversize belief cut to fit, marked ``claim_truncated`` -- the
    picker's row is ellipsized anyway, and the detail view says so out
    loud rather than showing a short claim as if it were whole."""
    trimmed = dict(belief)
    claim = str(trimmed.get("claim") or "")
    # Bytes, not characters: the cap is a wire cap. Cut on the encoded
    # form and decode back, dropping any partial rune.
    keep = claim.encode("utf-8")[: max(0, budget - 512)]
    trimmed["claim"] = keep.decode("utf-8", errors="ignore")
    trimmed["claim_truncated"] = True
    return trimmed


def _trim_pending(item: "dict | str", budget: int) -> "dict | str":
    """One oversize staged proposal cut to fit, marked ``text_truncated``.

    Item V made a proposal a RECORD rather than a bare string (it has to
    carry its pending id and the fields the proposed verdict is computed
    from), so the marker is now a flag on the row, exactly like
    :func:`_trim_belief`'s, instead of the bare "…" a string had no room
    to explain. A string still trims the old way -- see
    ``doxa.ui.labels.as_proposal`` for why one can still arrive."""
    if not isinstance(item, dict):
        keep = str(item).encode("utf-8")[: max(0, budget - 512)]
        return keep.decode("utf-8", errors="ignore") + "…"
    trimmed = dict(item)
    field = "text" if trimmed.get("text") else "claim"
    keep = str(trimmed.get(field) or "").encode("utf-8")[: max(0, budget - 512)]
    trimmed[field] = keep.decode("utf-8", errors="ignore")
    trimmed["text_truncated"] = True
    return trimmed


def _trim_evidence(row: dict, budget: int) -> dict:
    """One oversize evidence row cut to fit -- see :func:`_trim_belief`.
    A deriver note is capped at 300 chars by lore_core itself, so this
    exists for the same reason the other two do rather than because it is
    expected to fire."""
    trimmed = dict(row)
    keep = str(trimmed.get("note") or "").encode("utf-8")[: max(0, budget - 512)]
    trimmed["note"] = keep.decode("utf-8", errors="ignore")
    trimmed["note_truncated"] = True
    return trimmed


def _fit_belief_page(
    beliefs: "list[dict]", offset: int
) -> "tuple[list[dict], int | None]":
    """:func:`_fit_page` for the ``beliefs`` RPC."""
    return _fit_page(beliefs, offset, _trim_belief)


def _fit_pending_page(
    items: "list[dict]", offset: int
) -> "tuple[list[dict], int | None]":
    """:func:`_fit_page` for the ``pending`` RPC."""
    return _fit_page(items, offset, _trim_pending)


def _fit_evidence_page(
    rows: "list[dict]", offset: int
) -> "tuple[list[dict], int | None]":
    """:func:`_fit_page` for the ``belief_evidence`` RPC (item V) -- a
    third CALLER of the one shared byte budget, not a third budget."""
    return _fit_page(rows, offset, _trim_evidence)


def daemon_socket_path(session_id: str) -> Path:
    """Same AF_UNIX path-length discipline as peers.PeerHost: session-id
    prefix + pid, and readers never derive it -- they read it verbatim from
    the registry entry's daemon_socket field."""
    return runtime_dir() / f"daemon-{session_id[:8]}-{os.getpid()}.sock"


def _write_buffer_size(writer: asyncio.StreamWriter) -> int:
    """How many bytes are queued but unsent for this client, or 0 when the
    transport cannot say.

    0 on an unknown transport rather than a large number: this value gates
    a DROP, and a transport that does not report its buffer is not evidence
    that a client is misbehaving. Test doubles and non-socket transports
    land here."""
    transport = getattr(writer, "transport", None)
    sizer = getattr(transport, "get_write_buffer_size", None)
    if not callable(sizer):
        return 0
    try:
        return int(sizer())
    except Exception:  # noqa: BLE001 -- a transport mid-close counts as 0
        return 0


class SessionDaemon:
    """One detachable session: hosts an engine, serves the socket.

    ``engine_factory(cwd, session_id, daemon_socket)`` builds the engine --
    injectable so the test suite runs the whole daemon over a fake SDK
    client. The default is :meth:`_build_engine`, which builds whichever
    engine ``engine_id`` names through the :mod:`doxa.engines` registry.

    NOT a SessionEngine host specifically, since v1.13.0 (issue #39). The
    RPC surface below is still the one SessionEngine grew, and a second
    engine does not implement all of it -- see :data:`MEMORY_RPC_MEMBERS`,
    which answers the calls it cannot serve with a typed error instead of
    an AttributeError.
    """

    def __init__(
        self,
        cwd: str | None = None,
        model: str | None = None,
        session_id: str | None = None,
        linger_secs: float = DEFAULT_LINGER_SECS,
        engine_factory: "Callable[[str, str, str], Any] | None" = None,
        ring_capacity: int = RING_CAPACITY,
        base_branch: str | None = None,
        resume: str | None = None,
        spawn_depth: int = 0,
        parent_session_id: str | None = None,
        task: str | None = None,
        lore: "bool | None" = None,
        engine_id: "str | None" = None,
    ) -> None:
        self.cwd = str(cwd or os.getcwd())
        self.model = model
        # Both ids are checked HERE, at the one door argv comes through
        # (``__main__`` below hands ``--session-id``/``--resume`` straight
        # to this constructor). Downstream every one of them becomes a
        # filename -- the transcript ``<id>.jsonl``, the registry entry
        # ``<id>.json``, the peer socket ``peer-<id[:8]>-<pid>.sock``, the
        # daemon log -- so an id that is not a name would put this
        # session's files wherever it pointed. A ValueError here refuses
        # to start the daemon, which is the correct outcome: there is no
        # partial version of "run as this session".
        self.session_id = (
            require_session_id(session_id) if session_id else str(uuid.uuid4())
        )
        # v0.56.0 (/resume): this daemon CONTINUES an existing conversation
        # rather than starting one. The id is not new -- see spawn_daemon,
        # which passes the SAME string as both session_id and resume, so
        # the transcript file, the registry entry and the /search row all
        # stay the one session they already were.
        self.resume = require_session_id(resume, "resume id") if resume else None
        # Item S #1 (`doxa new --branch <name>`): the ref the session's OWN
        # worktree forks from, plumbed here from spawn_daemon's subprocess
        # arg. cli.py has already validated it exists before ever spawning
        # this process -- _apply_worktree still passes it through
        # worktrees.create's own (permissive) resolution, never trusting
        # the flag blindly.
        self.base_branch = base_branch
        # Session spawn (doxa.session_ops). All three ride the command
        # line for the reason spawn_daemon's docstring already states
        # about base_branch: a daemon is a separate process and its argv
        # is the only channel that reaches SessionDaemon.__init__ at all.
        #
        # spawn_depth is the one the caps actually enforce on, and its
        # being argv-borne rather than registry-derived IS the design --
        # see session_ops.MAX_SPAWN_DEPTH. It is clamped at 0 here because
        # a negative depth handed in by a hand-run daemon must read as
        # "root", never as extra headroom.
        self.spawn_depth = max(0, int(spawn_depth or 0))
        self.parent_session_id = parent_session_id or None
        # The first prompt this session runs by itself, with nobody
        # attached (see _run_initial_task). Only ever set by a spawn.
        self.task = (task or "").strip() or None
        # Does THIS session have memory (doxa.engine.LORE_ENV)? Threaded
        # the same argv way base_branch and spawn_depth are, and for the
        # same mechanical reason: a daemon is a separate process and its
        # command line is the only channel that reaches this constructor.
        # None means "take the config row's answer" -- which is ON -- so a
        # session nobody said anything about is unchanged.
        #
        # It has to be per SESSION rather than per machine because
        # doxa.fleet runs memory-on and memory-off agents side by side in
        # one run: a shared belief store is a coordination channel the
        # message ledger cannot see, and the experiment's whole
        # measurement is the communication structure.
        self.lore = lore
        # WHICH engine this daemon hosts (issue #39). Argv-borne like
        # everything above it, and for the same mechanical reason: a
        # daemon is a separate process and its command line is the only
        # channel that reaches this constructor. Normalised here so
        # `--engine ""` and `--engine "  "` both mean "the operator named
        # nothing", exactly as doxa.engines.get reads them.
        self.engine_id = (
            (engine_id or "").strip().lower() or engines_mod.DEFAULT_ENGINE_ID
        )
        self.linger_secs = linger_secs
        self.socket_path = daemon_socket_path(self.session_id)
        self._engine_factory = engine_factory or self._build_engine
        self.engine: Any = None
        self.ring = EventRing(ring_capacity)
        # A reconnecting renderer skips the event ring when it restores the
        # persisted transcript, but still needs any unanswered approval.
        self._pending_remote_inputs: dict[str, dict] = {}
        self.ready = asyncio.Event()
        self._done = asyncio.Event()
        self._server: asyncio.AbstractServer | None = None
        self._clients: set[asyncio.StreamWriter] = set()
        self._remote_clients: dict[asyncio.StreamWriter, str] = {}
        self._had_client = False
        self._turn_task: asyncio.Task | None = None
        # Mid-turn prompt queue (see doxa.promptqueue): ONE FIFO per
        # session, shared by every attached client -- the daemon is the
        # single source of truth a second tab must agree with, so this
        # lives here rather than on any one connection.
        self._prompt_queue = PromptQueue()
        self._linger_task: asyncio.Task | None = None
        # Set only by _linger_then_stop once it hands shutdown off to its
        # own task (see that method's comment) -- _cancel_linger never
        # touches this one, which is the whole point of it existing.
        self._shutdown_task: asyncio.Task | None = None
        self._pump_task: asyncio.Task | None = None
        self._stopping = False
        # Worktree-per-session (#3, doxa.worktrees): computed once, before
        # the engine is built, so the engine (and everything downstream --
        # the "hello" frame, EngineClient.cwd, SessionPane's GitLine) just
        # sees a cwd that happens to be a worktree. Cached so a headless
        # shutdown (linger/signal) and an explicit "stop" reply (which
        # needs the message inline, see _handle_call) never run the git
        # cleanup twice.
        self._worktree_note: "str | None" = None
        self._worktree_done = False

    # -- lifecycle ---------------------------------------------------

    def _build_engine(self, cwd: str, sid: str, dsock: str) -> Any:
        """The default ``engine_factory``: whichever engine
        :attr:`engine_id` names.

        Claude keeps its LITERAL construction rather than going through
        the registry's ``new_session``, and that is deliberate: this call
        is the one place ``daemon_socket``, ``resume``, ``spawn_depth``,
        ``parent_session_id`` and ``lore`` all reach a SessionEngine, and
        routing it through a ``**kwargs`` hop would make losing one of
        them a silent behaviour change rather than a TypeError.

        A second engine is built through :func:`doxa.engines.get`, with
        the same session vocabulary. Every provider's ``new_session``
        takes keyword arguments only and ignores what its engine has no
        use for (:class:`doxa.engines.EngineProvider`), so the argument
        list is DOXA's, not any one engine's -- but two of them are
        ignored by the vendor engines rather than honoured, and pretending
        otherwise is what this comment exists to prevent:

        * ``lore`` -- doxa.vendors.ChatApiEngine honours it (no snapshot,
          no lore_* operator, a gate allowing only what was offered). An
          engine that carries no ``lore`` attribute does not, and
          :meth:`_status` reports ``lore: true`` for it, which is what the
          session actually does; the startup warning below names it.
        * ``daemon_socket`` -- threaded, and load-bearing: the registry
          entry's ``daemon_socket`` field IS how :func:`spawn_daemon`
          learns this daemon is ready and how ``doxa attach`` finds it.
          An engine that dropped it would register as a session nothing
          could ever attach to."""
        if self.engine_id == engines_mod.DEFAULT_ENGINE_ID:
            return SessionEngine(
                cwd=cwd, model=self.model, session_id=sid, daemon_socket=dsock,
                resume=self.resume, spawn_depth=self.spawn_depth,
                parent_session_id=self.parent_session_id,
                lore=self.lore,
            )
        return engines_mod.get(self.engine_id).new_session(
            cwd=cwd, model=self.model, session_id=sid, daemon_socket=dsock,
            resume=self.resume, spawn_depth=self.spawn_depth,
            parent_session_id=self.parent_session_id,
            lore=self.lore,
        )

    def _apply_worktree(self) -> None:
        """Substitute ``self.cwd`` for its own worktree BEFORE the engine
        is built -- a no-op (returns None, leaves cwd alone) when the
        setting is off, ``cwd`` is not a git repo, or worktree creation
        fails for any reason: worktree-per-session is strictly additive,
        never a reason a session fails to start.

        A RESUME never creates one (v0.56.0). ``--resume`` is resolved by
        the CLI against ITS store, whose directories are keyed by the cwd
        the session ran in; substituting a freshly-created worktree here
        would hand the CLI a cwd the original conversation was never
        recorded under, and turn a resume into "No conversation found with
        session ID". The cwd a resume is launched with is the cwd LORE
        recorded for that session -- which IS its worktree, when it had
        one -- so the right move is to enter it as given, not to make a
        second one beside it."""
        if self.resume:
            return
        path = worktrees_mod.create(
            self.cwd, self.session_id, base_branch=self.base_branch
        )
        if path:
            self.cwd = path

    def _finalize_worktree(self) -> "str | None":
        """Worktree cleanup at REAL finalize (never at a mere detach --
        see doxa.worktrees.finalize's docstring). Runs at most once;
        later callers (a headless _shutdown after an RPC-driven one, or
        vice versa) get the cached result. A "kept" message always also
        goes to the daemon's own log -- the one channel guaranteed to
        exist even when finalize runs with no client attached."""
        if self._worktree_done:
            return self._worktree_note
        self._worktree_done = True
        try:
            note = worktrees_mod.finalize(self.cwd)
        except Exception:  # noqa: BLE001 -- cleanup bookkeeping must never
            note = None    # block a shutdown that is already underway
        self._worktree_note = note
        if note:
            print(f"doxa: {note}", file=sys.stderr)
        return note

    async def serve(self) -> None:
        """Start the engine, serve the socket, run until finalized (linger
        expiry, explicit stop, or SIGTERM)."""
        registry_dir()  # ensure runtime dirs exist with clamped perms
        with contextlib.suppress(OSError):
            self.socket_path.unlink()
        self._apply_worktree()
        self.engine = self._engine_factory(
            self.cwd, self.session_id, str(self.socket_path)
        )
        # --no-lore on an engine that has no memory switch. Said out loud,
        # in the one channel a headless session always has, rather than
        # swallowed. doxa.vendors.ChatApiEngine honours the flag; an engine
        # that carries no `lore` attribute does not, and _status reports
        # lore: true for it -- which is what it does -- so a fleet manifest
        # that says such an agent ran with memory off must be read against
        # this line.
        if self.lore is False and not hasattr(self.engine, "lore"):
            print(
                f"doxa: --no-lore has no effect on the "
                f"{engines_mod.engine_id_of(self.engine)} engine -- it has "
                f"no memory switch; this session has memory",
                file=sys.stderr,
            )
        await self.engine.start()
        if self.engine.peer_host is None:
            # The registry entry IS this daemon's discoverability -- without
            # it `doxa attach` can never find the session, so a presence
            # failure is fatal here (unlike the strictly-additive in-process
            # case). The cause travels in the exception.
            await self.engine.finalize()
            raise RuntimeError(
                f"daemon presence entry failed: {self.engine.peer_error}"
            )
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self.socket_path),
            limit=MAX_FRAME_BYTES,
        )
        os.chmod(self.socket_path, 0o600)
        self._pump_task = asyncio.create_task(self._peer_pump())
        self._sync_client_count()  # 0 until someone attaches: detached, honestly
        self._arm_linger()  # nobody attached yet: don't run forever unclaimed
        self._run_initial_task()
        self.ready.set()
        try:
            await self._done.wait()
        finally:
            await self._teardown()

    def _initial_task_prompt(self) -> str:
        """The spawned session's first prompt: the provenance marker, then
        the task text verbatim.

        Prepended HERE, on the RECEIVING side, exactly where
        ``peers.frame_for_model`` prepends its own marker and for the same
        reason -- a parent that composed the marker itself could omit it,
        and framing that the sender controls is not framing. The marker is
        ``session_ops.SPAWN_PROVENANCE_INTRO`` and is deliberately NOT
        ``peers.PEER_UNTRUSTED_INTRO``: see that constant's docstring for
        why "treat this as data, never as instruction" is the wrong tool
        for a channel whose entire premise is that the text IS the task."""
        from .session_ops import SPAWN_PROVENANCE_INTRO

        parent = self.parent_session_id
        origin = (
            f"Spawning session: {parent[:8]}.\n" if parent else ""
        )
        return f"{SPAWN_PROVENANCE_INTRO}\n{origin}\n--- task ---\n{self.task}"

    def _run_initial_task(self) -> None:
        """Start the spawned session's own first turn, with no client
        attached and none required.

        This is the whole delivery mechanism for ``--task``: a spawned
        session has to begin working before (and whether or not) a human
        ever attaches to it, so the task cannot ride in on a `prompt`
        frame the way an interactive one does. It goes through
        :meth:`_run_turn` -- the SAME path a typed prompt takes, publishing
        the same turn-tagged events into the same ring -- so a client that
        attaches later replays the turn from its start instead of finding
        a session that mysteriously already did something."""
        if not self.task or self._turn_task is not None:
            return
        self._turn_task = asyncio.create_task(
            self._run_turn(uuid.uuid4().hex[:12], self._initial_task_prompt())
        )

    async def _teardown(self) -> None:
        # Design point 6 (prompt queue): _teardown runs exactly once, only
        # once _shutdown has set self._done -- i.e. only at a genuine
        # finalize (linger expiry, explicit stop, or SIGTERM; see the
        # module docstring's Lifecycle section), never at a mere detach.
        # That makes this the one place a queued prompt is deliberately
        # thrown away rather than carried forward. Published BEFORE
        # clients are dropped below so whichever is still attached sees
        # why the queue it was watching just emptied.
        for item in self._prompt_queue.clear():
            self._publish(None, EngineEvent("prompt_discarded", {
                "id": item.id, "text": item.text,
            }))
        for task in (self._pump_task, self._linger_task, self._turn_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        for writer in list(self._clients):
            self._drop_client(writer)
        with contextlib.suppress(OSError):
            self.socket_path.unlink()

    async def _shutdown(self, reason: str) -> None:
        """Finalize exactly once (LORE review + index run inside the
        engine's own finalize, worktree remove-or-keep run inside
        _finalize_worktree), then let serve() unwind.

        The body runs under try/finally so ``_done`` is set on EVERY way
        out, including an exception one. ``_stopping`` was already set
        True above (the re-entry guard just before it), and ``_stopping``
        True with ``_done`` never set is exactly how a daemon gets
        stranded: it refuses every future shutdown attempt while never
        finishing this one. ``_linger_then_stop`` protects the common way
        that happens -- a client's ``_cancel_linger`` racing this call,
        landing a CancelledError on whichever await below is in flight,
        which the ``suppress(Exception)`` around finalize does NOT catch
        (CancelledError is a BaseException) -- by handing shutdown to its
        own task once armed, so nothing outside this method can cancel it
        again. This ``finally`` is the second, unconditional layer: even a
        future caller that reintroduces that race, or a genuinely
        unexpected exception, still leaves ``_done`` set and ``serve()``
        able to unwind."""
        if self._stopping:
            return
        self._stopping = True
        try:
            if self._turn_task is not None and not self._turn_task.done():
                self._turn_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._turn_task
            if self.engine is not None:
                with contextlib.suppress(Exception):
                    await self.engine.finalize()
            self._finalize_worktree()  # cached: a no-op if the stop RPC
            # below already ran it to embed the "kept" note in its reply.
        finally:
            self._done.set()

    # -- linger ------------------------------------------------------

    def _arm_linger(self) -> None:
        if self._stopping or self._linger_task is not None:
            return
        # Before the first client has EVER attached, wait the (generous)
        # claim window; after a detach, wait exactly the linger knob.
        delay = (
            self.linger_secs if self._had_client
            else max(self.linger_secs, INITIAL_CLAIM_SECS)
        )
        self._linger_task = asyncio.create_task(self._linger_then_stop(delay))

    def _cancel_linger(self) -> None:
        if self._linger_task is not None:
            self._linger_task.cancel()
            self._linger_task = None

    async def _linger_then_stop(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if self._clients:
            return
        if self._turn_task is not None and not self._turn_task.done():
            # A turn is RUNNING. _shutdown would cancel it mid-flight, and
            # for a spawned session (doxa.session_ops) that is the normal
            # case, not an edge one: it starts working immediately and
            # nobody may ever attach, so the unclaimed-linger timer would
            # otherwise kill the delegate exactly once per spawn. Wait
            # another full interval instead -- "unclaimed" was always
            # meant to mean idle-and-unwatched, and a session doing the
            # work it was asked for is not idle.
            self._linger_task = None
            self._arm_linger()
            return
        # Past this point shutdown MUST run to completion no matter who
        # calls _cancel_linger next: a client can attach the instant the
        # sleep above returns, and _cancel_linger cancels self._linger_task
        # unconditionally with no idea that this coroutine has moved past
        # the sleep and into _shutdown. Cancelling THIS task at that point
        # would deliver CancelledError to whatever _shutdown is awaiting
        # (_turn_task, engine.finalize()) -- see _shutdown's docstring for
        # why that strands the daemon. Clearing _linger_task before
        # spawning the shutdown task (no `await` runs between the two, so
        # nothing can interleave and see the old task reference) means
        # _cancel_linger has nothing left to touch: shutdown runs as its
        # own task, one this method's caller never holds a handle to again.
        self._linger_task = None
        self._shutdown_task = asyncio.create_task(
            self._shutdown("linger expired with no client attached")
        )
        # Await it, so whoever awaited THIS coroutine sees the stop as done
        # when it returns (the contract test_session_spawn pins: an idle
        # unclaimed session has stopped by the time _linger_then_stop
        # returns, not one loop tick later). This does not reopen the race
        # above: cancelling a task that is awaiting another Task cancels
        # only the wait -- the awaited Task keeps running to completion --
        # and _linger_task is already None, so _cancel_linger cannot reach
        # this coroutine anyway. Swallowing the CancelledError matches the
        # sleep branch: a cancelled waiter has nothing left to do.
        try:
            await self._shutdown_task
        except asyncio.CancelledError:
            return

    # -- event fan-out -----------------------------------------------

    async def _peer_pump(self) -> None:
        assert self.engine is not None
        async for ev in self.engine.peer_events():
            if ev.type == "needs_input" and not self._clients:
                # Parked with nobody attached at all: the ring below still
                # carries it for a later attach to replay, but a fully
                # detached session has no window to blink and no operator
                # watching it -- the desktop notification is the one signal
                # available, so it fires here rather than waiting on a TUI
                # that may not exist for hours. Always the "unfocused" gate
                # -- there is no focus concept with zero clients.
                notify_mod.notify_needs_input(
                    False, self._notify_label(), self._needs_input_summary(ev.data),
                )
            self._publish(None, ev)

    def _notify_label(self) -> str:
        return Path(self.cwd).name or self.session_id[:8]

    @staticmethod
    def _needs_input_summary(data: dict) -> str:
        if data.get("kind") == "ask_user":
            questions = data.get("questions") or []
            if questions and isinstance(questions[0], dict):
                return str(questions[0].get("question") or "question")
            return "question"
        return str(data.get("input_summary") or data.get("tool_name") or "")

    def _publish(
        self, turn_id: str | None, event: EngineEvent,
        exclude: "asyncio.StreamWriter | None" = None,
    ) -> None:
        """Fan out to every attached client except `exclude`, still
        recording the frame in the ring for everyone (including
        `exclude`) to replay later.

        `exclude` exists for exactly one caller: _handle_prompt's
        prompt_queued broadcast. The requesting connection already
        learns it was queued from its OWN reply (EngineClient.send
        yields a prompt_queued event built from that reply, so the
        caller can render it without waiting on this stream) --
        publishing it here too would additionally land it on that same
        client's peer_events() (_handle_event routes anything with no
        matching "turn" tag there), which _peer_pump reads
        unconditionally and would render a second time. Every OTHER
        attached client has no such reply to read and depends entirely
        on this broadcast, so it is never skipped for them."""
        if event.type == "needs_input":
            request_id = event.data.get("id")
            if isinstance(request_id, str):
                self._pending_remote_inputs[request_id] = dict(event.data)
        elif event.type == "needs_input_resolved":
            self._pending_remote_inputs.pop(event.data.get("id"), None)
        frame = self.ring.append(turn_id, event)
        payload = encode_frame(frame)
        for writer in list(self._clients):
            if writer is exclude:
                continue
            try:
                if _write_buffer_size(writer) > CLIENT_WRITE_BUFFER_MAX:
                    # Not reading, and far enough behind that continuing to
                    # buffer for it is this daemon's memory rather than
                    # that client's problem. See CLIENT_WRITE_BUFFER_MAX.
                    self._drop_client(writer)
                    continue
                writer.write(payload)
            except Exception:
                self._drop_client(writer)

    def _sync_client_count(self) -> None:
        """Keep the presence entry's attached-client count honest -- it is
        what tells every other session whether this one is detached."""
        host = getattr(self.engine, "peer_host", None)
        if host is not None:
            with contextlib.suppress(Exception):
                host.set_client_count(len(self._clients))

    def _remote_identity(self) -> str | None:
        """Status-bar label for attached browser drivers, bounded in width."""
        identities = sorted(set(self._remote_clients.values()))
        if not identities:
            return None
        shown = ", ".join(identities[:3])
        return shown + f" (+{len(identities) - 3})" if len(identities) > 3 else shown

    def _drop_client(self, writer: asyncio.StreamWriter) -> None:
        self._clients.discard(writer)
        remote_left = self._remote_clients.pop(writer, None)
        self._sync_client_count()
        if remote_left is not None:
            self._publish(None, EngineEvent("remote_driver_changed", {
                "identity": self._remote_identity(),
            }))
        with contextlib.suppress(Exception):
            writer.close()
        if not self._clients and self._had_client and not self._stopping:
            self._arm_linger()

    # -- client protocol ---------------------------------------------

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        assert self.engine is not None
        writer.write(encode_frame({
            "type": "hello",
            "proto": PROTOCOL_VERSION,
            "doxa": __version__,
            "session_id": self.session_id,
            "model": self.engine.model,
            # WHICH engine is behind this socket (issue #39). Beside
            # "model" because it is the same kind of answer and needed at
            # the same moment: a client that painted the belief pickers
            # for a session whose engine has none would be offering a
            # surface the daemon answers with NO_MEMORY_SURFACE. Read off
            # the live handle (doxa.engines.engine_id_of), not off this
            # daemon's argv -- an injected engine_factory is what the
            # suite uses, and the handle is the thing that is actually
            # here.
            "engine": engines_mod.engine_id_of(self.engine),
            # Beside "model" for the same reason it is: both are answers
            # the client needs before it paints, and EngineClient.attach
            # runs its first status refresh under contextlib.suppress --
            # so a refresh that fails would otherwise leave the mode chip
            # showing this client's seeded guess rather than the daemon's
            # fact. A safety indicator must not have a guess-shaped hole.
            "permission_mode": getattr(self.engine, "permission_mode", None),
            # Beside the mode for the same reason it is: a reattaching
            # client must not paint a cycle that includes a mode this
            # daemon's CLI would refuse.
            "bypass_armed": bool(getattr(self.engine, "bypass_armed", False)),
            "cwd": self.cwd,
            "next_seq": self.ring.next_seq,
        }))
        try:
            await writer.drain()
            while True:
                try:
                    line = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    break  # oversize frame: drop the client, not the daemon
                if not line:
                    break
                try:
                    frame = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(frame, dict):
                    continue
                await self._handle_frame(frame, writer)
                if self._stopping:
                    break
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self._drop_client(writer)

    async def _handle_frame(self, frame: dict, writer: asyncio.StreamWriter) -> None:
        ftype = frame.get("type")
        if ftype == "attach":
            cursor = frame.get("cursor")
            cursor = int(cursor) if isinstance(cursor, (int, float)) else None
            # No awaits between computing the replay set, joining the live
            # broadcast set, and buffering the replay writes: that ordering
            # (with the loop unable to interleave _publish in between) is
            # what guarantees replay-then-tail with no gap and no overlap.
            replay = self.ring.since(cursor)
            self._clients.add(writer)
            remote_login = frame.get("remote_login")
            if (
                isinstance(remote_login, str)
                and 0 < len(remote_login) <= 256
                and remote_login.strip()
                and not any(ord(char) < 32 for char in remote_login)
            ):
                self._remote_clients[writer] = remote_login.strip()
            self._had_client = True
            self._sync_client_count()
            self._cancel_linger()
            for f in replay:
                writer.write(encode_frame(f))
            await writer.drain()
            if writer in self._remote_clients:
                self._publish(None, EngineEvent("remote_driver_changed", {
                    "identity": self._remote_identity(),
                }))
        elif ftype == "prompt":
            await self._handle_prompt(frame, writer)
        elif ftype == "call":
            await self._handle_call(frame, writer)

    async def _handle_prompt(self, frame: dict, writer: asyncio.StreamWriter) -> None:
        req_id = frame.get("id")
        text = str(frame.get("text") or "")
        if not text.strip():
            await self._reply(writer, req_id, ok=False, error="empty prompt")
            return
        if self._turn_task is not None and not self._turn_task.done():
            # A turn is running: NEVER refuse and NEVER let it hang (the
            # two defects the old "a turn is already running" error used
            # to cause together -- see doxa.promptqueue's module
            # docstring). Enqueue instead, bounded (PromptQueueFull is the
            # one case that DOES refuse, with a clear reason, never a
            # silent drop), and acknowledge immediately.
            try:
                item = self._prompt_queue.enqueue(text)
            except PromptQueueFull as exc:
                await self._reply(writer, req_id, ok=False, error=str(exc))
                return
            position = self._prompt_queue.position(item.id) or len(self._prompt_queue)
            # Every OTHER attached client learns this the same
            # "everyone learns it" way model_changed already does -- a
            # second tab on this daemon must not disagree about what is
            # queued. `writer` itself is excluded: it learns the SAME
            # fact from the reply below, which EngineClient.send turns
            # into its own prompt_queued event -- see _publish's own
            # docstring for why publishing it here too would double it.
            self._publish(None, EngineEvent("prompt_queued", {
                "id": item.id, "text": text, "position": position,
            }), exclude=writer)
            await self._reply(
                writer, req_id, ok=True, queued=True,
                position=position, queue_id=item.id,
            )
            return
        turn_id = uuid.uuid4().hex[:12]
        # Claim the turn slot and BUFFER the reply bytes synchronously (no
        # await between the busy-check above and here): a racing prompt from
        # another client cannot slip past the busy-check, and the reply hits
        # the socket before the turn task -- which first runs on the next
        # loop tick -- can publish any tagged event. The client therefore
        # always learns its turn id ahead of the first event that carries it.
        self._turn_task = asyncio.create_task(self._run_turn(turn_id, text))
        await self._reply(writer, req_id, ok=True, turn=turn_id)

    async def _run_turn(self, turn_id: str, text: str) -> None:
        assert self.engine is not None
        try:
            async for ev in self.engine.send(text):
                self._publish(turn_id, ev)
        except asyncio.CancelledError:
            # Cancelled from outside (_shutdown/_teardown, both of which
            # own this task's lifetime and are off limits here): the
            # queue must NOT advance -- starting another turn on an
            # engine that is on its way down would fight the shutdown
            # this cancellation is part of. _advance_queue is therefore
            # unreachable below, exactly like SessionEngine.send()'s own
            # except clause for the in-process path.
            raise
        except Exception as exc:  # noqa: BLE001 -- a turn failure must reach the client
            self._publish(turn_id, EngineEvent("turn_done", {
                "is_error": True,
                "error": f"{type(exc).__name__}: {exc}",
                "session_cost_usd": self.engine.total_cost_usd,
            }))
        self._advance_queue()

    def _advance_queue(self) -> None:
        """The moment a turn reaches turn_done (success or a handled
        per-turn error -- never reached on cancellation, see _run_turn's
        except clause above), the next queued prompt, if any, becomes the
        next turn automatically. Every attached client learns which
        prompt just started the same way it learned it was queued."""
        item = self._prompt_queue.pop_next()
        if item is None:
            # Leave _turn_task pointing at the turn that just finished.
            # Every busy check in this class asks `is not None and not
            # .done()`, so a finished task is never mistaken for a running
            # one -- and the reference is a CONTRACT: a spawned session's
            # first turn can finish within the one loop tick between
            # serve() setting `ready` and a waiter observing it, and
            # test_session_spawn awaits `_turn_task` to prove the turn ran
            # at all. Clearing it here made that turn look like it never
            # started.
            return
        self._publish(None, EngineEvent("prompt_dequeued", {
            "id": item.id, "text": item.text,
        }))
        next_turn_id = uuid.uuid4().hex[:12]
        self._turn_task = asyncio.create_task(self._run_turn(next_turn_id, item.text))

    async def _handle_call(self, frame: dict, writer: asyncio.StreamWriter) -> None:
        assert self.engine is not None
        req_id = frame.get("id")
        method = frame.get("method")
        params = frame.get("params") or {}
        # The memory surface, checked ONCE here rather than nine times
        # below: every arm in MEMORY_RPC_MEMBERS calls a member only
        # SessionEngine has, and an engine that lacks it gets a typed
        # reply -- the same {"ok": false, "error": ...} shape a refused
        # peer send or a bad mode name already produces, which every
        # EngineClient wrapper for these calls already turns into an
        # EngineClientError or an error string. `status` is deliberately
        # NOT in that table: it must answer for every engine.
        member = MEMORY_RPC_MEMBERS.get(str(method))
        if member is not None and getattr(self.engine, member, None) is None:
            await self._reply(
                writer, req_id, ok=False,
                error=f"{NO_MEMORY_SURFACE}: "
                      f"{engines_mod.engine_id_of(self.engine)} has no "
                      f"{member}()",
            )
            return
        if method == "status":
            await self._reply(writer, req_id, ok=True, status=self._status())
        elif method == "peers":
            await self._reply(
                writer, req_id, ok=True,
                peers=[vars(p) for p in self.engine.list_peers()],
            )
        elif method == "msg":
            from .peers import PeerSendError
            try:
                peer = await self.engine.send_peer_message(
                    str(params.get("target") or ""), str(params.get("text") or ""),
                )
                await self._reply(writer, req_id, ok=True, peer=vars(peer))
            except PeerSendError as exc:
                await self._reply(writer, req_id, ok=False, error=str(exc))
        elif method == "set_model":
            # /model over the daemon split: a control request to the SDK
            # client the daemon already owns -- no reconnect, so the
            # transcript, this ring and every attached client survive it.
            try:
                model = await self.engine.set_model(params.get("model") or None)
            except Exception as exc:  # noqa: BLE001 -- the client shows it
                await self._reply(writer, req_id, ok=False,
                                  error=f"{type(exc).__name__}: {exc}")
                return
            # Every attached client learns the new model, not just the one
            # that asked -- two tabs on one daemon must not disagree.
            self._publish(None, EngineEvent("model_changed", {"model": model}))
            await self._reply(writer, req_id, ok=True, model=model)
        elif method == "set_permission_mode":
            # /mode over the daemon split (v0.42.0): the SAME shape as
            # set_model above, because it is the same kind of thing -- an
            # SDK control request against the client the daemon owns, so
            # no reconnect and nothing in the ring disturbed.
            #
            # The DAEMON owns the operation; the client sends a name.
            # SessionEngine.set_permission_mode validates the NAME; this
            # method decides whether THIS connection, right now, may
            # escalate into a gated one -- see _gated_mode_refusal. A
            # daemon still cannot show a confirmation dialog, so it does
            # not pretend to: it narrows the gated modes to the two
            # conditions under which the confirmation the TUI showed is
            # the only explanation for the request.
            wanted = str(params.get("mode") or "")
            refusal = self._gated_mode_refusal(wanted, writer)
            if refusal is not None:
                await self._reply(writer, req_id, ok=False, error=refusal)
                return
            try:
                mode = await self.engine.set_permission_mode(wanted)
            except Exception as exc:  # noqa: BLE001 -- the client shows it
                await self._reply(writer, req_id, ok=False,
                                  error=f"{type(exc).__name__}: {exc}")
                return
            # Every attached client learns it, not just the one that asked
            # -- and this event matters more than model_changed does: a
            # second tab on this daemon whose chip still says "default"
            # while the session no longer asks about anything is a status
            # line actively lying about a safety property.
            self._publish(
                None, EngineEvent("permission_mode_changed", {"mode": mode})
            )
            await self._reply(writer, req_id, ok=True, mode=mode)
        elif method == "branch":
            # /branch over the daemon split (item S #4): the SAME shape as
            # set_model above -- the daemon owns the git operation
            # (doxa.worktrees, against self.cwd, the session's own
            # worktree), the client gets a plain reply plus, on a
            # successful SWITCH, a broadcast every attached client (not
            # just whichever one asked) picks up -- same "everyone learns
            # it" rule model_changed already keeps every tab in sync with.
            # No arg lists (read-only, nothing to broadcast); an arg
            # switches.
            target = params.get("target")
            target = str(target).strip() if target else None
            if target:
                result = await asyncio.to_thread(
                    worktrees_mod.switch_base, self.cwd, target
                )
                if result.get("ok"):
                    self._publish(
                        None, EngineEvent("base_changed", {"base": result.get("base")})
                    )
            else:
                result = await asyncio.to_thread(
                    worktrees_mod.branch_status, self.cwd
                )
            await self._reply(writer, req_id, ok=True, result=result)
        elif method == "beliefs":
            # Item 3's beliefs-picker chip: lazy, click-only -- the status
            # bar's own belief_count() already rides in every "status"
            # reply above and must stay free; this is the heavier claim-
            # text SELECT, a separate call so a session that never opens
            # the picker never pays for it.
            #
            # PAGED since v0.28.0, and that is a defect fix, not a
            # refinement: reported as "clicking on 'beliefs' chip leads to
            # error message 'too much for a message'". One reply carrying
            # every active belief WITH its claim text runs past
            # MAX_FRAME_BYTES the moment a store gets real (the operator's
            # had ~517), and encode_frame's non-event branch replaces an
            # oversize reply wholesale with {"ok": false, "error": "reply
            # exceeded the frame cap"} -- so the picker did not open at
            # all, it printed that. The client asks for a window and gets
            # back however much of it FITS plus the offset to resume from
            # (see EngineClient.list_beliefs, which loops until
            # next_offset is None and hands the app one complete list --
            # the parity SessionEngine.list_beliefs' unpaged return
            # requires). Truncating claim text here instead was the other
            # option and was rejected: it would make the two engines
            # return different data for the same call.
            offset = max(0, int(params.get("offset") or 0))
            limit = max(1, int(params.get("limit") or BELIEF_LIST_LIMIT))
            fetch = min(limit, BELIEF_PAGE_ROWS)
            beliefs = await self.engine.list_beliefs(limit=fetch, offset=offset)
            page, next_offset = _fit_belief_page(beliefs, offset)
            if next_offset is None and len(beliefs) == fetch:
                # The SQL window ended this page, not the store -- there may
                # be more rows behind it, so say so rather than reporting a
                # short list as complete.
                next_offset = offset + len(page)
            await self._reply(
                writer, req_id, ok=True, beliefs=page, next_offset=next_offset,
            )
        elif method == "belief_evidence":
            # Item V: ONE belief's evidence trail, fetched on demand.
            #
            # This is how a picker over hundreds of beliefs shows what
            # each was derived from without blowing the frame cap: the
            # trail is never part of the `beliefs` page (which carries a
            # COUNT of it instead), and only the belief a reader actually
            # expanded is ever asked for. One page, capped at
            # BELIEF_EVIDENCE_LIMIT rows by the engine and put through the
            # same shared byte budget as everything else here -- a trail
            # that did not fit says so (`evidence_truncated`) rather than
            # arriving short and silent. No client-side paging loop: a
            # capped single-belief trail is not an unbounded list.
            bid = int(params.get("belief_id") or 0)
            limit = max(1, int(params.get("limit") or BELIEF_EVIDENCE_LIMIT))
            rows = await self.engine.belief_evidence(bid, limit=limit)
            page, next_offset = _fit_evidence_page(rows, 0)
            await self._reply(
                writer, req_id, ok=True, evidence=page,
                evidence_truncated=next_offset is not None,
            )
        elif method == "belief_action_state":
            # v0.48.0: a NARROWER capability than `lore_write`, asked of
            # the side that holds the store for the same reason -- an
            # outcome row and a status transition need only that lore_core
            # still has record_outcome/belief_supersede, not the 0.36.0
            # provenance ledger an approved WRITE needs.
            await self._reply(
                writer, req_id, ok=True, state=self.engine.belief_action_state(),
            )
        elif method == "belief_outcome":
            # ONE verdict against ONE belief, recorded as source="user" --
            # the same path lore_core.beliefs.cmd_outcome takes, because a
            # human selecting a verdict in a DOXA row IS that path. No list
            # parameter, same rule approve_pending follows.
            error = await self.engine.record_belief_outcome(
                int(params.get("belief_id") or 0),
                str(params.get("event") or ""),
                params.get("note"),
            )
            await self._reply(writer, req_id, ok=not error, error=error)
        elif method == "retract_belief":
            error = await self.engine.retract_belief(
                int(params.get("belief_id") or 0),
                str(params.get("reason") or "retracted from DOXA"),
            )
            await self._reply(writer, req_id, ok=not error, error=error)
        elif method == "lore_write":
            # Item V's read-only degradation, asked of the side that holds
            # the store. A detached session's lore_core is the DAEMON's,
            # not the client process's -- an attached terminal could have a
            # perfectly modern wheel installed while the daemon it is
            # driving loaded a stale plugin checkout, so the picker has to
            # ask the writer, not itself.
            await self._reply(
                writer, req_id, ok=True, state=self.engine.lore_write_state(),
            )
        elif method in ("approve_pending", "reject_pending"):
            # Item V: the WRITE half of the review gate, which v0.31.0
            # deliberately did not ship. LORE 0.36.0 concluded that review
            # (the write gate + provenance ledger, issue #43), and these
            # two drive lore_core's own approve path so an entry approved
            # from DOXA carries LORE's own `via approved` label.
            #
            # ONE id per call. There is no list parameter here and adding
            # one would be the whole security property gone: the gate
            # exists because a human looked at THIS proposal.
            pid = str(params.get("pid") or "")
            runner = (self.engine.approve_pending if method == "approve_pending"
                      else self.engine.reject_pending)
            error = await runner(pid)
            await self._reply(writer, req_id, ok=not error, error=error)
        elif method == "pending":
            # `/pending` over the daemon split -- since item V, the read
            # half of a surface that also has a write half (the two RPCs
            # above). It stays a separate call from them on purpose: this
            # one is what a glance costs, and it is safe to run without
            # having decided anything.
            #
            # PAGED from the day it shipped rather than after a report,
            # because the beliefs RPC already paid for that lesson: staged
            # proposals are free text of unbounded length, one reply
            # carrying all of them can pass MAX_FRAME_BYTES, and
            # encode_frame answers an oversize non-event reply by
            # discarding it wholesale in favour of an error the picker
            # would print where the list should have been.
            offset = max(0, int(params.get("offset") or 0))
            limit = max(1, int(params.get("limit") or PENDING_LIST_LIMIT))
            fetch = min(limit, PENDING_PAGE_ROWS)
            texts = await self.engine.list_pending(limit=fetch, offset=offset)
            page, next_offset = _fit_pending_page(texts, offset)
            if next_offset is None and len(texts) == fetch:
                # The window ended this page, not the staging area -- there
                # may be more behind it, so say so rather than reporting a
                # short list as complete.
                next_offset = offset + len(page)
            await self._reply(
                writer, req_id, ok=True, pending=page, next_offset=next_offset,
            )
        elif method == "context":
            # `/context` (item K) over the daemon split. The daemon owns the
            # SDK client, so it is the only side that can issue the CLI's
            # get_context_usage control request at all. The reply is already
            # narrowed to what /context renders AND to what fits
            # MAX_FRAME_BYTES (doxa.engine.context_breakdown drops the SDK's
            # pre-rendered gridRows and caps every list at CONTEXT_ROW_CAP),
            # so unlike `beliefs` and `pending` this one needs no pager.
            #
            # `usage` is null -- not {} -- when this session genuinely
            # cannot be asked; the pane says so rather than rendering an
            # empty breakdown as if it were a measured one.
            usage = await self.engine.context_usage()
            await self._reply(writer, req_id, ok=True, usage=usage)
        elif method == "answer_needs_input":
            # The resolution's own needs_input_resolved broadcast comes
            # from the ENGINE side (SessionEngine._wait_for_answer's
            # finally), over the same peer_events stream _peer_pump
            # already fans out -- nothing extra to publish here.
            ok = await self.engine.answer_needs_input(
                str(params.get("id") or ""), dict(params.get("answer") or {}),
            )
            await self._reply(writer, req_id, ok=ok)
        elif method == "queue":
            # `/queue`'s bare listing -- everything still waiting, FIFO
            # order. Small and bounded (PROMPT_QUEUE_MAXLEN), unlike the
            # beliefs/pending pagers just above, so it needs none of
            # their paging.
            #
            # BOTH queues, since issue #39: a prompt that arrived while a
            # peer-started turn was running is in the ENGINE's queue (see
            # _queued_count), and a /queue that listed only this one told
            # the user their prompt had vanished. This daemon's items
            # first -- they are the ones this class will dequeue first.
            await self._reply(
                writer, req_id, ok=True,
                queue=self._prompt_queue.snapshot() + await self._engine_queue(),
            )
        elif method == "cancel_queued":
            # `/queue`'s cancel half. The SAME "everyone learns it" rule
            # prompt_queued above follows: every attached client sees the
            # cancellation, not just whichever one asked for it.
            item = self._prompt_queue.cancel(str(params.get("id") or ""))
            if item is not None:
                self._publish(None, EngineEvent("prompt_cancelled", {
                    "id": item.id, "text": item.text,
                }))
                await self._reply(writer, req_id, ok=True)
                return
            # Not ours: the id may name a prompt in the ENGINE's queue,
            # which /queue now lists. SessionEngine.cancel_queued
            # publishes its own prompt_cancelled onto the out-of-band
            # stream _peer_pump already fans out, so nothing is published
            # here -- doing it too would draw the cancellation twice.
            canceller = getattr(self.engine, "cancel_queued", None)
            if canceller is not None and await canceller(
                str(params.get("id") or "")
            ):
                await self._reply(writer, req_id, ok=True)
                return
            await self._reply(
                writer, req_id, ok=False,
                error="no such queued prompt (already started, "
                      "cancelled, or discarded)",
            )
        elif method == "stop":
            # Worktree cleanup runs BEFORE the ack (fast, git-only) so a
            # "kept" note can ride in the SAME reply -- unlike
            # engine.finalize()'s LORE review below, which stays
            # ack-first/finalize-after so a slow review can never make a
            # `doxa stop` / quit-stop call itself time out.
            note = self._finalize_worktree()
            await self._reply(writer, req_id, ok=True, stopping=True, note=note)
            await self._shutdown("explicit stop")
        else:
            await self._reply(writer, req_id, ok=False,
                              error=f"unknown method: {method!r}")

    def _gated_mode_refusal(
        self, wanted: str, writer: asyncio.StreamWriter
    ) -> "str | None":
        """Why this connection may not escalate into ``wanted`` right now,
        or None when it may.

        Two conditions, both of which an escalation into
        :data:`GATED_SOCKET_MODES` must satisfy:

        * **The connection completed the attach handshake.** A client is in
          ``_clients`` only after it sent an ``attach`` frame
          (``_handle_frame``), which is what ``EngineClient.start`` does
          before it can call anything. A bare connection that reads the
          hello and issues a ``call`` has skipped it, and a gated mode is
          the one operation where "some process on this machine opened the
          socket" is not a good enough account of who asked.
        * **No turn is running and none is queued.** The model runs only
          inside a turn, so a session that is mid-turn is exactly the
          window in which a request to stop asking about tool calls did
          not come from the person at the keyboard. A user who wants the
          mode changed can change it when the turn ends; a turn cannot
          wait for the user, which is why the refusal goes this way round.

        Neither condition applies to a DE-escalation or to a no-op: the
        gate is on entering a gated mode, and getting out of one must
        never be the operation that is hard to perform. Nothing here is
        the confirmation dialog -- that is a UI act and still lives in
        ``doxa.session.commands._cmd_mode``; this is what makes the dialog
        the only remaining way a gated mode gets chosen.
        """
        if wanted not in GATED_SOCKET_MODES:
            return None
        if wanted == getattr(self.engine, "permission_mode", None):
            return None  # already there: not an escalation
        if writer not in self._clients:
            return (
                f"{wanted} needs an attached client; this connection never "
                "sent an attach frame"
            )
        if self._running() or self._queued_count():
            return (
                f"{wanted} cannot be set while a turn is running or queued; "
                "let the session go idle and ask again"
            )
        return None

    def _running(self) -> bool:
        """Is ANY turn running in this session -- this daemon's or the
        engine's own.

        Two turn sources, one answer. The daemon starts a turn per
        `prompt` frame and holds it in ``_turn_task``; the engine starts
        one of its own when an arriving peer message wakes the session
        (``SessionEngine._on_peer_frame``) and when its queue advances,
        and neither of those passes through this class at all. Reading
        only ``_turn_task`` reported a session mid-turn as idle, which is
        what ``doxa.fleet``'s quiescence wait believed.

        ``getattr`` rather than an attribute, because the surface is
        OPTIONAL: doxa.vendors.ChatApiEngine and doxa.codex.CodexEngine
        never start a turn of their own (their ``_on_peer_frame`` only
        queues an event for the pane; nothing in either calls ``send``),
        so False is the measured answer for them, not a guess."""
        if self._turn_task is not None and not self._turn_task.done():
            return True
        return bool(getattr(self.engine, "turn_running", False))

    def _queued_count(self) -> int:
        """How many prompts are waiting, across BOTH queues -- see
        :meth:`_running` for why there are two.

        A prompt reaches the engine's queue rather than this one whenever
        the engine was already busy with a turn the daemon did not start:
        a peer message arriving mid-turn queues there, and so does a
        `prompt` frame that arrives while a peer-started turn is running
        (the daemon hands it to ``engine.send``, which enqueues it and
        yields ``prompt_queued``)."""
        counter = getattr(self.engine, "queued_count", None)
        engine_queued = int(counter()) if callable(counter) else 0
        return len(self._prompt_queue) + engine_queued

    async def _engine_queue(self) -> "list[dict[str, str]]":
        """The ENGINE's own queued prompts, or an empty list for an engine
        that has no queue. Delegation rather than one shared queue: the
        two are filled by different code paths (this class's
        ``_handle_prompt``, and the engine's own peer/advance paths) and
        merging them would mean rewriting ``SessionEngine.send``'s
        queueing contract, which is out of scope for a fix that only has
        to stop `/queue` from hiding half the answer."""
        lister = getattr(self.engine, "list_queue", None)
        if lister is None:
            return []
        try:
            return list(await lister())
        except Exception:  # noqa: BLE001 -- a listing failure hides prompts,
            return []      # it must never break the RPC that asked

    def _status(self) -> dict:
        assert self.engine is not None
        return {
            "session_id": self.session_id,
            "model": self.engine.model,
            # Same field, same source, as the hello frame's -- a client
            # that reconnects to a daemon mid-life learns it from here
            # rather than only at connect.
            "engine": engines_mod.engine_id_of(self.engine),
            # v0.42.0: a REATTACHING client has to be told the truth about
            # this one before it paints anything. Every other field here is
            # a number that is merely stale until the next refresh; a mode
            # chip defaulting to "default" on a session actually running
            # unattended in bypassPermissions would be a status line
            # misreporting a safety property to the person who just came
            # back to check on it.
            "permission_mode": getattr(self.engine, "permission_mode", None),
            # Whether THIS daemon's CLI was spawned with the arming flag
            # (v0.58.0). A client cannot work this out for itself -- it did
            # not build the argv -- and every surface it paints derives
            # from it, so it rides the status reply like the mode does.
            "bypass_armed": bool(getattr(self.engine, "bypass_armed", False)),
            "cwd": self.cwd,
            # Identity surface for the client's status cache: the account
            # block the CLI reported at connect (may be {}), and where the
            # LORE store lives daemon-side.
            "account": getattr(self.engine, "account", None) or {},
            "lore_root": getattr(self.engine, "lore_root", None),
            "total_cost_usd": self.engine.total_cost_usd,
            "ctx_percentage": getattr(self.engine, "last_ctx_percentage", None),
            # Item X (ctx absolute): the absolute halves of that same
            # reading, so a reattaching client's very first status refresh
            # has them without waiting for a turn to end.
            "ctx_tokens": getattr(self.engine, "last_ctx_tokens", None),
            "ctx_max_tokens": getattr(self.engine, "last_ctx_max_tokens", None),
            # /usage over the split: the engine's own token accounting,
            # cached client-side like every other status value.
            "usage": self.engine.usage_summary(),
            "belief_count": self.engine.belief_count(),
            "disabled_tools": self.engine.disabled_tools(),
            "peers": [vars(p) for p in self.engine.list_peers()],
            "clients": len(self._clients),
            "remote_driver": self._remote_identity(),
            "pending_inputs": list(self._pending_remote_inputs.values()),
            # Is a turn running RIGHT NOW, and how many are waiting. The
            # daemon is the only thing that knows: a client can see the
            # turn IT dispatched, but a turn started by an arriving peer
            # message (doxa.peers.peer_inbound_turns_enabled) begins with
            # no client involved at all, and a harness waiting for a fleet
            # to go quiet would call that session idle while it was
            # answering another agent. See doxa.fleet's quiescence wait,
            # which is the caller this exists for.
            #
            # BOTH sides, since issue #39, and that is a defect fix: this
            # daemon's _turn_task is only the turns the daemon started.
            # A peer-started turn runs on the ENGINE's own task
            # (SessionEngine._on_peer_frame -> _run_queued_turn), and a
            # prompt submitted while one is running lands in the ENGINE's
            # PromptQueue, not this one -- so a session busy answering
            # another agent used to report running=false, queued=0 and
            # doxa.fleet's is_quiet called it idle.
            "running": self._running(),
            "queued": self._queued_count(),
            # Whether this session has memory. Published for the same
            # reason bypass_armed is: a client cannot work it out for
            # itself (it did not build the argv), and a status bar that
            # showed a memory chip for a session with no memory would be
            # reporting a capability the session does not have.
            "lore": bool(getattr(self.engine, "lore", True)),
        }

    async def _reply(
        self, writer: asyncio.StreamWriter, req_id: Any, ok: bool, **extra: Any
    ) -> None:
        frame = {"type": "reply", "id": req_id, "ok": ok, **extra}
        try:
            writer.write(encode_frame(frame))
            await writer.drain()
        except Exception:
            self._drop_client(writer)


def spawn_daemon(
    cwd: str,
    model: str | None = None,
    linger_secs: float = DEFAULT_LINGER_SECS,
    wait_secs: float = 60.0,
    base_branch: str | None = None,
    resume: str | None = None,
    spawn_depth: int = 0,
    parent_session_id: str | None = None,
    task: str | None = None,
    env: "dict[str, str] | None" = None,
    lore: bool = True,
    engine: "str | None" = None,
) -> "tuple[str, str]":
    """Spawn a detached daemon for ``cwd`` and wait for its registry entry.

    Returns (session_id, daemon_socket). The session id is minted HERE so
    the spawner can poll the one registry surface for exactly its own entry
    -- no scanning race with concurrently spawned sessions. Daemon stdout/
    stderr go to a per-session log under the runtime dir (diagnostics only;
    the engine never prints transcript text).

    ``resume`` (v0.56.0) means this daemon continues an EXISTING
    conversation, and then the id is not minted at all: the resumed id IS
    the session id. That equality is the feature, not a shortcut -- the
    transcript file, the registry entry, the tab record and the /search
    row are all keyed by session id, and minting a new one here would
    fork one conversation into two everywhere except inside the model's
    own context. Callers must check the session is not already RUNNING
    before asking for this (doxa.app.resume_session does); two daemons
    registered under one id is not a state this registry has an answer
    for.

    ``base_branch`` (item S #1, ``doxa new --branch``) rides along as a
    subprocess arg -- the daemon is a separate process, so this is the
    only way anything reaches ``SessionDaemon.__init__``'s own
    ``base_branch`` parameter.

    ``spawn_depth`` / ``parent_session_id`` / ``task`` (v1.3.0,
    ``doxa.session_ops``) ride the same channel for the same reason, and
    each is appended ONLY when it says something: a session a human
    started produces a byte-identical argv to the one this function built
    before v1.3.0, which is the same discipline the bypass arming flag
    follows -- a new capability must not change the command line of every
    session that does not use it.

    ``engine`` (issue #39) names which engine the daemon hosts, and is
    appended ONLY when it says something other than the default -- the
    same rule every flag above it follows, so a Claude session's argv is
    byte-identical to the one this function built before the daemon could
    host anything else. The id is NOT validated here: the child validates
    it through :func:`doxa.engines.get` and exits 2 with the registry's
    listing, and a second copy of that check in this process would be a
    second place for the registry's contents to be described.

    ``env`` replaces the child's whole environment instead of inheriting
    this process's, and exists for :mod:`doxa.fleet`: a fleet run gives
    every session its own ``DOXA_HOME`` (so the run's ledger is the whole
    file, with nothing to filter) and its own ``DOXA_RUNTIME_DIR`` (so the
    registry a fleet discovers is the fleet), and decides PER AGENT whether
    memory is on. None -- the default -- inherits, which is what every
    existing caller does and what a human-started session must keep doing.

    Note what changes with it: ``reg`` and ``log_path`` below are then
    resolved against the CHILD's runtime dir, not this process's, because
    the entry this function polls for is the one the child will write. That
    is the whole reason :func:`doxa.peers.runtime_dir` grew an ``env``
    parameter.

    This function is BLOCKING and stays that way: it polls with
    ``time.sleep(0.1)`` for up to ``wait_secs``. Callers on an event loop
    (``session_ops._spawn_after_confirm``) hand it to ``asyncio.to_thread``
    rather than reintroducing the stall v0.95.0 removed."""
    import subprocess
    import time as _time

    # Checked before it becomes four filenames (the log below, the
    # registry entry this function polls for, and the child's own
    # transcript and socket) -- see SessionDaemon.__init__, which checks
    # the same two ids again on the far side of argv.
    session_id = require_session_id(resume, "resume id") if resume else str(uuid.uuid4())
    reg = registry_dir(env)
    log_path = runtime_dir(env) / f"daemon-{session_id[:8]}.log"
    cmd = [
        sys.executable, "-m", "doxa.daemon",
        "--cwd", cwd, "--session-id", session_id,
        "--linger", str(linger_secs),
    ]
    if model:
        cmd += ["--model", model]
    if base_branch:
        cmd += ["--base-branch", base_branch]
    if resume:
        cmd += ["--resume", resume]
    if spawn_depth:
        cmd += ["--spawn-depth", str(int(spawn_depth))]
    if parent_session_id:
        cmd += ["--parent-session-id", str(parent_session_id)]
    if task:
        cmd += ["--task", str(task)]
    # Appended ONLY when memory is off, which is the same discipline every
    # flag above follows: a session that does not use a capability produces
    # a byte-identical argv to the one this function built before the
    # capability existed.
    if not lore:
        cmd += ["--no-lore"]
    # Same discipline, same reason (see the docstring): the default
    # engine adds nothing to the command line.
    if engine and engine.strip().lower() != engines_mod.DEFAULT_ENGINE_ID:
        cmd += ["--engine", engine.strip().lower()]
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, cwd=cwd, env=env,
        )
    entry_path = reg / f"{session_id}.json"
    deadline = _time.monotonic() + wait_secs
    while _time.monotonic() < deadline:
        if proc.poll() is not None:
            tail = ""
            with contextlib.suppress(OSError):
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            raise RuntimeError(
                f"doxa daemon exited during startup (code {proc.returncode}). "
                f"Log tail:\n{tail}"
            )
        if entry_path.exists():
            with contextlib.suppress(OSError, ValueError):
                data = json.loads(entry_path.read_text(encoding="utf-8"))
                dsock = data.get("daemon_socket")
                if dsock and Path(dsock).exists():
                    return session_id, str(dsock)
        _time.sleep(0.1)
    raise RuntimeError(f"doxa daemon did not become ready within {wait_secs:.0f}s")


def install_signal_handlers(
    daemon: SessionDaemon, loop: "asyncio.AbstractEventLoop | None" = None
) -> None:
    """SIGTERM and SIGINT both mean the same thing to a session daemon:
    finalize gracefully NOW (LORE review + index via engine.finalize), then
    exit -- an impatient user's Ctrl+C aimed at the daemon must never skip
    the review gate any more than systemd's SIGTERM does. Split out of
    _amain so the graceful-signal contract is testable in-process."""
    loop = loop or asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(
            sig, lambda: asyncio.ensure_future(daemon._shutdown("signal"))
        )


async def _amain(args: argparse.Namespace) -> int:
    if args.lore is False:
        # This process hosts exactly ONE session, so its environment IS a
        # per-session setting here -- which is why this is done in the
        # daemon's own entry point and nowhere a second session could
        # share it. LORE's op-log sync pushes and pulls the same store the
        # switch exists to disconnect this session from, and it reads only
        # an environment variable (doxa.lore_sync.sync_disabled), so this
        # is the one channel that reaches it.
        os.environ["LORE_DISABLE_SYNC"] = "1"
    daemon = SessionDaemon(
        cwd=args.cwd,
        model=args.model,
        session_id=args.session_id,
        linger_secs=args.linger,
        base_branch=args.base_branch,
        resume=args.resume,
        spawn_depth=args.spawn_depth,
        parent_session_id=args.parent_session_id,
        lore=args.lore,
        task=args.task,
        engine_id=args.engine,
    )
    install_signal_handlers(daemon)
    await daemon.serve()
    return 0


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(prog="doxa-daemon")
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--linger", type=float, default=DEFAULT_LINGER_SECS)
    parser.add_argument("--base-branch", default=None,
                        help="item S: fork the session worktree from this "
                             "ref instead of the launch cwd's checkout")
    parser.add_argument("--resume", default=None,
                        help="v0.56.0: continue the conversation with this "
                             "session id instead of starting a new one "
                             "(spawn_daemon passes the same value as "
                             "--session-id -- a resume keeps its id)")
    parser.add_argument("--spawn-depth", type=int, default=0,
                        help="v1.3.0 (doxa.session_ops): how deep this "
                             "session sits in a spawn chain. 0 -- the "
                             "default, and what a human-started session "
                             "always gets -- is a root. The depth cap is "
                             "enforced on THIS value because a process "
                             "carries it from birth, unlike a registry "
                             "chain that loses its ancestors when they are "
                             "reaped")
    parser.add_argument("--parent-session-id", default=None,
                        help="v1.3.0: the session that asked for this one "
                             "(peers.PeerInfo.parent_session_id). Display "
                             "and lineage only -- never enforcement")
    parser.add_argument("--no-lore", dest="lore", action="store_false",
                        default=None,
                        help="run this session with memory OFF: no LORE "
                             "snapshot in its system prompt, no per-turn "
                             "refresh, the lore_* tools absent from the "
                             "model's tool list rather than refusing, and "
                             "no writes -- no beliefs, no staged "
                             "proposals, no session index. PER SESSION: "
                             "doxa.fleet runs memory-on and memory-off "
                             "agents in one experiment, because a shared "
                             "belief store is a coordination channel the "
                             "message ledger cannot see. Omitted means "
                             "'whatever the config row says', which is on")
    parser.add_argument("--task", default=None,
                        help="v1.3.0: the first prompt this session runs "
                             "by itself, before anyone attaches. The "
                             "provenance marker is prepended by the daemon, "
                             "not by whoever passed this")
    parser.add_argument("--engine", default=engines_mod.DEFAULT_ENGINE_ID,
                        help="issue #39: which engine this daemon hosts "
                             "(doxa.engines; default %(default)s). Omitted "
                             "means the default, which is what every "
                             "session got before the daemon could host a "
                             "second engine")
    args = parser.parse_args(argv)
    try:
        engines_mod.get(args.engine)
    except KeyError as exc:
        # parser.error, not a print-and-return: an unknown engine is a
        # USAGE error, it exits 2 like every other one, and the message
        # carries the registry's own listing rather than a copy of it.
        parser.error(str(exc.args[0]))
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
