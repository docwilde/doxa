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

KEEPALIVE = "import time; time.sleep(600)"
AGGREGATE_WORKER = r"""
import errno, json, os, sys
mode, name, cap, token = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
path = '/fixture/' + name + '/.doxa-quota-probe-' + token
if mode == 'check':
    names = ('checkout', 'home', 'cache')
    sizes = {item: os.stat('/fixture/' + item + '/.doxa-quota-probe-' + token,
                           follow_symlinks=False).st_size for item in names}
    print(json.dumps({'sizes': sizes}, sort_keys=True))
    sys.exit(0)
flags = os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW
flags |= os.O_CREAT | os.O_EXCL if mode != 'append' else os.O_APPEND
fd = os.open(path, flags, 0o600)
written, result = 0, None
try:
    chunk = b'x' * 65536
    while written < cap:
        try:
            size = os.write(fd, chunk[:min(len(chunk), cap - written)])
            if size <= 0:
                raise RuntimeError('zero-byte write')
            written += size
            os.fsync(fd)
        except OSError as exc:
            result = exc.errno
            break
finally:
    os.close(fd)
print(json.dumps({'source': name, 'bytes_written': written, 'errno': result}, sort_keys=True))
"""


def _fixture_command(root: Path, image: str, name: str, token: str, action: str,
                     worker: str) -> list[str]:
    command = [action]
    if action == "run":
        command.append("--rm")
    command += ["--pull", "never", "--name", name,
                "--label", "org.doxa.quota-probe=" + token,
                "--network", "none", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                "--user", "0:0", "--pids-limit", "64", "--memory", "512m",
                "--memory-swap", "512m", "--cpus", "1",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=16777216"]
    for source in ("checkout", "home", "cache"):
        command += ["--mount", f"type=bind,src={root / source},dst=/fixture/{source}"]
    return command + ["--entrypoint", "python3", image, "-c", worker]


def _receipt(output: str) -> dict:
    if len(output.splitlines()) != 1:
        raise ValueError("quota worker emitted multiple receipt lines")
    row = json.loads(output)
    if not isinstance(row, dict):
        raise ValueError("quota worker emitted a non-object receipt")
    return row


def _inspect_fixture_container(endpoint: str, name: str, root: Path,
                               image: str, token: str, expected_id: str,
                               docker_config: Path) -> None:
    data = json.loads(_docker(endpoint, ["inspect", name], docker_config,
                              max_output=32768))
    if not isinstance(data, list) or len(data) != 1:
        raise ValueError("Docker inspect did not identify one fixture container")
    row = data[0]
    if not isinstance(row, dict):
        raise ValueError("Docker inspect returned a non-object container")
    expected = {(str(root / source), "/fixture/" + source)
                for source in ("checkout", "home", "cache")}
    mounts = row.get("Mounts")
    if not isinstance(mounts, list) or any(not isinstance(mount, dict) for mount in mounts):
        raise ValueError("Docker inspect omitted fixture mounts")
    actual = {(mount.get("Source"), mount.get("Destination")) for mount in mounts}
    host_config = row.get("HostConfig")
    config = row.get("Config")
    state = row.get("State")
    if not all(isinstance(part, dict) for part in (host_config, config, state)):
        raise ValueError("Docker inspect omitted fixture configuration")
    labels = config.get("Labels")
    caps = host_config.get("CapDrop")
    if not isinstance(labels, dict) or not isinstance(caps, list):
        raise ValueError("Docker inspect omitted fixture labels or capabilities")
    if (row.get("Id") != expected_id or row.get("Name") != "/" + name
            or config.get("Image") != image
            or labels.get("org.doxa.quota-probe") != token
            or state.get("Running") is not True
            or host_config.get("NetworkMode") != "none"
            or host_config.get("ReadonlyRootfs") is not True
            or host_config.get("Privileged") is not False
            or "ALL" not in caps
            or len(mounts) != 3 or actual != expected
            or any(mount.get("Type") != "bind" or mount.get("RW") is not True
                   for mount in mounts)):
        raise ValueError("fixture container image, network, root or bind mounts differ")


def validate_aggregate_receipt(row: object, source: str, cap: int,
                               expect_edquot: bool) -> int:
    if not isinstance(row, dict) or set(row) != {"source", "bytes_written", "errno"}:
        raise ValueError("aggregate quota receipt has missing or extra fields")
    size = row["bytes_written"]
    if (row["source"] != source or type(size) is not int or size < 0 or size > cap
            or row["errno"] != (errno.EDQUOT if expect_edquot else None)
            or (expect_edquot and size >= cap) or (not expect_edquot and size != cap)):
        raise ValueError(f"{source} aggregate write did not meet its expected quota result")
    return size


def _checked_marker_sizes(root: Path, token: str, expected: dict[str, int]) -> None:
    for source, size in expected.items():
        marker = root / source / (".doxa-quota-probe-" + token)
        meta = marker.lstat()
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.geteuid() or meta.st_size != size:
            raise ValueError("aggregate marker identity or size differs from worker receipt")


def _host_free_check(root: Path) -> None:
    free = os.statvfs(root).f_bavail * os.statvfs(root).f_frsize
    if free < MIN_HOST_FREE:
        raise ValueError("host free space fell below fixture floor")


def _aggregate_restart_probe(root: Path, endpoint: str, image: str, name: str,
                             docker_config: Path, token: str, cap: int,
                             baseline: dict[str, int]) -> dict:
    # Each prefix is large enough to separate aggregate accounting from a
    # per-bind limit, yet leaves room for a third bind to reach EDQUOT.
    prefix = min(8 * 1024 * 1024, min(baseline.values()) // 4, cap // 4)
    if prefix < 1024 * 1024:
        raise ValueError("quota fixture limit is too small for an aggregate proof")
    created = _docker(endpoint, _fixture_command(root, image, name, token, "create", KEEPALIVE),
                      docker_config)
    if not re.fullmatch(r"[0-9a-f]{64}", created):
        raise ValueError("Docker create did not return a container ID")
    _docker(endpoint, ["start", name], docker_config)
    _inspect_fixture_container(endpoint, name, root, image, token, created, docker_config)

    def worker(mode: str, source: str, limit: int) -> dict:
        output = _docker(endpoint, ["exec", name, "python3", "-c", AGGREGATE_WORKER,
                                    mode, source, str(limit), token], docker_config,
                         timeout=120)
        return _receipt(output)

    for source in ("checkout", "home"):
        validate_aggregate_receipt(worker("create", source, prefix), source, prefix, False)
        _host_free_check(root)
    third = validate_aggregate_receipt(worker("create", "cache", cap), "cache", cap, True)
    if third == 0:
        raise ValueError("third bind could not make a positive write before EDQUOT")
    _host_free_check(root)
    tolerance = max(256 * 1024, min(prefix // 4, 1024 * 1024))
    if baseline["cache"] - third < 2 * prefix - tolerance:
        raise ValueError("third bind EDQUOT did not demonstrate aggregate project accounting")
    sizes = {"checkout": prefix, "home": prefix, "cache": third}
    _checked_marker_sizes(root, token, sizes)
    _docker(endpoint, ["stop", "--time", "2", name], docker_config, timeout=15)
    _docker(endpoint, ["start", name], docker_config)
    _inspect_fixture_container(endpoint, name, root, image, token, created, docker_config)
    after = worker("check", "cache", 0)
    if set(after) != {"sizes"} or after["sizes"] != sizes:
        raise ValueError("aggregate bind contents changed across container restart")
    additional = validate_aggregate_receipt(worker("append", "cache", 1024 * 1024),
                                            "cache", 1024 * 1024, True)
    if additional > tolerance:
        raise ValueError("quota allowed too much additional data after restart")
    sizes["cache"] += additional
    _checked_marker_sizes(root, token, sizes)
    _host_free_check(root)
    return {"prefix_bytes_each": prefix, "third_bind_bytes_before_edquot": third,
            "additional_bytes_before_edquot_after_restart": additional,
            "container_id": created}


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


def _docker(endpoint: str, args: list[str], docker_config: Path, timeout: int = 30,
            max_output: int = 8192) -> str:
    command = ["docker", "--host", endpoint, *args]
    # An empty task-local config prevents a default Docker context, auth file
    # or credential helper in the user's home from entering this proof.
    env = {"PATH": "/usr/bin:/bin", "HOME": str(docker_config),
           "DOCKER_CONFIG": str(docker_config)}
    result = subprocess.run(command, env=env, capture_output=True, text=True,
                            timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f"Docker fixture command failed ({result.returncode}): {result.stderr[:300]}")
    if len(result.stdout) > max_output:
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


def _owned_container_id(endpoint: str, name: str, token: str,
                        docker_config: Path) -> str:
    rows = json.loads(_docker(endpoint, ["inspect", name], docker_config,
                              max_output=32768))
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError("fixture container identity could not be inspected")
    row = rows[0]
    config = row.get("Config")
    ident = row.get("Id")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if (not isinstance(config, dict) or row.get("Name") != "/" + name
            or not isinstance(labels, dict)
            or labels.get("org.doxa.quota-probe") != token
            or not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{64}", ident)):
        raise ValueError("refusing to remove a container without this fixture identity")
    return ident


def probe(root: Path, endpoint: str, image: str, max_write_mib: int,
          aggregate_restart: bool = False) -> dict:
    if not 1 <= max_write_mib <= MAX_WRITE_MIB:
        raise ValueError("probe cap must be 1..128 MiB")
    _checked_socket(endpoint)
    root, prerequisite = _checked_fixture(root)
    identities = {name: ((root / name).stat().st_dev, (root / name).stat().st_ino)
                  for name in ("checkout", "home", "cache")}
    cap = max_write_mib * 1024 * 1024
    token = uuid.uuid4().hex
    name = "doxa-quota-probe-" + token
    command = _fixture_command(root, image, name, token, "run", WORKER)
    receipts = {}
    aggregate = None
    with tempfile.TemporaryDirectory(prefix="doxa-quota-docker-config-", dir=os.environ["TMPDIR"]) as config_dir:
        docker_config = Path(config_dir)
        _checked_engine(endpoint, image, docker_config)
        try:
            for source in ("checkout", "home", "cache"):
                output = _docker(endpoint, command + [source, str(cap), token], docker_config, timeout=120)
                row = _receipt(output)
                receipts[source] = validate_receipt(row, source, cap)
                if any((root / source).iterdir()):
                    raise ValueError("quota worker left a file in its bind")
                if os.statvfs(root).f_bavail * os.statvfs(root).f_frsize < MIN_HOST_FREE:
                    raise ValueError("host free space fell below fixture floor")
            if aggregate_restart:
                aggregate = _aggregate_restart_probe(root, endpoint, image, name,
                                                     docker_config, token, cap, receipts)
            else:
                _checked_fixture(root)
            for source, expected in identities.items():
                current = (root / source).stat()
                if (current.st_dev, current.st_ino) != expected:
                    raise ValueError("quota fixture bind source changed during probe")
        finally:
            # A CLI timeout may leave a container alive. Inspect the random
            # label before removal so a name collision cannot remove another
            # user's or session's container.
            reaped = False
            try:
                owned_id = _owned_container_id(endpoint, name, token, docker_config)
                _docker(endpoint, ["rm", "-f", owned_id], docker_config, timeout=15)
                reaped = True
            except (RuntimeError, ValueError, subprocess.TimeoutExpired):
                pass
            if reaped:
                for source in ("checkout", "home", "cache"):
                    # Only remove our random marker after the container is
                    # stopped. unlink does not follow a replacement symlink.
                    (root / source / (".doxa-quota-probe-" + token)).unlink(missing_ok=True)
            elif aggregate is not None:
                raise RuntimeError("fixture container could not be reaped; markers left for review")
    if aggregate_restart:
        _checked_fixture(root)
        for source, expected in identities.items():
            current = (root / source).stat()
            if (current.st_dev, current.st_ino) != expected:
                raise ValueError("quota fixture bind source changed during cleanup")
    return {"version": 1, "fixture": str(root), "project_id": prerequisite["sources"]["root"]["project_id"],
            "max_write_mib": max_write_mib, "bytes_before_edquot": receipts,
            "aggregate_restart_verified_for_fixture": aggregate is not None,
            "aggregate_restart_evidence": aggregate,
            "hard_enforcement_verified_for_fixture": True, "admissible_as_hard_quota": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--docker-host", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--max-write-mib", type=int, default=128)
    parser.add_argument("--aggregate-restart", action="store_true",
                        help="also prove aggregate EDQUOT across live binds and after container restart")
    parser.add_argument("--acknowledge-fixture-writes", action="store_true")
    args = parser.parse_args(argv)
    if not args.acknowledge_fixture_writes:
        parser.error("explicit --acknowledge-fixture-writes is required")
    try:
        result = probe(args.fixture, args.docker_host, args.image, args.max_write_mib,
                       args.aggregate_restart)
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
