#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Publish a reconstructed candidate, await real PR CI, merge and release.

Runs only trusted base-branch code on a fresh runner. No candidate/upstream
program is executed here. No admin bypass, forged check, PAT or auto-merge
repository setting is needed. All remote writes use the ephemeral job token.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.error

import update


class GitHub:
    def __init__(self, repository, token):
        if repository != "docwilde/doxa" or not token:
            raise ValueError("publisher requires the DOXA job token")
        self.base = "https://api.github.com/repos/" + repository
        self.token = token

    def call(self, path, data=None, method=None):
        return update.api(self.base + path, data, method, token=self.token)


def whitespace_check(root):
    # Unified patches need literal space-only context lines. Canonical apply
    # verification covers that file; normal whitespace rules cover everything else.
    return update.run("git", "diff", "--check", "--", ".", ":(exclude)scripts/codex-protected/precompact.patch", cwd=root)


def not_found(github, path):
    try:
        return github.call(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def require_own_pr(pr, head, branch):
    if (pr.get("user", {}).get("login") != "github-actions[bot]"
            or pr.get("base", {}).get("ref") != "main"
            or pr.get("head", {}).get("ref") != branch
            or pr.get("head", {}).get("repo", {}).get("full_name") != "docwilde/doxa"
            or pr.get("head", {}).get("sha") != head):
        raise ValueError("automation PR ownership/head/base changed")


def real_pr_ci(github, head, branch, expected_workflow, required_jobs):
    """Dispatch CI is extra evidence, never a substitute for required PR CI."""
    listing = github.call("/actions/runs?head_sha=" + head + "&per_page=100")["workflow_runs"]
    if len(listing) >= 100:
        raise ValueError("CI run inventory exceeded bound")
    runs = [row for row in listing if row.get("head_sha") == head and row.get("head_branch") == branch
            and row.get("event") == "pull_request" and row.get("path", "").split("@")[0] == expected_workflow]
    if not runs:
        return False
    newest = max(runs, key=lambda row: row["id"])
    if newest.get("status") in ("action_required", "waiting") or newest.get("conclusion") == "action_required":
        # This may be refused for bot-origin runs on some GitHub deployments.
        # Refusal is a visible block; never replace it with a fake status.
        github.call(f"/actions/runs/{newest['id']}/approve", {}, "POST")
        return False
    if newest.get("status") != "completed":
        return False
    if newest.get("conclusion") != "success":
        raise ValueError("real pull-request Rust CI failed")
    jobs = github.call(f"/actions/runs/{newest['id']}/jobs?per_page=100")["jobs"]
    good = {row["name"] for row in jobs if row.get("conclusion") == "success"}
    if not required_jobs <= good:
        raise ValueError("real PR workflow lacks successful Linux/macOS gates")
    return True


def require_compiled_receipt(github, metadata):
    identity = str(metadata.get("verification_run_id", ""))
    if not identity.isdecimal():
        raise ValueError("compiled verifier run identity is missing")
    run = github.call("/actions/runs/" + identity)
    if (run.get("head_sha") != metadata["base_sha"]
            or run.get("path", "").split("@")[0] != ".github/workflows/codex-update.yml"
            or run.get("event") not in ("schedule", "workflow_dispatch")):
        raise ValueError("compiled verifier belongs to another workflow/base")
    jobs = github.call(f"/actions/runs/{identity}/jobs?per_page=100")["jobs"]
    if not any(row.get("name") == "Verify protected candidate" and row.get("conclusion") == "success" for row in jobs):
        raise ValueError("compiled verifier job has not passed")


def require_protection(github):
    # Full protection REST needs Administration:read, which a normal job token
    # cannot request. Public branch metadata exposes the relevant check/app and
    # enforcement. We enforce an exact fresh base ourselves; GitHub's normal
    # merge endpoint still enforces every configured review/rule/check.
    branch = github.call("/branches/main")
    checks = (branch.get("protection") or {}).get("required_status_checks") or {}
    if not branch.get("protected") or checks.get("enforcement_level") != "everyone":
        raise ValueError("main must enforce protected checks for everyone")
    configured = {(item["context"], item.get("app_id")) for item in checks.get("checks", [])}
    if ("rust", 15368) not in configured:
        raise ValueError("required real GitHub Actions rust gate is missing")


def tag_release(github, version, commit, notes):
    """Idempotent recovery preserves the already-merged exact release identity."""
    tag = "v" + version
    title = "DOXA " + version + " — protected Codex update"
    ref = not_found(github, "/git/ref/tags/" + tag)
    if ref:
        obj = ref["object"]
        if obj.get("type") != "tag":
            raise ValueError("existing DOXA release tag is not annotated")
        annotation = github.call("/git/tags/" + obj["sha"])
        if (annotation.get("tag") != tag or annotation.get("object", {}).get("sha") != commit
                or annotation.get("object", {}).get("type") != "commit"
                or annotation.get("message", "").rstrip() != title):
            raise ValueError("existing annotated tag differs from the merged release")
    else:
        annotation = github.call("/git/tags", {"tag": tag, "message": title,
            "object": commit, "type": "commit"}, "POST")
        github.call("/git/refs", {"ref": "refs/tags/" + tag, "sha": annotation["sha"]}, "POST")
    release = not_found(github, "/releases/tags/" + tag)
    expected = {"tag_name": tag, "name": title, "body": notes,
                "draft": False, "prerelease": "-" in version}
    if release:
        if any(release.get(key) != value for key, value in expected.items()):
            raise ValueError("existing GitHub release differs; refusing to overwrite it")
    else:
        github.call("/releases", dict(expected, target_commitish=commit), "POST")


def blocked(github, reason, run_url):
    title = "Protected Codex update blocked"
    rows = github.call("/issues?state=open&creator=github-actions%5Bbot%5D&per_page=100")
    matching = [row for row in rows if row.get("title") == title and "pull_request" not in row]
    body = ("The protected updater stopped without bypassing a protection gate.\n\n"
            + reason[:1500] + "\n\nRun: " + run_url + "\n")
    if matching:
        github.call("/issues/" + str(matching[0]["number"]), {"body": body}, "PATCH")
    else:
        github.call("/issues", {"title": title, "body": body}, "POST")


def publish(root, directory, github):
    metadata = update.load(update.read(directory / "candidate.json"))
    tag = metadata["pin"]["tag"]
    if not update.TAG.fullmatch(tag):
        raise ValueError("invalid candidate tag")
    base = metadata["base_sha"]
    if github.call("/git/ref/heads/main")["object"]["sha"] != base:
        raise ValueError("main advanced; rebuild the update against its new base")
    require_protection(github)
    require_compiled_receipt(github, metadata)
    update.verify_gate_receipt(directory, metadata, os.environ["EXPECTED_CANDIDATE_SHA"], os.environ["EXPECTED_PROOF_SHA"])
    branch = update.BRANCH + metadata["pin"]["version"]
    existing = github.call("/pulls?state=all&head=docwilde:" + branch + "&per_page=100")
    if existing:
        # Do not overwrite a closed proposal, someone else's head, or a release
        # another run already owns. Operator recovery has a separate command.
        raise ValueError("this immutable upstream release already has a proposal; resume it instead")
    changed = update.run("git", "diff", "--name-only", cwd=root).splitlines()
    untracked = update.run("git", "ls-files", "--others", "--exclude-standard", cwd=root).splitlines()
    if set(changed + untracked) != set(metadata["files"]):
        raise ValueError("publication workspace has unrelated changes")
    for name, digest in metadata["files"].items():
        if update.digest(update.read(root / name)) != digest:
            raise ValueError("publication bytes changed after reconstruction")
    # A unified patch must retain its literal space-only context lines. Exact
    # git-apply and canonical-diff checks cover that one file; all other source
    # still receives the ordinary whitespace check.
    whitespace_check(root)
    update.run("git", "checkout", "-b", branch, cwd=root)
    update.run("git", "add", "--", *metadata["files"], cwd=root)
    title = f"release: {metadata['doxa_version']} — protected Codex {metadata['pin']['version']}"
    update.run("git", "-c", "user.name=github-actions[bot]", "-c",
               "user.email=41898282+github-actions[bot]@users.noreply.github.com",
               "commit", "-m", title, cwd=root)
    head = update.run("git", "rev-parse", "HEAD", cwd=root)
    header = "AUTHORIZATION: basic " + base64.b64encode(("x-access-token:" + github.token).encode()).decode()
    env = dict(os.environ, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
               GIT_CONFIG_VALUE_0=header)
    update.run("git", "push", "https://github.com/docwilde/doxa.git", branch, cwd=root, env=env)
    body = ("Protected update from immutable official release " + tag + ".\n\n"
            "The carried protection patch and credential-free compiled compaction/helper/permission gates passed. "
            "Full Linux/macOS PR CI must pass before the protected merge.\n\n"
            + "Candidate SHA256: " + update.digest(update.read(directory / "candidate.json")) + "\n\n"
            + "Compiled proof SHA256: " + os.environ["EXPECTED_PROOF_SHA"] + "\n\n"
            + "Upstream diff SHA256: " + metadata["upstream_review"]["diff_sha256"] + "\n\n"
            + "Upstream changed paths: " + ", ".join(metadata["upstream_review"]["changed_paths"])[:3000] + "\n\n"
            + "Automation run: " + os.environ["GITHUB_SERVER_URL"] + "/" + os.environ["GITHUB_REPOSITORY"]
            + "/actions/runs/" + os.environ["GITHUB_RUN_ID"] + "\n")
    pr = github.call("/pulls", {"title": title, "body": body, "head": branch, "base": "main"}, "POST")
    # Explicit dispatch is useful extra CI, but its check is never used to
    # satisfy branch protection. The real pull_request run below must pass.
    github.call("/actions/workflows/rust-ci.yml/dispatches", {"ref": branch}, "POST")
    github.call("/actions/workflows/android-ci.yml/dispatches", {"ref": branch}, "POST")
    return finish(root, directory, github, pr["number"], head, branch)


def finish(root, directory, github, number, head=None, branch=None):
    metadata = update.load(update.read(directory / "candidate.json"))
    require_compiled_receipt(github, metadata)
    branch = branch or update.BRANCH + metadata["pin"]["version"]
    pr = github.call("/pulls/" + str(number))
    head = head or pr["head"]["sha"]
    require_own_pr(pr, head, branch)
    marker = "Candidate SHA256: " + update.digest(update.read(directory / "candidate.json"))
    if marker not in pr.get("body", ""):
        raise ValueError("PR is not bound to this verified candidate")
    proofs = re.findall(r"(?m)^Compiled proof SHA256: ([0-9a-f]{64})$", pr.get("body", ""))
    if len(proofs) != 1:
        raise ValueError("PR lacks its original compiled proof digest")
    update.verify_gate_receipt(directory, metadata, update.digest(update.read(directory / "candidate.json")), proofs[0])
    files = github.call(f"/pulls/{number}/files?per_page=100")
    if {row["filename"] for row in files} != set(metadata["files"]):
        raise ValueError("PR file inventory changed")
    # Compare every current PR file against exact independently reconstructed
    # candidate bytes. IDs/title/body alone never authorize a replacement head.
    for name, expected in metadata["files"].items():
        row = github.call("/contents/" + name + "?ref=" + head)
        if row.get("encoding") != "base64" or update.digest(base64.b64decode(row["content"])) != expected:
            raise ValueError("PR source bytes differ from verified candidate")
    deadline = time.monotonic() + 2700
    while not pr.get("merged"):
        require_protection(github)
        require_own_pr(pr, head, branch)
        if pr.get("state") != "open":
            raise ValueError("automation PR was closed without merging")
        if github.call("/git/ref/heads/main")["object"]["sha"] != metadata["base_sha"]:
            raise ValueError("main advanced before protected merge; fresh verification is required")
        rust = real_pr_ci(github, head, branch, ".github/workflows/rust-ci.yml", {"rust", "macos"})
        android = real_pr_ci(github, head, branch, ".github/workflows/android-ci.yml", {"android"})
        if rust and android:
            merged = github.call(f"/pulls/{number}/merge", {"sha": head, "merge_method": "squash",
                "commit_title": pr["title"], "commit_message": "Protected Codex update after compiled gates and required PR CI."}, "PUT")
            if not merged.get("merged"):
                raise ValueError("GitHub refused the protected merge")
            pr = github.call("/pulls/" + str(number))
            break
        if time.monotonic() >= deadline:
            raise ValueError("actual required PR CI/approval did not complete before the deadline")
        time.sleep(20)
        pr = github.call("/pulls/" + str(number))
    commit = pr.get("merge_commit_sha")
    if not update.SHA.fullmatch(commit or ""):
        raise ValueError("merged release commit is unavailable")
    require_protection(github)
    for workflow, jobs in (("rust-ci.yml", {"rust", "macos"}), ("android-ci.yml", {"android"})):
        if not real_pr_ci(github, head, branch, ".github/workflows/" + workflow, jobs):
            raise ValueError("merged PR no longer has all successful real CI receipts")
    # A tag belongs to the exact merged version, never a later main HEAD.
    tag_release(github, metadata["doxa_version"], commit, metadata["notes"])
    return {"pr": number, "commit": commit, "tag": "v" + metadata["doxa_version"], "released": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["publish", "resume", "blocked"])
    parser.add_argument("--root", type=Path, default=update.ROOT)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--pr", type=int)
    parser.add_argument("--reason", default="Candidate patch/build/guard verification failed; see the linked run.")
    args = parser.parse_args()
    github = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
    url = os.environ["GITHUB_SERVER_URL"] + "/" + os.environ["GITHUB_REPOSITORY"] + "/actions/runs/" + os.environ["GITHUB_RUN_ID"]
    try:
        if args.command == "blocked":
            blocked(github, args.reason, url)
            return
        value = publish(args.root, args.directory, github) if args.command == "publish" else finish(args.root, args.directory, github, args.pr)
        print(json.dumps(value, sort_keys=True))
    except Exception:
        # Provider/HTTP error bodies and token-bearing subprocess environments
        # are never copied into issue text. Preserve details in runner logs.
        blocked(github, "Publication or required CI was refused. Exact candidate/head retained for recovery.", url)
        raise


if __name__ == "__main__":
    main()
