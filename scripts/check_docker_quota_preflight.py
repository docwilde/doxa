#!/usr/bin/env python3
"""Read-only, fail-closed preflight for a Docker session's private bind sources.

This reports project-quota capability hints, never hard enforcement. A real
administrator-set block limit and a rootless Engine write test are still needed.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys
from typing import Callable


# linux/fs.h: FS_IOC_FSGETXATTR = _IOR('X', 31, struct fsxattr).
FS_IOC_FSGETXATTR = 0x801C581F
FS_XFLAG_PROJINHERIT = 0x00000200
FSXATTR_SIZE = 28
SOURCES = ("checkout", "home", "cache")
O_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
MAX_DESCENDANTS = 4096
MAX_DEPTH = 64


@dataclass(frozen=True)
class Mount:
    id: int
    point: Path
    fs_type: str
    options: frozenset[str]


def _unescape(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def parse_mountinfo(contents: str) -> list[Mount]:
    mounts = []
    ids = set()
    for line in contents.splitlines():
        fields = line.split()
        try:
            separator = fields.index("-")
            if separator < 6 or len(fields) < separator + 4:
                raise ValueError("short mountinfo row")
            mount_id = int(fields[0])
            point = Path(_unescape(fields[4]))
            if mount_id in ids or not point.is_absolute():
                raise ValueError("duplicate ID or relative mount point")
            ids.add(mount_id)
            mounts.append(Mount(mount_id, point, fields[separator + 1],
                                frozenset(fields[5].split(",") + fields[separator + 3].split(","))))
        except (ValueError, IndexError) as exc:
            raise ValueError("invalid mountinfo; quota capability is unknown") from exc
    if not mounts:
        raise ValueError("empty mountinfo; quota capability is unknown")
    return mounts


def mount_for(path: Path, mounts: list[Mount]) -> Mount:
    matches = [mount for mount in mounts if path == mount.point or mount.point in path.parents]
    if not matches:
        raise ValueError("no mount covers a private bind source")
    # Equal-length mount points can occur in stacked mounts; the visible one
    # cannot be established from this read-only snapshot, so refuse it.
    length = max(len(mount.point.parts) for mount in matches)
    best = [mount for mount in matches if len(mount.point.parts) == length]
    if len(best) != 1:
        raise ValueError("ambiguous stacked mount covers a private bind source")
    return best[0]


def project_metadata(fd: int) -> tuple[int, bool]:
    raw = bytearray(FSXATTR_SIZE)
    fcntl.ioctl(fd, FS_IOC_FSGETXATTR, raw, True)
    flags, _extsize, _nextents, project_id, _cowextsize, _padding = struct.unpack("=IIIII8s", raw)
    return project_id, bool(flags & FS_XFLAG_PROJINHERIT)


def _private_directory(path: Path, uid: int) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("private source must be an absolute, traversal-free path")
    fd = os.open("/", O_DIRECTORY)
    try:
        # O_NOFOLLOW on every component keeps the inspection anchored even if
        # a parent is replaced while the operator is running the preflight.
        for part in path.parts[1:]:
            try:
                child = os.open(part, O_DIRECTORY, dir_fd=fd)
            except OSError as exc:
                raise ValueError("private source path contains a symlink, missing directory or inaccessible component") from exc
            os.close(fd)
            fd = child
        meta = os.fstat(fd)
        if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != uid or meta.st_mode & 0o077:
            raise ValueError("private source must be owned by the current user and mode 0700")
        visible = os.stat(path, follow_symlinks=False)
        if (visible.st_dev, visible.st_ino) != (meta.st_dev, meta.st_ino):
            raise ValueError("private source changed during inspection")
        return fd
    except BaseException:
        os.close(fd)
        raise


def audit_descendants(root_fd: int, expected_dev: int, expected_project: int,
                      read_project: Callable[[int], tuple[int, bool]],
                      max_entries: int = MAX_DESCENDANTS,
                      max_depth: int = MAX_DEPTH) -> int:
    """Inspect existing entries without following links or reading file data.

    The walk has fixed work and descriptor-depth limits. It remains a snapshot,
    not proof that a project quota has a configured, enforced block limit.
    """
    checked = 0

    def walk(parent_fd: int, depth: int) -> None:
        nonlocal checked
        with os.scandir(parent_fd) as entries:
            for entry in entries:
                checked += 1
                if checked > max_entries:
                    raise ValueError("private tree exceeds the descendant inspection limit")
                try:
                    before = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise ValueError("private tree entry cannot be inspected") from exc
                if not (stat.S_ISDIR(before.st_mode) or stat.S_ISREG(before.st_mode)):
                    raise ValueError("private tree contains a symlink or special entry")
                try:
                    child_fd = os.open(entry.name, os.O_RDONLY | os.O_NONBLOCK |
                                       os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
                except OSError as exc:
                    raise ValueError("private tree entry changed or cannot be opened") from exc
                try:
                    current = os.fstat(child_fd)
                    if (current.st_dev, current.st_ino, stat.S_IFMT(current.st_mode)) != (
                            before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode)):
                        raise ValueError("private tree entry changed during inspection")
                    if current.st_dev != expected_dev:
                        raise ValueError("private tree entry crosses a filesystem boundary")
                    try:
                        project_id, inherits = read_project(child_fd)
                    except OSError as exc:
                        raise ValueError("private tree project metadata is unavailable") from exc
                    if project_id != expected_project:
                        raise ValueError("private tree contains a different project ID")
                    if stat.S_ISDIR(current.st_mode):
                        if not inherits:
                            raise ValueError("private tree directory lacks project inheritance")
                        if depth >= max_depth:
                            raise ValueError("private tree exceeds the directory depth limit")
                        walk(child_fd, depth + 1)
                finally:
                    os.close(child_fd)

    walk(root_fd, 0)
    return checked


def inspect(root: Path, mountinfo: str,
            read_project: Callable[[int], tuple[int, bool]] = project_metadata) -> dict:
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("session root must be absolute and traversal-free")
    report = {
        "version": 1, "session_root": str(root), "status": "unsupported",
        "capability_candidate": False, "hard_enforcement_verified": False,
        "admissible_as_hard_quota": False, "reasons": [], "sources": {},
    }
    reasons: list[str] = report["reasons"]
    mounts = parse_mountinfo(mountinfo)
    paths = {"root": root, **{name: root / name for name in SOURCES}}
    descriptors: dict[str, int] = {}
    try:
        for name, path in paths.items():
            descriptors[name] = _private_directory(path, os.geteuid())
        root_stat = os.fstat(descriptors["root"])
        root_mount = mount_for(root, mounts)
        report["filesystem"] = root_mount.fs_type
        report["mount_point"] = str(root_mount.point)
        if root_mount.fs_type not in {"xfs", "ext4"}:
            reasons.append("backing filesystem is not an explicitly supported project-quota filesystem")
        if not {"prjquota", "pquota"} & root_mount.options:
            reasons.append("project-quota mount option is not visible")
        for mount in mounts:
            if mount.point != root and root in mount.point.parents:
                reasons.append("nested host mount exists under the private session tree")
                break
        project_ids = set()
        for name, path in paths.items():
            meta = os.fstat(descriptors[name])
            source_mount = mount_for(path, mounts)
            if meta.st_dev != root_stat.st_dev or source_mount.id != root_mount.id:
                reasons.append(f"{name} is not on the private root's filesystem and mount")
            try:
                project_id, inherits = read_project(descriptors[name])
            except OSError:
                reasons.append(f"{name} project metadata is unavailable")
                project_id, inherits = 0, False
            if project_id == 0 or not inherits:
                reasons.append(f"{name} lacks a nonzero inherited project ID")
            project_ids.add(project_id)
            report["sources"][name] = {"project_id": project_id, "inherits_project": inherits}
        if len(project_ids) != 1:
            reasons.append("private bind sources have different project IDs")
        if not reasons:
            try:
                report["descendants_checked"] = audit_descendants(
                    descriptors["root"], root_stat.st_dev, next(iter(project_ids)), read_project)
            except ValueError as exc:
                reasons.append(str(exc))
    finally:
        for fd in descriptors.values():
            os.close(fd)
    if not reasons:
        report["status"] = "candidate_unverified"
        report["capability_candidate"] = True
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_root", type=Path, help="existing owner-private task-local root with checkout, home and cache")
    args = parser.parse_args(argv)
    try:
        report = inspect(args.session_root, Path("/proc/self/mountinfo").read_text())
    except (OSError, ValueError) as exc:
        report = {"version": 1, "status": "unsupported", "capability_candidate": False,
                  "hard_enforcement_verified": False, "admissible_as_hard_quota": False,
                  "reasons": [str(exc)]}
    print(json.dumps(report, sort_keys=True))
    # Exit remains nonzero even for a capability candidate: this tool has not
    # measured a configured hard block limit or writes through real bind mounts.
    return 3 if report["capability_candidate"] else 2


if __name__ == "__main__":
    sys.exit(main())
