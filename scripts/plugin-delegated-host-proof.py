#!/usr/bin/env python3
"""Opt-in, disposable-host proof for the grantless native plugin sandbox."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import tempfile

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
    smoke = subprocess.run([
        str(BWRAP), "--unshare-all", "--unshare-user", "--die-with-parent",
        "--disable-userns", "--cap-drop", "ALL", "--clearenv",
        "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
        "--proc", "/proc", "--dev", "/dev", "--size", "16777216",
        "--tmpfs", "/tmp", "--", "/bin/true",
    ], capture_output=True, timeout=5, check=False)
    require(smoke.returncode == 0,
            "Bubblewrap cannot create the required private namespaces: "
            + smoke.stderr[:300].decode("utf-8", errors="replace").strip())
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="read-only host prerequisite check")
    group.add_argument("--run", action="store_true", help="run the ignored proof after explicit opt-in")
    options = parser.parse_args()
    if options.run:
        require(os.environ.get("DOXA_PLUGIN_CGROUP_ACCEPTANCE") == "1"
                and os.environ.get("DOXA_PLUGIN_DISPOSABLE_HOST") == "1",
                "--run requires both DOXA_PLUGIN_CGROUP_ACCEPTANCE=1 and DOXA_PLUGIN_DISPOSABLE_HOST=1")
        target = check_target_dir(os.environ.get("CARGO_TARGET_DIR", ""))
    parent, scratch, version = preflight()
    print(f"plugin-proof host-ready kernel={platform.release()} bwrap={version} "
          f"uid={os.geteuid()} cpus={len(os.sched_getaffinity(0))} "
          f"parent={parent} scratch={scratch}", flush=True)
    if options.check:
        return 0
    root = Path(__file__).resolve().parent.parent
    build = ["cargo", "build", "--locked", "-p", "doxa-tui", "--bin", "doxa-plugin-worker"]
    if subprocess.run(build, cwd=root, check=False).returncode != 0:
        return 1
    worker = target / "debug" / "doxa-plugin-worker"
    command = ["cargo", "test", "--locked", "-p", "doxa-tui", "--lib",
               "delegated_cgroup_containment_acceptance", "--", "--ignored", "--nocapture"]
    environment = os.environ.copy()
    environment["RUST_TEST_THREADS"] = "1"
    with staged_worker(worker, scratch) as private_worker:
        environment["DOXA_PLUGIN_ACCEPTANCE_WORKER"] = str(private_worker)
        return subprocess.run(command, cwd=root, env=environment, check=False).returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ProofError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        print(f"plugin-proof unavailable: {exc}", file=sys.stderr)
        sys.exit(2)
