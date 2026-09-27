# SPDX-License-Identifier: AGPL-3.0-only
"""Host-only native detached launcher used by Claude's existing spawn gate."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import time
import uuid


class NativeSpawnConfigurationError(ValueError):
    """A fixed startup category; raw filesystem diagnostics stay private."""


def _protected_python(path: Path) -> bool:
    """uv interpreters can be 0775 inside an owned, private installation tree.

    A 0700 ancestor prevents other users, including group members, from
    reaching or replacing the interpreter. World-writable files remain refused.
    """
    meta = path.stat()
    if meta.st_uid != os.geteuid() or meta.st_mode & 0o002:
        return False
    protected = False
    # Check the route from the filesystem root. A private leaf underneath an
    # unprotected writable parent can itself be renamed and replaced.
    for parent in reversed(path.parents):
        entry = parent.stat()
        if not stat.S_ISDIR(entry.st_mode):
            return False
        if not protected and (entry.st_uid not in (0, os.geteuid()) or entry.st_mode & 0o022):
            return False
        if entry.st_uid == os.geteuid() and stat.S_ISDIR(entry.st_mode) and not entry.st_mode & 0o077:
            protected = True
    return protected


def native_launcher(config: dict):
    """Freeze daemon-selected executables; tool arguments cannot select a route."""
    if not isinstance(config, dict):
        raise ValueError("invalid native spawn configuration")
    paths = {}
    for key in ("daemon_bin", "python", "script", "runtime"):
        value = config.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise NativeSpawnConfigurationError("native spawn paths must be absolute")
        path = Path(value).resolve(strict=True)
        meta = path.stat()
        if key == "runtime":
            if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.geteuid() or meta.st_mode & 0o077:
                raise NativeSpawnConfigurationError("unsafe native runtime")
        elif (not stat.S_ISREG(meta.st_mode)
              or (meta.st_mode & 0o022 and not (key == "python" and _protected_python(path)))
              or (key != "script" and not os.access(path, os.X_OK))):
            raise NativeSpawnConfigurationError("unsafe native spawn executable")
        paths[key] = value if key == "python" else str(path)

    def launch(cwd: str, *, model=None, base_branch=None, spawn_depth=0,
               parent_session_id=None, task=None):
        from doxa.identity import require_session_id

        if not isinstance(spawn_depth, int) or isinstance(spawn_depth, bool) or not 1 <= spawn_depth <= 2:
            raise ValueError("invalid native child depth")
        parent = require_session_id(parent_session_id, "parent session id")
        if not isinstance(task, str) or not task.strip() or len(task) > 2000 or "\0" in task:
            raise ValueError("invalid native child task")
        child_cwd = Path(cwd).resolve(strict=True)
        if not child_cwd.is_dir():
            raise ValueError("invalid native child directory")
        for value in (model, base_branch):
            if value is not None and (not isinstance(value, str) or not value or value.startswith("-") or any(ord(ch) < 32 for ch in value)):
                raise ValueError("invalid native child selection")
        sid = str(uuid.uuid4())
        runtime = Path(paths["runtime"])
        registry_meta = runtime.joinpath("registry").lstat()
        if not stat.S_ISDIR(registry_meta.st_mode) or registry_meta.st_uid != os.geteuid() or registry_meta.st_mode & 0o077:
            raise ValueError("unsafe native registry directory")
        command = [paths["daemon_bin"], "--engine", "claude", "--runtime-dir", str(runtime),
                   "--cwd", str(child_cwd), "--session-id", sid, "--linger", "120",
                   "--claude-python", paths["python"], "--claude-script", paths["script"],
                   "--spawn-depth", str(spawn_depth), "--parent-session-id", parent, "--task", task]
        if model:
            command.extend(("--model", model))
        if base_branch:
            command.extend(("--base-branch", base_branch))
        log_path = runtime / f"daemon-{sid[:8]}.log"
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        child_env = os.environ.copy()
        child_env["DOXA_RUNTIME_DIR"] = str(runtime)
        with os.fdopen(fd, "ab") as log:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     cwd=str(child_cwd), start_new_session=True, env=child_env)
        expected_socket = runtime / f"daemon-{sid[:8]}-{child.pid}.sock"
        registry = runtime / "registry" / f"{sid}.json"
        deadline = time.monotonic() + 30
        try:
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise RuntimeError("native child exited during startup")
                try:
                    fd = os.open(registry, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, "rb") as entry_file:
                        meta = os.fstat(entry_file.fileno())
                        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.geteuid() or meta.st_mode & 0o077 or meta.st_size > 65536:
                            raise RuntimeError("unsafe native child registry")
                        entry = json.loads(entry_file.read(65537))
                    socket_meta = expected_socket.lstat()
                    if (entry.get("session_id") == sid and entry.get("pid") == child.pid
                            and entry.get("parent_session_id") == parent
                            and entry.get("daemon_socket") == str(expected_socket)
                            and stat.S_ISSOCK(socket_meta.st_mode) and socket_meta.st_uid == os.geteuid()
                            and not socket_meta.st_mode & 0o077):
                        return sid, str(expected_socket)
                except (OSError, ValueError):
                    pass
                time.sleep(0.05)
            raise RuntimeError("native child did not register within 30 seconds")
        except BaseException:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
            raise

    return launch
