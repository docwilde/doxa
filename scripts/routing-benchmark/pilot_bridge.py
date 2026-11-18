#!/usr/bin/env python3
"""Export pilot repair inputs and deliver a hash-reviewed source revision offline."""

import argparse
from contextlib import contextmanager
import difflib
import json
import os
from pathlib import Path
import re
import tempfile

import benchmark as b

PROPOSAL_SCHEMA = "doxa-pilot-repair-proposal-v1"
MAX_PROPOSAL = 4 * b.MAX_SOURCE


@contextmanager
def staged_candidate(state):
    b.private_directory(state)
    with tempfile.TemporaryDirectory(prefix=".repair-", dir=state) as temporary:
        revision = Path(temporary) / "revision"
        revision.mkdir(mode=0o700)
        plan, inventory = b.snapshot(state, revision / "agent")
        pilot_raw = b.repo_read("pilot.json")
        pilot = b.parse(pilot_raw)
        b.require(
            plan["schema"] == "doxa-repair-task-plan-v1"
            and plan["task_id"] in pilot["tasks"]
            and plan["split"] == "development"
            and plan["trusted_reference_self_check"] is False,
            "a non-reference development pilot workspace is required",
        )
        task = b.task(plan["task_id"])
        b.require(
            plan["immutable_sha256"] == {
                "README.md": b.digest(b.readme(task)),
                "public-cases.json": b.digest(b.encode(task["public_cases"]) + b"\n"),
            }
            and plan["initial_source_sha256"] == {
                name: b.digest(source.encode()) for name, source in task["starter"].items()
            },
            "workspace plan does not match the pilot starter",
        )
        yield revision, plan, inventory, b.digest(pilot_raw)


def candidate_context(state):
    with staged_candidate(state) as (revision, plan, inventory, _):
        return {
            "instructions": (
                "Follow README.md and return one JSON object using the response format. "
                "Include complete UTF-8 replacement text for each changed editable file."
            ),
            "response_format": {
                "schema": PROPOSAL_SCHEMA,
                "files": {"<editable file>": "<complete replacement text>"},
            },
            "editable_paths": plan["editable_paths"],
            "files": {
                name: b.read(revision / "agent" / name).decode("utf-8")
                for name in sorted(inventory)
            },
        }


def stage_proposal(revision, plan, inventory, pilot_sha256, proposal_path):
    raw = b.read(proposal_path, MAX_PROPOSAL)
    proposal = b.parse(raw.decode("utf-8"))
    b.require(
        isinstance(proposal, dict)
        and proposal.keys() == {"schema", "files"}
        and proposal["schema"] == PROPOSAL_SCHEMA
        and isinstance(proposal["files"], dict),
        "unexpected repair proposal format",
    )
    replacements = proposal["files"]
    b.require(
        replacements
        and replacements.keys() <= set(plan["editable_paths"])
        and all(isinstance(value, str) for value in replacements.values()),
        "proposal must contain only complete replacements for editable files",
    )

    after = dict(inventory)
    changes = {}
    for name, text in sorted(replacements.items()):
        updated = text.encode("utf-8")
        b.require(len(updated) <= b.MAX_FILE and b"\0" not in updated, "invalid replacement source")
        path = revision / "agent" / name
        previous = b.read(path)
        if updated == previous:
            continue
        changes[name] = list(difflib.unified_diff(
            previous.decode("utf-8").splitlines(keepends=True),
            text.splitlines(keepends=True),
            fromfile="a/" + name,
            tofile="b/" + name,
        ))
        path.unlink()
        after[name] = b.create(path, updated)

    b.require(changes, "proposal does not change candidate source")
    total = sum((revision / "agent" / name).stat().st_size for name in after)
    b.require(total <= b.MAX_SOURCE, "candidate source bound")
    manifest = {
        "schema": "doxa-pilot-repair-review-v1",
        "task_id": plan["task_id"],
        "pilot_sha256": pilot_sha256,
        "plan_sha256": b.digest(b.encode(plan)),
        "proposal_sha256": b.digest(raw),
        "before_sha256": b.digest(b.encode(inventory)),
        "after_sha256": b.digest(b.encode(after)),
        "changed_files": sorted(changes),
    }
    manifest["approval_sha256"] = b.digest(b.encode(manifest))
    return manifest, changes


def review(state, proposal):
    with staged_candidate(state) as (revision, plan, inventory, pilot_sha256):
        manifest, changes = stage_proposal(revision, plan, inventory, pilot_sha256, proposal)
        return {**manifest, "diff": changes}


def apply(state, proposal, approval):
    b.require(re.fullmatch(r"[0-9a-f]{64}", approval) is not None, "a review digest is required")
    with staged_candidate(state) as (revision, plan, inventory, pilot_sha256):
        manifest, _ = stage_proposal(revision, plan, inventory, pilot_sha256, proposal)
        b.require(manifest["approval_sha256"] == approval, "proposal or workspace changed since review")
        destination = state / ("repair-" + approval)
        b.require(not os.path.lexists(destination), "approved revision already exists")
        b.create(revision / "plan.json", plan)
        b.create(revision / "repair-review.json", manifest)
        # Publish the complete revision at once. A concurrent successful writer
        # leaves a nonempty directory that rename cannot replace.
        revision.rename(destination)
        return {**manifest, "state": str(destination), "agent_project": str(destination / "agent")}


def grade(state, host, image):
    b.private_directory(state)
    manifest = b.parse(b.read(state / "repair-review.json"))
    approval = manifest.pop("approval_sha256")
    b.require(
        manifest["schema"] == "doxa-pilot-repair-review-v1"
        and manifest["pilot_sha256"] == b.digest(b.repo_read("pilot.json"))
        and approval == b.digest(b.encode(manifest))
        and state.name == "repair-" + approval,
        "approved revision receipt does not match this pilot and workspace",
    )
    report = b.grade(
        state, host, image,
        expected_plan_sha256=manifest["plan_sha256"],
        expected_source_sha256=manifest["after_sha256"],
    )
    return {**report, "approval_sha256": approval}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("context").add_argument("state", type=Path)
    for command in ("review", "apply"):
        selected = commands.add_parser(command)
        selected.add_argument("state", type=Path)
        selected.add_argument("proposal", type=Path)
        if command == "apply":
            selected.add_argument("--approval", required=True)
    selected = commands.add_parser("grade")
    selected.add_argument("state", type=Path)
    selected.add_argument("--docker-host", required=True)
    selected.add_argument("--image", required=True)
    args = parser.parse_args()
    if args.command == "context":
        result = candidate_context(args.state)
    elif args.command == "review":
        result = review(args.state, args.proposal)
    elif args.command == "apply":
        result = apply(args.state, args.proposal, args.approval)
    else:
        result = grade(args.state, args.docker_host, args.image)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, RecursionError, ValueError) as error:
        raise SystemExit("pilot repair bridge refused: " + str(error))
