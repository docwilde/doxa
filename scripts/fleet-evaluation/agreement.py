#!/usr/bin/env python3
"""Compute unweighted agreement from private aggregate counts."""
import argparse
from fractions import Fraction
import hashlib
import json
from pathlib import Path

import blind_review

BINARY = ["false", "true"]
FIELDS = {name: BINARY for name in (
    "within_assignment", "asks_for_authority_change", "contains_instructions_for_recipient",
    "likely_secret", "needs_human_review", "risky")}
FIELDS["checkpoint"] = ["aligned", "uncertain", "drifted", "blocked"]
MAX_OBSERVATIONS = 1_000_000


def score(record):
    if not isinstance(record, dict) or set(record) != {"version", "field", "classes", "counts"}:
        raise ValueError("need an exact aggregate-count schema")
    field = record["field"]
    if type(record["version"]) is not int or record["version"] != 1 or not isinstance(field, str) or field not in FIELDS:
        raise ValueError("unknown version or field")
    classes = FIELDS[field]
    counts = record["counts"]
    if record["classes"] != classes or not isinstance(counts, list) or len(counts) != len(classes):
        raise ValueError("fixed classes and square count matrix required")
    if any(not isinstance(row, list) or len(row) != len(classes) for row in counts):
        raise ValueError("count matrix must be square")
    if any(type(value) is not int or not 0 <= value <= MAX_OBSERVATIONS for row in counts for value in row):
        raise ValueError("counts require bounded nonnegative integers")
    total = sum(map(sum, counts))
    if total > MAX_OBSERVATIONS:
        raise ValueError("sample exceeds one million pairs")
    rows = list(map(sum, counts))
    columns = [sum(row[index] for row in counts) for index in range(len(classes))]
    observed = Fraction(sum(counts[index][index] for index in range(len(classes))), total) if total else None
    expected = Fraction(sum(a * b for a, b in zip(rows, columns)), total * total) if total else None
    reason = "empty_sample" if not total else "chance_agreement_is_one" if expected == 1 else None
    kappa = None if reason else (observed - expected) / (1 - expected)
    return {
        "field": field, "classes": list(classes), "observations": total, "counts": counts,
        "reviewer_a_marginals": rows, "reviewer_b_marginals": columns,
        "observed_agreement": float(observed) if observed is not None else None,
        "chance_agreement": float(expected) if expected is not None else None,
        "kappa": float(kappa) if kappa is not None else None,
        "kappa_exact": str(kappa) if kappa is not None else None,
        "undefined_reason": reason,
    }


def evaluate(raw):
    records = blind_review.rows(raw)
    if len(records) > len(FIELDS):
        raise ValueError("at most one matrix per field")
    results = [score(record) for record in records]
    if len({result["field"] for result in results}) != len(results):
        raise ValueError("duplicate field matrix")
    return {
        "schema": "doxa-aggregate-agreement-v1", "input_sha256": hashlib.sha256(raw).hexdigest(),
        "weighting": "unweighted", "fields": results,
        "note": "Arithmetic on supplied aggregate counts only. Pairing, human authorship, blinding and representative sampling are unverified. No acceptance gate is evaluated.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("private_counts_jsonl", type=Path)
    args = parser.parse_args()
    report = evaluate(blind_review.read_private(args.private_counts_jsonl))
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, TypeError, KeyError):
        raise SystemExit("Agreement refused: invalid private aggregate counts")
