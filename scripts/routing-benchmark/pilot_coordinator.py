#!/usr/bin/env python3
"""Sequence development repairs, preserve uncertain holds and bind reviewed grades."""

import argparse
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import sqlite3
import subprocess

import benchmark as b
import pilot_bridge
import pilot_summary
from pilot_dispatch import proposal_body
from pilot_ledger import JEV_ARM, LedgerRefused, PilotLedger, policy


@contextmanager
def locked(state):
    b.private_directory(state)
    fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def initialize(state, host, image):
    b.private_directory(state.parent)
    grader = b.grader_preflight(host, image)
    state.mkdir(mode=0o700)
    ledger = PilotLedger.create(state / "pilot.sqlite")
    run_id, usage, _ = ledger.reporting_snapshot()
    config_sha256 = b.create(state / "run.json", {
        **pilot_summary.header(run_id, usage), "grader_host": host, "grader_image": grader["image"],
    })
    ledger.bind_coordinator(config_sha256)
    return run_id


def open_run(state):
    ledger = PilotLedger.open(state / "pilot.sqlite")
    raw = b.read(state / "run.json")
    ledger.verify_coordinator(b.digest(raw))
    config = b.parse(raw)
    run_id, usage, _ = ledger.reporting_snapshot()
    identity = pilot_summary.header(run_id, usage)
    b.require(config.keys() == identity.keys() | {"grader_host", "grader_image"}
              and all(config[key] == value for key, value in identity.items()),
              "coordinator state does not match its pilot ledger")
    return ledger, config


def trial_path(state, task, arm):
    plan, _, _ = policy()
    b.require(task in plan["tasks"] and arm in plan["arms"], "trial is outside the frozen roster")
    return state / f"{task}-{arm}"


def trial_calls(ledger, task, arm):
    return [row for row in ledger.reporting_snapshot()[2]
            if (row["task_id"], row["arm"]) == (task, arm)]


def trial_event(task, arm, call):
    return {"task_id": task, "arm": arm, "allocation": call["allocation"], "call": call["slot"],
            "request_sha256": call["request_sha256"]}


def request_for_trial(trial):
    plan, _, _ = policy()
    return b.encode({
        "model": "doxa-pilot",
        "messages": [
            {"role": "system", "content": "Return only a repair proposal JSON object for the supplied task."},
            {"role": "user", "content": b.encode(pilot_bridge.candidate_context(trial)).decode("utf-8")},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "max_tokens": plan["max_output_tokens"],
        "stream": False,
    }).decode("utf-8")


def run_trial(state, task, arm, dispatch):
    """Submit one proposal request for a frozen arm through an admitted transport."""
    trial = trial_path(state, task, arm)
    with locked(state):
        ledger, config = open_run(state)
        b.require(not trial_calls(ledger, task, arm) and not os.path.lexists(trial / "observation.json"),
                  "trial was already dispatched or has a terminal outcome")
        b.grader_preflight(config["grader_host"], config["grader_image"])
        if not trial.exists():
            b.prepare(task, trial)
        request = request_for_trial(trial)
        try:
            outcome, response = dispatch(ledger, (task, arm, 1), request)
        except LedgerRefused:
            outcome, response = "refused", None
        calls = trial_calls(ledger, task, arm)
        latest = max(calls, key=lambda row: pilot_summary.position(row["allocation"], row["slot"]), default=None)
        if latest is None:
            b.require(outcome == "refused", "transport did not record an admitted request")
            row = {"task_id": task, "arm": arm, "allocation": "decisions" if arm == JEV_ARM else "repair",
                   "call": 1, "status": "admission_refusal"}
        else:
            row = trial_event(task, arm, latest)
            if outcome == "settled" and latest["allocation"] == "repair":
                proposal = proposal_body(response)
                if proposal and b.digest(proposal) == latest.get("proposal_sha256"):
                    b.create(trial / "proposal.json", proposal)
                    try:
                        review = pilot_bridge.review(trial, trial / "proposal.json")
                    except ValueError:
                        pass  # A settled bill can still carry an invalid proposal.
                    else:
                        return {**row, "status": "awaiting_review", "approval_sha256": review["approval_sha256"]}
            row["status"] = "provider_failure"
        b.create(trial / "observation.json", row)
        return row


def grade_trial(state, task, arm, approval):
    trial = trial_path(state, task, arm)
    with locked(state):
        ledger, config = open_run(state)
        proposal = trial / "proposal.json"
        review = pilot_bridge.review(trial, proposal)
        b.require(review["approval_sha256"] == approval, "review does not match the supplied approval")
        call = next((row for row in trial_calls(ledger, task, arm) if row["allocation"] == "repair"), None)
        b.require(call is not None and call["settled"] and review["proposal_sha256"] == call.get("proposal_sha256"),
                  "proposal does not match this trial's settled repair")
        graded = trial / "observation.json"
        revision = trial / ("repair-" + approval)
        if graded.exists():
            row = b.parse(b.read(graded))
        else:
            if not revision.exists():
                pilot_bridge.apply(trial, proposal, approval)
            report = pilot_bridge.grade(revision, config["grader_host"], config["grader_image"])
            row = {**trial_event(task, arm, call), "status": "graded", "grade": report}
            b.create(graded, row)
        report = row.get("grade", {})
        b.require(row == {**trial_event(task, arm, call), "status": "graded", "grade": report}
                  and report.get("approval_sha256") == approval
                  and report.get("source_sha256") == review["after_sha256"]
                  and report.get("image") == config["grader_image"],
                  "grade observation does not match the approved repair and grader")
        result = Path(report["result_directory"])
        b.require(result.parent == revision and re.fullmatch(r"grade-[a-f0-9]{32}", result.name),
                  "grade result belongs to another trial")
        b.private_directory(result)
        b.require(b.parse(b.read(result / "report.json")) ==
                  {key: value for key, value in report.items() if key != "approval_sha256"},
                  "grade result differs from the original offline grader report")
        ledger.record_repair_grade(task, arm, call["slot"], request_sha256=call["request_sha256"],
                                   proposal_sha256=review["proposal_sha256"],
                                   grade_sha256=b.digest(b.encode(report)))
        return row


def summary(state):
    with locked(state):
        ledger, _ = open_run(state)
        run_id, usage, _ = ledger.reporting_snapshot()
        plan, _, _ = policy()
        rows = []
        for task in plan["tasks"]:
            for arm in plan["arms"]:
                observed = trial_path(state, task, arm) / "observation.json"
                if observed.exists():
                    rows.append(b.parse(b.read(observed)))
        return pilot_summary.summarize(ledger, {**pilot_summary.header(run_id, usage), "trials": rows})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", type=Path)
    commands = parser.add_subparsers(dest="action", required=True)
    init = commands.add_parser("initialize")
    init.add_argument("--docker-host", required=True)
    init.add_argument("--image", required=True)
    commands.add_parser("summary")
    for action in ("review", "grade"):
        selected = commands.add_parser(action)
        selected.add_argument("task")
        selected.add_argument("arm")
        if action == "grade":
            selected.add_argument("--approval", required=True)
    args = parser.parse_args()
    if args.action == "initialize":
        result = {"ledger_run_id": initialize(args.state, args.docker_host, args.image)}
    elif args.action == "summary":
        result = summary(args.state)
    elif args.action == "grade":
        result = grade_trial(args.state, args.task, args.arm, args.approval)
    else:
        trial = trial_path(args.state, args.task, args.arm)
        with locked(args.state):
            open_run(args.state)
            result = pilot_bridge.review(trial, trial / "proposal.json")
    print(b.encode(result).decode("utf-8"))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
        raise SystemExit("pilot coordinator refused: " + str(error))
