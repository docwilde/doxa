# SPDX-License-Identifier: AGPL-3.0-only
"""Own one review process group for exactly the lifetime of a control pipe.

This supervisor is separate from the SDK sidecar's process group. EOF on
stdin survives abrupt parent death, so killing the sidecar cannot orphan a
detached review. Keep the child unreaped until group cleanup prevents PID reuse.
"""
from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import time


def supervise(jobfile: str, lore_parent: str, timeout: float = 180.0) -> int:
    if not all(hasattr(os, name) for name in ("waitid", "WNOWAIT", "WEXITED", "WNOHANG")):
        return 1  # Never launch an unowned review on an unsupported platform.
    control = sys.stdin.fileno()
    os.set_blocking(control, False)

    def cancelled() -> bool:
        return bool(select.select([control], [], [], 0)[0])

    if cancelled():
        return 1
    child = subprocess.Popen(
        [sys.executable, "-I", "-c",
         "import sys; from pathlib import Path; "
         "sys.path.insert(0, sys.argv[2]); "
         "from lore_core.deriver import worker_run; "
         "sys.exit(worker_run(Path(sys.argv[1])))", jobfile, lore_parent],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )
    result = 1
    deadline = time.monotonic() + timeout
    try:
        while not cancelled() and time.monotonic() < deadline:
            status = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if status is not None:
                result = int(not (status.si_code == os.CLD_EXITED and status.si_status == 0))
                break
            select.select([control], [], [], min(0.05, max(0, deadline - time.monotonic())))
    finally:
        # waitid(WNOWAIT) retains the owned group leader even on successful
        # exit. Also stop descendants left behind by a completed reviewer.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            result = 1
    return result


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(1)
    raise SystemExit(supervise(sys.argv[1], sys.argv[2]))
