#!/usr/bin/env python3
"""Compare prepared code graph queries with idle and busy installed fixture daemons."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import tempfile
import threading
import time

from bench_codegraph_tui import COLS, ROWS, Tui, power_state, stats

FIXTURE_TEXT = "Deterministic native fixture response."


class FixtureClient:
    def __init__(self, sock: socket.socket, reader, session_id: str, root: Path):
        self.sock, self.reader = sock, reader
        hello = self.read()
        if (hello.get("type") != "hello" or hello.get("proto") != 1 or hello.get("engine") != "fixture"
                or hello.get("session_id") != session_id or hello.get("cwd") != str(root)):
            raise RuntimeError("daemon hello does not match the private installed fixture")
        self.send({"type": "attach", "cursor": hello["next_seq"]})
        self.request_id = 0

    def send(self, frame: dict) -> None:
        self.sock.sendall(json.dumps(frame, separators=(",", ":")).encode() + b"\n")

    def read(self) -> dict:
        line = self.reader.readline(256 * 1024 + 1)
        if not line or len(line) > 256 * 1024:
            raise RuntimeError("invalid daemon frame in fixture benchmark")
        return json.loads(line)

    def turn(self) -> float:
        self.request_id += 1
        self.send({"type": "prompt", "id": self.request_id, "text": "Fixture output latency probe"})
        observed = None
        accepted = False
        for _ in range(64):
            frame = self.read()
            if frame.get("type") == "reply" and frame.get("id") == self.request_id:
                if frame.get("ok") is not True:
                    raise RuntimeError("fixture prompt was refused")
                accepted = True
            event = frame.get("event", {})
            if event.get("type") == "text_delta":
                if event.get("data", {}).get("text") != FIXTURE_TEXT or observed is not None:
                    raise RuntimeError("unexpected installed fixture daemon output")
                observed = time.perf_counter()
            if event.get("type") == "turn_done" and observed is not None:
                if not accepted or event.get("data", {}).get("is_error") is True:
                    raise RuntimeError("fixture turn did not complete successfully")
                return observed
        raise RuntimeError("fixture turn exceeded its frame bound")


def fixture_registry(runtime: Path) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        files = list((runtime / "registry").glob("*.json"))
        if len(files) > 1:
            raise RuntimeError("expected one daemon in the isolated benchmark runtime")
        if files:
            entry = json.loads(files[0].read_text())
            if entry.get("engine") != "fixture" or Path(entry["daemon_socket"]).parent != runtime:
                raise RuntimeError("fixture registry does not match isolated benchmark runtime")
            return entry
        time.sleep(0.01)
    raise RuntimeError("installed fixture daemon did not publish its registry")


def concurrent_span(tui: Tui, daemon: FixtureClient, query: str, pool: ThreadPoolExecutor) -> dict:
    release = threading.Event()
    stop = threading.Event()
    span = []

    def emit_turns() -> list[float]:
        if not release.wait(5):
            raise RuntimeError("Enter was never submitted")
        observed = []
        while not stop.is_set():
            observed.append(daemon.turn())
            stop.wait(0.012)
        return observed

    def before_enter() -> list:
        release.set()
        return []

    def complete(started: float, visible: float) -> None:
        span.extend((started, visible))
        stop.set()

    worker = pool.submit(emit_turns)
    try:
        row = tui.timed_enter(query, before_write=before_enter, after_answer=complete)
    finally:
        stop.set()
        release.set()
    observed = worker.result(timeout=5)
    row["daemon_delta_ms"] = [round((at - span[0]) * 1000, 3) for at in observed if span[0] <= at <= span[1]]
    return row


def run(args, index: int, root: Path) -> dict:
    case = root / f"r{index}"
    home, runtime = case / "h", case / "rt"
    case.mkdir(mode=0o700)
    for path in (home, runtime):
        path.mkdir(mode=0o700)
    busy = index % 2 == 1
    record = {"process": index + 1, "condition": "concurrent" if busy else "idle",
              "load_before": round(os.getloadavg()[0], 2), "power_before": power_state()}
    tui = Tui(args.binary, home, None, args.root, None, daemon=args.daemon, runtime=runtime)
    try:
        entry = fixture_registry(runtime)
        tui.wait_screen(lambda screen: "fixture" in screen and "Prompt" in screen, what="installed fixture attach")
        sock = socket.socket(socket.AF_UNIX)
        with closing(sock), ThreadPoolExecutor(max_workers=1) as pool:
            sock.settimeout(4)
            sock.connect(entry["daemon_socket"])
            with sock.makefile("rb") as reader:
                daemon = FixtureClient(sock, reader, entry["session_id"], args.root)
                daemon.turn()
                tui.wait_screen(lambda screen: FIXTURE_TEXT in screen, what="real daemon output in installed TUI")
                tui.wait_idle()
                records = []
                for query in (f"file {args.file}", f"calls {args.file}", f"imports {args.file}",
                              f"symbol {args.symbol}", f"file {args.python_file}", f"calls {args.python_file}",
                              f"imports {args.python_file}", "symbol __doxa_daemon_pty_missing__"):
                    tui.type_command(f"/codegraph {query}")
                    if busy:
                        row = concurrent_span(tui, daemon, query, pool)
                    else:
                        row = {**tui.timed_enter(query), "daemon_delta_ms": []}
                    if row["origin"] != "indexed" or row["coverage"] is None:
                        raise RuntimeError("installed daemon benchmark expected a prepared indexed answer")
                    records.append(row)
                record["queries"] = records
    finally:
        tui.quit()
    deadline = time.monotonic() + 5
    while list((runtime / "registry").glob("*.json")):
        if time.monotonic() > deadline:
            raise RuntimeError("installed fixture daemon remained in its isolated runtime")
        time.sleep(0.01)
    record["power_after"] = power_state()
    record["load_after"] = round(os.getloadavg()[0], 2)
    return record


def summarize(records: list[dict]) -> dict:
    result = {}
    for condition in ("idle", "concurrent"):
        rows = [row for record in records if record["condition"] == condition for row in record["queries"]]
        overlapping = [row for row in rows if row["daemon_delta_ms"]]
        result[condition] = {
            "answer_visible": stats([row["answer_visible_ms"] for row in rows]),
            "scanning_visible": stats([row["scanning_visible_ms"] for row in rows if row["scanning_visible_ms"] is not None]),
            "queries_with_daemon_delta": len(overlapping),
            "deltas_during_query": sum(len(row["daemon_delta_ms"]) for row in rows),
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True, help="installed doxa-rs binary")
    parser.add_argument("--daemon", type=Path, required=True, help="matching installed doxa-daemon-rs binary")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--python-file", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--runs", type=int, default=8, help="even number of processes with alternating conditions")
    args = parser.parse_args()
    if args.runs % 2 or not 2 <= args.runs <= 100:
        parser.error("--runs must be even and between 2 and 100")
    for attribute in ("binary", "daemon", "root"):
        setattr(args, attribute, getattr(args, attribute).resolve(strict=True))
    binaries = {name: hashlib.sha256(getattr(args, name).read_bytes()).hexdigest() for name in ("binary", "daemon")}
    version = subprocess.check_output([str(args.binary), "--version"], text=True, timeout=5,
                                      env={"PATH": "/usr/bin:/bin"})
    if "Build    native Rust · release" not in version:
        raise RuntimeError("benchmark requires a release frontend")
    frontend_commit = next(line.strip().removeprefix("Commit").strip() for line in version.splitlines()
                           if line.strip().startswith("Commit "))
    tree = subprocess.check_output(["git", "-C", str(args.root), "rev-parse", "HEAD^{tree}"], text=True, timeout=5).strip()
    dirty = bool(subprocess.check_output(["git", "-C", str(args.root), "status", "--porcelain"], timeout=5))
    with tempfile.TemporaryDirectory(prefix="dxg-", dir="/tmp") as temp:
        records = [run(args, index, Path(temp).resolve()) for index in range(args.runs)]
    if any(hashlib.sha256(getattr(args, name).read_bytes()).hexdigest() != digest for name, digest in binaries.items()):
        raise RuntimeError("installed executable changed during the benchmark")
    coverage = [row["coverage"] for record in records for row in record["queries"]]
    if not coverage or any(row != coverage[0] for row in coverage):
        raise RuntimeError("fixture coverage changed during the benchmark")
    print(json.dumps({"benchmark": "installed_fixture_daemon_codegraph_pty", "sha256": binaries,
                      "frontend_commit": frontend_commit, "workload_tree": tree, "workload_dirty_at_start": dirty,
                      "kernel": platform.release(), "size": [COLS, ROWS],
                      "span": "Enter written to PTY master until answer header rows arrive at PTY master",
                      "summary": summarize(records), "records": records}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
