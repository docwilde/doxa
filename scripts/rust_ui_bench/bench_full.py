#!/usr/bin/env python3
"""Measure process launch + initial draw for the full-screen Ratatui model."""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import time
from pathlib import Path


def stats(samples_ns: list[int]) -> dict[str, float | int]:
    values = sorted(value / 1_000_000 for value in samples_ns)
    n = len(values)
    return {
        "n": n,
        "p50_ms": round(statistics.median(values), 4),
        "p95_ms": round(values[min(n - 1, int(n * 0.95))], 4),
        "min_ms": round(values[0], 4),
        "max_ms": round(values[-1], 4),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=10)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    binary = Path(__file__).parent / "target/release/full_ui"
    samples = []
    for _ in range(args.runs):
        started = time.perf_counter_ns()
        subprocess.run([binary, "--startup-only"], check=True, stdout=subprocess.DEVNULL)
        samples.append(time.perf_counter_ns() - started)
    print(json.dumps({"scope": "subprocess launch + one TestBackend draw", "startup": stats(samples)}, indent=2))


if __name__ == "__main__":
    main()
