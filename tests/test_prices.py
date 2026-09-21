# SPDX-License-Identifier: AGPL-3.0-only
"""The price sheet, and the arithmetic that turns tokens into a ceiling.

Every test here is named for the failure it catches, and the failures are
the ones that would make a ceiling worse than no ceiling:

* a row with no source or no date -- an unverifiable number that a later
  reader has no way to check or refresh;
* a model with no entry quietly costing nothing, which is the one bug
  this whole module exists to prevent: an unbounded slot that LOOKS
  bounded;
* cached input charged twice, or as if it were fresh -- the cached count
  is a SUBSET of the prompt count on every vendor here, and treating it
  as an extra would over-bill by the whole cached prompt;
* reasoning output charged twice, for the mirror reason: it is a subset
  of the completion count and is billed at the output rate;
* a sheet whose age nobody can read, so a run cannot say what price data
  bounded it.
"""

from __future__ import annotations

from datetime import date

import pytest

from doxa import budget as budget_mod
from doxa import prices as prices_mod


# -- the sheet itself ---------------------------------------------------


def test_every_row_names_the_page_it_came_from_and_the_day_it_was_read():
    """The property that separates this sheet from a guess. A rate whose
    provenance is not on the row is a rate nobody can re-check, and a
    sheet of those is exactly the "confident wrong number" doxa.budget's
    own docstring refuses."""
    assert prices_mod.PRICES, "an empty sheet bounds nothing"
    for row in prices_mod.PRICES:
        assert row.source.startswith("https://"), (
            f"{row.label} cites {row.source!r} -- a row's source must be the "
            "vendor's own pricing page"
        )
        # Raises if it is not a real ISO date, which is the point: a
        # date nobody can parse cannot be aged, and an unageable sheet
        # can never be called stale.
        read = date.fromisoformat(row.read_on)
        assert read <= date.today(), f"{row.label} was read in the future"
        for rate in (
            row.input_usd_per_mtok,
            row.cached_input_usd_per_mtok,
            row.output_usd_per_mtok,
        ):
            assert isinstance(rate, float) and rate >= 0.0


def test_cached_input_is_never_dearer_than_fresh_input():
    """Not a style check -- an inverted pair would mean a row was
    transcribed into the wrong columns, and every session on that model
    would then be bounded by arithmetic that is wrong in the dangerous
    direction on the largest term (prompts are mostly cache on a long
    session)."""
    for row in prices_mod.PRICES:
        assert row.cached_input_usd_per_mtok <= row.input_usd_per_mtok, (
            f"{row.label}: cached input priced above fresh input -- the "
            "columns are almost certainly swapped"
        )


def test_the_sheet_prices_no_claude_model():
    """Claude reports its own dollars, from the party doing the billing.
    A DOXA-maintained rate for it would be a second, worse answer to a
    question already answered -- and the first place the two would
    disagree is a bill."""
    assert not [p for p in prices_mod.PRICES if p.engine == "claude"]
    assert prices_mod.price_for("claude", "sonnet") is None
    assert budget_mod.enforcement_basis("claude", "sonnet") == (
        budget_mod.BASIS_REPORTED
    )


# -- the refusal --------------------------------------------------------


def test_a_model_with_no_entry_gets_no_price_rather_than_a_default():
    """THE test this module exists for. `glm-5-turbo` is a model
    doxa.vendors.GLM offers and the Z.ai pricing page does not carry, so
    the sheet does not carry it either -- and the answer is None, not a
    sibling's rate and not zero."""
    assert prices_mod.price_for("glm", "glm-5.3-flash") is not None, (
        "the control: a model that IS priced"
    )
    assert prices_mod.price_for("glm", "glm-5-turbo") is None
    assert prices_mod.cost_of(
        "glm", "glm-5-turbo",
        {"input_tokens": 10_000_000, "output_tokens": 10_000_000},
    ) is None, (
        "ten million tokens of an unpriced model must not convert to "
        "$0.00 -- that is an unbounded session wearing a bound one's face"
    )


def test_an_unpriced_model_is_not_quietly_matched_to_a_similar_name():
    """`deepseek-chat` is a real name DeepSeek ANSWERS -- with
    deepseek-flash, and it says so only in the response's own model field
    (doxa.vendors' module docstring measured the substitution). Guessing
    that here would price one model at another's rate on the strength of
    a prefix."""
    assert prices_mod.price_for("deepseek", "deepseek-chat") is None
    assert prices_mod.price_for("deepseek", "deepseek") is None
    assert prices_mod.price_for("glm", "glm") is None


def test_a_bare_engine_resolves_to_its_default_model_where_one_is_knowable():
    """A pool entry that names no model is not an unknown model -- it is
    the engine's own default, which is a fact. Except on codex, where the
    default lives in the operator's ~/.codex/config.toml and the stream
    never names what answered, so "unknown" is the honest answer."""
    assert prices_mod.resolve_model("deepseek", None) == "deepseek-flash"
    assert prices_mod.resolve_model("glm", None) == "glm-5.3-flash"
    assert prices_mod.resolve_model("codex", None) is None
    assert prices_mod.resolve_model("deepseek", "deepseek-v4-pro") == (
        "deepseek-v4-pro"
    )


# -- the arithmetic -----------------------------------------------------


def test_cached_input_is_the_subset_of_the_prompt_and_not_an_extra():
    """The semantics read off the parsers rather than assumed. On every
    vendor here the cached count is PART of the prompt count -- DeepSeek
    publishes prompt_cache_hit_tokens beside prompt_cache_miss_tokens and
    the two sum to prompt_tokens -- so the fresh charge is the difference.

    Charging the whole prompt at the fresh rate AND the cached count
    again would over-bill by the cached prompt, which on a long session
    is most of it."""
    row = prices_mod.price_for("deepseek", "deepseek-flash")
    assert row is not None

    cost = prices_mod.cost_of_counts(
        row, input_tokens=1_000_000, cache_read_input_tokens=400_000,
        output_tokens=0,
    )
    # 600k fresh at $0.30/Mtok + 400k cached at $0.006/Mtok.
    assert cost == pytest.approx(0.6 * 0.3 + 0.4 * 0.006)

    naive = 1.0 * 0.3 + 0.4 * 0.006
    assert cost < naive, (
        "the cached half was charged at the fresh rate as well -- the "
        "cached count is a subset, not an addition"
    )


def test_reasoning_output_is_billed_as_output_and_never_a_second_time():
    """`reasoning_output_tokens` (codex) and
    `completion_tokens_details.reasoning_tokens` (both vendors) are the
    hidden part of the completion, already inside the output count and
    billed at the output rate. A session with heavy reasoning and one
    with none, both reporting the same output total, cost the same."""
    row = prices_mod.price_for("glm", "glm-5.3")
    assert row is not None

    with_reasoning = prices_mod.cost_of(
        "glm", "glm-5.3",
        {"input_tokens": 1000, "output_tokens": 2000,
         "reasoning_output_tokens": 1900},
    )
    without = prices_mod.cost_of(
        "glm", "glm-5.3",
        {"input_tokens": 1000, "output_tokens": 2000,
         "reasoning_output_tokens": 0},
    )
    assert with_reasoning == without == pytest.approx(
        (1000 * 1.4 + 2000 * 4.4) / 1_000_000
    ), "reasoning was charged a second time on top of the output it is in"


def test_more_cached_tokens_than_prompt_tokens_never_credits_a_session():
    """The one arithmetic error a spend ceiling must be incapable of. A
    vendor reporting a cached count above the prompt count would
    otherwise produce negative fresh input, which SUBTRACTS from the
    session's spend and lifts the ceiling by however wrong the vendor
    was."""
    row = prices_mod.price_for("codex", "gpt-5.6-sol")
    assert row is not None

    cost = prices_mod.cost_of_counts(
        row, input_tokens=100, cache_read_input_tokens=5_000, output_tokens=0,
    )
    assert cost > 0.0
    assert cost == pytest.approx(5_000 * 0.8 / 1_000_000), (
        "fresh input floored at zero, cached charged at the cached rate"
    )


def test_a_bool_where_a_count_belongs_is_not_charged_as_one_token():
    """``isinstance(True, int)`` is True in Python, so a vendor sending
    ``true`` in a count field would otherwise be billed as one token and,
    worse, read as a present measurement."""
    assert prices_mod.cost_of(
        "glm", "glm-5", {"input_tokens": True, "output_tokens": None},
    ) == pytest.approx(0.0)


def test_the_arithmetic_matches_the_published_rate_for_each_vendor_shape():
    """One worked example per vendor, computed from the page's own
    numbers, so a transcription error in the sheet shows up as red rather
    than as a slightly wrong ceiling nobody notices."""
    cases = (
        # engine, model, in, cached, out, expected $
        ("deepseek", "deepseek-flash", 10_000, 4_000, 2_000,
         (6_000 * 0.3 + 4_000 * 0.006 + 2_000 * 1.2) / 1_000_000),
        ("glm", "glm-4.5-air", 50_000, 50_000, 1_000,
         (0 * 0.2 + 50_000 * 0.03 + 1_000 * 1.1) / 1_000_000),
        ("codex", "gpt-5.3-codex", 8_000, 0, 4_000,
         (8_000 * 3.5 + 4_000 * 28.0) / 1_000_000),
    )
    for engine, model, tokens_in, cached, tokens_out, expected in cases:
        got = prices_mod.cost_of(engine, model, {
            "input_tokens": tokens_in,
            "cache_read_input_tokens": cached,
            "output_tokens": tokens_out,
        })
        assert got == pytest.approx(expected), f"{engine}:{model}"


# -- how old is this sheet? --------------------------------------------


def test_the_sheet_reports_its_own_age_off_its_oldest_row():
    """A sheet is as fresh as its stalest row. Reporting the NEWEST date
    would let one refreshed row make every old one beside it look
    current, which is the shape of a half-updated sheet nobody notices."""
    oldest = min(p.read_on for p in prices_mod.PRICES)
    assert prices_mod.sheet_read_on() == oldest
    assert prices_mod.sheet_age_days(date.fromisoformat(oldest)) == 0


def test_a_sheet_older_than_the_threshold_says_so_out_loud():
    """The requirement that a stale sheet be VISIBLE. The threshold is a
    warning and never a refusal -- an old price still bounds better than
    no price -- but a run bounded by one must be able to say so."""
    read = date.fromisoformat(prices_mod.sheet_read_on())
    fresh = date.fromordinal(read.toordinal() + prices_mod.STALE_AFTER_DAYS)
    old = date.fromordinal(read.toordinal() + prices_mod.STALE_AFTER_DAYS + 1)

    assert prices_mod.stale(fresh) is False
    assert prices_mod.stale(old) is True
    assert "STALE" not in prices_mod.sheet_note(fresh)
    note = prices_mod.sheet_note(old)
    assert "STALE" in note
    assert prices_mod.sheet_read_on() in note, (
        "a staleness warning that does not carry the date is one nobody "
        "can act on"
    )


def test_the_note_names_the_date_and_the_pages_so_a_run_can_record_them():
    note = prices_mod.sheet_note(date.fromisoformat(prices_mod.sheet_read_on()))
    assert prices_mod.sheet_read_on() in note
    assert str(len(prices_mod.PRICES)) in note
    assert prices_mod.sources(), "a sheet with no sources is a sheet of guesses"
    for url in prices_mod.sources():
        assert url.startswith("https://")
