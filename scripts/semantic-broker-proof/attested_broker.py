#!/usr/bin/env python3
"""Operator-provisioned Linux semantic broker; never installed automatically.

Run as root only on a reviewed disposable/production host with a distinct
rootless Engine identity. Client pins the Ed25519 public key AND code/config,
Engine/analyzer/image hashes from a separate reviewed provisioning receipt.
All Engine/LSP I/O goes directly through the pinned Unix peer; no Docker CLI.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import hashlib
import http.client
import json
import os
import pathlib
import select
import selectors
import signal
import shutil
import socket
import stat
import struct
import sys
import subprocess
from cryptography.hazmat.primitives.serialization import load_pem_private_key
import time

PROTOCOL = "doxa-semantic-attested-v1"
POLICY = "immutable-root-snapshot-rootless-no-egress-v1"
MAX_TREE = 128 * 1024 * 1024
MAX_FILE = 8 * 1024 * 1024
MAX_ENTRIES = 20000
MAX_LSP = 32768
MAX_OUTPUT = 256 * 1024
ANALYZER_SETTINGS = {"cargo": {"buildScripts": {"enable": False}, "autoreload": True,
    "noDeps": True}, "procMacro": {"enable": False}, "checkOnSave": False,
    "files": {"watcher": "client"}, "cachePriming": {"numThreads": 1}, "numThreads": 1}


@contextmanager
def deadline(seconds):
    """Nested absolute deadline; never reset an outer transaction's budget."""
    previous, interval = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    signal.setitimer(signal.ITIMER_REAL, min(seconds, previous) if previous > 0 else seconds)
    try:
        yield
    finally:
        if previous > 0:
            signal.setitimer(signal.ITIMER_REAL, max(.000001, previous-(time.monotonic()-started)), interval)
        else:
            signal.setitimer(signal.ITIMER_REAL, 0)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def compact(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def sha_file(path, limit=256 * 1024 * 1024):
    with open(path, "rb") as source:
        hasher = hashlib.sha256()
        size = 0
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            require(size <= limit, "hashed file exceeds bound")
            hasher.update(chunk)
        return hasher.hexdigest()


def controlled(path, private=False):
    path = pathlib.Path(path)
    require(path.is_absolute(), "controlled path must be absolute")
    for item in [path, *path.parents]:
        info = item.lstat()
        require(not stat.S_ISLNK(info.st_mode) and info.st_uid == 0 and
                not info.st_mode & 0o022, "path is outside root control")
    if private:
        require(not path.stat().st_mode & 0o077, "private key permissions are too broad")


def scan_digest(domain, entries):
    hasher = hashlib.sha256(domain)
    for path, fact in sorted(entries.items()):
        name = path.encode()
        hasher.update(len(name).to_bytes(8, "big"))
        hasher.update(name)
        hasher.update(fact.encode())
    return hasher.hexdigest()


def snapshot(source, parent, nonce, expected, cancel=lambda: False):
    """Descriptor-walk ALL entries, refuse symlinks/nonregulars, freeze copy.

    The source-input hash follows DOXA's whole_scan exactly (original modes).
    Root owns every destination, source/Engine/client identities cannot write it.
    The manifest commits every copied byte; permission changes are separate.
    """
    started = time.monotonic()
    source = pathlib.Path(source)
    require(source.is_absolute() and source.resolve() == source, "noncanonical source root")
    destination = pathlib.Path(parent) / nonce
    destination.mkdir(mode=0o700)
    root_fd = None
    originals = {}
    mounted = {}
    bytes_read = 0
    try:
        root_fd = os.open(source, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY)
        def walk(fd, prefix, output):
            nonlocal bytes_read
            require(not cancel(), "semantic client cancelled")
            require(time.monotonic() - started < 20, "source snapshot deadline")
            info = os.fstat(fd)
            relative = prefix or "."
            originals[relative] = f"directory:{stat.S_IMODE(info.st_mode):o}"
            mounted[relative] = "directory:555"
            for name in sorted(os.listdir(fd)):
                require(not cancel(), "semantic client cancelled")
                require(name not in (".", "..") and len(name.encode()) <= 4096, "invalid source name")
                relative = f"{prefix}/{name}" if prefix else name
                require(len(relative.encode()) <= 4096 and len(originals) < MAX_ENTRIES,
                        "snapshot inventory limit")
                meta = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(meta.st_mode):
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        target = output / name
                        target.mkdir(mode=0o700)
                        walk(child, relative, target)
                        target.chmod(0o555)
                    finally:
                        os.close(child)
                else:
                    require(name not in ("rust-toolchain", "rust-toolchain.toml", "rust-analyzer.toml") and
                            not (name in ("config", "config.toml") and pathlib.PurePosixPath(relative).parent.name == ".cargo"),
                            f"project-controlled executable/configuration override refused: {relative}")
                    require(stat.S_ISREG(meta.st_mode), f"nonregular source input: {relative}")
                    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                    try:
                        before = os.fstat(child)
                        require(stat.S_ISREG(before.st_mode) and before.st_size <= MAX_FILE,
                                "source file size/type limit")
                        target = output / name
                        total = 0
                        hasher = hashlib.sha256()
                        with target.open("xb") as copied:
                            while chunk := os.read(child, 65536):
                                require(not cancel(), "semantic client cancelled")
                                total += len(chunk)
                                bytes_read += len(chunk)
                                require(total <= MAX_FILE and bytes_read <= MAX_TREE and
                                        time.monotonic() - started < 20, "source copy bound")
                                hasher.update(chunk)
                                copied.write(chunk)
                            copied.flush()
                            os.fsync(copied.fileno())
                        after = os.fstat(child)
                        require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                                 before.st_mode) == (after.st_dev, after.st_ino, after.st_size,
                                                     after.st_mtime_ns, after.st_mode),
                                "source changed during immutable copy")
                        require(total == before.st_size, "source size drift")
                        sha = hasher.hexdigest()
                        originals[relative] = f"file:{stat.S_IMODE(before.st_mode):o}:{total}:{sha}"
                        target.chmod(0o444)
                        mounted[relative] = f"file:444:{total}:{sha}"
                    finally:
                        os.close(child)
            require(time.monotonic() - started < 20, "source snapshot deadline")
        walk(root_fd, "", destination)
        observed = scan_digest(b"doxa-whole-worktree-input-v1\0", originals)
        require(observed == expected, "broker immutable bytes differ from client source inventory")
        destination.chmod(0o555)
        controlled(destination)
        return destination, scan_digest(b"doxa-whole-worktree-input-v1\0", mounted)
    except Exception:
        shutil.rmtree(destination)
        raise
    finally:
        if root_fd is not None:
            os.close(root_fd)


class Engine:
    def __init__(self, config):
        self.config = config
        self.pid = None
        self.pidfd = None
        self.inode = None
        self.sock = None

    def live(self):
        require(not self.config.get("_cancel", lambda: False)(), "semantic client cancelled")
        require(self.pidfd is not None and not select.select([self.pidfd], [], [], 0)[0],
                "reviewed Engine exited/restarted")
        info = os.stat(self.config["engine_socket"], follow_symlinks=False)
        require((info.st_dev, info.st_ino) == self.inode, "reviewed Engine socket replaced")
        require(sha_file(f"/proc/{self.pid}/exe") == self.config["engine_sha256"],
                "reviewed Engine executable changed")

    def connect(self):
        path = self.config["engine_socket"]
        meta = os.stat(path, follow_symlinks=False)
        require(stat.S_ISSOCK(meta.st_mode) and meta.st_uid == self.config["engine_uid"],
                "Engine socket owner/type differs from reviewed identity")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2)
        sock.connect(path)
        pid, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        require(uid == self.config["engine_uid"] and pid > 0, "Engine peer is not reviewed identity")
        require(sha_file(f"/proc/{pid}/exe") == self.config["engine_sha256"],
                "same-UID fake Engine executable refused")
        if self.pid is None:
            self.pid = pid
            self.pidfd = os.pidfd_open(pid)
            self.inode = (meta.st_dev, meta.st_ino)
        require(pid == self.pid and self.inode == (meta.st_dev, meta.st_ino),
                "Engine peer or socket changed between requests")
        self.live()
        self.sock = sock
        return sock

    def request(self, method, path, body=None, acceptable=(200, 201, 204, 304, 404)):
        with deadline(2):
            return self._request(method, path, body, acceptable)

    def _request(self, method, path, body, acceptable):
        sock = self.connect()
        try:
            data = b"" if body is None else compact(body)
            sock.sendall(f"{method} /v1.51{path} HTTP/1.1\r\nHost: docker\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data)
            response = http.client.HTTPResponse(sock)
            response.begin()
            require(response.status in acceptable, f"Engine request failed: {method} {path} {response.status}")
            content = response.read(MAX_OUTPUT + 1)
            require(len(content) <= MAX_OUTPUT, "Engine JSON output bound")
            self.live()
            return response.status, json.loads(content) if content else None
        finally:
            sock.close()
            self.sock = None

    def attach(self, cid):
        """Inspect then HTTP-upgrade on the SAME pinned Engine Unix FD."""
        with deadline(2):
            return self._attach(cid)

    def _attach(self, cid):
        sock = self.connect()
        sock.settimeout(2)
        sock.sendall(f"GET /v1.51/containers/{cid}/json HTTP/1.1\r\nHost: docker\r\nConnection: keep-alive\r\n\r\n".encode())
        response = http.client.HTTPResponse(sock)
        response.begin()
        require(response.status == 200, "attach pre-inspect failed")
        body = response.read(MAX_OUTPUT + 1)
        require(len(body) <= MAX_OUTPUT, "attach inspect output bound")
        observed = json.loads(body)
        require(observed["Id"] == cid, "attach pre-inspect container ID drift")
        sock.sendall(f"POST /v1.51/containers/{cid}/attach?stream=1&stdin=1&stdout=1&stderr=1 HTTP/1.1\r\nHost: docker\r\nConnection: Upgrade\r\nUpgrade: tcp\r\nContent-Length: 0\r\n\r\n".encode())
        header = bytearray()
        while not header.endswith(b"\r\n\r\n"):
            require(len(header) < 4096, "Engine upgrade header bound")
            data = sock.recv(1)
            require(data, "Engine closed before upgrade")
            header.extend(data)
        require(header.startswith(b"HTTP/1.1 101 ") and
                b"application/vnd.docker.multiplexed-stream" in header.lower(),
                "Engine attach is not upgraded non-TTY multiplexed stream")
        self.live()
        return sock

    def close(self):
        if self.sock:
            self.sock.close()
        if self.pidfd is not None:
            os.close(self.pidfd)


def host_pid(engine, reported):
    def nspids(pid):
        for line in pathlib.Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("NSpid:"):
                return [int(item) for item in line.split()[1:]]
        raise RuntimeError("missing kernel NSpid")
    depth = len(nspids(engine.pid))
    matches = []
    for path in pathlib.Path("/proc").iterdir():
        if path.name.isdigit():
            try:
                ids = nspids(int(path.name))
                if len(ids) > depth and ids[depth - 1] == reported:
                    matches.append(int(path.name))
            except (FileNotFoundError, ProcessLookupError):
                pass
    require(len(matches) == 1, "container PID cannot be mapped uniquely to host namespace")
    return matches[0]


def effective_policy(engine, cid, source, root, image):
    _, info = engine.request("GET", f"/containers/{cid}/json", acceptable=(200,))
    require(info["Id"] == cid and info["Image"] == image and not info["Config"]["Tty"],
            "container/image/TTY identity drift")
    config = info["HostConfig"]
    for field, expected in {"ReadonlyRootfs": True, "NetworkMode": "none", "Privileged": False,
        "Memory": 1073741824, "MemorySwap": 1073741824, "NanoCpus": 1000000000,
        "PidsLimit": 64, "PidMode": "", "IpcMode": "none", "CgroupnsMode": "private"}.items():
        require(config.get(field) == expected, f"effective Docker policy drift: {field}")
    require(config.get("CapDrop") == ["ALL"] and not config.get("CapAdd") and
            not config.get("Devices") and not config.get("DeviceRequests") and
            config.get("SecurityOpt") == ["no-new-privileges:true"] and
            config.get("LogConfig", {}).get("Type") == "none", "capability/device/log policy drift")
    require(config.get("Ulimits") == [{"Name":"nofile","Soft":64,"Hard":64}],
            "file descriptor limit metadata differs")
    mounts = info["Mounts"]
    require(len(mounts) == 1 and mounts[0]["Source"] == str(source) and
            mounts[0]["Destination"] == root and not mounts[0]["RW"], "immutable snapshot mount differs")
    networks = info["NetworkSettings"].get("Networks", {})
    require(set(networks) in (set(), {"none"}) and all(
        not entry.get(field) for entry in networks.values()
        for field in ("IPAddress", "Gateway", "GlobalIPv6Address", "IPv6Gateway")),
        "network attachments/address metadata differs from none")
    pid = host_pid(engine, info["State"]["Pid"])
    proc = pathlib.Path(f"/proc/{pid}")
    for namespace in ("mnt", "net", "pid", "ipc", "uts"):
        require(os.readlink(proc / "ns" / namespace) != os.readlink(f"/proc/{engine.pid}/ns/{namespace}"),
                f"container shares Engine {namespace} namespace")
    status = dict(line.split(":", 1) for line in (proc / "status").read_text().splitlines() if ":" in line)
    require(status["NoNewPrivs"].strip() == "1" and status["Seccomp"].strip() == "2" and
            int(status["CapEff"].strip(), 16) == 0 and int(status["CapBnd"].strip(), 16) == 0,
            "effective kernel privilege policy differs")
    descriptor_limits=[line.split()[3:5] for line in (proc/"limits").read_text().splitlines()
                       if line.startswith("Max open files")]
    require(descriptor_limits==[["64","64"]], "effective file descriptor limit differs")
    membership = (proc / "cgroup").read_text().strip()
    require(membership.startswith("0::/") and "\n" not in membership, "not a single cgroup v2 hierarchy")
    group = pathlib.Path("/sys/fs/cgroup") / membership[4:]
    for filename, expected in {"memory.max": "1073741824", "memory.swap.max": "0",
                               "pids.max": "64", "cpu.max": "100000 100000"}.items():
        require((group / filename).read_text().strip() == expected, f"effective cgroup differs: {filename}")
    interfaces = [line.split(":", 1)[0].strip() for line in (proc / "net/dev").read_text().splitlines()[2:]]
    require(interfaces == ["lo"] and len((proc / "net/route").read_text().splitlines()) == 1,
            "effective network namespace has egress interfaces/routes")
    # Kernel mount options, independently of Engine's reported HostConfig.
    mount_rows = [line.split() for line in (proc / "mountinfo").read_text().splitlines()]
    for target in ("/", root):
        selected = [fields for fields in mount_rows if fields[4] == target]
        require(len(selected) == 1 and "ro" in selected[0][5].split(","), "effective source/root mount writable")
    temporary = [fields for fields in mount_rows if fields[4] == "/tmp"]
    require(len(temporary) == 1 and all(flag in temporary[0][5].split(",")
            for flag in ("rw", "noexec", "nosuid", "nodev")), "effective temporary mount flags differ")
    require(any("size=65536k" in part or "size=67108864" in part for part in temporary[0]),
            "effective temporary disk ceiling differs")
    return pid


def analyzer_executable(config, pid, pidfd):
    """Hash a held proc-exe FD through an exec-clean, namespace-pinned helper.

    The broker never changes UID/user namespace or passes signer state. The
    first exec occurs as root, clearing copied signer memory before nsenter
    changes identity. Only a held public user-namespace FD survives exec.
    """
    require(not select.select([pidfd], [], [], 0)[0], "analyzer PID exited before executable check")
    namespace = os.open(f"/proc/{pid}/ns/user", os.O_RDONLY | os.O_CLOEXEC)
    child = None
    started = time.monotonic()
    try:
        identity = os.fstat(namespace).st_ino
        for name in ("nsenter", "python", "reader"):
            path = config[name + "_path"]
            controlled(path)
            require(sha_file(path) == config[name + "_sha256"], "reviewed reader helper changed: " + name)
        argv = [config["nsenter_path"], f"--user=/proc/self/fd/{namespace}", "--",
            config["python_path"], "-I", config["reader_path"], str(pid), str(identity),
            str(os.stat("/proc/self/ns/mnt").st_ino), str(os.stat("/proc/self/ns/pid").st_ino)]
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env={"LANG":"C","PYTHONDONTWRITEBYTECODE":"1"},
            cwd="/", close_fds=True, pass_fds=(namespace,), start_new_session=True)
        buffers = {"stdout":bytearray(),"stderr":bytearray()}
        with selectors.DefaultSelector() as selector:
            for name, pipe in (("stdout",child.stdout),("stderr",child.stderr)):
                os.set_blocking(pipe.fileno(),False);selector.register(pipe,selectors.EVENT_READ,name)
            while selector.get_map():
                remaining=2-(time.monotonic()-started)
                require(remaining>0,"analyzer reader helper deadline")
                for key,_ in selector.select(min(.1,remaining)):
                    data=os.read(key.fileobj.fileno(),4097-len(buffers[key.data]))
                    if not data:selector.unregister(key.fileobj)
                    else:
                        buffers[key.data].extend(data)
                        require(len(buffers[key.data])<=4096,"analyzer reader helper output bound")
                require(not config.get("_cancel",lambda:False)(),"semantic client cancelled")
        child.wait(timeout=max(.000001,2-(time.monotonic()-started)))
        require(child.returncode==0,"reviewed analyzer reader failed: "+buffers["stderr"].decode(errors="replace")[:256])
        result=json.loads(buffers["stdout"])
        require(result["sha256"]==config["analyzer_sha256"] and result["namespaces"]["user"]==identity,
                "held analyzer executable bytes/namespace differ")
        require(os.stat(f"/proc/{pid}/ns/user").st_ino==identity and not select.select([pidfd],[],[],0)[0],
                "analyzer process/user namespace changed during executable check")
        return result
    finally:
        os.close(namespace)
        if child is not None and child.returncode is None:
            try:os.killpg(child.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            try:child.wait(timeout=1)
            except subprocess.TimeoutExpired:config["_reader_disabled"]=True
        if child is not None:
            child.stdout.close();child.stderr.close()


class Lsp:
    def __init__(self, stream, client=None):
        self.stream = stream
        self.client = client
        self.latest_status = None
        self.guard_quiescence = False
        self.messages = []
        self.stderr = bytearray()
        self.pending = bytearray()
        self.output = 0
        self.deadline = time.monotonic() + 20

    def send(self, value):
        body = compact(value)
        require(len(body) <= MAX_LSP, "LSP request exceeds32KiB")
        self.stream.sendall(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)

    def exact(self, count):
        result = bytearray()
        while len(result) < count:
            remaining = self.deadline - time.monotonic()
            require(remaining > 0, "LSP session deadline")
            readable, _, _ = select.select([self.stream] + ([self.client] if self.client else []), [], [], min(.1, remaining))
            if self.client and self.client in readable:
                raise RuntimeError("semantic client cancelled or sent trailing challenge data")
            if self.stream not in readable:
                continue
            self.stream.settimeout(min(2, remaining))
            data = self.stream.recv(count - len(result))
            require(data, "attached analyzer ended early")
            result.extend(data)
        return bytes(result)

    def receive(self):
        while True:
            position = self.pending.find(b"\r\n\r\n")
            if position >= 0:
                header = self.pending[:position].decode("ascii")
                require(position <= 1024 and header.startswith("Content-Length: "), "invalid bounded LSP header")
                size = int(header.removeprefix("Content-Length: "))
                require(0 <= size <= MAX_LSP, "LSP body exceeds32KiB")
                if len(self.pending) >= position + 4 + size:
                    body = self.pending[position + 4:position + 4 + size]
                    del self.pending[:position + 4 + size]
                    message = json.loads(body)
                    if len(self.messages) < 32:
                        self.messages.append({"method":str(message.get("method"))[:64], "id":message.get("id")})
                    if message.get("method") == "experimental/serverStatus":
                        self.latest_status = message.get("params", {})
                        if self.guard_quiescence:
                            require(self.latest_status.get("health") == "ok",
                                "analyzer quiescence/health drift: workspace unhealthy: " + str(self.latest_status.get("message"))[:256])
                            require(self.latest_status.get("quiescent") is True and
                                    self.latest_status.get("health") == "ok", "analyzer quiescence/health drift")
                    return message
            else:
                require(len(self.pending) <= 1024, "LSP header exceeds1KiB")
            frame = self.exact(8)
            size = int.from_bytes(frame[4:], "big")
            require(frame[0] in (1, 2) and frame[1:4] == b"\0\0\0" and size <= MAX_LSP + 1024,
                    "invalid Docker stdout frame")
            data = self.exact(size)
            self.output += size
            require(self.output <= MAX_OUTPUT, "total analyzer output exceeds256KiB")
            if frame[0] == 1:
                self.pending.extend(data)
            else:
                self.stderr.extend(data[:max(0,1024-len(self.stderr))])
            # stderr is bounded and never treated as LSP stdout.

    def until(self, predicate):
        for _ in range(256):
            message = self.receive()
            if predicate(message):
                return message
            if "id" in message and "method" in message:
                require(message["method"] == "workspace/configuration", "unexpected analyzer client request")
                # No project-controlled client settings or server commands.
                self.send({"jsonrpc": "2.0", "id": message["id"], "result": []})
        raise RuntimeError("analyzer message count bound")


def transaction(config, challenge, raw):
    require(config.get("_broker_sha256", sha_file(__file__)) == sha_file(__file__), "broker code changed since startup")
    if "_config_path" in config:
        require(sha_file(config["_config_path"]) == config["config_sha256"], "broker configuration changed since startup")
    require(challenge.get("protocol") == PROTOCOL and len(challenge.get("nonce", "")) == 64 and
            all(c in "0123456789abcdef" for c in challenge["nonce"]), "invalid transaction nonce")
    root = challenge["root"]
    require(root.startswith(config["allowed_source_parent"] + "/"), "source outside provisioned scope")
    require(challenge["request"]["method"] == "textDocument/definition" and
            challenge["request"]["id"] == 3, "not a bounded definition request")
    require(challenge["request"]["params"]["textDocument"]["uri"].startswith(f"file://{root}/"),
            "definition source outside source root")
    source = None
    cid = None
    create_attempted = False
    analyzer_pidfd = None
    engine = Engine(config)
    attached = None
    lsp = None
    receipt = {"protocol": PROTOCOL, "nonce": challenge["nonce"], "challenge_sha256": digest(raw),
        "source_input_sha256": challenge["source_input_sha256"], "policy": POLICY,
        "broker_sha256": config.get("_broker_sha256", sha_file(__file__)), "config_sha256": config["config_sha256"],
        "engine_sha256": config["engine_sha256"], "analyzer_sha256": config["analyzer_sha256"],
        "image_id": config["image_id"], "cleanup_removed": False, "status": "unknown"}
    try:
        source, mounted = snapshot(root, config["snapshot_parent"], challenge["nonce"],
                                   challenge["source_input_sha256"], config.get("_cancel", lambda: False))
        receipt.update(source_immutable=True, snapshot_content_sha256=mounted)
        _, info = engine.request("GET", "/info", acceptable=(200,))
        require("name=rootless" in info["SecurityOptions"] and
                info["CgroupVersion"] == "2", "reviewed daemon is not effective rootless cgroupv2")
        _, image = engine.request("GET", f"/images/{config['image_id']}/json", acceptable=(200,))
        require(image["Id"] == config["image_id"], "pinned image bytes absent")
        profile = {"Labels": {"io.doxa.semantic.nonce": challenge["nonce"]}, "Image": config["image_id"], "Entrypoint": ["/usr/local/bin/rust-analyzer"],
            "WorkingDir": root, "Env": ["HOME=/tmp", "TMPDIR=/tmp", "CARGO_HOME=/tmp/cargo",
                "RUSTUP_HOME=/tmp/rustup", "CARGO_NET_OFFLINE=true", "RA_LOG=error"],
            "AttachStdin": True, "AttachStdout": True, "AttachStderr": True,
            "OpenStdin": True, "StdinOnce": False, "Tty": False,
            "HostConfig": {"ReadonlyRootfs": True, "NetworkMode": "none", "IpcMode": "none",
                "CgroupnsMode": "private", "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"],
                "Memory": 1073741824, "MemorySwap": 1073741824, "NanoCpus": 1000000000,
                "PidsLimit": 64, "Ulimits": [{"Name": "nofile", "Soft": 64, "Hard": 64}],
                "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=67108864,mode=1777"},
                "LogConfig": {"Type": "none", "Config": {}},
                "Mounts": [{"Type": "bind", "Source": str(source), "Target": root, "ReadOnly": True}]}}
        create_attempted = True
        _, created = engine.request("POST", "/containers/create?name=doxa-semantic-" + challenge["nonce"],
                                    profile, acceptable=(201,))
        created_id = created.get("Id") if isinstance(created,dict) else None
        require(type(created_id) is str and len(created_id) == 64 and
                all(c in "0123456789abcdef" for c in created_id), "invalid created container ID")
        cid = created_id
        receipt["container_id"] = cid
        engine.request("POST", f"/containers/{cid}/start", acceptable=(204,))
        pid = effective_policy(engine, cid, source, root, config["image_id"])
        require(not config.get("_reader_disabled"), "analyzer reader disabled after unkillable timeout")
        analyzer_pidfd = os.pidfd_open(pid)
        executable = analyzer_executable(config, pid, analyzer_pidfd)
        attached = engine.attach(cid)
        lsp = Lsp(attached, config.get("_client"))
        initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "processId": None, "rootUri": f"file://{root}",
            "workspaceFolders": [{"uri": f"file://{root}", "name": "doxa-semantic"}],
            "capabilities": {"experimental": {"serverStatusNotification": True},
                "textDocument": {"definition": {"linkSupport": True}}},
            "initializationOptions": ANALYZER_SETTINGS}}
        # This bootstrap option is justified only by the already verified
        # root-owned immutable mount. The mutable-source producer stays closed.
        require(receipt.get("source_immutable") and source is not None,
                "immutable snapshot is required for analyzer bootstrap")
        lsp.send(initialize)
        init_reply = lsp.until(lambda message: message.get("id") == 1 and "result" in message)
        require("error" not in init_reply, "analyzer initialization failed")
        lsp.send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        lsp.until(lambda message: message.get("method") == "experimental/serverStatus" and
                  message.get("params", {}).get("quiescent") is True and
                  message.get("params", {}).get("health") == "ok")
        lsp.guard_quiescence = True
        lsp.send(challenge["request"])
        definition = lsp.until(lambda message: message.get("id") == 3)
        require("error" not in definition, "analyzer definition error")
        final_pid = effective_policy(engine, cid, source, root, config["image_id"])
        require(final_pid == pid and analyzer_executable(config, final_pid, analyzer_pidfd) == executable,
                "analyzer process/held executable drift after definition")
        engine.live()
        require(sha_file(__file__) == receipt["broker_sha256"], "broker code changed during transaction")
        if "_config_path" in config:
            require(sha_file(config["_config_path"]) == config["config_sha256"], "broker config changed during transaction")
        receipt.update(status="attested", engine_identity_stable=True, effective_policy_verified=True,
                       quiescent=True, attached_same_container=True, request=challenge["request"],
                       response=definition, analyzer_executable=executable,
                       analyzer_reader_sha256=config["reader_sha256"],
                       reader_exec_clean=True, analyzer_identity_stable=True,
                       analyzer_settings=ANALYZER_SETTINGS)
        lsp.send({"jsonrpc": "2.0", "id": 4, "method": "shutdown", "params": None})
        shutdown = lsp.until(lambda message: message.get("id") == 4)
        require("error" not in shutdown and shutdown.get("result", "missing") is None,
                "analyzer shutdown failed")
        lsp.send({"jsonrpc": "2.0", "method": "exit", "params": None})
        exit_deadline = time.monotonic() + 2
        while time.monotonic() < exit_deadline:
            _, ended = engine.request("GET", f"/containers/{cid}/json", acceptable=(200,))
            if not ended["State"]["Running"]:
                require(ended["State"]["ExitCode"] == 0 and not ended["State"]["OOMKilled"],
                        "analyzer did not exit successfully")
                receipt["analyzer_exit_successful"] = True
                break
            time.sleep(.02)
        require(receipt.get("analyzer_exit_successful"), "analyzer successful exit deadline")
    except Exception as error:
        receipt.update(status="unknown", error=str(error)[:512])
        if lsp is not None:
            receipt.update(analyzer_latest_status=lsp.latest_status, analyzer_messages=lsp.messages,
                analyzer_stderr=lsp.stderr.decode(errors="replace"))
    finally:
        signal.setitimer(signal.ITIMER_REAL, 8)
        config.pop("_cancel", None)
        config.pop("_client", None)
        if attached:
            attached.close()
        if create_attempted and cid is None:
            recovery = Engine(config)
            try:
                code, remaining = recovery.request("GET", "/containers/doxa-semantic-" + challenge["nonce"] + "/json", acceptable=(200,404))
                if code == 200:
                    require(remaining["Name"] == "/doxa-semantic-" + challenge["nonce"] and
                            remaining["Image"] == config["image_id"] and
                            remaining["Config"]["Labels"].get("io.doxa.semantic.nonce") == challenge["nonce"],
                            "unknown-create recovery identity differs")
                    recovered_id = remaining["Id"]
                    require(len(recovered_id) == 64 and all(c in "0123456789abcdef" for c in recovered_id),
                            "unknown-create recovery CID invalid")
                    cid = recovered_id
                    receipt["container_id"] = cid
                    receipt["create_response_recovered"] = True
                else:
                    require(code==404,"unknown-create absence not verified")
                    receipt["cleanup_removed"] = True
            except Exception as error:
                receipt["cleanup_recovery_error"] = str(error)[:512]
            finally:
                recovery.close()
        if cid:
            try:
                engine.request("DELETE", f"/containers/{cid}?force=1&v=1", acceptable=(204, 404))
                code, _ = engine.request("GET", f"/containers/{cid}/json", acceptable=(404,))
                require(code == 404, "container remains after removal")
                engine.live()
                engine.request("GET", "/containers/doxa-semantic-" + challenge["nonce"] + "/json", acceptable=(404,))
                receipt["cleanup_removed"] = True
            except Exception as error:
                receipt.update(status="unknown", cleanup_removed=False, cleanup_error=str(error)[:512])
                # A restarted peer cannot attest the original LSP stream, but
                # a newly pinned Engine may remove our exact named CID. This
                # separate cleanup authority never restores binding authority.
                recovered = Engine(config)
                try:
                    code, remaining = recovered.request("GET", f"/containers/{cid}/json", acceptable=(200,404))
                    if code == 200:
                        require(remaining["Id"] == cid and
                                remaining["Name"] == "/doxa-semantic-" + challenge["nonce"],
                                "recovery cleanup identity differs")
                        recovered.request("DELETE", f"/containers/{cid}?force=1&v=1", acceptable=(204,404))
                    recovered.request("GET", f"/containers/{cid}/json", acceptable=(404,))
                    recovered.request("GET", "/containers/doxa-semantic-" + challenge["nonce"] + "/json", acceptable=(404,))
                    receipt.update(cleanup_removed=True, cleanup_recovery_used=True)
                except Exception as recovery_error:
                    receipt["cleanup_recovery_error"] = str(recovery_error)[:512]
                finally:
                    recovered.close()
        engine.close()
        if analyzer_pidfd is not None:
            os.close(analyzer_pidfd)
        if source:
            shutil.rmtree(source)
        signal.setitimer(signal.ITIMER_REAL, 0)
    return receipt


def read_exact(stream, count):
    output = bytearray()
    while len(output) < count:
        data = stream.recv(count - len(output))
        require(data, "client challenge ended early")
        output.extend(data)
    return bytes(output)


def validate_config(config):
    client = config.get("client_uid")
    engine = config.get("engine_uid")
    require(type(client) is int and type(engine) is int and 0 < client < 2**32 and
            0 < engine < 2**32 and client != engine, "root/Engine/client UID separation is required")


def client_cancelled(client):
    readable, _, _ = select.select([client], [], [], 0)
    return bool(readable)  # EOF or any extra challenge data cancels execution.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=pathlib.Path)
    args = parser.parse_args()
    require(os.geteuid() == 0, "broker requires external root-controlled identity")
    controlled(pathlib.Path(__file__).resolve())
    controlled(args.config)
    raw_config = args.config.read_bytes()
    require(len(raw_config) <= 16384, "broker config exceeds bound")
    config = json.loads(raw_config)
    validate_config(config)
    config["config_sha256"] = digest(raw_config)
    config["_config_path"] = str(args.config)
    config["_broker_sha256"] = sha_file(__file__)
    controlled(config["signing_key"], private=True)
    controlled(config["snapshot_parent"])
    signer = load_pem_private_key(pathlib.Path(config["signing_key"]).read_bytes(), password=None)
    endpoint = pathlib.Path(config["broker_socket"])
    controlled(endpoint.parent)
    require(not endpoint.exists(), "broker socket must be fresh")
    def deadline(_signal, _frame):
        raise RuntimeError("absolute broker transaction deadline")
    signal.signal(signal.SIGALRM, deadline)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(endpoint))
    endpoint.chmod(0o666)
    server.listen(4)
    print("DOXA_ATTESTED_BROKER_READY", flush=True)
    try:
        while True:
            client, _ = server.accept()
            with client:
                client.settimeout(2)
                signal.setitimer(signal.ITIMER_REAL, 50)
                try:
                    _, uid, _ = struct.unpack("3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    require(uid == config["client_uid"], "unreviewed semantic client identity")
                    length = int.from_bytes(read_exact(client, 4), "big")
                    require(0 < length <= 16384, "client challenge exceeds16KiB")
                    raw = read_exact(client, length)
                    config["_cancel"] = lambda: client_cancelled(client)
                    config["_client"] = client
                    receipt = transaction(config, json.loads(raw), raw)
                    signal.setitimer(signal.ITIMER_REAL, 2)
                    print(json.dumps({"event":"DOXA_ATTESTED_BROKER_RECEIPT",
                        "status":receipt.get("status"),"error":receipt.get("error"),
                        "cleanup_removed":receipt.get("cleanup_removed"),
                        "container_id":receipt.get("container_id"), "analyzer_latest_status":receipt.get("analyzer_latest_status"),
                        "analyzer_messages":receipt.get("analyzer_messages"),
                        "analyzer_stderr":receipt.get("analyzer_stderr")},sort_keys=True),flush=True)
                    payload = compact(receipt)
                    signed = signer.sign(payload)
                    require(len(signed) == 64, "Ed25519 signing failed")
                    envelope = compact({"payload_hex": payload.hex(), "signature_hex": signed.hex()})
                    require(len(envelope) <= MAX_OUTPUT, "signed receipt exceeds256KiB")
                    client.sendall(len(envelope).to_bytes(4, "big") + envelope)
                except Exception as error:
                    print(f"DOXA_BROKER_REFUSED {str(error)[:256]}", flush=True)
                finally:
                    signal.setitimer(signal.ITIMER_REAL, 0)
    finally:
        server.close()
        endpoint.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
