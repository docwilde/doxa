#!/usr/bin/env python3
"""Protocol/negative tests; fake Engine fixtures never establish containment."""
import copy
from contextlib import ExitStack, contextmanager
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import sys
import threading
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("root_broker", Path(__file__).with_name("root_broker.py"))
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


def policy():
    h = "1" * 64
    return {"version": 1, "session_id": "unit-scope", "caller_uid": 1000,
            "engine": {"uid": 1001, "gid": 1001, "socket": "/run/user/1001/docker.sock",
                       "exe_path": "/usr/bin/dockerd", "exe_sha256": h, "config_path": "/etc/doxa/engine.json",
                       "config_sha256": h, "api_version": "1.51"},
            "root": "/srv/doxa/unit-scope", "image": "sha256:" + h,
            "worker": {"path": b.WORKER, "sha256": h},
            "providers": {"claude": {"path": "/usr/local/bin/claude", "sha256": h,
                                      "artifacts": {"/usr/local/bin/claude": h}, "egress": ["api.anthropic.com"],
                                      "proxy_port": 3128, "environment_keys": ["ANTHROPIC_API_KEY", "LANG"]}},
            "authority": {"path": "/usr/libexec/doxa/doxa-hardened-gateway", "sha256": h},
            "broker": {"path": "/usr/libexec/doxa/root_broker.py", "sha256": h},
            "limits": {"memory_bytes": 1073741824, "cpu_quota": 100000, "cpu_period": 100000, "pids": 64,
                       "disk_bytes": 1073741824, "output_bytes": 1048576, "deadline_seconds": 10,
                       "operation_seconds": 10, "stdin_bytes": 1048576}}


def inspect(p, cid="2" * 64):
    spec = b.create_spec(p, "3" * 64)
    return {"Id": cid, "Image": p["image"], "Config": spec,
            "HostConfig": spec["HostConfig"], "State": {"Running": True, "Pid": 42},
            "Mounts": [{"Type": "bind", "Destination": target, "Source": b.policy_paths(p)[source],
                        "RW": writable, "Propagation": "rprivate"}
                       for target, source, writable in [("/workspace", "workspace", True), ("/home/doxa", "home", True),
                                                        ("/work-cache", "cache", True), ("/run/doxa/session", "session_mount", False)]],
            "NetworkSettings": {"Networks": {"none": {}}}}


class PolicyTests(unittest.TestCase):
    def test_distinct_uids_and_fixed_layout(self):
        p = b.validate_policy(policy())
        self.assertEqual(b.policy_paths(p)["workspace"], "/srv/doxa/unit-scope/checkout")
        self.assertEqual(b.policy_paths(p)["state_dir"], "/run/doxa/hardened/unit-scope")
        self.assertEqual(b.policy_paths(p)["session_mount"], "/srv/doxa/unit-scope/broker")
        self.assertEqual(b.resource_limits(p)["nano_cpus"], 1000000000)

    def test_scope_widening_is_refused(self):
        changes = [lambda p: p.update(version=True), lambda p: p.update(caller_uid=0),
                   lambda p: p["engine"].update(uid=1000), lambda p: p.update(root="/"),
                   lambda p: p.update(image="ubuntu:latest"), lambda p: p["worker"].update(path="/bin/sh"),
                   lambda p: p["limits"].update(pids=0), lambda p: p["limits"].update(deadline_seconds=0),
                   lambda p: p["limits"].update(cpu_quota=50000), lambda p: p.update(session_id="../other"),
                   lambda p: p["providers"]["claude"].update(path="/bin/sh"),
                   lambda p: p["providers"]["claude"].update(egress=["*.anthropic.com"]),
                   lambda p: p["providers"]["claude"].update(egress=["127.0.0.1"]),
                   lambda p: p["providers"]["claude"].update(environment_keys=["LD_PRELOAD"]),
                   lambda p: p["providers"]["claude"].update(environment_keys=["OPENAI_API_KEY"]),
                   lambda p: p["providers"]["claude"].update(proxy_port=3130),
                   lambda p: p.update(paths={"workspace": "/home/owner"})]
        for change in changes:
            with self.subTest(change=change):
                p = policy(); change(p)
                with self.assertRaises(b.Refused): b.validate_policy(p)

    def test_codex_requires_entire_protected_package(self):
        p = policy(); h = "1" * 64
        p["providers"] = {"codex": {"path": "/usr/local/bin/codex", "sha256": h,
                            "artifacts": {"/opt/doxa/codex-provider/codex": h}, "egress": ["api.openai.com"],
                            "proxy_port": 3129, "environment_keys": ["OPENAI_API_KEY"]}}
        with self.assertRaises(b.Refused): b.validate_policy(p)
        for name in ["codex-app-server", "codex-code-mode-host", "codex-code-mode-host-payload", "receipt.json"]:
            p["providers"]["codex"]["artifacts"]["/opt/doxa/codex-provider/"+name] = h
        b.validate_policy(p)

    def test_duplicate_and_nonfinite_json_are_refused(self):
        for raw in [b'{"v":1,"v":1}', b'{"n":NaN}', b'{"n":Infinity}']:
            with self.assertRaises(b.Refused): b.strict_json(raw)

    def test_engine_inspect_checks_all_authority_fields(self):
        p = policy(); good = inspect(p)
        b.inspect_exact(p, "3" * 64, "2" * 64, good)
        changes = [lambda i: i.update(Id="4"*64), lambda i: i.update(Image="sha256:"+"4"*64),
                   lambda i: i["Config"].update(Cmd=["hold"]), lambda i: i["Config"].update(Tty=True),
                   lambda i: i["HostConfig"].update(NetworkMode="host"), lambda i: i["HostConfig"].update(CapAdd=["SYS_ADMIN"]),
                   lambda i: i["HostConfig"].update(SecurityOpt=["seccomp=unconfined"]),
                   lambda i: i["HostConfig"].update(ExtraHosts=["owner:host-gateway"]),
                   lambda i: i["Mounts"].append({"Destination": "/host"}),
                   lambda i: i["Mounts"][0].update(Source="/home/owner"),
                   lambda i: i["Mounts"][3].update(RW=True),
                   lambda i: i["NetworkSettings"].update(Networks={"default": {"IPAddress": "1.2.3.4"}}),
                   lambda i: i["State"].update(Running=False)]
        for change in changes:
            i = copy.deepcopy(good); change(i)
            with self.assertRaises(b.Refused): b.inspect_exact(p, "3"*64, "2"*64, i)

    def test_install_is_root_only(self):
        with patch.object(b.os, "geteuid", return_value=1000):
            with self.assertRaisesRegex(b.Refused, "only root"):
                b.install(Path("/no/source"), Path("/no/destination"))


class FramingTests(unittest.TestCase):
    def test_live_root_start_time_must_match_instance_before_and_after_challenge(self):
        pidfd=os.pidfd_open(os.getpid())
        try:
            current=b.process_start_time(os.getpid())
            b.root_start_time_matches(os.getpid(),pidfd,current)
            with self.assertRaisesRegex(b.Refused,"start time differs"):
                b.root_start_time_matches(os.getpid(),pidfd,current+1)
            with patch.object(b,"process_start_time",side_effect=[current,current+1]):
                b.root_start_time_matches(os.getpid(),pidfd,current)
                with self.assertRaisesRegex(b.Refused,"start time differs"):
                    b.root_start_time_matches(os.getpid(),pidfd,current)
        finally:os.close(pidfd)

    def setUp(self):
        self.a, self.c = socket.socketpair()
    def tearDown(self):
        self.a.close(); self.c.close()

    def test_binary_frames_preserve_bytes(self):
        ca = b.Channel(self.a, b.Deadline(1)); cc = b.Channel(self.c, b.Deadline(1))
        raw = bytes(range(256)) + b"\0\n\r"
        ca.frame(b.TAG_STDIN, raw)
        self.assertEqual(cc.receive_frame(), (b.TAG_STDIN, raw))

    def test_frame_length_bound_before_body(self):
        self.a.sendall(bytes([b.TAG_STDIN]) + (b.MAX_FRAME+1).to_bytes(4, "big"))
        cc = b.Channel(self.c, b.Deadline(1))
        with self.assertRaisesRegex(b.Refused, "frame bound"): cc.receive_frame()

    def test_slow_header_does_not_reset_deadline(self):
        cc = b.Channel(self.c, b.Deadline(.12))
        def trickle():
            for byte in b"HTTP/1.1 200 OK\r\n":
                try: self.a.send(bytes([byte]))
                except OSError: return
                time.sleep(.04)
        worker = threading.Thread(target=trickle); worker.start()
        started = time.monotonic()
        with self.assertRaisesRegex(b.Refused, "deadline"): b.response(cc)
        self.a.close(); worker.join(1)
        self.assertLess(time.monotonic()-started, .3)

    def test_chunked_body_is_bounded_and_consumed(self):
        self.a.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n")
        status, _, body = b.response(b.Channel(self.c, b.Deadline(1)), 5)
        self.assertEqual(status, 200); self.assertEqual(body.read(), b"abcde")

    def test_http_ambiguity_and_duplicate_fields_fail(self):
        for header in [b"Content-Length: 0\r\nContent-Length: 0\r\n", b"Content-Length: 0\r\nTransfer-Encoding: chunked\r\n"]:
            self.a.sendall(b"HTTP/1.1 200 OK\r\n"+header+b"\r\n")
            with self.assertRaises(b.Refused): b.response(b.Channel(self.c, b.Deadline(1)))

    def test_slow_body_does_not_reset_deadline(self):
        self.a.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nx")
        _, _, body = b.response(b.Channel(self.c, b.Deadline(.1)))
        with self.assertRaisesRegex(b.Refused, "deadline"): body.read()

    def test_kernel_peer_pidfd_and_wrong_executable(self):
        # Real local Linux kernel primitive; it does not prove a root broker.
        expected = b.sha_file("/proc/self/exe")
        try:
            pid, fd = b.peer_pin(self.c, os.geteuid(), expected)
        except OSError as error:
            if error.errno == 92: self.skipTest("SO_PEERPIDFD unsupported kernel")
            raise
        try: self.assertEqual(pid, os.getpid())
        finally: os.close(fd)
        with self.assertRaisesRegex(b.Refused, "executable"):
            b.peer_pin(self.c, os.geteuid(), "f" * 64)
        with self.assertRaisesRegex(b.Refused, "UID"):
            b.peer_pin(self.c, os.geteuid()+1, expected)


class ExecIdentityTests(unittest.TestCase):
    def test_inspect_create_inspect_upgrade_share_exact_connection(self):
        broker = b.Broker.__new__(b.Broker)
        broker.policy = policy(); broker.policy_sha256 = "3" * 64
        broker.identity = Mock(return_value="2"*64)
        broker.checked_inspect = Mock()
        engine = broker.engine = Mock()
        channel = Mock(); engine.connect.return_value = channel
        eid = "5" * 64; cmd = [b.WORKER, "workspace-hardened", "status"]
        engine.exchange.side_effect = [(201, {"Id": eid}), (200, {"ContainerID": "2"*64, "Running": False,
            "ProcessConfig": {"entrypoint": cmd[0], "arguments": cmd[1:], "tty": False, "privileged": False, "user": "0:0"}})]
        with patch.object(b, "response", return_value=(101, {"upgrade": "tcp", "content-type": "application/vnd.docker.multiplexed-stream"}, None)):
            cid, result_eid, used = broker.exec_start(b.Deadline(1), cmd)
        self.assertIs(used, channel); self.assertEqual(result_eid, eid)
        self.assertEqual(broker.checked_inspect.call_count, 2)
        for call in broker.checked_inspect.call_args_list:
            self.assertIs(call.args[0], channel)
        for call in engine.exchange.call_args_list:
            self.assertIs(call.args[0], channel)
        self.assertIs(engine.send_request.call_args.args[0], channel)

    def test_unwrapped_exec_is_refused(self):
        broker = b.Broker.__new__(b.Broker)
        for cmd in [["/bin/sh", "-c", "true"], [b.WORKER, "exec", "/usr/local/bin/claude"], [b.WORKER, "hold"]]:
            with self.assertRaisesRegex(b.Refused, "mandatory"):
                broker.exec_start(b.Deadline(1), cmd)

    def test_cancel_disconnect_and_output_bounds(self):
        for scenario in ("cancel", "disconnect", "oversize", "deadline"):
            with self.subTest(scenario=scenario):
                worker, engine_fd = socket.socketpair(); client_fd, user = socket.socketpair()
                for s in (worker, engine_fd, client_fd, user): s.setblocking(False)
                upstream = b.Channel(engine_fd, b.Deadline(.1))
                downstream = b.Channel(client_fd, upstream.deadline)
                if scenario == "cancel": user.send(bytes([b.TAG_CANCEL])+b"\0\0\0\0")
                elif scenario == "disconnect": user.close()
                elif scenario == "oversize": worker.send(b"\1\0\0\0"+(b.MAX_FRAME+1).to_bytes(4,"big"))
                try:
                    with self.assertRaises(b.Refused):
                        b.stream_stdio(Mock(), upstream, downstream, {"stdin_bytes":100, "output_bytes":100})
                finally:
                    for s in (worker, engine_fd, client_fd, user): s.close()

    def test_stdin_eof_remains_closed(self):
        worker, engine_fd = socket.socketpair(); client_fd, user = socket.socketpair()
        upstream = b.Channel(engine_fd, b.Deadline(.2)); downstream = b.Channel(client_fd, upstream.deadline)
        user.sendall(bytes([b.TAG_EOF])+b"\0\0\0\0"+bytes([b.TAG_STDIN])+b"\0\0\0\1x")
        try:
            with self.assertRaisesRegex(b.Refused, "stdin after EOF"):
                b.stream_stdio(Mock(), upstream, downstream, {"stdin_bytes":100, "output_bytes":100})
        finally:
            for s in (worker, engine_fd, client_fd, user): s.close()


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR", str(Path.home() / "ssd-cache/tmp")))
        self.root = Path(self.temp.name)
        (self.root / "checkout").mkdir(); (self.root / "home").mkdir()
        self.paths = {"workspace": str(self.root / "checkout"), "home": str(self.root / "home")}
        self.a, self.c = socket.socketpair()
        self.sender = b.Channel(self.a, b.Deadline(1)); self.receiver = b.Channel(self.c, b.Deadline(1))
        self.p = policy()
        self.inventory = b.hashlib.sha256(b"doxa-staging-v1\0")
        self.count = 0
    def tearDown(self):
        self.a.close(); self.c.close(); self.temp.cleanup()
    def entry(self, relative, data=None, scope="checkout", digest=None):
        directory = data is None
        row = {"scope": scope, "relative_path": relative, "kind": "directory" if directory else "file",
               "mode": 0o700 if directory else 0o600, "size": 0 if directory else len(data),
               "sha256": None if directory else digest or b.sha(data)}
        raw = b.compact(row); self.inventory.update(len(raw).to_bytes(8,"big")); self.inventory.update(raw); self.count += 1
        self.sender.frame(b.TAG_ENTRY, raw)
        if data: self.sender.frame(b.TAG_FILE, data)
    def finish(self, digest=None):
        self.sender.json_frame(b.TAG_STAGED, {"entry_count": self.count, "tree_sha256": digest or self.inventory.hexdigest()})
    def receive(self, initial=True, family="none"):
        with patch.object(b, "policy_paths", return_value=self.paths), patch.object(b.os, "fchown"):
            return b.receive_staging(self.p, self.receiver, family, initial)
    def test_fixed_checkout_and_private_provider_bytes(self):
        self.entry("src"); self.entry("src/main.py", b"print(1)\n")
        self.entry("claude", scope="home"); self.entry("claude/.credentials.json", b'{"key":"synthetic"}', "home")
        self.finish(); result = self.receive()
        self.assertEqual(result["entry_count"], 4)
        self.assertEqual((self.root/"checkout/src/main.py").read_bytes(), b"print(1)\n")
        self.assertEqual((self.root/"home/claude/.credentials.json").stat().st_mode & 0o777, 0o600)
    def test_path_escape_and_other_home_scopes_fail(self):
        for relative in ["../outside", "/outside", "x/../../outside", "x//file", "x/./file"]:
            with self.assertRaises(b.Refused): b.relative_parts(relative)
        self.entry("ssh", scope="home"); self.finish()
        with self.assertRaisesRegex(b.Refused, "prefix"): self.receive()
    def test_symlink_parent_never_followed(self):
        outside = self.root/"outside"; outside.mkdir()
        (self.root/"home/claude").symlink_to(outside, target_is_directory=True)
        self.entry("claude/.credentials.json", b'{"token":"synthetic"}', "home"); self.finish()
        with self.assertRaises(OSError): self.receive(False, "claude")
        self.assertFalse((outside/".credentials.json").exists())
    def test_existing_auth_is_never_overwritten(self):
        (self.root/"home/claude").mkdir(); existing = self.root/"home/claude/.credentials.json"; existing.write_bytes(b'{"token":"newer"}')
        self.entry("claude/.credentials.json", b'{"token":"synthetic"}', "home"); self.finish()
        self.receive(False, "claude")
        self.assertEqual(existing.read_bytes(), b'{"token":"newer"}')
    def test_bad_byte_hash_rolls_back_new_files(self):
        self.entry("src"); self.entry("src/data", b"changed", digest="f"*64); self.finish()
        with self.assertRaisesRegex(b.Refused, "digest"): self.receive()
        self.assertEqual(list((self.root/"checkout").iterdir()), [])
    def test_inventory_hash_mismatch_rolls_back(self):
        self.entry("one", b"one"); self.finish("f"*64)
        with self.assertRaisesRegex(b.Refused, "inventory"): self.receive()
        self.assertFalse((self.root/"checkout/one").exists())
    def test_home_update_cannot_write_checkout_or_cross_family(self):
        self.entry("new", b"x"); self.finish()
        with self.assertRaisesRegex(b.Refused, "scope"): self.receive(False, "claude")
    def test_home_source_must_be_private(self):
        source = self.root/"credentials"; source.write_bytes(b"synthetic"); source.chmod(0o644)
        with self.assertRaisesRegex(b.Refused, "owner-private"):
            b.send_staging(self.sender, home_files=[("claude/auth.json", source)], family="claude")
    def test_client_refuses_source_symlink(self):
        source = self.root/"source"; source.mkdir(); (source/"link").symlink_to("/etc/passwd")
        with self.assertRaisesRegex(b.Refused, "links"):
            b.send_staging(self.sender, checkout=source)
    def test_nonroot_forged_receipt_rejected(self):
        fake = self.root/"receipt.json"; fake.write_text('{"broker_uid":0,"ready":true}')
        if os.geteuid() == 0:
            os.chown(fake, 1000, 1000)
        with self.assertRaisesRegex(b.Refused, "root control"):
            b.read_regular(fake, root=True, readonly=True)


class AuthorityCleanupTests(unittest.TestCase):
    def test_completed_nonzero_authority_never_signals_reaped_pid(self):
        broker = b.Broker.__new__(b.Broker)
        broker.policy = policy(); broker.policy["authority"]["path"] = "/usr/bin/false"
        broker.policy_path = "/not-opened-fixture"
        broker.policy_sha256 = "3" * 64
        broker.manifest_path = Path("/not-opened-manifest")
        broker.state_dir = Path(os.environ.get("TMPDIR", str(Path.home()/"ssd-cache/tmp")))
        broker.preflight = Mock(); broker.identity = Mock(return_value="2"*64); broker.gateway_live = Mock()
        broker.engine = Mock(pidfd=None)
        readfd, writefd = os.pipe(); broker.engine.pidfd = readfd
        try:
            with patch.object(b.os, "killpg") as kill:
                with self.assertRaisesRegex(b.Refused, "authority refused"):
                    broker.native_check(b.Deadline(1), "prepare")
                kill.assert_not_called()
            with patch.object(b.os, "killpg") as kill:
                with self.assertRaisesRegex(b.Refused, "staging gate refused"):
                    broker.stage_check(b.Deadline(1), "before", "none")
                kill.assert_not_called()
        finally:
            os.close(readfd); os.close(writefd)

    def test_repeated_settings_upload_replaces_atomically_and_auth_is_preserved(self):
        test = StagingTests(); test.setUp()
        try:
            (test.root/"home/claude").mkdir()
            settings = test.root/"home/claude/settings.json"; settings.write_bytes(b"{}")
            test.entry("claude/settings.json", b'{"permissions":{}}', "home"); test.finish()
            test.receive(False, "claude")
            self.assertEqual(settings.read_bytes(), b'{"permissions":{}}')
            self.assertFalse(any(p.name.startswith(".doxa-stage-") for p in settings.parent.iterdir()))
        finally:
            test.tearDown()

    def test_runtime_history_and_unselected_config_are_not_staging_inputs(self):
        for name in ["codex/config.toml", "codex/sessions/history.jsonl", "claude/projects/log.jsonl", "claude/skills/.git/config", "claude/adopted-plugins/p/hooks/do.sh", "claude/skills/p/.mcp.json"]:
            with self.assertRaises(b.Refused): b.home_entry_policy(name.split("/"), "file", 1)


class NativeDiagnosticTests(unittest.TestCase):
    def fixture(self):
        broker=b.Broker.__new__(b.Broker);broker.policy=policy();broker.policy_sha256="3"*64
        broker.state_dir=Path(os.environ.get("TMPDIR",str(Path.home()/"ssd-cache/tmp"))).resolve()
        broker.native_diagnostic=Mock();return broker

    def test_stderr_flood_does_not_deadlock_and_retains_bounded_reason_exit(self):
        broker=self.fixture();command=[sys.executable,"-I","-c","import os;os.write(2,b'x'*(4*1024*1024));os.write(1,b'{}');raise SystemExit(7)"]
        with self.assertRaisesRegex(b.Refused,"exit=7"):
            broker.run_native(command,b.Deadline(2),"stage-check","before","none","native refused")
        args=broker.native_diagnostic.call_args.args
        self.assertEqual(args[4],7);self.assertEqual(len(args[5]),b.MAX_NATIVE_STDERR);self.assertEqual(args[6],4*1024*1024)

    def test_stderr_flood_obeys_original_deadline_and_kills_owned_child(self):
        broker=self.fixture();command=[sys.executable,"-I","-c","import os\nwhile True:os.write(2,b'x'*65536)"]
        start=time.monotonic()
        with self.assertRaises(b.Refused):broker.run_native(command,b.Deadline(.05),"broker-check","prepare","none","native refused")
        self.assertLess(time.monotonic()-start,.5);args=broker.native_diagnostic.call_args.args
        self.assertEqual(args[4],-9);self.assertLessEqual(len(args[5]),b.MAX_NATIVE_STDERR)
        with self.assertRaises(ProcessLookupError):os.kill(args[3],0)

    def test_native_service_stderr_drains_flood_and_closes_boundedly(self):
        with subprocess.Popen([sys.executable,"-I","-c","import os;os.write(2,b'y'*(2*1024*1024));os.write(1,b'done')"],stdout=subprocess.PIPE,stderr=subprocess.PIPE) as child:
            drain=b.NativeStderr(child.stderr)
            self.assertEqual(child.stdout.read(4),b"done");self.assertEqual(child.wait(timeout=2),0)
            data,total=drain.finish();self.assertEqual(len(data),b.MAX_NATIVE_STDERR);self.assertEqual(total,2*1024*1024)
            self.assertFalse(drain.thread.is_alive());self.assertTrue(child.stderr.closed)


class GatewaySocketCleanupTests(unittest.TestCase):
    @contextmanager
    def fixture(self,alive=False):
        short=Path.home()/".dt";short.mkdir(mode=0o700,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="gs-",dir=short) as directory:
            root=Path(directory);runtime=root/"r";runtime.mkdir();mount=root/"b";mount.mkdir()
            broker=b.Broker.__new__(b.Broker);broker.policy=policy();broker.policy["engine"]["gid"]=os.getgid()
            broker.policy["providers"]["codex"]={};broker.state_dir=runtime;broker.gateway_sockets={};broker.gateway_parents={}
            shapes=broker.gateway_socket_paths
            sockets=[];child=subprocess.Popen([sys.executable,"-I","-c","import time;time.sleep(5)" if alive else "pass"])
            broker.gateway_pidfd=os.pidfd_open(child.pid)
            if not alive:child.wait(timeout=2)
            original_stat=os.stat;original_lstat=os.lstat
            def root_socket(meta):
                if stat.S_ISSOCK(meta.st_mode):
                    fields=list(meta);fields[4]=0
                    if stat.S_IMODE(meta.st_mode)==0o600:fields[5]=0
                    return os.stat_result(fields)
                return meta
            with patch.object(b,"policy_paths",return_value={"session_mount":str(mount)}),patch.object(b,"controlled",side_effect=lambda p,**kw:original_stat(p)),\
                 patch.object(b.os,"stat",side_effect=lambda *a,**kw:root_socket(original_stat(*a,**kw))),\
                 patch.object(b.os,"lstat",side_effect=lambda *a,**kw:root_socket(original_lstat(*a,**kw))):
                for path,gid,mode in broker.gateway_socket_paths():
                    sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);sock.bind(str(path));path.chmod(mode);sockets.append(sock)
                    parent=original_stat(path.parent);broker.gateway_parents[path.parent]=(parent.st_dev,parent.st_ino,parent.st_uid,parent.st_gid,parent.st_mode)
                broker.capture_gateway_sockets()
                try:yield broker,mount,sockets
                finally:
                    for sock in sockets:sock.close()
                    if child.poll() is None:child.kill();child.wait(timeout=2)
                    os.close(broker.gateway_pidfd)

    def test_exact_pinned_sockets_removed_only_after_pidfd_exit(self):
        with self.fixture() as (broker,mount,sockets):
            self.assertEqual(len(broker.gateway_sockets),4);broker.remove_gateway_sockets()
            self.assertFalse(any(path.exists() for path,_,_ in broker.gateway_socket_paths()))
        with self.fixture(alive=True) as (broker,mount,sockets):
            with self.assertRaisesRegex(b.Refused,"PIDFD exit"):broker.remove_gateway_sockets()
            self.assertTrue(all(path.exists() for path,_,_ in broker.gateway_socket_paths()))

    def test_replacement_and_unknown_socket_fail_without_unlinking(self):
        for replacement in (False,True):
            with self.fixture() as (broker,mount,sockets):
                path=mount/("hook.sock" if replacement else "unknown.sock")
                if replacement:path.unlink()
                extra=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);extra.bind(str(path));path.chmod(0o660);sockets.append(extra)
                with self.assertRaises(b.Refused):broker.remove_gateway_sockets()
                self.assertTrue(all(path.exists() for path,_,_ in broker.gateway_socket_paths()));self.assertTrue(path.exists())

    def test_changed_parent_and_unlink_error_refuse_cleanup(self):
        with self.fixture() as (broker,mount,sockets):
            mount.chmod(0o700)
            with self.assertRaisesRegex(b.Refused,"parent changed"):broker.remove_gateway_sockets()
            self.assertTrue(all(path.exists() for path,_,_ in broker.gateway_socket_paths()))

    def test_socket_mode_and_link_mutations_refuse_cleanup(self):
        for hardlink in (False,True):
            with self.fixture() as (broker,mount,sockets):
                path=mount/"hook.sock"
                if hardlink:os.link(path,mount/"extra-link")
                else:path.chmod(0o600)
                with self.assertRaises(b.Refused):broker.remove_gateway_sockets()
                self.assertTrue(all(path.exists() for path,_,_ in broker.gateway_socket_paths()))
        with self.fixture() as (broker,mount,sockets),patch.object(b.os,"unlink",side_effect=PermissionError("injected cleanup refusal")):
            with self.assertRaises(PermissionError):broker.remove_gateway_sockets()
            self.assertTrue(all(path.exists() for path,_,_ in broker.gateway_socket_paths()))


class HardenedFollowupTests(unittest.TestCase):
    def test_inherited_image_labels_are_pinned_metadata(self):
        p = policy(); info = inspect(p)
        info["Config"]["Labels"]["org.fixture.image"] = "fixed"
        with self.assertRaises(b.Refused): b.inspect_exact(p,"3"*64,"2"*64,info)
        b.inspect_exact(p,"3"*64,"2"*64,info,{"org.fixture.image":"fixed"})
        info["Config"]["Labels"]["io.doxa.root-policy"] = "unreviewed"
        with self.assertRaises(b.Refused): b.inspect_exact(p,"3"*64,"2"*64,info,{"org.fixture.image":"fixed"})

    def test_git_builtin_grammar_blocks_alias_and_external_execution(self):
        denied = [["-c", "alias.escape=!claude --version", "escape"], ["escape"], ["config", "alias.x", "!sh"],
                  ["diff", "--ext-diff"], ["diff", "--textconv"], ["apply", "--unsafe-paths"],
                  ["status", "--exec-path=/home/doxa"], ["rev-parse", "--git-dir=/root"],
                  ["apply", "/home/doxa/claude/.credentials.json"], ["diff", "--output=/workspace/file"],
                  ["status", "-c", "core.fsmonitor=claude"], ["status", "--", "../home"],
                  ["apply","--cached","-"], ["apply","--index","-"]]
        for argv in denied:
            with self.subTest(argv=argv):
                with self.assertRaises(b.Refused): b.git_arguments(argv)
        for argv in [["status","--porcelain"], ["--no-pager","diff","--find-renames","--unified=3","HEAD","--"], ["diff","--cached","--binary","HEAD"], ["merge-base","HEAD","main"],
                     ["ls-files","--others","--exclude-standard"], ["apply","--reverse","--recount","--check","-"],
                     ["rev-parse","--verify","HEAD"]]:
            result = b.git_arguments(argv)
            self.assertIn("core.hooksPath=/dev/null",result); self.assertIn("core.fsmonitor=false",result)
            self.assertIn("credential.helper=",result); self.assertIn("diff.external=",result)
            self.assertIn("safe.directory=/workspace",result)
            if "status" in argv or "diff" in argv:self.assertIn("--ignore-submodules=all",result)
            if argv[0] == "diff": self.assertIn("--no-ext-diff",result); self.assertIn("--no-textconv",result)

    def test_exited_container_can_be_verified_for_cleanup(self):
        p=policy(); info=inspect(p); info["State"]={"Running":False,"Pid":0}
        with self.assertRaises(b.Refused): b.inspect_exact(p,"3"*64,"2"*64,info)
        b.inspect_exact(p,"3"*64,"2"*64,info,live=False)

    def test_stop_handles_already_absent_cid(self):
        broker=b.Broker.__new__(b.Broker); broker.manifest={"cid":"2"*64,"status":"ready"}
        broker.engine=Mock(); channel=Mock(); broker.engine.connect.return_value=channel
        broker.engine.exchange.return_value=(404,{"message":"absent"})
        broker.persist=Mock(); broker.close_gateway=Mock(); broker.close_adapters=Mock(); broker.describe=Mock(return_value={"status":"stopped"})
        self.assertEqual(broker.stop(b.Deadline(1)),{"status":"stopped"})
        broker.persist.assert_called_once_with("2"*64,"stopped")

    def test_resume_failure_runs_independent_cleanup(self):
        broker=b.Broker.__new__(b.Broker); broker.policy=policy()
        broker.manifest={"cid":"2"*64,"status":"ready","engine_pid":33,"engine_socket_dev":1,"engine_socket_ino":2}
        broker.preflight=Mock(); broker.engine=Mock(pid=33,socket_identity=(1,2)); channel=Mock(); broker.engine.connect.return_value=channel
        broker.engine.exchange.return_value=(200,{"Id":"2"*64})
        broker.checked_inspect=Mock(side_effect=b.Refused("injected native scope refusal")); broker.cleanup=Mock()
        with self.assertRaises(b.Refused): broker.resume(b.Deadline(1))
        broker.cleanup.assert_called_once_with("2"*64)

    def test_repeated_prepared_snapshot_bytes_are_idempotent(self):
        test=StagingTests(); test.setUp()
        try:
            directory=test.root/"home/claude/skills/s"; directory.mkdir(parents=True)
            saved=directory/"SKILL.md"; saved.write_bytes(b"prepared"); saved.chmod(0o600)
            test.entry("claude/skills/s/SKILL.md",b"prepared","home"); test.finish()
            test.receive(False,"claude"); self.assertEqual(saved.read_bytes(),b"prepared")
        finally: test.tearDown()

    def test_client_snapshot_writes_only_private_destination_and_checks_bytes(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR",str(Path.home()/"ssd-cache/tmp"))) as directory:
            destination=Path(directory).resolve()/"frozen.jsonl"
            a,c=socket.socketpair(); send=b.Channel(a,b.Deadline(1)); receive=b.Channel(c,b.Deadline(1))
            data=b'{"type":"session_meta","payload":{"id":"unit","cwd":"/workspace"}}\n'
            try:
                send.frame(b.TAG_SNAPSHOT,data); send.json_frame(b.TAG_SNAPSHOT_END,{"bytes":len(data),"chunk_sha256":b.sha(data)})
                b.receive_client_snapshot(receive,{"status":"snapshot","bytes":len(data),"chunk_sha256":b.sha(data)},destination)
                self.assertEqual(destination.read_bytes(),data); self.assertEqual(destination.stat().st_mode&0o777,0o600)
            finally: a.close(); c.close()

    def test_snapshot_source_identity_and_postreview_changes_are_checked(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR",str(Path.home()/"ssd-cache/tmp"))) as directory:
            home=Path(directory); source=home/"codex/sessions/day/log.jsonl"; source.parent.mkdir(parents=True)
            source.write_bytes(b'{"type":"session_meta","payload":{"id":"unit-thread","cwd":"/workspace"}}\n')
            broker=b.Broker.__new__(b.Broker); broker.policy=policy(); broker.policy["engine"]["uid"]=os.geteuid()
            broker.policy["providers"]["codex"]={}; broker.policy_sha256="3"*64; broker.state_lock=threading.RLock()
            broker.transport_live=Mock(); broker.stage_check=Mock(); broker.identity=Mock(return_value="2"*64)
            client=Mock(deadline=b.Deadline(1)); request={"v":1,"session_id":"unit-scope","op":"provider_snapshot","family":"codex","relative":"codex/sessions/day/log.jsonl","thread":"unit-thread"}
            quota=b.compact({"session_id":"unit-scope","root":broker.policy["root"],"boot_id":b.boot_id(),"project_id":5004})
            def project_ioctl(fd,command,buffer,mutate): struct.pack_into("I",buffer,12,5004)
            with patch.object(b,"policy_paths",return_value={"home":str(home)}),patch.object(b,"read_regular",return_value=quota),patch.object(b.fcntl,"ioctl",side_effect=project_ioctl):
                broker.provider_snapshot(request,client)
                metadata=client.json_frame.call_args_list[0].args[1]
                self.assertEqual(metadata["source"]["inode"],source.stat().st_ino)
                self.assertEqual(metadata["family"],"codex"); self.assertEqual(metadata["source"]["thread"],"unit-thread")
                verify=dict(request,op="verify_provider_snapshot",expected=metadata)
                broker.provider_snapshot(verify,Mock(deadline=b.Deadline(1)))
                source.write_bytes(source.read_bytes()+b'{"changed":true}\n')
                with self.assertRaisesRegex(b.Refused,"changed after snapshot"):
                    broker.provider_snapshot(verify,Mock(deadline=b.Deadline(1)))


class LifecycleReconciliationTests(unittest.TestCase):
    def test_guard_revalidates_active_provider_without_idle_requirement(self):
        broker=b.Broker.__new__(b.Broker);broker.policy=policy();broker.policy_sha256="3"*64
        broker.closing=threading.Event();broker.active_provider=True;broker.manifest={"status":"ready"}
        broker.identity=Mock(return_value="2"*64);broker.native_check=Mock();broker.cleanup=Mock()
        result=broker.guard(b.Deadline(1));self.assertIs(result["guard_ready"],True)
        broker.native_check.assert_called_once();self.assertEqual(broker.native_check.call_args.args[1:],("provider_exec","none"))
        broker.cleanup.assert_not_called()
        broker.native_check.side_effect=b.Refused("live quota drift")
        with self.assertRaisesRegex(b.Refused,"quota drift"):broker.guard(b.Deadline(1))
        broker.cleanup.assert_called_once_with("2"*64)

    def resume_fixture(self):
        broker=b.Broker.__new__(b.Broker); broker.policy=policy()
        broker.manifest={"cid":"2"*64,"status":"ready","engine_pid":33,"engine_socket_dev":1,"engine_socket_ino":2}
        broker.preflight=Mock(); broker.engine=Mock(pid=33,socket_identity=(1,2)); channel=Mock()
        broker.engine.connect.return_value=channel; broker.engine.exchange.return_value=(200,{"Id":"2"*64})
        broker.checked_inspect=Mock(return_value={"State":{"Running":False,"Pid":0}})
        broker.stage_check=Mock(); broker.cleanup=Mock(return_value=True)
        return broker

    def test_resume_preflight_exception_still_removes_owned_old_scope(self):
        broker=self.resume_fixture(); broker.preflight.side_effect=b.Refused("helper failed before inspect")
        with self.assertRaisesRegex(b.Refused,"helper failed"):broker.resume(b.Deadline(1))
        broker.cleanup.assert_called_once_with("2"*64)

    def test_resume_handover_preserves_new_cid_uncertain_cleanup(self):
        for absent in (False,True):
            broker=self.resume_fixture(); broker.persist=Mock()
            if absent:broker.engine.exchange.return_value=(404,{})
            def failed_prepare(deadline):
                broker.manifest={"cid":"4"*64,"status":"cleanup-required"}
                raise b.Refused("new CID cleanup uncertain")
            broker.prepare=Mock(side_effect=failed_prepare)
            with self.assertRaisesRegex(b.Refused,"new CID cleanup uncertain"):broker.resume(b.Deadline(1))
            self.assertEqual(broker.manifest,{"cid":"4"*64,"status":"cleanup-required"})
            self.assertEqual(broker.cleanup.call_args_list,[unittest.mock.call("2"*64)])

    def test_absent_resume_refuses_new_scope_until_old_helpers_retire(self):
        broker=self.resume_fixture();broker.engine.exchange.return_value=(404,{})
        broker.cleanup.return_value=False;broker.prepare=Mock()
        with self.assertRaisesRegex(b.Refused,"helper cleanup is uncertain"):broker.resume(b.Deadline(1))
        broker.prepare.assert_not_called();self.assertTrue(broker.cleanup.called)

    def test_helper_teardown_error_never_skips_engine_removal(self):
        broker=b.Broker.__new__(b.Broker); broker.manifest={"cid":"2"*64,"status":"ready"}
        broker.manifest_path=Path("/unused-root-manifest"); broker.persist=Mock(); broker.engine=Mock()
        broker.engine.request.side_effect=[(204,{}),(404,{})]
        broker.close_gateway=Mock(side_effect=RuntimeError("helper failure")); broker.close_adapters=Mock()
        with patch.object(b,"atomic_root_write") as write:
            self.assertFalse(broker.cleanup("2"*64))
        self.assertEqual([c.args[1:3] for c in broker.engine.request.call_args_list],
                         [("DELETE","/containers/"+"2"*64+"?force=1&v=1"),("GET","/containers/"+"2"*64+"/json")])
        broker.close_adapters.assert_called_once(); broker.persist.assert_not_called()
        self.assertEqual(broker.manifest["status"],"cleanup-required"); write.assert_called_once()

    def test_lost_create_reply_reconciles_only_deterministic_root_name(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR",str(Path.home()/"ssd-cache/tmp"))) as directory:
            broker=b.Broker.__new__(b.Broker); broker.policy=policy(); broker.policy_sha256="3"*64
            broker.state_dir=Path(directory); broker.manifest=None; broker.preflight=Mock(); broker.stage_check=Mock()
            broker.engine=Mock(); broker.engine.request.side_effect=[(200,{"SecurityOptions":["name=rootless"]}),b.Refused("lost create reply")]
            broker.reconcile_create=Mock(return_value=True); broker.cleanup=Mock()
            with self.assertRaisesRegex(b.Refused,"lost create"):
                broker.prepare(b.Deadline(1))
            path=broker.engine.request.call_args_list[-1].args[2]
            self.assertEqual(path,"/containers/create?name=doxa-hardened-unit-scope")
            broker.reconcile_create.assert_called_once(); broker.cleanup.assert_not_called()

    def test_busy_second_provider_does_not_cleanup_active_scope(self):
        broker=b.Broker.__new__(b.Broker); broker.policy=policy(); broker.state_lock=threading.RLock()
        broker.closing=threading.Event(); broker.active_provider=True; broker.active_workspace=0
        broker.cleanup=Mock(); broker.preflight=Mock(); broker.engine=Mock()
        request={"v":1,"session_id":"unit-scope","op":"provider_exec","family":"claude","argv":[]}
        with self.assertRaisesRegex(b.Refused,"one provider"):
            broker.execute(request,Mock())
        broker.cleanup.assert_not_called(); broker.engine.connect.assert_not_called()

    def test_unreviewed_git_input_does_not_touch_engine(self):
        broker=b.Broker.__new__(b.Broker); broker.policy=policy(); broker.cleanup=Mock(); broker.engine=Mock()
        request={"v":1,"session_id":"unit-scope","op":"workspace_exec","argv":["-c","alias.escape=!claude --version","escape"]}
        with self.assertRaises(b.Refused): broker.execute(request,Mock())
        broker.cleanup.assert_not_called(); broker.engine.connect.assert_not_called()


class GitMetadataTests(unittest.TestCase):
    @contextmanager
    def fixture_root(self,p):
        root=Path(p["root"]);runtime=root/"runtime";runtime.mkdir(exist_ok=True)
        paths=b.policy_paths(p);paths["state_dir"]=str(runtime)
        def stage(*_):
            target=root/".git-staging";target.mkdir(mode=0o700,exist_ok=True)
            return target,(0,False)
        original_remove=b.shutil.rmtree
        original_rename=os.rename
        def rename(source,target):
            path=Path(source);mode=path.stat().st_mode&0o777
            # This UID1000 fixture emulates ROOT DAC override for a cross-parent
            # directory rename. Actual ROOT does not alter the published mode.
            if path.is_dir():path.chmod(mode|0o200)
            try:return original_rename(source,target)
            finally:
                current=Path(target) if Path(target).exists() else path
                if current.exists() and current.is_dir():current.chmod(mode)
        def remove(target,*args,**kwargs):
            path=Path(target)
            if path.exists():
                for child in path.rglob("*"):
                    if child.is_dir():child.chmod(0o700)
                path.chmod(0o700)
            return original_remove(target,*args,**kwargs)
        with ExitStack() as stack:
            for name,args in [("geteuid",{"return_value":0}),("chown",{}),("fchown",{})]:
                stack.enter_context(patch.object(b.os,name,**args))
            stack.enter_context(patch.object(b,"controlled"));stack.enter_context(patch.object(b,"check_git_lock"))
            stack.enter_context(patch.object(b,"policy_paths",return_value=paths))
            stack.enter_context(patch.object(b,"private_git_stage",side_effect=stage))
            stack.enter_context(patch.object(b,"git_project",return_value=(0,False)))
            stack.enter_context(patch.object(b.shutil,"rmtree",side_effect=remove))
            stack.enter_context(patch.object(b.os,"rename",side_effect=rename))
            yield

    def test_generated_view_omits_executable_configuration_and_alternates(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR",str(Path.home()/"ssd-cache/tmp"))) as directory:
            p=policy(); p["root"]=str(Path(directory).resolve()); p["engine"]["gid"]=os.getgid()
            root=Path(p["root"]);(root/"broker").mkdir();source=root/"checkout/.git";source.mkdir(parents=True)
            (source/"HEAD").write_text("ref: refs/heads/main\n");(source/"refs/heads").mkdir(parents=True)
            (source/"refs/heads/main").write_text("1"*40+"\n")
            (source/"objects/info").mkdir(parents=True);(source/"objects/info/alternates").write_text("/secret\n")
            (source/"config").write_text("[filter escape]\nclean = touch /marker\n[include]\npath = /secret\n")
            (source/"hooks").mkdir();(source/"hooks/pre-commit").write_text("touch /marker")
            (source/"info").mkdir();(source/"info/attributes").write_text("* filter=escape")
            with self.fixture_root(p):
                view,worker=b.build_git_view(p,b.Deadline(1))
            try:
                self.assertNotIn(b"filter",(view/"config").read_bytes());self.assertNotIn(b"include",(view/"config").read_bytes())
                self.assertFalse((view/"hooks").exists());self.assertFalse((view/"objects/info/alternates").exists())
                self.assertEqual((view/"info/attributes").read_bytes(),b"")
                self.assertEqual((view/"HEAD").stat().st_mode&0o777,0o440);self.assertEqual(view.stat().st_mode&0o777,0o550)
                self.assertTrue(worker.startswith("/run/doxa/session/git-safe/"));self.assertEqual(b.git_environment(worker)["GIT_CONFIG_GLOBAL"],"/dev/null")
            finally:
                for path in view.rglob("*"):
                    if path.is_dir():path.chmod(0o700)
                view.chmod(0o700)

    def test_git_objects_links_and_byte_overrun_fail_closed(self):
        for symlink in (False,True):
            with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR",str(Path.home()/"ssd-cache/tmp"))) as directory:
                p=policy();p["root"]=str(Path(directory).resolve());p["limits"]["disk_bytes"]=1024
                root=Path(p["root"]);(root/"broker").mkdir();source=root/"checkout/.git";source.mkdir(parents=True)
                (source/"HEAD").write_text("1"*40+"\n");(source/"objects").mkdir()
                if symlink:(source/"objects/escape").symlink_to("/etc/passwd")
                else:(source/"objects/large").write_bytes(b"x"*2048)
                with self.fixture_root(p):
                    with self.assertRaises(b.Refused):b.build_git_view(p,b.Deadline(1))

    def test_slow_private_build_and_retirement_never_publish_partial_audit_tree(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR",str(Path.home()/"ssd-cache/tmp"))) as directory:
            p=policy();p["root"]=str(Path(directory).resolve());p["engine"]["gid"]=os.getgid()
            root=Path(p["root"]);(root/"broker").mkdir();source=root/"checkout/.git";source.mkdir(parents=True)
            (source/"HEAD").write_text("1"*40+"\n");(source/"objects").mkdir();(source/"objects/blob").write_bytes(b"x"*65536)
            private_write=threading.Event();continue_build=threading.Event();completed=threading.Event()
            observations=[];errors=[];result=[];original_write=os.write
            def slow(fd,data):
                if b"[core]" in bytes(data):
                    private_write.set();self.assertTrue(continue_build.wait(2))
                return original_write(fd,data)
            def producer():
                try:result.append(b.build_git_view(p,b.Deadline(3)))
                except BaseException as error:errors.append(error)
                finally:completed.set()
            def audit():
                with b.git_metadata_lock(p,b.Deadline(1),exclusive=False):
                    safe=root/"broker/git-safe"
                    rows=list(safe.iterdir()) if safe.exists() else []
                    for view in rows:
                        self.assertTrue((view/"config").is_file());self.assertEqual((view/"info/attributes").read_bytes(),b"")
                        self.assertEqual(view.stat().st_mode&0o777,0o550)
                        for path in view.rglob("*"):self.assertEqual(path.stat().st_mode&0o777,0o550 if path.is_dir() else 0o440)
                    observations.append(len(rows))
            with self.fixture_root(p),patch.object(b.os,"write",side_effect=slow):
                worker=threading.Thread(target=producer);worker.start()
                try:
                    self.assertTrue(private_write.wait(2))
                    for _ in range(10):audit()
                    self.assertEqual(observations,[0]*10)
                    continue_build.set();self.assertTrue(completed.wait(2));worker.join(1);self.assertFalse(errors)
                    audit();self.assertEqual(observations[-1],1)
                    view=result[0][0];retired=threading.Event()
                    def retire():
                        try:b.retire_git_view(p,view,b.Deadline(1));retired.set()
                        except BaseException as error:errors.append(error)
                    with b.git_metadata_lock(p,b.Deadline(1),exclusive=False):
                        reader=os.open(view,os.O_RDONLY|os.O_DIRECTORY);held=os.fstat(reader)
                        remover=threading.Thread(target=retire);remover.start()
                        self.assertFalse(retired.wait(.05));self.assertEqual(view.stat().st_ino,held.st_ino)
                        self.assertTrue((view/"config").exists());os.close(reader)
                    self.assertTrue(retired.wait(1));remover.join(1);audit();self.assertEqual(observations[-1],0)
                    self.assertFalse(errors);self.assertFalse(view.exists())
                finally:continue_build.set();worker.join(2)

    def test_real_flock_wait_obeys_single_deadline(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR",str(Path.home()/"ssd-cache/tmp"))) as directory:
            p=policy();p["root"]=str(Path(directory).resolve())
            with self.fixture_root(p),b.git_metadata_lock(p,b.Deadline(1),exclusive=False):
                start=time.monotonic()
                with self.assertRaises(b.Refused):
                    with b.git_metadata_lock(p,b.Deadline(.05)):self.fail("exclusive lock overlapped auditor")
                self.assertLess(time.monotonic()-start,.3)

    def test_root_lock_rejects_wrong_owner_mode_links_and_inode(self):
        meta=Mock(st_mode=0o100600,st_uid=0,st_gid=0,st_nlink=1,st_dev=1,st_ino=2,st_size=0,st_mtime_ns=1,st_ctime_ns=1)
        path=Mock();path.lstat.return_value=meta
        changes=[{"st_uid":1000},{"st_gid":1001},{"st_mode":0o100640},{"st_nlink":2},{"st_mode":0o040600},{"st_size":1}]
        with patch.object(b,"controlled"):
            for change in changes:
                bad=copy.copy(meta)
                for key,value in change.items():setattr(bad,key,value)
                with patch.object(b.os,"fstat",return_value=bad):
                    with self.assertRaises(b.Refused):b.check_git_lock(1,path)
            with patch.object(b.os,"fstat",return_value=meta):
                fresh=copy.copy(meta);fresh.st_ino=3;path.lstat.return_value=fresh
                with self.assertRaises(b.Refused):b.check_git_lock(1,path)
                fresh=copy.copy(meta);fresh.st_size=1;path.lstat.return_value=fresh
                with self.assertRaises(b.Refused):b.check_git_lock(1,path)


class WorkspaceControlTests(unittest.TestCase):
    def fixture(self):
        broker=b.Broker.__new__(b.Broker);broker.policy=policy();broker.policy_sha256="3"*64
        broker.state_lock=threading.RLock();broker.git_lock=threading.Lock();broker.closing=threading.Event()
        broker.manifest={"cid":"2"*64,"status":"ready"};broker.active_provider=True;broker.active_workspace=0
        broker.identity=Mock(side_effect=lambda:broker.manifest["cid"]);broker.preflight=Mock();broker.stage_check=Mock()
        broker.native_check=Mock();broker.engine=Mock();broker.cleanup=Mock();broker.exec_exit=Mock(return_value=0)
        broker.exec_start=Mock(return_value=("2"*64,"exec-scope",Mock()))
        return broker

    def test_slow_workspace_copy_keeps_active_provider_guard_responsive(self):
        broker=self.fixture();copying=threading.Event();release=threading.Event();errors=[];published=[]
        request={"v":1,"session_id":"unit-scope","op":"workspace_exec","argv":["status","--porcelain"]}
        def slow(policy,deadline,publication_scope):
            copying.set();self.assertTrue(release.wait(2))
            with publication_scope():published.append(True)
            return Path("/unused-scoped-view"),"/run/doxa/session/git-safe/"+"1"*32
        def execute():
            try:broker.execute(request,Mock())
            except BaseException as error:errors.append(error)
        with patch.object(b,"build_git_view",side_effect=slow),patch.object(b,"retire_git_view"),patch.object(b,"stream_stdio"):
            worker=threading.Thread(target=execute);worker.start()
            try:
                self.assertTrue(copying.wait(1));self.assertEqual(broker.active_workspace,1)
                self.assertTrue(broker.state_lock.acquire(timeout=.2),"private copy held global control lock")
                try:
                    start=time.monotonic();proof=broker.guard(b.Deadline(.2))
                    self.assertTrue(proof["guard_ready"]);self.assertLess(time.monotonic()-start,.1)
                    with self.assertRaisesRegex(b.Refused,"transport is active"):broker.idle()
                finally:broker.state_lock.release()
                release.set();worker.join(2);self.assertFalse(worker.is_alive());self.assertFalse(errors)
                self.assertEqual(published,[True]);self.assertEqual(broker.active_workspace,0);self.assertTrue(broker.active_provider)
                broker.cleanup.assert_not_called();broker.exec_start.assert_called_once()
            finally:release.set();worker.join(2)

    def test_changed_scope_during_private_copy_never_publishes_or_executes(self):
        broker=self.fixture();published=[]
        request={"v":1,"session_id":"unit-scope","op":"workspace_exec","argv":["status"]}
        def changed(policy,deadline,publication_scope):
            broker.engine=Mock()
            with publication_scope():published.append(True)
            self.fail("scope change was admitted")
        with patch.object(b,"build_git_view",side_effect=changed):
            with self.assertRaisesRegex(b.Refused,"scope changed"):broker.execute(request,Mock())
        self.assertFalse(published);broker.exec_start.assert_not_called();self.assertEqual(broker.active_workspace,0)
        broker.cleanup.assert_called_once_with("2"*64)


class ConcurrentControlTests(unittest.TestCase):
    def test_control_snapshot_dispatches_while_provider_stdio_is_active(self):
        short=Path.home()/".dt"; short.mkdir(mode=0o700,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="rb-",dir=short) as directory:
            runtime=Path(directory); endpoint=runtime/"broker.sock"; original_uid=os.geteuid()
            p=policy(); p["caller_uid"]=original_uid
            fake=Mock(policy=p,state_dir=runtime,manifest=None,state_lock=threading.RLock(),closing=threading.Event())
            fake.describe.return_value={"status":"unprepared","protocol":b.PROTOCOL}
            started,release=threading.Event(),threading.Event()
            def execute(request,client):
                started.set(); release.wait(2)
            fake.execute.side_effect=execute
            def snapshot(request,client): client.json_frame(b.TAG_JSON,{"status":"concurrent-snapshot"})
            fake.provider_snapshot.side_effect=snapshot
            fake.idle.side_effect=lambda: b.require(not started.is_set(),"session transport is active")
            signals={}
            def signal_handler(number,handler): signals[number]=handler
            errors=[]
            def run():
                try: b.serve(Path("/fixture-policy"))
                except BaseException as error: errors.append(error)
            with patch.object(b,"Broker",return_value=fake),patch.object(b,"publish_instance",return_value=({"start_time":1},b"{}")),patch.object(b,"controlled"),patch.object(b.os,"geteuid",return_value=0),patch.object(b.os,"chown"),patch.object(b.signal,"signal",side_effect=signal_handler),patch.object(b,"policy_paths",return_value={"socket":str(endpoint)}):
                server=threading.Thread(target=run); server.start()
                for _ in range(100):
                    if endpoint.exists(): break
                    time.sleep(.005)
                def connection(op):
                    peer=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); peer.connect(str(endpoint))
                    channel=b.Channel(peer,b.Deadline(1)); raw=b.compact({"v":1,"session_id":p["session_id"],"op":op})
                    channel.send(len(raw).to_bytes(4,"big")+raw)
                    return peer,channel
                provider, _=connection("provider_exec")
                self.assertTrue(started.wait(1))
                try:
                    fake.guard=Mock(return_value={"status":"ready","guard_ready":True})
                    guard,channel=connection("guard")
                    try:
                        tag,payload=channel.receive_frame();self.assertEqual(tag,b.TAG_JSON)
                        self.assertIs(b.strict_json(payload)["guard_ready"],True);fake.guard.assert_called_once()
                    finally:guard.close()
                    control, channel=connection("provider_snapshot")
                    try:
                        tag,payload=channel.receive_frame()
                        self.assertEqual(tag,b.TAG_JSON); self.assertEqual(b.strict_json(payload)["status"],"concurrent-snapshot")
                    finally: control.close()
                    idle, channel=connection("prepare")
                    try:
                        tag,payload=channel.receive_frame()
                        self.assertEqual(tag,b.TAG_ERROR); self.assertIn("active",b.strict_json(payload)["error"])
                        fake.prepare.assert_not_called()
                    finally: idle.close()
                finally:
                    release.set(); provider.close()
                    signals[b.signal.SIGTERM]()
                    server.join(3)
                self.assertFalse(server.is_alive()); self.assertFalse(errors)


if __name__ == "__main__": unittest.main()
