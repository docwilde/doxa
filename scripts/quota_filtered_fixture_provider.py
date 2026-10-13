#!/usr/bin/env python3
"""Credential-free image-owned workload for the disposable quota proof only.

Installed as /usr/local/bin/claude in the proof image and invoked through the
reviewed worker's exec-hardened path. It is not a live provider implementation.
"""
import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import socket
import struct
import sys

ROOTS = {"checkout": Path("/workspace"), "home": Path("/home/doxa"), "cache": Path("/work-cache")}
MARKER = ".quota-filtered-proof"


def write(source: str, amount: int, append: bool = False) -> dict:
    if amount < 1 or amount > 64 * 1024 * 1024:
        raise RuntimeError("write cap exceeds fixture bound")
    path = ROOTS[source] / MARKER
    fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC
                 | (os.O_APPEND if append else os.O_CREAT | os.O_EXCL), 0o600)
    written, failure = 0, None
    try:
        chunk = b"q" * 65536
        while written < amount:
            try:
                size = os.write(fd, chunk[:min(len(chunk), amount - written)])
                if size <= 0:
                    raise RuntimeError("zero-byte fixture write")
                written += size
                os.fsync(fd)
            except OSError as cause:
                failure = cause.errno
                break
    finally:
        os.close(fd)
    return {"source": source, "bytes_written": written, "errno": failure}


def mutate() -> dict:
    directory = ROOTS["checkout"] / "owner-filter-control"
    directory.mkdir(mode=0o700)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    attr = bytearray(28)
    fcntl.ioctl(fd, 0x801c581f, attr, True)
    before = struct.unpack("IIIII8s", attr)
    altered = list(before)
    altered[0] &= ~0x200
    altered[3] = 0
    results = {}
    try:
        fcntl.ioctl(fd, 0x401c5820, struct.pack("IIIII8s", *altered))
        results["FSSETXATTR"] = None
    except OSError as cause:
        results["FSSETXATTR"] = cause.errno
    libc = ctypes.CDLL(None, use_errno=True)
    machine = os.uname().machine
    syscall = 16 if machine == "x86_64" else 29 if machine == "aarch64" else None
    if syscall is None:
        raise RuntimeError("unsupported fixture architecture")
    flags = ctypes.c_ulong(0)
    for command in (0x40086602, 0x40046602, 0x1_401C5820, 0x1_40086602):
        ctypes.set_errno(0)
        value = libc.syscall(ctypes.c_long(syscall), ctypes.c_long(fd), ctypes.c_ulonglong(command),
                             ctypes.byref(flags))
        results[hex(command)] = ctypes.get_errno() if value == -1 else None
    fcntl.ioctl(fd, 0x801c581f, attr, True)
    after = struct.unpack("IIIII8s", attr)
    os.close(fd)
    return {"attempts": results, "project_before": before[3], "project_after": after[3],
            "inherits_before": bool(before[0] & 0x200), "inherits_after": bool(after[0] & 0x200),
            "uid": os.getuid(), "directory": directory.name}


def broker() -> dict:
    path = Path("/run/doxa/session") / MARKER
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        failure = None
    except OSError as cause:
        failure = cause.errno
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(3)
    client.connect("/run/doxa/session/egress-claude.sock")
    answer = client.recv(32).decode()
    client.close()
    return {"write_errno": failure, "reply": answer}


def main() -> None:
    action = sys.argv[1]
    if action == "mutate":
        result = mutate()
    elif action == "broker":
        result = broker()
    elif action == "sizes":
        result = {name: (root / MARKER).stat().st_size for name, root in ROOTS.items()}
    elif action in ("write", "append", "solo"):
        source, amount = sys.argv[2], int(sys.argv[3])
        result = write(source, amount, action == "append")
        if action == "solo":
            (ROOTS[source] / MARKER).unlink()
    else:
        raise RuntimeError("unsupported quota fixture action")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
