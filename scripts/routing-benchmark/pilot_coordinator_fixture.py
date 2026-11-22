#!/usr/bin/env python3
"""Rehearse one matched development task through loopback and the real offline grader."""

import argparse
import json
from pathlib import Path
import tempfile

import benchmark as b
import pilot_admission_fixture as wire_fixture
import pilot_coordinator as coordinator
from pilot_dispatch import run_call
from pilot_ledger import FIXED_ARMS, JEV_ARM, PilotLedger


def exercise(state, adapter, host, image):
    coordinator.initialize(state, host, image)
    task_id = "easy-config"
    task = b.task(task_id)
    proposal = b.encode({"schema": "doxa-pilot-repair-proposal-v1", "files": task["reference"]}).decode()
    dispatches = {"decisions": 0, "repair": 0}
    first_grade = None

    def submit(ledger, scope, request):
        arm = scope[1]
        model = FIXED_ARMS[arm][0] if arm in FIXED_ARMS else "deepseek-v4-pro"

        def decide(body):
            dispatches["decisions"] += 1
            wire_fixture.verify_hold(state / "pilot.sqlite", scope, "decisions", body)
            return wire_fixture.fixture_reply("decisions", "success", model)

        def repair(body):
            dispatches["repair"] += 1
            wire_fixture.verify_hold(state / "pilot.sqlite", scope, "repair", body)
            forwarded = b.parse(body)
            b.require(forwarded["model"] == model and "tools" not in forwarded, "unexpected repair request")
            visible = b.parse(forwarded["messages"][-1]["content"])
            b.require(all(visible["files"][name] == text for name, text in task["starter"].items()),
                      "fixture repair did not receive the frozen starter")
            if arm == "fixed_pro_high":
                raise TimeoutError("synthetic disconnect after admission")
            return 200, b.encode({
                "object": "chat.completion", "model": model,
                "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": proposal}}],
                "usage": {"prompt_tokens": 25, "completion_tokens": 20, "total_tokens": 45},
            })

        return run_call(adapter, state, ledger, scope, request,
                        lambda body: wire_fixture.through_http(decide, body),
                        lambda body: wire_fixture.through_http(repair, body))

    for arm in ("fixed_flash_none", "fixed_pro_high", JEV_ARM):
        row = coordinator.run_trial(state, task_id, arm, submit)
        if arm == "fixed_pro_high":
            b.require(row["status"] == "provider_failure", "ambiguous fixture call was not retained")
            continue
        b.require(row["status"] == "awaiting_review", "candidate proposal did not reach owner review")
        if first_grade is not None:
            observed = coordinator.trial_path(state, task_id, arm) / "observation.json"
            event = {key: value for key, value in row.items() if key != "approval_sha256"}
            b.create(observed, {**event, "status": "graded", "grade": first_grade})
            try:
                coordinator.grade_trial(state, task_id, arm, row["approval_sha256"])
            except ValueError as error:
                b.require("grade result belongs to another trial" in str(error), "unexpected copied-grade refusal")
            else:
                raise ValueError("a grade from another arm was accepted")
            observed.unlink()
        grade = coordinator.grade_trial(state, task_id, arm, row["approval_sha256"])
        b.require(grade["grade"]["passed"], "reference fixture did not pass the offline grader")
        b.require(coordinator.grade_trial(state, task_id, arm, row["approval_sha256"]) == grade,
                  "resuming a recorded grade changed its receipt")
        first_grade = grade["grade"]
    try:
        coordinator.run_trial(state, task_id, "fixed_pro_high", submit)
    except ValueError:
        pass
    else:
        raise ValueError("restarted coordinator replayed a refused trial")
    report = coordinator.summary(state)
    b.require(dispatches == {"decisions": 1, "repair": 3} and report["completed_trials"] == 2
              and report["outcomes"]["provider_failure"] == 1
              and PilotLedger.open(state / "pilot.sqlite").summary()["allocations"]["repair"]["unresolved"] == 1,
              "fixture accounting or completed trial counts changed")
    return {"paid_requests": 0, "fixture_requests": dispatches, "summary": report}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("adapter", type=Path)
    parser.add_argument("--docker-host", required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="dpc-") as temporary:
        print(json.dumps(exercise(Path(temporary) / "run", args.adapter.resolve(strict=True),
                                  args.docker_host, args.image), sort_keys=True))
