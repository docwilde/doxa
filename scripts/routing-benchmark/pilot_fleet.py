"""Bind a development fleet to one private launch intent and retained pilot hold."""

import hashlib
import os
from pathlib import Path
import stat
import subprocess

import benchmark as b
from pilot_coordinator import locked, open_run
from pilot_ledger import bounded_int, micros, policy


def executable_pin(path):
    path = Path(path).resolve(strict=True)
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        b.require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                  and not info.st_mode & 0o022 and info.st_mode & 0o100,
                  "an owned, trusted native executable is required")
        return {"path": str(path), "sha256": hashlib.file_digest(stream, "sha256").hexdigest()}


def router_config(plan):
    flash = next(row for row in plan["candidates"] if row["model"] == "deepseek-flash")
    candidate = {
        "id": "flash", "provider": "deepseek", "model": flash["model"], "effort": flash["effort"],
        "description": "Pinned DeepSeek development target",
        "context_tokens": 36_864, "max_output_tokens": 4096, "max_tool_requests": 4,
        "supports_tools": True, "input_usd_micros_per_million": micros(flash["input_usd_per_million"]),
        "output_usd_micros_per_million": micros(flash["output_usd_per_million"]),
    }
    config = {
        "version": 1, "jev_model": plan["router_model"], "criteria_version": "development-fleet-v1",
        "candidates": [candidate, {**candidate, "id": "flash-alternate"}],
        "fallback_id": "flash", "confidence_threshold": 0.75, "max_calls": 3,
        "max_spend_usd_micros": 3, "max_input_bytes": 24_576, "deadline_ms": 1000,
    }
    return config, flash


def plan_fleet(state, slot, binary, daemon, workspace):
    plan, _, _ = policy()
    b.require(bounded_int(slot, 1, plan["max_fleets"]), "fleet slot is outside the pilot")
    b.private_directory(workspace)
    with locked(state):
        _, run = open_run(state)
        b.grader_preflight(run["grader_host"], run["grader_image"])
        intent_root = state / f"slot-{slot}"
        intent_root.mkdir(mode=0o700)
        runtime = state / "f"
        runtime.mkdir(mode=0o700, exist_ok=True)
        home = intent_root / "home"
        home.mkdir(mode=0o700)
        run_id = f"p-{run['ledger_run_id'][:12]}-{slot}"
        binary, daemon = executable_pin(binary), executable_pin(daemon)
        config, flash = router_config(plan)
        config_path = intent_root / "router.json"
        config_sha256 = b.create(config_path, config)
        args = [
            binary["path"], "fleet", "start", "--pool", "router:flash", "--supervisor", "router:flash",
            "--router-config", str(config_path), "--sessions", "2",
            "--run-budget", "2.49", "--review-budget", "0.24", "--isolation", "docker-offline",
            "--alignment-supervisor", "deepseek:deepseek-flash", "--supervision-mode", "shadow",
            "--message-review", "shadow", "--message-judge", "llm:deepseek:deepseek-flash",
            "--review-input-price", flash["input_usd_per_million"],
            "--review-output-price", flash["output_usd_per_million"],
            "--review-max-calls", "6", "--approve", "none", "--memory-off", "2",
            "--root", str(runtime), "--run-id", run_id, "--cwd", str(workspace),
            "--quiescence-timeout", "180", "--quiet-dwell", "20",
            "--prompt", "Read README.md, report one evidence-backed status, and finish your turn.",
        ]
        environment = {
            "PATH": "/usr/bin:/bin", "HOME": str(home), "DOXA_HOME": str(home),
            "DOXA_DAEMON_BIN": daemon["path"], "DOXA_DOCKER_HOST": run["grader_host"],
            "DOXA_DOCKER_IMAGE": run["grader_image"], "DOXA_LORE": "0", "DOXA_MCP_ENABLED": "0",
            "LORE_DISABLE_SYNC": "1", "LORE_DISABLE_REVIEW": "1",
        }
        preflight = subprocess.run([*args, "--dry-run"], cwd=workspace, env=environment,
                                   capture_output=True, timeout=15, check=True)
        b.require(b"Access review:" in preflight.stdout and not os.path.lexists(runtime / run_id),
                  "dry run did not confirm native preflight without creating the fleet")
        intent = {
            "ledger_run_id": run["ledger_run_id"], "slot": slot, "run_id": run_id,
            "binary": binary, "daemon": daemon, "args": args, "environment": environment,
            "router_path": str(config_path), "router_sha256": config_sha256,
            "runtime": str(runtime), "workspace": str(workspace),
            "ceiling_usd_micros": micros(plan["usd_per_fleet"]),
        }
        return b.create(intent_root / "intent.json", intent)


def drive_fleet(state, slot, expected_intent, execute, *, resume=False):
    """Call the admitted executor with the exact native start or resume command."""
    with locked(state):
        ledger, run = open_run(state)
        command = state / f"slot-{slot}" / "intent.json"
        raw = b.read(command)
        b.require(b.digest(raw) == expected_intent, "fleet launch intent changed")
        intent = b.parse(raw)
        b.require(intent["ledger_run_id"] == run["ledger_run_id"] and intent["slot"] == slot
                  and intent["run_id"] == f"p-{run['ledger_run_id'][:12]}-{slot}",
                  "fleet launch intent belongs to a different pilot slot")
        for role in ("binary", "daemon"):
            b.require(executable_pin(intent[role]["path"]) == intent[role], "fleet executable changed")
        router = Path(intent["router_path"])
        b.require(b.digest(b.read(router)) == intent["router_sha256"], "fleet Router configuration changed")
        runtime = Path(intent["runtime"])
        run_dir = runtime / intent["run_id"]
        if resume:
            ledger.verify_fleet_hold(slot, expected_intent)
            verify_resume(run_dir, intent)
            args = [intent["binary"]["path"], "fleet", "resume", intent["run_id"], "--root", str(runtime)]
        else:
            b.require(not os.path.lexists(run_dir), "existing fleet can only be resumed")
            ledger.reserve_fleet(slot, ceiling_usd_micros=intent["ceiling_usd_micros"],
                                 configuration_sha256=expected_intent)
            args = intent["args"]
        return execute(args, intent["environment"], Path(intent["workspace"]))


def verify_resume(run_dir, intent):
    b.private_directory(run_dir)
    data = b.parse(b.read(run_dir / "manifest.json", 1024 * 1024))
    spec, slots = data["spec"], data["slots"]
    b.require(data["native_version"] == 1 and data["run_id"] == intent["run_id"]
              and data["phase"] == "monitoring" and data["live"] is True and data["mode"] == "supervisor"
              and data["ledger_path"] == str(run_dir / "home/peers/messages.jsonl")
              and spec["cwd"] == intent["workspace"] and spec["isolation"] == "docker-offline"
              and spec["run_budget_usd"] == 2.49 and spec["lore_enabled"] is False
              and data["supervision"]["context"]["review"]["budget_usd"] == 0.24
              and len(slots) == 3, "fleet runtime differs from the admitted launch")
    for index, row in enumerate(slots):
        config = run_dir / f"router-configs/slot-{index}.json"
        b.require(row["index"] == index and row["engine"] == "router" and row["model"] == "flash"
                  and row["isolation"] == "docker-offline" and row["router_config"] == str(config)
                  and row["router_config_sha256"] == b.digest(b.read(config)),
                  "fleet slot or Router journal identity changed")
