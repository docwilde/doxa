#!/usr/bin/env python3
"""Opt-in, disposable-host proof for the grantless native plugin sandbox."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

CGROUP_ROOT = Path("/sys/fs/cgroup")
BWRAP = Path("/usr/bin/bwrap")
REQUIRED_CONTROLLERS = frozenset({"memory", "pids", "cpu"})
REQUIRED_BINARIES = (
    "/bin/bash", "/bin/sh", "/bin/sleep", "/usr/bin/awk",
    "/usr/bin/python3", "/usr/bin/readlink", "/usr/bin/seq",
    "/usr/bin/setsid",
)
BWRAP_FLAGS = (
    "--json-status-fd", "--ro-bind-fd", "--unshare-all", "--unshare-user",
    "--die-with-parent", "--disable-userns", "--cap-drop", "--clearenv",
    "--ro-bind", "--tmpfs", "--size",
)
NAMESPACES = ("net", "mnt", "user", "pid")
PROOF_CASES = frozenset({"approved-wasm", "boundary", "pids", "memory", "cpu", "setsid-cancel", "timeout"})
PROOF_LOG_LIMIT = 128 * 1024
BUILD_LOG_LIMIT = 8 * 1024 * 1024
BUILD_TIMEOUT_SECONDS = 900
PROOF_TIMEOUT_SECONDS = 45
IPV4_ROUTE_POLICY = 'NR == 1 { if (NF != 11 || $1 != "Iface") exit 1; next } { if (NF != 11 || $1 != "lo") exit 1 } END { if (NR == 0) exit 1 }'
IPV6_ROUTE_POLICY = 'NF { if (NF != 10 || $10 != "lo") exit 1 }'


class ProofError(Exception):
    """A missing host prerequisite; no worker may be launched."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ProofError(reason)


def bounded_read(path: Path, limit: int = 4096) -> str:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    require(len(data) <= limit, f"{path}: prerequisite file exceeds {limit} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProofError(f"{path}: prerequisite file is not UTF-8") from exc


def delegated_parent(root: Path, membership: str, uid: int, pid: int) -> Path:
    """Mirror the runner's required empty-parent/direct-supervisor shape."""
    unified = [row[3:] for row in membership.splitlines() if row.startswith("0::")]
    require(len(unified) == 1, "exactly one unified cgroup v2 membership is required")
    relative = unified[0]
    parts = relative.split("/")
    require(relative.startswith("/") and len(relative) <= 1024
            and len(parts) >= 3 and parts[0] == ""
            and all(part not in ("", ".", "..") for part in parts[1:]),
            "caller must occupy a direct supervisor leaf below a delegated parent")
    leaf = root.joinpath(*parts[1:])
    parent = leaf.parent
    require(parent != root, "supervisor leaf lacks a delegated parent")
    cursor = root
    for part in parts[1:]:
        cursor = cursor / part
        meta = cursor.lstat()
        require(stat.S_ISDIR(meta.st_mode), "cgroup membership traverses a link or non-directory")
    require(str(pid) in bounded_read(leaf / "cgroup.procs").splitlines(),
            "caller is not in its reported supervisor leaf")
    meta = parent.lstat()
    require(meta.st_uid == uid and bool(meta.st_mode & stat.S_IWUSR),
            "delegated parent is not user-owned and user-writable")
    require(not bounded_read(parent / "cgroup.procs").strip(),
            "delegated parent contains processes")
    enabled = set(bounded_read(parent / "cgroup.subtree_control").split())
    require(REQUIRED_CONTROLLERS <= enabled,
            "delegated parent must enable memory, pids and cpu for children")
    return parent


def check_cgroup_files(parent: Path) -> None:
    for name in ("cgroup.kill", "cgroup.events", "memory.peak", "memory.swap.max",
                 "pids.peak", "pids.events", "cpu.stat"):
        require((parent / name).is_file(), f"delegated parent lacks {name}")
    require(os.access(parent / "cgroup.kill", os.W_OK),
            "delegated parent cannot kill all worker descendants")
    for name in ("cgroup.max.depth", "cgroup.max.descendants"):
        limit = bounded_read(parent / name).strip()
        require(limit == "max" or (limit.isdecimal() and int(limit) > 0),
                f"delegated parent does not permit a child: {name}")


def check_mount() -> None:
    mounts = bounded_read(Path("/proc/self/mountinfo"), 1_000_000).splitlines()
    matching = []
    for row in mounts:
        before, separator, after = row.partition(" - ")
        fields = before.split()
        if separator and len(fields) > 5 and fields[4] == str(CGROUP_ROOT):
            matching.append((fields[5].split(","), after.split()[0]))
    require(len(matching) == 1 and matching[0][1] == "cgroup2"
            and "rw" in matching[0][0],
            "/sys/fs/cgroup must be a writable cgroup v2 mount")


def check_scratch() -> Path:
    raw = os.environ.get("TMPDIR", "")
    scratch = Path(raw)
    require(raw and scratch.is_absolute() and not scratch.is_relative_to("/tmp"),
            "TMPDIR must be an absolute real-disk directory outside /tmp")
    meta = scratch.lstat()
    require(stat.S_ISDIR(meta.st_mode) and meta.st_uid == os.geteuid()
            and meta.st_mode & 0o777 == 0o700
            and not scratch.resolve(strict=True).is_relative_to("/tmp"),
            "TMPDIR must be an owner-owned private directory (mode 0700)")
    result = subprocess.run(["stat", "-f", "-c", "%T", str(scratch)],
                            capture_output=True, text=True, timeout=5, check=True)
    require(result.stdout.strip() not in {"tmpfs", "ramfs"},
            "TMPDIR must use real-disk storage, not tmpfs or ramfs")
    return scratch


def check_target_dir(raw: str) -> Path:
    target = Path(raw)
    require(raw and target.is_absolute() and not target.is_relative_to("/tmp")
            and not target.resolve(strict=False).is_relative_to("/tmp"),
            "CARGO_TARGET_DIR must be an absolute real-disk path outside /tmp")
    existing = target
    while not existing.exists():
        require(existing != existing.parent, "CARGO_TARGET_DIR has no existing parent")
        existing = existing.parent
    require(existing.is_dir(), "CARGO_TARGET_DIR parent is not a directory")
    result = subprocess.run(["stat", "-f", "-c", "%T", str(existing)],
                            capture_output=True, text=True, timeout=5, check=True)
    require(result.stdout.strip() not in {"tmpfs", "ramfs"},
            "CARGO_TARGET_DIR must use real-disk storage")
    return target


def check_namespace_receipt(stdout: bytes, host: dict[str, str]) -> None:
    """Reject partial, duplicated or host-identical sandbox namespace evidence."""
    require(len(stdout) <= 1024, "Bubblewrap namespace receipt exceeds 1 KiB")
    try:
        rows = stdout.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise ProofError("Bubblewrap namespace receipt is not ASCII") from exc
    require(len(rows) == len(NAMESPACES),
            "Bubblewrap namespace receipt is missing or has extra rows")
    for name, row in zip(NAMESPACES, rows):
        expected_prefix = name + "="
        require(row.startswith(expected_prefix),
                f"Bubblewrap namespace receipt is missing {name}")
        identity = row[len(expected_prefix):]
        require(re.fullmatch(re.escape(name) + r":\[[0-9]+\]", identity) is not None,
                f"Bubblewrap {name} namespace identity is malformed")
        require(identity != host[name],
                f"Bubblewrap reused host {name} namespace")


def check_bwrap() -> str:
    meta = BWRAP.lstat()
    require(stat.S_ISREG(meta.st_mode) and meta.st_uid in (0, os.geteuid())
            and meta.st_mode & 0o022 == 0 and meta.st_mode & 0o111
            and meta.st_nlink == 1,
            "/usr/bin/bwrap must be a trusted, singly linked executable")
    help_result = subprocess.run([str(BWRAP), "--help"], capture_output=True,
                                 text=True, timeout=5, check=True)
    require(all(flag in help_result.stdout for flag in BWRAP_FLAGS),
            "Bubblewrap lacks a launcher flag (especially --ro-bind-fd or --json-status-fd)")
    version = subprocess.run([str(BWRAP), "--version"], capture_output=True,
                             text=True, timeout=5, check=True).stdout.strip()
    host_namespaces = {name: os.readlink(f"/proc/self/ns/{name}") for name in NAMESPACES}
    smoke = subprocess.run([
        str(BWRAP), "--unshare-all", "--unshare-user", "--die-with-parent",
        "--disable-userns", "--cap-drop", "ALL", "--clearenv",
        "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
        "--proc", "/proc", "--dev", "/dev", "--size", "16777216",
        "--tmpfs", "/tmp", "--", "/bin/sh", "-ec",
        "for tool in /bin/bash /bin/sh /bin/sleep /usr/bin/awk /usr/bin/python3 "
        "/usr/bin/readlink /usr/bin/seq /usr/bin/setsid; do "
        "test -x \"$tool\" || { echo \"sandbox tool missing: $tool\" >&2; exit 1; }; done\n"
        "/bin/bash -c :\n/bin/sleep 0\n/usr/bin/awk 'BEGIN { exit 0 }' /dev/null\n"
        "/usr/bin/python3 -c pass\n/usr/bin/seq 1 1 >/dev/null\n"
        "/usr/bin/setsid /bin/true\n"
        "/usr/bin/awk 'NR > 2 { split($0, a, \":\"); gsub(/[[:space:]]/, \"\", a[1]); "
        "if (a[1] != \"lo\") exit 1 }' /proc/net/dev\n"
        f"/usr/bin/awk '{IPV4_ROUTE_POLICY}' /proc/net/route\n"
        "if test -f /proc/net/ipv6_route; then "
        f"/usr/bin/awk '{IPV6_ROUTE_POLICY}' /proc/net/ipv6_route; fi\n"
        "printf 'net=%s\\nmnt=%s\\nuser=%s\\npid=%s\\n' "
        "\"$(/usr/bin/readlink /proc/self/ns/net)\" "
        "\"$(/usr/bin/readlink /proc/self/ns/mnt)\" "
        "\"$(/usr/bin/readlink /proc/self/ns/user)\" "
        "\"$(/usr/bin/readlink /proc/self/ns/pid)\"",
    ], capture_output=True, timeout=5, check=False)
    require(smoke.returncode == 0,
            "Bubblewrap cannot create the required private namespaces: "
            + smoke.stderr[:300].decode("utf-8", errors="replace").strip())
    check_namespace_receipt(smoke.stdout, host_namespaces)
    return version


def preflight() -> tuple[Path, Path, str]:
    require(os.geteuid() != 0, "run as a delegated non-root test user")
    for binary in REQUIRED_BINARIES:
        require(Path(binary).is_file() and os.access(binary, os.X_OK),
                f"required sandbox fixture tool is missing: {binary}")
    require(shutil.which("cargo") is not None and shutil.which("rustc") is not None,
            "cargo and rustc are required")
    require(len(os.sched_getaffinity(0)) >= 2,
            "CPU quota proof requires at least two available processors")
    scratch = check_scratch()
    check_mount()
    parent = delegated_parent(CGROUP_ROOT, bounded_read(Path("/proc/self/cgroup")),
                              os.geteuid(), os.getpid())
    require(os.access(parent, os.W_OK | os.X_OK),
            "delegated parent is not writable by this user")
    check_cgroup_files(parent)
    version = check_bwrap()
    return parent, scratch, version


@contextmanager
def staged_worker(source: Path, scratch: Path):
    """Give the acceptance test a private inode, even when Cargo hardlinks its output."""
    with tempfile.TemporaryDirectory(prefix="doxa-plugin-worker-", dir=scratch) as directory:
        worker = Path(directory) / "doxa-plugin-worker"
        with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC), "rb") as reader:
            before = os.fstat(reader.fileno())
            require(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid()
                    and before.st_mode & 0o111 != 0 and before.st_mode & 0o022 == 0,
                    "built plugin worker is not an owner-controlled executable")
            digest = hashlib.sha256()
            fd = os.open(worker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o700)
            with os.fdopen(fd, "wb") as writer:
                os.fchmod(writer.fileno(), 0o700)
                while chunk := reader.read(1024 * 1024):
                    writer.write(chunk)
                    digest.update(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            after = os.fstat(reader.fileno())
            require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                    "built plugin worker changed while staging")
        metadata = worker.stat()
        require(metadata.st_uid == os.geteuid() and metadata.st_nlink == 1
                and stat.S_IMODE(metadata.st_mode) == 0o700
                and metadata.st_size == before.st_size,
                "staged plugin worker lacks a private executable identity")
        check = hashlib.sha256()
        with worker.open("rb") as staged:
            while chunk := staged.read(1024 * 1024):
                check.update(chunk)
        require(check.digest() == digest.digest(),
                "staged plugin worker differs from the built executable")
        yield worker


def file_sha256(path: Path) -> str:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC), "rb") as stream:
        return descriptor_sha256(stream.fileno(), str(path))


def descriptor_sha256(fd: int, label: str) -> str:
    digest = hashlib.sha256()
    before = os.fstat(fd)
    require(stat.S_ISREG(before.st_mode) and before.st_size <= 1024 * 1024 * 1024,
            f"{label}: executable is not a regular file below 1 GiB")
    offset = 0
    while chunk := os.pread(fd, 1024 * 1024, offset):
        digest.update(chunk)
        offset += len(chunk)
        require(offset <= 1024 * 1024 * 1024, f"{label}: executable exceeded 1 GiB")
    after = os.fstat(fd)
    require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
            f"{label}: executable changed during hashing")
    return digest.hexdigest()


def run_bounded(args: list[str], *, cwd: Path, environment: dict[str, str],
                limit: int, timeout: float, executable: str | None = None,
                pass_fds: tuple[int, ...] = ()) -> tuple[int, bytes]:
    """Bound output as it arrives; kill the new process group on failure."""
    process = subprocess.Popen(args, cwd=cwd, env=environment, executable=executable,
                               pass_fds=pass_fds, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, start_new_session=True, bufsize=0)
    assert process.stdout is not None
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                require(remaining > 0, "proof subprocess exceeded its deadline")
                if not selector.select(min(remaining, 0.25)):
                    continue
                chunk = os.read(process.stdout.fileno(), min(8192, limit + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                require(len(output) <= limit, "proof subprocess exceeded its output limit")
        remaining = deadline - time.monotonic()
        require(remaining > 0, "proof subprocess exceeded its deadline")
        return process.wait(timeout=remaining), bytes(output)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired as exc:
            raise ProofError("proof subprocess could not be reaped after group kill") from exc
        raise
    finally:
        process.stdout.close()


def build_identity(root: Path, environment: dict[str, str]) -> dict[str, object]:
    toolchain: dict[str, object] = {}
    for binary in ("cargo", "rustc"):
        path = shutil.which(binary, path=environment.get("PATH"))
        require(path is not None, f"{binary} is unavailable")
        resolved = Path(path).resolve(strict=True)
        status, output = run_bounded([binary, "-vV"], cwd=root, environment=environment,
                                     limit=8192, timeout=10)
        require(status == 0, f"{binary} version probe failed")
        toolchain[binary] = {"path": str(resolved), "sha256": file_sha256(resolved),
                             "version": output.decode("utf-8")}
    build_keys = sorted(key for key in environment if key in {
        "CARGO_HOME", "CARGO_TARGET_DIR", "CARGO_INCREMENTAL", "CARGO_ENCODED_RUSTFLAGS",
        "RUSTFLAGS", "RUSTDOCFLAGS", "RUSTC", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER",
        "RUSTC_BOOTSTRAP", "RUSTUP_TOOLCHAIN", "CC", "CXX", "CFLAGS", "CXXFLAGS",
        "PKG_CONFIG_PATH", "PKG_CONFIG_LIBDIR",
    } or key.startswith(("CARGO_BUILD_", "CARGO_PROFILE_", "CARGO_TARGET_")))
    pairs = [(key, environment[key]) for key in build_keys]
    env_sha256 = hashlib.sha256(json.dumps(pairs, separators=(",", ":")).encode()).hexdigest()
    cargo_home = Path(environment.get("CARGO_HOME", str(Path.home() / ".cargo")))
    require(cargo_home.is_absolute(), "CARGO_HOME must be absolute for proof identity")
    config_roots = [*root.parents, root, cargo_home]
    config_files: dict[str, str] = {}
    for directory in config_roots:
        for name in ("config", "config.toml"):
            candidate = directory / ".cargo" / name if directory != cargo_home else directory / name
            if candidate.exists() or candidate.is_symlink():
                config_files[str(candidate)] = file_sha256(candidate)
    return {"toolchain": toolchain, "build_environment_sha256": env_sha256,
            "build_environment_keys": build_keys, "cargo_config_sha256": config_files}


def test_artifact(output: bytes, target: Path) -> Path:
    candidates = []
    for line in output.splitlines():
        try:
            message = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue  # Cargo diagnostics are not compiler-artifact messages.
        if not isinstance(message, dict):
            continue
        target_info = message.get("target")
        profile = message.get("profile")
        if (message.get("reason") == "compiler-artifact"
                and isinstance(target_info, dict) and target_info.get("name") == "doxa_tui"
                and target_info.get("kind") == ["lib"]
                and isinstance(profile, dict) and profile.get("test") is True
                and isinstance(message.get("executable"), str)):
            candidates.append(Path(message["executable"]))
    require(len(candidates) == 1, "Cargo did not identify exactly one DOXA library test executable")
    artifact = candidates[0]
    require(artifact.is_absolute() and artifact.resolve(strict=True).is_relative_to(target.resolve(strict=True))
            and not artifact.is_symlink(), "test executable escaped the reviewed target directory")
    return artifact


def source_identity(root: Path) -> dict[str, str]:
    def git(*args: str) -> str:
        result = subprocess.run(["git", *args], cwd=root, capture_output=True,
                                text=True, timeout=10, check=True)
        return result.stdout.strip()

    require(not git("status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching"),
            "proof source checkout must be clean, including untracked and ignored files")
    commit = git("rev-parse", "--verify", "HEAD")
    tree = git("rev-parse", "--verify", "HEAD^{tree}")
    require(re.fullmatch(r"[0-9a-f]{40,64}", commit) is not None
            and re.fullmatch(r"[0-9a-f]{40,64}", tree) is not None,
            "proof source commit or tree identity is invalid")
    return {"commit": commit, "tree": tree}


def cgroup_identity(parent: Path, root: Path = CGROUP_ROOT,
                    membership: str | None = None) -> dict[str, str | int]:
    parent_meta = parent.lstat()
    membership = membership if membership is not None else bounded_read(Path("/proc/self/cgroup"))
    unified = [row[3:] for row in membership.splitlines() if row.startswith("0::")]
    require(len(unified) == 1, "cgroup proof identity requires one unified membership")
    relative = unified[0]
    parts = relative.split("/")
    require(relative.startswith("/") and all(part not in ("", ".", "..") for part in parts[1:]),
            "cgroup proof identity has invalid membership")
    leaf = root.joinpath(*parts[1:])
    require(leaf.parent == parent, "cgroup proof supervisor is no longer under delegated parent")
    leaf_meta = leaf.lstat()
    require(stat.S_ISDIR(parent_meta.st_mode) and stat.S_ISDIR(leaf_meta.st_mode),
            "cgroup proof identity is no longer a directory")
    return {"parent": str(parent), "parent_device": parent_meta.st_dev,
            "parent_inode": parent_meta.st_ino, "supervisor": str(leaf),
            "supervisor_device": leaf_meta.st_dev, "supervisor_inode": leaf_meta.st_ino}


def require_no_plugin_cgroups(parent: Path) -> None:
    entries = list(parent.iterdir())
    require(len(entries) <= 4096, "delegated parent has too many children to audit")
    require(not any(entry.name.startswith("doxa-plugin-") for entry in entries),
            "private plugin worker cgroup remains in delegated parent")


def stop_plugin_cgroups(parent: Path) -> None:
    """Emergency cleanup if the proof test is killed before Rust Drop runs."""
    groups = [entry for entry in parent.iterdir() if entry.name.startswith("doxa-plugin-")]
    require(len(groups) <= 64, "too many plugin cgroups for bounded emergency cleanup")
    for group in groups:
        require(group.is_dir() and not group.is_symlink(), "plugin cleanup saw a non-directory")
        (group / "cgroup.kill").write_text("1")
        deadline = time.monotonic() + 3
        while "populated 0" not in bounded_read(group / "cgroup.events").splitlines():
            require(time.monotonic() < deadline, "plugin cgroup remained populated after emergency kill")
            time.sleep(0.01)
        group.rmdir()
    require_no_plugin_cgroups(parent)


def proof_cases(output: bytes) -> dict[str, dict[str, str]]:
    require(len(output) <= PROOF_LOG_LIMIT, "plugin proof output exceeded 128 KiB")
    try:
        lines = output.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ProofError("plugin proof output is not UTF-8") from exc
    require(any("test result: ok. 1 passed;" in line for line in lines),
            "plugin proof did not report exactly one passing Rust test")
    cases: dict[str, dict[str, str]] = {}
    for line in lines:
        marker = "plugin-acceptance case="
        if marker not in line:
            continue
        prefix, _, suffix = line.partition(marker)
        require(not prefix or prefix.startswith("test native_plugins::runner_sandbox::acceptance::delegated_cgroup_containment_acceptance ... "),
                "plugin proof case has unexpected test-runner prefix")
        line = marker + suffix
        fields: dict[str, str] = {}
        for word in line.split()[1:]:
            key, separator, value = word.partition("=")
            require(separator == "=" and key not in fields and value,
                    "plugin proof case contains a malformed or duplicate field")
            fields[key] = value
        name = fields.get("case", "")
        require(name in PROOF_CASES and name not in cases,
                "plugin proof case is unexpected or duplicated")
        require(fields.get("cleanup") == "removed", "plugin proof case did not remove its cgroup")
        required = {"case", "outcome", "elapsed_ms", "cleanup"}
        if name == "approved-wasm":
            required.add("stale_approval")
            require(fields.get("outcome") == "Return(17)"
                    and fields.get("stale_approval") == "refused",
                    "approved Wasm or stale-approval proof failed")
        else:
            required.update({"memory_peak", "oom_kill", "pids_peak", "pids_max",
                             "cpu_usec", "cpu_throttled", "stdout_bytes", "stderr_bytes"})
        require(set(fields) == required, "plugin proof case fields do not match the reviewed contract")
        for key in required - {"case", "outcome", "stale_approval", "cleanup"}:
            require(fields[key].isdecimal(), f"plugin proof case has invalid {key}")
        expected = {"boundary": "Exit(0)", "cpu": "Timeout", "setsid-cancel": "Cancelled",
                    "timeout": "Timeout"}
        if name in expected:
            require(fields["outcome"] == expected[name], f"plugin proof {name} outcome changed")
        elif name == "pids":
            require(fields["outcome"] == "Cancelled" or re.fullmatch(r"Exit\([0-9]+\)", fields["outcome"]) is not None,
                    "PID proof outcome changed")
        elif name == "memory":
            require(fields["outcome"] == "Cancelled" or re.fullmatch(r"(?:Exit|Crash)\([0-9]+\)", fields["outcome"]) is not None,
                    "memory proof outcome changed")
        cases[name] = fields
    require(set(cases) == PROOF_CASES, "plugin proof is missing one or more of the seven required cases")
    return cases


def receipt_name(raw: str | None, scratch: Path) -> Path:
    require(raw is not None and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,95}", raw) is not None
            and raw not in (".", ".."),
            "--run requires a simple --receipt NAME inside private TMPDIR")
    return scratch / raw


def write_receipt(path: Path, evidence: dict[str, object]) -> None:
    data = (json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    print(f"plugin-proof receipt={path} sha256={hashlib.sha256(data).hexdigest()} "
          "purpose=review-only tui-execution=disabled", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="read-only host prerequisite check")
    group.add_argument("--run", action="store_true", help="run the ignored proof after explicit opt-in")
    parser.add_argument("--receipt", metavar="NAME", help="exclusive private JSON receipt name for --run")
    options = parser.parse_args()
    if options.run:
        require(os.environ.get("DOXA_PLUGIN_CGROUP_ACCEPTANCE") == "1"
                and os.environ.get("DOXA_PLUGIN_DISPOSABLE_HOST") == "1",
                "--run requires both DOXA_PLUGIN_CGROUP_ACCEPTANCE=1 and DOXA_PLUGIN_DISPOSABLE_HOST=1")
        target = check_target_dir(os.environ.get("CARGO_TARGET_DIR", ""))
    else:
        require(options.receipt is None, "--check does not create an acceptance receipt")
    parent, scratch, version = preflight()
    print(f"plugin-proof host-ready namespace=isolated net=loopback-only routes=loopback-only "
          f"kernel={platform.release()} bwrap={version} "
          f"uid={os.geteuid()} cpus={len(os.sched_getaffinity(0))} "
          f"parent={parent} scratch={scratch}", flush=True)
    if options.check:
        return 0
    root = Path(__file__).resolve().parent.parent
    receipt = receipt_name(options.receipt, scratch)
    require(not receipt.exists(), "plugin proof receipt already exists")
    source = source_identity(root)
    cgroup = cgroup_identity(parent)
    require_no_plugin_cgroups(parent)
    bwrap_sha256 = file_sha256(BWRAP)
    environment = os.environ.copy()
    environment["RUST_TEST_THREADS"] = "1"
    environment["CARGO_TERM_COLOR"] = "never"
    environment["DOXA_PLUGIN_ACCEPTANCE_PARENT"] = str(parent)
    environment["DOXA_PLUGIN_ACCEPTANCE_PARENT_ID"] = (
        f"{cgroup['parent_device']}:{cgroup['parent_inode']}")
    build_identity_before = build_identity(root, environment)
    build_command = ["cargo", "build", "--locked", "-p", "doxa-tui", "--bin", "doxa-plugin-worker"]
    build_status, build_output = run_bounded(build_command, cwd=root, environment=environment,
                                             limit=BUILD_LOG_LIMIT, timeout=BUILD_TIMEOUT_SECONDS)
    sys.stdout.buffer.write(build_output)
    sys.stdout.flush()
    if build_status != 0:
        return 1
    worker = target / "debug" / "doxa-plugin-worker"
    compile_command = ["cargo", "test", "--locked", "-p", "doxa-tui", "--lib",
                       "--no-run", "--message-format=json"]
    compile_status, compile_output = run_bounded(
        compile_command, cwd=root, environment=environment,
        limit=BUILD_LOG_LIMIT, timeout=BUILD_TIMEOUT_SECONDS)
    require(compile_status == 0, "Cargo failed to build the proof test executable")
    artifact = test_artifact(compile_output, target)
    with staged_worker(worker, scratch) as private_worker:
        environment["DOXA_PLUGIN_ACCEPTANCE_WORKER"] = str(private_worker)
        worker_sha256 = file_sha256(private_worker)
        with os.fdopen(os.open(artifact, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC), "rb") as test_file:
            meta = os.fstat(test_file.fileno())
            require(stat.S_ISREG(meta.st_mode) and meta.st_uid == os.geteuid()
                    and meta.st_mode & 0o022 == 0 and meta.st_mode & 0o111,
                    "proof test executable is not owner-controlled and executable")
            test_sha256 = descriptor_sha256(test_file.fileno(), str(artifact))
            test_fd = test_file.fileno()
            command = [str(artifact), "delegated_cgroup_containment_acceptance",
                       "--ignored", "--nocapture", "--test-threads=1"]
            try:
                test_status, output = run_bounded(
                    command, cwd=root, environment=environment, limit=PROOF_LOG_LIMIT,
                    timeout=PROOF_TIMEOUT_SECONDS, executable=f"/proc/self/fd/{test_fd}",
                    pass_fds=(test_fd,))
            except BaseException:
                stop_plugin_cgroups(parent)
                raise
        sys.stdout.buffer.write(output)
        sys.stdout.flush()
        if test_status != 0:
            stop_plugin_cgroups(parent)
            return test_status
        try:
            cases = proof_cases(output)
        except BaseException:
            stop_plugin_cgroups(parent)
            raise
    require_no_plugin_cgroups(parent)
    require(cgroup_identity(parent) == cgroup, "delegated cgroup identity changed during proof")
    require(file_sha256(BWRAP) == bwrap_sha256, "Bubblewrap binary changed during proof")
    require(source_identity(root) == source, "proof source checkout changed during proof")
    require(file_sha256(artifact) == test_sha256, "proof test executable changed during proof")
    require(build_identity(root, environment) == build_identity_before,
            "proof toolchain or build configuration changed during proof")
    write_receipt(receipt, {
        "format_version": 1, "purpose": "review-only; does not authorize TUI execution",
        "tui_execution_authorized": False, "recorded_at": datetime.now(timezone.utc).isoformat(),
        "source": source, "worker_sha256": worker_sha256, "bwrap_sha256": bwrap_sha256,
        "test_executable_sha256": test_sha256, "build": build_identity_before,
        "host": {"kernel": platform.release(), "bwrap_version": version,
                 "uid": os.geteuid(), "cpus": len(os.sched_getaffinity(0)), "cgroup": cgroup},
        "proof_log_sha256": hashlib.sha256(output).hexdigest(),
        "cases": {name: cases[name] for name in sorted(cases)},
    })
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ProofError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        print(f"plugin-proof unavailable: {exc}", file=sys.stderr)
        sys.exit(2)
