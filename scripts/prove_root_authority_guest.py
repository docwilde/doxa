#!/usr/bin/env python3
"""ROOT-only, credential-free live-kernel proof on the disposable guest.

Uses the reviewed root broker's pinned Engine HTTP transport, never a Docker
CLI or shell. Native diagnostics report kernel facts and cannot activate a
profile. All own containers/listeners are removed and absence is checked.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import tarfile
import time


PROVIDER = r'''#!/usr/bin/env python3
"""Reviewed ROOT-authority proof fixture; no credentials or live provider."""
import array, ctypes, errno, fcntl, json, os, socket, struct, sys
from pathlib import Path

def exact(s, count):
    out = b''
    while len(out) < count:
        part = s.recv(count-len(out))
        if not part: return out
        out += part
    return out

def exchange(s, stage):
    body = json.dumps({'proof':'root-authority-origin','stage':stage,'pid':os.getpid()},sort_keys=True).encode()
    s.sendall(struct.pack('!I',len(body))+body)
    header = exact(s,4)
    if len(header)!=4: return {'reply_bytes':len(header),'reply':None}
    size = struct.unpack('!I',header)[0]
    if size>65536: raise RuntimeError('diagnostic reply exceeds bound')
    data = exact(s,size)
    return {'reply_bytes':len(data),'reply':json.loads(data) if len(data)==size else None}

def restrictions():
    result = {'uid':os.getuid(),'pid':os.getpid(),'cgroup':Path('/proc/self/cgroup').read_text().strip()}
    for path in ['/sys/fs/cgroup/cgroup.procs','/sys/fs/cgroup/cgroup.threads','/sys/fs/cgroup/cgroup.subtree_control']:
        try:
            fd=os.open(path,os.O_WRONLY|os.O_CLOEXEC|os.O_NOFOLLOW)
            os.close(fd); failure=None
        except OSError as cause: failure=cause.errno
        result[path]=failure
    libc=ctypes.CDLL(None,use_errno=True)
    arch=os.uname().machine
    calls={'x86_64':{'unshare':272,'setns':308,'ioctl':16},'aarch64':{'unshare':97,'setns':268,'ioctl':29}}[arch]
    for name,args in [('unshare',(0x02000000|0x00020000,)),('setns',(-1,0))]:
        ctypes.set_errno(0)
        rc=libc.syscall(ctypes.c_long(calls[name]),*[ctypes.c_long(v) for v in args])
        result[name]={'result':rc,'errno':ctypes.get_errno()}
    path=Path('/workspace/origin-restrictions')
    path.mkdir(mode=0o700)
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    original=bytearray(28);fcntl.ioctl(fd,0x801c581f,original,True)
    old=list(struct.unpack('IIIII8s',original));new=list(old);new[0]&=~0x200;new[3]=0
    try:fcntl.ioctl(fd,0x401c5820,struct.pack('IIIII8s',*new));failure=None
    except OSError as cause:failure=cause.errno
    final=bytearray(28);fcntl.ioctl(fd,0x801c581f,final,True);after=struct.unpack('IIIII8s',final)
    os.close(fd)
    result['quota_mutation']={'errno':failure,'project_before':old[3],'project_after':after[3],
        'inherits_before':bool(old[0]&0x200),'inherits_after':bool(after[0]&0x200)}
    result['status']={line.split(':',1)[0]:line.split(':',1)[1].strip() for line in Path('/proc/self/status').read_text().splitlines()
        if line.split(':',1)[0] in {'NoNewPrivs','Seccomp','Seccomp_filters','CapEff','CapPrm','CapBnd'}}
    return result

def main():
    action=sys.argv[1]
    if action=='restrictions':result=restrictions()
    else:
        s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);s.settimeout(40)
        s.connect('/run/doxa/session/egress-claude.sock')
        result={'action':action,'connector_pid':os.getpid(),'uid':os.getuid()}
        if action=='inside':
            result['stdio']={str(fd):{'name':os.readlink('/proc/self/fd/'+str(fd)),
                'device':os.fstat(fd).st_dev,'inode':os.fstat(fd).st_ino,'rdev':os.fstat(fd).st_rdev} for fd in (0,1,2)}
            result['private_devpts_device']=Path('/dev/pts').stat().st_dev
            result['frame']=exchange(s,'inside')
        elif action=='foreign-pty':
            result['first_frame']=exchange(s,'first')
            data,ancillary,flags,_=s.recvmsg(1,socket.CMSG_SPACE(4),socket.MSG_CMSG_CLOEXEC)
            if data!=b'F' or flags & (socket.MSG_TRUNC|socket.MSG_CTRUNC):raise RuntimeError('descriptor diagnostic framing')
            received=[]
            for level,kind,content in ancillary:
                if level!=socket.SOL_SOCKET or kind!=socket.SCM_RIGHTS:raise RuntimeError('unreviewed diagnostic ancillary')
                numbers=array.array('i');numbers.frombytes(content);received.extend(numbers)
            if len(received)!=1:raise RuntimeError('exactly one diagnostic descriptor required')
            fd=received[0]
            try:
                actual=os.fstat(fd)
                result['received_device']={'name':os.readlink('/proc/self/fd/'+str(fd)),'device':actual.st_dev,
                    'inode':actual.st_ino,'rdev':actual.st_rdev,'private_devpts_device':Path('/dev/pts').stat().st_dev}
                result['second_frame']=exchange(s,'foreign-device')
            finally:os.close(fd)
        elif action in ('delegated','post-handshake'):
            if action=='post-handshake':result['first_frame']=exchange(s,'first')
            pid=os.fork()
            if pid==0:
                try:
                    child={'writer_pid':os.getpid(),'frame':exchange(s,'delegated')}
                    print(json.dumps({'child':child}),flush=True)
                    code=0
                except (OSError,RuntimeError) as cause:
                    print(json.dumps({'child':{'writer_pid':os.getpid(),'error':str(cause)}}),flush=True);code=0
                os._exit(code)
            result['delegated_writer_pid']=pid
            _,status=os.waitpid(pid,0);result['child_status']=status
        else:raise RuntimeError('unsupported origin fixture action')
        s.close()
    print(json.dumps(result,sort_keys=True),flush=True)

if __name__=='__main__':main()
'''


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise RuntimeError(reason)


def sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


def private_write(path: Path, value: dict) -> None:
    data = json.dumps(value, sort_keys=True, indent=2).encode() + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "wb") as destination:
        destination.write(data)
        destination.flush()
        os.fsync(destination.fileno())


def replace_private(path: Path, value: dict) -> None:
    staged = path.with_name(path.name + ".new")
    private_write(staged, value)
    os.replace(staged, path)


def checked_process(argv: list[str], pass_fds: tuple[int, ...] = (), timeout: int = 40) -> dict:
    run = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         pass_fds=pass_fds, timeout=timeout, check=False)
    require(len(run.stdout) <= 128 * 1024 and len(run.stderr) <= 128 * 1024, "native diagnostic output bound")
    return {"argv": argv, "returncode": run.returncode, "stdout": run.stdout, "stderr": run.stderr}


def broker_import(path: Path):
    spec = importlib.util.spec_from_file_location("reviewed_root_broker", path)
    require(spec is not None and spec.loader is not None, "reviewed broker module missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def image_build(module, engine, base: str, output: Path, worker: Path | None, worker_sha: str) -> tuple[str, str]:
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", base) is not None, "immutable fixture base required")
    helper = PROVIDER.encode()
    digest = hashlib.sha256(helper).hexdigest()
    dockerfile = (f"FROM {base}\nCOPY origin-provider.py /usr/local/bin/claude\n"
                  + ("COPY doxa-isolation-worker /usr/local/bin/doxa-isolation-worker\n" if worker else "")
                  +
                  f"LABEL org.doxa.root-authority-proof=credential-free org.doxa.worker-sha256={worker_sha}\n").encode()
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as files:
        for name, data, mode in [("Dockerfile", dockerfile, 0o644), ("origin-provider.py", helper, 0o755)]:
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode, entry.uid, entry.gid, entry.mtime = len(data), mode, 0, 0, 0
            files.addfile(entry, io.BytesIO(data))
        if worker is not None:
            module.controlled(worker)
            require(sha_file(worker) == worker_sha and worker.stat().st_size < 128 * 1024 * 1024,
                    "reviewed current worker source differs or exceeds fixture bound")
            entry = tarfile.TarInfo("doxa-isolation-worker")
            entry.size, entry.mode, entry.uid, entry.gid, entry.mtime = worker.stat().st_size, 0o755, 0, 0, 0
            with worker.open("rb") as source:
                files.addfile(entry, source)
    data = archive.getvalue()
    (output / "origin-provider.py").write_bytes(helper)
    (output / "Dockerfile").write_bytes(dockerfile)
    deadline = module.Deadline(120)
    channel = engine.connect(deadline)
    try:
        query = "/build?t=doxa-root-authority-proof%3A20261013&dockerfile=Dockerfile&rm=1&forcerm=1&pull=0&networkmode=none"
        channel.send(f"POST /v{engine.config['api_version']}{query} HTTP/1.1\r\nHost: docker\r\n"
                     f"Content-Type: application/x-tar\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data)
        status, _, body = module.response(channel, 2 * 1024 * 1024)
        require(status == 200, "typed Engine fixture image build refused")
        response = body.read()
        require(len(response) <= 2 * 1024 * 1024, "fixture build log bound")
        (output / "image-build.ndjson").write_bytes(response)
        image = None
        for line in response.splitlines():
            item = json.loads(line)
            require(not item.get("error"), "Engine fixture image build failed")
            if "aux" in item:
                image = item["aux"].get("ID", image)
        # Classic builder reports its final ID via inspect of our own fixed tag.
        if image is None:
            _, info = engine.request(module.Deadline(10), "GET", "/images/doxa-root-authority-proof:20261013/json")
            image = info.get("Id")
        require(type(image) is str and re.fullmatch(r"sha256:[0-9a-f]{64}", image), "built fixture has no immutable image ID")
        engine.live()
        return image, digest
    finally:
        channel.sock.close()


class ProbeExec:
    def __init__(self, module, engine, cid: str, action: str, tty: bool = False):
        self.module, self.engine, self.cid = module, engine, cid
        self.tty = tty
        self.deadline = module.Deadline(60)
        _, created = engine.request(self.deadline, "POST", f"/containers/{cid}/exec", {
            "AttachStdin": True, "AttachStdout": True, "AttachStderr": True,
            "Tty": tty, "User": "0:0", "WorkingDir": "/workspace",
            "Cmd": [module.WORKER, "exec-hardened", "/usr/local/bin/claude", action],
            "Env": ["HOME=/home/doxa", "TMPDIR=/work-cache/tmp", "PATH=/usr/local/bin:/usr/bin:/bin"]}, (201,))
        self.eid = created.get("Id")
        require(type(self.eid) is str and re.fullmatch(r"[0-9a-f]{64}", self.eid), "invalid scoped exec ID")
        self.channel = engine.connect(self.deadline)
        # Verify the exact CID on the same pinned connection before upgrading.
        engine.exchange(self.channel, "GET", f"/containers/{cid}/json")
        engine.send_request(self.channel, "POST", f"/exec/{self.eid}/start", {"Detach": False, "Tty": tty}, upgrade=True)
        status, headers, _ = module.response(self.channel)
        require(status == 101 and headers.get("upgrade") == "tcp", "typed Engine exec did not upgrade")
        engine.live()

    def finish(self) -> dict:
        output, errors = bytearray(), bytearray()
        try:
            while True:
                self.deadline.wait(reads=[self.channel.sock])
                if not self.channel.sock.recv(1, socket.MSG_PEEK):
                    break
                if self.tty:
                    data = self.channel.sock.recv(65536)
                    require(data and len(output)+len(data) <= 128*1024, "private TTY raw stream bound")
                    output.extend(data)
                    continue
                header = self.channel.exact(8)
                count = int.from_bytes(header[4:], "big")
                require(header[0] in (1, 2) and header[1:4] == b"\0\0\0" and count <= 65536, "Engine exec framing drift")
                target = output if header[0] == 1 else errors
                require(len(target) + count <= 128 * 1024, "Engine exec output bound")
                target.extend(self.channel.exact(count))
            _, state = self.engine.request(self.module.Deadline(5), "GET", f"/exec/{self.eid}/json")
            require(state.get("ContainerID") == self.cid and state.get("Running") is False, "exact exec did not quiesce")
            return {"exec_id": self.eid, "exit_code": state.get("ExitCode"), "stdout": output.decode(), "stderr": errors.decode()}
        finally:
            self.channel.sock.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broker", type=Path, required=True)
    parser.add_argument("--gateway", type=Path, required=True)
    parser.add_argument("--quota-provision", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-image", default="sha256:5eb3f63be16d9bd9d0794e52feb02534ca6c1d581d535c8d3adb3df012aa2eaa")
    parser.add_argument("--worker-sha256", default="59055842c8f4c6f92d1058eeb60010118c563e42f2336221b2225534765ec250")
    parser.add_argument("--worker-file", type=Path)
    parser.add_argument("--engine-socket", type=Path, default=Path("/run/user/1001/doxa-proof/docker.sock"))
    parser.add_argument("--engine-exe", type=Path, default=Path("/usr/bin/dockerd"))
    parser.add_argument("--engine-config", type=Path, default=Path("/etc/doxa-proof/engine-daemon.json"))
    parser.add_argument("--engine-sha256", default="8f1c19d27e182ce6c82ddfd900d596195b0fd9d6740a9ff1ccac584e04355cb0")
    parser.add_argument("--engine-config-sha256", default="3a6774e75043931ce3dd1874bac3aabc86908a1bb21fe07c68eb7e46054f26c9")
    parser.add_argument("--session-id", default="root-authority-20261013")
    parser.add_argument("--project-id", type=int, default=5003)
    parser.add_argument("--root-base", type=Path, default=Path("/srv/doxa-quota-proof"))
    parser.add_argument("--positive-only", action="store_true")
    parser.add_argument("--trace-native", action="store_true")
    args = parser.parse_args()
    require(os.geteuid() == 0, "disposable guest proof requires ROOT")
    require(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,63}", args.session_id) is not None, "proof session ID is unsafe")
    require(5003 <= args.project_id <= 5010, "proof uses only reserved project range")
    require(args.output.is_absolute() and not args.output.exists(), "fresh private proof output required")
    args.output.mkdir(mode=0o700, parents=True)
    module = broker_import(args.broker)
    for path in [args.broker, args.gateway, args.quota_provision, args.engine_exe, args.engine_config]:
        module.controlled(path)
    endpoint = os.lstat(args.engine_socket)
    require(stat.S_ISSOCK(endpoint.st_mode) and endpoint.st_uid == endpoint.st_gid == 1001
            and stat.S_IMODE(endpoint.st_mode) == 0o600, "Engine socket is not normalized private Engine authority")
    root = args.root_base / args.session_id
    state = Path("/run/doxa/hardened") / args.session_id
    require(not root.exists() and not state.exists(), "proof must not adopt an existing session")
    state.mkdir(mode=0o700, parents=True)
    policy = {"version": 1, "session_id": args.session_id, "caller_uid": 1000,
        "engine": {"uid": 1001, "gid": 1001, "socket": str(args.engine_socket), "exe_path": str(args.engine_exe),
            "exe_sha256": args.engine_sha256, "config_path": str(args.engine_config),
            "config_sha256": args.engine_config_sha256, "api_version": "1.51"},
        "root": str(root), "image": args.base_image, "worker": {"path": module.WORKER, "sha256": args.worker_sha256},
        "providers": {"claude": {"path": "/usr/local/bin/claude", "sha256": "0" * 64,
            "artifacts": {"/usr/local/bin/claude": "0" * 64}, "egress": ["api.anthropic.com"],
            "proxy_port": 3128, "environment_keys": []}},
        "authority": {"path": str(args.gateway), "sha256": sha_file(args.gateway)},
        "broker": {"path": str(args.broker), "sha256": sha_file(args.broker)},
        "limits": {"memory_bytes": 512 * 1024 * 1024, "cpu_quota": 100000, "cpu_period": 100000,
            "pids": 128, "disk_bytes": 64 * 1024 * 1024, "output_bytes": 128 * 1024,
            "stdin_bytes": 128 * 1024, "deadline_seconds": 120, "operation_seconds": 30}}
    engine = module.Engine(policy)
    report = {"scope": "credential-free-kernel-authority-proof", "session_id": args.session_id,
              "base_image": args.base_image, "worker_sha256": args.worker_sha256,
              "native_gateway_sha256": policy["authority"]["sha256"], "cases": {}, "cleanup": {}}
    containers, cgroups, listeners = [], [], []
    active = None
    host_pty_fds = []
    try:
        image, helper_sha = image_build(module, engine, args.base_image, args.output, args.worker_file, args.worker_sha256)
        policy["image"] = image
        provider = policy["providers"]["claude"]
        provider["sha256"] = helper_sha
        provider["artifacts"]["/usr/local/bin/claude"] = helper_sha
        module.validate_policy(policy)
        report.update({"image": image, "provider_sha256": helper_sha, "engine_pid": engine.pid,
                       "engine_socket_device": engine.socket_identity[0], "engine_socket_inode": engine.socket_identity[1]})
        created = checked_process([str(args.quota_provision), "create", args.session_id, str(root), "1000", "1001", "1001",
                                   str(args.project_id), str(policy["limits"]["disk_bytes"]), image, args.worker_sha256])
        report["quota_provision"] = created
        require(created["returncode"] == 0, "ROOT quota provisioning failed")
        policy_path = state / "policy.json"
        private_write(policy_path, policy)
        policy_sha = sha_file(policy_path)
        _, created = engine.request(module.Deadline(10), "POST", "/containers/create", module.create_spec(policy, policy_sha), (201,))
        cid = created.get("Id")
        require(type(cid) is str and re.fullmatch(r"[0-9a-f]{64}", cid), "Engine returned invalid proof CID")
        containers.append(cid)
        engine.request(module.Deadline(10), "POST", f"/containers/{cid}/start", statuses=(204,))
        channel = engine.connect(module.Deadline(10))
        try:
            _, image_info = engine.exchange(channel, "GET", f"/images/{image}/json")
            require(image_info.get("Id") == image, "image metadata does not match exact immutable ID")
            image_labels = image_info.get("Config", {}).get("Labels") or {}
            require(type(image_labels) is dict and all(type(k) is type(v) is str for k,v in image_labels.items()),
                    "immutable image labels are malformed")
            _, info = engine.exchange(channel, "GET", f"/containers/{cid}/json")
        finally:
            channel.sock.close()
        init = module.host_init_pid(engine.pid, info["State"]["Pid"])
        cgroup = Path(f"/proc/{init}/cgroup").read_text().strip().removeprefix("0::")
        cgroups.append(cgroup)
        report["inspect_init"] = {"cid":cid,"host_init_pid":init,"cgroup":cgroup,
            "Config": {key:info.get("Config",{}).get(key) for key in
                ["Entrypoint","Cmd","User","WorkingDir","Tty","OpenStdin","Labels"]}}
        mounts = Path(f"/proc/{init}/mountinfo").read_text()
        require(len(mounts) <= 2 * 1024 * 1024, "kernel mount evidence exceeds bound")
        report["kernel_mountinfo"] = mounts
        module.inspect_exact(policy, policy_sha, cid, info, image_labels=image_labels)
        require(engine.archive_hash(module.Deadline(10), cid, module.WORKER) == args.worker_sha256, "actual image worker SHA differs")
        require(engine.archive_hash(module.Deadline(10), cid, "/usr/local/bin/claude") == helper_sha, "actual origin fixture SHA differs")
        manifest_path = state / "manifest.json"
        private_write(manifest_path, {"version": 1, "session_id": args.session_id, "policy_sha256": policy_sha,
            "cid": cid, "host_init_pid": init, "engine_pid": engine.pid,
            "engine_socket_dev": engine.socket_identity[0], "engine_socket_ino": engine.socket_identity[1],
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(), "gateway_pid": None, "status": "preparing"})
        native = [str(args.gateway), "kernel-check", "--policy", str(policy_path), "--manifest", str(manifest_path),
                  "--engine-pidfd", str(engine.pidfd)]
        traced_native = (["/usr/bin/strace", "-ff", "-o", str(args.output / "native-trace"), "-s", "128", *native]
                         if args.trace_native else native)
        result = checked_process(traced_native, (engine.pidfd,))
        report["cases"]["kernel_positive"] = result
        require(result["returncode"] == 0, "actual native kernel authority positive failed")
        facts = json.loads(result["stdout"])
        require(facts.get("cid") == cid and facts.get("quota_ready") is True and facts.get("filter_ready") is True
                and "ready" not in facts and "gateway_ready" not in facts, "kernel diagnostic promoted scope or omitted observed facts")
        restrictions = ProbeExec(module, engine, cid, "restrictions").finish()
        report["cases"]["worker_no_migration_authority"] = restrictions
        require(restrictions["exit_code"] == 0, "reviewed worker restriction fixture failed")
        observed = json.loads(restrictions["stdout"])
        require(all(observed[name] in (1, 13, 30) for name in ["/sys/fs/cgroup/cgroup.procs", "/sys/fs/cgroup/cgroup.threads", "/sys/fs/cgroup/cgroup.subtree_control"])
                and observed["unshare"]["errno"] == 1 and observed["setns"]["errno"] == 1
                and observed["quota_mutation"]["errno"] == 1
                and observed["quota_mutation"]["project_before"] == observed["quota_mutation"]["project_after"] == args.project_id,
                "worker retained cgroup/namespace/quota migration authority")
        result = checked_process(native, (engine.pidfd,))
        report["cases"]["kernel_after_restriction_probe"] = result
        require(result["returncode"] == 0, "native kernel authority changed after restriction probe")
        if args.positive_only:
            return

        endpoint_path = root / "broker" / "egress-claude.sock"
        for name, actor_cid, action, expected, frames, tty in [("inside", cid, "inside", 0, 1, False),
            ("private_pty", cid, "inside", 0, 1, True), ("delegated_descriptor", cid, "delegated", 1, 1, False),
            ("post_handshake_descriptor", cid, "post-handshake", 1, 2, False),
            ("foreign_host_pty", cid, "foreign-pty", 1, 2, False)]:
            command = native.copy()
            command[1] = "kernel-listener"
            command += ["--frames", str(frames)]
            inherited = (engine.pidfd,)
            if action == "foreign-pty":
                master, slave = os.openpty()
                host_pty_fds.extend([master, slave])
                command += ["--send-fd", str(slave)]
                inherited += (slave,)
                report["host_pty_source"] = {"name":os.ttyname(slave),"device":os.fstat(slave).st_dev,
                    "inode":os.fstat(slave).st_ino,"rdev":os.fstat(slave).st_rdev}
            active = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, pass_fds=inherited)
            listeners.append(endpoint_path)
            end = time.monotonic() + 10
            while not endpoint_path.exists() and active.poll() is None and time.monotonic() < end:
                time.sleep(.02)
            require(endpoint_path.exists(), "native kernel diagnostic listener did not bind")
            provider_exec = ProbeExec(module, engine, actor_cid, action, tty=tty)
            stdout, stderr = active.communicate(timeout=45)
            report["cases"][name] = {"native_returncode": active.returncode, "native_stdout": stdout, "native_stderr": stderr,
                                     "fixture": provider_exec.finish()}
            require((active.returncode == 0) == (expected == 0), f"native origin case {name} had wrong acceptance")
            active = None
            if endpoint_path.exists():
                endpoint_path.unlink()
            if action == "foreign-pty":
                for fd in host_pty_fds:
                    os.close(fd)
                host_pty_fds.clear()
        # A same-UID host writer is refused by actual container scope, while
        # the client identity is refused earlier by the kernel socket mode.
        for uid in [1000, 1001]:
            command = native.copy(); command[1] = "kernel-listener"; command += ["--frames", "1"]
            active = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, pass_fds=(engine.pidfd,))
            end = time.monotonic() + 10
            while not endpoint_path.exists() and active.poll() is None and time.monotonic() < end:
                time.sleep(.02)
            require(endpoint_path.exists(), "host-negative native listener did not bind")
            code = "import json,os,socket;os.setgroups([]);os.setgid("+str(uid)+");os.setuid("+str(uid)+");s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);s.settimeout(5)\ntry:s.connect("+repr(str(endpoint_path))+");print(json.dumps({'uid':os.getuid(),'connected':True}));s.sendall(b'\\x00\\x00\\x00\\x01X');s.recv(16)\nexcept OSError as e:print(json.dumps({'uid':os.getuid(),'errno':e.errno}))"
            actor = checked_process(["/usr/bin/python3", "-c", code], timeout=10)
            if uid == 1000:
                require('"errno": 13' in actor["stdout"], "client UID reached ROOT listener")
                active.terminate()
            stdout, stderr = active.communicate(timeout=10)
            report["cases"][f"host_uid_{uid}"] = {"native_returncode": active.returncode, "native_stdout": stdout,
                "native_stderr": stderr, "actor": actor}
            require(active.returncode != 0, "host writer was accepted as container origin")
            active = None
            if endpoint_path.exists():
                endpoint_path.unlink()
        sibling_spec = module.create_spec(policy, policy_sha)
        sibling_spec["Labels"]["io.doxa.root-proof-sibling"] = "credential-free-origin-negative"
        _, sibling_created = engine.request(module.Deadline(10), "POST", "/containers/create", sibling_spec, (201,))
        sibling = sibling_created.get("Id")
        require(type(sibling) is str and re.fullmatch(r"[0-9a-f]{64}", sibling), "sibling proof CID invalid")
        containers.append(sibling)
        engine.request(module.Deadline(10), "POST", f"/containers/{sibling}/start", statuses=(204,))
        _, sibling_info = engine.request(module.Deadline(10), "GET", f"/containers/{sibling}/json")
        sibling_init = module.host_init_pid(engine.pid, sibling_info["State"]["Pid"])
        cgroups.append(Path(f"/proc/{sibling_init}/cgroup").read_text().strip().removeprefix("0::"))
        command = native.copy(); command[1] = "kernel-listener"; command += ["--frames", "1"]
        active = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, pass_fds=(engine.pidfd,))
        end = time.monotonic() + 10
        while not endpoint_path.exists() and active.poll() is None and time.monotonic() < end:
            time.sleep(.02)
        require(endpoint_path.exists(), "sibling-negative native listener did not bind")
        provider_exec = ProbeExec(module, engine, sibling, "inside")
        stdout, stderr = active.communicate(timeout=45)
        report["cases"]["sibling_scope"] = {"native_returncode": active.returncode, "native_stdout": stdout,
            "native_stderr": stderr, "fixture": provider_exec.finish()}
        require(active.returncode != 0, "sibling container origin was accepted")
        active = None
    finally:
        if active is not None:
            active.kill(); active.communicate(timeout=5)
        for fd in host_pty_fds:
            os.close(fd)
        for cid in reversed(containers):
            cleanup = {"cid": cid}
            try:
                engine.request(module.Deadline(10), "DELETE", f"/containers/{cid}?force=1&v=1", statuses=(204, 404))
                status, _ = engine.request(module.Deadline(10), "GET", f"/containers/{cid}/json", statuses=(404,))
                cleanup["engine_confirmed_absent"] = status == 404
            except Exception as cause:
                cleanup["error"] = str(cause)
                cleanup["engine_confirmed_absent"] = False
            report["cleanup"][cid] = cleanup
        for path in listeners:
            if path.exists():
                path.unlink()
        deadline = time.monotonic() + 10
        while any(Path("/sys/fs/cgroup", cg.lstrip("/")).exists() for cg in cgroups) and time.monotonic() < deadline:
            time.sleep(.05)
        report["cleanup"]["cgroups_absent"] = all(not Path("/sys/fs/cgroup", cg.lstrip("/")).exists() for cg in cgroups)
        report["cleanup"]["sockets_absent"] = all(not path.exists() for path in listeners)
        report["scope_note"] = "Actual kernel checks and credential-free fixture origin only; no full gateway/provider admission or send-time cgroup claim. ROOT-assisted outside-send/move-back is not a worker capability."
        replace_private(args.output / "report.json", report)
        engine.close()
        print(json.dumps(report, sort_keys=True))
        require(all(item.get("engine_confirmed_absent") is True for item in report["cleanup"].values() if isinstance(item, dict))
                and report["cleanup"]["cgroups_absent"] and report["cleanup"]["sockets_absent"], "proof cleanup is incomplete")


if __name__ == "__main__":
    main()
