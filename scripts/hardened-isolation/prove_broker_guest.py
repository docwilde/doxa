#!/usr/bin/env python3
"""Read-only pinned Engine + negative peer proof on an authorized private guest.

Creates only an isolated fake-peer socket. Does not create/start/stop containers,
change Engine config, invoke a provider, grant readiness or install root services.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import struct
import time

spec = importlib.util.spec_from_file_location("root_broker", Path(__file__).with_name("root_broker.py"))
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


def proof(args):
    b.require(os.geteuid() == 0, "guest peer proof requires root")
    config = {"uid": 1001, "gid": 1001, "socket": str(args.socket), "exe_path": str(args.exe.resolve(strict=True)),
              "exe_sha256": b.sha_file(args.exe.resolve(strict=True), root=True), "config_path": str(args.config),
              "config_sha256": b.sha(b.read_regular(args.config, root=True, readonly=True)), "api_version": args.api_version}
    engine = b.Engine({"engine": config})
    result = {"scope": "read-only-Engine-and-negative-peer-primitive", "admissible_as_hardened": False,
              "caller_uid": 1000, "engine_uid": 1001, "root_uid": 0}
    try:
        _, info = engine.request(b.Deadline(5), "GET", "/info")
        result.update(engine_info_read=True, engine_rootless="name=rootless" in info.get("SecurityOptions", []),
                      engine_pid=engine.pid, engine_socket_dev=engine.socket_identity[0], engine_socket_ino=engine.socket_identity[1],
                      engine_exe_sha256=config["exe_sha256"], engine_config_sha256=config["config_sha256"],
                      exact_kernel_peer_pidfd=True)
        child = os.fork()
        if child == 0:
            os.setgroups([]); os.setgid(1000); os.setuid(1000)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(config["socket"])
            except PermissionError:
                os._exit(0)
            except OSError:
                os._exit(2)
            else:
                os._exit(1)
        _, status = os.waitpid(child, 0)
        b.require(os.waitstatus_to_exitcode(status) == 0, "caller Engine socket exclusion was not proved")
        result["caller_cannot_open_engine_socket"] = True
        scratch = Path("/run/doxa") / ("broker-negative-"+str(os.getpid()))
        scratch.mkdir(mode=0o700)
        os.chown(scratch, 1001, 1001)
        endpoint = scratch / "fake-engine.sock"
        ready_read, ready_write = os.pipe()
        finish_read, finish_write = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(ready_read); os.close(finish_write)
            os.setgroups([]); os.setgid(1001); os.setuid(1001); os.umask(0o077)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(endpoint)); listener.listen(1)
            os.write(ready_write, b"R"); os.close(ready_write)
            listener.settimeout(3)
            try:
                peer, _ = listener.accept()
                peer.close()
            except OSError:
                pass
            b.select.select([finish_read], [], [], 3)
            os.close(finish_read)
            os._exit(0)
        os.close(ready_write); os.close(finish_read)
        try:
            b.require(os.read(ready_read, 1) == b"R", "same-Engine-UID fake fixture did not start")
            fake_config = dict(config, socket=str(endpoint))
            fake = b.Engine({"engine": fake_config})
            try:
                fake.connect(b.Deadline(2))
            except b.Refused as error:
                b.require("peer executable" in str(error), "fake peer failed for an unrelated condition")
                result["same_engine_uid_fake_executable_refused"] = True
            else:
                raise b.Refused("same Engine UID fake peer was accepted")
            finally:
                fake.close()
        finally:
            os.close(ready_read)
            os.write(finish_write, b"D"); os.close(finish_write)
            os.waitpid(child, 0)
            shutil.rmtree(scratch)
        engine.live()
        result["engine_instance_unchanged"] = True
        return result
    finally:
        engine.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--exe", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--api-version", default="1.51")
    args = parser.parse_args()
    print(json.dumps(proof(args), sort_keys=True, indent=2))
