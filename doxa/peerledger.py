# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.peerledger -- the append-only record of what peer sessions said to
each other, and the send-side limit that bounds what can enter it.

This is an INSTRUMENT before it is a feature. docs/plans/emergent-organization.md
asks whether hierarchy emerges among N agents given one task and no assigned
roles; the answer is read entirely off this file. Its "What the ledger must
record" section is why the fields below are the fields, and its opening rule
is the one that governs every judgement call here: *decided now because it
cannot be backfilled*. A message that was not recorded, or was recorded
without its reply pointer, or was truncated, is a measurement that cannot be
taken again -- the sessions are gone.

The record, one JSON object per line, append-only::

    {"v": 1,
     "id": "<uuid4 hex>",
     "ts": "2026-09-17T19:32:00.123456Z",
     "from": {"session": "<session id>", "title": "...", "repo": "/abs/path",
              "model": "claude-opus-5", "engine": "claude"},
     "to": ["<session id>", "..."],
     "kind": "direct" | "broadcast",
     "in_reply_to": "<message id>" | null,
     "body": "<scrubbed text>",
     "body_sha256": "<hex of the PRE-scrub body>",
     "latency_ms": <int> | null,
     "turn": {"id": "<turn id>" | null, "state": "idle" | "running"}}

Four of those carry weight beyond their obvious reading:

* ``ts`` is sub-second and UTC. Ordering at one-second resolution is not
  ordering at all when a reply-round of 992 messages can land inside a
  second; betweenness centrality computed over a ledger that cannot say
  which of two messages came first is computed over a coin flip.
* ``to`` is a LIST even for a direct message. Broadcast is therefore the
  same shape with more entries, not a second record type -- every reader,
  every query and every graph edge-builder has one case to handle.
* ``in_reply_to`` is the only thing that makes threads reconstructable.
  Nothing else in the record can: two messages a microsecond apart are not
  evidence that one answered the other.
* ``body_sha256`` hashes the body BEFORE scrubbing. Scrubbing is
  context-sensitive by nature, so hashing after it would let two identical
  messages hash differently -- and identical-message detection is how an
  agent (or the analysis) recognises a loop. The hash is taken first, the
  scrub is applied second, and only the scrubbed text is ever written.

Bodies are stored in full. Truncation would be cheap and would destroy the
measurement: what distinguishes a coordination message from a status ping
IS the content.

WHO WRITES A RECORD. The SENDING session, once, at the moment of the send.
The receivers read. That is the only rule under which one line describes one
message: a record has one ``from`` and N ``to``, so a receiver-written record
would be N records for one message, with N-1 chances to disagree. It follows
that ``turn`` is the SENDER's turn context (the turn the send was made in,
``{"id": null, "state": "idle"}`` when no turn was running) and that
``latency_ms`` is how long the SENDER took to produce this message -- wall
milliseconds since the message named in ``in_reply_to``, or None when the
sender has no such reference point.

TRUST. Bodies pass ``lore_core.scrub.scrub_secrets`` before they reach disk,
the same choke point ``doxa.peers`` applies to received frames and
``doxa.transcript`` applies to transcripts. The sender's own free-text
self-description (title, repo, model, engine) is scrubbed too -- not because
a peer wrote it (it is first-party: a session describing itself) but because
a session's title is derived from its first prompt, and a prompt can contain
a credential. Identifiers -- session ids, recipient ids, message ids -- are
NEVER scrubbed: mangling one breaks the graph the file exists to draw, and a
uuid4 hex is 32 characters, under ``lore_core.scrub``'s 40-character hex-run
threshold, so nothing would have matched anyway.

WHERE. ``$DOXA_HOME/peers/messages.jsonl``, file 0600, directory 0700 --
durable state, so DOXA_HOME rather than the runtime dir (doxa.config's own
block draws that line). One file per DOXA_HOME, which is also how a harness
collects a run: point DOXA_HOME at a per-run directory and the run's ledger
is the whole file, with no filtering and nothing to separate afterwards.

CONCURRENCY. Writers hold an exclusive ``flock`` for the size check and the
write together, and append whole lines to an ``O_APPEND`` descriptor. Readers
never trust the tail: they consume only up to the last newline in the file
and leave any partial bytes for the next poll. Those two halves are what make
"a concurrent reader never sees half a line" true rather than probable --
neither alone is enough, because a long body can take more than one
``os.write`` even under a lock.

READ COST. A browser graph polls this a few times a second, so re-parsing the
file per call is the one thing the reader may not do. :class:`PeerLedger`
keeps a byte offset and parses only what has arrived since, keyed on
(st_dev, st_ino) so a replaced file is noticed and re-read from the start.
The parsed tail is capped at :data:`CACHE_RECORDS`; a query that reaches past
it falls back to a bounded streaming scan of the file, which is slow and
correct rather than fast and wrong.

BLOCKING. :meth:`PeerLedger.append` is file I/O and takes a cross-process
lock, so N sessions appending at once serialise on it. From async code use
:meth:`PeerLedger.append_async`, which is exactly
``asyncio.to_thread(self.append, ...)`` and exists so that nobody has to
remember to write that.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import stat as stat_module
import threading
import uuid
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import _lore_bootstrap  # noqa: F401 -- sys.path shim, see that module

from lore_core.scrub import scrub_secrets

__all__ = [
    "CACHE_RECORDS",
    "DEFAULT_LIMIT",
    "Delivery",
    "IDLE_TURN",
    "KINDS",
    "LEDGER_NAME",
    "LedgerFull",
    "MAX_LEDGER_BYTES",
    "Message",
    "PeerLedger",
    "RECORD_VERSION",
    "RateLimiter",
    "SendDecision",
    "SendLimits",
    "SendRefused",
    "Sender",
    "TURN_STATES",
    "TurnRef",
    "UnknownCursor",
    "decide_send",
    "ledger",
    "ledger_dir",
    "ledger_path",
]

#: Bumped only when the on-disk shape changes incompatibly. Readers keep
#: whatever they can parse regardless -- a record from a newer writer is
#: still evidence, and dropping it silently would be the worst available
#: response to a version skew.
RECORD_VERSION = 1

DIR_NAME = "peers"
LEDGER_NAME = "messages.jsonl"

#: The size ceiling, and the arithmetic behind it (all three numbers are
#: from docs/plans/emergent-organization.md, which measured them):
#:
#: * A single active session writes ~19 KB/hour. 2 GiB is ~113,000
#:   session-hours -- thirteen years of one session talking continuously.
#: * At N=32 a full reply-broadcast round is 992 messages and ~4.5 MB, and
#:   the plan budgets ten rounds per run: ~45 MB. The whole experiment (four
#:   cells, five replications) is ~900 MB, so even a harness that never
#:   archives between runs finishes inside this ceiling.
#: * A pathological fleet does reach it: the send limit's default window
#:   allows ~2.4 MB/minute/session, so 32 looping sessions fill 2 GiB in
#:   under half an hour. That is the case this number exists to catch.
#:
#: The ceiling REFUSES; it does not rotate. Rotation is the wrong response
#: to pathology: hitting the ceiling means something is wrong and the log is
#: the evidence you would want intact, and a log that quietly discards its
#: oldest half destroys exactly the beginning of the runaway that would
#: explain it. Moving the file aside is a deliberate act by a human or a
#: harness, and afterwards the next append starts a fresh file.
MAX_LEDGER_BYTES = 2 * 1024 * 1024 * 1024

#: How many parsed records the reader keeps resident. ~4.6 KB per message
#: measured at the plan's mean, so 5,000 is roughly 23 MB of text and the
#: last five reply-rounds at N=32 -- far more than any poll asks for, and
#: bounded regardless of how large the file grows.
CACHE_RECORDS = 5_000

#: What a query returns when the caller does not say. Small on purpose: a
#: graph poll wants the newest edges, not the history.
DEFAULT_LIMIT = 50

KINDS = ("direct", "broadcast")
TURN_STATES = ("idle", "running")

#: Microsecond resolution, UTC, trailing Z -- the same format doxa.peers
#: writes its heartbeats in, so a reader that already parses one parses both.
TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_ts(moment: datetime) -> str:
    """A datetime as the ledger writes it: UTC, microseconds, trailing Z."""
    return moment.astimezone(timezone.utc).strftime(TS_FORMAT)


def parse_ts(text: str) -> "datetime | None":
    """The inverse, or None -- an unparseable stamp must never read as
    "now", which is what a bare ``datetime.now()`` fallback would do."""
    try:
        return datetime.strptime(text, TS_FORMAT).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def ledger_dir() -> Path:
    """``$DOXA_HOME/peers``. Resolved per call, like doxa.config's own
    helpers, so a test (or a harness giving each run its own DOXA_HOME) can
    move it without reimporting anything."""
    from . import config as config_module

    return config_module.doxa_home() / DIR_NAME


def ledger_path() -> Path:
    return ledger_dir() / LEDGER_NAME


class LedgerFull(RuntimeError):
    """The ceiling was reached and the write was REFUSED.

    Loud by construction: an exception, carrying the numbers and the path,
    rather than a False nobody checks. Callers surface it; nothing in this
    module rotates, trims or drops a record to make room."""

    def __init__(self, path: Path, size: int, ceiling: int, needed: int) -> None:
        self.path = path
        self.size_bytes = size
        self.ceiling_bytes = ceiling
        self.needed_bytes = needed
        super().__init__(
            f"peer ledger full: {path} is {size} bytes and this record needs "
            f"{needed} more, over the {ceiling}-byte ceiling. The write was "
            "REFUSED and nothing was rotated or dropped -- reaching this "
            "ceiling means message volume is pathological and this file is "
            "the evidence. Move it aside deliberately (the next append starts "
            "a fresh one) once it has been read."
        )


class UnknownCursor(LookupError):
    """``since(message_id)`` was given an id this ledger does not contain.

    Raised rather than answered with everything: a reader whose cursor has
    gone missing asking for "everything since" would silently receive the
    entire file, which is the one answer it was not asking for."""

    def __init__(self, message_id: str) -> None:
        self.message_id = message_id
        super().__init__(
            f"no message {message_id!r} in this ledger -- the cursor is from "
            "another ledger or the file was replaced. Re-bootstrap from "
            "latest_id() rather than re-reading from the start."
        )


@dataclass(frozen=True)
class Sender:
    """The ``from`` block: who sent, and what they say they are.

    ``session`` is the only required field and the only one that is an
    identity rather than a claim. The other four are SELF-DESCRIPTION, and
    doxa.peers' own rule about those applies here word for word: they may be
    displayed and they may be analysed, they may never be treated as
    verified. Recording them is the point -- the mixed-vendor arm of the
    experiment needs to know which model spoke -- but the analysis reads
    them as what a session claimed, which is exactly what they are."""

    session: str
    title: "str | None" = None
    repo: "str | None" = None
    model: "str | None" = None
    engine: "str | None" = None

    def __post_init__(self) -> None:
        if not isinstance(self.session, str) or not self.session.strip():
            raise ValueError(
                "Sender.session must be a non-empty session id -- a record "
                "whose sender cannot be named is not a measurement"
            )
        for name in ("title", "repo", "model", "engine"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"Sender.{name} must be a string or None, got {type(value).__name__}")

    def scrubbed(self) -> "Sender":
        """The same sender with its free text run through ``scrub_secrets``.

        The session id is deliberately left alone: it is an identifier, not
        prose, and a scrubber that rewrote it would break every join in the
        analysis to protect a string that carries no secret."""
        return replace(
            self,
            title=_scrub_optional(self.title),
            repo=_scrub_optional(self.repo),
            model=_scrub_optional(self.model),
            engine=_scrub_optional(self.engine),
        )

    def to_obj(self) -> "dict[str, Any]":
        return {
            "session": self.session,
            "title": self.title,
            "repo": self.repo,
            "model": self.model,
            "engine": self.engine,
        }

    @classmethod
    def from_obj(cls, obj: Any) -> "Sender":
        if not isinstance(obj, dict):
            raise ValueError("'from' must be an object")
        return cls(
            session=str(obj.get("session") or ""),
            title=_optional_str(obj.get("title")),
            repo=_optional_str(obj.get("repo")),
            model=_optional_str(obj.get("model")),
            engine=_optional_str(obj.get("engine")),
        )


@dataclass(frozen=True)
class TurnRef:
    """The sender's turn context at the moment of the send.

    ``state`` is "running" when a turn was in flight and "idle" when the
    send happened outside one. Both are worth recording and they mean
    different things for the experiment: a message sent while the sender is
    idle is unprompted, and an unprompted message is the shape a coordinator
    has."""

    id: "str | None" = None
    state: str = "idle"

    def __post_init__(self) -> None:
        if self.state not in TURN_STATES:
            raise ValueError(
                f"turn state must be one of {TURN_STATES}, got {self.state!r}"
            )
        if self.id is not None and not isinstance(self.id, str):
            raise ValueError("turn id must be a string or None")

    def to_obj(self) -> "dict[str, Any]":
        return {"id": self.id, "state": self.state}

    @classmethod
    def from_obj(cls, obj: Any) -> "TurnRef":
        if obj is None:
            return IDLE_TURN
        if not isinstance(obj, dict):
            raise ValueError("'turn' must be an object")
        state = obj.get("state")
        return cls(
            id=_optional_str(obj.get("id")),
            state=state if state in TURN_STATES else "idle",
        )


#: The turn context of a send made outside any turn. A module constant
#: rather than a default-constructed instance per call, because it is the
#: single most common value in the file.
IDLE_TURN = TurnRef()


@dataclass(frozen=True)
class Message:
    """One ledger line, parsed. Frozen: a record is evidence, and evidence
    that a reader can edit in place is evidence about the reader."""

    id: str
    ts: str
    sender: Sender
    to: "tuple[str, ...]"
    kind: str
    body: str
    body_sha256: str
    in_reply_to: "str | None" = None
    latency_ms: "int | None" = None
    turn: TurnRef = IDLE_TURN
    version: int = RECORD_VERSION

    @property
    def sent_at(self) -> "datetime | None":
        return parse_ts(self.ts)

    def delivered_to(self, session_id: str) -> bool:
        return session_id in self.to

    def involves(self, session_id: str) -> bool:
        return self.sender.session == session_id or session_id in self.to

    def to_obj(self) -> "dict[str, Any]":
        """The contract, in the contract's key order. ``from`` is a Python
        keyword, which is the whole reason the attribute is ``sender`` and
        the mapping happens here rather than by ``asdict``."""
        return {
            "v": self.version,
            "id": self.id,
            "ts": self.ts,
            "from": self.sender.to_obj(),
            "to": list(self.to),
            "kind": self.kind,
            "in_reply_to": self.in_reply_to,
            "body": self.body,
            "body_sha256": self.body_sha256,
            "latency_ms": self.latency_ms,
            "turn": self.turn.to_obj(),
        }

    def to_line(self) -> str:
        """One line, no trailing newline. ``ensure_ascii=False`` keeps
        non-English bodies readable and roughly a third smaller; JSON string
        escaping already guarantees no literal newline can appear inside
        one, which is what keeps one record on one line."""
        return json.dumps(self.to_obj(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_obj(cls, obj: Any) -> "Message":
        if not isinstance(obj, dict):
            raise ValueError("a ledger record must be a JSON object")
        message_id = obj.get("id")
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("record has no id")
        recipients = obj.get("to")
        if not isinstance(recipients, list):
            raise ValueError("'to' must be a list, even for a direct message")
        kind = obj.get("kind")
        if kind not in KINDS:
            raise ValueError(f"'kind' must be one of {KINDS}, got {kind!r}")
        body = obj.get("body")
        if not isinstance(body, str):
            raise ValueError("'body' must be a string")
        latency = obj.get("latency_ms")
        if latency is not None and not isinstance(latency, int):
            latency = None
        version = obj.get("v")
        return cls(
            id=message_id,
            ts=str(obj.get("ts") or ""),
            sender=Sender.from_obj(obj.get("from")),
            to=tuple(str(one) for one in recipients),
            kind=kind,
            body=body,
            body_sha256=str(obj.get("body_sha256") or ""),
            in_reply_to=_optional_str(obj.get("in_reply_to")),
            latency_ms=latency,
            turn=TurnRef.from_obj(obj.get("turn")),
            version=version if isinstance(version, int) else RECORD_VERSION,
        )

    @classmethod
    def parse_line(cls, line: str) -> "Message":
        return cls.from_obj(json.loads(line))


def _scrub_optional(value: "str | None") -> "str | None":
    if value is None:
        return None
    return scrub_secrets(value)


def _optional_str(value: Any) -> "str | None":
    if value is None or isinstance(value, (dict, list, tuple, set, bool)):
        return None
    text = str(value)
    return text or None


def _recipients(to: Any) -> "tuple[str, ...]":
    """Validate and normalise the recipient list.

    Duplicates are collapsed, order preserved. A ledger that recorded the
    same peer twice would claim two deliveries that never happened, and the
    rate limiter collapses them the same way -- the two must agree or the
    budget and the record disagree about what a broadcast cost."""
    if isinstance(to, str) or not isinstance(to, Sequence):
        raise ValueError(
            "'to' must be a sequence of session ids -- a list even for a "
            "direct message, so broadcast is not a second record shape"
        )
    seen: "list[str]" = []
    for one in to:
        if not isinstance(one, str) or not one.strip():
            raise ValueError(f"recipient ids must be non-empty strings, got {one!r}")
        if one not in seen:
            seen.append(one)
    if not seen:
        raise ValueError("'to' must name at least one recipient")
    return tuple(seen)


def _write_all(fd: int, data: bytes) -> None:
    """``os.write`` until the buffer is gone. A short write is legal for a
    large body; under the exclusive lock the continuation still lands at the
    end of the file, and a reader that is mid-poll sees the partial bytes as
    a line with no newline yet -- which it ignores by rule."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:  # pragma: no cover -- POSIX does not do this for regular files
            raise OSError("peer ledger write made no progress")
        view = view[written:]


def _ensure_dir(directory: Path) -> None:
    """Create and clamp to 0700 -- same-user, enforced by the filesystem,
    the same boundary doxa.peers puts around the registry."""
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)


class PeerLedger:
    """Append, read and query one ledger file.

    One instance may be shared by threads: the write path holds a mutex for
    the duration of stamp-and-write (so the timestamp order in the file is
    the file order), and the read path holds its own for the incremental
    parse. Across PROCESSES the ``flock`` in :meth:`_write` is what
    serialises appends; timestamps then come from N wall clocks, so file
    order stays the tiebreaker and readers must not re-sort by ``ts``."""

    def __init__(
        self,
        path: "Path | str | None" = None,
        *,
        ceiling_bytes: int = MAX_LEDGER_BYTES,
        cache_records: int = CACHE_RECORDS,
        now: "Callable[[], datetime] | None" = None,
    ) -> None:
        self._path = Path(path) if path is not None else None
        self._ceiling = int(ceiling_bytes)
        self._cache_records = max(1, int(cache_records))
        self._now = now or _utc_now
        self._write_lock = threading.Lock()
        self._read_lock = threading.Lock()
        self._last_ts: "datetime | None" = None
        self._cached_for: "Path | None" = None
        self._records: "deque[Message]" = deque()
        self._index: "dict[str, int]" = {}
        self._first_seq = 0
        self._total = 0
        self._offset = 0
        self._ident: "tuple[int, int] | None" = None
        self._malformed = 0

    # -- where it lives ------------------------------------------------

    @property
    def path(self) -> Path:
        """Resolved per call when the instance was built without one, so a
        DOXA_HOME that moves under a long-lived instance is followed rather
        than cached into a stale answer."""
        return self._path if self._path is not None else ledger_path()

    @property
    def ceiling_bytes(self) -> int:
        return self._ceiling

    @property
    def malformed_lines(self) -> int:
        """Lines this reader could not parse since its last full re-read.
        Surfaced rather than swallowed: a UI that says "3 unreadable
        records" is honest; one that silently shows fewer edges is not."""
        return self._malformed

    def size_bytes(self) -> int:
        try:
            return os.stat(self.path).st_size
        except OSError:
            return 0

    def headroom_bytes(self) -> int:
        return max(0, self._ceiling - self.size_bytes())

    def is_full(self) -> bool:
        return self.size_bytes() >= self._ceiling

    # -- writing -------------------------------------------------------

    def append(
        self,
        *,
        sender: Sender,
        to: "Sequence[str]",
        body: str,
        kind: str = "direct",
        in_reply_to: "str | None" = None,
        latency_ms: "int | None" = None,
        turn: "TurnRef | None" = None,
    ) -> Message:
        """Record one sent message and return it as written.

        ``body`` is the RAW body: this method hashes it, then scrubs it, and
        only the scrubbed text reaches disk. Passing pre-scrubbed text is a
        quiet bug -- the hash would then describe the redaction rather than
        the message, and two identical messages would stop matching.

        Raises :class:`LedgerFull` when the ceiling is reached, ValueError
        on a record that would not satisfy the contract."""
        # Validate -- every refusal here is a contract violation by the
        # caller, and a ledger that accepted it would record a lie.
        recipients = _recipients(to)
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
        if not isinstance(body, str):
            raise ValueError(f"body must be a string, got {type(body).__name__}")
        if in_reply_to is not None and (not isinstance(in_reply_to, str) or not in_reply_to.strip()):
            raise ValueError("in_reply_to must be a message id or None")
        if latency_ms is not None and (not isinstance(latency_ms, int) or latency_ms < 0):
            raise ValueError("latency_ms must be a non-negative int or None")
        if not isinstance(sender, Sender):
            raise ValueError("sender must be a Sender")

        # Hash the body BEFORE scrubbing, scrub after. Order is the whole
        # point: see this module's docstring.
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        scrubbed = scrub_secrets(body)

        # Stamp and write under one lock, so the ts order of two records
        # written by two threads is the order they appear in the file.
        with self._write_lock:
            message = Message(
                id=uuid.uuid4().hex,
                ts=format_ts(self._stamp()),
                sender=sender.scrubbed(),
                to=recipients,
                kind=kind,
                body=scrubbed,
                body_sha256=digest,
                in_reply_to=in_reply_to,
                latency_ms=latency_ms,
                turn=turn if turn is not None else IDLE_TURN,
            )
            self._write(message)
        return message

    async def append_async(self, **kwargs: Any) -> Message:
        """:meth:`append` off the event loop. Appending takes a
        cross-process lock, and at N=32 that is 31 other sessions that can
        be holding it -- not a wait an async UI may take inline."""
        return await asyncio.to_thread(lambda: self.append(**kwargs))

    def _stamp(self) -> datetime:
        """Now, never earlier than the last stamp this instance issued.

        A clock that steps backwards (NTP, a suspend/resume) would otherwise
        write a record that sorts before its own predecessor, and the
        experiment reads order off this field. The nudge is one microsecond,
        which is below the resolution of anything being measured and above
        the resolution of the format."""
        moment = self._now().astimezone(timezone.utc)
        if self._last_ts is not None and moment <= self._last_ts:
            moment = self._last_ts + timedelta(microseconds=1)
        self._last_ts = moment
        return moment

    def _write(self, message: Message) -> None:
        line = (message.to_line() + "\n").encode("utf-8")
        path = self.path
        _ensure_dir(path.parent)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            # Exclusive for the size check AND the write: two processes that
            # each checked a size below the ceiling and then both wrote
            # would cross it together.
            fcntl.flock(fd, fcntl.LOCK_EX)
            info = os.fstat(fd)
            if stat_module.S_IMODE(info.st_mode) != 0o600:
                # An existing file keeps its old mode through O_CREAT, and
                # the whole same-user boundary rests on this bit.
                os.fchmod(fd, 0o600)
            if info.st_size + len(line) > self._ceiling:
                raise LedgerFull(path, info.st_size, self._ceiling, len(line))
            _write_all(fd, line)
        finally:
            os.close(fd)  # releases the flock

    # -- reading -------------------------------------------------------

    def snapshot(self) -> "list[Message]":
        """Every resident record, oldest first, after picking up whatever
        has been appended since the last call. This is the cheap path a
        polling reader should use."""
        with self._read_lock:
            self._refresh()
            return list(self._records)

    def count(self) -> int:
        """Records in the file, including ones evicted from the cache."""
        with self._read_lock:
            self._refresh()
            return self._total

    def latest_id(self) -> "str | None":
        """The newest record's id -- what an incremental reader bootstraps
        its cursor from before it starts polling :meth:`since`."""
        with self._read_lock:
            self._refresh()
            return self._records[-1].id if self._records else None

    def recent(self, limit: int = DEFAULT_LIMIT) -> "list[Message]":
        """The newest ``limit`` records, newest first."""
        return self._newest_first(lambda _message: True, limit)

    def sent_by(self, session_id: str, limit: int = DEFAULT_LIMIT) -> "list[Message]":
        """What this session sent, newest first."""
        return self._newest_first(lambda m: m.sender.session == session_id, limit)

    def received_by(self, session_id: str, limit: int = DEFAULT_LIMIT) -> "list[Message]":
        """What was delivered to this session, newest first. A broadcast
        appears here for every recipient it named, which is what makes
        in-degree countable without a second index."""
        return self._newest_first(lambda m: session_id in m.to, limit)

    def since(
        self,
        message_id: "str | None",
        *,
        session_id: "str | None" = None,
        limit: "int | None" = None,
    ) -> "list[Message]":
        """Records appended after ``message_id``, OLDEST first.

        Oldest-first because this is the incremental form: a reader appends
        what it gets to what it already has, and reversing that per poll is
        work it should not have to do. ``message_id=None`` means "from the
        beginning". ``session_id`` narrows to messages that session sent or
        received. Raises :class:`UnknownCursor` if the id is not in the
        file."""
        with self._read_lock:
            self._refresh()
            cached = list(self._records)
            first_seq = self._first_seq
            seq = self._index.get(message_id) if message_id else None

        if message_id is None:
            found = cached if first_seq == 0 else list(self._iter_file())
        elif seq is not None:
            found = cached[seq - first_seq + 1:]
        else:
            # The cursor is older than the resident tail (or absent). Slow
            # path by design: correctness over speed, and a reader that
            # polls regularly never reaches it.
            found = self._after_in_file(message_id)

        if session_id is not None:
            found = [message for message in found if message.involves(session_id)]
        if limit is not None:
            found = found[: max(0, limit)]
        return found

    # -- reading, internals --------------------------------------------

    def _newest_first(self, predicate: "Callable[[Message], bool]", limit: int) -> "list[Message]":
        limit = max(0, int(limit))
        if limit == 0:
            return []
        with self._read_lock:
            self._refresh()
            cached = list(self._records)
            complete = self._first_seq == 0
        found = [message for message in reversed(cached) if predicate(message)][:limit]
        if len(found) == limit or complete:
            return found
        # The cache could not fill the request and the file holds more than
        # the cache does. Stream it, keeping only as many matches as were
        # asked for -- bounded memory over an unbounded file.
        matches: "deque[Message]" = deque(maxlen=limit)
        for message in self._iter_file():
            if predicate(message):
                matches.append(message)
        return list(reversed(matches))

    def _refresh(self) -> None:
        """Parse whatever arrived since the last call. Caller holds
        ``_read_lock``."""
        path = self.path
        if self._cached_for != path:
            self._reset(path)
        try:
            info = os.stat(path)
        except OSError:
            self._reset(path)
            return
        ident = (info.st_dev, info.st_ino)
        if ident != self._ident or info.st_size < self._offset:
            # Replaced or truncated -- a different file wearing the same
            # name, so everything cached describes something else.
            self._reset(path)
            self._ident = ident
        if info.st_size <= self._offset:
            return

        with open(path, "rb") as handle:
            handle.seek(self._offset)
            chunk = handle.read(info.st_size - self._offset)

        cut = chunk.rfind(b"\n")
        if cut < 0:
            # Bytes have arrived but no line is complete yet. Consume
            # nothing and leave the offset where it is: this is the half of
            # the partial-line guarantee that lives on the reader.
            return
        self._offset += cut + 1
        for raw in chunk[: cut + 1].splitlines():
            if not raw.strip():
                continue
            try:
                self._remember(Message.parse_line(raw.decode("utf-8", "replace")))
            except (ValueError, TypeError, json.JSONDecodeError):
                self._malformed += 1

    def _reset(self, path: Path) -> None:
        self._cached_for = path
        self._records.clear()
        self._index.clear()
        self._first_seq = 0
        self._total = 0
        self._offset = 0
        self._ident = None
        self._malformed = 0

    def _remember(self, message: Message) -> None:
        self._records.append(message)
        self._index[message.id] = self._total
        self._total += 1
        while len(self._records) > self._cache_records:
            evicted = self._records.popleft()
            self._index.pop(evicted.id, None)
            self._first_seq += 1

    def _iter_file(self) -> "Iterator[Message]":
        """Every parseable record, oldest first, straight off disk. A line
        without its newline is the record still being written: it ends the
        iteration rather than being parsed."""
        try:
            handle = open(self.path, "rb")
        except OSError:
            return
        with handle:
            for raw in handle:
                if not raw.endswith(b"\n"):
                    return
                if not raw.strip():
                    continue
                try:
                    yield Message.parse_line(raw.decode("utf-8", "replace"))
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue

    def _after_in_file(self, message_id: str) -> "list[Message]":
        found: "list[Message]" = []
        seen = False
        for message in self._iter_file():
            if seen:
                found.append(message)
            elif message.id == message_id:
                seen = True
        if not seen:
            raise UnknownCursor(message_id)
        return found


_LEDGERS: "dict[Path, PeerLedger]" = {}
_LEDGERS_LOCK = threading.Lock()


def ledger() -> PeerLedger:
    """The process-wide ledger for the current DOXA_HOME.

    Cached per resolved path rather than globally: the reader's whole value
    is the offset it has already parsed, so handing every caller the same
    instance is what makes a poll cheap -- and keying on the path means a
    test (or a harness) that repoints DOXA_HOME gets its own, with no stale
    offsets from the previous one."""
    path = ledger_path()
    with _LEDGERS_LOCK:
        instance = _LEDGERS.get(path)
        if instance is None:
            instance = PeerLedger(path)
            _LEDGERS[path] = instance
        return instance


# -- the send-side rate limit -----------------------------------------
#
# Separated from the ledger deliberately, and with no I/O of its own: the
# decision is a pure function of (limits, history, turn, fan-out, now), so
# it is testable by calling it, and the same numbers can be replayed offline
# against a collected ledger to ask what a different limit would have done.


@dataclass(frozen=True)
class SendLimits:
    """What one session may send. Units are DELIVERIES, never calls.

    Defaults sized against docs/plans/emergent-organization.md's N=32: one
    broadcast is 31 deliveries, so ``per_turn=64`` is two full broadcasts
    plus a couple of directs in a single turn and refuses the third, and
    ``per_window=512`` in 60 s is about sixteen broadcasts a minute --
    comfortably above the plan's ten reply-rounds per run and far below the
    rate at which one looping session could fill the ledger."""

    per_turn: int = 64
    per_window: int = 512
    window_secs: float = 60.0

    def __post_init__(self) -> None:
        for name in ("per_turn", "per_window"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 1:
                raise ValueError(f"SendLimits.{name} must be a positive int, got {value!r}")
        if not isinstance(self.window_secs, (int, float)) or self.window_secs <= 0:
            raise ValueError("SendLimits.window_secs must be a positive number")


@dataclass(frozen=True)
class Delivery:
    """One charge against the budget: ``count`` deliveries at ``at``,
    inside ``turn_id``. A broadcast to 31 peers is ONE Delivery with
    ``count=31`` -- the record of a call, priced as its fan-out."""

    at: datetime
    count: int
    turn_id: "str | None" = None


@dataclass(frozen=True)
class SendDecision:
    """The answer, whether it was yes or no.

    A refusal carries the reason, the scope that refused, and when that
    scope frees up, because an agent told why it was refused can reason
    about it -- reply to fewer peers, wait, stop -- and one that is silently
    throttled just retries, which is the behaviour the limit exists to
    prevent."""

    allowed: bool
    fanout: int
    turn_id: "str | None"
    turn_used: int
    turn_limit: int
    window_used: int
    window_limit: int
    window_secs: float
    scope: "str | None" = None
    reset_at: "datetime | None" = None
    retry_after_secs: "float | None" = None
    reason: "str | None" = None

    def reset_description(self) -> str:
        """When the refusing scope frees up, in words.

        The window answers with a clock time. A turn cannot: nothing knows
        when the current turn ends, and inventing a time would be worse than
        naming the event -- so it names the event."""
        if self.allowed:
            return "not refused"
        if self.reset_at is not None:
            after = "" if self.retry_after_secs is None else f" (in {self.retry_after_secs:.1f} s)"
            return f"at {format_ts(self.reset_at)}{after}"
        if self.scope == "turn":
            turn = self.turn_id or "the current turn"
            return f"when turn {turn} ends"
        return "never, at this fan-out"

    def raise_if_refused(self) -> "SendDecision":
        if not self.allowed:
            raise SendRefused(self)
        return self


class SendRefused(RuntimeError):
    """A send was refused by the rate limit. Carries the decision, so a
    caller surfacing it to a model surfaces the reason and the reset."""

    def __init__(self, decision: SendDecision) -> None:
        self.decision = decision
        super().__init__(decision.reason or "peer send refused")


def decide_send(
    limits: SendLimits,
    history: "Sequence[Delivery]",
    *,
    turn_id: "str | None",
    fanout: int,
    now: datetime,
) -> SendDecision:
    """Would this send be allowed? Pure -- no clock, no file, no state.

    ``fanout`` is the number of DELIVERIES the call would make: one
    broadcast to 31 peers is 31. Counting calls instead would make a single
    call an unbounded amplifier, which at N=32 is the difference between a
    limit and a decoration.

    ``turn_id=None`` (a send outside any turn) is bounded by the window
    alone. A None bucket would never reset, so it would eventually refuse
    every out-of-turn send forever while naming a reset time that never
    comes -- worse than deferring to the window, which does reset."""
    if not isinstance(fanout, int) or fanout < 1:
        raise ValueError(f"fanout must be at least one delivery, got {fanout!r}")

    # Tally
    turn_used = (
        sum(one.count for one in history if one.turn_id == turn_id)
        if turn_id is not None
        else 0
    )
    cutoff = now - timedelta(seconds=limits.window_secs)
    in_window = [one for one in history if one.at > cutoff]
    window_used = sum(one.count for one in in_window)

    common: "dict[str, Any]" = {
        "fanout": fanout,
        "turn_id": turn_id,
        "turn_used": turn_used,
        "turn_limit": limits.per_turn,
        "window_used": window_used,
        "window_limit": limits.per_window,
        "window_secs": limits.window_secs,
    }
    fan_note = (
        "one broadcast counts once per recipient, so a fan-out of "
        f"{fanout} costs {fanout}"
    )

    # The per-turn bound first: it is the one a caller can act on
    # immediately, by sending to fewer peers.
    if turn_id is not None and turn_used + fanout > limits.per_turn:
        never = fanout > limits.per_turn
        reason = (
            f"peer send refused: {fanout} deliveries would put this turn at "
            f"{turn_used + fanout}, over the per-turn limit of {limits.per_turn} "
            f"({fan_note}). "
        )
        if never:
            reason += (
                "This fan-out exceeds the whole per-turn budget and will "
                "never fit -- send to fewer peers."
            )
        else:
            reason += (
                f"The per-turn budget resets when turn {turn_id} ends; "
                f"{window_used} of {limits.per_window} deliveries are used in "
                f"the last {limits.window_secs:g} s."
            )
        return SendDecision(
            allowed=False, scope="turn", reset_at=None, retry_after_secs=None,
            reason=reason, **common,
        )

    if window_used + fanout > limits.per_window:
        if fanout > limits.per_window:
            reason = (
                f"peer send refused: a fan-out of {fanout} exceeds the whole "
                f"{limits.per_window}-delivery window budget and will never "
                f"fit ({fan_note}) -- send to fewer peers."
            )
            return SendDecision(
                allowed=False, scope="window", reset_at=None,
                retry_after_secs=None, reason=reason, **common,
            )
        reset_at = _window_reset(
            in_window, window_used + fanout - limits.per_window, limits.window_secs, now
        )
        retry_after = max(0.0, (reset_at - now).total_seconds())
        reason = (
            f"peer send refused: {fanout} deliveries would put this session at "
            f"{window_used + fanout} in the last {limits.window_secs:g} s, over "
            f"the limit of {limits.per_window} ({fan_note}). Enough of the "
            f"budget frees up at {format_ts(reset_at)} (in {retry_after:.1f} s)."
        )
        return SendDecision(
            allowed=False, scope="window", reset_at=reset_at,
            retry_after_secs=retry_after, reason=reason, **common,
        )

    return SendDecision(allowed=True, **common)


def _window_reset(
    in_window: "Sequence[Delivery]", need: int, window_secs: float, now: datetime
) -> datetime:
    """When enough deliveries have aged out for this send to fit.

    Not "when the oldest one expires": with a fan-out of 31 against a budget
    that is 5 short, waiting for one delivery to age out is still a refusal,
    and a reset time that is wrong in the impatient direction teaches an
    agent to ignore it."""
    freed = 0
    for one in sorted(in_window, key=lambda delivery: delivery.at):
        freed += one.count
        if freed >= need:
            return one.at + timedelta(seconds=window_secs)
    return now + timedelta(seconds=window_secs)  # pragma: no cover -- need <= window_used


class RateLimiter:
    """The decision function plus the history it decides against.

    Holds nothing but memory: no file, no clock of its own beyond the one
    injected. The pure decision lives in :func:`decide_send`; this class is
    the small mutable shell that remembers what was already spent."""

    def __init__(
        self,
        limits: "SendLimits | None" = None,
        *,
        now: "Callable[[], datetime] | None" = None,
    ) -> None:
        self.limits = limits if limits is not None else SendLimits()
        self._now = now or _utc_now
        self._history: "deque[Delivery]" = deque()

    def check(
        self,
        *,
        recipients: "Sequence[str]",
        turn_id: "str | None" = None,
        now: "datetime | None" = None,
    ) -> SendDecision:
        """Would this send be allowed? Records nothing."""
        moment = now or self._now()
        self._prune(moment, turn_id)
        return decide_send(
            self.limits, list(self._history),
            turn_id=turn_id, fanout=_fanout(recipients), now=moment,
        )

    def charge(
        self,
        *,
        recipients: "Sequence[str]",
        turn_id: "str | None" = None,
        now: "datetime | None" = None,
    ) -> SendDecision:
        """Decide, and spend the budget when the answer is yes.

        Takes the RECIPIENTS, not a count, on purpose: the one mistake this
        limit exists to prevent is charging a broadcast as a single send,
        and an API that is handed the list cannot be miscounted by its
        caller."""
        moment = now or self._now()
        decision = self.check(recipients=recipients, turn_id=turn_id, now=moment)
        if decision.allowed:
            self._history.append(
                Delivery(at=moment, count=decision.fanout, turn_id=turn_id)
            )
        return decision

    def charge_or_raise(self, **kwargs: Any) -> SendDecision:
        """:meth:`charge`, raising :class:`SendRefused` on a refusal."""
        return self.charge(**kwargs).raise_if_refused()

    def used_in_turn(self, turn_id: "str | None") -> int:
        if turn_id is None:
            return 0
        return sum(one.count for one in self._history if one.turn_id == turn_id)

    def used_in_window(self, now: "datetime | None" = None) -> int:
        moment = now or self._now()
        cutoff = moment - timedelta(seconds=self.limits.window_secs)
        return sum(one.count for one in self._history if one.at > cutoff)

    def history(self) -> "list[Delivery]":
        return list(self._history)

    def reset(self) -> None:
        self._history.clear()

    def _prune(self, now: datetime, turn_id: "str | None") -> None:
        """Forget deliveries that can no longer refuse anything.

        A delivery survives if it is inside the window OR belongs to the
        turn being asked about -- a turn can outlive the window, and
        dropping its deliveries would hand back per-turn budget that was
        already spent."""
        cutoff = now - timedelta(seconds=self.limits.window_secs)
        while self._history:
            oldest = self._history[0]
            if oldest.at > cutoff or (turn_id is not None and oldest.turn_id == turn_id):
                break
            self._history.popleft()


def _fanout(recipients: "Sequence[str]") -> int:
    """Deliveries a send to ``recipients`` would make. Duplicates collapse,
    exactly as :func:`_recipients` collapses them for the record -- the
    budget and the ledger must price a broadcast identically."""
    return len(_recipients(recipients))
