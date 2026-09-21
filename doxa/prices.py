# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.prices -- the price sheet, with its sources, its dates, and its
refusals.

:mod:`doxa.budget` argued for two releases that DOXA must not multiply
token counts by a price sheet: "a hardcoded price that drifts produces a
confident wrong number, and a ceiling enforced against a confident wrong
number is worse than no ceiling, because it is believed". That argument
is still correct and this module does not overturn it. It answers it, by
building the sheet the argument said a sheet would have to be.

WHAT MAKES A NUMBER HERE DIFFERENT FROM A GUESS

Four properties, and every one of them is enforced by the shape of
:class:`Price` rather than by a convention somebody has to remember:

1. **Every entry names its source and the date it was read.** Not a
   comment at the top of the file -- a field on the row, so a sheet that
   grows half-updated says which half.
2. **A model with no entry has no price.** :func:`price_for` returns
   None, :func:`cost_of` returns None, and every caller turns that None
   into a REFUSAL that names the model. There is no default rate, no
   nearest-neighbour match and no zero. A model DOXA has never priced is
   reported as unbounded BY NAME -- never silently treated as free, which
   is the one failure this whole feature exists to prevent.
3. **Where a vendor publishes more than one rate for one model, the
   HIGHEST one is the entry.** DOXA cannot observe which service tier a
   turn ran at (OpenAI's "fast"/priority tier is set in the operator's
   own ``~/.codex/config.toml``, which DOXA inherits and never reads) nor
   which side of DeepSeek's peak/off-peak clock it landed on. An
   over-estimate makes a session stop at or BEFORE its share; an
   under-estimate lets it run past. Only one of those is a ceiling, so
   the entry carries the upper bound and ``note`` records the discounted
   rate it was chosen over.
4. **The sheet's age is readable.** :func:`sheet_read_on`,
   :func:`sheet_age_days` and :func:`stale` are what let the fleet print
   which price data bounded a run and warn when that data is old. A sheet
   nobody can date is a sheet nobody can distrust.

WHAT IS NOT PRICED HERE, AND WHY

**Claude.** The Claude engine reports ``ResultMessage.total_cost_usd`` --
a real figure from the party doing the billing -- and
:data:`doxa.engines.EngineCapabilities.cost` is True for it alone. A
DOXA-maintained price for a model that tells DOXA its own price would be
a second, worse answer to a question already answered. See
:func:`doxa.budget.enforcement_basis`, which returns ``"reported"`` for
Claude and ``"priced"`` for anything this module covers -- two different
grounds, never conflated.

PER MODEL, NOT PER ENGINE. ``EngineCapabilities.cost`` is an engine-level
fact and stays one: it means "this engine's own stream carries dollars",
which is a property of the protocol and not of the model. But whether a
ceiling can be ENFORCED is a property of the model, because that is what
a price is attached to -- a ``glm`` slot dealt ``glm-5.3-flash`` is
bounded and one dealt a model this sheet has never seen is not, and no
engine-level flag can say both. That question is asked here, by
:func:`price_for`, and :func:`doxa.budget.enforceable_for` takes a model
argument for exactly this reason.

THE TOKEN SEMANTICS, READ OFF THE PARSERS RATHER THAN ASSUMED

Both parsers (:meth:`doxa.codex.CodexEngine._absorb_usage` and
:meth:`doxa.vendors.ChatApiEngine._absorb_usage`) normalise into the same
four keys, and the arithmetic in :func:`cost_of_counts` depends on what
each one MEANS:

* ``input_tokens`` -- the WHOLE prompt for that call, cached tokens
  INCLUDED. Codex reads it from ``turn.completed.usage.input_tokens``;
  the vendors read it from ``usage.prompt_tokens``.
* ``cache_read_input_tokens`` -- the SUBSET of the above that was served
  from cache. Codex: ``cached_input_tokens``. The vendors:
  ``prompt_tokens_details.cached_tokens``, with DeepSeek's top-level
  ``prompt_cache_hit_tokens`` preferred when present -- and DeepSeek
  publishes that field beside ``prompt_cache_miss_tokens``, the two
  summing to ``prompt_tokens``, which is the measurement that says
  "subset" rather than "extra".
  So FRESH input is ``input_tokens - cache_read_input_tokens``, and
  charging ``input_tokens`` at the fresh rate AND the cached count again
  would bill the cached half twice.
* ``output_tokens`` -- the WHOLE completion, hidden reasoning INCLUDED.
  Codex: ``output_tokens``. The vendors: ``completion_tokens``.
* ``reasoning_output_tokens`` -- the SUBSET of the above that was hidden
  reasoning. Codex: ``reasoning_output_tokens``. The vendors:
  ``completion_tokens_details.reasoning_tokens``. Reasoning is billed at
  the OUTPUT rate on all three vendors, so it contributes through
  ``output_tokens`` and is deliberately NOT added a second time. It is
  still carried, reported and tested, because "reasoning costs nothing"
  and "reasoning is already in the output count" look identical in a
  total and are very different claims.

A clamp guards the one shape that would corrupt the arithmetic: if a
vendor ever reports more cached tokens than prompt tokens, fresh input
floors at zero rather than going negative and CREDITING the session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

__all__ = [
    "PRICES",
    "STALE_AFTER_DAYS",
    "Price",
    "cost_of",
    "cost_of_counts",
    "default_model_for",
    "models_for",
    "price_for",
    "priced",
    "resolve_model",
    "sheet_age_days",
    "sheet_note",
    "sheet_read_on",
    "sources",
    "stale",
]

#: Tokens per unit of the published rates. Every vendor below publishes
#: dollars per MILLION tokens, so the sheet stores that unit unchanged
#: rather than a per-token float nobody can check against a web page.
TOKENS_PER_UNIT = 1_000_000

#: How old the sheet may get before it is called stale. Sixty days,
#: because that is roughly the cadence at which the three vendors below
#: have actually moved: DeepSeek and Z.ai reprice with model releases
#: (weeks), OpenAI with generations (months). It is a WARNING threshold,
#: never a refusal -- a stale price still bounds better than no price,
#: and a release that expired its own ceiling on an aeroplane would be a
#: worse failure than an old number that is loudly labelled old.
STALE_AFTER_DAYS = 60


@dataclass(frozen=True)
class Price:
    """What one model costs, per million tokens, and where that came from.

    ``source`` and ``read_on`` are not documentation. They are the reason
    a reader can believe the other three fields, and a row without them
    cannot be constructed -- they have no defaults."""

    engine: str
    model: str
    #: FRESH input: the part of the prompt that was NOT served from cache.
    #: See the module docstring -- ``input_tokens`` includes the cached
    #: portion, and this rate applies to the difference.
    input_usd_per_mtok: float
    #: Cache-read input. Every vendor here prices it separately and far
    #: below fresh input, which is why the split is worth carrying.
    cached_input_usd_per_mtok: float
    #: Output, INCLUDING hidden reasoning tokens -- see the module
    #: docstring on why reasoning is not a fourth rate.
    output_usd_per_mtok: float
    #: The vendor's own pricing page. Not a doc mirror, not a blog post.
    source: str
    #: ISO date this row was read off that page, by a human or an agent
    #: who opened it. Per ROW, because a sheet is updated in pieces.
    read_on: str
    #: Which published rate this row is, when the vendor publishes more
    #: than one, and what the cheaper one was. Empty when the vendor
    #: publishes a single rate for the model.
    note: str = ""

    @property
    def label(self) -> str:
        return f"{self.engine}:{self.model}"

    def to_obj(self) -> "dict[str, object]":
        """The manifest's row. Every field, because a run's record of what
        bounded it is worthless if a reader has to come back to this file
        to find out what the numbers were."""
        return {
            "engine": self.engine,
            "model": self.model,
            "input_usd_per_mtok": self.input_usd_per_mtok,
            "cached_input_usd_per_mtok": self.cached_input_usd_per_mtok,
            "output_usd_per_mtok": self.output_usd_per_mtok,
            "source": self.source,
            "read_on": self.read_on,
            "note": self.note,
        }


# -- the sheet ---------------------------------------------------------
#
# Read on 2026-09-21 from each vendor's own pricing page, by fetching the
# page. Nothing below was recalled, inferred from a sibling model, or
# carried over from a previous release. A model absent from the page it
# would belong to is absent from this sheet -- see the UNPRICED comments,
# which name them rather than leaving a reader to notice the gap.

_DEEPSEEK_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing"
_GLM_SOURCE = "https://docs.z.ai/guides/overview/pricing"
_OPENAI_SOURCE = "https://developers.openai.com/api/docs/pricing"
_READ_ON = "2026-09-21"

# DeepSeek publishes TWO rates per model on one clock: peak (01:00-04:00
# and 06:00-10:00 UTC, Mon-Fri) and off-peak (everything else), the
# off-peak being exactly half. DOXA does not know which side of that
# clock a turn landed on -- a turn can straddle it -- so every row below
# is the PEAK rate and the note records the off-peak half it was chosen
# over. A session therefore stops at or before its share.
_DEEPSEEK: "tuple[Price, ...]" = (
    Price(
        engine="deepseek", model="deepseek-flash",
        input_usd_per_mtok=0.3,
        cached_input_usd_per_mtok=0.006,
        output_usd_per_mtok=1.2,
        source=_DEEPSEEK_SOURCE, read_on=_READ_ON,
        note="peak rate; off-peak is half (0.15 / 0.003 / 0.6)",
    ),
    Price(
        engine="deepseek", model="deepseek-v4-pro",
        input_usd_per_mtok=1.32,
        cached_input_usd_per_mtok=0.044,
        output_usd_per_mtok=3.96,
        source=_DEEPSEEK_SOURCE, read_on=_READ_ON,
        note="peak rate; off-peak is half (0.66 / 0.022 / 1.98)",
    ),
)

# Z.ai publishes ONE rate per model -- no peak clock, no service tiers --
# so these rows carry no note. Every model doxa.vendors.GLM lists is here
# EXCEPT `glm-5-turbo`, which the pricing page does not carry at all;
# it is therefore unpriced and a slot dealt it is reported unbounded by
# name. The two Flash models are published at zero, which is the vendor
# SAYING free rather than DOXA assuming it -- the distinction the whole
# module turns on.
_GLM: "tuple[Price, ...]" = (
    Price(
        engine="glm", model="glm-5.3-flash",
        input_usd_per_mtok=0.15, cached_input_usd_per_mtok=0.03,
        output_usd_per_mtok=0.5, source=_GLM_SOURCE, read_on=_READ_ON,
    ),
    Price(
        engine="glm", model="glm-5.3",
        input_usd_per_mtok=1.4, cached_input_usd_per_mtok=0.26,
        output_usd_per_mtok=4.4, source=_GLM_SOURCE, read_on=_READ_ON,
    ),
    Price(
        engine="glm", model="glm-5.2",
        input_usd_per_mtok=1.4, cached_input_usd_per_mtok=0.26,
        output_usd_per_mtok=4.4, source=_GLM_SOURCE, read_on=_READ_ON,
    ),
    Price(
        engine="glm", model="glm-5.1",
        input_usd_per_mtok=1.4, cached_input_usd_per_mtok=0.26,
        output_usd_per_mtok=4.4, source=_GLM_SOURCE, read_on=_READ_ON,
    ),
    Price(
        engine="glm", model="glm-5",
        input_usd_per_mtok=1.0, cached_input_usd_per_mtok=0.2,
        output_usd_per_mtok=3.2, source=_GLM_SOURCE, read_on=_READ_ON,
    ),
    Price(
        engine="glm", model="glm-4.7",
        input_usd_per_mtok=0.6, cached_input_usd_per_mtok=0.11,
        output_usd_per_mtok=2.2, source=_GLM_SOURCE, read_on=_READ_ON,
    ),
    Price(
        engine="glm", model="glm-4.6",
        input_usd_per_mtok=0.6, cached_input_usd_per_mtok=0.11,
        output_usd_per_mtok=2.2, source=_GLM_SOURCE, read_on=_READ_ON,
    ),
    Price(
        engine="glm", model="glm-4.5",
        input_usd_per_mtok=0.6, cached_input_usd_per_mtok=0.11,
        output_usd_per_mtok=2.2, source=_GLM_SOURCE, read_on=_READ_ON,
    ),
    Price(
        engine="glm", model="glm-4.5-air",
        input_usd_per_mtok=0.2, cached_input_usd_per_mtok=0.03,
        output_usd_per_mtok=1.1, source=_GLM_SOURCE, read_on=_READ_ON,
    ),
)

# OpenAI publishes up to four rates per model: standard, batch, flex and
# "fast" (the priority service tier). DOXA drives `codex exec` as a
# subprocess and inherits the operator's own ~/.codex/config.toml, which
# is where a service tier is chosen -- so DOXA cannot know which rate a
# turn was billed at. Every row below is the FAST rate, the highest
# published, and the note records the standard rate it was chosen over.
#
# UNPRICED, and named here rather than left as a gap: `gpt-reserve` and
# `codex-auto-review`, both offered by the local Codex CLI's model list
# and neither carried on the pricing page. A codex slot dealt one of
# those -- or dealt NO model, which is the common case, because `codex
# exec` then picks its own default and DOXA's capability map says
# `resolved_model=False` (the stream never names what answered) -- is
# reported unbounded by name.
_OPENAI: "tuple[Price, ...]" = (
    Price(
        engine="codex", model="gpt-6-astra",
        input_usd_per_mtok=20.0, cached_input_usd_per_mtok=2.0,
        output_usd_per_mtok=100.0,
        source=_OPENAI_SOURCE, read_on=_READ_ON,
        note="fast (priority) tier; standard is 10.0 / 1.0 / 50.0",
    ),
    Price(
        engine="codex", model="gpt-5.6-sol",
        input_usd_per_mtok=8.0, cached_input_usd_per_mtok=0.8,
        output_usd_per_mtok=40.0,
        source=_OPENAI_SOURCE, read_on=_READ_ON,
        note="fast (priority) tier; standard is 4.0 / 0.4 / 20.0",
    ),
    Price(
        engine="codex", model="gpt-5.6-terra",
        input_usd_per_mtok=4.0, cached_input_usd_per_mtok=0.4,
        output_usd_per_mtok=24.0,
        source=_OPENAI_SOURCE, read_on=_READ_ON,
        note="fast (priority) tier; standard is 2.0 / 0.2 / 12.0",
    ),
    Price(
        engine="codex", model="gpt-5.6-luna",
        input_usd_per_mtok=0.4, cached_input_usd_per_mtok=0.04,
        output_usd_per_mtok=2.4,
        source=_OPENAI_SOURCE, read_on=_READ_ON,
        note="fast (priority) tier; standard is 0.2 / 0.02 / 1.2",
    ),
    Price(
        engine="codex", model="gpt-5.5",
        input_usd_per_mtok=12.5, cached_input_usd_per_mtok=1.25,
        output_usd_per_mtok=75.0,
        source=_OPENAI_SOURCE, read_on=_READ_ON,
        note=(
            "fast (priority) tier, <272K context; standard is "
            "5.0 / 0.5 / 30.0"
        ),
    ),
    Price(
        engine="codex", model="gpt-5.3-codex",
        input_usd_per_mtok=3.5, cached_input_usd_per_mtok=0.35,
        output_usd_per_mtok=28.0,
        source=_OPENAI_SOURCE, read_on=_READ_ON,
        note="fast (priority) tier; standard is 1.75 / 0.175 / 14.0",
    ),
)

#: Every row, keyed by ``(engine, model)``. A tuple rather than a dict so
#: the declaration above reads as a sheet; the index is built once below.
PRICES: "tuple[Price, ...]" = _DEEPSEEK + _GLM + _OPENAI

_BY_KEY: "dict[tuple[str, str], Price]" = {
    (p.engine, p.model): p for p in PRICES
}


# -- lookup ------------------------------------------------------------


def _key(engine_id: "str | None", model: "str | None") -> "tuple[str, str]":
    return (
        (engine_id or "").strip().lower(),
        (model or "").strip().lower(),
    )


def price_for(engine_id: "str | None", model: "str | None") -> "Price | None":
    """The row for one model, or None when this sheet does not carry it.

    None is the REFUSAL, and every caller is required to treat it as one.
    There is deliberately no fuzzy match: ``deepseek-chat`` is a name
    DeepSeek answers with ``deepseek-flash`` (doxa.vendors' module
    docstring measured the substitution) and guessing that here would
    price a model by the name of a different one. The engine prices
    against the model that ANSWERED wherever the protocol reports it."""
    engine, name = _key(engine_id, model)
    if not engine or not name:
        return None
    return _BY_KEY.get((engine, name))


def priced(engine_id: "str | None", model: "str | None") -> bool:
    """Does this sheet carry a price for that model? The question
    :func:`doxa.budget.enforceable_for` asks on behalf of every surface
    that has to decide whether a ceiling means anything."""
    return price_for(engine_id, model) is not None


def models_for(engine_id: "str | None") -> "tuple[str, ...]":
    """Every model this sheet prices for one engine, sorted. What an
    error message owes a reader who named a model that is not here."""
    engine = (engine_id or "").strip().lower()
    return tuple(sorted(p.model for p in PRICES if p.engine == engine))


def default_model_for(engine_id: "str | None") -> "str | None":
    """Which model an engine runs when the operator names none.

    Asked because the fleet's pool entries are frequently bare
    (``--pool deepseek@1``), and a bare entry is not an unknown model --
    it is the engine's own default, which is a knowable fact. Read off
    :data:`doxa.vendors.VENDORS` rather than duplicated, so a vendor that
    changes its default does not leave a second copy here saying the old
    one.

    None for ``codex``, and that is the honest answer rather than a gap:
    ``codex exec`` with no ``-m`` picks a default from the operator's own
    ``~/.codex/config.toml``, DOXA never reads that file, and the JSON
    stream never names the model that answered (``resolved_model=False``
    in :data:`doxa.codex.CODEX_CAPABILITIES`). A bare codex slot is
    therefore genuinely unpriceable and is reported so by name.

    None for ``claude`` too, for the opposite reason: it reports its own
    dollars and never reaches this sheet."""
    engine = (engine_id or "").strip().lower()
    if engine not in {"deepseek", "glm"}:
        return None
    try:
        from .vendors import VENDORS

        spec = VENDORS.get(engine)
    except Exception:  # noqa: BLE001 -- an engine that cannot import has no default
        return None
    return None if spec is None else spec.default_model


def resolve_model(
    engine_id: "str | None", model: "str | None"
) -> "str | None":
    """The model a slot will actually run: what was asked for, or the
    engine's default when nothing was. The one place that fallback
    happens, so the fleet's note, the manifest and the engine's own
    charging cannot disagree about which model a bare pool entry meant."""
    named = (model or "").strip()
    return named or default_model_for(engine_id)


# -- the arithmetic ----------------------------------------------------


def cost_of_counts(
    price: Price,
    *,
    input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    output_tokens: int = 0,
) -> float:
    """Dollars for one call's tokens, under one row of the sheet.

    ``input_tokens`` is the WHOLE prompt and ``cache_read_input_tokens``
    is the subset of it that was cached, so the fresh portion is the
    difference and the cached portion is charged once, at its own rate.
    ``output_tokens`` is the whole completion, hidden reasoning included,
    and reasoning is billed at the output rate -- so there is no fourth
    term here and adding one would double-charge it. The module docstring
    records which field of which vendor's stream each of these came from.

    The clamp matters: a vendor reporting more cached tokens than prompt
    tokens would otherwise produce NEGATIVE fresh input and credit the
    session, which is the one arithmetic error a spend ceiling must not
    be capable of."""
    fresh = max(0, int(input_tokens) - int(cache_read_input_tokens))
    cached = max(0, int(cache_read_input_tokens))
    out = max(0, int(output_tokens))
    return (
        fresh * price.input_usd_per_mtok
        + cached * price.cached_input_usd_per_mtok
        + out * price.output_usd_per_mtok
    ) / TOKENS_PER_UNIT


def cost_of(
    engine_id: "str | None",
    model: "str | None",
    counts: "dict[str, int] | None",
) -> "float | None":
    """Dollars for one call, or None when this sheet cannot say.

    `counts` is the four-key shape both engines normalise into --
    ``input_tokens``, ``cache_read_input_tokens``, ``output_tokens`` and
    ``reasoning_output_tokens`` (carried, reported, and not a separate
    charge; see the module docstring).

    None means REFUSE, and it means it for two different reasons that the
    caller must treat identically: no row for that model, or no model
    named at all. Returning 0.0 for either would be the "silently treated
    as free" failure this module was written to remove."""
    price = price_for(engine_id, model)
    if price is None:
        return None
    counts = counts or {}
    return cost_of_counts(
        price,
        input_tokens=_count(counts.get("input_tokens")),
        cache_read_input_tokens=_count(counts.get("cache_read_input_tokens")),
        output_tokens=_count(counts.get("output_tokens")),
    )


def _count(value: "object | None") -> int:
    """One token count off a wire dict, or 0. Bools are excluded because
    ``isinstance(True, int)`` is True and a vendor sending ``true`` where
    a count belongs must not be charged as one token."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


# -- how old is this sheet? --------------------------------------------


def sheet_read_on() -> str:
    """The OLDEST ``read_on`` in the sheet, as an ISO date.

    Oldest, not newest: a sheet is as fresh as its stalest row, and
    reporting the newest would let one updated row make eleven old ones
    look current."""
    return min(p.read_on for p in PRICES)


def sources() -> "tuple[str, ...]":
    """Every pricing page this sheet was read from, sorted. Printed by
    the fleet and recorded in the manifest, so "which price data bounded
    this run" is answerable without opening this file."""
    return tuple(sorted({p.source for p in PRICES}))


def sheet_age_days(today: "date | None" = None) -> "int | None":
    """How many days since the oldest row was read, or None when the date
    cannot be parsed. `today` is injectable so the suite can age the
    sheet without waiting sixty days."""
    try:
        read = date.fromisoformat(sheet_read_on())
    except ValueError:
        return None
    return ((today or date.today()) - read).days


def stale(today: "date | None" = None) -> bool:
    """Is this sheet older than :data:`STALE_AFTER_DAYS`?

    A sheet whose date cannot be parsed counts as stale -- an unreadable
    date is not evidence of freshness."""
    age = sheet_age_days(today)
    return True if age is None else age > STALE_AFTER_DAYS


def sheet_note(today: "date | None" = None) -> str:
    """One sentence naming the price data in force, for the operator to
    read before a run and for the manifest to keep afterwards.

    The same job :func:`doxa.fleet.capacity_note` does for memory: a
    number that bounded a run and cannot be dated afterwards is a number
    nobody can audit the bill against."""
    age = sheet_age_days(today)
    aged = "of unreadable age" if age is None else f"{age} days old"
    note = (
        f"price sheet read {sheet_read_on()} ({aged}, {len(PRICES)} models "
        f"from {len(sources())} vendor pricing pages)"
    )
    if stale(today):
        note += (
            f" -- STALE: older than {STALE_AFTER_DAYS} days, so any ceiling "
            "it enforces is bounded by numbers that may have moved. Re-read "
            "the sources in doxa/prices.py"
        )
    return note
