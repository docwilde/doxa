#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Opt-in, bounded Claude subscription check against the native daemon.

Only synthetic prompts enter the account. The user's Claude credential is read
through the normal DOXA isolation path; no account files or replies are logged.
This is a local release verifier, never a CI test.
"""

import collections
import datetime
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from verify_native_vendors_live import Wire, terminate_group


TURN_TIMEOUT = 90


def emit(result):
    print(json.dumps(result, sort_keys=True), flush=True)


def attach(process, registry):
    deadline = time.monotonic() + 30
    while not registry.exists():
        if process.poll() is not None:
            raise RuntimeError("native daemon exited during startup")
        if time.monotonic() >= deadline:
            raise TimeoutError("native daemon startup timed out")
        time.sleep(.02)
    wire = Wire(json.loads(registry.read_text())["daemon_socket"])
    try:
        hello = wire.receive(time.monotonic() + 10)
        wire.send({"type": "attach", "cursor": None})
        return wire, hello
    except BaseException:
        wire.sock.close()
        raise


def turn(wire, prompt, expected):
    wire.counter += 1
    turn_id = wire.counter
    wire.send({"type": "prompt", "id": turn_id, "text": prompt})
    deadline = time.monotonic() + TURN_TIMEOUT
    counts = collections.Counter()
    rendered = ""
    while True:
        frame = wire.receive(deadline)
        event = frame.get("event", {})
        kind = event.get("type")
        if kind:
            counts[kind] += 1
        if kind == "text_delta":
            rendered += event.get("data", {}).get("text", "")
        if kind == "needs_input":
            raise RuntimeError("unexpected native approval request")
        if kind == "turn_done":
            done = event.get("data", {})
            usage_reported = all(isinstance(done.get(name), int)
                and not isinstance(done.get(name), bool)
                for name in ("input_tokens", "output_tokens"))
            return {
                "ok": not done.get("is_error") and rendered.strip() == expected
                    and usage_reported,
                "text_deltas": counts["text_delta"],
                "reasoning_events": counts["reasoning_delta"],
                "usage_reported": usage_reported,
            }
        if frame.get("id") == turn_id and frame.get("ok") is False:
            raise RuntimeError("native prompt rejected")


def run():
    daemon = os.environ.get("DOXA_NATIVE_DAEMON", "")
    claude = shutil.which("claude")
    if not Path(daemon).is_file() or not claude:
        return {"result": "missing_native_daemon_or_claude_cli", "submitted_turns": 0}
    parent = Path(os.environ.get("TMPDIR", str(Path.home() / ".cache/doxa-live")))
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="c-", dir=parent) as directory:
        root = Path(directory)
        root.chmod(0o700)
        workspace = root / "workspace"
        workspace.mkdir(mode=0o700)
        lore = root / "lore"
        lore.mkdir(mode=0o700)
        token = secrets.token_hex(4)
        session = str(uuid.uuid4())
        environment = {
            "HOME": str(Path.home()),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TMPDIR": str(parent),
            "DOXA_HOME": str(root / "doxa"),
            "DOXA_LORE": "0",
            "DOXA_AGENT_PEER_SEND": "0",
            "LORE_ROOT": str(lore),
            "LORE_PROJECTS_DIR": str(root / "projects"),
        }
        if os.environ.get("CLAUDE_CONFIG_DIR"):
            environment["CLAUDE_CONFIG_DIR"] = os.environ["CLAUDE_CONFIG_DIR"]
        command = [daemon, "--runtime-dir", str(root / "runtime"),
                   "--cwd", str(workspace), "--session-id", session,
                   "--engine", "claude", "--claude-bin", claude,
                   "--effort", "low", "--linger", "10"]
        result = {"started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  "submitted_turns": 0, "provider": "claude"}
        process = subprocess.Popen(command, env=environment, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        wire = None
        try:
            registry = root / "runtime/registry" / f"{session}.json"
            wire, hello = attach(process, registry)
            result["initial_model_reported"] = bool(hello.get("model"))
            result["catalog_available"] = bool(wire.call("list_models").get("models"))
            if not result["initial_model_reported"] or not result["catalog_available"]:
                result["result"] = "model_catalog_unavailable_no_paid_turns"
                return result
            result["submitted_turns"] += 1
            result["first"] = turn(wire,
                f"Reply with only this synthetic token: {token}. No tools.", token)
            if not result["first"]["ok"]:
                result["result"] = "first_turn_failed"
                return result
            wire.call("stop", timeout=5)
            wire.sock.close()
            wire = None
            terminate_group(process)
            registry.unlink(missing_ok=True)
            process = subprocess.Popen([*command, "--resume", "true"], env=environment,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       start_new_session=True)
            wire, resumed = attach(process, registry)
            result["resumed_model_reported"] = bool(resumed.get("model"))
            if not result["resumed_model_reported"]:
                result["result"] = "resumed_model_unknown_no_second_turn"
                return result
            result["submitted_turns"] += 1
            result["second"] = turn(wire,
                "Repeat only the synthetic token from your previous answer. No tools.", token)
            result["result"] = "passed" if result["second"]["ok"] else "resume_turn_failed"
        except (TimeoutError, RuntimeError, OSError, ValueError) as error:
            # Native stderr and exception bodies may carry account/private data.
            result["result"] = type(error).__name__
        finally:
            if wire:
                try:
                    wire.call("stop", timeout=2)
                except (TimeoutError, RuntimeError, OSError, ValueError):
                    pass
                wire.sock.close()
            terminate_group(process)
        return result


if __name__ == "__main__":
    if sys.argv[1:] != ["--live"]:
        emit({"result": "explicit_live_opt_in_required", "submitted_turns": 0})
        sys.exit(2)
    outcome = run()
    emit(outcome)
    sys.exit(int(outcome["result"] != "passed"))
