#!/usr/bin/env python3
"""Quote the committed development pilot without provider calls or task execution."""
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from pilot_ledger import micros, token_cost

ROOT = Path(__file__).resolve().parent


def quote():
    raw = (ROOT / "pilot.json").read_bytes()
    pilot = json.loads(raw)
    corpus_raw = (ROOT / "corpus.json").read_bytes()
    corpus = json.loads(corpus_raw)
    by_id = {row["id"]: row for row in corpus["tasks"]}
    tasks = pilot["tasks"]
    if len(set(tasks)) != len(tasks) or any(by_id[name]["split"] != "development" for name in tasks):
        raise ValueError("pilot tasks must be distinct development tasks")
    allocations = {key: Decimal(value) for key, value in pilot["allocations_usd"].items()}
    if any(value < 0 for value in allocations.values()) or sum(allocations.values()) != Decimal(pilot["authorized_usd"]):
        raise ValueError("allocations must total the authorized ceiling")
    trials = len(tasks) * len(pilot["arms"]) * pilot["trials_per_task_per_arm"]
    calls = trials * pilot["max_calls_per_trial"]
    bounds = []
    for candidate in pilot["candidates"]:
        rate_in = Decimal(candidate["input_usd_per_million"])
        rate_out = Decimal(candidate["output_usd_per_million"])
        if min(rate_in, rate_out) < 0:
            raise ValueError("prices must be nonnegative")
        cost = (pilot["max_input_tokens"] * rate_in + pilot["max_output_tokens"] * rate_out) / 1_000_000
        held_micros = token_cost(pilot["max_input_tokens"], pilot["max_output_tokens"], micros(rate_in), micros(rate_out))
        bounds.append({"model": candidate["model"], "effort": candidate["effort"],
                       "request_bound_usd": str(cost), "request_hold_usd_micros": held_micros})
    worst = max(Decimal(row["request_bound_usd"]) for row in bounds) * calls
    worst_hold_micros = max(row["request_hold_usd_micros"] for row in bounds) * calls
    if worst_hold_micros > micros(allocations["repair"]):
        raise ValueError("repair allocation cannot cover the declared worst case")
    decisions = len(tasks) * pilot["trials_per_task_per_arm"] * pilot["max_calls_per_trial"]
    decision_input_bound = pilot["max_decision_input_bytes"] + pilot["decision_input_framing_tokens"]
    decision_hold_micros = token_cost(decision_input_bound, 0, micros(pilot["decision_input_usd_per_million"]), 0)
    if decision_hold_micros * decisions > micros(allocations["decisions"]):
        raise ValueError("decision allocation cannot cover the declared worst case")
    if pilot["max_fleets"] * Decimal(pilot["usd_per_fleet"]) > allocations["fleet_development"]:
        raise ValueError("fleet allocations exceed their ceiling")
    return {
        "schema": "doxa-evaluation-pilot-quote-v1",
        "pilot_sha256": hashlib.sha256(raw).hexdigest(),
        "corpus_sha256": hashlib.sha256(corpus_raw).hexdigest(),
        "trials": trials,
        "maximum_repair_calls": calls,
        "maximum_all_pro_repair_usd": str(worst),
        "maximum_all_pro_admission_usd_micros": worst_hold_micros,
        "maximum_decisions": decisions,
        "maximum_decision_admission_usd_micros": decision_hold_micros * decisions,
        "candidate_request_bounds": bounds,
        "allocations_usd": pilot["allocations_usd"],
        "authorized_usd": pilot["authorized_usd"],
        "paid_requests": 0,
        "execution_ready": False,
        "remaining_prerequisites": [
            "provider_transport_with_exact_request_admission",
            "fleet_launch_and_resume_bound_to_ledger",
            "pinned_rootless_grader_image",
            "provider_account_and_exact_price_verification",
            "provider_observations_bound_to_approved_grades",
            "guide_freeze_and_independent_label_collection",
        ],
        "note": "Offline arithmetic against declared price bounds. This quote neither enforces spend nor establishes live prices, credentials, task success or quality.",
    }


if __name__ == "__main__":
    print(json.dumps(quote(), indent=2, sort_keys=True))
