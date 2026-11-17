#!/usr/bin/env python3
"""Exercise the Rust forwarding and SQLite admission boundary with synthetic responses."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

import benchmark as b
from pilot_ledger import FIXED_ARMS, JEV_ARM, LedgerRefused, PilotLedger, micros, policy

MAX_ADAPTER_PAYLOAD = 256 * 1024
ENVIRONMENT = {"PATH": "/usr/bin:/bin"}
SOURCE = "private synthetic candidate body"


def wire(adapter, state, **input_fields):
    payload = b.encode(input_fields)
    b.require(len(payload) <= MAX_ADAPTER_PAYLOAD, "pilot fixture request exceeds the adapter bound")
    result = subprocess.run(
        [str(adapter)], input=payload, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env={**ENVIRONMENT, "DOXA_HOME": str(state)}, timeout=10,
    )
    b.require(result.returncode == 0 and len(result.stdout) <= MAX_ADAPTER_PAYLOAD,
              "pilot wire fixture refused")
    return b.parse(result.stdout)


def bound_wire(prepared, framing=0):
    wire_bytes = prepared["wire"].encode("utf-8")
    receipt = prepared["receipt"]
    b.require(receipt["request_sha256"] == b.digest(wire_bytes)
              and receipt["input_bound"] == len(wire_bytes) + framing,
              "forwarding receipt does not bind the exact wire request")
    return wire_bytes, receipt


def complete_usage(status, body):
    if status != 200 or len(body) > MAX_ADAPTER_PAYLOAD:
        return None
    try:
        response = b.parse(body)
        choices = response["choices"]
        usage = response["usage"]
        model = response["model"]
        prompt, completion, total = (usage[key] for key in
                                     ("prompt_tokens", "completion_tokens", "total_tokens"))
        if (response["object"] == "chat.completion" and isinstance(model, str) and model
                and isinstance(choices, list) and len(choices) == 1
                and choices[0]["finish_reason"] in {"stop", "length", "tool_calls", "content_filter"}
                and type(prompt) is int and type(completion) is int and type(total) is int
                and prompt > 0 and completion >= 0 and total == prompt + completion):
            return model, prompt, completion
    except (KeyError, IndexError, TypeError, ValueError, RecursionError):
        pass
    return None


def run_call(adapter, state, ledger, scope, request, decision_transport, repair_transport):
    task, arm, slot = scope
    plan, _, _ = policy()
    if arm == JEV_ARM:
        config = b.router_config("flash-direct")
        config["candidates"] = [row for row in config["candidates"]
                                if (row["model"], row["effort"]) in FIXED_ARMS.values()]
        common = {"config": config, "request": request}
        decision_prepared = wire(adapter, state, operation="prepare", **common)
        decision_bytes, receipt = bound_wire(decision_prepared, plan["decision_input_framing_tokens"])
        decision = ledger.reserve_decision(
            task, slot, model=receipt["model"], input_bound=receipt["input_bound"],
            input_rate=micros(plan["decision_input_usd_per_million"]),
            request_sha256=receipt["request_sha256"],
        )
        b.require(decision.held_usd_micros == receipt["held_usd_micros"], "Jev hold differs from the wire bound")
        started = time.monotonic_ns()
        try:
            status, response = decision_transport(decision_bytes)
            if status != 200 or len(response) > 64 * 1024:
                return "ambiguous_decision", None
            selected = wire(adapter, state, operation="finish", **common,
                            decision_sha256=receipt["request_sha256"], response=response.decode("utf-8"))
        except (OSError, ValueError, subprocess.SubprocessError):
            return "ambiguous_decision", None
        observed = selected["decision"]
        ledger.settle_usage(
            decision.id, request_sha256=observed["request_sha256"], model=observed["model"],
            input_tokens=observed["usage"]["input_tokens"], output_tokens=observed["usage"]["output_tokens"],
            latency_ms=(time.monotonic_ns() - started) // 1_000_000,
        )
        prepared = selected["repair"]
    else:
        prepared = wire(adapter, state, operation="fixed", arm=arm, request=request)
    repair_bytes, receipt = bound_wire(prepared)
    candidate = next(row for row in plan["candidates"]
                     if (row["model"], row["effort"]) == (receipt["model"], receipt["effort"]))
    repair = ledger.reserve_repair(
        task, arm, slot, model=receipt["model"], effort=receipt["effort"],
        input_bound=receipt["input_bound"], output_limit=receipt["output_limit"],
        input_rate=micros(candidate["input_usd_per_million"]),
        output_rate=micros(candidate["output_usd_per_million"]),
        request_sha256=receipt["request_sha256"],
    )
    started = time.monotonic_ns()
    try:
        status, response = repair_transport(repair_bytes)
    except OSError:
        return "ambiguous_repair", None
    usage = complete_usage(status, response)
    if usage is None:
        return "ambiguous_repair", response
    model, prompt, completion = usage
    ledger.settle_usage(
        repair.id, request_sha256=receipt["request_sha256"], model=model,
        input_tokens=prompt, output_tokens=completion,
        latency_ms=(time.monotonic_ns() - started) // 1_000_000,
    )
    return "settled", response


def fixture_reply(allocation, kind, model):
    if kind == "dropped":
        raise TimeoutError("synthetic timeout after dispatch")
    if allocation == "decisions":
        target = {"deepseek-flash": "flash-direct", "deepseek-v4-pro": "pro-thinking"}[model]
        probabilities = {candidate: 0.94 if candidate == target else 0.06
                         for candidate in ("flash-direct", "pro-thinking")}
        response = {
            "model": "jev-1.13.0", "usage": {"input_tokens": 41, "output_tokens": 1},
            "answers": {"target": {"type": "choice", "choice": target,
                                   "probabilities": probabilities, "confidence": 0.88}},
        }
    else:
        response = {
            "object": "chat.completion", "model": model,
            "choices": [{"finish_reason": "stop", "message": {"content": SOURCE}}],
            "usage": {"prompt_tokens": 38, "completion_tokens": 11, "total_tokens": 49},
        }
    if kind == "missing_usage":
        response.pop("usage")
    return 200, b.encode(response) + b"\n"


def verify_hold(path, scope, allocation, outgoing):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        row = connection.execute(
            """SELECT request_sha256, held_usd_micros, settled_at_ns FROM reservations
               WHERE task_id = ? AND arm = ? AND slot = ? AND allocation = ?""",
            (*scope, allocation),
        ).fetchone()
    b.require(row is not None and row[0] == b.digest(outgoing) and row[1] > 0 and row[2] is None,
              "fixture transport observed dispatch without a committed exact hold")


def independent_summary(path):
    program = ("import json, sys\n"
               "from pilot_ledger import PilotLedger\n"
               "print(json.dumps(PilotLedger.open(sys.argv[1]).summary()))\n")
    result = subprocess.run(
        [sys.executable, "-c", program, str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        cwd=b.ROOT, env=ENVIRONMENT, check=True, timeout=10,
    )
    return b.parse(result.stdout)


def run_fixture(adapter, state):
    ledger_path = state / "fixture.sqlite3"
    ledger = PilotLedger.create(ledger_path)
    request = b.encode({
        "model": "doxa-pilot", "messages": [{"role": "user", "content": SOURCE}],
        "temperature": 0.2, "max_tokens": 32768, "stream": False,
    }).decode("utf-8")
    dispatches = {"decisions": 0, "repair": 0}
    wire_hashes = {}

    def submit(scope, model, *, decision_kind="success", repair_kind="success", expected):
        def response_sender(allocation, kind):
            def send(outgoing):
                verify_hold(ledger_path, scope, allocation, outgoing)
                dispatches[allocation] += 1
                wire_hashes[(scope, allocation)] = b.digest(outgoing)
                return fixture_reply(allocation, kind, model)
            return send

        result, response = run_call(
            adapter, state, ledger, scope, request,
            response_sender("decisions", decision_kind), response_sender("repair", repair_kind),
        )
        b.require(result == expected, "synthetic dispatch outcome differs")
        if result == "settled":
            b.require(response == fixture_reply("repair", repair_kind, model)[1],
                      "synthetic provider response changed")

    routed = ("easy-config", JEV_ARM, 1)
    fixed = ("easy-config", "fixed_pro_high", 1)
    submit(routed, "deepseek-v4-pro", expected="settled")
    submit(fixed, "deepseek-v4-pro", expected="settled")
    b.require(wire_hashes[(routed, "repair")] == wire_hashes[(fixed, "repair")],
              "matched fixed and routed wire hashes differ")
    ambiguous = ("easy-config", JEV_ARM, 2)
    submit(ambiguous, "deepseek-flash", repair_kind="missing_usage", expected="ambiguous_repair")
    dropped = ("easy-expiry", "fixed_flash_none", 1)
    submit(dropped, "deepseek-flash", repair_kind="dropped", expected="ambiguous_repair")
    no_decision = ("intermediate-graph", JEV_ARM, 1)
    submit(no_decision, "deepseek-flash", decision_kind="missing_usage", expected="ambiguous_decision")
    b.require(dispatches == {"decisions": 3, "repair": 4}, "incorrect synthetic dispatch count")

    before = ledger.summary()
    b.require(before == independent_summary(ledger_path), "a new process observed different ledger accounting")
    ledger = PilotLedger.open(ledger_path)
    for scope in (ambiguous, dropped, no_decision, ("easy-config", JEV_ARM, 3)):
        try:
            submit(scope, "deepseek-flash", expected="duplicate")
        except LedgerRefused:
            continue
        raise ValueError("an unresolved dispatch allowed a replay or continuation")
    smaller_cap = b.parse(request)
    smaller_cap["max_tokens"] -= 1
    pending = ("difficult-budget", "fixed_flash_none", 1)

    def unexpected_dispatch(_):
        raise RuntimeError("a refused allowance reached the fixture transport")

    try:
        run_call(adapter, state, ledger, pending, b.encode(smaller_cap).decode("utf-8"),
                 unexpected_dispatch, unexpected_dispatch)
    except LedgerRefused:
        pass
    else:
        raise ValueError("the ledger admitted a request without the frozen output allowance")
    after = ledger.summary()
    b.require(dispatches == {"decisions": 3, "repair": 4} and before == after,
              "replay changed dispatch counts or accounting")
    allocations = after["allocations"]
    b.require(allocations["decisions"]["unresolved"] == 1
              and allocations["repair"]["unresolved"] == 2
              and all(allocations[name]["unresolved_usd_micros"] > 0 for name in dispatches),
              "ambiguous reservations were released")
    result = {
        "schema": "doxa-pilot-admission-fixture-v1",
        "synthetic_dispatches": dispatches, "paid_requests": 0,
        "committed_holds_verified_at_dispatch": True,
        "frozen_allowance_refused_before_dispatch": True,
        "replays_and_unsettled_continuations_refused": True,
        "reopened_in_independent_process": True,
        "ledger": after,
    }
    b.require(SOURCE.encode() not in b.read(ledger_path) and SOURCE.encode() not in b.encode(result),
              "candidate body entered the fixture ledger or report")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wire_fixture", type=Path)
    adapter = parser.parse_args().wire_fixture.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="doxa-pilot-admission-") as root:
        print(json.dumps(run_fixture(adapter, Path(root)), indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit("pilot admission fixture refused: " + str(error))
