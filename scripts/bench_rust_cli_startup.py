#!/usr/bin/env python3
"""Time a fresh doxa-rs --demo process to its first visible PTY text.

Build first: cargo build --release --bin doxa-rs --manifest-path rust/doxa-tui/Cargo.toml
This uses the real CLI and UI, but --demo deliberately skips discovery and
provider/daemon startup. Each sample starts a new process and empty DOXA_HOME.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import pty
import select
import signal
import statistics
import struct
import subprocess
import tempfile
import termios
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "rust/doxa-tui/target/release/doxa-rs"
VISIBLE_MARKER = b"Sessions"


def summary(samples: list[float]) -> dict[str, float | int]:
    ordered = sorted(samples)
    return {
        "n": len(samples),
        "p50_ms": round(statistics.median(ordered), 4),
        "p95_ms": round(ordered[math.ceil(0.95 * len(ordered)) - 1], 4),
        "min_ms": round(ordered[0], 4),
        "max_ms": round(ordered[-1], 4),
    }


def sample(width: int, height: int, timeout: float) -> dict[str, float]:
    with tempfile.TemporaryDirectory(prefix="doxa-cli-startup-") as temp:
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))
        env = {**os.environ, "TERM": "xterm-256color", "DOXA_HOME": str(Path(temp) / "home")}
        started = time.perf_counter()
        proc = subprocess.Popen(
            [str(BINARY), "--demo"], stdin=slave, stdout=slave, stderr=slave,
            cwd=temp, env=env, start_new_session=True,
        )
        os.close(slave)
        first_byte = None
        first_visible = None
        pending = b""
        try:
            deadline = started + timeout
            while first_visible is None:
                remaining = deadline - time.perf_counter()
                if remaining <= 0 or not select.select([master], [], [], remaining)[0]:
                    raise TimeoutError("no visible terminal text before timeout")
                try:
                    chunk = os.read(master, 65536)
                except OSError as error:
                    raise RuntimeError("PTY closed before visible text") from error
                if not chunk:
                    raise RuntimeError("process exited before visible text")
                received = time.perf_counter()
                if first_byte is None:
                    first_byte = received
                if VISIBLE_MARKER in pending + chunk:
                    first_visible = received
                pending = (pending + chunk)[-len(VISIBLE_MARKER):]
            os.write(master, b"\x11")  # Ctrl+Q detaches the demo UI.
            proc.wait(timeout=3)
            if proc.returncode:
                raise RuntimeError(f"doxa-rs exited {proc.returncode}")
            return {
                "first_byte_ms": (first_byte - started) * 1000,
                "first_visible_ms": (first_visible - started) * 1000,
            }
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            os.close(master)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=50)
    parser.add_argument("--timeout", type=float, default=2.0)
    args = parser.parse_args()
    if args.runs < 1 or args.timeout <= 0:
        parser.error("--runs and --timeout must be positive")
    if not BINARY.is_file():
        parser.error(f"release binary missing: {BINARY}")
    result = {}
    for width, height in ((160, 48), (80, 24)):
        samples = [sample(width, height, args.timeout) for _ in range(args.runs)]
        result[f"{width}x{height}"] = {
            metric: summary([row[metric] for row in samples])
            for metric in ("first_byte_ms", "first_visible_ms")
        }
    print(json.dumps({"binary": str(BINARY), "mode": "--demo", "runs_per_size": args.runs,
                      "results": result}, indent=2))


if __name__ == "__main__":
    main()
