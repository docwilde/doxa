#!/usr/bin/env python3
"""Bounded, opt-in EDQUOT proof on an administrator-prepared rootless fixture.

This never provisions quotas or changes DOXA admission. The caller supplies a
private task-local project-quota tree and a pinned, credential-free Python image.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import uuid

from check_docker_quota_preflight import inspect


MAX_WRITE_MIB = 128
MIN_HOST_FREE = 512 * 1024 * 1024
IMAGE = re.compile(r"(?:[a-zA-Z0-9][a-zA-Z0-9./:_-]*@)?sha256:[0-9a-fA-F]{64}\Z")
WORKER = r"""
import errno, json, os, sys
name, cap, token = sys.argv[1], int(sys.argv[2]), sys.argv[3]
directory = '/fixture/' + name
path = directory + '/.doxa-quota-probe-' + token
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
written, result = 0, None
try:
    chunk = b'x' * 1048576
    while written < cap:
        try:
            remaining = min(len(chunk), cap - written)
            size = os.write(fd, chunk[:remaining])
            if size <= 0:
                raise RuntimeError('zero-byte write')
            written += size
            os.fsync(fd)
        except OSError as exc:
            result = exc.errno
            break
finally:
    os.close(fd)
    os.unlink(path)
print(json.dumps({'source': name, 'bytes_written': written, 'errno': result,
                  'edquot': result == errno.EDQUOT}, sort_keys=True))
"""


def _checked_socket(endpoint: str) -> Path:
    prefix = f"unix:///run/user/{os.geteuid()}/"
    if not endpoint.startswith(prefix) or endpoint == prefix + "docker.sock":
        raise ValueError("use an explicit task-local rootless Unix Engine socket")
    path = Path(endpoint.removeprefix("unix://"))
    if path.resolve(strict=True) != path:
        raise ValueError("Docker socket must be canonical")
    metadata = path.lstat()
    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise ValueError("Docker socket must be an owner-owned Unix socket")
    return path


def _checked_fixture(root: Path) -> tuple[Path, dict]:
    temp = Path(os.environ["TMPDIR"])
    if not temp.is_absolute() or temp == Path("/tmp") or temp.resolve(strict=True) != temp:
        raise ValueError("TMPDIR must be a canonical real-disk task directory")
    if not root.is_absolute() or root.resolve(strict=True) != root or temp not in root.parents:
        raise ValueError("fixture must be a canonical child of TMPDIR")
    if root == temp or root.name in {"doxa", "isolation"}:
        raise ValueError("refusing a live or shared root")
    report = inspect(root, Path("/proc/self/mountinfo").read_text())
    if not report["capability_candidate"]:
        raise ValueError("project-quota fixture prerequisite check failed: " + "; ".join(report["reasons"]))
    if report["descendants_checked"] > 3:
        raise ValueError("quota probe requires empty checkout, home and cache directories")
    for name in ("checkout", "home", "cache"):
        if any((root / name).iterdir()):
            raise ValueError("quota probe requires empty bind sources")
    free = os.statvfs(root).f_bavail * os.statvfs(root).f_frsize
    if free < MIN_HOST_FREE:
        raise ValueError("host filesystem has less than 512 MiB available")
    return root, report


def _docker(endpoint: str, args: list[str], docker_config: Path, timeout: int = 30) -> str:
    command = ["docker", "--host", endpoint, *args]
    # An empty task-local config prevents a default Docker context, auth file
    # or credential helper in the user's home from entering this proof.
    env = {"PATH": "/usr/bin:/bin", "HOME": str(docker_config),
           "DOCKER_CONFIG": str(docker_config)}
    result = subprocess.run(command, env=env, capture_output=True, text=True,
                            timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f"Docker fixture command failed ({result.returncode}): {result.stderr[:300]}")
    if len(result.stdout) > 8192:
        raise RuntimeError("Docker fixture output exceeded bound")
    return result.stdout.strip()


def _checked_engine(endpoint: str, image: str, docker_config: Path) -> None:
    if not IMAGE.fullmatch(image):
        raise ValueError("fixture image must be pinned by sha256 digest")
    info = json.loads(_docker(endpoint, ["info", "--format", "{{json .}}"], docker_config))
    if not any("name=rootless" in option for option in info.get("SecurityOptions", [])):
        raise ValueError("fixture Engine must report rootless mode")
    image_info = json.loads(_docker(endpoint, ["image", "inspect", image], docker_config))
    row = image_info[0]
    if image.startswith("sha256:"):
        valid = row.get("Id") == image
    else:
        valid = image in row.get("RepoDigests", [])
    if not valid:
        raise ValueError("reviewed fixture image digest differs from local image")


def validate_receipt(data: object, source: str, cap: int) -> int:
    if not isinstance(data, dict) or set(data) != {"source", "bytes_written", "errno", "edquot"}:
        raise ValueError("quota worker receipt has missing or extra fields")
    size = data["bytes_written"]
    if (data["source"] != source or type(size) is not int or not 0 < size < cap
            or data["errno"] != errno.EDQUOT or data["edquot"] is not True):
        raise ValueError(f"{source} did not prove bounded EDQUOT through its bind")
    return size


def probe(root: Path, endpoint: str, image: str, max_write_mib: int) -> dict:
    if not 1 <= max_write_mib <= MAX_WRITE_MIB:
        raise ValueError("probe cap must be 1..128 MiB")
    _checked_socket(endpoint)
    root, prerequisite = _checked_fixture(root)
    identities = {name: ((root / name).stat().st_dev, (root / name).stat().st_ino)
                  for name in ("checkout", "home", "cache")}
    cap = max_write_mib * 1024 * 1024
    token = uuid.uuid4().hex
    name = "doxa-quota-probe-" + token[:12]
    command = ["run", "--rm", "--pull", "never", "--name", name, "--network", "none", "--read-only",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
               "--user", "0:0", "--pids-limit", "64", "--memory", "512m",
               "--memory-swap", "512m", "--cpus", "1", "--tmpfs", "/tmp:rw,nosuid,nodev,size=16777216"]
    for source in ("checkout", "home", "cache"):
        command += ["--mount", f"type=bind,src={root / source},dst=/fixture/{source}"]
    command += ["--entrypoint", "python3", image, "-c", WORKER]
    receipts = {}
    with tempfile.TemporaryDirectory(prefix="doxa-quota-docker-config-", dir=os.environ["TMPDIR"]) as config_dir:
        docker_config = Path(config_dir)
        _checked_engine(endpoint, image, docker_config)
        try:
            for source in ("checkout", "home", "cache"):
                output = _docker(endpoint, command + [source, str(cap), token], docker_config, timeout=120)
                if len(output.splitlines()) != 1:
                    raise ValueError("quota worker emitted multiple receipt lines")
                row = json.loads(output)
                receipts[source] = validate_receipt(row, source, cap)
                if any((root / source).iterdir()):
                    raise ValueError("quota worker left a file in its bind")
                if os.statvfs(root).f_bavail * os.statvfs(root).f_frsize < MIN_HOST_FREE:
                    raise ValueError("host free space fell below fixture floor")
            _checked_fixture(root)
            for source, expected in identities.items():
                current = (root / source).stat()
                if (current.st_dev, current.st_ino) != expected:
                    raise ValueError("quota fixture bind source changed during probe")
        finally:
            # A CLI timeout may leave a container alive. Remove only this
            # random fixture name, never another session or Engine object.
            reaped = False
            try:
                _docker(endpoint, ["rm", "-f", name], docker_config, timeout=15)
                reaped = True
            except (RuntimeError, subprocess.TimeoutExpired):
                pass
            if reaped:
                for source in ("checkout", "home", "cache"):
                    # Only remove our random marker after the container is
                    # stopped. unlink does not follow a replacement symlink.
                    (root / source / (".doxa-quota-probe-" + token)).unlink(missing_ok=True)
    return {"version": 1, "fixture": str(root), "project_id": prerequisite["sources"]["root"]["project_id"],
            "max_write_mib": max_write_mib, "bytes_before_edquot": receipts,
            "hard_enforcement_verified_for_fixture": True, "admissible_as_hard_quota": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--docker-host", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--max-write-mib", type=int, default=128)
    parser.add_argument("--acknowledge-fixture-writes", action="store_true")
    args = parser.parse_args(argv)
    if not args.acknowledge_fixture_writes:
        parser.error("explicit --acknowledge-fixture-writes is required")
    try:
        result = probe(args.fixture, args.docker_host, args.image, args.max_write_mib)
        code = 0
    except (OSError, ValueError, RuntimeError, IndexError, KeyError,
            subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        result = {"version": 1, "hard_enforcement_verified_for_fixture": False,
                  "admissible_as_hard_quota": False, "reason": str(exc)}
        code = 2
    print(json.dumps(result, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
