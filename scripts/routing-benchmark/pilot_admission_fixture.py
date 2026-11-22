#!/usr/bin/env python3
"""Exercise the Rust forwarding and SQLite admission boundary with synthetic responses."""

import argparse
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading

import benchmark as b
from pilot_dispatch import MAX_ADAPTER_PAYLOAD, post_exact, run_call
from pilot_ledger import JEV_ARM, LedgerRefused, PilotLedger

ENVIRONMENT = {"PATH": "/usr/bin:/bin"}
SOURCE = "private synthetic candidate body"


def fixture_reply(allocation, kind, model):
    if kind == "dropped":
        raise TimeoutError("synthetic timeout after dispatch")
    if allocation == "decisions":
        target = {"deepseek-flash": "flash-direct", "deepseek-v4-pro": "pro-thinking"}[model]
        score, other, confidence = (0.62, 0.38, 0.24) if kind == "low_confidence" else (0.94, 0.06, 0.88)
        probabilities = {candidate: score if candidate == target else other
                         for candidate in ("flash-direct", "pro-thinking")}
        response = {
            "model": "jev-1.13.0",
            "usage": {"input_tokens": 100_000 if kind == "oversized_usage" else 41, "output_tokens": 1},
            "answers": {"target": {"type": "choice", "choice": target,
                                   "probabilities": probabilities, "confidence": confidence}},
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


def through_http(callback, body):
    """Drive the production byte transport against one local fixture response."""
    errors = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            try:
                b.require(self.path == "/test" and self.headers["Authorization"] == "Bearer fixture",
                          "unexpected fixture request")
                length = int(self.headers["Content-Length"])
                b.require(0 < length <= MAX_ADAPTER_PAYLOAD, "unexpected fixture body size")
                status, payload = callback(self.rfile.read(length))
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except TimeoutError:
                self.close_connection = True
            except Exception as error:
                errors.append(error)
                self.close_connection = True

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        server.timeout = 2
        worker = threading.Thread(target=server.handle_request)
        worker.start()
        try:
            return post_exact(http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2),
                              "/test", body, key="fixture", response_limit=MAX_ADAPTER_PAYLOAD)
        finally:
            worker.join(timeout=3)
            b.require(not worker.is_alive(), "fixture HTTP request did not stop")
            if errors:
                raise errors[0]


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
            def respond(outgoing):
                verify_hold(ledger_path, scope, allocation, outgoing)
                dispatches[allocation] += 1
                wire_hashes[(scope, allocation)] = b.digest(outgoing)
                return fixture_reply(allocation, kind, model)
            return lambda outgoing: through_http(respond, outgoing)

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
    b.require(dispatches == {"decisions": 3, "repair": 4} and before == ledger.summary(),
              "replay changed dispatch counts or accounting")
    allocations = before["allocations"]
    b.require(allocations["decisions"]["unresolved"] == 1
              and allocations["repair"]["unresolved"] == 2
              and all(allocations[name]["unresolved_usd_micros"] > 0 for name in dispatches),
              "ambiguous reservations were released")
    no_selection = ("intermediate-delegation", JEV_ARM, 1)
    submit(no_selection, "deepseek-flash", decision_kind="low_confidence", expected="unselected_decision")
    b.require(ledger.summary()["allocations"]["decisions"]["settled"] == 3
              and dispatches == {"decisions": 4, "repair": 4},
              "verified unselected decision was not settled independently of forwarding")
    overage = ("difficult-budget", JEV_ARM, 1)
    try:
        submit(overage, "deepseek-flash", decision_kind="oversized_usage", expected="no_repair")
    except LedgerRefused as error:
        b.require("exceeded the frozen scope" in str(error), "unexpected overage refusal")
    else:
        raise ValueError("verified Jev usage overage did not freeze admissions")
    ledger = PilotLedger.open(ledger_path)
    try:
        run_call(adapter, state, ledger, pending, request, unexpected_dispatch, unexpected_dispatch)
    except LedgerRefused as error:
        b.require("exceeded the frozen scope" in str(error), "unrelated repair refusal did not bind the overage")
    else:
        raise ValueError("verified Jev usage overage permitted another dispatch")
    after = ledger.summary()
    b.require(after["scope_breach"] and after == independent_summary(ledger_path)
              and dispatches == {"decisions": 5, "repair": 4},
              "overage did not persist or changed repair dispatches")
    result = {
        "schema": "doxa-pilot-admission-fixture-v1",
        "synthetic_dispatches": dispatches, "paid_requests": 0,
        "committed_holds_verified_at_dispatch": True,
        "frozen_allowance_refused_before_dispatch": True,
        "replays_and_unsettled_continuations_refused": True,
        "reopened_in_independent_process": True,
        "verified_jev_overage_freezes_admission": True,
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
