#!/usr/bin/env python3
"""Actual UID1000 fixture daemon/frontend join on the disposable guest.

One frontend prompt exercises production Runtime/IsolationHost/ROOT guard.
The fixture is host-side deterministic text, never a vendor/provider proof.
No Engine CLI, shared configuration change, paid call or ready flag authorizes.
"""
from __future__ import annotations
import argparse
import ctypes
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import termios
import time

SID = "root-daemon-20261013"
PROJECT = 5011
DAEMON_SHA = "bdab19e567fe3e7b337414933f64d90be5cd361c55434b482d1d1cd7e378b1aa"
FRONTEND_SHA = "5b4d8ef78eb7c42d84fe9c80f4220cdefdad6d2cad5987399dedc5497aa965c2"
BROKER_SHA = "6f83930003e5976e8e64f4950ff460c8795ffcc5d12c810f1218b09ff9a62fb5"
GATEWAY_SHA = "18d1d633a67aee1dd1bfd67eaf5de82ebcc42dfb0ef112198c6691439de98fe9"
WORKER_SHA = "cfbf9586d176289a53cfbe5c9c8fbd9d7f253a4c20525941dceb7bc68bf2dd1f"
EXPECTED_TEXT = "Deterministic native fixture response."
PUBLIC = Path("/usr/libexec/doxa/daemon-proof-20261013")
MAX_FRAME = 65536
MAX_OUTPUT = 2 * 1024 * 1024
FAILED_SPAWNS = []


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


def root_artifact(path, digest=None, maximum=1024*1024):
    path = Path(path)
    require(path.is_absolute() and all(p not in (".", "..") for p in path.parts), "noncanonical ROOT artifact")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for index, component in enumerate(path.parts[1:]):
            parent = os.fstat(descriptor)
            require(stat.S_ISDIR(parent.st_mode) and parent.st_uid == 0 and parent.st_mode & 0o022 == 0,
                    "ROOT artifact ancestor is mutable outside ROOT")
            final = index == len(path.parts) - 2
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            if not final: flags |= os.O_DIRECTORY
            copied = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor); descriptor = copied
        before = os.fstat(descriptor)
        require(stat.S_ISREG(before.st_mode) and before.st_uid == 0 and before.st_nlink == 1
                and before.st_mode & 0o7022 == 0 and 0 < before.st_size <= maximum,
                "ROOT artifact type/ownership/mode/size differs")
        data = bytearray()
        while chunk := os.read(descriptor, 65536):
            data.extend(chunk); require(len(data) <= maximum, "ROOT artifact read exceeds bound")
        after = os.fstat(descriptor)
        require((before.st_dev,before.st_ino,before.st_size,before.st_ctime_ns,before.st_mtime_ns)
                == (after.st_dev,after.st_ino,after.st_size,after.st_ctime_ns,after.st_mtime_ns), "ROOT artifact changed")
        if digest is not None: require(hashlib.sha256(data).hexdigest() == digest, "ROOT artifact content pin differs")
        return bytes(data)
    finally:
        os.close(descriptor)


def private_new(path, data, mode=0o600, uid=0):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    with os.fdopen(fd, "wb") as out:
        os.fchmod(out.fileno(), mode)
        os.fchown(out.fileno(), uid, uid)
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


def proc(pid):
    raw = Path(f"/proc/{pid}/stat").read_text()
    fields = raw[raw.rindex(")") + 2:].split()
    ids = Path(f"/proc/{pid}/status").read_text().splitlines()
    uid = [int(v) for v in next(v for v in ids if v.startswith("Uid:")).split()[1:]]
    return {"pid": pid, "state": fields[0], "ppid": int(fields[1]), "pgid": int(fields[2]),
            "starttime": int(fields[19]), "uids": uid}


def alive(fd):
    p = select.poll()
    p.register(fd, select.POLLIN)
    return not p.poll(0)


class Budget:
    def __init__(self, seconds):
        self.end = time.monotonic() + seconds

    def left(self, maximum=1.0):
        left = self.end - time.monotonic()
        require(left > 0, "shared proof deadline expired")
        return min(left, maximum)


def child_setup(parent, tty=False):
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0 or libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        os._exit(125)
    if os.getppid() != parent:
        os._exit(125)
    if tty:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def failed_spawn(process, descriptor, original):
    failure = {"pid": process.pid, "error": str(original), "teardown_error": None}
    try:
        if descriptor is not None:
            if alive(descriptor): signal.pidfd_send_signal(descriptor, signal.SIGKILL)
        elif process.returncode is None:
            # This direct child has never been waited/polled by another
            # thread. Its PID remains reserved even if it has just exited.
            process.kill()
        process.wait(timeout=2)
    except BaseException as cause:
        failure["teardown_error"] = str(cause)
    finally:
        for file in (process.stdin, process.stdout, process.stderr):
            if file is not None: file.close()
    failure["returncode"] = process.returncode
    if process.returncode is None:
        # Preserve ownership for the orchestrator's cleanup retry. A
        # constructor failure cannot leave an unrecorded background task.
        failure.update(process=process, pidfd=descriptor)
    elif descriptor is not None:
        os.close(descriptor)
    FAILED_SPAWNS.append(failure)


def pin_spawned(process, uid, snapshot=proc, opener=os.pidfd_open):
    """Pin only a direct, still-unreaped Popen child; never a /proc selection."""
    descriptor = None
    try:
        descriptor = opener(process.pid)
        identity = snapshot(process.pid)
        require(identity["uids"] == [uid] * 4, "actual child UID differs")
        return descriptor, identity
    except BaseException as original:
        failed_spawn(process, descriptor, original)
        raise


class Child:
    def __init__(self, name, argv, env, uid=0, tty=None):
        self.name = name
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.pending = bytearray()
        self.records = []
        parent = os.getpid()
        self.process = subprocess.Popen(argv, env=env, cwd="/", stdin=tty if tty is not None else subprocess.PIPE,
            stdout=tty if tty is not None else subprocess.PIPE,
            stderr=tty if tty is not None else subprocess.PIPE,
            user=uid, group=uid, extra_groups=[], close_fds=True, start_new_session=True,
            preexec_fn=lambda: child_setup(parent, tty is not None))
        self.pidfd, self.identity = pin_spawned(self.process, uid)
        try:
            self.channels = []
            for label in ("stdout", "stderr"):
                file = getattr(self.process, label)
                if file is not None:
                    os.set_blocking(file.fileno(), False)
                    self.channels.append((file.fileno(), label))
        except BaseException as original:
            failed_spawn(self.process, self.pidfd, original)
            raise

    def revalidate(self):
        if alive(self.pidfd):
            require(proc(self.process.pid)["starttime"] == self.identity["starttime"], "owned process changed")

    def send(self, data):
        self.revalidate()
        require(self.process.stdin is not None and alive(self.pidfd), "owned command recipient exited")
        os.write(self.process.stdin.fileno(), data)

    def signal(self, sig):
        self.revalidate()
        if alive(self.pidfd):
            signal.pidfd_send_signal(self.pidfd, sig)

    def evidence(self):
        return dict(self.identity, name=self.name, returncode=self.process.poll(),
                    pidfd_exited=not alive(self.pidfd), stdout=self.stdout.decode(errors="replace"),
                    stderr=self.stderr.decode(errors="replace"))


class Pump:
    def __init__(self):
        self.children = []
        self.pty = None
        self.terminal = bytearray()

    def add(self, child):
        self.children.append(child)
        return child

    def step(self, budget):
        channels = {fd: (child, label) for child in self.children for fd, label in child.channels}
        if self.pty is not None:
            channels[self.pty] = (None, "terminal")
        ready, _, _ = select.select(list(channels), [], [], budget.left(.1))
        for fd in ready:
            child, label = channels[fd]
            try:
                data = os.read(fd, 16384)
            except OSError as cause:
                if cause.errno == 5 and child is None:
                    data = b""
                else:
                    raise
            if not data:
                if child is None:
                    self.pty = None
                else:
                    child.channels = [(f, s) for f, s in child.channels if f != fd]
                continue
            if child is None:
                self.terminal.extend(data)
                require(len(self.terminal) <= 16 * 1024 * 1024, "terminal evidence exceeds bound")
            else:
                target = getattr(child, label)
                target.extend(data)
                require(len(target) <= MAX_OUTPUT, "owned process output exceeds bound")
                if child.name == "observer" and label == "stdout":
                    child.pending.extend(data)
                    while b"\n" in child.pending:
                        line, _, rest = child.pending.partition(b"\n")
                        require(len(line) <= MAX_FRAME, "observer record exceeds bound")
                        child.records.append(json.loads(line))
                        child.pending = bytearray(rest)
                    require(len(child.pending) <= MAX_FRAME, "observer partial record exceeds bound")

    def wait(self, predicate, budget):
        while not predicate():
            self.step(budget)

    def exit(self, child, budget):
        self.wait(lambda: not alive(child.pidfd), budget)
        child.process.wait(timeout=budget.left(2))
        while child.channels:
            self.step(budget)
        return child.process.returncode


def observer(args):
    require(os.getuid() == os.geteuid() == 1000, "observer must be actual UID1000")
    budget = Budget(args.seconds)
    channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    channel.settimeout(budget.left(10))
    channel.connect(str(args.socket))
    pid, uid, gid = struct.unpack("3i", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    peer = struct.unpack("i", channel.getsockopt(socket.SOL_SOCKET, 77, 4))[0]
    require((pid, uid, gid) == (args.daemon_pid, 1000, 1000) and alive(peer), "observer peer is not owned daemon")
    require(proc(pid)["starttime"] == args.daemon_starttime, "observer peer starttime changed")
    channel.setblocking(False)
    os.set_blocking(0, False)
    pending = bytearray()
    commands = bytearray()
    attached = False
    request_id = 100
    def emit(row):
        print(json.dumps(row, sort_keys=True), flush=True)
    emit({"kind": "peer", "uid": os.getuid(), "pid": os.getpid(), "daemon": proc(pid)})
    try:
        while True:
            ready, _, _ = select.select([channel, 0], [], [], budget.left(.1))
            if channel in ready:
                data = channel.recv(16384)
                if not data:
                    emit({"kind": "closed"})
                    return
                pending.extend(data)
                require(len(pending) <= 2 * MAX_FRAME, "daemon partial framing bound")
                while b"\n" in pending:
                    line, _, rest = pending.partition(b"\n")
                    require(len(line) <= MAX_FRAME, "daemon frame bound")
                    pending = bytearray(rest)
                    frame = json.loads(line)
                    emit({"kind": "frame", "frame": frame})
                    if not attached:
                        require(frame.get("type") == "hello" and frame.get("session_id") == SID
                                and frame.get("engine") == "fixture", "observer hello scope differs")
                        payload = json.dumps({"type": "attach", "cursor": frame["next_seq"]}).encode() + b"\n"
                        channel.settimeout(budget.left(2)); channel.sendall(payload); channel.setblocking(False)
                        attached = True
            if 0 in ready:
                data = os.read(0, 4096)
                if not data:
                    return
                commands.extend(data)
                require(len(commands) <= 4096, "observer control bound")
                while b"\n" in commands:
                    line, _, rest = commands.partition(b"\n")
                    commands = bytearray(rest)
                    op = line.decode()
                    require(op in ("status", "stop_if_idle", "finish"), "observer cannot submit prompts or arbitrary RPC")
                    if op == "finish":
                        return
                    request_id += 1
                    payload = json.dumps({"type": "call", "id": request_id, "method": op, "params": {}}).encode() + b"\n"
                    channel.settimeout(budget.left(2)); channel.sendall(payload); channel.setblocking(False)
                    emit({"kind": "request", "id": request_id, "method": op})
    finally:
        channel.close()
        os.close(peer)


def scoped_processes(policy, public_script):
    """Diagnostic census only; unknown tasks are never killed from argv hints."""
    needles = {str(policy), str(Path("/run/doxa/hardened") / SID / "manifest.json")}
    result = []
    for directory in Path("/proc").iterdir():
        if not directory.name.isdecimal():
            continue
        try:
            argv = (directory / "cmdline").read_bytes().split(b"\0")
            strings = [v.decode(errors="replace") for v in argv if v]
            own_scope = any(v in needles for v in strings) or (str(public_script) in strings and "--observer" in strings)
            if own_scope:
                result.append(dict(proc(int(directory.name)), argv=strings))
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    return result


def main(args):
    require(args.ack_disposable and os.geteuid() == 0 and os.uname().nodename == "doxa-proof", "disposable ROOT guest acknowledgement required")
    require(args.session_id == SID and args.project_id == PROJECT, "only reserved daemon scope may be used")
    budget = Budget(args.seconds)
    require(args.output.is_absolute() and args.output.is_relative_to("/root/isolation-proof") and not args.output.exists(), "fresh private evidence directory required")
    args.output.mkdir(mode=0o700)
    report = {"scope": "actual-uid1000-fixture-daemon-frontend-root-guard", "session_id": SID,
              "project_id": PROJECT, "paid_calls": 0, "live_provider_verified": False,
              "daemon_source": "934661cd", "status": "failed", "cleanup": {}}
    pump = Pump()
    broker = daemon = frontend = obs = None
    engine = module = None
    cid = cgroup = None
    master = slave = None
    body_error = None
    cleanup_errors = []
    state = Path("/run/doxa/hardened") / SID
    quota_root = Path("/srv/doxa-quota-proof") / SID
    policy_path = Path("/etc/doxa/hardened") / (SID + ".json")
    caller_base = Path("/srv/doxa-daemon-proof-20261013")
    runtime = Path("/run/user/1000/df-20261013")
    public_script = PUBLIC / "prove-hardened-daemon.py"
    try:
        for path, digest in [(PUBLIC / "doxa-daemon", DAEMON_SHA), (PUBLIC / "doxa-rs", FRONTEND_SHA)]:
            m = path.lstat()
            require(stat.S_ISREG(m.st_mode) and m.st_uid == 0 and m.st_gid == 0 and m.st_nlink == 1
                    and stat.S_IMODE(m.st_mode) == 0o555 and sha(path) == digest, "staged immutable caller binary differs")
        require(hashlib.sha256(root_artifact(public_script)).hexdigest() == sha(__file__), "public observer source differs from executed harness")
        policy = json.loads(root_artifact(args.template_policy))
        require(policy["caller_uid"] == 1000 and policy["engine"]["uid"] == policy["engine"]["gid"] == 1001,
                "template identities differ")
        require(policy["broker"]["sha256"] == BROKER_SHA and policy["authority"]["sha256"] == GATEWAY_SHA
                and policy["worker"]["sha256"] == WORKER_SHA, "template must use final frozen ROOT binaries")
        # Authenticate code bytes and no-follow ROOT ancestors before importing
        # any Python. A caller pathname cannot select privileged proof code.
        root_artifact(policy["broker"]["path"], BROKER_SHA, 4*1024*1024)
        spec = importlib.util.spec_from_file_location("reviewed_daemon_root_broker", policy["broker"]["path"])
        require(spec is not None and spec.loader is not None, "frozen ROOT broker source missing")
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        module.controlled(args.template_policy)
        for path, digest in [(policy["broker"]["path"], BROKER_SHA), (policy["authority"]["path"], GATEWAY_SHA)]:
            module.controlled(Path(path)); require(sha(path) == digest, "frozen ROOT artifact changed")
        require(not any(p.exists() for p in (state, quota_root, policy_path, caller_base, runtime)), "proof never adopts existing scope")
        policy["session_id"] = SID; policy["root"] = str(quota_root)
        module.validate_policy(policy)
        engine = module.Engine(policy)
        # The constructor has no kernel peer capability. Establish the real
        # pinned Unix connection before using its PID/socket observations.
        channel = engine.connect(module.Deadline(budget.left(10)))
        channel.sock.close()
        require(engine.pid == 27821 and engine.socket_identity[1] == 688, "shared Engine identity differs")
        before = {"pid": engine.pid, "socket": engine.socket_identity, "config_sha256": sha(policy["engine"]["config_path"])}
        report["engine_before"] = before
        report["pins"] = {"daemon": DAEMON_SHA, "frontend": FRONTEND_SHA, "broker": BROKER_SHA,
                          "gateway": GATEWAY_SHA, "worker": WORKER_SHA, "image": policy["image"]}
        caller_base.mkdir(mode=0o755)
        caller = caller_base / "uid1000"; caller.mkdir(mode=0o700); os.chown(caller, 1000, 1000)
        for name in ("source", "home", "state"):
            p = caller / name; p.mkdir(mode=0o700); os.chown(p, 1000, 1000)
        require(Path("/run/user/1000").stat().st_uid == 1000 and stat.S_IMODE(Path("/run/user/1000").stat().st_mode) == 0o700,
                "existing caller runtime parent must be private")
        runtime.mkdir(mode=0o700); os.chown(runtime, 1000, 1000)
        private_new(caller / "source" / "plain.txt", b"credential-free daemon fixture\n", uid=1000)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(caller / "home"), "DOXA_HOME": str(caller / "state"),
               "DOXA_RUNTIME_DIR": str(runtime), "DOXA_LORE": "0", "DOXA_WORKTREE": "0",
               "XDG_RUNTIME_DIR": "/run/user/1000", "LANG": "C.UTF-8", "TERM": "xterm-256color",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
               "TMPDIR": str(caller / "home")}
        def command(name, argv, uid=0, environment=None):
            child = pump.add(Child(name, argv, environment or {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, uid))
            code = pump.exit(child, budget)
            require(code == 0, name + " failed: " + child.stderr.decode(errors="replace")[:1024])
            return child
        for number, argv in enumerate([["/usr/bin/git", "init", "-q", str(caller / "source")],
            ["/usr/bin/git", "-C", str(caller / "source"), "add", "."],
            ["/usr/bin/git", "-C", str(caller / "source"), "-c", "user.name=Disposable Fixture",
             "-c", "user.email=fixture@example.invalid", "commit", "-qm", "test: add daemon fixture source"]]):
            command("git" + str(number), argv, 1000, env)
        provision = command("quota-provision", [str(args.quota_provision), "create", SID, str(quota_root), "1000", "1001", "1001",
                             str(PROJECT), str(policy["limits"]["disk_bytes"]), policy["image"], WORKER_SHA])
        report["quota_provision"] = provision.stdout.decode()
        source = args.output / "source-policy.json"
        private_new(source, json.dumps(policy, sort_keys=True).encode() + b"\n")
        python = str(Path(sys.executable).resolve())
        installed = command("root-install", [python, "-I", policy["broker"]["path"], "install", "--source", str(source), "--destination", str(policy_path)])
        report["receipt"] = json.loads(installed.stdout)
        broker = pump.add(Child("root-broker", [python, "-I", policy["broker"]["path"], "serve", "--policy", str(policy_path)],
                                {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}))
        pump.wait(lambda: (state / "broker-instance.json").exists() or not alive(broker.pidfd), budget)
        require(alive(broker.pidfd), "owned ROOT broker failed before startup")
        daemon_argv = [str(PUBLIC / "doxa-daemon"), "--session-id", SID, "--runtime-dir", str(runtime),
                       "--cwd", str(caller / "source"), "--engine", "fixture", "--isolation", "docker-hardened",
                       "--linger", "30", "--resume", "false"]
        rejected = pump.add(Child("fixture-resume-rejection", daemon_argv[:-1] + ["true"], env, 1000))
        require(pump.exit(rejected, budget) != 0 and b"Codex options require --engine codex" in rejected.stderr,
                "fixture resume must reject before Runtime/registry startup")
        report["fixture_resume_rejected"] = rejected.evidence()
        daemon = pump.add(Child("daemon", daemon_argv, env, 1000))
        registry = runtime / "registry" / (SID + ".json")
        pump.wait(lambda: registry.exists() or not alive(daemon.pidfd), budget)
        require(alive(daemon.pidfd), "actual UID1000 daemon failed before registry")
        entry = json.loads(registry.read_bytes()); require(entry["pid"] == daemon.process.pid and entry["session_id"] == SID, "registry hint differs from pinned daemon")
        daemon_socket = Path(entry["daemon_socket"])
        require(daemon_socket.parent == runtime and daemon_socket.lstat().st_uid == 1000
                and stat.S_ISSOCK(daemon_socket.lstat().st_mode), "daemon socket escaped own runtime")
        root_manifest = json.loads((state / "manifest.json").read_bytes())
        cid = root_manifest["cid"]; require(re.fullmatch(r"[0-9a-f]{64}", cid), "ROOT exact CID unavailable")
        info_status, info = engine.request(module.Deadline(budget.left(10)), "GET", f"/containers/{cid}/json")
        require(info_status == 200, "actual session CID not live")
        init = module.host_init_pid(engine.pid, info["State"]["Pid"])
        cgroup = Path(f"/proc/{init}/cgroup").read_text().strip().removeprefix("0::")
        report.update(cid=cid, host_init_pid=init, actual_cgroup=cgroup, registry=entry)
        obs = pump.add(Child("observer", [python, "-I", str(public_script), "--observer", "--socket", str(daemon_socket),
            "--daemon-pid", str(daemon.process.pid), "--daemon-starttime", str(daemon.identity["starttime"]), "--seconds", str(int(budget.left(240)))], env, 1000))
        def frames(): return [row["frame"] for row in obs.records if row.get("kind") == "frame"]
        pump.wait(lambda: any(frame.get("type") == "hello" for frame in frames()) or not alive(obs.pidfd), budget)
        hello = next(frame for frame in frames() if frame.get("type") == "hello")
        require(hello["isolation"]["profile"] == "docker-hardened" and hello["engine"] == "fixture", "daemon hello did not expose actual IsolationHost")
        report["hello"] = hello
        master, slave = os.openpty(); fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("4H", 48, 160, 0, 0))
        os.set_blocking(master, False); pump.pty = master
        frontend = pump.add(Child("frontend", [str(PUBLIC / "doxa-rs"), "attach", SID], env, 1000, slave))
        os.close(slave); slave = None
        obs.send(b"status\n")
        pump.wait(lambda: any(frame.get("type") == "reply" and frame.get("id") == 101 for frame in frames()), budget)
        # Wait for raw terminal mode and an actual frontend attach, not a sleep.
        pump.wait(lambda: b"\x1b[?1049h" in pump.terminal and not (termios.tcgetattr(master)[3] & termios.ICANON), budget)
        prompt = "UID1000 frontend ROOT guard fixture 20261013"
        os.write(master, prompt.encode() + b"\r")
        pump.wait(lambda: any(frame.get("type") == "event" and frame["event"]["type"] in ("turn_done", "turn_refused") for frame in frames()), budget)
        events = [frame["event"] for frame in frames() if frame.get("type") == "event"]
        require(sum(event["type"] == "turn_started" for event in events) == 1 and sum(event["type"] == "turn_done" for event in events) == 1
                and not any(event["type"] == "turn_refused" for event in events), "one frontend fixture turn did not terminate successfully")
        require("".join(event["data"].get("text", "") for event in events if event["type"] == "text_delta") == EXPECTED_TEXT,
                "deterministic fixture output differs")
        ansi = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")
        pump.wait(lambda: EXPECTED_TEXT.encode() in ansi.sub(b"", pump.terminal), budget)
        obs.send(b"status\n")
        pump.wait(lambda: any(frame.get("type") == "reply" and frame.get("id") == 102 for frame in frames()), budget)
        idle = next(frame for frame in frames() if frame.get("type") == "reply" and frame.get("id") == 102)
        require(idle["ok"] and idle["status"]["running"] is False and idle["status"]["queued"] == 0, "exact fixture daemon not idle")
        report.update(prompt=prompt, events=events, idle=idle, frontend_rendered=True, isolation_host_root_guard_turn=True)
        os.write(master, b"\x11")
        require(pump.exit(frontend, budget) == 0, "captured frontend did not exit after CtrlQ")
        obs.send(b"stop_if_idle\n")
        pump.wait(lambda: any(frame.get("type") == "reply" and frame.get("id") == 103 for frame in frames()), budget)
        stopped = next(frame for frame in frames() if frame.get("type") == "reply" and frame.get("id") == 103)
        require(stopped["ok"], "owned stop_if_idle refused")
        report["owner_stop_if_idle"] = stopped
        require(pump.exit(daemon, budget) == 0, "captured daemon did not cleanly stop")
    except BaseException as cause:
        body_error = str(cause)
    finally:
        cleanup = Budget(45)
        for failed in FAILED_SPAWNS:
            process = failed.get("process")
            descriptor = failed.get("pidfd")
            if process is not None:
                try:
                    if descriptor is not None:
                        if alive(descriptor): signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                    elif process.returncode is None: process.kill()
                    process.wait(timeout=cleanup.left(2))
                except BaseException as cause: cleanup_errors.append("failed spawn teardown: " + str(cause))
                finally:
                    if descriptor is not None: os.close(descriptor)
            if failed.get("teardown_error"): cleanup_errors.append("initial spawn teardown: " + failed["teardown_error"])
        report["failed_spawns"] = [{k:v for k,v in row.items() if k not in ("process", "pidfd")} for row in FAILED_SPAWNS]
        # Signal only retained exact own pidfds, never process groups or argv hints.
        first = [child for child in (frontend, obs, daemon) if child is not None]
        owned = first + [child for child in pump.children if child is not broker and child not in first]
        for child in owned:
            if child is None: continue
            try:
                if alive(child.pidfd): child.signal(signal.SIGTERM)
                pump.exit(child, Budget(cleanup.left(5)))
            except BaseException as cause:
                cleanup_errors.append(child.name + ": " + str(cause))
                try: child.signal(signal.SIGKILL); pump.exit(child, Budget(cleanup.left(2)))
                except BaseException as final: cleanup_errors.append(child.name + " kill: " + str(final))
        if broker is not None:
            try:
                broker.signal(signal.SIGTERM); pump.exit(broker, Budget(cleanup.left(5)))
            except BaseException as cause:
                cleanup_errors.append("broker: " + str(cause))
                try: broker.signal(signal.SIGKILL); pump.exit(broker, Budget(cleanup.left(2)))
                except BaseException as final: cleanup_errors.append("broker kill: " + str(final))
        if engine is not None:
            try:
                if cid is None and (state / "manifest.json").exists():
                    row = json.loads((state / "manifest.json").read_bytes())
                    require(row["session_id"] == SID, "cleanup manifest scope changed"); cid = row.get("cid")
                if cid is not None:
                    require(re.fullmatch(r"[0-9a-f]{64}", cid), "cleanup CID invalid")
                    status, info = engine.request(module.Deadline(cleanup.left(10)), "GET", f"/containers/{cid}/json", statuses=(200, 404))
                    if status == 200:
                        labels = info["Config"]["Labels"]
                        require(labels.get("io.doxa.root-session") == SID and labels.get("io.doxa.root-policy") == report["receipt"]["policy_sha256"], "cleanup live CID labels differ")
                        init = module.host_init_pid(engine.pid, info["State"]["Pid"])
                        cgroup = Path(f"/proc/{init}/cgroup").read_text().strip().removeprefix("0::")
                        engine.request(module.Deadline(cleanup.left(10)), "DELETE", f"/containers/{cid}?force=1&v=1", statuses=(204,))
                        cleanup_errors.append("diagnostic forced CID cleanup required")
                    status, _ = engine.request(module.Deadline(cleanup.left(10)), "GET", f"/containers/{cid}/json", statuses=(404,))
                    report["cleanup"]["cid404"] = status == 404
                if cgroup is not None:
                    pump.wait(lambda: not Path("/sys/fs/cgroup", cgroup.lstrip("/")).exists(), cleanup)
                    report["cleanup"]["cgroup_absent"] = True
                engine.live()
                require(engine.pid == before["pid"] and engine.socket_identity == tuple(before["socket"])
                        and sha(policy["engine"]["config_path"]) == before["config_sha256"], "shared Engine changed")
                report["cleanup"]["shared_engine_unchanged"] = True
            except BaseException as cause: cleanup_errors.append("Engine cleanup: " + str(cause))
        try:
            pump.wait(lambda: not scoped_processes(policy_path, public_script), cleanup)
            sockets = [path for directory in (state, quota_root / "broker", runtime) if directory.exists()
                       for path in directory.rglob("*") if stat.S_ISSOCK(path.lstat().st_mode)]
            require(not sockets, "owned scope socket remains")
            require(not (runtime / "registry" / (SID + ".json")).exists(), "owned registry remains")
            if (state / "manifest.json").exists():
                row = json.loads((state / "manifest.json").read_bytes())
                require(row.get("status") == "stopped", "ROOT scope did not reach stopped")
                report["cleanup"]["root_stopped"] = True
            report["cleanup"].update(remaining_scoped_processes=0, sockets_absent=True, registry_absent=True)
        except BaseException as cause: cleanup_errors.append("scope census: " + str(cause))
        report["processes"] = [child.evidence() for child in pump.children]
        for child in pump.children:
            if alive(child.pidfd): cleanup_errors.append(child.name + " pidfd still live")
        if obs is not None: report["observer_records"] = obs.records
        private_new(args.output / "terminal.raw", bytes(pump.terminal))
        report["cleanup"]["errors"] = cleanup_errors
        if body_error is not None: report["error"] = body_error
        required = ["cid404", "cgroup_absent", "shared_engine_unchanged", "root_stopped", "sockets_absent", "registry_absent"]
        if body_error is None and not cleanup_errors and all(report["cleanup"].get(key) is True for key in required): report["status"] = "passed"
        private_new(args.output / "report.json", json.dumps(report, sort_keys=True, indent=2).encode() + b"\n")
        for child in pump.children: os.close(child.pidfd)
        if master is not None: os.close(master)
        if slave is not None: os.close(slave)
    print(json.dumps({"status": report["status"], "output": str(args.output), "scope": report["scope"]}))
    if report["status"] != "passed": raise SystemExit(1)


def self_test():
    """Actual known-child startup-failure teardown, without any guest setup."""
    for failed_open in (False, True):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
        def bad_snapshot(pid):
            row = proc(pid); row["uids"] = [-1]*4; return row
        def bad_open(_pid):
            raise PermissionError("injected pidfd-open failure")
        try:
            pin_spawned(process, os.geteuid(), snapshot=bad_snapshot,
                        opener=bad_open if failed_open else os.pidfd_open)
        except (RuntimeError, PermissionError):
            pass
        else:
            raise AssertionError("startup failure accepted")
        require(process.returncode is not None, "failed constructor left its actual child alive")
        require(all(file.closed for file in (process.stdin, process.stdout, process.stderr)), "failed constructor left pipe descriptors open")
        try: os.kill(process.pid, 0)
        except ProcessLookupError: pass
        else: raise AssertionError("failed constructor child was not reaped")
    from unittest.mock import patch
    spawned = []
    original_popen = subprocess.Popen
    def tracked_popen(*positional, **keywords):
        # This source-only test keeps its current credentials; the actual
        # disposable-guest Child path still uses explicit UID/GID/groups.
        for key in ("user", "group", "extra_groups"): keywords.pop(key, None)
        process = original_popen(*positional, **keywords)
        spawned.append(process)
        return process
    with patch.object(subprocess, "Popen", tracked_popen), patch.object(os, "set_blocking", side_effect=OSError("injected pipe setup failure")):
        try:
            Child("test", [sys.executable, "-c", "import time; time.sleep(30)"], dict(os.environ), uid=os.geteuid())
        except OSError:
            pass
        else:
            raise AssertionError("pipe setup failure accepted")
    require(len(spawned) == 1 and spawned[0].returncode is not None, "pipe setup failure left its actual child alive")
    require(all(file.closed for file in (spawned[0].stdin, spawned[0].stdout, spawned[0].stderr)), "pipe setup failure left descriptors open")
    print("known-child UID-validation, pidfd-open and pipe-setup failures killed/reaped; all pipes closed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observer", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--socket", type=Path)
    parser.add_argument("--daemon-pid", type=int)
    parser.add_argument("--daemon-starttime", type=int)
    parser.add_argument("--seconds", type=int, default=240)
    parser.add_argument("--ack-disposable", action="store_true")
    parser.add_argument("--session-id", default=SID)
    parser.add_argument("--project-id", type=int, default=PROJECT)
    parser.add_argument("--template-policy", type=Path)
    parser.add_argument("--quota-provision", type=Path, default=Path("/root/isolation-proof/quota-bin/doxa-quota-provision-family"))
    parser.add_argument("--output", type=Path)
    options = parser.parse_args()
    require(10 <= options.seconds <= 300, "finite proof deadline outside bound")
    if options.self_test: self_test()
    elif options.observer: observer(options)
    else: main(options)
