# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.events -- the event record and the list caps, with no SDK behind them.

Split out of ``doxa.engine`` for a measured reason. Importing
``claude_agent_sdk`` costs **404 ms** (330 ms of it ``mcp.types`` building
pydantic models), and it is 74% of the 546 ms it took to import
``doxa.app``. Most of the code that wanted these four names never runs an
agent: ``doxa.client`` talks to a daemon over a socket, ``doxa.session.
runtime`` renders events someone else produced, ``doxa.session.chips``
only needs to know where a list stops. Every one of them paid for the SDK
to learn a dataclass and three integers.

``doxa.engine`` re-exports all four, so ``from .engine import EngineEvent``
keeps working; the SDK now loads when a session actually starts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# The hello frame's version, shared by both sides of the socket. It lives
# here rather than in doxa.daemon because doxa.client -- which only needs
# to compare one integer -- would otherwise import the whole daemon, and
# through it the SDK.
PROTOCOL_VERSION = 1


# How many active beliefs the chip's picker will ever list in one open
# (:meth:`SessionEngine.list_beliefs`, and EngineClient's paging loop over
# the daemon's `beliefs` RPC, both default to this so the two paths agree
# on where "the list" ends). v0.28.0 raised it from an implicit 500 after
# an operator with ~517 active beliefs: at 500 the picker silently dropped
# the tail, which is the one thing a belief list must not do. The cap has
# to exist at all (this SELECTs every claim BODY), so the picker now SAYS
# when it was reached -- see SessionPane.open_beliefs_picker's note row.
BELIEF_LIST_LIMIT = 2000

# How many staged proposals ``/pending`` will ever list in one open
# (:meth:`SessionEngine.list_pending`, and EngineClient's paging loop over
# the daemon's `pending` RPC, both default to this so the two paths agree
# on where "the list" ends). Same shape and same honesty rule as
# BELIEF_LIST_LIMIT above: the picker SAYS when the cap bit rather than
# showing a short list as if it were the whole staging area. Lower than
# the belief cap because a pending queue that ever gets near 500 is
# already a signal to go review it, not to scroll further.
PENDING_LIST_LIMIT = 500

# How many evidence rows one belief's trail ever carries into the picker
# (item V). Unlike the two caps above this one is per BELIEF, not per
# store, and it is deliberately small: the trail is fetched lazily, one
# belief at a time, precisely so a picker over 600 beliefs never has to
# put 600 trails in a wire frame. A belief with more evidence than this
# says so rather than showing a short trail as a complete one.
BELIEF_EVIDENCE_LIMIT = 40


@dataclass
class EngineEvent:
    """One typed event out of :meth:`SessionEngine.send` /
    :meth:`SessionEngine.start` / :meth:`SessionEngine.finalize`.

    ``type`` is one of: turn_started, text_delta, reasoning_delta, tool_call,
    tool_result, turn_done, session_done -- the seven event kinds the TUI
    (doxa/app.py) switches on to build/update blocks (reasoning_delta,
    v0.25.0: the model's own summarized reasoning, routed like text_delta
    -- see doxa.app.ReasoningSection and show_reasoning() above) -- plus
    peer_joined, peer_left,
    peer_message, tool_disabled, needs_input and needs_input_resolved,
    which arrive out-of-band on the same EngineEvent type via
    :meth:`SessionEngine.peer_events` (a turn generator can only yield
    while a turn runs; peer activity doesn't wait for one, and a
    two-strikes disable -- or a can_use_tool callback blocked on a
    question -- fires from inside the SDK's own control-request dispatch,
    outside our generator's yield points).

    ``needs_input`` (data: ``id``, ``kind`` -- ``"ask_user"`` or
    ``"permission"`` --, ``tool_name``, plus ``questions`` for ask_user or
    ``input_summary``/``title``/``display_name``/``description`` for
    permission) is queued by :meth:`_on_can_use_tool` and answered by
    :meth:`answer_needs_input`; ``needs_input_resolved`` (data: ``id``)
    follows once it is, so every attached client -- not just the one that
    answered -- can drop its own copy of the dialog (same "everyone
    learns" convention ``model_changed`` already follows for /model).
    """

    type: str
    data: dict[str, Any] = field(default_factory=dict)
