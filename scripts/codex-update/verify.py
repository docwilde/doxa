#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Credential-free build/compiled contract gates in the read-only-token job."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import hashlib

GUARD_TESTS = (
    ("codex-core", "infrastructure_failures_never_authorize_compaction"),
    ("codex-hooks", "doxa_required_hook_rejects_infrastructure_and_invalid_output"),
    ("codex-core", "doxa_auto_"),
    ("codex-core", "doxa_async_compaction_cannot_start_unreviewed_work"),
)


def guard_command(cargo, package, name):
    return [cargo, "test", "--locked", "--profile", "dev-small", "-p", package, "--lib", name]


def checked_inventory(listing, name):
    tests = [line.split(": test")[0] for line in listing.splitlines() if line.endswith(": test")]
    if not tests or not all(name in test for test in tests):
        raise ValueError("compiled protected guard test inventory missing: " + name)
    return tests


def inherited_scope(arguments):
    """Keep installer children inside the enclosing verified 8GiB scope."""
    if arguments and arguments[0] == "systemd-run":
        prefix = ["systemd-run", "--user", "--scope", "-q", "-p", "MemoryMax=8G", "-p", "MemorySwapMax=0"]
        if arguments[:len(prefix)] != prefix:
            raise ValueError("installer scope command changed; refusing to escape the outer memory boundary")
        return arguments[len(prefix):]
    return arguments


def require_memory_boundary():
    row = Path("/proc/self/cgroup").read_text().strip().splitlines()
    paths = [line[3:] for line in row if line.startswith("0::/")]
    if len(paths) != 1:
        raise ValueError("verifier requires a cgroup-v2 memory boundary")
    group = Path("/sys/fs/cgroup") / paths[0].lstrip("/")
    if ((group / "memory.max").read_text().strip() != str(8 * 1024**3)
            or (group / "memory.swap.max").read_text().strip() != "0"):
        raise ValueError("verifier requires exactly 8GiB MemoryMax and no swap")


def candidate_binding(root, directory):
    import update
    raw = update.read(directory / "candidate.json")
    meta = update.load(raw)
    pin = update.baseline(root)
    if pin != meta["pin"]:
        raise ValueError("compiled candidate pin differs from materialized source")
    for name, expected in meta["files"].items():
        if update.digest(update.read(root / name)) != expected:
            raise ValueError("compiled candidate source bytes differ: " + name)
        if update.digest(update.read(directory / "files" / name)) != expected:
            raise ValueError("compiled candidate artifact bytes differ: " + name)
    return {"candidate_sha256": update.digest(raw), "pin_sha256": update.digest(update.encode(pin)),
            "source_commit": pin["source_commit"], "patch_sha256": pin["patch_sha256"],
            "base_sha": meta["base_sha"], "verification_run_id": meta["verification_run_id"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--candidate-directory", type=Path, required=True)
    parser.add_argument("--job-output", type=Path, required=True)
    args = parser.parse_args()
    root, cache = args.root.absolute(), args.cache.absolute()
    require_memory_boundary()
    binding = candidate_binding(root, args.candidate_directory)
    cache.mkdir(mode=0o700, parents=True, exist_ok=False)
    scratch = cache / "scratch"
    scratch.mkdir(mode=0o700)
    # No account credentials, model requests, or mutable owner state are used.
    environment = {key: value for key, value in os.environ.items()
                   if key in ("PATH", "LANG", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")}
    environment.update(HOME=str(cache), TMPDIR=str(scratch), CARGO_BUILD_JOBS="1", RUST_TEST_THREADS="1")
    os.environ.clear()
    os.environ.update(environment)
    spec = importlib.util.spec_from_file_location("protected_installer", root / "scripts/install_codex_protected.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    original_run = installer.run
    installer.run = lambda arguments, **kwargs: original_run(inherited_scope(arguments), **kwargs)
    server = installer.build(cache, None)
    helper, identity = installer.build_code_mode_host(cache, None)
    artifacts = installer.verified_artifacts(cache, server, helper, identity)
    source = installer.prepare_source(cache)
    cargo, toolchain = installer.toolchain(cache, None)
    native_env = installer.build_environment(dict(toolchain, TMPDIR=str(scratch), CARGO_BUILD_JOBS="1",
        RUST_TEST_THREADS="1", CARGO_TARGET_DIR=str(cache / "target")))
    # Cargo accepting an absent test filter is not proof. Capture the listing
    # and require every reviewed guard test family to exist before executing it.
    for package, name in GUARD_TESTS:
        command = guard_command(cargo, package, name)
        listing = subprocess.check_output(command + ["--", "--list"], cwd=source / "codex-rs", env=native_env, text=True)
        checked_inventory(listing, name)
        subprocess.run(command, cwd=source / "codex-rs", env=native_env, check=True)
    for suffix in ([], ["--default-window"]):
        subprocess.run(["python3", str(root / "scripts/codex-protected/verify_automatic.py"),
                        "--server", str(server), "--scratch", str(scratch)] + suffix, check=True, env=environment)
    # Compile native launcher/permission/provenance tests without account data.
    doxa_env = dict(native_env, CARGO_TARGET_DIR=str(cache / "doxa-target"))
    subprocess.run([cargo, "test", "--locked", "-p", "doxa-engines"], cwd=root, env=doxa_env, check=True)
    subprocess.run([cargo, "build", "--locked", "-p", "doxa-engines", "--bin", "doxa-codex-protected"],
                   cwd=root, env=doxa_env, check=True)
    launcher = cache / "doxa-target/debug/doxa-codex-protected"
    # A fixed local-only delegator is sufficient: protected app-server/helper
    # branches must use the verified payload, never the official fallback CLI.
    official = cache / "no-official-fallback"
    official.write_text("#!/bin/sh\nprintf 'unexpected official CLI delegation\\n' >&2\nexit 97\n")
    official.chmod(0o700)
    providers = cache / "providers"
    providers.mkdir(mode=0o700)
    protected = installer.install(server, providers, official, launcher, helper, identity, artifacts)
    subprocess.run([str(protected), "--version"], check=True, env=environment)
    subprocess.run(["python3", str(root / "scripts/codex-protected/verify_code_mode.py"),
                    "--server", str(server), "--launcher", str(protected),
                    "--negative-server", str(server), "--scratch", str(scratch)], check=True, env=environment)
    if candidate_binding(root, args.candidate_directory) != binding:
        raise ValueError("candidate identity changed during compiled verification")
    if installer.SOURCE != binding["source_commit"] or installer.PATCH_SHA256 != binding["patch_sha256"]:
        raise ValueError("compiled installer identity differs from exact candidate")
    import update
    proof = dict(binding, schema=1, compiled_gates="passed", paid_requests=0,
                 memory_max_bytes=8*1024**3, memory_swap_max_bytes=0, artifacts=artifacts)
    raw = update.encode(proof)
    proof_path = args.candidate_directory / "compiled-gates.json"
    with proof_path.open("xb") as stream:
        stream.write(raw)
    with args.job_output.open("a") as stream:
        stream.write("candidate_sha256=" + binding["candidate_sha256"] + "\n")
        stream.write("proof_sha256=" + update.digest(raw) + "\n")
    print(json.dumps({"candidate_sha256": binding["candidate_sha256"], "proof_sha256": update.digest(raw),
                      "compiled_gates": "passed", "paid_requests": 0}, sort_keys=True))


if __name__ == "__main__":
    main()
