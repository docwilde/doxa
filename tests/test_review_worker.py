"""Owned fake reviewers; no SDK/provider requests or real memory writes."""
import json
import ctypes
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

import pytest


SUPERVISOR = Path(__file__).resolve().parents[1] / "doxa/review_worker.py"


def pidfd_open(pid):
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    # Some bundled Linux Python builds omit the binding. Use libc's stable
    # process handle instead of falling back to recyclable numeric PIDs.
    libc = ctypes.CDLL(None, use_errno=True)
    if not hasattr(libc, "pidfd_open"):
        pytest.skip("Linux stable process handles unavailable")
    handle = libc.pidfd_open(pid, 0)
    if handle < 0:
        raise OSError(ctypes.get_errno(), "pidfd_open")
    return handle


def kill_handle(handle):
    if hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal(handle, signal.SIGKILL)
    else:
        libc = ctypes.CDLL(None, use_errno=True)
        assert libc.pidfd_send_signal(handle, signal.SIGKILL, None, 0) == 0


def fixture(tmp_path):
    package = tmp_path / "lore_core"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "deriver.py").write_text('''
import json, os, subprocess, sys, time
def worker_run(path):
    job = json.loads(path.read_text())
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    from pathlib import Path
    Path(job['ready']).write_text(json.dumps([os.getpid(), child.pid]))
    while not Path(job['release']).exists(): time.sleep(.01)
    return 0
''')
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"ready": str(tmp_path / "ready"), "release": str(tmp_path / "release")}))
    return job


def wait_ready(tmp_path):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            return json.loads((tmp_path / "ready").read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(.01)
    raise AssertionError("owned reviewer did not start")


@pytest.mark.skipif(sys.platform != "linux", reason="Linux stable process handles")
@pytest.mark.parametrize("abrupt_parent_death", [False, True])
def test_review_worker_and_provider_end_with_owner_or_success(tmp_path, abrupt_parent_death):
    job = fixture(tmp_path)
    # This parent holds the supervisor's private control writer. SIGKILL
    # closes it without running Python finally/atexit, like a killed sidecar.
    script = '''
import subprocess, sys
p = subprocess.Popen([sys.executable, '-I', sys.argv[1], sys.argv[2], sys.argv[3]],
    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    start_new_session=True)
sys.exit(p.wait())
'''
    parent = subprocess.Popen([sys.executable, "-I", "-c", script, str(SUPERVISOR), str(job), str(tmp_path)])
    handles = []
    try:
        handles = [pidfd_open(pid) for pid in wait_ready(tmp_path)]
        if abrupt_parent_death:
            parent.kill()
        else:
            (tmp_path / "release").touch()
        parent.wait(timeout=5)
        if not abrupt_parent_death:
            assert parent.returncode == 0
        deadline = time.monotonic() + 3
        for handle in handles:
            assert select.select([handle], [], [], max(0, deadline - time.monotonic()))[0], "owned review descendant survived"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()
        for handle in handles:
            if not select.select([handle], [], [], 0)[0]:
                kill_handle(handle)
            os.close(handle)
