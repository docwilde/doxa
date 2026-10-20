#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Credential-free build/compiled contract gates in the read-only-token job."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    args = parser.parse_args()
    root, cache = args.root.absolute(), args.cache.absolute()
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
    server = installer.build(cache, None)
    helper, identity = installer.build_code_mode_host(cache, None)
    artifacts = installer.verified_artifacts(cache, server, helper, identity)
    source = installer.prepare_source(cache)
    cargo, toolchain = installer.toolchain(cache, None)
    native_env = installer.build_environment(dict(toolchain, TMPDIR=str(scratch), CARGO_BUILD_JOBS="1",
        RUST_TEST_THREADS="1", CARGO_TARGET_DIR=str(cache / "target")))
    # Cargo accepting an absent test filter is not proof. Capture the listing
    # and require every reviewed guard test family to exist before executing it.
    filters = ["infrastructure_failures_never_authorize_compaction",
               "doxa_required_hook_rejects_infrastructure_and_invalid_output",
               "doxa_auto_", "doxa_async_compaction_cannot_start_unreviewed_work"]
    for name in filters:
        command = [cargo, "test", "--locked", "--profile", "dev-small", "-p", "codex-core", "--lib", name]
        listing = subprocess.check_output(command + ["--", "--list"], cwd=source / "codex-rs", env=native_env, text=True)
        tests = [line.split(": test")[0] for line in listing.splitlines() if line.endswith(": test")]
        if not tests or not all(name in test for test in tests):
            raise ValueError("compiled protected guard test inventory missing: " + name)
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
    print(json.dumps({"compiled_gates": "passed", "paid_requests": 0,
                      "source_commit": installer.SOURCE, "patch_sha256": installer.PATCH_SHA256,
                      "artifacts": artifacts}, sort_keys=True))


if __name__ == "__main__":
    main()
