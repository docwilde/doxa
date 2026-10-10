#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Opt-in live verification of an active Claude manual-to-auto transition.

Two short synthetic subscription turns at most. The first must reach a real
Bash approval, switch mode without answering it, resolve that exact card and
execute a private marker script. The second checks a new Bash call in auto.
Receipts contain allowlisted metadata only; account files and provider text
are neither printed nor retained by this script.
"""

import argparse
import collections
import datetime
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

from verify_native_claude_live import stop_and_wait
from verify_native_vendors_live import Wire, terminate_group


TURN_TIMEOUT = 90


class EventWire(Wire):
    """Keep events received before an RPC reply for the active turn reader."""

    def __init__(self, path):
        super().__init__(path)
        self.events = collections.deque()

    def call(self, method, params=None, timeout=15):
        self.counter += 1
        request_id = self.counter
        self.send({"type": "call", "id": request_id, "method": method,
                   "params": params or {}})
        deadline = time.monotonic() + timeout
        while True:
            frame = super().receive(deadline)
            if frame.get("id") == request_id:
                return frame
            if "event" in frame:
                if len(self.events) >= 256:
                    raise RuntimeError("event_overflow")
                self.events.append(frame)

    def receive_event(self, deadline):
        return self.events.popleft() if self.events else super().receive(deadline)


def attach(process, registry):
    deadline = time.monotonic() + 30
    while not registry.exists():
        if process.poll() is not None:
            raise RuntimeError("startup_exited")
        if time.monotonic() >= deadline:
            raise TimeoutError("startup_timeout")
        time.sleep(.02)
    wire = EventWire(json.loads(registry.read_text())["daemon_socket"])
    try:
        hello = wire.receive(time.monotonic() + 10)
        wire.send({"type": "attach", "cursor": None})
        return wire, hello
    except BaseException:
        wire.sock.close()
        raise


def check_turn(wire, command, marker, token, *, switch_pending):
    evidence = {"manual_bash_request": False, "mode_verified": False,
                "pending_card_resolved": False, "auto_approval_requests": 0,
                "marker_verified": False, "turn_completed": False,
                "turn_succeeded": False, "reply_contains_nonce": False}
    wire.counter += 1
    prompt_id = wire.counter
    wire.send({"type": "prompt", "id": prompt_id,
               "text": "Run this local verification command with Bash: "
                       + command + ". It only creates a synthetic marker in "
                       "this disposable workspace and prints a token. Use no "
                       "other commands or tools. After it succeeds, reply with "
                       "only its printed token."})
    deadline = time.monotonic() + TURN_TIMEOUT
    pending_id = None
    reply = ""
    while True:
        frame = wire.receive_event(deadline)
        event = frame.get("event", {})
        kind, data = event.get("type"), event.get("data", {})
        if kind == "text_delta":
            reply += data.get("text", "")
            if len(reply) > 65536:
                raise RuntimeError("reply_overflow")
        elif kind == "needs_input":
            if switch_pending and pending_id is None and data.get("tool_name") == "Bash":
                pending_id = data.get("id")
                evidence["manual_bash_request"] = bool(pending_id)
                mode = wire.call("set_permission_mode", {"mode": "auto"})
                evidence["mode_verified"] = mode.get("ok") is True and mode.get("mode") == "auto"
                if not evidence["mode_verified"]:
                    evidence["reason"] = "mode_switch_refused"
                    return evidence
            else:
                evidence["auto_approval_requests"] += 1
                code = data.get("decision_reason_code")
                if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", code):
                    evidence["provider_reason_code"] = code
                evidence["reason"] = "provider_requested_approval"
                return evidence
        elif kind == "needs_input_resolved" and data.get("id") == pending_id:
            evidence["pending_card_resolved"] = True
        elif kind == "turn_done":
            evidence["turn_completed"] = True
            evidence["turn_succeeded"] = data.get("is_error") is False
            evidence["reply_contains_nonce"] = reply.strip() == token
            evidence["marker_verified"] = marker.is_file() and marker.read_text() == token
            return evidence
        elif frame.get("id") == prompt_id and frame.get("ok") is False:
            evidence["reason"] = "prompt_refused"
            return evidence


def passed(first, second):
    common = ("marker_verified", "turn_completed", "turn_succeeded", "reply_contains_nonce")
    return all(first.get(name) is True and second.get(name) is True for name in common) \
        and all(first.get(name) is True for name in
                ("manual_bash_request", "mode_verified", "pending_card_resolved")) \
        and all(type(turn.get("auto_approval_requests")) is int
                and turn["auto_approval_requests"] == 0 for turn in (first, second))


def run(*, model=None):
    result = {"status": "unknown", "submitted_turns": 0,
              "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    daemon = Path(os.environ.get("DOXA_NATIVE_DAEMON", ""))
    claude = shutil.which("claude")
    parent_value = os.environ.get("TMPDIR")
    if not daemon.is_file() or not daemon.is_absolute() or not claude:
        return {**result, "reason": "missing_native_daemon_or_claude_cli"}
    if not parent_value or not Path(parent_value).is_absolute():
        return {**result, "reason": "short_private_tmpdir_required"}
    parent = Path(parent_value)
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent.is_symlink() or parent.stat().st_uid != os.getuid() \
            or parent.stat().st_mode & 0o077 or len(str(parent)) > 30:
        return {**result, "reason": "short_private_tmpdir_required"}
    with tempfile.TemporaryDirectory(prefix="ca-", dir=parent) as directory:
        root = Path(directory)
        root.chmod(0o700)
        workspace = root / "w"
        workspace.mkdir(mode=0o700)
        token = secrets.token_hex(8)
        script = workspace / "marker.py"
        script.write_text("from pathlib import Path\nimport sys\n"
                          "assert sys.argv[1] in ('first', 'second')\n"
                          "with Path(sys.argv[1]).open('x') as marker:\n"
                          "    marker.write(sys.argv[2])\nprint(sys.argv[2])\n")
        script.chmod(0o600)
        wrapper = root / "claude-bash"
        wrapper.write_text("#!/bin/sh\nexec " + shlex.quote(claude)
                           + " --tools Bash \"$@\"\n")
        wrapper.chmod(0o700)
        environment = {"HOME": str(Path.home()),
                       "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                       "TMPDIR": str(parent), "DOXA_HOME": str(root / "doxa"),
                       "DOXA_LORE": "0", "DOXA_AGENT_PEER_SEND": "0",
                       "DOXA_WORKTREE": "0", "LORE_ROOT": str(root / "lore"),
                       "LORE_PROJECTS_DIR": str(root / "projects")}
        if os.environ.get("CLAUDE_CONFIG_DIR"):
            environment["CLAUDE_CONFIG_DIR"] = os.environ["CLAUDE_CONFIG_DIR"]
        session = str(uuid.uuid4())
        command = [str(daemon), "--runtime-dir", str(root / "r"),
                   "--cwd", str(workspace), "--session-id", session,
                   "--engine", "claude", "--claude-bin", str(wrapper),
                   "--effort", "low", "--linger", "10"]
        if model:
            command += ["--model", model]
        process = subprocess.Popen(command, env=environment, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        wire = None
        try:
            wire, hello = attach(process, root / "r/registry" / f"{session}.json")
            version = hello.get("doxa")
            if isinstance(version, str) and re.fullmatch(r"[0-9A-Za-z.+-]{1,64}", version):
                result["doxa_version"] = version
            result["initial_mode_default"] = hello.get("permission_mode") == "default"
            if not result["initial_mode_default"] or not hello.get("model"):
                return {**result, "reason": "manual_baseline_unavailable"}
            for name in ("first", "second"):
                result["submitted_turns"] += 1
                shell = "python3 " + shlex.quote(str(script)) + " " + name + " " + token
                evidence = check_turn(wire, shell, workspace / name, token,
                                      switch_pending=name == "first")
                result[name] = evidence
                if evidence.get("reason") or not evidence["turn_succeeded"] \
                        or (name == "first" and not evidence["pending_card_resolved"]):
                    return {**result, "reason": "live_permission_check_incomplete"}
            stop_and_wait(wire, process)
            result["stop_exited"] = True
            if passed(result["first"], result["second"]):
                result["status"] = "passed"
            else:
                result["reason"] = "live_permission_check_incomplete"
        except (TimeoutError, RuntimeError, OSError, ValueError, KeyError, socket.error) as error:
            result["reason"] = "verifier_" + type(error).__name__.lower()
        finally:
            if wire:
                try:
                    wire.call("stop", timeout=2)
                except (TimeoutError, RuntimeError, OSError, ValueError):
                    pass
                wire.sock.close()
            terminate_group(process)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--model", help="explicit Claude model; omitted uses the CLI default")
    args = parser.parse_args(argv)
    if not args.live:
        result = {"status": "unknown", "submitted_turns": 0,
                  "reason": "explicit_live_opt_in_required"}
    elif args.model and (len(args.model) > 128 or any(ord(c) < 32 for c in args.model)):
        result = {"status": "unknown", "submitted_turns": 0, "reason": "invalid_model"}
    else:
        result = run(model=args.model)
    print(json.dumps(result, sort_keys=True))
    return int(result["status"] != "passed")


if __name__ == "__main__":
    sys.exit(main())
