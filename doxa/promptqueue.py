# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.promptqueue -- the ONE bounded FIFO a mid-turn prompt goes through.

Typing a prompt while a turn is still running used to do two wrong things
at once: the daemon refused it outright (``ok=False, error="a turn is
already running in this session"``), and the CLIENT side reacted to that
refusal in a way that left the ORIGINAL turn's own transcript block frozen
mid-thought forever (see ``doxa.session.pane``/``doxa.session.runtime`` --
Textual's ``exclusive=True`` "turn" worker group cancelled the block that
was rendering the first turn the instant a second prompt was submitted).

The fix the owner asked for: never error, never hang. A prompt typed while
a turn is running is accepted immediately and queued -- a small, bounded,
named FIFO -- and starts automatically as the next turn the moment the
current one reaches ``turn_done``. (Steering -- delivering it mid-turn the
way Claude Code's own "type while it works" does -- was investigated and
rejected: see ``doxa.engine.SessionEngine.send``'s docstring for the SDK
evidence. Queueing is the fallback the design permits, and it is what
actually runs.)

This class is the ONE implementation of that FIFO, used identically by:

* :class:`doxa.daemon.SessionDaemon` -- one queue per socket session,
  shared by every attached client (a second tab must not disagree about
  what is queued, so the daemon is the single source of truth and
  broadcasts every change through ``_publish``);
* :class:`doxa.engine.SessionEngine` -- one queue per in-process session
  (no socket, so "every attached client" is just the one pane, reached
  through the same out-of-band ``peer_events()`` stream a peer-driven turn
  already uses).

Sharing this class, rather than two ad-hoc lists, is what keeps the two
paths from drifting apart on FIFO order, the bound, or the id shape a
cancel call needs.
"""

from __future__ import annotations

import itertools
from collections import deque
from dataclasses import dataclass

#: The bound. Small and named on purpose (design point 3): a mid-turn
#: prompt is for the rare case of "I thought of one more thing while it
#: works", not a batch-submission queue -- eight deep is already generous
#: for that, and the bound exists so a caller who keeps typing gets a
#: clear, immediate refusal instead of the daemon quietly accumulating an
#: unbounded backlog it will grind through long after anyone is watching.
PROMPT_QUEUE_MAXLEN = 8


class PromptQueueFull(Exception):
    """Raised by :meth:`PromptQueue.enqueue` once the bound is reached.

    The caller turns this into a clear reply (``ok=False, error=...`` over
    the socket; a plain exception in-process) -- design point 3 requires
    that the bound be enforced with a clear answer, never a silent drop."""


@dataclass(slots=True, frozen=True)
class QueuedPrompt:
    """One prompt waiting in line: its own id (for /queue's cancel) and
    the verbatim text it will be sent as once it is dequeued."""

    id: str
    text: str


class PromptQueue:
    """A per-session bounded FIFO. See the module docstring for who owns
    one and why both owners share this exact class."""

    def __init__(self, maxlen: int = PROMPT_QUEUE_MAXLEN) -> None:
        self._maxlen = maxlen
        self._items: "deque[QueuedPrompt]" = deque()
        # Monotonic per-queue counter, not uuid4: a queued prompt's id only
        # ever has to be unique within ITS OWN session's small backlog (the
        # cancel call is scoped to one session already), and a short,
        # readable "q3" is what a human types back at /queue.
        self._ids = itertools.count(1)

    def __len__(self) -> int:
        return len(self._items)

    def enqueue(self, text: str) -> QueuedPrompt:
        """Append `text`. Raises :class:`PromptQueueFull` once `maxlen`
        prompts are already waiting -- the caller's job to report that,
        never to drop it silently."""
        if len(self._items) >= self._maxlen:
            raise PromptQueueFull(
                f"the queue is full ({self._maxlen} prompts already "
                "waiting) -- let one finish, or cancel one with /queue, "
                "before adding another"
            )
        item = QueuedPrompt(id=f"q{next(self._ids)}", text=text)
        self._items.append(item)
        return item

    def position(self, item_id: str) -> "int | None":
        """1-based place in line (1 = next to start), or None if `item_id`
        is not currently queued -- already dequeued, cancelled, or never
        queued at all."""
        for index, item in enumerate(self._items):
            if item.id == item_id:
                return index + 1
        return None

    def pop_next(self) -> "QueuedPrompt | None":
        """The next prompt to start, FIFO order -- called exactly once per
        completed turn, by whichever side owns this queue, to decide
        whether another turn should start automatically."""
        return self._items.popleft() if self._items else None

    def cancel(self, item_id: str) -> "QueuedPrompt | None":
        """Remove one queued prompt by id, wherever it sits in line.
        Returns it (for the caller to announce) or None if it was already
        gone -- started, cancelled, or discarded by someone else."""
        for item in self._items:
            if item.id == item_id:
                self._items.remove(item)
                return item
        return None

    def snapshot(self) -> "list[dict[str, str]]":
        """Everything still waiting, FIFO order, for /queue's listing."""
        return [{"id": item.id, "text": item.text} for item in self._items]

    def clear(self) -> "list[QueuedPrompt]":
        """Discard everything still waiting and return what was discarded,
        in order -- design point 6: a queued prompt survives a mere
        detach (this is never called for that), but finalize is the one
        moment it is deliberately thrown away rather than carried
        forward. The caller announces each discarded item (visible in the
        transcript) before -- or as part of -- calling this."""
        items = list(self._items)
        self._items.clear()
        return items
