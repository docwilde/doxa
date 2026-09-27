# SPDX-License-Identifier: AGPL-3.0-only
"""Own the native review worker process group for a parent control pipe.

The native worker builds and verifies its review job, runs the provider and
stages results. This retained SDK supervisor carries only bounded transcript
metadata; it never loads a Python memory backend or serializes authority.
"""
from __future__ import annotations

import json
import os
import select
import shutil
import signal
import subprocess
import sys
import time

MAX_METADATA_BYTES = 16 * 1024
ENGINES = frozenset(("claude", "codex", "deepseek", "glm"))


def supervise(metadata: dict, engine: str, timeout: float = 180.0) -> int:
    if (engine not in ENGINES or not isinstance(metadata, dict)
            or not 0 < timeout <= 180
            or not all(hasattr(os, name) for name in ("waitid", "WNOWAIT", "WEXITED", "WNOHANG"))):
        return 1
    try:
        raw = (json.dumps(metadata, ensure_ascii=False, allow_nan=False) + "\n").encode()
    except (ValueError, TypeError, RecursionError):
        return 1
    if len(raw) > MAX_METADATA_BYTES:
        return 1
    binary = os.environ.get("DOXA_LORE_RS", "").strip() or shutil.which("lore-rs")
    if not binary:
        return 1
    control = sys.stdin.fileno()
    os.set_blocking(control, False)

    def cancelled() -> bool:
        return bool(select.select([control], [], [], 0)[0])

    if cancelled():
        return 1
    child = subprocess.Popen([binary, "review-worker", "--engine", engine],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True)
    result = 1
    deadline = time.monotonic() + timeout
    try:
        # A blocked/malformed worker cannot stall the parent-EOF supervisor.
        os.set_blocking(child.stdin.fileno(), False)
        offset = 0
        while offset < len(raw) and not cancelled() and time.monotonic() < deadline:
            readers, writers, _ = select.select([control], [child.stdin], [],
                min(0.05, max(0, deadline-time.monotonic())))
            if readers:
                break
            if writers:
                try:
                    count = os.write(child.stdin.fileno(), raw[offset:])
                except BlockingIOError:
                    continue
                if not count:
                    break
                offset += count
        child.stdin.close()
        if offset != len(raw):
            return 1
        while not cancelled() and time.monotonic() < deadline:
            status = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if status is not None:
                result = int(not (status.si_code == os.CLD_EXITED and status.si_status == 0))
                break
            select.select([control], [], [], min(0.05, max(0, deadline-time.monotonic())))
    except (OSError, ValueError):
        result = 1
    finally:
        # Keep the leader unreaped until all its owned descendants are stopped.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if not child.stdin.closed:
            child.stdin.close()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            result = 1
    return result


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(1)
    try:
        if len(sys.argv[2].encode()) > MAX_METADATA_BYTES:
            raise ValueError()
        metadata = json.loads(sys.argv[2])
        raise SystemExit(supervise(metadata, sys.argv[1]))
    except (ValueError, OSError):
        raise SystemExit(1)
