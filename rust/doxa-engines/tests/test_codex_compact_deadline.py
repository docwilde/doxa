"""Whole hook deadline tests; no providers or real memory workers."""
import importlib.util
from pathlib import Path
import sys
import types
import pytest
from unittest import mock

PATH = Path(__file__).parents[1] / "codex_compact_hook.py"
spec = importlib.util.spec_from_file_location("codex_compact_deadline_hook", PATH)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


def test_whole_review_deadline_interrupt_reaps_worker_before_blocking(monkeypatch):
    class Process:
        pid = 456
        def __init__(self):
            self.waits = 0
            self.stdin = mock.Mock()
        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1: raise TimeoutError("review deadline")
            return -9
    process = Process(); killed = []
    monkeypatch.setattr(hook.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(hook.os, "killpg", lambda *a: killed.append(a))
    monkeypatch.setattr(hook, "REVIEW_SUPERVISOR_SOURCE", "verified source", raising=False)
    with pytest.raises(TimeoutError):
        hook.run_worker(Path("fixture"), Path("fixture"))
    assert killed == []
    process.stdin.close.assert_called_once()
    assert process.waits == 2


def test_main_bounds_imports_and_job_construction_and_restores_alarm(monkeypatch):
    import io
    timers = []; handlers = []
    monkeypatch.setattr(hook.signal, "signal", lambda *a: handlers.append(a) or "old-handler")
    monkeypatch.setattr(hook.signal, "setitimer", lambda *a: timers.append(a))
    monkeypatch.setattr(hook.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"{}")))
    monkeypatch.setattr(hook.sys, "argv", ["hook", "fixture"])
    monkeypatch.setattr(hook, "review", lambda *a: (_ for _ in ()).throw(TimeoutError("build deadline")))
    result = hook.main()
    assert result["continue"] is False
    assert timers == [(hook.signal.ITIMER_REAL, 210), (hook.signal.ITIMER_REAL, 0)]
    assert handlers[-1] == (hook.signal.SIGALRM, "old-handler")
