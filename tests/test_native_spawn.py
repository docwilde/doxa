# SPDX-License-Identifier: AGPL-3.0-only
"""Native child launch is provider-free here; only a host-chosen route can run."""
import json
import os
from pathlib import Path
import socket

import pytest

from doxa.native_spawn import native_launcher


def config(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    (runtime / "registry").mkdir(mode=0o700)
    executable = tmp_path / "daemon"
    executable.write_text("#!/bin/sh\nexit 1\n")
    executable.chmod(0o700)
    script = tmp_path / "sidecar.py"
    script.write_text("# fixture\n")
    script.chmod(0o600)
    return {"daemon_bin": str(executable), "python": str(executable),
            "script": str(script), "runtime": str(runtime)}


def test_native_route_carries_exact_depth_parent_task_and_runtime(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    seen = {}
    listener = None

    class Child:
        pid = os.getpid()

        def __init__(self, command, **kwargs):
            nonlocal listener
            seen.update(command=command, kwargs=kwargs)
            sid = command[command.index("--session-id") + 1]
            runtime = Path(cfg["runtime"])
            path = runtime / f"daemon-{sid[:8]}-{self.pid}.sock"
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(str(path))
            path.chmod(0o600)
            registry = runtime / "registry" / f"{sid}.json"
            registry.write_text(json.dumps({"session_id": sid, "pid": self.pid,
                "daemon_socket": str(path), "parent_session_id": "parent-123"}))
            registry.chmod(0o600)

        def poll(self):
            return None

    monkeypatch.setattr("doxa.native_spawn.subprocess.Popen", Child)
    launch = native_launcher(cfg)
    sid, path = launch(str(tmp_path), model="sonnet", base_branch="main",
                      spawn_depth=2, parent_session_id="parent-123", task="do the work")
    try:
        command = seen["command"]
        assert command[0] == cfg["daemon_bin"]
        for key, value in {"--engine": "claude", "--spawn-depth": "2",
            "--parent-session-id": "parent-123", "--task": "do the work",
            "--runtime-dir": cfg["runtime"], "--model": "sonnet", "--base-branch": "main"}.items():
            assert command[command.index(key) + 1] == value
        assert seen["kwargs"]["env"]["DOXA_RUNTIME_DIR"] == cfg["runtime"]
        assert seen["kwargs"]["start_new_session"] is True
        assert sid in Path(cfg["runtime"]).joinpath("registry", f"{sid}.json").read_text()
        assert Path(path).exists()
    finally:
        listener.close()


def test_native_child_route_rejects_invalid_depth_parent_and_task_before_spawn(tmp_path, monkeypatch):
    launch = native_launcher(config(tmp_path))
    monkeypatch.setattr("doxa.native_spawn.subprocess.Popen", lambda *a, **k: pytest.fail("must not spawn"))
    for fields in ({"spawn_depth": 0}, {"spawn_depth": 3}, {"spawn_depth": True},
                   {"parent_session_id": "../bad"}, {"task": ""}, {"model": "--engine"}):
        args = dict(spawn_depth=1, parent_session_id="parent", task="work")
        args.update(fields)
        with pytest.raises((ValueError, TypeError)):
            launch(str(tmp_path), **args)


def test_native_host_config_rejects_untrusted_runtime_and_executable(tmp_path):
    cfg = config(tmp_path)
    Path(cfg["runtime"]).chmod(0o755)
    with pytest.raises(ValueError):
        native_launcher(cfg)
    Path(cfg["runtime"]).chmod(0o700)
    Path(cfg["daemon_bin"]).chmod(0o777)
    with pytest.raises(ValueError):
        native_launcher(cfg)


def test_native_startup_effort_is_an_explicit_engine_option(tmp_path, monkeypatch):
    from doxa.engine import SessionEngine
    from doxa import config as config_mod
    monkeypatch.setenv("DOXA_EFFORT", "low")
    config_mod.invalidate()
    engine = SessionEngine(cwd=str(tmp_path), session_id="effort-fixture", effort="max")
    assert engine._effort_override == "max"
    options = engine._build_options()
    assert options.effort == "max"
    assert engine.effort == "max"
    with pytest.raises(ValueError, match="unsupported Claude effort"):
        SessionEngine(cwd=str(tmp_path), session_id="bad-effort", effort="unrecognized")
    config_mod.invalidate()
