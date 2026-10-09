#!/usr/bin/env python3
"""Bounded end-to-end benchmark of doxa-codegraph source scans.

Build the production binary first, then pass it with --binary. A no-hit symbol
query parses every listed Rust/Python source without depending on a known file.
Use a trusted binary. The harness creates no temporary files or repository
writes, and disables Git fsmonitor hooks and optional index locks in children.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import selectors
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path


def percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    return round(ordered[math.ceil(fraction * len(ordered)) - 1], 3)


def git_safe_env() -> dict[str, str]:
    git_route = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                 "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                 "GIT_NAMESPACE", "GIT_PREFIX"}
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("GIT_CONFIG_", "GIT_TRACE"))
           and key != "GIT_CONFIG_PARAMETERS" and key not in git_route}
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat",
                "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": "false"})
    return env


def kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def bounded_command(argv: list[str], timeout: float, stdout_cap: int = 128 * 1024,
                    stderr_cap: int = 128 * 1024) -> dict:
    """Capture at most the configured bytes per pipe, including for a noisy child."""
    started = time.perf_counter()
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=git_safe_env(),
        start_new_session=True,
    )
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {"stdout": stdout_cap, "stderr": stderr_cap}
    outcome = "ok"
    try:
        with selectors.DefaultSelector() as selector:
            for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            while selector.get_map():
                remaining = started + timeout - time.perf_counter()
                if remaining <= 0:
                    outcome = "timeout"
                    break
                events = selector.select(remaining)
                if not events:
                    outcome = "timeout"
                    break
                for key, _ in events:
                    name = key.data
                    try:
                        chunk = os.read(key.fileobj.fileno(),
                                        min(65536, limits[name] - len(buffers[name]) + 1))
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    buffers[name].extend(chunk)
                    if len(buffers[name]) > limits[name]:
                        outcome = f"{name}_limit"
                        break
                if outcome != "ok":
                    break
        if outcome == "ok":
            remaining = started + timeout - time.perf_counter()
            try:
                process.wait(timeout=max(0, remaining))
            except subprocess.TimeoutExpired:
                outcome = "timeout"
        if outcome != "ok":
            kill_group(process)
    finally:
        process.stdout.close()
        process.stderr.close()
        if process.poll() is None:
            kill_group(process)
    return {"outcome": outcome, "stdout": bytes(buffers["stdout"]),
            "stderr": bytes(buffers["stderr"]), "exit_code": process.returncode,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}


def run_query(binary: Path, root: Path, value: str, timeout: float) -> dict:
    result = bounded_command([str(binary), "--root", str(root), "symbol", value], timeout)
    stdout, stderr = result["stdout"], result["stderr"]
    elapsed_ms = result["elapsed_ms"]
    if result["outcome"] != "ok":
        return {"elapsed_ms": elapsed_ms, "outcome": result["outcome"]}
    if result["exit_code"]:
        return {"elapsed_ms": elapsed_ms, "outcome": "error",
                "exit_code": result["exit_code"],
                "error": stderr.decode("utf-8", "replace")[:512]}
    try:
        answer = json.loads(stdout)
        if (answer["scope"] != str(root) or answer["query"] != "symbol"
                or answer["value"] != value):
            raise ValueError("reply does not match the requested worktree and query")
        if answer["rows"] or answer["omitted_rows"]:
            raise ValueError("benchmark sentinel has a symbol hit")
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


def git_metadata(root: Path, args: list[str], deadline: float) -> bytes:
    remaining = deadline - time.perf_counter()
    if remaining <= 0:
        raise RuntimeError("overall budget exhausted before Git metadata")
    result = bounded_command(["git", "-C", str(root), *args], min(5, remaining),
                             stdout_cap=64 * 1024, stderr_cap=4 * 1024)
    if result["outcome"] != "ok" or result["exit_code"]:
        raise RuntimeError(f"Git metadata failed: {result['outcome']} exit={result['exit_code']}")
    return result["stdout"]


def status_evidence(root: Path, deadline: float) -> dict:
    status = git_metadata(root, ["status", "--porcelain=v1", "--untracked-files=all", "-z"],
                          deadline)
    return {"dirty": bool(status), "entries": status.count(b"\0"), "bytes": len(status),
            "sha256": hashlib.sha256(status).hexdigest()}


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
        entry = {"root": str(root), "warmups": [], "samples": []}
        report["repositories"].append(entry)
        try:
            entry["head"] = git_metadata(root, ["rev-parse", "HEAD"], deadline).decode().strip()
            entry["status_before"] = status_evidence(root, deadline)
        except (RuntimeError, UnicodeError) as error:
            entry["metadata_error"] = str(error)
            break
        for phase, count in (("warmups", args.warmups), ("samples", args.runs)):
            for _ in range(count):
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    entry[phase].append({"outcome": "budget_exhausted"})
                    break
                entry[phase].append(run_query(binary, root, args.symbol,
                                              min(args.timeout_seconds, remaining)))
        try:
            entry["status_after"] = status_evidence(root, deadline)
            entry["worktree_status_changed"] = entry["status_before"] != entry["status_after"]
        except RuntimeError as error:
            entry["metadata_error"] = str(error)
        times = [sample["elapsed_ms"] for sample in entry["samples"]
                 if sample["outcome"] == "ok"]
        if times:
            entry["summary_ms"] = {"n": len(times), "p50": round(statistics.median(times), 3),
                                    "p95": percentile(times, 0.95), "min": min(times),
                                    "max": max(times)}
    print(json.dumps(report, indent=2))
    return 0 if len(report["repositories"]) == len(roots) and all(
                    "metadata_error" not in entry and not entry["worktree_status_changed"] and
                    len(entry["samples"]) == args.runs and
                    len(entry["warmups"]) == args.warmups and
                    all(sample["outcome"] == "ok" for phase in ("warmups", "samples")
                        for sample in entry[phase])
                    for entry in report["repositories"]) else 1


if __name__ == "__main__":
    sys.exit(main())
