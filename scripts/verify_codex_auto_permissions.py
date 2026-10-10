#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Opt-in Codex permission verification through the native DOXA daemon.

At most three synthetic turns. Test a pending command switch, then an idle
same-session switch and two ordinary sandboxed commands. The last command
probes one disposable path outside the workspace and temporary-directory roots. Only the exact
disposable baseline command may receive one approval if the active switch is
refused. Auto turns are never approved. Receipts exclude account/provider text.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from verify_claude_auto_permissions import attach
from verify_native_claude_live import stop_and_wait
from verify_native_vendors_live import terminate_group

TURN_TIMEOUT = 90


def command_matches(summary, command):
    try:
        value = json.loads(summary)
        actual = shlex.split(value["command"])
        expected = shlex.split(command)
        return actual == expected or (len(actual) == 3
            and actual[0] in ("bash", "/bin/bash", "/usr/bin/bash")
            and actual[1] in ("-c", "-lc") and shlex.split(actual[2]) == expected)
    except (TypeError, ValueError, KeyError):
        return False


def turn(wire, command, marker, token, *, baseline=False):
    evidence = {"command_approvals": 0, "baseline_approved_once": False,
                "active_switch_verified": False, "active_switch_rejected": False,
                "pending_preserved_after_refusal": False, "pending_card_resolved": False,
                "marker_verified": False, "turn_completed": False,
                "turn_succeeded": False, "exact_reply": False, "tool_calls": 0,
                "tool_failures": 0, "sandbox_probe_unexpectedly_writable": False,
                "tool_permission_error": False}
    wire.counter += 1
    prompt_id = wire.counter
    instruction = ("For this permission UI verification, request sandbox_permissions "
                   "require_escalated with justification 'Disposable marker verification'. "
                   "If the approval policy changes to never before execution, retry the exact same "
                   "command once using the normal sandbox, without escalation. "
                   if baseline else "Use the normal sandbox; do not request escalation. ")
    wire.send({"type": "prompt", "id": prompt_id, "text":
        "Use exec_command to attempt this exact command: " + command + ". "
        + instruction + "The command only creates a disposable marker and prints a token. "
        "For normal sandbox execution, run it once. Use no other commands or tools. "
        "Reply only with its printed token."})
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
        elif kind == "tool_call":
            evidence["tool_calls"] += 1
        elif kind == "tool_result":
            evidence["tool_failures"] += int(data.get("is_error") is True)
        elif kind == "tool_result_detail":
            detail = data.get("text", "")
            if isinstance(detail, str):
                evidence["sandbox_probe_unexpectedly_writable"] |= "sandbox probe unexpectedly writable" in detail
                evidence["tool_permission_error"] |= "PermissionError" in detail
        elif kind == "needs_input":
            evidence["command_approvals"] += 1
            if not baseline or pending_id is not None or data.get("tool_name") != "command_execution" \
                    or not command_matches(data.get("input_summary"), command):
                return {**evidence, "reason": "unexpected_approval_left_unanswered"}
            pending_id = data.get("id")
            if not isinstance(pending_id, str) or not pending_id:
                return {**evidence, "reason": "missing_pending_identity"}
            mode = wire.call("set_permission_mode", {"mode": "auto"})
            evidence["active_switch_verified"] = mode.get("ok") is True and mode.get("mode") == "auto"
            if not evidence["active_switch_verified"]:
                evidence["active_switch_rejected"] = mode.get("ok") is False \
                    and mode.get("error") == "Finish the current response and queued prompts, then change permissions for the next turn"
                status = wire.call("status").get("status", {})
                state = wire.call("get_state")
                evidence["pending_preserved_after_refusal"] = status.get("permission_mode") == "on-request" \
                    and state.get("pending_inputs_complete") is True \
                    and any(item.get("id") == pending_id for item in state.get("pending_inputs", [])) \
                    and not marker.exists()
                if not evidence["active_switch_rejected"] or not evidence["pending_preserved_after_refusal"]:
                    return {**evidence, "reason": "unverified_active_switch_result"}
                approval = wire.call("answer_needs_input", {"id": pending_id, "answer": {"decision": "allow"}})
                evidence["baseline_approved_once"] = approval.get("ok") is True
                if not evidence["baseline_approved_once"]:
                    return {**evidence, "reason": "baseline_approval_refused"}
        elif kind == "needs_input_resolved" and pending_id and data.get("id") == pending_id:
            evidence["pending_card_resolved"] = True
        elif kind == "turn_done":
            evidence["turn_completed"] = True
            evidence["turn_succeeded"] = data.get("is_error") is False
            evidence["exact_reply"] = reply.strip() == token
            evidence["marker_verified"] = marker.is_file() and marker.read_text() == token
            return evidence
        elif frame.get("id") == prompt_id and frame.get("ok") is False:
            return {**evidence, "reason": "prompt_refused"}


def successful(evidence):
    return all(evidence.get(key) is True for key in
               ("turn_completed", "turn_succeeded", "exact_reply", "marker_verified"))


def idle(wire):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = wire.call("status").get("status", {})
        if state.get("running") is False and type(state.get("queued")) is int and state["queued"] == 0:
            return state
        time.sleep(.02)
    raise TimeoutError("idle_timeout")


def checkpoint(root, session):
    paths = list((root / "projects").rglob(session + ".codex.json"))
    if len(paths) != 1:
        raise RuntimeError("checkpoint_identity_unavailable")
    data = json.loads(paths[0].read_text())
    if data.get("session_id") != session or data.get("turn_incomplete") is not False:
        raise RuntimeError("incomplete_checkpoint")
    return data


def summarize(result):
    turns = [result.get(name, {}) for name in ("baseline", "first_auto", "second_auto")]
    automatic = all(successful(turn) and type(turn.get("command_approvals")) is int
                    and turn["command_approvals"] == 0 for turn in turns[1:])
    continuity = result.get("same_provider_thread") is True and result.get("auto_mode_persisted") is True
    sandbox = result.get("sandbox_write_blocked") is True
    result["auto_commands"] = "passed" if automatic and continuity and sandbox else "unknown"
    result["same_session_idle_switch"] = "passed" if result.get("idle_switch_verified") is True \
        and automatic and continuity and sandbox else "unknown"
    first = turns[0]
    if successful(first) and first.get("pending_card_resolved") is True and first.get("active_switch_verified") is True:
        result["pending_command_switch"] = "passed"
    elif successful(first) and first.get("pending_card_resolved") is True \
            and first.get("active_switch_rejected") is True \
            and first.get("pending_preserved_after_refusal") is True and first.get("baseline_approved_once") is True:
        result["pending_command_switch"] = "unsupported_requires_idle"
    else:
        result["pending_command_switch"] = "unknown"
    result["status"] = "unknown"
    if result["auto_commands"] == result["same_session_idle_switch"] == "passed" and result.get("stop_exited") is True:
        if result["pending_command_switch"] == "passed":
            result["status"] = "passed"
        elif result["pending_command_switch"] == "unsupported_requires_idle":
            result["status"] = "partial"
    return result


def run(daemon, codex, lore, auth_home, parent, model):
    result = {"status": "unknown", "submitted_turns": 0,
              "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    if not all(path.is_absolute() for path in (daemon, codex, lore, auth_home, parent)) \
            or not all(path.is_file() for path in (daemon, codex, lore)):
        return {**result, "reason": "absolute_installed_paths_required"}
    if not parent.is_dir() or parent.is_symlink() or parent.stat().st_uid != os.getuid() \
            or parent.stat().st_mode & 0o077 or len(str(parent)) > 30:
        return {**result, "reason": "short_private_tmpdir_required"}
    result["daemon_sha256"] = hashlib.sha256(daemon.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="cx-", dir=parent) as directory, \
            tempfile.TemporaryDirectory(prefix=".doxa-codex-sandbox-", dir=Path.home()) as outside_directory:
        root = Path(directory)
        root.chmod(0o700)
        workspace = root / "w"
        workspace.mkdir(mode=0o700)
        private_codex = root / "codex"
        private_codex.mkdir(mode=0o700)
        for name in ("auth.json", "models_cache.json"):
            source = auth_home / name
            if source.is_file():
                destination = private_codex / name
                shutil.copyfile(source, destination)
                destination.chmod(0o600)
        if not (private_codex / "auth.json").is_file():
            return {**result, "reason": "missing_existing_account_login"}
        token = secrets.token_hex(8)
        script = workspace / "marker.py"
        script.write_text("from pathlib import Path\nimport sys\n"
            "assert sys.argv[1] in ('baseline', 'first_auto', 'second_auto')\n"
            "with Path(sys.argv[1]).open('x') as marker:\n"
            "    marker.write(sys.argv[2])\n"
            "if sys.argv[1] == 'second_auto':\n"
            "    try:\n        Path(sys.argv[3]).write_text('disposable sandbox probe')\n"
            "    except OSError as error:\n"
            "        if error.errno not in (1, 13, 30):\n            raise\n"
            "        Path('sandbox-blocked').write_text(sys.argv[2])\n"
            "        Path('sandbox-errno').write_text(str(error.errno))\n"
            "    else:\n        raise RuntimeError('sandbox probe unexpectedly writable')\n"
            "print(sys.argv[2])\n")
        script.chmod(0o600)
        environment = {"HOME": str(Path.home()), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TMPDIR": str(parent), "DOXA_HOME": str(root / "doxa"), "CODEX_HOME": str(private_codex),
            "LORE_ROOT": str(root / "lore"), "LORE_PROJECTS_DIR": str(root / "projects"),
            "DOXA_LORE_RS": str(lore), "DOXA_LORE": "0", "DOXA_AGENT_PEER_SEND": "0",
            "DOXA_PEER_INBOUND_TURNS": "0", "DOXA_SPAWN_SESSIONS": "0", "DOXA_WORKTREE": "0",
            "LORE_DISABLE_SYNC": "1", "LORE_SYNC_URL": ""}
        session = str(uuid.uuid4())
        command = [str(daemon), "--runtime-dir", str(root / "r"), "--cwd", str(workspace),
            "--session-id", session, "--engine", "codex", "--codex-bin", str(codex),
            "--effort", "low", "--sandbox", "workspace-write", "--linger", "10"]
        if model:
            command += ["--model", model]
        process = subprocess.Popen(command, env=environment, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        wire = None
        try:
            wire, hello = attach(process, root / "r/registry" / (session + ".json"))
            version = hello.get("doxa")
            if isinstance(version, str) and re.fullmatch(r"[0-9A-Za-z.+-]{1,64}", version):
                result["doxa_version"] = version
            selected_model = hello.get("model")
            if isinstance(selected_model, str) and re.fullmatch(r"[0-9A-Za-z.+-]{1,128}", selected_model):
                result["model"] = selected_model
            result["initial_on_request"] = hello.get("permission_mode") == "on-request"
            if not result["initial_on_request"]:
                return {**result, "reason": "manual_baseline_unavailable"}
            thread_id = None
            for name in ("baseline", "first_auto", "second_auto"):
                if name == "first_auto":
                    idle(wire)
                    mode = wire.call("set_permission_mode", {"mode": "auto"})
                    result["idle_switch_verified"] = mode.get("ok") is True and mode.get("mode") == "auto"
                    if not result["idle_switch_verified"]:
                        return {**result, "reason": "idle_switch_refused"}
                result["submitted_turns"] += 1
                shell = "python3 " + shlex.quote(str(script)) + " " + name + " " + token
                if name == "second_auto":
                    shell += " " + shlex.quote(str(Path(outside_directory) / "marker"))
                result[name] = turn(wire, shell, workspace / name, token, baseline=name == "baseline")
                if name == "second_auto":
                    blocked = workspace / "sandbox-blocked"
                    result["sandbox_write_blocked"] = blocked.is_file() and blocked.read_text() == token \
                        and not (Path(outside_directory) / "marker").exists()
                    result["sandbox_outside_marker_absent"] = not (Path(outside_directory) / "marker").exists()
                    denied = workspace / "sandbox-errno"
                    if denied.is_file() and denied.read_text() in ("1", "13", "30"):
                        result["sandbox_denial_errno"] = int(denied.read_text())
                if not successful(result[name]) or result[name].get("reason"):
                    return {**result, "reason": "command_execution_unverified"}
                idle(wire)
                saved = checkpoint(root, session)
                if thread_id is None:
                    thread_id = saved.get("thread_id")
                elif not thread_id or saved.get("thread_id") != thread_id:
                    return {**result, "reason": "provider_thread_changed"}
            blocked = workspace / "sandbox-blocked"
            result["sandbox_write_blocked"] = blocked.is_file() and blocked.read_text() == token \
                and not (Path(outside_directory) / "marker").exists()
            result["same_provider_thread"] = bool(thread_id)
            result["auto_mode_persisted"] = saved.get("permission_mode") == "auto"
            stop_and_wait(wire, process)
            result["stop_exited"] = True
            result = summarize(result)
        except (TimeoutError, RuntimeError, OSError, ValueError, KeyError) as error:
            result["reason"] = "verifier_" + type(error).__name__.lower()
            if str(error) in ("startup_exited", "startup_timeout", "checkpoint_identity_unavailable",
                              "incomplete_checkpoint", "idle_timeout", "reply_overflow"):
                result["reason"] = str(error)
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
    parser.add_argument("--daemon", type=Path, default=Path.home() / ".local/bin/doxa-daemon-rs")
    parser.add_argument("--codex", type=Path, default=Path.home() / ".local/share/doxa/providers/codex-current/codex")
    parser.add_argument("--lore", type=Path, default=Path.home() / ".local/bin/lore-rs")
    parser.add_argument("--auth-home", type=Path, default=Path.home() / ".codex")
    parser.add_argument("--model", help="explicit account model; omitted uses the account default")
    args = parser.parse_args(argv)
    if not args.live:
        result = {"status": "unknown", "submitted_turns": 0, "reason": "explicit_live_opt_in_required"}
    elif args.model and not re.fullmatch(r"[a-zA-Z0-9._-]{1,128}", args.model):
        result = {"status": "unknown", "submitted_turns": 0, "reason": "invalid_model"}
    else:
        result = run(args.daemon, args.codex, args.lore, args.auth_home,
                     Path(os.environ.get("TMPDIR", "")), args.model)
    print(json.dumps(result, sort_keys=True))
    return int(result["status"] != "passed")


if __name__ == "__main__":
    sys.exit(main())
