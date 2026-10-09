#!/usr/bin/env python3
"""Bounded, read-only end-to-end benchmark of doxa-codegraph source scans.

Build the production binary first, then pass it with --binary. A no-hit symbol
query parses every listed Rust/Python source without depending on a known file.
The harness never writes to a repository or creates a temporary directory.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path


def percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    return round(ordered[math.ceil(fraction * len(ordered)) - 1], 3)


def run_query(binary: Path, root: Path, value: str, timeout: float) -> dict:
    started = time.perf_counter()
    process = subprocess.Popen(
        [str(binary), "--root", str(root), "symbol", value],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Git enumeration is a child of the CLI; kill the whole group.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        return {"elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "outcome": "timeout"}
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    if process.returncode or len(stdout) > 128 * 1024 or len(stderr) > 128 * 1024:
        return {"elapsed_ms": elapsed_ms, "outcome": "error",
                "exit_code": process.returncode,
                "error": stderr.decode("utf-8", "replace")[:512]}
    try:
        answer = json.loads(stdout)
        if (answer["scope"] != str(root) or answer["query"] != "symbol"
                or answer["value"] != value):
            raise ValueError("reply does not match the requested worktree and query")
        coverage = answer["coverage"]
        return {"elapsed_ms": elapsed_ms, "outcome": "ok", "reply_bytes": len(stdout),
                "status": answer["status"], "enumerated_files": coverage["enumerated_files"],
                "parsed_rust_files": coverage["parsed_rust_files"],
                "parsed_python_files": coverage["parsed_python_files"],
                "rust_skipped_files": coverage["rust_skipped_files"],
                "rust_unparseable_files": coverage["rust_unparseable_files"],
                "skipped_files": coverage["skipped"]["count"],
                "unparseable_files": coverage["unparseable"]["count"]}
    except (KeyError, TypeError, ValueError) as error:
        return {"elapsed_ms": elapsed_ms, "outcome": "invalid_reply",
                "error": str(error)[:512]}


def git_head(root: Path) -> str:
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                            check=True, capture_output=True, text=True, timeout=5)
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--root", required=True, action="append", type=Path,
                        help="Git worktree to scan; repeat for up to four repositories")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=15)
    parser.add_argument("--budget-seconds", type=float, default=180)
    parser.add_argument("--symbol", default="__doxa_codegraph_benchmark_no_hit__")
    args = parser.parse_args()
    if not 1 <= len(args.root) <= 4 or not 1 <= args.runs <= 10 or not 0 <= args.warmups <= 2:
        parser.error("requires 1-4 roots, 1-10 runs, and 0-2 warmups")
    if not 0 < args.timeout_seconds <= 60 or not 0 < args.budget_seconds <= 600:
        parser.error("timeout must be at most 60 seconds and budget at most 600 seconds")
    if not args.symbol or len(args.symbol) > 128 or any(ord(c) < 32 for c in args.symbol):
        parser.error("symbol must be a printable, nonempty string up to 128 characters")
    if not os.environ.get("TMPDIR") or not Path(os.environ["TMPDIR"]).is_dir():
        parser.error("set TMPDIR to an existing real-disk directory before running")
    temp_root = Path(os.environ["TMPDIR"]).resolve()
    if temp_root == Path("/tmp") or Path("/tmp") in temp_root.parents:
        parser.error("TMPDIR must be outside /tmp")
    binary = args.binary.resolve(strict=True)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        parser.error("--binary must name an executable file")
    roots = [root.resolve(strict=True) for root in args.root]
    if len(set(roots)) != len(roots):
        parser.error("duplicate --root")
    deadline = time.perf_counter() + args.budget_seconds
    report = {"harness": "scripts/bench_codegraph_scan.py", "binary": str(binary),
              "platform": platform.platform(), "python": platform.python_version(),
              "query": ["symbol", args.symbol], "runs": args.runs,
              "warmups": args.warmups, "timeout_seconds": args.timeout_seconds,
              "budget_seconds": args.budget_seconds, "repositories": []}
    for root in roots:
        entry = {"root": str(root), "head": git_head(root), "warmups": [], "samples": []}
        report["repositories"].append(entry)
        for phase, count in (("warmups", args.warmups), ("samples", args.runs)):
            for _ in range(count):
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    entry[phase].append({"outcome": "budget_exhausted"})
                    break
                entry[phase].append(run_query(binary, root, args.symbol,
                                              min(args.timeout_seconds, remaining)))
        times = [sample["elapsed_ms"] for sample in entry["samples"]
                 if sample["outcome"] == "ok"]
        if times:
            entry["summary_ms"] = {"n": len(times), "p50": round(statistics.median(times), 3),
                                    "p95": percentile(times, 0.95), "min": min(times),
                                    "max": max(times)}
    print(json.dumps(report, indent=2))
    return 0 if all(len(entry["samples"]) == args.runs and
                    len(entry["warmups"]) == args.warmups and
                    all(sample["outcome"] == "ok" for phase in ("warmups", "samples")
                        for sample in entry[phase])
                    for entry in report["repositories"]) else 1


if __name__ == "__main__":
    sys.exit(main())
