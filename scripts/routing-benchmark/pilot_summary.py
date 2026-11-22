#!/usr/bin/env python3
"""Join private pilot observations with durable accounting and emit totals only."""

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sqlite3

import benchmark as b
from pilot_ledger import JEV_ARM, PilotLedger, bounded_int, check_digest, policy

MANIFEST_SCHEMA = "doxa-pilot-trial-observations-v1"
OUTCOMES = (
    "functional_pass", "functional_failure", "provider_failure",
    "admission_refusal", "incomplete", "grader_infrastructure_failure",
)
GRADER_FAILURES = (
    "engine_unavailable", "image_unavailable", "container_start", "cleanup_unconfirmed",
)
CASE_FAILURES = (None, "timeout", "output_limit", "candidate_process_failed", "invalid_output")


def header(run_id, accounting):
    return {
        "schema": MANIFEST_SCHEMA,
        "ledger_run_id": run_id,
        "pilot_sha256": accounting["pilot_sha256"],
        "corpus_sha256": accounting["corpus_sha256"],
    }


def position(allocation, slot):
    return slot, allocation == "repair"


def classify_grade(report, task_id, corpus_sha256, driver_sha256, task):
    b.require(isinstance(report, dict) and report.get("schema") == "doxa-repair-grade-v1"
              and report.get("task_id") == task_id and report.get("corpus_sha256") == corpus_sha256
              and report.get("driver_sha256") == driver_sha256 and report.get("split") == "development"
              and report.get("family") == task["family"] and report.get("difficulty") == task["difficulty"]
              and report.get("network") == "none" and report.get("docker_host") == "local_rootless_unix_socket",
              "grade does not match the frozen task and grader")
    for key in ("approval_sha256", "source_sha256"):
        check_digest(report.get(key))
    image = report.get("image")
    b.require(isinstance(image, str) and image.startswith("sha256:"), "pinned grade image required")
    check_digest(image.removeprefix("sha256:"))
    b.require(report.get("source_readonly") is True
              and all(report.get(key) is False for key in (
                  "trusted_reference_self_check", "evaluator_answers_mounted",
                  "provider_credentials_passed", "host_candidate_execution",
                  "model_quality_validated", "human_labels",
              )), "grade protections or provenance differ from the pilot")
    expected = {(split, index) for split in ("public_cases", "evaluator_cases")
                for index in range(len(task[split]))}
    case_count = len(expected)
    tests = report.get("tests")
    b.require(isinstance(tests, list) and len(tests) == case_count, "grade case roster is incomplete")
    for result in tests:
        b.require(isinstance(result, dict) and type(result.get("passed")) is bool
                  and bounded_int(result.get("index"), 0, case_count - 1)
                  and result.get("set") in ("public_cases", "evaluator_cases")
                  and "failure" in result and result["failure"] in CASE_FAILURES
                  and (result["failure"] is None or result["passed"] is False)
                  and bounded_int(result.get("duration_ms"), 0, 2**32 - 1),
                  "invalid grade case observation")
        case = result["set"], result["index"]
        b.require(case in expected, "unknown or duplicate grade case")
        expected.remove(case)
    passed = all(result["passed"] for result in tests)
    b.require(report.get("passed") is passed, "grade aggregate does not match its observations")
    if any(result["failure"] is not None for result in tests):
        return "incomplete"
    return "functional_pass" if passed else "functional_failure"


def classify_trial(row, calls, plan, accounting, driver_sha256, task):
    status = row.get("status")
    common = {"task_id", "arm", "status"}
    if status == "incomplete":
        b.require(row.keys() == common, "unsupported incomplete trial fields")
        return status
    if status == "grader_infrastructure_failure" and not calls:
        b.require(row.keys() == common | {"failure"} and row["failure"] in GRADER_FAILURES,
                  "unknown grader infrastructure failure category")
        return status

    shape = common | {"allocation", "call"}
    allocation, slot = row.get("allocation"), row.get("call")
    b.require(allocation in ("repair", "decisions")
              and (allocation != "decisions" or row["arm"] == JEV_ARM)
              and bounded_int(slot, 1, plan["max_calls_per_trial"]), "trial event is outside pilot scope")
    event = position(allocation, slot)
    b.require(all(position(*key) <= event for key in calls), "trial outcome precedes a later reservation")
    registered = calls.get((allocation, slot))
    if status == "admission_refusal":
        b.require(row.keys() == shape and registered is None,
                  "admission refusal conflicts with a committed reservation")
        return status

    shape.add("request_sha256")
    b.require(registered is not None and registered["request_sha256"] == row.get("request_sha256"),
              "trial outcome does not match a reserved request")
    if status == "provider_failure":
        b.require(row.keys() == shape, "unsupported provider failure fields")
        return status

    b.require(allocation == "repair", "grade outcome must bind to a repair reservation")
    if status == "grader_infrastructure_failure":
        b.require(row.keys() == shape | {"failure"} and row["failure"] in GRADER_FAILURES,
                  "unknown grader infrastructure failure category")
        return status

    b.require(status == "graded" and row.keys() == shape | {"grade"}, "unknown or invalid trial outcome")
    outcome = classify_grade(row["grade"], row["task_id"], accounting["corpus_sha256"], driver_sha256, task)
    b.require(registered["settled"] and registered.get("grade_sha256") == b.digest(b.encode(row["grade"])),
              "grade does not bind to the settled repair reservation")
    return outcome


def summarize(ledger, manifest):
    plan, _, binding = policy()
    run_id, accounting, reservations = ledger.reporting_snapshot()
    expected_header = header(run_id, accounting)
    b.require((accounting["pilot_sha256"], accounting["corpus_sha256"]) == binding
              and isinstance(manifest, dict) and manifest.keys() == expected_header.keys() | {"trials"}
              and all(manifest[key] == value for key, value in expected_header.items()),
              "private trial manifest does not match this ledger, plan and corpus")
    roster = {(task, arm) for task in plan["tasks"] for arm in plan["arms"]}
    rows = manifest["trials"]
    b.require(isinstance(rows, list) and len(rows) <= len(roster), "invalid trial roster length")
    driver_sha256 = b.digest(b.repo_read("driver.py"))
    corpus_raw = b.repo_read("corpus.json", 2 * 1024 * 1024)
    b.require(b.digest(corpus_raw) == accounting["corpus_sha256"], "pilot corpus changed during summary")
    development = [task for task in b.parse(corpus_raw)["tasks"] if task["id"] in plan["tasks"]]
    tasks = {task["id"]: task for task in development if task["split"] == "development"}
    b.require(set(tasks) == set(plan["tasks"]) and len(development) == len(tasks),
              "pilot roster does not match the frozen development corpus")
    events = defaultdict(dict)
    for reservation in reservations:
        scope = reservation["task_id"], reservation["arm"]
        b.require(scope in roster, "ledger trial lies outside the frozen roster")
        events[scope][reservation["allocation"], reservation["slot"]] = reservation
    recorded = set()
    counts = Counter()
    case_count = 0
    case_duration_ms = 0
    for row in rows:
        b.require(isinstance(row, dict) and isinstance(row.get("task_id"), str)
                  and isinstance(row.get("arm"), str), "invalid trial scope")
        scope = row["task_id"], row["arm"]
        b.require(scope in roster and scope not in recorded, "unknown or duplicate pilot trial")
        recorded.add(scope)
        outcome = classify_trial(row, events[scope], plan, accounting, driver_sha256, tasks[row["task_id"]])
        counts[outcome] += 1
        if row["status"] == "graded":
            observations = row["grade"]["tests"]
            case_count += len(observations)
            case_duration_ms += sum(case["duration_ms"] for case in observations)
    counts["incomplete"] += len(roster) - len(recorded)
    return {
        "schema": "doxa-development-pilot-summary-v1",
        "planned_trials": len(roster),
        "completed_trials": counts["functional_pass"] + counts["functional_failure"],
        "outcomes": {status: counts[status] for status in OUTCOMES},
        "grader_observations": {"cases": case_count, "total_duration_ms": case_duration_ms},
        "accounting": accounting,
        "model_quality_validated": False,
        "provider_billing_verified": False,
        "human_labels": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("template", "summarize"))
    parser.add_argument("ledger", type=Path)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    ledger = PilotLedger.open(args.ledger)
    manifest_path = args.manifest.absolute()
    b.private_directory(manifest_path.parent)
    if args.action == "template":
        run_id, accounting, _ = ledger.reporting_snapshot()
        b.create(manifest_path, {**header(run_id, accounting), "trials": []})
    else:
        manifest = b.parse(b.read(manifest_path, 256 * 1024))
        print(json.dumps(summarize(ledger, manifest), indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, RecursionError, sqlite3.Error):
        raise SystemExit("pilot summary refused: input is not a matching private ledger and observation manifest")
