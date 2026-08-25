# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.transcript -- rebuild a session's CONVERSATION from the transcript
DOXA already persists, so a restored tab comes back with what was on
screen instead of an empty pane bound to a live session.

The gap this closes (v0.32.0, measured, not assumed). v0.23.0's tab
restore reattaches every saved tab to its still-live daemon, and the
daemon's attach handler replays its event ring to the fresh client
(doxa/daemon.py, ``EventRing.since``) -- so a SHORT session did come back
with its transcript, which is why the gap went unnoticed. The ring holds
:data:`doxa.daemon.RING_CAPACITY` = 512 frames and one ``text_delta`` is
one frame: a single 700-delta answer pushes ``turn_started`` off the far
end, doxa.app's ``_peer_pump`` then has no ``TurnBlock`` to render the
surviving deltas into, and every one of them is dropped on the floor.
Measured against a real daemon over a real socket: ring next_seq 702,
oldest buffered seq 190, restored tab rendered **zero** turn blocks and
said nothing about it. The user's report -- "restore the view ... and
their content" -- is that pane.

Why the transcript file and not a bigger ring, or a new RPC. DOXA already
writes every prompt, every assistant block and every tool result to
``$LORE_PROJECTS_DIR/<slug>/<session_id>.jsonl`` at
``doxa.engine.SessionEngine._persist`` -- the scrub choke point, the same
file ``lore_store.index_live`` indexes for /search. It is complete, it is
already scrubbed, it outlives the daemon, and it is on the SAME machine as
the TUI (the daemon socket is a Unix socket; there is no remote case). So
restore reads it directly and no transcript ever crosses the wire: the
64KB ``peers.MAX_FRAME_BYTES`` frame cap that forced v0.28.0 to page the
beliefs RPC -- and that v0.31.0 generalized into ``daemon._fit_page`` for
the ``pending`` RPC as well -- is not on this path at all, because this
path has no frame. ``_fit_page`` deliberately gains no third caller here:
one implementation enforcing one budget, for the calls that really are
frames.

What DOES stay capped is the rendering, for the same reason the cap
exists: :data:`DEFAULT_TURN_LIMIT` turns and :data:`MAX_TEXT_CHARS` of
assistant prose per turn, both applied by :func:`read_turns`, which
reports what it left out (:attr:`Transcript.dropped_turns`,
:attr:`Turn.text_truncated`) rather than handing back a short answer that
renders as if it were whole. doxa.app mounts the result in batches so a
thousand-turn restore never blocks the event loop in one go.

Detached vs. terminated. Both read the same file, which is the point:
a DETACHED session's daemon is still alive, so its tab restores as a real
attached session whose scrollback happens to come from disk; a TERMINATED
session has nothing to attach to, so its tab restores read-only (doxa.app's
``ArchivedSessionTab``). The transcript is identical either way -- what
differs is whether there is a session behind it, and the two are labelled
differently precisely so the user can tell which they got.

Never raises. A missing file, an unreadable one, a half-written last line
(the engine appends, so the tail can be torn) all read as "less
transcript", never as a failed restore -- the same posture doxa.tabsets
takes on its own record and doxa.config takes on settings.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import _lore_bootstrap  # noqa: F401 -- sys.path shim, see that module

from lore_core.config import PROJECTS_DIR, project_slug

# How many of the most recent turns a restored tab renders. A restore is
# "put me back where I was", not "re-read the whole project": the turns
# BEFORE this are still on disk and still in /search, and the pane says
# how many it skipped rather than pretending the session started there.
DEFAULT_TURN_LIMIT = 40

# Per-turn ceiling on assistant prose, in characters. A single turn can
# hold a whole file dump; rendering it in full costs a Markdown re-parse
# per restored turn for text nobody scrolls back to. Marked when it bites
# (Turn.text_truncated) -- a cut transcript must never read as a complete
# one.
MAX_TEXT_CHARS = 20_000

# Per-turn ceiling on tool chips. Same reasoning, and the same honesty:
# Turn.tools_dropped counts what did not get a chip.
MAX_TOOLS_PER_TURN = 30


@dataclass
class ToolRecord:
    """One tool call as the transcript remembers it: the name and input
    from the assistant's ``tool_use`` block, the result text from the
    matching ``tool_result`` block one record later. ``result`` is None
    for a call whose result never landed (the session ended mid-tool) --
    rendered as still-running rather than as a silent success."""

    call_id: str
    name: str
    tool_input: dict = field(default_factory=dict)
    result: "str | None" = None
    is_error: bool = False


@dataclass
class Turn:
    """One user prompt and everything that answered it."""

    prompt: str
    text: str = ""
    tools: "list[ToolRecord]" = field(default_factory=list)
    text_truncated: bool = False
    tools_dropped: int = 0


@dataclass
class Transcript:
    """:func:`read_turns`' answer. ``dropped_turns`` is how many EARLIER
    turns the limit cut -- nonzero means this transcript is a tail, and
    the caller is required to say so on screen."""

    turns: "list[Turn]"
    dropped_turns: int = 0

    def __bool__(self) -> bool:
        return bool(self.turns)


def transcript_path(session_id: str, cwd: str) -> "Path | None":
    """Where :class:`doxa.engine.SessionEngine` persists this session --
    ``PROJECTS_DIR/<project_slug(cwd)>/<session_id>.jsonl``, resolved the
    same way the engine's own constructor resolves it. None when the
    arguments cannot name a file (an empty session id) or the slug lookup
    fails; the FILE need not exist, callers check that themselves."""
    if not session_id:
        return None
    try:
        return PROJECTS_DIR / project_slug(cwd or ".") / f"{session_id}.jsonl"
    except Exception:  # noqa: BLE001 -- a path we cannot build is "no transcript"
        return None


def _records(path: Path) -> "list[dict]":
    """Every parseable JSON line, in file order. A torn final line (the
    engine appends; a session killed mid-write leaves one) is skipped, not
    raised -- one lost record costs the last few words of a transcript,
    an exception would cost the whole restore."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: "list[dict]" = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def _is_prompt(record: dict) -> bool:
    """A turn boundary. The engine writes a user prompt as a STRING
    content (``_persist_user_text``) and tool results as a LIST of
    ``tool_result`` blocks (``_persist_tool_results``) -- both under
    ``type: "user"``, so the content's own shape is what tells a new turn
    from the middle of one."""
    if record.get("type") != "user":
        return False
    message = record.get("message")
    if not isinstance(message, dict):
        return False
    return isinstance(message.get("content"), str)


def parse(records: "list[dict]") -> "list[Turn]":
    """Fold raw transcript records into turns, in order. Exposed for tests
    and for any caller that already has the records in hand; :func:`read`
    is the file-reading front door.

    Assistant records BEFORE the first prompt (there are none today, but a
    format that grows a preamble must not lose it) open an unattributed
    turn with an empty prompt rather than being dropped."""
    turns: "list[Turn]" = []
    by_call: "dict[str, ToolRecord]" = {}

    def current() -> Turn:
        if not turns:
            turns.append(Turn(prompt=""))
        return turns[-1]

    for record in records:
        rtype = record.get("type")
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if _is_prompt(record):
            turns.append(Turn(prompt=str(content)))
            by_call = {}
            continue
        if not isinstance(content, list):
            continue
        if rtype == "assistant":
            turn = current()
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    turn.text += str(block.get("text") or "")
                elif block.get("type") == "tool_use":
                    call_id = str(block.get("id") or "")
                    tool = ToolRecord(
                        call_id=call_id,
                        name=str(block.get("name") or "tool"),
                        tool_input=(
                            block.get("input")
                            if isinstance(block.get("input"), dict)
                            else {}
                        ),
                    )
                    turn.tools.append(tool)
                    if call_id:
                        by_call[call_id] = tool
        elif rtype == "user":
            # A tool_result batch: fills in calls THIS turn already opened.
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_result":
                    continue
                tool = by_call.get(str(block.get("tool_use_id") or ""))
                if tool is None:
                    continue
                tool.result = str(block.get("content") or "")
                tool.is_error = bool(block.get("is_error"))
    return turns


def _cap(turns: "list[Turn]", limit: int) -> Transcript:
    """Keep the LAST ``limit`` turns and cap each one, reporting every cut.
    The tail is what a restore is for -- the turn the user was looking at
    when the window closed is the last one, never the first.

    ``limit <= 0`` means no turn cap at all. The per-turn text and tool
    caps still apply either way: those bound ONE turn's render cost, which
    is not a thing any caller should be able to opt out of."""
    dropped = max(0, len(turns) - limit) if limit > 0 else 0
    kept = turns[dropped:] if dropped else list(turns)
    for turn in kept:
        if len(turn.text) > MAX_TEXT_CHARS:
            turn.text = turn.text[:MAX_TEXT_CHARS]
            turn.text_truncated = True
        if len(turn.tools) > MAX_TOOLS_PER_TURN:
            turn.tools_dropped = len(turn.tools) - MAX_TOOLS_PER_TURN
            turn.tools = turn.tools[:MAX_TOOLS_PER_TURN]
    return Transcript(turns=kept, dropped_turns=dropped)


def read(
    session_id: str, cwd: str, limit: "int | None" = None
) -> Transcript:
    """The restore front door: this session's persisted conversation,
    newest ``limit`` turns, every cut counted. An empty
    :class:`Transcript` for a session with no file on disk (never
    persisted, or persisted under another project) -- indistinguishable
    from "nothing was said", which is the correct rendering for both.

    ``limit`` defaults to :data:`DEFAULT_TURN_LIMIT` read AT CALL TIME,
    not bound into the signature: a default argument evaluated at import
    would make the constant unsettable, and this is the one knob a caller
    (or a test measuring the truncation notice) has."""
    path = transcript_path(session_id, cwd)
    if path is None:
        return Transcript(turns=[])
    return _cap(
        parse(_records(path)),
        DEFAULT_TURN_LIMIT if limit is None else limit,
    )


def exists(session_id: str, cwd: str) -> bool:
    """Is there a transcript file behind this session id at all? The
    question doxa.tabsets asks about a saved tab whose daemon is gone:
    with a transcript there is an archived tab worth restoring, without
    one there is nothing to show and the tab is simply skipped."""
    path = transcript_path(session_id, cwd)
    if path is None:
        return False
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False
