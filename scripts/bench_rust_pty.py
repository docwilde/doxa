#!/usr/bin/env python3
"""Measure production Rust Crossterm loop through a PTY with fixture frames.

This measures process startup, keyboard scroll, and terminal resizing with
terminal escape output. It is not the Textual sidebar-width fixture.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import pty
import select
import signal
import socket
import statistics
import struct
import subprocess
import tempfile
import termios
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "rust/doxa-tui/target/release/bench_pty_frames"


def stats(values: list[float]) -> dict[str, float | int]:
    values = sorted(values)
    return {
        "n": len(values),
        "median_ms": round(statistics.median(values), 4),
        "p95_ms": round(values[min(len(values) - 1, int(len(values) * 0.95))], 4),
        "min_ms": round(values[0], 4),
        "max_ms": round(values[-1], 4),
    }


def winsize(fd: int, width: int, height: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))


def read_draw(fd: int, started: float, timeout: float = 2.0) -> tuple[float, float, int]:
    first = None
    last = None
    size = 0
    deadline = started + timeout
    while time.perf_counter() < deadline:
        wait = min(0.012 if first is not None else 0.1, max(0.0, deadline - time.perf_counter()))
        if not select.select([fd], [], [], wait)[0]:
            if first is not None:
                break
            continue
        try:
            chunk = os.read(fd, 1 << 20)
        except OSError:
            break
        if not chunk:
            break
        now = time.perf_counter()
        if first is None:
            first = now
        last = now
        size += len(chunk)
    if first is None or last is None:
        raise RuntimeError("no terminal redraw observed before timeout")
    return (first - started) * 1000, (last - started) * 1000, size


def frame(sock: socket.socket, value: dict) -> None:
    sock.sendall(json.dumps(value, separators=(",", ":")).encode() + b"\n")


def run_once() -> dict:
    with tempfile.TemporaryDirectory(prefix="doxa-pty-bench-") as temp:
        path = str(Path(temp) / "frames.sock")
        master, slave = pty.openpty()
        winsize(slave, 160, 48)
        env = {**os.environ, "TERM": "xterm-256color", "DOXA_HOME": str(Path(temp) / "home")}
        started = time.perf_counter()
        proc = subprocess.Popen([str(BINARY), path], stdin=slave, stdout=slave, stderr=slave,
                                cwd=temp, env=env, start_new_session=True)
        os.close(slave)
        try:
            startup_first, startup_last, _ = read_draw(master, started)
            sock = socket.socket(socket.AF_UNIX)
            deadline = time.monotonic() + 2
            while True:
                try:
                    sock.connect(path)
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.005)
            with sock:
                frame(sock, {"type": "hello", "session_id": "bench-one", "model": "fixture", "cwd": temp})
                frame(sock, {"type": "event", "session_id": "bench-one", "event": {"type": "text_delta", "data": {
                    "text": "".join(f"- transcript line {i:03}: reproducible text\n" for i in range(160))}}})
                read_draw(master, time.perf_counter())
                append_first, append_last, append_bytes = [], [], []
                for i in range(40):
                    chunk = f"delta {i:03}: {'x' * 28}\n"
                    # Vary phase relative to the 50 ms input poll rather than
                    # always sending immediately after the previous redraw.
                    time.sleep((i % 5) * 0.01)
                    sent = time.perf_counter()
                    frame(sock, {"type": "event", "session_id": "bench-one", "event": {
                        "type": "text_delta", "data": {"text": chunk}}})
                    first, last, size = read_draw(master, sent)
                    append_first.append(first)
                    append_last.append(last)
                    append_bytes.append(size)
                os.write(master, b"\t")  # Prompt -> transcript focus.
                read_draw(master, time.perf_counter())

                scroll_first, scroll_last, scroll_bytes = [], [], []
                for _ in range(40):
                    sent = time.perf_counter()
                    os.write(master, b"\x1b[A")
                    first, last, size = read_draw(master, sent)
                    scroll_first.append(first)
                    scroll_last.append(last)
                    scroll_bytes.append(size)

                resize_first, resize_last, resize_bytes = [], [], []
                for i in range(20):
                    width = 150 if i % 2 == 0 else 160
                    changed = time.perf_counter()
                    winsize(master, width, 48)
                    os.killpg(proc.pid, signal.SIGWINCH)
                    first, last, size = read_draw(master, changed)
                    resize_first.append(first)
                    resize_last.append(last)
                    resize_bytes.append(size)
            os.write(master, b"\x11")  # Ctrl+Q
            proc.wait(timeout=3)
            if proc.returncode:
                raise RuntimeError(f"TUI exited {proc.returncode}")
            return {
                "startup_first_byte_ms": startup_first,
                "startup_frame_bytes_written_ms": startup_last,
                "append_first_byte_ms": append_first,
                "append_last_byte_ms": append_last,
                "append_output_bytes": append_bytes,
                "scroll_first_byte_ms": scroll_first,
                "scroll_last_byte_ms": scroll_last,
                "scroll_output_bytes": scroll_bytes,
                "resize_first_byte_ms": resize_first,
                "resize_last_byte_ms": resize_last,
                "resize_output_bytes": resize_bytes,
            }
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            os.close(master)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    raw = [run_once() for _ in range(args.runs)]
    result = {key: stats([value for run in raw for value in run[key]])
              for key in ("append_first_byte_ms", "append_last_byte_ms",
                          "scroll_first_byte_ms", "scroll_last_byte_ms",
                          "resize_first_byte_ms", "resize_last_byte_ms")}
    result["startup_first_byte_ms"] = stats([run["startup_first_byte_ms"] for run in raw])
    result["startup_frame_bytes_written_ms"] = stats([run["startup_frame_bytes_written_ms"] for run in raw])
    result["output_bytes"] = {}
    for series in ("append", "scroll", "resize"):
        byte_stats = stats([float(value) for run in raw for value in run[f"{series}_output_bytes"]])
        result["output_bytes"][series] = {key.replace("_ms", "_bytes"): value for key, value in byte_stats.items()}
    print(json.dumps({"runs": args.runs, "size": [160, 48], "appends_per_run": 40, "scrolls_per_run": 40,
                      "terminal_resizes_per_run": 20, "results": result}, indent=2))


if __name__ == "__main__":
    main()
