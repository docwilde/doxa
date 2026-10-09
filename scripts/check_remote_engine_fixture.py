#!/usr/bin/env python3
"""Evaluate private mock remote-Engine evidence without contacting Docker.

This is an operator design preflight, not a production admission path. Even a
complete fixture cannot authenticate daemon-host observations or enable remote
Docker Engine / Docker Desktop sessions.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import posixpath
import stat
import sys

MAX_FIXTURE_BYTES = 64 * 1024
MIB = 1024 * 1024
EXPECTED_MOUNTS = {
    "checkout": ("/workspace", True),
    "home": ("/home/doxa", True),
    "cache": ("/work-cache", True),
    "broker": ("/run/doxa/session", False),
}


def _positive_integer(value: object) -> bool:
    return type(value) is int and value > 0


def _private_posix_path(value: object) -> bool:
    return (isinstance(value, str) and value.startswith("/") and not value.startswith("//")
            and value == posixpath.normpath(value) and "\x00" not in value and "\n" not in value)


def _finite_int(value: object) -> int | None:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key in fixture")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise ValueError("nonfinite JSON value in fixture")


def evaluate(fixture: dict) -> dict:
    failures: list[str] = []
    if type(fixture) is not dict or fixture.get("version") != 1 or fixture.get("kind") not in ("remote-linux", "docker-desktop"):
        failures.append("unsupported fixture version or Engine kind")
        fixture = fixture if type(fixture) is dict else {}
    expected_keys = {"version", "kind", "session_id", "daemon_session_root", "daemon_uid",
                     "worker_uid_map_host_uid", "engine_info", "mounts", "broker", "requested", "effective"}
    if set(fixture) != expected_keys:
        failures.append("fixture has missing or unexpected top-level fields")
    session_id = fixture.get("session_id")
    root = fixture.get("daemon_session_root")
    if (not isinstance(session_id, str) or not session_id or len(session_id) > 128
            or not all(ch.isascii() and (ch.isalnum() or ch == "-") for ch in session_id)
            or not _private_posix_path(root) or posixpath.basename(root) != session_id):
        failures.append("daemon-host private session root or identity is invalid")

    info = fixture.get("engine_info")
    uid = fixture.get("daemon_uid")
    mapped_uid = fixture.get("worker_uid_map_host_uid")
    if (type(info) is not dict or info.get("SecurityOptions") is None
            or type(info.get("SecurityOptions")) is not list
            or "name=rootless" not in info["SecurityOptions"]
            or not _positive_integer(uid) or type(mapped_uid) is not int or mapped_uid != uid):
        failures.append("rootless daemon and worker-to-host UID identity are unproven")
    if (type(info) is not dict or info.get("CgroupVersion") != "2"
            or not isinstance(info.get("CgroupDriver"), str) or info.get("CgroupDriver") in ("", "none")
            or any(info.get(key) is not True for key in ("MemoryLimit", "CpuCfsQuota", "PidsLimit"))):
        failures.append("Engine cgroup v2 memory, CPU or PID capability is unproven")

    mounts = fixture.get("mounts")
    if not isinstance(mounts, list) or len(mounts) != 4 or not _private_posix_path(root):
        failures.append("four exact private bind mounts are unproven")
    else:
        seen: set[str] = set()
        for row in mounts:
            if type(row) is not dict or row.get("name") not in EXPECTED_MOUNTS:
                failures.append("unexpected bind mount")
                continue
            name = row["name"]
            target, writable = EXPECTED_MOUNTS[name]
            if name in seen:
                failures.append("duplicate bind mount")
            seen.add(name)
            if (row.get("source") != posixpath.join(root, name)
                    or row.get("target") != target or row.get("writable") is not writable
                    or type(row.get("owner_uid")) is not int or row.get("owner_uid") != uid or row.get("mode") != "0700"
                    or row.get("canonical") is not True or row.get("symlink_free") is not True):
                failures.append(f"{name} daemon-host mount ownership or mapping is unproven")
        if seen != set(EXPECTED_MOUNTS):
            failures.append("private bind mount set is incomplete")

    broker = fixture.get("broker")
    if (type(broker) is not dict or broker.get("transport") != "authenticated-session-forward"
            or broker.get("bound_session_id") != session_id or type(broker.get("owner_uid")) is not int
            or broker.get("owner_uid") != uid
            or broker.get("mode") != "0700" or broker.get("exclusive") is not True
            or broker.get("capability_bound") is not True
            or broker.get("worker_engine_socket_exposed") is not False):
        failures.append("private authenticated session broker transport is unproven")

    requested = fixture.get("requested")
    effective = fixture.get("effective")
    if type(requested) is not dict or type(effective) is not dict:
        failures.append("requested or effective resource evidence is missing")
    else:
        memory = requested.get("memory_bytes")
        cpus = requested.get("cpus")
        pids = requested.get("pids")
        valid_request = (_positive_integer(memory) and 128 * MIB <= memory <= 1024 * 1024 * MIB
                         and type(cpus) in (float, int) and math.isfinite(cpus) and 0.25 <= cpus <= 256
                         and _positive_integer(pids) and 16 <= pids <= 65536)
        if not valid_request:
            failures.append("requested memory, CPU or PID ceiling is invalid")
        else:
            memory_max = _finite_int(effective.get("memory_max"))
            swap_max = _finite_int(effective.get("memory_swap_max"))
            pids_max = _finite_int(effective.get("pids_max"))
            cpu = effective.get("cpu_max")
            parts = cpu.split() if isinstance(cpu, str) else []
            quota, period = (_finite_int(part) for part in parts) if len(parts) == 2 else (None, None)
            cpu_ok = (quota is not None and period is not None and quota > 0 and period > 0
                      and (quota - 1) * 1_000_000_000 <= int(cpus * 1_000_000_000) * period)
            if (effective.get("private_cgroup_namespace") is not True
                    or memory_max is None or memory_max == 0 or memory_max > memory or swap_max != 0
                    or pids_max is None or pids_max == 0 or pids_max > pids or not cpu_ok):
                failures.append("worker-observed finite cgroup memory, swap, CPU or PID controls are unproven")
            writes = effective.get("writable_mount_probe")
            if type(writes) is not dict or any(writes.get(name) is not True for name in ("checkout", "home", "cache")):
                failures.append("worker UID write mapping on all private mounts is unproven")
            if effective.get("broker_handshake_bound") is not True:
                failures.append("worker-to-broker capability handshake is unproven")

    return {
        "version": 1, "kind": fixture.get("kind"),
        "status": "fixture_complete_untrusted" if not failures else "blocked",
        "fixture_gates_satisfied": not failures,
        "production_admissible": False,
        "reason": "remote Engine and Docker Desktop runtime admission remains disabled",
        "failed_gates": sorted(set(failures)),
        "live_proof_needed": ["authenticated daemon-host ownership and mount observation",
                              "private broker transport and session binding",
                              "real rootless worker cgroup and UID probes",
                              "protected launch, resume and teardown verification"],
    }


def load_private_fixture(path: Path) -> dict:
    scratch = os.environ.get("TMPDIR")
    if not scratch:
        raise ValueError("TMPDIR must name a real-disk fixture directory")
    scratch_path = Path(scratch).resolve(strict=True)
    if not path.is_absolute() or ".." in path.parts or path.resolve(strict=True) != path or not path.is_relative_to(scratch_path):
        raise ValueError("fixture must be a canonical absolute path under TMPDIR")
    parent = path.parent.stat()
    if parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
        raise ValueError("fixture parent must be owner-private")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.geteuid() or meta.st_nlink != 1 or meta.st_mode & 0o077 or meta.st_size > MAX_FIXTURE_BYTES:
            raise ValueError("fixture must be an owner-private regular file at most 64 KiB")
        with os.fdopen(fd, "rb", closefd=False) as file:
            raw = file.read(MAX_FIXTURE_BYTES + 1)
        if len(raw) > MAX_FIXTURE_BYTES:
            raise ValueError("fixture exceeds 64 KiB")
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        if type(value) is not dict:
            raise ValueError("fixture root must be an object")
        return value
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path, help="owner-private mock Engine JSON under TMPDIR")
    args = parser.parse_args(argv)
    try:
        report = evaluate(load_private_fixture(args.fixture))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        report = {"version": 1, "status": "blocked", "fixture_gates_satisfied": False,
                  "production_admissible": False, "failed_gates": [str(exc)]}
    print(json.dumps(report, sort_keys=True))
    return 3 if report["status"] == "fixture_complete_untrusted" else 2


if __name__ == "__main__":
    sys.exit(main())
