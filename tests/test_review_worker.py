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
    binary = tmp_path / "lore-rs"
    binary.write_text(f"#!{sys.executable}\n" + '''import json, os, subprocess, sys, time
from pathlib import Path
assert sys.argv[1:] == ['review-worker', '--engine', 'claude']
metadata = json.loads(sys.stdin.readline())
assert metadata['session_id'] == 'owned'
assert 'agent' not in metadata and 'source_engine' not in metadata
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
root = Path(metadata['cwd'])
(root / 'ready').write_text(json.dumps([os.getpid(), child.pid]))
while not (root / 'release').exists(): time.sleep(.01)
''')
    binary.chmod(0o700)
    return {"cwd":str(tmp_path), "session_id":"owned", "transcript":str(tmp_path / "owned.jsonl"), "older":True}


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
def test_review_worker_and_provider_end_with_owner_or_success(tmp_path, monkeypatch, abrupt_parent_death):
    metadata = fixture(tmp_path)
    monkeypatch.setenv("DOXA_LORE_RS", str(tmp_path / "lore-rs"))
    # This parent holds the supervisor's private control writer. SIGKILL
    # closes it without running Python finally/atexit, like a killed sidecar.
    script = '''
import subprocess, sys
p = subprocess.Popen([sys.executable, '-I', sys.argv[1], 'claude', sys.argv[2]],
    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    start_new_session=True)
sys.exit(p.wait())
'''
    parent = subprocess.Popen([sys.executable, "-I", "-c", script, str(SUPERVISOR), json.dumps(metadata)])
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


def test_review_supervisor_bounds_blocked_metadata_writer(tmp_path, monkeypatch):
    binary = tmp_path / "lore-rs"
    binary.write_text(f"#!{sys.executable}\n" + '''import os, sys, time
from pathlib import Path
Path(os.environ['OWNED_READY']).write_text(str(os.getpid()))
time.sleep(30)
''')
    binary.chmod(0o700)
    monkeypatch.setenv("DOXA_LORE_RS", str(binary))
    monkeypatch.setenv("OWNED_READY", str(tmp_path / "ready"))
    code = '''import json, sys
from doxa.review_worker import supervise
sys.exit(supervise(json.loads(sys.argv[1]), 'claude', timeout=.15))
'''
    metadata = {"cwd":str(tmp_path), "session_id":"owned", "transcript":"x" * 12000}
    started = time.monotonic()
    process = subprocess.Popen([sys.executable, "-c", code, json.dumps(metadata)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    handle = None
    try:
        deadline = time.monotonic()+2
        while not (tmp_path / "ready").exists() and time.monotonic()<deadline:
            time.sleep(.005)
        handle = pidfd_open(int((tmp_path / "ready").read_text()))
        assert process.wait(timeout=2) == 1
        assert time.monotonic()-started < 2
        assert select.select([handle], [], [], .5)[0]
    finally:
        process.stdin.close()
        if process.poll() is None:
            process.kill(); process.wait()
        if handle is not None:
            if not select.select([handle], [], [], 0)[0]:
                kill_handle(handle)
            os.close(handle)
