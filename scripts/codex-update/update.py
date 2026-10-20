#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Immutable protected Codex updates; candidate work never needs a write token.

Only the existing reviewed protection patch is carried forward. A conflict,
changed test inventory, missing CI approval or stale base stops publication.
No model, account, personal token, floating payload or automatic semantic port.
"""
from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
UPSTREAM = "https://api.github.com/repos/openai/codex"
TAG = re.compile(r"rust-v(\d+)\.(\d+)\.(\d+)\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
PIN = "scripts/codex-protected/pin.json"
BRANCH = "automation/codex-protected-"
MAX_FILE = 4 * 1024 * 1024

# Only these current executable pins and active contract fixtures may advance.
# Comments, historical examples, unsupported-version probes and migration paths
# are not a substitute inventory of currently trusted provider identities.
PYTHON_PINS = {
    "scripts/install_codex_protected.py": {"SOURCE", "SOURCE_TAG", "PROVIDER", "PATCH_SHA256", "AGENT_PREFIX"},
    "scripts/build-session-image.py": {"SOURCE", "PATCH"},
    "scripts/codex-protected/verify_code_mode.py": {"AGENT_PREFIX"},
}
OTHER_PINS = {
    "scripts/codex-protected/verify_automatic.py": ('.startswith("doxa_codex_rs/',),
    "rust/doxa-engines/src/codex_compact.rs": ('pub const SUPPORTED_VERSION:', 'pub const PROTECTED_AGENT_PREFIX:'),
    "rust/doxa-engines/src/bin/doxa-codex-protected.rs": ('const SOURCE:', 'const PATCH:'),
    "rust/doxa-engines/tests/codex_launcher.rs": ('const PATCH:', '"source_commit":', '"code_mode_host_source_commit":'),
    "rust/doxa-engines/src/compact_hook.rs": ('"version":',),
    "rust/doxa-engines/src/codex_appserver.rs": ('let desktop =', 'assert_eq!(provider_version("codex_cli_rs/'),
    "rust/doxa-engines/tests/fixtures/codex_protected_fixture.rs": ('agent=', 'CompactGate::prepare('),
}


def rewrite_pins(name, raw, old, new):
    changed = []
    for line in raw.decode().splitlines(keepends=True):
        stripped = line.lstrip()
        allowed = False
        if not stripped.startswith(("#", "//", "/*", "*")):
            assignment = re.match(r"([A-Z_]+)\s*=", stripped)
            allowed = bool(assignment and assignment[1] in PYTHON_PINS.get(name, set()))
            allowed |= any(marker in line for marker in OTHER_PINS.get(name, ()))
        if allowed:
            for key in ("source_commit", "patch_sha256", "provider", "tag", "version"):
                if key not in old or key not in new:
                    continue
                before, after = old[key], new[key]
                # A version suffix/prefix such as 0.163.01 or x0.163.0 is not
                # the reviewed version literal. Never rename it by prefix.
                line = re.sub(r"(?<![A-Za-z0-9_.])" + re.escape(before) + r"(?![A-Za-z0-9_.])",
                              lambda _: after, line)
        changed.append(line)
    return "".join(changed).encode()


def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


def load(raw):
    return json.loads(raw, object_pairs_hook=unique)


def encode(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
            raise ValueError("update input must be regular and bounded")
        raw = stream.read(MAX_FILE + 1)
    if len(raw) > MAX_FILE:
        raise ValueError("update input exceeded bound")
    return raw


def run(*args, cwd=ROOT, **kw):
    return subprocess.check_output(args, cwd=cwd, text=True, **kw).strip()


def api(url, data=None, method=None, *, token=None):
    if not url.startswith("https://api.github.com/"):
        raise ValueError("only GitHub API URLs are accepted")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "doxa-protected-codex-update",
               "X-GitHub-Api-Version": "2026-03-10"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=None if data is None else encode(data), headers=headers,
                                 method=method)
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("GitHub API response exceeded bound")
    return load(raw) if raw else None


def baseline(root):
    """Read literals, never import candidate-controlled installer Python."""
    module = ast.parse(read(root / "scripts/install_codex_protected.py"))
    values = {}
    for node in module.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values[target.id] = node.value.value
    text = read(root / "rust/doxa-engines/src/codex_compact.rs").decode()
    match = re.search(r'pub const SUPPORTED_VERSION: &str = "(\d+\.\d+\.\d+)";', text)
    if not match or not SHA.fullmatch(values.get("SOURCE", "")):
        raise ValueError("reviewed Codex source/version literals changed shape")
    version = match[1]
    patch = read(root / "scripts/codex-protected/precompact.patch")
    expected = {"CONTRACT": "doxa-precompact-fail-closed-v1",
                "PROVIDER": f"codex-{version}-precompact-v1", "PATCH_SHA256": digest(patch)}
    if any(values.get(key) != value for key, value in expected.items()):
        raise ValueError("reviewed installer/patch/engine identity differs")
    if "SOURCE_TAG" in values and values["SOURCE_TAG"] != "rust-v" + version:
        raise ValueError("reviewed official source tag differs from engine version")
    identity = {"schema": 1, "version": version, "tag": "rust-v" + version,
                "source_commit": values["SOURCE"], "patch_sha256": digest(patch),
                "contract": expected["CONTRACT"], "provider": expected["PROVIDER"]}
    if (root / PIN).exists() and load(read(root / PIN)) != identity:
        raise ValueError("structured pin manifest differs from reviewed source literals")
    return identity


def choose(releases, current):
    found = []
    for item in releases:
        match = TAG.fullmatch(item.get("tag_name", ""))
        if match and not item.get("draft", True) and not item.get("prerelease", True):
            version = tuple(map(int, match.groups()))
            if version > tuple(map(int, current.split("."))):
                found.append((version, item))
    # Never silently skip a stable release when several appeared between polls.
    return min(found, key=lambda item: item[0])[1] if found else None


def official_release(tag):
    if not TAG.fullmatch(tag):
        raise ValueError("only stable rust-vX.Y.Z releases are eligible")
    release = api(UPSTREAM + "/releases/tags/" + tag)
    if release.get("tag_name") != tag or release.get("draft") or release.get("prerelease"):
        raise ValueError("upstream release is not a published stable Codex release")
    ref = api(UPSTREAM + "/git/ref/tags/" + tag)["object"]
    for _ in range(3):
        if ref.get("type") == "commit" and SHA.fullmatch(ref.get("sha", "")):
            return release, ref["sha"]
        if ref.get("type") != "tag" or not SHA.fullmatch(ref.get("sha", "")):
            break
        ref = api(UPSTREAM + "/git/tags/" + ref["sha"])["object"]
    raise ValueError("upstream release does not resolve to one immutable commit")


def patch_version(patch, old, new):
    """Change added release metadata only; preserve every protection-code byte."""
    output, changed = [], 0
    for line in patch.decode().splitlines(keepends=True):
        if line.startswith("+") and not line.startswith("+++") and old in line:
            # Only known release-stamping forms may change unattended.
            formatted_agent = (r'\+\s*"doxa_codex_rs/' + re.escape(old)
                               + r' \(doxa-precompact-fail-closed-v1; doxa-midturn-auto-v1; \{\}\)",?\n?')
            if not (re.fullmatch(r'\+version = "' + re.escape(old) + r'"\n?', line)
                    or re.fullmatch(formatted_agent, line)
                    or ('doxa_codex_rs/' + old in line and 'get_codex_user_agent()' in line)):
                raise ValueError("patch has an unreviewed version-dependent protection line")
            line = line.replace(old, new)
            changed += 1
        output.append(line)
    if changed < 2:
        raise ValueError("reviewed version-stamping inventory changed")
    return "".join(output).encode()


def next_version(value):
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-beta\.(\d+))?", value)
    if not match:
        raise ValueError("DOXA release series requires explicit maintenance")
    major, minor, patch, beta = match.groups()
    return f"{major}.{minor}.{patch}-beta.{int(beta)+1}" if beta else f"{major}.{minor}.{int(patch)+1}"


def sensitive_changes(status_lines):
    sensitive = []
    prefixes = ("codex-rs/core/src/session/", "codex-rs/core/src/tools/", "codex-rs/core/src/unified_exec/",
                "codex-rs/hooks/", "codex-rs/app-server/src/request_processors/turn",
                "codex-rs/app-server-protocol/src/protocol/v2/turn")
    manifests = {"codex-rs/core/Cargo.toml", "codex-rs/hooks/Cargo.toml", "codex-rs/app-server/Cargo.toml",
                 "codex-rs/core/src/lib.rs", "codex-rs/protocol/src/protocol.rs"}
    for row in status_lines:
        fields = row.split("\t")
        for name in fields[1:]:
            if (name in manifests or name.startswith(prefixes)
                    or (name.startswith("codex-rs/") and name.endswith(".rs")
                        and (fields[0].startswith(("A", "R"))
                             or re.search(r"compact|permission|sandbox|approv|exec|turn|hook|session", name, re.I)))):
                sensitive.append(name)
    return sorted(set(sensitive))


def replacement_files(root, old, new, patch, release_date=None):
    """Reconstruct the bounded publication from the trusted base, not an artifact."""
    result = {}
    for name in sorted(set(PYTHON_PINS) | set(OTHER_PINS)):
        if not (root / name).is_file():
            continue
        raw = read(root / name)
        revised = rewrite_pins(name, raw, old, new)
        if revised != raw:
            result[name] = revised
    installer_path = "scripts/install_codex_protected.py"
    installer = result.get(installer_path, read(root / installer_path)).decode()
    # Cross-version receipt trust rotates one explicitly reviewed generation.
    # Never replace historical migration IDs by a blanket version substitution.
    previous = {"PREVIOUS_SOURCE": repr(old["source_commit"]),
                "PREVIOUS_PROVIDER": repr(old["provider"]),
                "PREVIOUS_PATCHES": repr({old["patch_sha256"]})}
    for name, value in previous.items():
        installer, count = re.subn(r"(?m)^" + name + r" = [^\n]+$", name + " = " + value, installer)
        if count != 1:
            raise ValueError("reviewed cross-version receipt rotation is unavailable: " + name)
    result[installer_path] = installer.encode()
    result["scripts/codex-protected/precompact.patch"] = patch
    result[PIN] = encode(new)
    manifests = []
    versions = set()
    for path in sorted((root / "rust").glob("*/Cargo.toml")):
        data = tomllib.loads(read(path).decode())
        if data["package"]["name"].startswith("doxa-"):
            versions.add(data["package"]["version"])
            manifests.append((path, data["package"]["version"]))
    if len(versions) != 1 or not manifests:
        raise ValueError("DOXA package versions disagree")
    previous = versions.pop()
    version = next_version(previous)
    for path, _ in manifests:
        raw = read(path)
        revised, count = re.subn(rb'(?m)^version = "' + re.escape(previous.encode()) + rb'"$',
                                 b'version = "' + version.encode() + b'"', raw, count=1)
        if count != 1:
            raise ValueError("DOXA package version field changed shape")
        result[str(path.relative_to(root))] = revised
    lock = read(root / "Cargo.lock").decode()
    count = 0
    def package(match):
        nonlocal count
        block = match[0]
        name = re.search(r'^name = "([^"]+)"$', block, re.M)
        if name and name[1].startswith("doxa-"):
            if f'version = "{previous}"' not in block:
                raise ValueError("DOXA lock version differs")
            count += 1
            return block.replace(f'version = "{previous}"', f'version = "{version}"', 1)
        return block
    lock = re.sub(r'(?ms)^\[\[package\]\]\n.*?(?=^\[\[package\]\]|\Z)', package, lock)
    if count != len(manifests):
        raise ValueError("DOXA lock package inventory differs")
    result["Cargo.lock"] = lock.encode()
    release_date = release_date or time.strftime('%Y-%m-%d', time.gmtime())
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", release_date):
        raise ValueError("invalid frozen release date")
    entry = (f"## {version} — {release_date}\n\n"
             "**Protected Codex**\n\n"
             f"- Update **`codex-current`** to protected Codex **{new['version']}**, pinned to `{new['source_commit']}`.\n"
             "- Retain compiled compaction refusal, permission checks and complete helper provenance; incompatible upstream releases stop the updater.\n\n")
    changelog = read(root / "CHANGELOG.md").decode()
    at = changelog.find("\n## ")
    if at < 0:
        raise ValueError("CHANGELOG release structure changed")
    result["CHANGELOG.md"] = (changelog[:at+1] + entry + changelog[at+1:]).encode()
    return result, version, entry


def prepare(root, directory, tag=None, release_date=None):
    old = baseline(root)
    if tag is None:
        releases = []
        # Pagination is bounded; refuse an incomplete history instead of skipping.
        for page in range(1, 11):
            rows = api(UPSTREAM + f"/releases?per_page=100&page={page}")
            releases.extend(rows)
            if len(rows) < 100:
                break
        else:
            raise ValueError("release history exceeds discovery bound")
        item = choose(releases, old["version"])
        if item is None:
            return {"action": "none"}
        tag = item["tag_name"]
    match = TAG.fullmatch(tag)
    if not match or tuple(map(int, match.groups())) <= tuple(map(int, old["version"].split("."))):
        raise ValueError("release must advance the reviewed pin")
    release, commit = official_release(tag)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    source = directory / "source"
    run("git", "clone", "--depth", "1", "--branch", tag, "https://github.com/openai/codex.git", str(source))
    if run("git", "rev-parse", "HEAD", cwd=source) != commit:
        raise ValueError("fetched upstream release moved")
    run("git", "fetch", "--depth", "1", "origin", "tag", old["tag"], cwd=source)
    if run("git", "rev-parse", old["tag"] + "^{commit}", cwd=source) != old["source_commit"]:
        raise ValueError("the reviewed upstream baseline tag moved")
    status = run("git", "diff", "--name-status", "--no-renames", old["source_commit"], commit, cwd=source).splitlines()
    upstream_diff = subprocess.check_output(["git", "diff", "--binary", old["source_commit"], commit], cwd=source)
    sensitive = sensitive_changes(status)
    review = {"source_before": old["source_commit"], "source_after": commit,
              "diff_sha256": digest(upstream_diff), "changed_paths": [row.split("\t")[-1] for row in status],
              "sensitive_paths": sensitive}
    (directory / "upstream-review.json").write_bytes(encode(review))
    if sensitive:
        raise ValueError("upstream safety entrypoints changed; semantic review required; diff_sha256="
                         + review["diff_sha256"] + "; paths=" + ", ".join(sensitive[:12]))
    version = tag.removeprefix("rust-v")
    patch = patch_version(read(root / "scripts/codex-protected/precompact.patch"), old["version"], version)
    patch_path = directory / "carried.patch"
    patch_path.write_bytes(patch)
    run("git", "apply", "--check", str(patch_path), cwd=source)
    run("git", "apply", str(patch_path), cwd=source)
    run("git", "add", "-N", ".", cwd=source)
    patch = subprocess.check_output(["git", "diff", "HEAD", "--binary"], cwd=source)
    new = dict(old, version=version, tag=tag, source_commit=commit, patch_sha256=digest(patch),
               provider=f"codex-{version}-precompact-v1")
    release_date = release_date or time.strftime('%Y-%m-%d', time.gmtime())
    files, doxa_version, entry = replacement_files(root, old, new, patch, release_date)
    metadata = {"schema": 1, "action": "propose", "base_sha": run("git", "rev-parse", "HEAD", cwd=root),
                "old_pin": old, "pin": new, "doxa_version": doxa_version, "notes": entry,
                "release_date": release_date, "release_id": release["id"],
                "verification_run_id": os.environ.get("CODEX_UPDATE_ORIGINAL_RUN", os.environ.get("GITHUB_RUN_ID", "local")),
                "upstream_review": review,
                "files": {name: digest(raw) for name, raw in files.items()}}
    for name, raw in files.items():
        target = directory / "files" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    (directory / "candidate.json").write_bytes(encode(metadata))
    return metadata


def materialize(root, directory):
    metadata = load(read(directory / "candidate.json"))
    if metadata["base_sha"] != run("git", "rev-parse", "HEAD", cwd=root):
        raise ValueError("candidate base changed")
    if metadata["old_pin"] != baseline(root):
        raise ValueError("candidate reviewed baseline differs")
    patch = read(directory / "files/scripts/codex-protected/precompact.patch")
    files, version, notes = replacement_files(root, metadata["old_pin"], metadata["pin"], patch, metadata["release_date"])
    if metadata["pin"]["patch_sha256"] != digest(patch) or version != metadata["doxa_version"] or notes != metadata["notes"]:
        raise ValueError("candidate release identity differs")
    if {name: digest(raw) for name, raw in files.items()} != metadata["files"]:
        raise ValueError("candidate file inventory differs")
    for name, raw in files.items():
        if read(directory / "files" / name) != raw:
            raise ValueError("candidate bytes differ from deterministic reconstruction")
    for name, raw in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    return metadata


def verify_gate_receipt(directory, metadata, expected_candidate, expected_proof):
    for value in (expected_candidate, expected_proof):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("compiled gate job-output digest is missing")
    candidate = read(directory / "candidate.json")
    raw = read(directory / "compiled-gates.json")
    proof = load(raw)
    pin = metadata["pin"]
    expected = {"schema": 1, "compiled_gates": "passed", "paid_requests": 0,
                "candidate_sha256": digest(candidate), "pin_sha256": digest(encode(pin)),
                "source_commit": pin["source_commit"], "patch_sha256": pin["patch_sha256"],
                "base_sha": metadata["base_sha"], "verification_run_id": metadata["verification_run_id"],
                "memory_max_bytes": 8*1024**3, "memory_swap_max_bytes": 0}
    if (digest(candidate) != expected_candidate or digest(raw) != expected_proof
            or any(proof.get(key) != value for key, value in expected.items())
            or proof.get("artifacts", {}).get("source_commit") != pin["source_commit"]
            or proof.get("artifacts", {}).get("patch_sha256") != pin["patch_sha256"]):
        raise ValueError("compiled gate receipt does not bind this exact candidate/pin/job")
    return raw


def validate_artifact(root, directory, reconstruction, expected_candidate=None, expected_proof=None):
    """The write-token job recreates everything without running upstream code."""
    supplied = load(read(directory / "candidate.json"))
    receipt = verify_gate_receipt(directory, supplied, expected_candidate, expected_proof)
    expected = prepare(root, reconstruction, supplied["pin"]["tag"], supplied["release_date"])
    if supplied != expected:
        raise ValueError("artifact differs from independent official-source reconstruction")
    for name in expected["files"]:
        if read(directory / "files" / name) != read(reconstruction / "files" / name):
            raise ValueError("artifact code differs from independent reconstruction")
    result = materialize(root, reconstruction)
    (reconstruction / "compiled-gates.json").write_bytes(receipt)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "materialize", "validate-artifact", "identity"])
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--tag")
    parser.add_argument("--reconstruction", type=Path)
    parser.add_argument("--expected-candidate-sha", default=os.environ.get("EXPECTED_CANDIDATE_SHA"))
    parser.add_argument("--expected-proof-sha", default=os.environ.get("EXPECTED_PROOF_SHA"))
    args = parser.parse_args()
    if args.command == "identity":
        value = baseline(args.root)
    elif args.command == "prepare":
        value = prepare(args.root, args.directory.absolute(), args.tag)
    elif args.command == "validate-artifact":
        value = validate_artifact(args.root, args.directory.absolute(), args.reconstruction.absolute(),
                                  args.expected_candidate_sha, args.expected_proof_sha)
    else:
        value = materialize(args.root, args.directory.absolute())
    print(json.dumps(value, sort_keys=True))
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a") as stream:
            stream.write("action=" + value.get("action", "none") + "\n")
            stream.write("tag=" + value.get("pin", {}).get("tag", "") + "\n")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError, KeyError) as error:
        raise SystemExit("protected Codex update blocked: " + str(error))
