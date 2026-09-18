# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.budget -- the spend ceilings, and an honest account of what they bound.

Until 1.11.0 a DOXA session could not message anything, and the only thing
that could start a turn was a human pressing enter. Both halves changed in
one release: ``peer_send`` reaches another session
(:func:`doxa.peers.peer_send_enabled`), an arriving message can START a
turn in an idle one (:func:`doxa.peers.peer_inbound_turns_enabled`), and
:mod:`doxa.fleet` arms both for every one of a run's N sessions because a
run with them off measures nothing. The grant landed; the other side of it
did not. DOXA has always COUNTED the money --
:attr:`doxa.engine.SessionEngine.total_cost_usd` accumulates every
``ResultMessage.total_cost_usd`` -- and has never once looked at the
number. This module is the number being looked at.

WHERE THE CHECK HAPPENS, AND WHY THERE

**Before a turn starts, never during one.** That is a deliberate choice
with a cost, and the cost is stated rather than hidden: a session can
exceed its ceiling by at most the price of the ONE turn that crosses it.

The alternative would be to stop a turn mid-flight, and DOXA has nothing
to stop it WITH. The only dollar figure that exists is the one on the
``ResultMessage`` that ENDS a turn -- there is no per-token, per-message
or per-tool cost event on the way there. Synthesising one would mean
multiplying token counts by a price sheet DOXA maintains itself, which is
precisely what :mod:`doxa.vendors` refused for the cost chip and refused
correctly: a hardcoded price that drifts produces a confident wrong number,
and a ceiling enforced against a confident wrong number is worse than no
ceiling, because it is believed.

So the ceiling is a gate on STARTING. Spend is compared against it at the
one moment a comparison is possible, using the only figure that is real.
A turn already running is never interrupted -- see
:meth:`doxa.engine.SessionEngine._send_turn`, where the check sits ahead
of every side effect that turn would have.

WHAT A CEILING CANNOT DO

**An engine that does not report cost cannot be held to one.**
:data:`doxa.engines.EngineCapabilities.cost` is False for ``codex`` (its
JSON stream carries token counts and no dollars) and for both API vendors
(no response field from either carries a dollar figure; DeepSeek's
``GET /user/balance`` is per ACCOUNT, and in the very experiment those
engines exist for, 32 sessions share one key). Their ``total_cost_usd``
stays 0.0 for the life of the session, so a ceiling compared against it
would never fire -- silently, forever, while the settings modal showed a
number.

The answer is NOT to check anyway and hope. A check that can never fire is
the failure mode this module exists to prevent, one layer up. The answer is
:func:`enforceable_for` and :func:`unenforceable_note`, which say so at the
moment the ceiling is SET -- in the settings modal's own row, and in the
fleet's pre-flight -- so nobody sets a number believing it does something.
No price sheet is invented here, and none should be added later without
re-reading :mod:`doxa.vendors`' own paragraph on why.

WHERE THE VALUE COMES FROM

:func:`session_ceiling` reads through :func:`doxa.config.raw`, like every
other knob: the environment, then ``~/.doxa/config.toml``, then unset. A
repository DOXA happens to have open is not one of those doors, and must
not become one -- a repo that could raise its own session's spend ceiling
would be a repo that raises its own spend ceiling.

Read PER TURN rather than captured at connect, which is what makes "raise
it and continue" work: a session stopped at its ceiling is stopped, not
finished. Change the number (Ctrl+, -> Session, the config file, or the
environment of a session yet to start) and the very next prompt goes
through. ``doxa.config.load`` caches on (path, mtime, size) and
``doxa.config.save`` invalidates, so the modal's write is visible to the
next turn without a restart.
"""

from __future__ import annotations

from . import config as config_mod

__all__ = [
    "SESSION_BUDGET_ENV",
    "configured_warning",
    "enforceable_for",
    "exhausted",
    "format_usd",
    "per_session_share",
    "refusal_text",
    "session_ceiling",
    "start_note",
    "unenforceable_note",
    "usd",
]

#: The per-session ceiling's environment variable. Its config-file twin is
#: the ``session_budget_usd`` row in :data:`doxa.config.SETTINGS`, and
#: :func:`doxa.config.raw` is what resolves between them -- this module
#: never reads ``os.environ`` itself, for the reason the module docstring
#: gives about which doors exist.
SESSION_BUDGET_ENV = "DOXA_SESSION_BUDGET_USD"

#: Dollars, to four places. Cost per turn is routinely a fraction of a
#: cent, so two places would print ``$0.00`` for a real turn and make the
#: refusal look like it fired on nothing.
_USD_PLACES = 4


def usd(text: "str | float | None") -> "float | None":
    """A dollar figure, or None for "no ceiling".

    None is returned for empty, unparseable, zero and negative input, and
    all four mean the same thing to every caller: OFF. That is the
    default-off requirement made unavoidable rather than remembered --
    there is no value of this knob that a typo can turn into a surprise
    ceiling, and no way for ``0`` to mean "refuse everything", which is
    how a mistyped ceiling would otherwise brick a session.

    Garbage falls back to OFF rather than raising, the same direction
    :func:`doxa.config.linger_secs` already falls: a typo in a config file
    must cost the user their ceiling, never their session. The settings
    modal's own ``kind="number"`` coercion refuses the typo at SAVE time,
    which is where a human is still looking."""
    if text is None:
        return None
    try:
        value = float(str(text).strip().lstrip("$").replace(",", "") or 0.0)
    except ValueError:
        return None
    return value if value > 0 else None


def session_ceiling() -> "float | None":
    """This session's ceiling in dollars, or None when there is none.

    Default OFF: an unset knob returns None and nothing anywhere changes
    for anyone who has not asked for a ceiling."""
    return usd(config_mod.raw(SESSION_BUDGET_ENV))


def format_usd(value: float) -> str:
    return f"${value:,.{_USD_PLACES}f}"


def exhausted(spent: float, ceiling: "float | None") -> bool:
    """Has `spent` reached `ceiling`?

    ``>=`` rather than ``>``: a session that has spent exactly its ceiling
    has spent its ceiling. None (no ceiling) is never exhausted, which is
    the whole of the default-off behaviour at this layer."""
    if ceiling is None:
        return False
    return float(spent) >= float(ceiling)


def refusal_text(
    spent: float, ceiling: float, *, peer_started: bool = False
) -> str:
    """What the transcript says when a turn is refused.

    Three things, because a refusal missing any of them is a stall with a
    message attached: the arithmetic (so the user can check it), what is
    still working (so "stopped spending" is not read as "died"), and the
    exact name of the knob that lifts it (so continuing does not require
    finding this docstring).

    `peer_started` names the CAUSE when the refused turn was one an
    arriving peer message tried to start. That is the path nobody is
    watching, and a line that did not distinguish it would leave the user
    to discover from a bill that something other than them was pressing
    enter."""
    who = (
        "a peer message tried to start a turn in this session"
        if peer_started
        else "this session"
    )
    return (
        f"⊘ spend ceiling reached — {who} has spent "
        f"{format_usd(spent)} of its {format_usd(ceiling)} ceiling, so no "
        "further turns will START. Nothing else has stopped: the "
        "transcript, /usage, /queue and every command still work, and the "
        "session is still attached. To continue, raise or clear the "
        f"ceiling — {SESSION_BUDGET_ENV} in the environment, or the "
        "session_budget_usd row in ~/.doxa/config.toml (Ctrl+, → Session) "
        "— and send again."
    )


# -- can this ceiling be enforced at all? -----------------------------


def enforceable_for(engine_id: "str | None") -> bool:
    """Does the engine behind `engine_id` report a dollar figure?

    Asked of the registry (:func:`doxa.engines.get`) rather than of a live
    handle, because the question is asked at SETTING time, when there may
    be no session yet. An unknown id answers True: a ceiling on an engine
    DOXA does not recognise must not be reported as broken on the strength
    of a typo, and :func:`doxa.engines.get` already refuses unknown ids by
    name at the one place that matters."""
    from . import engines as engines_mod

    try:
        return bool(engines_mod.get(engine_id).supports().cost)
    except Exception:  # noqa: BLE001 -- an engine that cannot be asked is not evidence
        return True


def unenforceable_note(engine_label: str) -> str:
    """The sentence a ceiling that cannot fire is entitled to.

    Always returns text -- WHEN to show it is the caller's decision, and
    the two callers decide differently (the settings modal asks about the
    configured engine at setting time, a starting session asks its own
    live handle). One sentence, so the two surfaces cannot drift into two
    different accounts of the same limitation.

    It names the engine, because a warning that says "your engine" to
    someone running three of them is a warning they cannot act on."""
    return (
        f"NOT ENFORCEABLE on {engine_label}: it reports token counts but no "
        "dollar figure, so its spend reads as $0.00 to DOXA and this "
        "ceiling would never fire. DOXA will not multiply tokens by a "
        "price sheet it would have to maintain — see doxa.vendors — so the "
        "honest answer is that this number does nothing here. It applies "
        "to claude sessions."
    )


def configured_warning() -> "str | None":
    """What the settings modal shows under the ceiling row, or None.

    None whenever there is nothing to warn about: no ceiling set, or a
    configured engine that does report cost. Both halves are resolved
    through the same precedence every other row uses, so the warning
    appears exactly when the value the user is looking at is inert."""
    ceiling = session_ceiling()
    if ceiling is None:
        return None
    engine_id = config_mod.engine()
    if enforceable_for(engine_id):
        return None
    return unenforceable_note(f"engine {engine_id!r}")


def start_note(
    ceiling: "float | None",
    *,
    reports_cost: bool = True,
    engine_label: str = "this session's engine",
) -> "str | None":
    """One line for a session that starts WITH a ceiling, or None.

    Separate from :func:`refusal_text` on purpose: this is the line that
    makes a ceiling visible while it is still doing nothing. A limit whose
    first appearance in the transcript is the moment it fires is a limit
    the user finds out about by being stopped.

    `reports_cost` is asked of the LIVE handle by the caller
    (:func:`doxa.engines.capabilities_of`) rather than looked up here from
    an id, because by start time the session has an engine and what that
    engine can actually do beats what the config file says it would be."""
    if ceiling is None:
        return None
    if not reports_cost:
        return (
            f"spend ceiling {format_usd(ceiling)} is set — "
            + unenforceable_note(engine_label)
        )
    return (
        f"spend ceiling {format_usd(ceiling)} for this session: no turn "
        "will START once that much has been spent, including a turn an "
        "arriving peer message would otherwise have started. Raise or "
        f"clear it with {SESSION_BUDGET_ENV} or the session_budget_usd "
        "row (Ctrl+, → Session)."
    )


# -- the fleet's half -------------------------------------------------


def per_session_share(total: float, n: int) -> float:
    """A run-wide ceiling, divided into the per-session ceilings that
    actually enforce it.

    There is no cross-process cost aggregator in DOXA and this does not
    invent one. What it does is arithmetic with a property worth stating
    plainly: N sessions each held to ``total / N`` can together spend at
    most ``total``, because each one is separately bounded and the bounds
    add. A run-wide number is what the operator reasons about ("this
    overnight run may cost fifty dollars"); N individually reasonable
    numbers are what thirty-two sessions turn into one unreasonable one.

    The two honest limitations, neither of which the division hides:

    * **Unused share is not reallocated.** A quiet session's unspent half
      is not available to a busy one. The run therefore spends AT MOST
      `total`, and typically less.
    * **The one-turn overshoot is per session, so it is N-fold for a run.**
      Each session may exceed its own share by the price of the turn that
      crosses it (see this module's docstring), so the true worst case is
      ``total + N x (one turn)``. At N=32 that is thirty-two turns of
      slack, which is a real number an operator should hear rather than
      discover.
    """
    return float(total) / max(1, int(n))
