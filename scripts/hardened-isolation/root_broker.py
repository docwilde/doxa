#!/usr/bin/env python3
"""Root-installed session broker. No CLI, shell, host path or Engine RPC passthrough.

The native authority callback is a deliberately gated composition seam: ready is
never granted without quota, filter and native origin/gateway checks. This module
alone, or a fake Engine fixture, does not authorize hardened isolation.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
import fcntl
import hashlib
import io
import ipaddress
import json
import os
import pwd
from pathlib import Path
import re
import secrets
import select
import signal
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tarfile
import threading
import time

PROTOCOL = "doxa-root-session-broker-v1"
POLICY_CONTRACT = "doxa-root-session-policy-v1"
AUTHORITY_CONTRACT = "doxa-root-session-authority-v1"
WORKER = "/usr/local/bin/doxa-isolation-worker"
SO_PEERPIDFD = 77
MAX_JSON = 65536
MAX_HEADER = 8192
MAX_FRAME = 65536
MAX_ARTIFACT = 512 * 1024 * 1024
MAX_NATIVE_STDERR = 8192
TAG_JSON, TAG_STDOUT, TAG_STDERR, TAG_EXIT, TAG_ERROR = range(1, 6)
TAG_STDIN, TAG_EOF, TAG_CANCEL = range(6, 9)
TAG_ENTRY, TAG_FILE, TAG_STAGED = range(9, 12)
MAX_ENTRIES = 20000
TAG_SNAPSHOT, TAG_SNAPSHOT_END = 12, 13
MAX_CONTROLS = 8
OPS = {"guard", "provider_snapshot", "verify_provider_snapshot", "describe", "prepare", "resume", "stop", "provider_exec", "workspace_exec", "register_hook", "unregister_hook"}
SECRET_KEYS = {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"}
ENV_KEYS = SECRET_KEYS | {"TERM", "LANG", "LC_ALL", "LORE_SKIP", "CLAUDE_CODE_DISABLE_AUTO_MEMORY", "DOXA_PEER_INBOUND_TURNS"}


class Refused(RuntimeError):
    pass


def require(condition, reason):
    if not condition:
        raise Refused(reason)


def compact(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def strict_json(data):
    def pairs(rows):
        result = {}
        for key, value in rows:
            require(key not in result, "duplicate JSON field")
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(Refused("nonfinite JSON")))


def keys(value, expected, optional=()):
    require(type(value) is dict and set(value) <= set(expected) | set(optional)
            and set(expected) <= set(value), "missing or unknown scoped field")


def integer(value, low, high):
    return type(value) is int and low <= value <= high


def hash_value(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def canonical(value):
    require(type(value) is str and value.startswith("/") and "\x00" not in value,
            "absolute canonical path required")
    path = Path(value)
    require(str(path) == value and ".." not in path.parts, "noncanonical policy path")
    return path


def controlled(path, private=False, readonly=False):
    """Reject every symlink and every writable non-root ancestor."""
    path = canonical(str(path))
    for item in [path, *path.parents]:
        meta = item.lstat()
        require(not stat.S_ISLNK(meta.st_mode) and meta.st_uid == 0
                and not meta.st_mode & 0o022, "path is outside root control")
    meta = path.stat()
    if private:
        require(not meta.st_mode & 0o077, "root private permissions required")
    if readonly:
        require(not meta.st_mode & 0o222, "read-only root policy required")
    return meta


def read_regular(path, maximum=MAX_JSON, root=False, readonly=False):
    if root:
        controlled(path, readonly=readonly)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                and before.st_size <= maximum, "regular single-link bounded file required")
        data = bytearray()
        while chunk := os.read(fd, min(65536, maximum + 1 - len(data))):
            data.extend(chunk)
            require(len(data) <= maximum, "file bound exceeded")
        after = os.fstat(fd)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        require(identity(before) == identity(after) and len(data) == before.st_size,
                "file changed while reading")
        return bytes(data)
    finally:
        os.close(fd)


def sha_file(path, root=False):
    if root:
        controlled(path)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        meta = os.fstat(fd)
        require(stat.S_ISREG(meta.st_mode) and meta.st_size <= MAX_ARTIFACT,
                "executable file size/type bound")
        out = hashlib.sha256()
        size = 0
        while chunk := os.read(fd, 1024 * 1024):
            size += len(chunk)
            require(size <= MAX_ARTIFACT, "executable file size bound")
            out.update(chunk)
        require(size == meta.st_size, "executable size changed")
        return out.hexdigest()
    finally:
        os.close(fd)


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def hostname(value):
    require(type(value) is str and len(value) <= 253 and value == value.lower()
            and "." in value and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p)
                                    for p in value.split(".")), "exact DNS hostname required")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise Refused("literal-IP egress forbidden")


def policy_paths(p):
    root = canonical(p["root"])
    runtime = Path("/run/doxa/hardened") / p["session_id"]
    return {"state_dir": str(runtime), "socket": str(runtime / "broker.sock"),
            "receipt": str(runtime / "broker-receipt.json"), "workspace": str(root / "checkout"),
            "home": str(root / "home"), "cache": str(root / "cache"), "session_mount": str(root / "broker")}


def resource_limits(p):
    r = p["limits"]
    return {"memory_bytes": r["memory_bytes"], "nano_cpus": r["cpu_quota"] * 1000000000 // r["cpu_period"],
            "pids": r["pids"], "operation_seconds": r["operation_seconds"], "exec_seconds": r["deadline_seconds"],
            "output_bytes": r["output_bytes"], "stdin_bytes": r["stdin_bytes"]}


def validate_policy(p):
    keys(p, {"version", "session_id", "caller_uid", "engine", "root", "image", "worker",
             "providers", "authority", "broker", "limits"})
    require(type(p["version"]) is int and p["version"] == 1 and type(p["session_id"]) is str
            and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,63}", p["session_id"]), "policy identity")
    require(integer(p["caller_uid"], 1, 2**32-1), "caller identity must be non-root")
    e = p["engine"]
    keys(e, {"uid", "gid", "socket", "exe_path", "exe_sha256", "config_path", "config_sha256", "api_version"})
    require(integer(e["uid"], 1, 2**32-1) and integer(e["gid"], 1, 2**32-1) and e["uid"] != p["caller_uid"],
            "root, caller and Engine identities must be distinct")
    require(e["api_version"] in {"1.47", "1.48", "1.49", "1.50", "1.51", "1.52"}, "reviewed Engine API version")
    for field in ("socket", "exe_path", "config_path"):
        canonical(e[field])
    require(hash_value(e["exe_sha256"]) and hash_value(e["config_sha256"]), "Engine hash pins required")
    require(type(p["image"]) is str and re.fullmatch(r"sha256:[0-9a-f]{64}", p["image"]), "immutable image pin required")
    keys(p["worker"], {"path", "sha256"})
    require(p["worker"]["path"] == WORKER and hash_value(p["worker"]["sha256"]), "fixed image worker identity required")
    root = canonical(p["root"])
    require(root not in {Path("/"), Path("/run"), Path("/home"), Path("/root")}
            and not str(root).startswith("/run/doxa/hardened"), "private session root required")
    r = p["limits"]
    keys(r, {"memory_bytes", "cpu_quota", "cpu_period", "pids", "disk_bytes", "output_bytes", "deadline_seconds",
             "stdin_bytes", "operation_seconds"})
    for field, low, high in (("memory_bytes", 64*1024*1024, 16*1024**3),
                             ("cpu_quota", 10000, 1600000), ("cpu_period", 10000, 1000000), ("pids", 16, 512),
                             ("disk_bytes", 64*1024*1024, 1024**4), ("operation_seconds", 1, 120),
                             ("deadline_seconds", 1, 3600), ("output_bytes", 1024, 64*1024*1024),
                             ("stdin_bytes", 1024, 64*1024*1024)):
        require(integer(r[field], low, high), "finite reviewed resource bound required")
    require(1 <= r["cpu_quota"] // r["cpu_period"] <= 16 and r["cpu_quota"] % r["cpu_period"] == 0,
            "integral finite worker CPU ceiling required")
    providers = p["providers"]
    require(type(providers) is dict and set(providers) <= {"claude", "codex"} and len(providers) > 0,
            "only reviewed provider families")
    for family, provider in providers.items():
        keys(provider, {"path", "sha256", "artifacts", "egress", "proxy_port", "environment_keys"})
        require(provider["path"] == f"/usr/local/bin/{family}" and hash_value(provider["sha256"]), "fixed provider entrypoint")
        require(type(provider["artifacts"]) is dict and 1 <= len(provider["artifacts"]) <= 8,
                "provider artifact pins required")
        for path, value in provider["artifacts"].items():
            canonical(path)
            allowed = path == provider["path"] or (family == "codex" and path in {
                "/opt/doxa/codex-provider/codex", "/opt/doxa/codex-provider/codex-app-server",
                "/opt/doxa/codex-provider/codex-code-mode-host", "/opt/doxa/codex-provider/codex-code-mode-host-payload",
                "/opt/doxa/codex-provider/receipt.json"})
            require(allowed and hash_value(value), "unknown provider artifact")
        if family == "codex":
            require(set(provider["artifacts"]) >= {"/opt/doxa/codex-provider/codex", "/opt/doxa/codex-provider/codex-app-server",
                "/opt/doxa/codex-provider/codex-code-mode-host", "/opt/doxa/codex-provider/codex-code-mode-host-payload",
                "/opt/doxa/codex-provider/receipt.json"}, "Codex complete protected package pins required")
        direct = provider["artifacts"].get(provider["path"])
        codex_dispatcher = provider["artifacts"].get("/opt/doxa/codex-provider/codex") if family == "codex" else None
        require(provider["sha256"] in (direct, codex_dispatcher), "provider target byte pin missing")
        require(type(provider["egress"]) is list and 1 <= len(provider["egress"]) <= 32
                and len(set(provider["egress"])) == len(provider["egress"]), "exact family egress scope")
        for host in provider["egress"]:
            hostname(host)
        require(provider["proxy_port"] == {"claude": 3128, "codex": 3129}[family], "fixed family loopback proxy port")
        env = provider["environment_keys"]
        require(type(env) is list and set(env) <= ENV_KEYS and len(set(env)) == len(env), "tiny selected environment")
        require(("OPENAI_API_KEY" not in env if family == "claude" else "ANTHROPIC_API_KEY" not in env),
                "provider secret family mismatch")
    for field in ("authority", "broker"):
        keys(p[field], {"path", "sha256"})
        canonical(p[field]["path"])
        require(hash_value(p[field]["sha256"]), "root authority hash pin required")
    return p


class Deadline:
    """One monotonic budget across connect, slow headers, body and stream."""
    def __init__(self, seconds, cancelled=lambda: False):
        self.end = time.monotonic() + seconds
        self.cancelled = cancelled

    def check(self):
        require(time.monotonic() < self.end, "absolute transaction deadline")
        require(not self.cancelled(), "client cancelled")

    def wait(self, reads=(), writes=()):
        self.check()
        while True:
            ready = select.select(reads, writes, [], min(.1, max(0, self.end-time.monotonic())))
            self.check()
            if ready[0] or ready[1]:
                return ready


class Channel:
    def __init__(self, sock, deadline):
        self.sock, self.deadline, self.buffer = sock, deadline, bytearray()
        sock.setblocking(False)

    def send(self, data):
        data = memoryview(data)
        while data:
            self.deadline.wait(writes=[self.sock])
            try:
                count = self.sock.send(data)
            except BlockingIOError:
                continue
            require(count > 0, "peer closed while writing")
            data = data[count:]

    def exact(self, size):
        while len(self.buffer) < size:
            self.deadline.wait(reads=[self.sock])
            try:
                chunk = self.sock.recv(min(65536, size-len(self.buffer)))
            except BlockingIOError:
                continue
            require(chunk, "peer closed while reading")
            self.buffer.extend(chunk)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def line(self, maximum=MAX_HEADER):
        result = bytearray()
        while not result.endswith(b"\r\n"):
            require(len(result) < maximum, "HTTP line bound")
            result.extend(self.exact(1))
        return bytes(result)

    def frame(self, tag, payload):
        require(len(payload) <= MAX_FRAME, "frame bound")
        self.send(bytes([tag]) + len(payload).to_bytes(4, "big") + payload)

    def json_frame(self, tag, value):
        self.frame(tag, compact(value))

    def receive_frame(self):
        header = self.exact(5)
        size = int.from_bytes(header[1:], "big")
        require(size <= MAX_FRAME, "frame bound")
        return header[0], self.exact(size)


class HttpBody:
    def __init__(self, channel, headers, maximum):
        self.channel, self.maximum, self.total = channel, maximum, 0
        require(not ("content-length" in headers and "transfer-encoding" in headers), "ambiguous HTTP body")
        self.chunked = headers.get("transfer-encoding") == "chunked"
        require("transfer-encoding" not in headers or self.chunked, "unsupported transfer encoding")
        self.remaining = int(headers.get("content-length", "0")) if not self.chunked else None
        require(self.remaining is None or 0 <= self.remaining <= maximum, "HTTP body bound")
        self.chunk_remaining = 0
        self.done = False

    def read(self, size=-1):
        if size < 0:
            parts = []
            while part := self.read(65536):
                parts.append(part)
            return b"".join(parts)
        if self.done or size == 0:
            return b""
        if self.chunked:
            if self.chunk_remaining == 0:
                line = self.channel.line(128).strip()
                require(re.fullmatch(b"[0-9a-fA-F]{1,16}", line), "invalid chunk framing")
                self.chunk_remaining = int(line, 16)
                if self.chunk_remaining == 0:
                    require(self.channel.line(2) == b"\r\n", "HTTP trailers forbidden")
                    self.done = True
                    return b""
            count = min(size, self.chunk_remaining)
            self.chunk_remaining -= count
        else:
            if self.remaining == 0:
                self.done = True
                return b""
            count = min(size, self.remaining)
            self.remaining -= count
        self.total += count
        require(self.total <= self.maximum, "HTTP body bound")
        result = self.channel.exact(count)
        if self.chunked and self.chunk_remaining == 0:
            require(self.channel.exact(2) == b"\r\n", "invalid chunk termination")
        return result


def response(channel, maximum=MAX_JSON):
    status_line = channel.line()
    match = re.fullmatch(b"HTTP/1\\.[01] ([0-9]{3}) [^\r\n]*\r\n", status_line)
    require(match is not None, "invalid Engine status line")
    headers, total = {}, len(status_line)
    while True:
        line = channel.line()
        total += len(line)
        require(total <= MAX_HEADER, "Engine header bound")
        if line == b"\r\n":
            break
        require(not line.startswith((b" ", b"\t")) and b":" in line, "invalid Engine header")
        name, value = line[:-2].split(b":", 1)
        name = name.decode("ascii").lower()
        require(name not in headers and re.fullmatch(r"[a-z0-9-]+", name), "duplicate/invalid Engine header")
        headers[name] = value.decode("ascii").strip().lower()
    return int(match[1]), headers, HttpBody(channel, headers, maximum)


def kernel_peer_pin(sock, uid):
    """Atomic kernel identity; no cross-UID /proc executable permission assumed."""
    pid, actual_uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    require(actual_uid == uid and pid > 0, "peer UID differs from root-installed authority")
    fd = struct.unpack("i", sock.getsockopt(socket.SOL_SOCKET, SO_PEERPIDFD, 4))[0]
    os.set_inheritable(fd, False)
    try:
        info = dict(line.split(":", 1) for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines() if ":" in line)
        require(int(info.get("Pid", "-1")) == pid and not select.select([fd], [], [], 0)[0],
                "kernel peer pidfd identity is unavailable")
        return pid, fd
    except BaseException:
        os.close(fd)
        raise


def peer_pin(sock, uid, executable_hash):
    pid, fd = kernel_peer_pin(sock, uid)
    try:
        require(sha_file(f"/proc/{pid}/exe") == executable_hash, "peer executable differs from reviewed hash")
        return pid, fd
    except BaseException:
        os.close(fd)
        raise


def process_start_time(pid):
    # comm may contain spaces and parentheses; field22 follows the closing comm.
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return int(fields[19])


def root_start_time_matches(pid, pidfd, expected):
    require(integer(expected,1,2**64-1) and process_start_time(pid)==expected
            and not select.select([pidfd],[],[],0)[0], "live ROOT peer start time differs from instance")


class Engine:
    def __init__(self, policy):
        self.policy = policy
        self.config = policy["engine"]
        self.pid = self.pidfd = self.socket_identity = None

    def live(self):
        require(self.pidfd is not None and not select.select([self.pidfd], [], [], 0)[0], "reviewed Engine exited/restarted")
        meta = os.lstat(self.config["socket"])
        require((meta.st_dev, meta.st_ino) == self.socket_identity, "reviewed Engine socket was replaced")
        require(sha_file(f"/proc/{self.pid}/exe") == self.config["exe_sha256"], "reviewed Engine executable changed")
        require(sha(read_regular(self.config["config_path"], root=True, readonly=True)) == self.config["config_sha256"],
                "immutable Engine config changed")

    def connect(self, deadline):
        deadline.check()
        config = self.config
        controlled(config["exe_path"])
        require(sha_file(config["exe_path"]) == config["exe_sha256"], "installed Engine binary changed")
        require(sha(read_regular(config["config_path"], root=True, readonly=True)) == config["config_sha256"], "Engine config differs")
        meta = os.lstat(config["socket"])
        require(stat.S_ISSOCK(meta.st_mode) and meta.st_uid == config["uid"] and meta.st_gid == config["gid"]
                and not meta.st_mode & 0o077, "Engine socket must exclude caller and other identities")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(min(2, max(.001, deadline.end-time.monotonic())))
        fd = None
        try:
            sock.connect(config["socket"])
            pid, fd = peer_pin(sock, config["uid"], config["exe_sha256"])
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            path = config["config_path"].encode()
            require(b"--config-file=" + path in cmdline or any(
                cmdline[i] == b"--config-file" and i+1 < len(cmdline) and cmdline[i+1] == path
                for i in range(len(cmdline))), "Engine does not use reviewed immutable config")
            identity = (meta.st_dev, meta.st_ino)
            if self.pid is None:
                self.pid, self.pidfd, self.socket_identity = pid, fd, identity
                fd = None
            require(pid == self.pid and identity == self.socket_identity, "Engine instance drift between requests")
            self.live()
            return Channel(sock, deadline)
        except Exception:
            sock.close()
            raise
        finally:
            if fd is not None:
                os.close(fd)

    def send_request(self, channel, method, path, body=None, upgrade=False):
        require(method in {"GET", "POST", "DELETE"} and path.startswith("/") and "\r" not in path and "\n" not in path,
                "internal Engine request invariant")
        data = b"" if body is None else compact(body)
        connection = b"Connection: Upgrade\r\nUpgrade: tcp\r\n" if upgrade else b"Connection: keep-alive\r\n"
        channel.send(f"{method} /v{self.config['api_version']}{path} HTTP/1.1\r\nHost: docker\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\n".encode() + connection + b"\r\n" + data)

    def exchange(self, channel, method, path, body=None, statuses=(200,), maximum=MAX_JSON):
        self.live()
        self.send_request(channel, method, path, body)
        status, _, body_reader = response(channel, maximum)
        require(status in statuses, "scoped Engine operation refused")
        data = body_reader.read()
        self.live()
        return status, strict_json(data) if data else None

    def request(self, deadline, method, path, body=None, statuses=(200,)):
        channel = self.connect(deadline)
        try:
            return self.exchange(channel, method, path, body, statuses)
        finally:
            channel.sock.close()

    def archive_hash(self, deadline, cid, path):
        channel = self.connect(deadline)
        try:
            self.send_request(channel, "GET", f"/containers/{cid}/archive?path={path}")
            status, _, body = response(channel, MAX_ARTIFACT + 1024*1024)
            require(status == 200, "fixed image artifact unavailable")
            found = []
            with tarfile.open(fileobj=body, mode="r|") as archive:
                for member in archive:
                    require(len(found) == 0 and member.isfile() and member.size <= MAX_ARTIFACT,
                            "fixed image artifact must be one regular file")
                    stream = archive.extractfile(member)
                    digest = hashlib.sha256()
                    while chunk := stream.read(65536):
                        deadline.check()
                        digest.update(chunk)
                    found.append(digest.hexdigest())
            while body.read(65536):
                pass
            require(len(found) == 1, "fixed image artifact absent")
            self.live()
            return found[0]
        finally:
            channel.sock.close()

    def close(self):
        if self.pidfd is not None:
            os.close(self.pidfd)
            self.pidfd = None


def host_init_pid(engine_pid, reported):
    def nspids(pid):
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("NSpid:"):
                return [int(item) for item in line.split()[1:]]
        raise Refused("kernel namespace PID mapping unavailable")
    depth = len(nspids(engine_pid))
    candidates = []
    for path in Path("/proc").iterdir():
        if path.name.isdigit():
            try:
                ids = nspids(int(path.name))
                if len(ids) > depth and ids[depth-1] == reported:
                    candidates.append(int(path.name))
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
    require(len(candidates) == 1, "container host init PID is not uniquely mapped")
    # This is a candidate only; native authority independently pins/revalidates it.
    return candidates[0]


def create_spec(policy, policy_sha256):
    r, paths = resource_limits(policy), policy_paths(policy)
    binds = [f"{paths[key]}:{target}:{mode}" for key, target, mode in (
        ("workspace", "/workspace", "rw"), ("home", "/home/doxa", "rw"),
        ("cache", "/work-cache", "rw"), ("session_mount", "/run/doxa/session", "ro"))]
    return {"Image": policy["image"], "Entrypoint": [WORKER], "Cmd": ["hold-hardened"],
            "User": "0:0", "WorkingDir": "/workspace", "Env": ["HOME=/home/doxa", "TMPDIR=/work-cache/tmp", "PATH=/usr/local/bin:/usr/bin:/bin"],
            "Tty": False, "OpenStdin": False, "AttachStdin": False, "AttachStdout": False, "AttachStderr": False,
            "Labels": {"io.doxa.root-session": policy["session_id"], "io.doxa.root-policy": policy_sha256},
            "HostConfig": {"ReadonlyRootfs": True, "NetworkMode": "none", "Privileged": False,
                "CapDrop": ["ALL"], "CapAdd": [], "SecurityOpt": ["no-new-privileges:true"],
                "Memory": r["memory_bytes"], "MemorySwap": r["memory_bytes"], "NanoCpus": r["nano_cpus"],
                "PidsLimit": r["pids"], "PidMode": "", "IpcMode": "none", "CgroupnsMode": "private",
                "Binds": binds, "Devices": [], "DeviceRequests": [], "LogConfig": {"Type": "none", "Config": {}},
                "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0}, "AutoRemove": False,
                "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=65536k,mode=1777"}}}


def inspect_exact(policy, policy_sha256, cid, info, image_labels=None, live=True):
    expected = create_spec(policy, policy_sha256)
    require(type(info) is dict and info.get("Id") == cid and info.get("Image") == policy["image"], "container/image identity drift")
    config = info.get("Config", {})
    for name in ("Entrypoint", "Cmd", "User", "WorkingDir", "Tty", "OpenStdin"):
        require(config.get(name) == expected[name], "container init identity drift")
    require(config.get("Labels") == {**(image_labels or {}), **expected["Labels"]}, "root labels/pinned image metadata drift")
    for name, value in expected["HostConfig"].items():
        if name in {"Binds", "Devices", "DeviceRequests", "CapAdd"}:
            require((info.get("HostConfig", {}).get(name) or []) == value, "effective Engine container policy drift")
        else:
            require(info.get("HostConfig", {}).get(name) == value, "effective Engine container policy drift")
    host = info.get("HostConfig", {})
    for name in ("Links", "VolumesFrom", "ExtraHosts", "PortBindings", "Dns", "DnsOptions", "DnsSearch", "GroupAdd", "Sysctls"):
        require(not host.get(name), "unreviewed host networking/process extension")
    require(not host.get("PublishAllPorts") and not host.get("UTSMode")
            and not host.get("UsernsMode") and not host.get("Runtime") in {"runsc", "kata"}, "unreviewed namespace/runtime extension")
    require(not config.get("Volumes") and not config.get("Healthcheck"), "unreviewed image-owned mounts or health exec")
    mounts = info.get("Mounts", [])
    expected_mounts = {"/workspace": (policy_paths(policy)["workspace"], True), "/home/doxa": (policy_paths(policy)["home"], True),
                       "/work-cache": (policy_paths(policy)["cache"], True), "/run/doxa/session": (policy_paths(policy)["session_mount"], False)}
    require(len(mounts) == 4, "unexpected Engine mount")
    for mount in mounts:
        target = mount.get("Destination")
        require(target in expected_mounts and (mount.get("Source"), mount.get("RW")) == expected_mounts.pop(target)
                and mount.get("Type") == "bind" and mount.get("Propagation") == "rprivate", "Engine mount authority drift")
    networks = info.get("NetworkSettings", {}).get("Networks", {})
    require(set(networks) <= {"none"} and all(not entry.get(field) for entry in networks.values()
            for field in ("IPAddress", "Gateway", "GlobalIPv6Address", "IPv6Gateway")), "Engine network namespace policy drift")
    if live:
        require(info.get("State", {}).get("Running") is True and integer(info["State"].get("Pid"), 1, 2**31-1), "session container is not live")
    return info


def atomic_root_write(path, data, mode):
    path = Path(path)
    controlled(path.parent)
    temporary = path.parent / (path.name + ".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fchmod(output.fileno(), mode)
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def kill_owned_child(child):
    # wait() may already have reaped a refused child. Its numerical PID can then
    # be reused, so never signal that process group after a completed wait.
    if child.returncode is None:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=2)


class NativeStderr:
    """Continuously drain a native service; retain only a bounded prefix."""
    def __init__(self,pipe):
        self.pipe=pipe;self.data=bytearray();self.total=0;self.lock=threading.Lock();self.stop=threading.Event()
        os.set_blocking(pipe.fileno(),False)
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()

    def run(self):
        while not self.stop.is_set():
            ready,_,_=select.select([self.pipe],[],[],.05)
            if not ready:continue
            try:chunk=os.read(self.pipe.fileno(),MAX_FRAME)
            except BlockingIOError:continue
            if not chunk:return
            with self.lock:
                self.total=min(2**63-1,self.total+len(chunk));self.data.extend(chunk[:MAX_NATIVE_STDERR-len(self.data)])

    def snapshot(self):
        with self.lock:return bytes(self.data),self.total

    def finish(self):
        self.stop.set();self.thread.join(.2)
        require(not self.thread.is_alive(),"native diagnostic drain did not stop")
        for _ in range(16):
            try:chunk=os.read(self.pipe.fileno(),MAX_FRAME)
            except BlockingIOError:break
            if not chunk:break
            with self.lock:
                self.total=min(2**63-1,self.total+len(chunk));self.data.extend(chunk[:MAX_NATIVE_STDERR-len(self.data)])
        self.pipe.close()
        return self.snapshot()


class Broker:
    def __init__(self, policy_path):
        self.policy_path = str(canonical(str(policy_path)))
        self.raw_policy = read_regular(self.policy_path, root=True, readonly=True)
        self.policy = validate_policy(strict_json(self.raw_policy))
        require(self.policy["broker"]["path"] == str(Path(__file__).resolve(strict=True))
                and self.policy["broker"]["sha256"] == sha_file(__file__), "running broker code is not root policy pin")
        self.policy_sha256 = sha(self.raw_policy)
        self.state_dir = Path(policy_paths(self.policy)["state_dir"])
        controlled(self.state_dir)
        lock = open_git_metadata_lock(self.policy)
        os.close(lock)
        self.manifest_path = self.state_dir / "manifest.json"
        self.engine = Engine(self.policy)
        self.image_labels = None
        self.state_lock = threading.RLock()
        self.git_lock = threading.Lock()
        self.closing = threading.Event()
        self.active_provider = False
        self.active_workspace = 0
        require(not (self.state_dir / "uncertain-create.json").exists(), "uncertain Engine create requires root reconciliation")
        self.manifest = None
        self.host_init_pid = None
        self.gateway = None
        self.gateway_pidfd = None
        self.gateway_sockets = {}
        self.gateway_parents = {}
        self.gateway_stderr = None
        self.gateway_start_error = None
        self.adapters = {}
        if self.manifest_path.exists():
            self.manifest = strict_json(read_regular(self.manifest_path, root=True))
            require(self.manifest.get("policy_sha256") == self.policy_sha256
                    and self.manifest.get("session_id") == self.policy["session_id"]
                    and self.manifest.get("boot_id") == boot_id(), "root manifest authority is stale")

    def persist(self, cid, status):
        require(re.fullmatch(r"[0-9a-f]{64}", cid) is not None, "exact container CID required")
        self.engine.live()
        self.manifest = {"session_id": self.policy["session_id"],
                         "version": 1, "policy_sha256": self.policy_sha256, "boot_id": boot_id(), "cid": cid,
                         "host_init_pid": self.host_init_pid, "gateway_pid": None,
                         "status": status, "engine_pid": self.engine.pid,
                         "engine_socket_dev": self.engine.socket_identity[0], "engine_socket_ino": self.engine.socket_identity[1]}
        atomic_root_write(self.manifest_path, compact(self.manifest) + b"\n", 0o600)

    def identity(self):
        require(self.manifest is not None and self.manifest.get("status") != "stopped", "session is not prepared")
        self.engine.live()
        require(self.manifest["engine_pid"] == self.engine.pid and
                (self.manifest["engine_socket_dev"], self.manifest["engine_socket_ino"]) == self.engine.socket_identity,
                "root manifest Engine instance drift")
        return self.manifest["cid"]

    def guard(self, deadline):
        """Live ROOT authority check for a new turn on a persistent transport."""
        cid = None
        try:
            require(not self.closing.is_set(), "root broker is closing")
            cid = self.identity()
            require(self.manifest["status"] == "ready", "session admission is not ready")
            self.native_check(deadline, "provider_exec", "none")
            return {"protocol":PROTOCOL,"session_id":self.policy["session_id"],"policy_sha256":self.policy_sha256,
                    "cid":cid,"status":"ready","guard_ready":True}
        except BaseException:
            if cid is not None:self.cleanup(cid)
            raise

    def preflight(self):
        p = self.policy
        require(sha(read_regular(self.policy_path, root=True, readonly=True)) == self.policy_sha256, "root policy changed")
        for name in ("authority", "broker"):
            controlled(p[name]["path"])
            require(sha_file(p[name]["path"]) == p[name]["sha256"], "root native authority identity changed")
        # Mount roots are fixed at installation, never supplied by a caller.
        for name in ("workspace", "home", "cache", "session_mount"):
            path = canonical(policy_paths(p)[name])
            require(path.resolve(strict=True) == path and path.is_dir(), "fixed session mount root changed")
            controlled(path.parent)
        controlled(policy_paths(p)["session_mount"])

    def native_diagnostic(self,operation,phase,family,pid,code,data,total,error=None):
        """Native-only diagnostics, never provider stdio or admission evidence."""
        require(operation in {"broker-check","stage-check","serve"},"native diagnostic scope")
        controlled(self.state_dir)
        path=self.state_dir/("native-"+operation+".json")
        if path.exists():
            meta=controlled(path,private=True)
            require(stat.S_ISREG(meta.st_mode) and meta.st_nlink==1 and stat.S_IMODE(meta.st_mode)==0o600,"unsafe native diagnostic sink")
        record={"contract":"doxa-root-native-diagnostic-v1","session_id":self.policy["session_id"],
                "policy_sha256":self.policy_sha256,"operation":operation,"phase":phase,"family":family,"pid":pid,
                "cid":getattr(self,"manifest",None).get("cid") if getattr(self,"manifest",None) else None,
                "exit_code":code,"stderr":data[:MAX_NATIVE_STDERR].decode("utf-8","replace"),
                "stderr_bytes":total,"stderr_truncated":total>MAX_NATIVE_STDERR,"error":str(error)[:256] if error else None}
        atomic_root_write(path,compact(record)+b"\n",0o600)

    def run_native(self,command,deadline,operation,phase,family,refusal,pass_fds=()):
        output=bytearray();diagnostic=bytearray();total=0;failure=None
        with subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                              env={"PATH":"/usr/bin:/bin","LANG":"C","TMPDIR":str(self.state_dir)},
                              pass_fds=pass_fds,start_new_session=True) as child:
            pipes={child.stdout.fileno():"stdout",child.stderr.fileno():"stderr"}
            for fd in pipes:os.set_blocking(fd,False)
            try:
                while pipes:
                    deadline.check()
                    ready,_,_=select.select(list(pipes),[],[],min(.05,max(0,deadline.end-time.monotonic())))
                    for fd in ready:
                        try:chunk=os.read(fd,MAX_FRAME)
                        except BlockingIOError:continue
                        if not chunk:del pipes[fd];continue
                        if pipes[fd]=="stdout":
                            output.extend(chunk);require(len(output)<=MAX_JSON,"native authority response bound")
                        else:
                            total=min(2**63-1,total+len(chunk));diagnostic.extend(chunk[:MAX_NATIVE_STDERR-len(diagnostic)])
                deadline.check();code=child.wait(timeout=max(.001,deadline.end-time.monotonic()));deadline.check()
                require(code==0,refusal+" (exit="+str(code)+")")
            except BaseException as error:
                failure=error;kill_owned_child(child);raise
            finally:
                # Diagnostics are private and cannot weaken or replace a gate.
                try:self.native_diagnostic(operation,phase,family,child.pid,child.returncode,bytes(diagnostic),total,failure)
                except Exception:pass
        return bytes(output)

    def native_check(self, deadline, phase, family="none"):
        """No plugin or generic host RPC: exactly one hash-pinned native seam."""
        self.preflight()
        cid = self.identity()
        authority = self.policy["authority"]
        command = [authority["path"], "broker-check", "--policy", self.policy_path,
                   "--manifest", str(self.manifest_path), "--engine-pidfd", str(self.engine.pidfd),
                   "--phase", phase, "--family", family]
        self.gateway_live()
        output=self.run_native(command,deadline,"broker-check",phase,family,"native authority refused composition",(self.engine.pidfd,))
        proof = strict_json(output)
        keys(proof, {"contract", "ready", "policy_sha256", "cid", "engine_pid", "boot_id", "phase", "family",
                     "quota_ready", "filter_ready", "gateway_ready", "hook_ready"})
        require(proof["contract"] == AUTHORITY_CONTRACT and proof["ready"] is True
                and proof["policy_sha256"] == self.policy_sha256 and proof["cid"] == cid
                and proof["engine_pid"] == self.engine.pid and proof["boot_id"] == boot_id()
                and proof["phase"] == phase and proof["family"] == family
                and all(proof[field] is True for field in ("quota_ready", "filter_ready", "gateway_ready")),
                "quota/filter/gateway composition is incomplete")
        if family != "none":
            require(proof["hook_ready"] is True,
                    "root native provider hook is unavailable")
        self.engine.live()
        self.gateway_live()
        return proof

    def stage_check(self, deadline, phase, family):
        self.preflight()
        authority = self.policy["authority"]
        command = [authority["path"], "stage-check", "--policy", self.policy_path,
                   "--phase", phase, "--family", family]
        data=self.run_native(command,deadline,"stage-check",phase,family,"native quota staging gate refused")
        result = strict_json(data)
        keys(result, {"contract", "policy_sha256", "phase", "family", "quota_ready", "boot_id"})
        require(result["contract"] == "doxa-root-session-staging-v1" and result["policy_sha256"] == self.policy_sha256
                and result["phase"] == phase and result["family"] == family and result["quota_ready"] is True
                and result["boot_id"] == boot_id(), "live native quota staging proof is incomplete")

    def stage(self, client, family="none", initial=False):
        require(initial or family in self.policy["providers"], "staging provider scope refused")
        require(not initial or self.manifest is None, "initial staging requires a new session")
        self.stage_check(client.deadline, "before", family)
        result = receive_staging(self.policy, client, family, initial)
        self.stage_check(client.deadline, "after", family)
        return result

    def hook_control(self, request, deadline):
        op = request["op"]
        fields = {"v", "session_id", "op", "client_pid", "capability"} | ({"socket"} if op == "register_hook" else set())
        keys(request, fields)
        require(integer(request["client_pid"], 1, 2**31-1) and type(request["capability"]) is str
                and re.fullmatch(r"[0-9a-f]{48}", request["capability"]), "typed callback registration identity")
        if op == "register_hook":
            canonical(request["socket"])
            require(len(request["socket"].encode()) < 108, "callback Unix socket path bound")
        self.gateway_live()
        nonce = secrets.token_hex(16)
        control = {k: v for k, v in request.items() if k not in {"v"}}
        control.update(policy_sha256=self.policy_sha256, nonce=nonce)
        endpoint = self.state_dir / "gateway-status.sock"
        controlled(endpoint)
        require(stat.S_ISSOCK(endpoint.lstat().st_mode) and not endpoint.stat().st_mode & 0o077,
                "native control endpoint is not rootprivate")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(min(2, max(.001, deadline.end-time.monotonic())))
        fd = None
        try:
            sock.connect(str(endpoint))
            pid, fd = peer_pin(sock, 0, self.policy["authority"]["sha256"])
            require(pid == self.gateway.pid, "native control peer differs from owned gateway")
            channel = Channel(sock, deadline)
            raw = compact(control)
            channel.send(len(raw).to_bytes(4, "big") + raw)
            length = int.from_bytes(channel.exact(4), "big")
            require(0 < length <= MAX_JSON, "native control reply bound")
            result = strict_json(channel.exact(length))
            require(type(result) is dict and result.get("nonce") == nonce and result.get("session_id") == self.policy["session_id"]
                    and result.get("policy_sha256") == self.policy_sha256 and result.get("cid") == self.identity()
                    and result.get("hook_ready") is (op == "register_hook") and result.get("error") is None
                    and not select.select([fd], [], [], 0)[0], "native callback control acknowledgement refused")
            self.gateway_live()
            return {"protocol": PROTOCOL, "session_id": self.policy["session_id"], "cid": self.identity(),
                    "policy_sha256": self.policy_sha256, "hook_ready": result["hook_ready"], "status": "registered" if op == "register_hook" else "unregistered"}
        finally:
            sock.close()
            if fd is not None:
                os.close(fd)

    def gateway_live(self):
        require(self.gateway is not None and self.gateway.poll() is None and self.gateway_pidfd is not None
                and not select.select([self.gateway_pidfd], [], [], 0)[0], "root native gateway is unavailable")
        require(sha_file(f"/proc/{self.gateway.pid}/exe") == self.policy["authority"]["sha256"],
                "root native gateway executable drift")

    def start_gateway(self, deadline):
        if self.gateway is not None:
            self.gateway_live()
            return
        authority = self.policy["authority"]
        self.gateway_sockets={};self.gateway_parents={};self.gateway_start_error=None
        for path,_,_ in self.gateway_socket_paths():
            parent=controlled(path.parent)
            self.gateway_parents[path.parent]=(parent.st_dev,parent.st_ino,parent.st_uid,parent.st_gid,parent.st_mode)
            require(not path.exists() and not path.is_symlink(),"gateway socket requires verified prior cleanup")
        self.check_unknown_gateway_sockets()
        self.gateway = subprocess.Popen([authority["path"], "serve", "--policy", self.policy_path,
            "--manifest", str(self.manifest_path), "--engine-pidfd", str(self.engine.pidfd)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "TMPDIR": str(self.state_dir)},
            pass_fds=(self.engine.pidfd,), start_new_session=True)
        self.gateway_pidfd = os.pidfd_open(self.gateway.pid)
        self.gateway_stderr=NativeStderr(self.gateway.stderr)
        status_path = self.state_dir / "gateway-status.sock"
        # Existence is only a startup signal. Native broker-check authenticates
        # root peer pidfd, executable, fresh nonce and all current scope facts.
        try:
            while len(self.gateway_sockets)!=len(self.gateway_socket_paths()):
                deadline.check()
                self.capture_gateway_sockets()
                require(self.gateway.poll() is None, "native gateway startup failed (exit="+str(self.gateway.returncode)+")")
                select.select([self.gateway_pidfd], [], [], min(.02, max(0, deadline.end-time.monotonic())))
            self.gateway_live()
        except BaseException as error:
            self.gateway_start_error=error
            data,total=self.gateway_stderr.snapshot()
            try:self.native_diagnostic("serve","startup","none",self.gateway.pid,self.gateway.poll(),data,total,error)
            except Exception:pass
            raise

    def gateway_socket_paths(self):
        broker=Path(policy_paths(self.policy)["session_mount"])
        return [(self.state_dir/"gateway-status.sock",0,0o600),(broker/"hook.sock",self.policy["engine"]["gid"],0o660)]+[
            (broker/("egress-"+family+".sock"),self.policy["engine"]["gid"],0o660) for family in sorted(self.policy["providers"])]

    def check_unknown_gateway_sockets(self):
        broker=Path(policy_paths(self.policy)["session_mount"]);controlled(broker)
        expected={path.name for path,_,_ in self.gateway_socket_paths() if path.parent==broker}
        for path in broker.iterdir():
            meta=path.lstat()
            require(not (stat.S_ISSOCK(meta.st_mode) or path.name.endswith(".sock")) or path.name in expected,
                    "untracked gateway socket in fixed broker scope")

    def capture_gateway_sockets(self):
        require(self.gateway_pidfd is not None and not select.select([self.gateway_pidfd],[],[],0)[0],
                "gateway socket capture requires live owned PIDFD")
        self.check_unknown_gateway_sockets()
        for path,gid,mode in self.gateway_socket_paths():
            parent=controlled(path.parent)
            require((parent.st_dev,parent.st_ino,parent.st_uid,parent.st_gid,parent.st_mode)==self.gateway_parents[path.parent],
                    "gateway socket parent changed")
            try:meta=path.lstat()
            except FileNotFoundError:continue
            identity=(meta.st_dev,meta.st_ino,meta.st_uid,meta.st_gid,meta.st_mode,meta.st_nlink)
            if path in self.gateway_sockets:
                require(identity==self.gateway_sockets[path],"captured gateway socket replaced")
            elif stat.S_ISSOCK(meta.st_mode) and meta.st_uid==0 and meta.st_gid==gid and stat.S_IMODE(meta.st_mode)==mode and meta.st_nlink==1:
                self.gateway_sockets[path]=identity

    def remove_gateway_sockets(self):
        require(self.gateway_pidfd is not None and select.select([self.gateway_pidfd],[],[],0)[0],
                "gateway socket cleanup requires captured PIDFD exit")
        self.check_unknown_gateway_sockets()
        held={};pending=[]
        try:
            for path,gid,mode in self.gateway_socket_paths():
                parent=controlled(path.parent)
                expected=self.gateway_parents.get(path.parent)
                require(expected is not None and (parent.st_dev,parent.st_ino,parent.st_uid,parent.st_gid,parent.st_mode)==expected,
                        "gateway socket cleanup parent changed")
                if path.parent not in held:
                    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);held[path.parent]=fd
                    meta=os.fstat(fd);require((meta.st_dev,meta.st_ino,meta.st_uid,meta.st_gid,meta.st_mode)==expected,"gateway socket parent FD drift")
                try:meta=os.stat(path.name,dir_fd=held[path.parent],follow_symlinks=False)
                except FileNotFoundError:continue
                identity=(meta.st_dev,meta.st_ino,meta.st_uid,meta.st_gid,meta.st_mode,meta.st_nlink)
                require(path in self.gateway_sockets and identity==self.gateway_sockets[path]
                        and stat.S_ISSOCK(meta.st_mode) and meta.st_uid==0 and meta.st_gid==gid and stat.S_IMODE(meta.st_mode)==mode and meta.st_nlink==1,
                        "gateway socket cleanup found untracked/replaced endpoint")
                pending.append((path,identity))
            for path,identity in pending:
                meta=os.stat(path.name,dir_fd=held[path.parent],follow_symlinks=False)
                require((meta.st_dev,meta.st_ino,meta.st_uid,meta.st_gid,meta.st_mode,meta.st_nlink)==identity,"gateway socket changed before unlink")
                os.unlink(path.name,dir_fd=held[path.parent])
            self.gateway_sockets.clear()
        finally:
            for fd in held.values():os.close(fd)

    def close_gateway(self):
        failure=None
        if self.gateway is not None:
            try:
                if self.gateway_pidfd is not None and not select.select([self.gateway_pidfd],[],[],0)[0]:self.capture_gateway_sockets()
            except Exception as error:failure=error
            if self.gateway.poll() is None:
                try:
                    os.killpg(self.gateway.pid, signal.SIGTERM)
                    self.gateway.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    os.killpg(self.gateway.pid, signal.SIGKILL)
                    self.gateway.wait(timeout=1)
            require(self.gateway_pidfd is not None and select.select([self.gateway_pidfd],[],[],0)[0],"owned gateway PIDFD has not exited")
            if getattr(self,"gateway_stderr",None) is not None:
                data,total=self.gateway_stderr.finish();self.gateway_stderr=None
                try:self.native_diagnostic("serve","closed","none",self.gateway.pid,self.gateway.returncode,data,total,failure or self.gateway_start_error)
                except Exception:pass
            if failure is None:self.remove_gateway_sockets()
            if failure is not None:raise failure
            self.gateway = None
        if self.gateway_pidfd is not None:
            os.close(self.gateway_pidfd)
            self.gateway_pidfd = None

    def start_adapter(self, deadline, family):
        if family in self.adapters:
            return
        # Every adapter starts through the image filter too. Keeping its exact
        # upgraded FD allows bounded cleanup; container removal kills all execs.
        cid, eid, channel = self.exec_start(deadline, [WORKER, "egress-proxy-hardened", family],
            [f"DOXA_EGRESS_SOCKET=/run/doxa/session/egress-{family}.sock"])
        self.adapters[family] = (eid, channel)

    def close_adapters(self):
        for _, channel in self.adapters.values():
            channel.sock.close()
        self.adapters.clear()

    def checked_inspect(self, channel, cid, live=True):
        if self.image_labels is None:
            _, image = self.engine.exchange(channel, "GET", f"/images/{self.policy['image']}/json")
            require(image.get("Id") == self.policy["image"], "pinned image metadata identity drift")
            labels = image.get("Config", {}).get("Labels") or {}
            require(type(labels) is dict and all(type(k) is str and type(v) is str for k, v in labels.items()), "pinned image labels malformed")
            self.image_labels = labels
        _, info = self.engine.exchange(channel, "GET", f"/containers/{cid}/json")
        return inspect_exact(self.policy, self.policy_sha256, cid, info, self.image_labels, live)

    def verify_artifacts(self, deadline, cid):
        artifacts = {WORKER: self.policy["worker"]["sha256"]}
        for provider in self.policy["providers"].values():
            for path, digest in provider["artifacts"].items():
                require(path not in artifacts or artifacts[path] == digest, "artifact hash collision")
                artifacts[path] = digest
        for path, digest in artifacts.items():
            require(self.engine.archive_hash(deadline, cid, path) == digest, "immutable image artifact bytes differ")

    def describe(self):
        return {"protocol": PROTOCOL, "session_id": self.policy["session_id"], "policy_sha256": self.policy_sha256,
                "status": "unprepared" if self.manifest is None else self.manifest["status"],
                "cid": None if self.manifest is None else self.manifest["cid"],
                "admission": "requires-live-composed-native-check"}

    def container_name(self):
        return "doxa-hardened-" + self.policy["session_id"]

    def reconcile_create(self):
        """A lost create reply is reconciled by fixed name+exact scope, never guessed CID."""
        try:
            deadline = Deadline(5)
            channel = self.engine.connect(deadline)
            try:
                status, info = self.engine.exchange(channel, "GET", f"/containers/{self.container_name()}/json", statuses=(200,404))
                if status == 404:
                    return True
                cid = info.get("Id")
                require(type(cid) is str and re.fullmatch(r"[0-9a-f]{64}", cid), "reconciled CID invalid")
                self.checked_inspect(channel, cid, live=False)
                self.host_init_pid = host_init_pid(self.engine.pid, info["State"]["Pid"]) if info["State"].get("Running") else None
                self.persist(cid, "cleanup-required")
            finally:
                channel.sock.close()
            return self.cleanup(cid)
        except Exception:
            atomic_root_write(self.state_dir / "uncertain-create.json", compact({"session_id": self.policy["session_id"],
                "policy_sha256": self.policy_sha256, "name": self.container_name(), "status": "cleanup-required"})+b"\n", 0o600)
            return False

    def prepare(self, deadline):
        self.preflight()
        require(not (self.state_dir / "uncertain-create.json").exists(), "uncertain create requires root reconciliation")
        self.stage_check(deadline, "before", "none")
        require(self.manifest is None or self.manifest["status"] == "stopped", "session already has a root manifest; use resume")
        _, info = self.engine.request(deadline, "GET", "/info")
        require("name=rootless" in info.get("SecurityOptions", []), "separate Engine must be rootless")
        cid = None
        try:
            _, created = self.engine.request(deadline, "POST", "/containers/create?name=" + self.container_name(), create_spec(self.policy, self.policy_sha256), (201,))
            candidate = created.get("Id")
            require(type(candidate) is str and re.fullmatch(r"[0-9a-f]{64}", candidate), "Engine returned invalid CID")
            cid = candidate
            self.persist(cid, "preparing")
            self.engine.request(deadline, "POST", f"/containers/{cid}/start", statuses=(204,))
            channel = self.engine.connect(deadline)
            try:
                inspected = self.checked_inspect(channel, cid)
                self.host_init_pid = host_init_pid(self.engine.pid, inspected["State"]["Pid"])
                self.persist(cid, "preparing")
            finally:
                channel.sock.close()
            self.verify_artifacts(deadline, cid)
            self.run_probe(deadline, [WORKER, "probe-hardened", str(resource_limits(self.policy)["memory_bytes"]),
                                     str(resource_limits(self.policy)["nano_cpus"] // 1000000000), str(resource_limits(self.policy)["pids"])])
            filter_output = self.run_probe(deadline, [WORKER, "filter-probe"])
            filtered = strict_json(filter_output)
            require(filtered.get("contract") == "doxa-hardened-worker-v1" and all(filtered.get(name) is True
                    for name in ("quota_mutation_denied", "namespace_mutation_denied", "native_high_bits_denied", "compat_filter_present")),
                    "image worker mutation filter proof failed")
            self.start_gateway(deadline)
            for family in self.policy["providers"]:
                self.start_adapter(deadline, family)
            self.native_check(deadline, "prepare")
            self.persist(cid, "ready")
            return self.describe()
        except BaseException:
            if cid is not None:
                self.cleanup(cid)
            else:
                self.reconcile_create()
            raise

    def resume(self, deadline):
        cid = self.manifest.get("cid") if self.manifest is not None and self.manifest.get("status") != "stopped" else None
        try:
            self.preflight()
            require(self.manifest is not None and self.manifest.get("status") != "stopped", "session is not prepared")
            cid = self.manifest["cid"]
            old_engine = self.engine
            changed_engine = False
            try:
                channel = old_engine.connect(deadline)
            except (Refused, OSError):
                # Only explicit idle ROOT resume may re-pin a restarted daemon.
                # Transport paths never cross this transition.
                self.close_gateway(); self.close_adapters()
                fresh = Engine(self.policy)
                try:
                    channel = fresh.connect(deadline)
                except BaseException:
                    fresh.close()
                    raise
                self.engine = fresh
                old_engine.close()
                changed_engine = True
            changed_engine = changed_engine or self.manifest["engine_pid"] != self.engine.pid or (self.manifest["engine_socket_dev"], self.manifest["engine_socket_ino"]) != self.engine.socket_identity
            try:
                status, observed = self.engine.exchange(channel, "GET", f"/containers/{cid}/json", statuses=(200,404))
                if status == 404:
                    self.host_init_pid = None
                    require(self.cleanup(cid), "absent session helper cleanup is uncertain")
                    cid = None
                    return self.prepare(deadline)
                inspected = self.checked_inspect(channel, cid, live=False)
                running = inspected["State"].get("Running") is True
                current_init = host_init_pid(self.engine.pid, inspected["State"]["Pid"]) if running else None
                if not changed_engine and running and self.manifest["status"] == "ready":
                    require(current_init == self.manifest["host_init_pid"], "container init drift on resume")
                self.host_init_pid = current_init
                if changed_engine or self.manifest["status"] != "ready":
                    self.persist(cid, "preparing")
            finally:
                channel.sock.close()
            self.stage_check(deadline, "before", "none")
            if not running:
                require(self.cleanup(cid), "exited session scope cleanup is uncertain")
                cid = None
                return self.prepare(deadline)
            self.verify_artifacts(deadline, cid)
            cold_restart = changed_engine or self.gateway is None or set(self.adapters) != set(self.policy["providers"])
            self.start_gateway(deadline)
            self.native_check(deadline, "resume")
            if cold_restart:
                require(self.cleanup(cid), "old session scope cleanup is uncertain")
                cid = None
                return self.prepare(deadline)
            self.persist(cid, "ready")
            return self.describe()
        except BaseException:
            if cid is not None: self.cleanup(cid)
            raise

    def cleanup(self, cid):
        """Independent bounded teardown; helper failures never skip CID removal."""
        helpers_closed = True
        try:self.close_gateway()
        except Exception:helpers_closed = False
        try:self.close_adapters()
        except Exception:helpers_closed = False
        try:
            cleanup = Deadline(3)
            self.engine.request(cleanup, "DELETE", f"/containers/{cid}?force=1&v=1", statuses=(204, 404))
            self.engine.request(cleanup, "GET", f"/containers/{cid}/json", statuses=(404,))
            if helpers_closed:
                self.persist(cid, "stopped")
                return True
        except Exception:
            pass
        if self.manifest is not None:
            self.manifest["status"] = "cleanup-required"
            atomic_root_write(self.manifest_path, compact(self.manifest) + b"\n", 0o600)
        return False

    def stop(self, deadline):
        if self.manifest is None or self.manifest["status"] == "stopped":
            return self.describe()
        cid = self.manifest["cid"]
        try:
            channel = self.engine.connect(deadline)
            try:
                status, info = self.engine.exchange(channel, "GET", f"/containers/{cid}/json", statuses=(200,404))
                if status != 404:
                    self.checked_inspect(channel, cid, live=False)
                    self.engine.exchange(channel, "DELETE", f"/containers/{cid}?force=1&v=1", statuses=(204,404))
                    self.engine.exchange(channel, "GET", f"/containers/{cid}/json", statuses=(404,))
            finally:
                channel.sock.close()
            self.persist(cid, "stopped")
            self.close_gateway(); self.close_adapters()
            return self.describe()
        except BaseException:
            self.cleanup(cid)
            raise

    def exec_start(self, deadline, cmd, env=(), cwd="/workspace"):
        require(cmd[0] == WORKER and len(cmd) >= 2 and cmd[1] in {"probe-hardened", "filter-probe", "exec-hardened", "workspace-hardened", "egress-proxy-hardened"}, "mandatory wrapped exec entrypoint")
        channel = self.engine.connect(deadline)
        try:
            cid = self.identity()
            self.checked_inspect(channel, cid)
            body = {"AttachStdin": True, "AttachStdout": True, "AttachStderr": True, "Tty": False,
                    "Privileged": False, "User": "0:0", "WorkingDir": cwd, "Cmd": cmd, "Env": list(env)}
            _, created = self.engine.exchange(channel, "POST", f"/containers/{cid}/exec", body, (201,))
            eid = created.get("Id")
            require(type(eid) is str and re.fullmatch(r"[0-9a-f]{64}", eid), "Engine returned invalid exec ID")
            # Reinspect exact CID and exec config before starting on this same FD.
            self.checked_inspect(channel, cid)
            _, inspected = self.engine.exchange(channel, "GET", f"/exec/{eid}/json")
            process = inspected.get("ProcessConfig", {})
            require(inspected.get("ContainerID") == cid and inspected.get("Running") is False
                    and process.get("entrypoint") == cmd[0] and process.get("arguments") == cmd[1:]
                    and process.get("tty") is False and process.get("privileged") is False and process.get("user") == "0:0",
                    "Engine exec identity differs from mandatory wrapper")
            self.engine.send_request(channel, "POST", f"/exec/{eid}/start", {"Detach": False, "Tty": False}, upgrade=True)
            status, headers, _ = response(channel)
            require(status == 101 and headers.get("upgrade") == "tcp"
                    and headers.get("content-type") in {"application/vnd.docker.multiplexed-stream", "application/vnd.docker.raw-stream"}, "Engine did not upgrade exact exec FD")
            self.engine.live()
            return cid, eid, channel
        except BaseException:
            channel.sock.close()
            raise

    def exec_exit(self, deadline, eid):
        _, info = self.engine.request(deadline, "GET", f"/exec/{eid}/json")
        require(info.get("ContainerID") == self.identity() and info.get("Running") is False
                and integer(info.get("ExitCode"), 0, 255), "exec did not exit cleanly")
        return info["ExitCode"]

    def run_probe(self, deadline, cmd):
        cid, eid, channel = self.exec_start(deadline, cmd)
        output = bytearray()
        try:
            while True:
                deadline.wait(reads=[channel.sock])
                if not channel.sock.recv(1, socket.MSG_PEEK):
                    break
                header = channel.exact(8)
                count = int.from_bytes(header[4:], "big")
                require(header[0] in (1, 2) and header[1:4] == b"\0\0\0" and count <= MAX_FRAME, "invalid Engine stream frame")
                data = channel.exact(count)
                require(len(output) + len(data) <= MAX_JSON, "worker probe output bound")
                output.extend(data)
            require(self.exec_exit(deadline, eid) == 0, "mandatory hardened probe failed")
            return bytes(output)
        except BaseException:
            self.cleanup(cid)
            raise
        finally:
            channel.sock.close()

    def provider_snapshot(self, request, client):
        verify = request["op"] == "verify_provider_snapshot"
        keys(request, {"v", "session_id", "op", "family", "relative"} | ({"expected"} if verify else set()),
             {"thread"} | (set() if verify else {"offset", "limit"}))
        family = request["family"]
        require(family in self.policy["providers"], "snapshot provider family refused")
        parts = relative_parts(request["relative"])
        require((family == "codex" and len(parts) >= 3 and parts[0] == "codex" and parts[1] in {"sessions", "archived_sessions"})
                or (family == "claude" and len(parts) == 4 and parts[:2] == ["claude", "projects"]), "snapshot transcript prefix refused")
        require(parts[-1].endswith(".jsonl"), "snapshot requires a transcript JSONL file")
        thread = request.get("thread")
        require(thread is None or type(thread) is str and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}", thread), "snapshot thread identity")
        with self.state_lock:
            self.transport_live()
            self.stage_check(client.deadline, "before", family)
            cid = self.identity()
            quota = strict_json(read_regular(Path("/etc/doxa/quota-enforced") / (self.policy["session_id"]+".json"), root=True))
            require(quota.get("session_id") == self.policy["session_id"] and quota.get("root") == self.policy["root"]
                    and quota.get("boot_id") == boot_id() and integer(quota.get("project_id"), 1, 2**32-1), "snapshot authoritative quota scope differs")
            rootfd = os.open(policy_paths(self.policy)["home"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            parentfd = None
            fd = None
            try:
                parentfd = staging_parent(rootfd, parts)
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parentfd)
                before = os.fstat(fd)
                require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_uid == self.policy["engine"]["uid"]
                        and 0 < before.st_size <= 32*1024*1024, "actual provider source owner/type/link/size differs")
                fsx = bytearray(28)
                fcntl.ioctl(fd, 0x801c581f, fsx, True)
                require(struct.unpack_from("I", fsx, 12)[0] == quota["project_id"], "transcript escaped installed project quota")
                data = bytearray()
                while chunk := os.read(fd, MAX_FRAME):
                    client.deadline.check()
                    data.extend(chunk)
                    require(len(data) <= 32*1024*1024, "provider transcript grew beyond snapshot bound")
                after = os.fstat(fd)
                facts = lambda meta: (meta.st_dev, meta.st_ino, meta.st_size, meta.st_ctime_ns, meta.st_mtime_ns, meta.st_nlink, meta.st_uid, meta.st_mode)
                require(facts(before) == facts(after) and len(data) == before.st_size, "actual provider source changed during snapshot")
            finally:
                if fd is not None: os.close(fd)
                if parentfd is not None: os.close(parentfd)
                os.close(rootfd)
            first = bytes(data).split(b"\n", 1)[0]
            require(len(first) <= 1024*1024, "provider transcript header bound")
            header = strict_json(first)
            require(type(header) is dict, "provider transcript header object required")
            if family == "codex":
                require(header.get("type") == "session_meta" and type(header.get("payload")) is dict, "Codex session_meta header required")
                observed_thread = header["payload"].get("id")
                cwd = header["payload"].get("cwd")
            else:
                observed_thread = header.get("sessionId") or parts[-1][:-6]
                cwd = header.get("cwd")
            require(type(observed_thread) is str and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}", observed_thread) and (thread is None or observed_thread == thread), "provider header thread differs")
            require(cwd == "/workspace" or type(cwd) is str and cwd.startswith("/workspace/") and ".." not in cwd.split("/"), "provider header cwd escapes workspace")
            source = {"device": before.st_dev, "inode": before.st_ino, "size": before.st_size,
                      "ctime": before.st_ctime_ns // 1000000000, "ctime_nsec": before.st_ctime_ns % 1000000000,
                      "mtime": before.st_mtime_ns // 1000000000, "mtime_nsec": before.st_mtime_ns % 1000000000,
                      "sha256": sha(data), "relative": request["relative"], "thread": observed_thread}
            result = {"protocol": PROTOCOL, "status": "snapshot-verified" if verify else "snapshot", "session_id": self.policy["session_id"],
                      "cid": cid, "policy_sha256": self.policy_sha256, "boot_id": boot_id(), "family": family, "source": source}
            self.stage_check(client.deadline, "after", family)
            if verify:
                expected = request["expected"]
                keys(expected, {"protocol", "status", "session_id", "cid", "policy_sha256", "boot_id", "family", "source", "offset", "bytes", "chunk_sha256"})
                require(all(expected[k] == result[k] for k in ("protocol", "session_id", "cid", "policy_sha256", "boot_id", "family", "source")),
                        "original provider source identity/bytes changed after snapshot")
                result["matches"] = True
                client.json_frame(TAG_JSON, result)
                return
            offset, limit = request.get("offset", 0), request.get("limit", 32*1024*1024)
            require(integer(offset, 0, before.st_size) and integer(limit, 1, 8*1024*1024 if offset else 32*1024*1024), "snapshot byte window bound")
            selected = bytes(data[offset:offset+limit])
            result.update(offset=offset, bytes=len(selected), chunk_sha256=sha(selected))
        # Source bytes are frozen; slow client draining does not hold the state lock.
        client.json_frame(TAG_JSON, result)
        for offset in range(0, len(selected), MAX_FRAME):
            client.frame(TAG_SNAPSHOT, selected[offset:offset+MAX_FRAME])
        client.json_frame(TAG_SNAPSHOT_END, {"bytes": len(selected), "chunk_sha256": sha(selected)})

    def idle(self):
        require(not self.active_provider and self.active_workspace == 0, "session transport is active; lifecycle/staging requires idle")

    def transport_live(self):
        require(not self.closing.is_set(), "root broker is closing")
        self.gateway_live()

    def execute(self, request, client):
        p, op = self.policy, request["op"]
        extra = {"argv", "env", "cwd"} | ({"family"} if op == "provider_exec" else set())
        keys(request, {"v", "session_id", "op", "argv"} | ({"family"} if op == "provider_exec" else set()), extra | {"staging"})
        argv = request["argv"]
        require(type(argv) is list and len(argv) <= 256 and all(type(a) is str and "\x00" not in a and len(a.encode()) <= 8192 for a in argv)
                and sum(len(a.encode()) for a in argv) <= 65536, "bounded argument vector required")
        environment = request.get("env", {})
        require(type(environment) is dict and all(type(k) is str and type(v) is str and "\x00" not in v and len(v.encode()) <= 16384
                                                for k, v in environment.items()), "bounded environment required")
        require(request.get("staging", False) is False or (request.get("staging") is True and op == "provider_exec"), "staging operation scope refused")
        cwd = request.get("cwd", ".")
        require(type(cwd) is str and len(cwd) <= 4096 and (cwd == "." or bool(relative_parts(cwd))), "workspace-relative cwd required")
        family = request.get("family", "none")
        require(family == "none" if op == "workspace_exec" else family in p["providers"], "unreviewed provider family")
        if op == "provider_exec":
            require(set(environment) <= set(p["providers"][family]["environment_keys"]), "provider environment key refused")
        else:
            require(set(environment) <= {"TERM", "LANG", "LC_ALL"}, "workspace command environment refused")
            safe_git = git_arguments(argv)
        deadline = Deadline(resource_limits(p)["exec_seconds"], self.closing.is_set)
        cid = None
        active = False
        stream = None
        git_view = None
        git_acquired = False
        try:
            if op == "workspace_exec":
                while not self.git_lock.acquire(timeout=.1):deadline.check()
                git_acquired = True
            with self.state_lock:
                require(not self.closing.is_set(), "root broker is closing")
                if op == "provider_exec":
                    require(not self.active_provider, "one provider transport per session")
                    if request.get("staging") is True: self.idle()
                self.preflight()
                self.engine.connect(deadline).sock.close()
                cid = self.identity()
                require(self.manifest["status"] == "ready", "session admission is not ready")
                require(family == "none" if op == "workspace_exec" else family in p["providers"], "unreviewed provider family")
                if op == "provider_exec":
                    require(not self.active_provider, "one provider transport per session")
                    if request.get("staging") is True:
                        self.idle()
                        client.deadline = deadline
                        self.stage(client, family)
                if op == "provider_exec":
                    self.native_check(deadline, op, family)
                    provider = p["providers"][family]
                    require(set(environment) <= set(provider["environment_keys"]), "provider environment key refused")
                    port = provider["proxy_port"]
                    cmd = [WORKER, "exec-hardened", provider["path"], *argv]
                    env = [f"{k}={v}" for k, v in environment.items()]
                    env += [f"{key}=http://127.0.0.1:{port}" for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")]
                    env += ["NO_PROXY=", "no_proxy=", "CLAUDE_CONFIG_DIR=/home/doxa/claude" if family == "claude" else "CODEX_HOME=/home/doxa/codex"]
                    env += ["HOME=/home/doxa", "TMPDIR=/work-cache/tmp", "PATH=/usr/local/bin:/usr/bin:/bin"]
                    _, eid, stream = self.exec_start(deadline, cmd, env, "/workspace" + ("" if cwd == "." else "/" + cwd))
                    self.active_provider = True
                else:
                    self.stage_check(deadline,"before","none")
                    scope_engine=self.engine
                    self.active_workspace += 1
                active = True
            if op == "workspace_exec":
                deadline.cancelled=lambda:self.closing.is_set() or self.manifest is None or self.manifest.get("cid")!=cid or self.manifest.get("status")!="ready"
                def same_scope():
                    require(self.engine is scope_engine and self.identity()==cid and self.manifest["status"]=="ready",
                            "workspace scope changed during private build")
                @contextmanager
                def publication_scope():
                    with self.state_lock:
                        same_scope()
                        # Native shared quota audit precedes publication EX lock:
                        # state -> native/shared -> EX, never EX -> state.
                        self.native_check(deadline,op,family)
                        yield
                git_view,worker_git_view=build_git_view(p,deadline,publication_scope)
                with self.state_lock:
                    same_scope()
                    self.stage_check(deadline,"after","none")
                    self.native_check(deadline,op,family)
                    cmd=[WORKER,"workspace-hardened",*safe_git]
                    env=[f"{k}={v}" for k,v in environment.items()]
                    env += [f"{k}={v}" for k,v in git_environment(worker_git_view).items()]
                    env += ["HOME=/home/doxa","TMPDIR=/work-cache/tmp","PATH=/usr/local/bin:/usr/bin:/bin"]
                    _,eid,stream=self.exec_start(deadline,cmd,env,"/workspace"+("" if cwd=="." else "/"+cwd))
            client.deadline = deadline
            client.json_frame(TAG_JSON, {"protocol": PROTOCOL, "status": "exec-ready", "cid": cid, "exec_id": eid})
            stream_stdio(self.engine, stream, client, resource_limits(p), self.transport_live)
            status = self.exec_exit(deadline, eid)
            client.json_frame(TAG_EXIT, {"exit_code": status, "cid": cid, "exec_id": eid})
        except BaseException:
            if cid is not None:
                with self.state_lock:
                    self.cleanup(cid)
            raise
        finally:
            if stream is not None: stream.sock.close()
            if git_view is not None:
                try:retire_git_view(p,git_view,Deadline(3))
                except Exception:
                    with self.state_lock:
                        if cid is not None:self.cleanup(cid)
            if git_acquired: self.git_lock.release()
            if active:
                with self.state_lock:
                    if op == "provider_exec": self.active_provider = False
                    else: self.active_workspace -= 1


def check_git_lock(fd,path):
    controlled(path,private=True)
    info=os.fstat(fd);current=path.lstat()
    identity=lambda m:(m.st_dev,m.st_ino,m.st_uid,m.st_gid,m.st_mode,m.st_nlink,m.st_size,m.st_mtime_ns,m.st_ctime_ns)
    require(stat.S_ISREG(info.st_mode) and info.st_uid==0 and info.st_gid==0 and info.st_nlink==1 and info.st_size==0
            and stat.S_IMODE(info.st_mode)==0o600 and identity(info)==identity(current),
            "Git metadata lock is outside ROOT control")
    return identity(info)


def open_git_metadata_lock(policy):
    require(os.geteuid()==0,"Git metadata lock requires ROOT")
    path=Path(policy_paths(policy)["state_dir"])/"git-metadata.lock"
    controlled(path.parent)
    try:
        fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
        os.fchown(fd,0,0);os.fchmod(fd,0o600)
    except FileExistsError:
        fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:check_git_lock(fd,path)
    except BaseException:os.close(fd);raise
    return fd


@contextmanager
def git_metadata_lock(policy,deadline,exclusive=True):
    fd=open_git_metadata_lock(policy)
    path=Path(policy_paths(policy)["state_dir"])/"git-metadata.lock"
    try:
        held=check_git_lock(fd,path)
        while True:
            deadline.check()
            try:fcntl.flock(fd,(fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)|fcntl.LOCK_NB);break
            except BlockingIOError:select.select([],[],[],min(.02,max(0,deadline.end-time.monotonic())))
        require(check_git_lock(fd,path)==held,"Git metadata lock changed during bounded wait")
        try:yield
        finally:require(check_git_lock(fd,path)==held,"Git metadata lock changed during operation")
    finally:os.close(fd)


def git_project(fd):
    fsx=bytearray(28);fcntl.ioctl(fd,0x801c581f,fsx,True)
    return struct.unpack_from("I",fsx,12)[0],bool(struct.unpack_from("I",fsx,0)[0]&0x200)


def private_git_stage(policy,deadline=None):
    root=Path(policy["root"]);controlled(root)
    stage=root/".git-staging"
    if not stage.exists():
        with git_metadata_lock(policy,deadline or Deadline(3)):
            try:
                stage.mkdir(mode=0o700);os.chown(stage,0,0);stage.chmod(0o700)
            except FileExistsError:pass
    controlled(stage,private=True);info=stage.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid==0 and info.st_gid==0 and stat.S_IMODE(info.st_mode)==0o700,
            "Git staging directory is outside ROOT private scope")
    rootfd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    stagefd=os.open(stage,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        expected=git_project(rootfd)
        require(git_project(stagefd)==expected and os.fstat(stagefd).st_dev==os.fstat(rootfd).st_dev,
                "Git staging escaped inherited quota project/filesystem")
    finally:os.close(stagefd);os.close(rootfd)
    return stage,expected


def retire_git_view(policy,view,deadline):
    stage,_=private_git_stage(policy,deadline)
    safe=Path(policy_paths(policy)["session_mount"])/"git-safe"
    require(view.parent==safe and re.fullmatch(r"[0-9a-f]{32}",view.name),"Git retirement scope differs")
    retired=stage/("retired-"+secrets.token_hex(16))
    with git_metadata_lock(policy,deadline):
        require(view.is_dir() and not view.is_symlink(),"Git published view is unavailable")
        os.rename(view,retired)
    # The audited tree cannot observe recursive deletion. All retired bytes
    # remain charged to the same project until this ROOT-only removal completes.
    shutil.rmtree(retired)


def build_git_view(policy, deadline, publication_scope=None):
    """Build privately; atomically publish immutable metadata under audit lock."""
    require(os.geteuid() == 0, "Git metadata view requires ROOT")
    paths = policy_paths(policy)
    broker_path = Path(paths["session_mount"]);controlled(broker_path)
    stage,expected_project=private_git_stage(policy,deadline)
    safe=broker_path/"git-safe"
    retired=[]
    with git_metadata_lock(policy,deadline):
        if safe.exists():
            info=safe.lstat()
            require(stat.S_ISDIR(info.st_mode) and info.st_uid==0 and info.st_gid==policy["engine"]["gid"]
                    and stat.S_IMODE(info.st_mode) in {0o550,0o750},"Git metadata root is outside ROOT control")
            for child in safe.iterdir():
                require(re.fullmatch(r"[0-9a-f]{32}",child.name) and child.is_dir() and not child.is_symlink(),"unexpected Git metadata generation")
                target=stage/("retired-"+secrets.token_hex(16));os.rename(child,target);retired.append(target)
        else:
            top=stage/("safe-root-"+secrets.token_hex(16));top.mkdir(mode=0o700)
            os.chown(top,0,policy["engine"]["gid"]);top.chmod(0o750);os.rename(top,safe)
    for child in retired:shutil.rmtree(child)
    generation = secrets.token_hex(16)
    destination = stage / generation
    destination.mkdir(mode=0o700)
    os.chown(destination,0,policy["engine"]["gid"])
    cap = min(policy["limits"]["disk_bytes"],128*1024*1024)
    count, total = 1, 0
    rootfd = srcfd = dstfd = None
    head = packed = None
    def account(depth, size=0):
        nonlocal count,total
        count += 1; total += size
        require(count <= 4096 and depth <= 24 and total <= cap, "Git metadata snapshot resource bound")
        deadline.check()
    def write_bytes(parentfd,name,data,depth):
        account(depth,len(data))
        fd = os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parentfd)
        try:
            require(git_project(fd)[0]==expected_project[0],"Git staging file escaped project quota")
            os.fchown(fd,0,policy["engine"]["gid"])
            view = memoryview(data)
            while view:
                deadline.check(); written=os.write(fd,view);require(written>0,"Git metadata write failed");view=view[written:]
            os.fsync(fd);os.fchmod(fd,0o440)
        finally: os.close(fd)
    def copy_file(source,parentfd,name,depth,maximum=None):
        before = os.fstat(source)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= (maximum or cap), "Git metadata regular single-link file required")
        account(depth,before.st_size)
        fd = os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parentfd)
        observed=0
        try:
            require(git_project(fd)[0]==expected_project[0],"Git staging copy escaped project quota")
            os.fchown(fd,0,policy["engine"]["gid"])
            while chunk := os.read(source,MAX_FRAME):
                deadline.check(); observed += len(chunk);require(observed <= before.st_size,"Git metadata file grew")
                view=memoryview(chunk)
                while view:
                    written=os.write(fd,view);require(written>0,"Git metadata copy failed");view=view[written:]
            after=os.fstat(source)
            identity=lambda m:(m.st_dev,m.st_ino,m.st_size,m.st_mtime_ns,m.st_ctime_ns,m.st_nlink)
            require(observed==before.st_size and identity(before)==identity(after),"Git metadata changed during copy")
            os.fsync(fd);os.fchmod(fd,0o440)
        finally: os.close(fd)
    def directory(parentfd,name,depth):
        account(depth)
        os.mkdir(name,0o700,dir_fd=parentfd)
        fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parentfd)
        try:
            require(git_project(fd)==expected_project,"Git staging directory escaped quota inheritance")
            os.fchown(fd,0,policy["engine"]["gid"])
        except BaseException:os.close(fd);raise
        return fd
    def tree(source,target,prefix,depth):
        before=os.fstat(source)
        for name in sorted(os.listdir(source)):
            require(name not in {".",".."} and len(name.encode()) <= 255,"Git metadata component bound")
            relative=prefix+"/"+name
            if relative in {"objects/info/alternates","objects/info/http-alternates"}:continue
            meta=os.stat(name,dir_fd=source,follow_symlinks=False)
            if stat.S_ISDIR(meta.st_mode):
                child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=source)
                output=directory(target,name,depth)
                try:tree(child,output,relative,depth+1)
                finally:os.fchmod(output,0o550);os.fsync(output);os.close(output);os.close(child)
            else:
                require(stat.S_ISREG(meta.st_mode),"Git metadata refuses symlinks/specials")
                child=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=source)
                try:copy_file(child,target,name,depth)
                finally:os.close(child)
        after=os.fstat(source)
        require((before.st_dev,before.st_ino,before.st_mtime_ns,before.st_ctime_ns)==(after.st_dev,after.st_ino,after.st_mtime_ns,after.st_ctime_ns),"Git metadata directory changed")
    def optional_file(name,maximum=None):
        try:fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=srcfd)
        except FileNotFoundError:return
        try:copy_file(fd,dstfd,name,2,maximum)
        finally:os.close(fd)
    try:
        rootfd=os.open(paths["workspace"],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        srcfd=os.open(".git",os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=rootfd)
        dstfd=os.open(destination,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        require(git_project(dstfd)==expected_project and os.fstat(dstfd).st_dev==safe.stat().st_dev,"Git generation quota/filesystem differs")
        headfd=os.open("HEAD",os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=srcfd)
        try:
            headmeta=os.fstat(headfd)
            require(stat.S_ISREG(headmeta.st_mode) and headmeta.st_nlink==1 and headmeta.st_size<=4096,"Git HEAD bound")
            head=os.read(headfd,4097)
            after=os.fstat(headfd)
            require(len(head)==headmeta.st_size and (headmeta.st_dev,headmeta.st_ino,headmeta.st_size,headmeta.st_mtime_ns,headmeta.st_ctime_ns)==
                    (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns),"Git HEAD changed during read")
        finally:os.close(headfd)
        require(re.fullmatch(b"(?:[0-9a-f]{40}|[0-9a-f]{64}|ref: refs/(?:heads|tags)/[A-Za-z0-9_./-]+)\n?",head),"Git HEAD syntax refused")
        write_bytes(dstfd,"HEAD",head,2)
        optional_file("index");optional_file("shallow",8*1024*1024)
        try:packfd=os.open("packed-refs",os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=srcfd)
        except FileNotFoundError:pass
        else:
            try:
                meta=os.fstat(packfd)
                require(stat.S_ISREG(meta.st_mode) and meta.st_nlink==1 and meta.st_size<=8*1024*1024,"packed refs bound")
                packed=os.read(packfd,8*1024*1024+1)
                after=os.fstat(packfd)
                require(len(packed)==meta.st_size and (meta.st_dev,meta.st_ino,meta.st_size,meta.st_mtime_ns,meta.st_ctime_ns)==
                        (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns),"packed refs changed during read")
            finally:os.close(packfd)
            rows=[];accept_peeled=False
            for row in packed.splitlines():
                if row.startswith(b"#"):continue
                if re.fullmatch(b"(?:[0-9a-f]{40}|[0-9a-f]{64}) refs/(?:heads|tags)/[A-Za-z0-9_./-]+",row):
                    require(b".." not in row.split(b" ",1)[1].split(b"/"),"packed ref component escapes")
                    rows.append(row);accept_peeled=True
                elif row.startswith(b"^") and accept_peeled:
                    require(re.fullmatch(b"[\x5e](?:[0-9a-f]{40}|[0-9a-f]{64})",row),"peeled ref syntax")
                    rows.append(row);accept_peeled=False
                else:accept_peeled=False
            write_bytes(dstfd,"packed-refs",b"\n".join(rows)+b"\n",2)
        for name in ("objects","refs"):
            try:source=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=srcfd)
            except FileNotFoundError:
                require(name=="refs","Git object database is missing")
                output=directory(dstfd,name,2);os.fchmod(output,0o550);os.close(output);continue
            output=directory(dstfd,name,2)
            try:
                if name=="objects":tree(source,output,name,3)
                else:
                    for selected in ("heads","tags"):
                        try:child=os.open(selected,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=source)
                        except FileNotFoundError:continue
                        out=directory(output,selected,3)
                        try:tree(child,out,"refs/"+selected,4)
                        finally:os.fchmod(out,0o550);os.close(out);os.close(child)
            finally:os.fchmod(output,0o550);os.fsync(output);os.close(output);os.close(source)
        sha256_format=bool(re.fullmatch(b"[0-9a-f]{64}\n?",head)) or bool(packed and re.search(b"(?:^|\n)[0-9a-f]{64} refs/",packed))
        if head.startswith(b"ref: "):
            selected=head[5:].strip().decode();parts=relative_parts(selected)
            parent=staging_parent(dstfd,parts)
            try:
                try:fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
                except FileNotFoundError:pass
                else:
                    try:sha256_format=bool(re.fullmatch(b"[0-9a-f]{64}\n?",os.read(fd,129)))
                    finally:os.close(fd)
            finally:os.close(parent)
        config=b"[core]\nrepositoryformatversion = "+(b"1" if sha256_format else b"0")+b"\nbare = false\nworktree = /workspace\n[submodule]\nrecurse = false\n"
        if sha256_format:config+=b"[extensions]\nobjectFormat = sha256\n"
        write_bytes(dstfd,"config",config,2)
        infofd=directory(dstfd,"info",2)
        try:write_bytes(infofd,"attributes",b"",3)
        finally:os.fchmod(infofd,0o550);os.fsync(infofd);os.close(infofd)
        os.fchmod(dstfd,0o550);os.fsync(dstfd)
        published=safe/generation
        with publication_scope() if publication_scope is not None else nullcontext():
            with git_metadata_lock(policy,deadline):os.rename(destination,published)
        return published,"/run/doxa/session/git-safe/"+generation
    except BaseException:
        shutil.rmtree(destination,ignore_errors=True)
        raise
    finally:
        for fd in (dstfd,srcfd,rootfd):
            if fd is not None:os.close(fd)


def git_environment(view):
    """Exact ROOT-generated Git inputs; never inherited from provider/caller."""
    return {"GIT_CONFIG_NOSYSTEM":"1","GIT_CONFIG_SYSTEM":"/dev/null","GIT_CONFIG_GLOBAL":"/dev/null",
            "GIT_TERMINAL_PROMPT":"0","GIT_ASKPASS":"/bin/false","GIT_ALLOW_PROTOCOL":"",
            "GIT_ATTR_NOSYSTEM":"1","GIT_NO_REPLACE_OBJECTS":"1","GIT_OPTIONAL_LOCKS":"0",
            "GIT_CONFIG_COUNT":"0","GIT_CONFIG_PARAMETERS":"","GIT_DIR":view,"GIT_WORK_TREE":"/workspace"}


def git_arguments(argv):
    """Only reviewed builtins/grammars; user aliases/global exec options denied."""
    no_pager = argv[:1] == ["--no-pager"]
    if no_pager: argv = argv[1:]
    require(argv and argv[0] in {"status", "diff", "rev-parse", "merge-base", "ls-files", "apply"}, "workspace Git builtin refused")
    command = argv[0]
    flags = {
        "status": {"--porcelain", "--porcelain=v1", "--porcelain=v2", "--short", "-s", "--branch", "-b", "-z",
                   "--untracked-files=all", "--untracked-files=normal", "--untracked-files=no"},
        "diff": {"--no-ext-diff", "--no-textconv", "--binary", "--name-only", "--name-status", "--numstat", "--stat",
                 "--shortstat", "--patch", "-p", "--raw", "--cached", "--staged", "--no-color", "--color=never", "--no-renames", "--find-renames", "-z"},
        "rev-parse": {"HEAD", "--verify", "--quiet", "-q", "--show-toplevel", "--show-prefix", "--show-cdup", "--abbrev-ref", "--git-dir", "--is-inside-work-tree"},
        "merge-base": {"--all", "-a", "--is-ancestor", "--fork-point"},
        "ls-files": {"--others", "--exclude-standard", "--cached", "--modified", "--deleted", "--stage", "--unmerged", "--error-unmatch", "-z"},
        "apply": {"--reverse", "-R", "--recount", "--check", "--cached", "--index", "--binary", "--stat", "--numstat", "--summary", "--whitespace=nowarn", "--whitespace=error", "-"}}
    after_separator = False
    for arg in argv[1:]:
        if arg == "--":
            require(not after_separator, "duplicate Git path separator")
            after_separator = True
            continue
        if arg.startswith("-") and not after_separator:
            allowed = arg in flags[command] or (command == "diff" and re.fullmatch(r"(?:-U|--unified=)[0-9]{1,4}", arg))
            require(allowed, "workspace Git option refused")
        else:
            require(not arg.startswith("/") and "\x00" not in arg and ".." not in arg.split("/")
                    and not arg.startswith(("-", ":")), "workspace Git revision/path refused")
    require(command != "apply" or not set(argv[1:]) & {"--cached","--index"}, "Git snapshot index mutation is unavailable")
    overrides = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "credential.helper=",
                 "-c", "diff.external=", "-c", "core.attributesFile=/dev/null", "-c", "core.pager=cat", "-c", "core.quotepath=false",
                 "-c", "safe.directory=/workspace"]
    enforced = ["--no-ext-diff", "--no-textconv"] if command == "diff" else []
    if command in {"status","diff"}: enforced += ["--ignore-submodules=all"]
    return [*overrides, *(["--no-pager"] if no_pager else []), command, *enforced, *argv[1:]]


def stream_stdio(engine, upstream, downstream, resources, live_guard=lambda: None):
    """Bounded queues + one deadline; disconnect/cancel kills entire scope."""
    incoming, engine_data = bytearray(), bytearray()
    to_engine, to_client = bytearray(), bytearray()
    output_count = input_count = 0
    stdin_closed = False
    shutdown_sent = False
    max_queue = 4 * MAX_FRAME
    deadline = upstream.deadline
    while True:
        deadline.check()
        engine.live()
        live_guard()
        reads = []
        if len(to_client) < max_queue:
            reads.append(upstream.sock)
        if len(to_engine) < max_queue:
            reads.append(downstream.sock)
        writes = [s for s, pending in ((upstream.sock, to_engine), (downstream.sock, to_client)) if pending]
        readable, writable, _ = deadline.wait(reads, writes)
        for sock in writable:
            queue = to_engine if sock is upstream.sock else to_client
            try:
                sent = sock.send(queue)
            except BlockingIOError:
                continue
            require(sent > 0, "stdio peer closed")
            del queue[:sent]
        if downstream.sock in readable:
            data = downstream.sock.recv(MAX_FRAME)
            require(data, "client disconnected; exec cancelled")
            incoming.extend(data)
            while len(incoming) >= 5:
                tag, size = incoming[0], int.from_bytes(incoming[1:5], "big")
                require(size <= MAX_FRAME, "stdin frame bound")
                if len(incoming) < size + 5:
                    break
                payload = bytes(incoming[5:size+5])
                del incoming[:size+5]
                require(tag in (TAG_STDIN, TAG_EOF, TAG_CANCEL), "unknown stdio control frame")
                if tag == TAG_CANCEL:
                    require(size == 0, "cancel payload forbidden")
                    raise Refused("client cancelled exec")
                if tag == TAG_EOF:
                    require(size == 0 and not stdin_closed, "duplicate/invalid stdin EOF")
                    stdin_closed = True
                else:
                    require(not stdin_closed, "stdin after EOF")
                    input_count += size
                    require(input_count <= resources["stdin_bytes"], "stdin budget exceeded")
                    to_engine.extend(payload)
                    require(len(to_engine) <= max_queue + MAX_FRAME, "stdin backpressure bound")
        if stdin_closed and not to_engine and not shutdown_sent:
            upstream.sock.shutdown(socket.SHUT_WR)
            shutdown_sent = True
        if upstream.sock in readable:
            data = upstream.sock.recv(MAX_FRAME)
            if not data:
                require(not engine_data, "truncated Engine multiplexed frame")
                while to_client:
                    deadline.wait(writes=[downstream.sock])
                    sent = downstream.sock.send(to_client)
                    require(sent > 0, "client closed while draining output")
                    del to_client[:sent]
                return
            engine_data.extend(data)
            while len(engine_data) >= 8:
                header = engine_data[:8]
                count = int.from_bytes(header[4:], "big")
                require(header[0] in (1, 2) and header[1:4] == b"\0\0\0" and count <= MAX_FRAME, "Engine output frame bound")
                if len(engine_data) < count+8:
                    break
                payload = bytes(engine_data[8:count+8])
                del engine_data[:count+8]
                output_count += count
                require(output_count <= resources["output_bytes"], "stdout/stderr budget exceeded")
                tag = TAG_STDOUT if header[0] == 1 else TAG_STDERR
                to_client.extend(bytes([tag]) + count.to_bytes(4, "big") + payload)
                require(len(to_client) <= max_queue + MAX_FRAME + 5, "output backpressure bound")


def install(source, destination):
    require(os.geteuid() == 0, "only root may install policy")
    p = validate_policy(strict_json(read_regular(source, root=True)))
    destination = canonical(str(destination))
    controlled(destination.parent)
    require(not destination.exists(), "installed session policy is immutable; replace with a new reviewed session")
    controlled(policy_paths(p)["state_dir"])
    lock=open_git_metadata_lock(p)
    os.close(lock)
    for name in ("socket", "receipt"):
        controlled(Path(policy_paths(p)[name]).parent)
        require(not Path(policy_paths(p)[name]).exists(), "client endpoint/receipt must be fresh")
    for name in ("authority", "broker"):
        controlled(p[name]["path"])
        require(sha_file(p[name]["path"]) == p[name]["sha256"], "installed root authority hash differs")
    controlled(p["engine"]["exe_path"])
    require(sha_file(p["engine"]["exe_path"]) == p["engine"]["exe_sha256"], "Engine binary pin differs")
    require(sha(read_regular(p["engine"]["config_path"], root=True, readonly=True)) == p["engine"]["config_sha256"], "Engine config pin differs")
    data = compact(p) + b"\n"
    atomic_root_write(destination, data, 0o444)
    interpreter = str(Path(sys.executable).resolve(strict=True))
    script = str(Path(__file__).resolve(strict=True))
    controlled(interpreter)
    controlled(script)
    receipt = {"contract": PROTOCOL, "session_id": p["session_id"], "caller_uid": p["caller_uid"], "broker_uid": 0,
               "broker_socket": policy_paths(p)["socket"], "policy_path": str(destination), "policy_sha256": sha(data),
               "broker_script": p["broker"]["path"], "broker_sha256": p["broker"]["sha256"], "broker_exe": interpreter,
               "broker_exe_sha256": sha_file(interpreter), "boot_id": boot_id()}
    atomic_root_write(policy_paths(p)["receipt"], compact(receipt) + b"\n", 0o444)
    return receipt


def publish_instance(broker, identity):
    own_pidfd = os.pidfd_open(os.getpid())
    try:
        own_info = dict(line.split(":",1) for line in Path(f"/proc/self/fdinfo/{own_pidfd}").read_text().splitlines() if ":" in line)
        argv = [item.decode() for item in Path("/proc/self/cmdline").read_bytes().rstrip(b"\0").split(b"\0")]
        require(argv[1:] == ["-I",str(Path(__file__).resolve()),"serve","--policy",broker.policy_path], "ROOT broker must execute exact isolated argv")
        instance = {"contract":"doxa-root-broker-instance-v1","session_id":broker.policy["session_id"],"pid":os.getpid(),
                    "pidfd_pid":int(own_info["Pid"]),"start_time":process_start_time(os.getpid()),"boot_id":boot_id(),
                    "socket_dev":identity[0],"socket_ino":identity[1],"exe_sha256":sha_file("/proc/self/exe"),
                    "script_sha256":sha_file(__file__),"policy_sha256":broker.policy_sha256,"argv":argv}
        instance_raw = compact(instance)+b"\n"
        atomic_root_write(broker.state_dir/"broker-instance.json",instance_raw,0o444)
    finally:
        os.close(own_pidfd)
    return instance, instance_raw


def serve(policy_path):
    require(os.geteuid() == 0, "broker requires root authority")
    controlled(str(Path(__file__).resolve()))
    broker = Broker(policy_path)
    endpoint = Path(policy_paths(broker.policy)["socket"])
    controlled(endpoint.parent)
    lockfd = os.open(broker.state_dir / "broker.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if endpoint.exists():
        meta = endpoint.lstat()
        require(stat.S_ISSOCK(meta.st_mode) and meta.st_uid == 0, "broker endpoint was replaced")
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.settimeout(.2)
        try:
            stale.connect(str(endpoint))
        except ConnectionRefusedError:
            endpoint.unlink()
        else:
            raise Refused("broker endpoint is already listening")
        finally:
            stale.close()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(endpoint))
    endpoint.chmod(0o600)
    os.chown(endpoint, 0, pwd.getpwuid(broker.policy["caller_uid"]).pw_gid)
    endpoint.chmod(0o660)
    listener.listen(4)
    listener.settimeout(.2)
    identity = (endpoint.stat().st_dev, endpoint.stat().st_ino)
    instance, instance_raw = publish_instance(broker, identity)
    stopping = False
    slots = threading.BoundedSemaphore(MAX_CONTROLS)
    workers = set()
    workers_lock = threading.Lock()
    def stop(*_):
        nonlocal stopping
        stopping = True
        broker.closing.set()
    def handle(sock):
        try:
            with sock:
                client = Channel(sock, Deadline(resource_limits(broker.policy)["operation_seconds"], broker.closing.is_set))
                try:
                    _, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    require(uid == broker.policy["caller_uid"], "client identity differs from installed session")
                    length = int.from_bytes(client.exact(4), "big")
                    require(0 < length <= MAX_JSON, "request bound")
                    request = strict_json(client.exact(length))
                    require(type(request) is dict and type(request.get("v")) is int and request.get("v") == 1
                            and request.get("session_id") == broker.policy["session_id"] and request.get("op") in OPS | {"attest"},
                            "unscoped/unknown operation")
                    if request.get("op") == "attest":
                        keys(request,{"v","session_id","op","nonce"})
                        require(type(request["nonce"]) is str and re.fullmatch(r"[0-9a-f]{32}",request["nonce"]),"ROOT nonce challenge bound")
                        client.json_frame(TAG_JSON,{"protocol":PROTOCOL,"status":"root-attested","session_id":broker.policy["session_id"],
                            "nonce":request["nonce"],"pid":os.getpid(),"start_time":instance["start_time"],
                            "policy_sha256":broker.policy_sha256,"instance_sha256":sha(instance_raw)})
                        length = int.from_bytes(client.exact(4),"big")
                        require(0 < length <= MAX_JSON,"request bound")
                        request = strict_json(client.exact(length))
                        require(type(request) is dict and type(request.get("v")) is int and request["v"]==1
                                and request.get("session_id")==broker.policy["session_id"] and request.get("op") in OPS,"unscoped/unknown operation")
                    op = request["op"]
                    if op in {"provider_exec", "workspace_exec"}:
                        broker.execute(request, client)
                    elif op in {"provider_snapshot", "verify_provider_snapshot"}:
                        broker.provider_snapshot(request, client)
                    elif op in {"register_hook", "unregister_hook"}:
                        with broker.state_lock:
                            client.json_frame(TAG_JSON, broker.hook_control(request, client.deadline))
                    else:
                        keys(request, {"v", "session_id", "op"}, {"staging"} if op == "prepare" else ())
                        require(type(request.get("staging", False)) is bool, "staging flag must be boolean")
                        with broker.state_lock:
                            if op not in {"describe","guard"}: broker.idle()
                            if request.get("staging") is True: broker.stage(client, initial=True)
                            deadline = client.deadline
                            deadline.cancelled = lambda: broker.closing.is_set() or bool(select.select([sock], [], [], 0)[0])
                            result = broker.describe() if op == "describe" else getattr(broker, op)(deadline)
                        client.json_frame(TAG_JSON, result)
                except Exception as error:
                    try:
                        client.deadline = Deadline(1)
                        client.json_frame(TAG_ERROR, {"error": str(error)[:256], "status": broker.describe()["status"]})
                    except Exception:
                        pass
        finally:
            slots.release()
            with workers_lock:
                workers.discard(threading.current_thread())
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopping:
            try:
                sock, _ = listener.accept()
            except socket.timeout:
                continue
            if not slots.acquire(blocking=False):
                sock.close()
                continue
            worker = threading.Thread(target=handle, args=(sock,), daemon=True)
            with workers_lock: workers.add(worker)
            worker.start()
    finally:
        broker.closing.set()
        listener.close()
        with workers_lock: remaining = list(workers)
        stop_at = time.monotonic()+6
        for worker in remaining: worker.join(max(0, stop_at-time.monotonic()))
        with broker.state_lock:
            if broker.manifest is not None and broker.manifest.get("status") != "stopped":
                broker.cleanup(broker.manifest["cid"])
        (broker.state_dir/"broker-instance.json").unlink(missing_ok=True)
        if endpoint.exists() and (endpoint.stat().st_dev, endpoint.stat().st_ino) == identity:
            endpoint.unlink()
        broker.engine.close()
        os.close(lockfd)


def relative_parts(value):
    require(type(value) is str and 0 < len(value.encode()) <= 4096 and not value.startswith("/")
            and "\x00" not in value, "staging relative path required")
    parts = value.split("/")
    require(len(parts) <= 128 and all(part not in {"", ".", ".."} and len(part.encode()) <= 255 for part in parts),
            "staging path escapes fixed scope")
    return parts


def staging_parent(rootfd, parts):
    fd = os.dup(rootfd)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def home_entry_policy(parts, kind, size):
    require(parts[0] in {"claude", "codex"}, "selected provider home prefix required")
    if kind == "directory":
        require(parts == ["claude"] or parts == ["codex"] or
                (parts[0] == "claude" and len(parts) <= 24 and parts[1] in {"skills", "adopted-plugins"}
                 and not set(parts[2:]) & {".git", "hooks"}), "provider input directory is outside fixed inventory")
        return "directory"
    relative = "/".join(parts)
    if relative in {"claude/.credentials.json", "codex/auth.json"}:
        require(size <= 1024*1024, "credential file exceeds 1 MiB")
        return "preserve-auth"
    if relative == "claude/settings.json":
        require(size <= 65536, "Claude settings exceeds 64 KiB")
        return "replace-config"
    if relative == "claude/.doxa-logged-out":
        require(size <= 1024, "logout marker bound")
        return "create"
    require(parts[0] == "claude" and len(parts) >= 3 and len(parts) <= 24 and parts[1] in {"skills", "adopted-plugins"}
            and not set(parts[2:]) & {".git", "hooks", ".mcp.json", "mcp.json"} and size <= 4*1024*1024,
            "provider home file is outside fixed prepared inventory")
    return "create"


def receive_staging(policy, channel, family, initial):
    paths = policy_paths(policy)
    roots = {"checkout": paths["workspace"], "home": paths["home"]}
    allowed_home = {"claude"} if family == "claude" else {"codex"} if family == "codex" else set(policy["providers"])
    rootfds = {scope: os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW) for scope, path in roots.items()}
    seen, created, total = set(), [], 0
    config_updates = []
    snapshot_bytes = {}
    inventory = hashlib.sha256(b"doxa-staging-v1\0")
    try:
        if initial:
            require(not os.listdir(rootfds["checkout"]), "new checkout staging root must be empty")
        while True:
            tag, payload = channel.receive_frame()
            if tag == TAG_CANCEL:
                require(not payload, "staging cancel payload")
                raise Refused("client cancelled staging")
            if tag == TAG_STAGED:
                end = strict_json(payload)
                keys(end, {"entry_count", "tree_sha256"})
                require(end["entry_count"] == len(seen) and end["tree_sha256"] == inventory.hexdigest(), "staging inventory digest differs")
                for parent, temporary, target in config_updates:
                    os.replace(temporary, target, src_dir_fd=parent, dst_dir_fd=parent)
                    os.fsync(parent)
                return {"entry_count": len(seen), "bytes": total, "tree_sha256": inventory.hexdigest()}
            require(tag == TAG_ENTRY, "expected typed staging entry")
            entry = strict_json(payload)
            keys(entry, {"scope", "relative_path", "kind", "mode", "size", "sha256"})
            scope, kind = entry["scope"], entry["kind"]
            require(scope in roots and (initial or scope == "home") and kind in {"file", "directory"}, "typed staging scope/kind refused")
            parts = relative_parts(entry["relative_path"])
            require(scope != "home" or parts[0] in allowed_home, "provider home staging prefix refused")
            identity = (scope, entry["relative_path"])
            require(identity not in seen and len(seen) < MAX_ENTRIES, "staging inventory bound/duplicate")
            seen.add(identity)
            require(integer(entry["mode"], 0, 0o777) and integer(entry["size"], 0, min(MAX_ARTIFACT, policy["limits"]["disk_bytes"]))
                    and (hash_value(entry["sha256"]) if kind == "file" else entry["sha256"] is None and entry["size"] == 0),
                    "staging file metadata bound")
            record = compact(entry)
            inventory.update(len(record).to_bytes(8, "big")); inventory.update(record)
            disposition = home_entry_policy(parts, kind, entry["size"]) if scope == "home" else "create"
            if scope == "home" and len(parts) >= 3 and parts[1] in {"skills", "adopted-plugins"}:
                snapshot_key = "/".join(parts[:3] if parts[1] == "adopted-plugins" else parts[:2])
                snapshot_bytes[snapshot_key] = snapshot_bytes.get(snapshot_key, 0) + entry["size"]
                require(snapshot_bytes[snapshot_key] <= 64*1024*1024, "prepared snapshot exceeds 64 MiB")
            parentfd = staging_parent(rootfds[scope], parts)
            try:
                if kind == "directory":
                    try:
                        os.mkdir(parts[-1], 0o700, dir_fd=parentfd)
                    except FileExistsError:
                        # Existing provider config directories are allowed; files never overwrite.
                        require(scope == "home", "checkout staging entry already exists")
                        existing = os.open(parts[-1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parentfd)
                        os.close(existing)
                    else:
                        fd = os.open(parts[-1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parentfd)
                        try:
                            os.fchown(fd, policy["engine"]["uid"], policy["engine"]["gid"])
                            os.fchmod(fd, 0o700)
                            os.fsync(fd)
                        finally:
                            os.close(fd)
                        created.append((scope, tuple(parts), True))
                else:
                    preserve = False
                    filename = parts[-1]
                    existing_digest = None
                    if disposition == "preserve-auth" or (scope == "home" and disposition == "create"):
                        try:
                            existing = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parentfd)
                        except FileNotFoundError:
                            pass
                        else:
                            try:
                                meta = os.fstat(existing)
                                require(stat.S_ISREG(meta.st_mode) and meta.st_nlink == 1 and meta.st_size <= (1024*1024 if disposition == "preserve-auth" else 4*1024*1024),
                                        "existing provider input has unsafe metadata")
                                if disposition != "preserve-auth":
                                    saved = bytearray()
                                    while chunk := os.read(existing, MAX_FRAME): saved.extend(chunk)
                                    existing_digest = sha(saved)
                                preserve = True
                            finally:
                                os.close(existing)
                    if disposition == "replace-config":
                        # Keep prior settings until the complete uploaded inventory verifies.
                        filename = ".doxa-stage-" + secrets.token_hex(16)
                    fd = None if preserve else os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parentfd)
                    if fd is not None:
                        created.append((scope, tuple(parts[:-1] + [filename]), False))
                    try:
                        if fd is not None:
                            os.fchown(fd, policy["engine"]["uid"], policy["engine"]["gid"])
                            os.fchmod(fd, 0o700 if scope == "checkout" and entry["mode"] & 0o100 else 0o600)
                        remaining, digest = entry["size"], hashlib.sha256()
                        structured = bytearray()
                        while remaining:
                            kind_tag, chunk = channel.receive_frame()
                            require(kind_tag == TAG_FILE and 0 < len(chunk) <= remaining, "staging file stream framing")
                            remaining -= len(chunk); total += len(chunk)
                            require(total <= policy["limits"]["disk_bytes"], "aggregate staged byte ceiling")
                            digest.update(chunk)
                            if disposition in {"preserve-auth", "replace-config"}:
                                structured.extend(chunk)
                            if fd is not None:
                                view = memoryview(chunk)
                                while view:
                                    channel.deadline.check()
                                    count = os.write(fd, view)
                                    require(count > 0, "staging file write failed")
                                    view = view[count:]
                        require(digest.hexdigest() == entry["sha256"], "staged file byte digest differs")
                        require(existing_digest is None or existing_digest == entry["sha256"], "prepared snapshot input changed; explicit replacement required")
                        if disposition in {"preserve-auth", "replace-config"}:
                            require(type(strict_json(structured)) is dict, "provider auth/settings must be JSON object")
                        if fd is not None:
                            os.fsync(fd)
                        if disposition == "replace-config":
                            config_updates.append((os.dup(parentfd), filename, parts[-1]))
                    finally:
                        if fd is not None:
                            os.close(fd)
                os.fsync(parentfd)
            finally:
                os.close(parentfd)
    except BaseException:
        for scope, parts, directory in reversed(created):
            try:
                parent = staging_parent(rootfds[scope], parts)
                try:
                    (os.rmdir if directory else os.unlink)(parts[-1], dir_fd=parent)
                finally:
                    os.close(parent)
            except OSError:
                pass  # Failed staging remains non-authorizing; native after-gate never runs.
        raise
    finally:
        for fd in rootfds.values():
            os.close(fd)
        for fd, _, _ in config_updates:
            os.close(fd)


def send_staging(channel, checkout=None, home_files=(), family=None):
    inventory = hashlib.sha256(b"doxa-staging-v1\0")
    count = 0
    emitted_dirs = set()
    def entry(scope, relative, kind, mode, size, digest):
        nonlocal count
        relative_parts(relative)
        row = {"scope": scope, "relative_path": relative, "kind": kind, "mode": mode, "size": size, "sha256": digest}
        raw = compact(row)
        inventory.update(len(raw).to_bytes(8, "big")); inventory.update(raw)
        count += 1
        require(count <= MAX_ENTRIES, "client staging inventory bound")
        channel.frame(TAG_ENTRY, raw)
    def send_file(fd, scope, relative):
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_size <= MAX_ARTIFACT, "client staging regular file bound")
        if scope == "home":
            require(before.st_uid == os.geteuid() and not before.st_mode & 0o077 and before.st_nlink == 1,
                    "provider home source must be owner-private single-link file")
        digest = hashlib.sha256()
        while chunk := os.read(fd, MAX_FRAME):
            channel.deadline.check(); digest.update(chunk)
        os.lseek(fd, 0, os.SEEK_SET)
        entry(scope, relative, "file", stat.S_IMODE(before.st_mode) & 0o777, before.st_size, digest.hexdigest())
        while chunk := os.read(fd, MAX_FRAME):
            channel.frame(TAG_FILE, chunk)
        after = os.fstat(fd)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        require(identity(before) == identity(after), "caller staging input changed during scan")
    def walk(fd, prefix):
        for name in sorted(os.listdir(fd)):
            relative = f"{prefix}/{name}" if prefix else name
            meta = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(meta.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    entry("checkout", relative, "directory", 0o700, 0, None)
                    walk(child, relative)
                finally:
                    os.close(child)
            else:
                require(stat.S_ISREG(meta.st_mode), "caller staging refuses links and special files")
                child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                try:
                    send_file(child, "checkout", relative)
                finally:
                    os.close(child)
    if checkout is not None:
        fd = os.open(checkout, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            walk(fd, "")
        finally:
            os.close(fd)
    for relative, source in home_files:
        parts = relative_parts(relative)
        require(parts[0] in {"claude", "codex"} and (family is None or parts[0] == family), "caller provider home scope")
        for depth in range(1, len(parts)):
            directory = "/".join(parts[:depth])
            if directory not in emitted_dirs:
                emitted_dirs.add(directory)
                entry("home", directory, "directory", 0o700, 0, None)
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            send_file(fd, "home", relative)
        finally:
            os.close(fd)
    channel.json_frame(TAG_STAGED, {"entry_count": count, "tree_sha256": inventory.hexdigest()})


def authenticated_client(session_id, seconds):
    require(type(session_id) is str and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,63}", session_id), "client session identity")
    receipt_path = Path("/run/doxa/hardened") / session_id / "broker-receipt.json"
    receipt = strict_json(read_regular(receipt_path, root=True, readonly=True))
    keys(receipt, {"contract", "session_id", "caller_uid", "broker_uid", "broker_socket", "policy_path", "policy_sha256",
                   "broker_script", "broker_sha256", "broker_exe", "broker_exe_sha256", "boot_id"})
    require(receipt["contract"] == PROTOCOL and receipt["session_id"] == session_id and receipt["caller_uid"] == os.geteuid()
            and receipt["broker_uid"] == 0 and receipt["boot_id"] == boot_id(), "root client receipt identity differs")
    require(receipt["broker_socket"] == str(receipt_path.parent / "broker.sock"), "root receipt socket scope differs")
    require(sha_file(receipt["broker_script"], root=True) == receipt["broker_sha256"]
            and sha_file(receipt["broker_exe"], root=True) == receipt["broker_exe_sha256"], "installed root broker identity changed")
    raw = read_regular(receipt["policy_path"], root=True, readonly=True)
    require(sha(raw) == receipt["policy_sha256"], "root policy differs from provisioning receipt")
    policy = validate_policy(strict_json(raw))
    require(policy["session_id"] == session_id and policy["caller_uid"] == os.geteuid()
            and policy["broker"]["path"] == receipt["broker_script"]
            and policy["broker"]["sha256"] == receipt["broker_sha256"], "root policy broker scope differs")
    meta = os.lstat(receipt["broker_socket"])
    require(stat.S_ISSOCK(meta.st_mode) and meta.st_uid == 0, "client socket is not root-owned")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(min(seconds, 2))
    pidfd = None
    try:
        sock.connect(receipt["broker_socket"])
        pid, pidfd = kernel_peer_pin(sock, 0)
        instance_raw = read_regular(receipt_path.parent / "broker-instance.json", root=True, readonly=True)
        instance = strict_json(instance_raw)
        keys(instance, {"contract", "session_id", "pid", "pidfd_pid", "start_time", "boot_id", "socket_dev", "socket_ino",
                        "exe_sha256", "script_sha256", "policy_sha256", "argv"})
        require(instance["contract"] == "doxa-root-broker-instance-v1" and instance["session_id"] == session_id
                and instance["pid"] == pid and instance["pidfd_pid"] == pid and integer(instance["start_time"],1,2**64-1)
                and instance["boot_id"] == receipt["boot_id"] and instance["policy_sha256"] == receipt["policy_sha256"]
                and instance["exe_sha256"] == receipt["broker_exe_sha256"] and instance["script_sha256"] == receipt["broker_sha256"]
                and instance["argv"][1:] == ["-I", receipt["broker_script"], "serve", "--policy", receipt["policy_path"]]
                and (instance["socket_dev"], instance["socket_ino"]) == (meta.st_dev, meta.st_ino), "ROOT-controlled broker instance differs")
        root_start_time_matches(pid,pidfd,instance["start_time"])
        current = os.lstat(receipt["broker_socket"])
        require((current.st_dev,current.st_ino) == (meta.st_dev,meta.st_ino), "ROOT broker socket replaced during attestation")
        channel = Channel(sock, Deadline(seconds))
        nonce = secrets.token_hex(16)
        challenge = compact({"v":1,"session_id":session_id,"op":"attest","nonce":nonce})
        channel.send(len(challenge).to_bytes(4,"big")+challenge)
        tag, payload = channel.receive_frame()
        require(tag == TAG_JSON, "ROOT attestation challenge refused")
        answer = strict_json(payload)
        require(answer == {"protocol":PROTOCOL,"status":"root-attested","session_id":session_id,"nonce":nonce,
                           "pid":pid,"start_time":instance["start_time"],"policy_sha256":receipt["policy_sha256"],
                           "instance_sha256":sha(instance_raw)} and not select.select([pidfd], [], [], 0)[0],
                "fresh ROOT instance challenge differs")
        root_start_time_matches(pid,pidfd,instance["start_time"])
        return channel, pidfd, policy
    except BaseException:
        sock.close()
        if pidfd is not None:
            os.close(pidfd)
        raise


def client_exec(op, session_id, family=None, argv=(), envnames=(), cwd=".", checkout=None, home_files=(), callback_socket=None, callback_pid=None, capability=None, relative=None, thread=None, offset=0, limit=32*1024*1024, snapshot_destination=None, expect_file=None):
    """Typed caller CLI: secrets read by name and sent only over root-pinned IPC."""
    require(op in OPS, "unknown client operation")
    channel, pidfd, policy = authenticated_client(session_id, 120)
    try:
        request = {"v": 1, "session_id": session_id, "op": op}
        if op in {"provider_exec", "workspace_exec"}:
            selected = ENV_KEYS if op == "provider_exec" else {"LANG", "LC_ALL", "TERM"}
            require(set(envnames) <= selected and len(set(envnames)) == len(envnames), "explicit environment name refused")
            require(all(name in os.environ for name in envnames), "selected environment value is absent")
            request.update(argv=list(argv), env={name: os.environ[name] for name in envnames}, cwd=cwd)
            if op == "provider_exec":
                require(family in policy["providers"], "client provider family refused")
                request["family"] = family
            channel.deadline = Deadline(policy["limits"]["deadline_seconds"])
        elif op in {"provider_snapshot", "verify_provider_snapshot"}:
            require(family in policy["providers"] and relative is not None, "snapshot client family/relative required")
            request.update(family=family, relative=relative)
            if thread is not None: request["thread"] = thread
            if op == "provider_snapshot":
                require(snapshot_destination is not None, "caller snapshot destination required")
                request.update(offset=offset, limit=limit)
            else:
                require(expect_file is not None, "caller snapshot expectation required")
                request["expected"] = strict_json(read_client_private(expect_file))
            channel.deadline = Deadline(policy["limits"]["operation_seconds"])
        elif op in {"register_hook", "unregister_hook"}:
            require(callback_pid is not None and capability is not None, "callback identity is required")
            request.update(client_pid=callback_pid, capability=capability)
            if op == "register_hook":
                require(callback_socket is not None, "callback socket is required")
                request["socket"] = str(callback_socket)
            channel.deadline = Deadline(policy["limits"]["operation_seconds"])
        else:
            require(not argv and not envnames and family is None and cwd == ".", "non-exec client fields refused")
            channel.deadline = Deadline(policy["limits"]["operation_seconds"])
        staging = checkout is not None or bool(home_files)
        require(not staging or op in {"prepare", "provider_exec"}, "client staging operation refused")
        require(checkout is None or op == "prepare", "checkout staging only at prepare")
        if staging:
            request["staging"] = True
        data = compact(request)
        require(len(data) <= MAX_JSON, "client request bound")
        channel.send(len(data).to_bytes(4, "big") + data)
        if staging:
            send_staging(channel, checkout, home_files, family)
        tag, payload = channel.receive_frame()
        require(not select.select([pidfd], [], [], 0)[0], "root broker exited during reply")
        if tag == TAG_ERROR:
            raise Refused(strict_json(payload).get("error", "root broker refused"))
        require(tag == TAG_JSON, "root broker initial reply framing")
        if op == "provider_snapshot":
            receive_client_snapshot(channel, strict_json(payload), snapshot_destination)
        if op not in {"provider_exec", "workspace_exec"}:
            sys.stdout.buffer.write(payload + b"\n")
            return 0
        reply = strict_json(payload)
        require(reply.get("protocol") == PROTOCOL and reply.get("status") == "exec-ready", "root broker did not admit transport")
        return client_stdio(channel, pidfd, policy["limits"])
    finally:
        channel.sock.close()
        os.close(pidfd)


def client_private_parent(path):
    path = canonical(str(path))
    require(path.parent.resolve(strict=True) == path.parent, "caller private path contains symlink")
    meta = path.parent.stat()
    require(meta.st_uid == os.geteuid() and stat.S_ISDIR(meta.st_mode) and not meta.st_mode & 0o077, "caller private parent required")
    return path


def read_client_private(path):
    path = client_private_parent(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        meta = os.fstat(fd)
        require(stat.S_ISREG(meta.st_mode) and meta.st_uid == os.geteuid() and meta.st_nlink == 1
                and not meta.st_mode & 0o077 and meta.st_size <= MAX_JSON, "caller expected proof must be private bounded single-link file")
        data = os.read(fd, MAX_JSON+1)
        require(len(data) == meta.st_size, "caller expected proof size drift")
        return data
    finally:
        os.close(fd)


def receive_client_snapshot(channel, metadata, destination):
    require(metadata.get("status") == "snapshot" and integer(metadata.get("bytes"), 0, 32*1024*1024)
            and hash_value(metadata.get("chunk_sha256")), "root snapshot metadata malformed")
    destination = client_private_parent(destination)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    digest, total = hashlib.sha256(), 0
    try:
        os.fchmod(fd, 0o600)
        while True:
            tag, payload = channel.receive_frame()
            if tag == TAG_ERROR: raise Refused(strict_json(payload).get("error", "root snapshot refused"))
            if tag == TAG_SNAPSHOT_END:
                ending = strict_json(payload)
                require(ending == {"bytes": total, "chunk_sha256": digest.hexdigest()} and total == metadata["bytes"]
                        and digest.hexdigest() == metadata["chunk_sha256"], "root snapshot byte proof differs")
                os.fsync(fd)
                break
            require(tag == TAG_SNAPSHOT and total + len(payload) <= metadata["bytes"], "root snapshot framing/budget")
            digest.update(payload); total += len(payload)
            view = memoryview(payload)
            while view:
                channel.deadline.check()
                count = os.write(fd, view)
                require(count > 0, "caller snapshot write failed")
                view = view[count:]
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        os.close(fd)


def client_stdio(channel, pidfd, limits):
    """Preserve raw stdio with bounded framing and independent stderr."""
    pending, incoming = bytearray(), bytearray()
    outputs = {1: bytearray(), 2: bytearray()}
    descriptors = {1: sys.stdout.fileno(), 2: sys.stderr.fileno()}
    stdin_fd = sys.stdin.fileno()
    old_flags = {fd: fcntl.fcntl(fd, fcntl.F_GETFL) for fd in {stdin_fd, *descriptors.values()}}
    for fd, flags in old_flags.items():
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    eof = False
    total_in = total_out = 0
    exit_code = None
    queue_limit = MAX_FRAME * 4
    try:
        while True:
            if exit_code is not None and not any(outputs.values()):
                return exit_code
            reads = [channel.sock] if exit_code is None and sum(map(len, outputs.values())) < queue_limit else []
            if not eof and len(pending) < queue_limit:
                reads.append(stdin_fd)
            writes = ([channel.sock] if pending else []) + [descriptors[k] for k, q in outputs.items() if q]
            readable, writable, _ = channel.deadline.wait(reads, writes)
            require(not select.select([pidfd], [], [], 0)[0] or exit_code is not None, "root broker exited during transport")
            for key, fd in descriptors.items():
                if fd in writable:
                    try:
                        count = os.write(fd, outputs[key])
                    except BlockingIOError:
                        continue
                    require(count > 0, "client output closed")
                    del outputs[key][:count]
            if channel.sock in writable:
                try:
                    count = channel.sock.send(pending)
                except BlockingIOError:
                    count = 0
                del pending[:count]
            if stdin_fd in readable:
                try:
                    data = os.read(stdin_fd, MAX_FRAME)
                except BlockingIOError:
                    data = None
                if data is not None:
                    total_in += len(data)
                    require(total_in <= limits["stdin_bytes"], "client stdin budget exceeded")
                    pending.extend(bytes([TAG_STDIN if data else TAG_EOF]) + len(data).to_bytes(4, "big") + data)
                    eof = not data
            if channel.sock in readable:
                data = channel.sock.recv(MAX_FRAME)
                require(data, "root broker disconnected before exit")
                incoming.extend(data)
                while len(incoming) >= 5:
                    tag, size = incoming[0], int.from_bytes(incoming[1:5], "big")
                    require(size <= MAX_FRAME, "root broker output frame bound")
                    if len(incoming) < size + 5:
                        break
                    payload = bytes(incoming[5:size+5])
                    del incoming[:size+5]
                    if tag in {TAG_STDOUT, TAG_STDERR}:
                        total_out += size
                        require(total_out <= limits["output_bytes"], "client output budget exceeded")
                        outputs[1 if tag == TAG_STDOUT else 2].extend(payload)
                    elif tag == TAG_EXIT:
                        result = strict_json(payload)
                        require(integer(result.get("exit_code"), 0, 255), "root broker exit status")
                        exit_code = result["exit_code"]
                        require(not incoming, "data after root broker exit")
                        break
                    elif tag == TAG_ERROR:
                        raise Refused(strict_json(payload).get("error", "root broker refused transport"))
                    else:
                        raise Refused("unknown root broker output tag")
    except BaseException:
        try:
            channel.deadline = Deadline(.5)
            channel.frame(TAG_CANCEL, b"")
        except Exception:
            pass
        raise
    finally:
        for fd, flags in old_flags.items():
            fcntl.fcntl(fd, fcntl.F_SETFL, flags)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    installing = sub.add_parser("install")
    installing.add_argument("--source", required=True, type=Path)
    installing.add_argument("--destination", required=True, type=Path)
    serving = sub.add_parser("serve")
    serving.add_argument("--policy", required=True, type=Path)
    client = sub.add_parser("client")
    client.add_argument("--session-id", required=True)
    client.add_argument("--op", required=True, choices=sorted(OPS))
    client.add_argument("--family", choices=("claude", "codex"))
    client.add_argument("--env", action="append", default=[], dest="envnames")
    client.add_argument("--cwd", default=".")
    client.add_argument("--checkout", type=Path)
    client.add_argument("--home-file", nargs=2, action="append", default=[], metavar=("RELATIVE", "SOURCE"))
    client.add_argument("--callback-socket", type=Path)
    client.add_argument("--callback-pid", type=int)
    client.add_argument("--relative")
    client.add_argument("--thread")
    client.add_argument("--offset", type=int, default=0)
    client.add_argument("--limit", type=int, default=32*1024*1024)
    client.add_argument("--snapshot-destination", type=Path)
    client.add_argument("--expect-file", type=Path)
    client.add_argument("--capability-env", help="explicit environment name holding a 48-hex callback capability")
    client.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command == "client":
        argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        capability = os.environ.get(args.capability_env) if args.capability_env else None
        sys.exit(client_exec(args.op, args.session_id, args.family, argv, args.envnames, args.cwd, args.checkout, args.home_file, args.callback_socket, args.callback_pid, capability, args.relative, args.thread, args.offset, args.limit, args.snapshot_destination, args.expect_file))
    elif args.command == "install":
        print(json.dumps(install(args.source, args.destination), sort_keys=True))
    else:
        serve(args.policy)


if __name__ == "__main__":
    try:
        main()
    except (Refused, OSError, ValueError) as error:
        print(f"root isolation broker refused: {str(error)[:256]}", file=sys.stderr)
        sys.exit(1)
