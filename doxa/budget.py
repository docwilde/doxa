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

WHAT A CEILING CAN BE ENFORCED AGAINST, AND ON WHAT GROUNDS

Two grounds, never conflated, and :func:`enforcement_basis` returns which
one is in force for a given engine and model:

* ``"reported"`` -- the engine itself carries a dollar figure.
  :data:`doxa.engines.EngineCapabilities.cost` is True for exactly one
  engine, ``claude``, whose ``ResultMessage.total_cost_usd`` comes from
  the party doing the billing. Nothing beats that and nothing here tries
  to.
* ``"priced"`` -- the engine reports TOKEN COUNTS and
  :mod:`doxa.prices` carries a sourced, dated price for the model that
  produced them. ``codex`` and both API vendors are in this case:
  their streams have no dollars in them (no response field from either
  vendor carries one; DeepSeek's ``GET /user/balance`` is per ACCOUNT,
  and in the very experiment those engines exist for, 32 sessions share
  one key) but every one of them reports input, cached-input, output and
  reasoning tokens, which is the other half of a price.

Until 1.15.0 the second ground did not exist, and this docstring argued
it should not: "a hardcoded price that drifts produces a confident wrong
number, and a ceiling enforced against a confident wrong number is worse
than no ceiling, because it is believed". That argument is unchanged and
:mod:`doxa.prices` is built to satisfy it rather than to overrule it --
every row names the vendor page it was read from and the date it was
read, the sheet's age is readable by the operator and recorded in the
fleet manifest, and a model the sheet does not carry gets NO price. Not a
default, not a nearest sibling, not zero.

**A model with no price still cannot be held to a ceiling**, and that is
now the whole of what "unenforceable" means -- a property of the MODEL,
not of the engine, because a price is attached to a model. A ``glm`` slot
dealt ``glm-5.3-flash`` is bounded; one dealt a model nobody has priced
is not, and no engine-level flag could say both. The answer is still not
to check anyway and hope: :func:`enforceable_for` and
:func:`unenforceable_note` say so at the moment the ceiling is SET -- in
the settings modal's own row, and in the fleet's pre-flight -- naming the
MODEL, so nobody sets a number believing it does something.

WHERE THE VALUE COMES FROM

:func:`session_ceiling` reads through :func:`doxa.config.raw`, like every
other knob: the environment, then ``~/.doxa/config.toml``, then unset. A
repository DOXA happens to have open is not one of those doors, and must
not become one -- a repo that could raise its own session's spend ceiling
would be a repo that raises its own spend ceiling.

CAPTURED AT SESSION START, not re-read per turn. That is a deliberate
reversal: it used to be read on every turn, so that raising the number
mid-session (Ctrl+, -> Session, or the config file) let a stopped session
continue. But ``~/.doxa/config.toml`` is an ordinary same-user file, and
the session being capped is a session with file tools -- so "the very
next prompt goes through" was also available to the capped agent, by
writing its own ceiling. A limit a limited party can raise is not a limit.

:func:`SessionEngine.budget_ceiling` therefore snapshots this ONCE, at
construction. Env is resolved before the file at that moment, exactly as
:func:`doxa.config.raw` always did, and the fleet's per-run environment
still beats the file for a session it is about to spawn. Raising a
RUNNING session's ceiling now takes a new session -- which is a real cost,
and the smaller of the two.

This function itself still reads live, because its other callers are
DISPLAY (the settings modal's warning row, the start note): what the
number is configured to be is a different question from what this session
is enforcing, and only the second one has to be immovable.
"""

from __future__ import annotations

from . import config as config_mod
from . import prices as prices_mod

__all__ = [
    "BASIS_NONE",
    "BASIS_PRICED",
    "BASIS_REPORTED",
    "SESSION_BUDGET_ENV",
    "configured_warning",
    "enforceable_for",
    "enforcement_basis",
    "exhausted",
    "format_usd",
    "per_session_share",
    "priced_note",
    "refusal_text",
    "session_ceiling",
    "start_note",
    "unenforceable_note",
    "usd",
]

#: The engine hands DOXA a dollar figure of its own. Claude, and only
#: Claude -- see the module docstring's two grounds.
BASIS_REPORTED = "reported"
#: The engine hands DOXA token counts and :mod:`doxa.prices` carries a
#: sourced price for the model that produced them.
BASIS_PRICED = "priced"
#: Neither. The ceiling is inert and every surface that shows it says so.
BASIS_NONE = "none"

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
    """The CONFIGURED ceiling in dollars right now, or None when there is
    none.

    Default OFF: an unset knob returns None and nothing anywhere changes
    for anyone who has not asked for a ceiling.

    A live read, and the module docstring says which callers may use it:
    the display ones. What a running session ENFORCES is the snapshot
    :meth:`doxa.engine.SessionEngine.budget_ceiling` took at
    construction -- a ceiling re-read per turn is one the capped session
    can raise by writing the same config file."""
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


def enforcement_basis(
    engine_id: "str | None",
    model: "str | None" = None,
    *,
    reports_cost: "bool | None" = None,
) -> str:
    """On what grounds this ceiling can be enforced for that engine and
    model: :data:`BASIS_REPORTED`, :data:`BASIS_PRICED` or
    :data:`BASIS_NONE`.

    PER MODEL, because that is what a price is attached to, and the
    single most important thing this function does is refuse to answer
    the question at engine level. ``deepseek`` is not bounded or
    unbounded; ``deepseek:deepseek-flash`` is bounded, and a deepseek
    slot dealt a model the sheet has never seen is not.

    Asked of the registry (:func:`doxa.engines.get`) rather than of a
    live handle, because the question is asked at SETTING time, when
    there may be no session yet. An unknown id answers
    :data:`BASIS_REPORTED`: a ceiling on an engine DOXA does not
    recognise must not be reported as broken on the strength of a typo,
    and :func:`doxa.engines.get` already refuses unknown ids by name at
    the one place that matters.

    A bare `model` is RESOLVED rather than refused -- a pool entry that
    names no model still runs one, and
    :func:`doxa.prices.resolve_model` knows which for every engine whose
    default DOXA can know. Codex's it cannot (that default lives in the
    operator's own ``~/.codex/config.toml`` and the stream never names
    what answered), so a bare codex slot honestly answers
    :data:`BASIS_NONE` rather than being priced as something.

    `reports_cost` is the escape hatch a LIVE caller uses: a running
    session asks its own handle (:func:`doxa.engines.capabilities_of`)
    rather than the registry, because a handle is believed about itself
    and what an engine can actually do beats what a registry entry says
    it would be. Left None, the registry is asked."""
    if reports_cost is None:
        from . import engines as engines_mod

        try:
            reports_cost = bool(engines_mod.get(engine_id).supports().cost)
        except Exception:  # noqa: BLE001 -- an engine that cannot be asked is not evidence
            return BASIS_REPORTED
    if reports_cost:
        return BASIS_REPORTED
    resolved = prices_mod.resolve_model(engine_id, model)
    if prices_mod.priced(engine_id, resolved):
        return BASIS_PRICED
    return BASIS_NONE


def enforceable_for(
    engine_id: "str | None", model: "str | None" = None
) -> bool:
    """Can a ceiling actually fire for that engine and model?

    The boolean shorthand for :func:`enforcement_basis`, kept under its
    old name because every caller that only has to decide "warn or not"
    should not have to learn which of the two grounds applies. What
    changed is the second argument: the answer is a fact about a MODEL
    now, and a caller that passes none gets the engine's default model
    resolved rather than a guess about the engine."""
    return enforcement_basis(engine_id, model) != BASIS_NONE


def _engine_id_in(engine_label: str) -> str:
    """The engine id inside a label a caller built for display.

    The two callers phrase it differently -- the settings modal says
    ``engine 'codex'``, a starting session says ``codex`` -- and the
    notes below want to list that engine's priced models rather than
    none. Best effort by construction: a label this cannot read yields an
    empty id, the model list is then omitted, and the sentence still says
    the thing it exists to say."""
    cleaned = engine_label.strip().replace("'", " ").replace('"', " ")
    parts = cleaned.split()
    return parts[-1].lower() if parts else ""


def priced_note(engine_label: str, model: "str | None") -> str:
    """The sentence a ceiling enforced against DOXA's OWN price sheet is
    entitled to.

    A ceiling on this ground is real, and it is not the vendor's
    arithmetic. A surface that showed it identically to Claude's would be
    hiding the one difference that matters when a bill disagrees with a
    manifest, so this names the model being priced, the sheet's date, and
    the direction the estimate errs in."""
    named = (model or "").strip() or "this session's model"
    return (
        f"ENFORCED on {engine_label} against DOXA's own price sheet: "
        f"{named} reports token counts and no dollars, so spend is the "
        "sheet's per-model rates multiplied by the tokens the engine "
        f"reported ({prices_mod.sheet_note()}). Where a vendor publishes "
        "several rates for one model the sheet carries the HIGHEST, so a "
        "session stops at or before its ceiling, never after."
    )


def unenforceable_note(
    engine_label: str, model: "str | None" = None
) -> str:
    """The sentence a ceiling that cannot fire is entitled to.

    Always returns text -- WHEN to show it is the caller's decision, and
    the two callers decide differently (the settings modal asks about the
    configured engine at setting time, a starting session asks its own
    live handle). One sentence, so the two surfaces cannot drift into two
    different accounts of the same limitation.

    It names the engine AND the model, because since 1.16.0 the
    limitation is the MODEL's: the same engine is bounded on a priced
    model and unbounded on this one. A warning that said only "your
    engine" would be one the reader cannot act on -- the action is to
    name a model the sheet carries, and those are listed."""
    named = (model or "").strip()
    known = prices_mod.models_for(_engine_id_in(engine_label))
    which = f"model {named!r}" if named else "the model it would run"
    covered = (
        " Priced models for this engine: " + ", ".join(known) + "."
        if known else ""
    )
    return (
        f"NOT ENFORCEABLE on {engine_label}: it reports token counts but no "
        f"dollar figure, and DOXA's price sheet carries no entry for "
        f"{which}, so those tokens cannot be converted to dollars and this "
        "ceiling would never fire. DOXA will not guess a price -- an "
        "invented rate produces a ceiling that is believed and wrong, "
        f"which is worse than no ceiling at all.{covered} A claude session "
        "is bounded by the figure the vendor itself reports."
    )


def configured_warning() -> "str | None":
    """What the settings modal shows under the ceiling row, or None.

    None whenever there is nothing to say: no ceiling set, or a ceiling
    on an engine that reports its own dollars (where the number means
    exactly what it looks like it means). The two other cases both get a
    sentence, because both are things a person setting a number is
    entitled to know before they trust it:

    * :data:`BASIS_NONE` -- the number is inert, and
      :func:`unenforceable_note` names the model it has no price for.
    * :data:`BASIS_PRICED` -- the number is real but it is DOXA's
      arithmetic over its own sheet rather than the vendor's, and
      :func:`priced_note` says so with the sheet's date on it.

    Engine and model are both resolved through the same precedence every
    other row uses, so this describes the session the operator would
    actually get."""
    ceiling = session_ceiling()
    if ceiling is None:
        return None
    engine_id = config_mod.engine()
    model = prices_mod.resolve_model(engine_id, config_mod.model())
    basis = enforcement_basis(engine_id, model)
    if basis == BASIS_REPORTED:
        return None
    if basis == BASIS_PRICED:
        return priced_note(f"engine {engine_id!r}", model)
    return unenforceable_note(f"engine {engine_id!r}", model)


def start_note(
    ceiling: "float | None",
    *,
    basis: str = BASIS_REPORTED,
    engine_label: str = "this session's engine",
    model: "str | None" = None,
) -> "str | None":
    """One line for a session that starts WITH a ceiling, or None.

    Separate from :func:`refusal_text` on purpose: this is the line that
    makes a ceiling visible while it is still doing nothing. A limit whose
    first appearance in the transcript is the moment it fires is a limit
    the user finds out about by being stopped.

    `basis` is computed by the caller from the LIVE handle
    (:func:`doxa.engines.capabilities_of` plus the model it is actually
    running) rather than looked up here from an id, because by start time
    the session has an engine and what that engine can actually do beats
    what the config file says it would be. It replaced a bare
    ``reports_cost`` boolean in 1.16.0: there are three outcomes now, not
    two, and a session bounded by DOXA's own price sheet says something
    different from one bounded by the vendor's own figure."""
    if ceiling is None:
        return None
    if basis == BASIS_NONE:
        return (
            f"spend ceiling {format_usd(ceiling)} is set — "
            + unenforceable_note(engine_label, model)
        )
    if basis == BASIS_PRICED:
        return (
            f"spend ceiling {format_usd(ceiling)} for this session, "
            + priced_note(engine_label, model)
            + " No turn will START once that much has been spent, "
            "including a turn an arriving peer message would otherwise "
            f"have started. Raise or clear it with {SESSION_BUDGET_ENV} "
            "or the session_budget_usd row (Ctrl+, → Session)."
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
