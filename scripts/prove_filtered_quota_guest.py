#!/usr/bin/env python3
"""Bounded filtered-worker EDQUOT/restart/remount proof in the private guest.

Requires the credential-free proof image containing the exact reviewed worker
and quota_filtered_fixture_provider.py as /usr/local/bin/claude. All workload
execs pass through exec-hardened. Receipts are observations, never admission.
The shared Engine restart must be coordinated with the guest owner first.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import threading
import time

BASE = Path("/root/isolation-proof")
MOUNT = Path("/srv/doxa-quota-proof")
BACKING = BASE / "quota-proof.ext4"
ID = "quota-filtered-proof"
ROOT = MOUNT / ID
PROVISION = BASE / "quota-bin/doxa-quota-provision"
NAME = "doxa-quota-filtered-proof"
WORKER = "/usr/local/bin/doxa-isolation-worker"
MARKER = ".quota-filtered-proof"


def run(args: list[str], expected: int = 0, keep_stdin: bool = False) -> str:
    env = {**os.environ, "TMPDIR": str(BASE)}
    if keep_stdin:
        process = subprocess.Popen(args, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=env)
        try:
            process.wait(timeout=90)
            output, errors = process.stdout.read(65537), process.stderr.read(65537)
        except BaseException:
            process.kill()
            process.wait()
            raise
        finally:
            process.stdin.close()
        code = process.returncode
    else:
        process = subprocess.run(args, text=True, capture_output=True, timeout=90, env=env)
        code, output, errors = process.returncode, process.stdout, process.stderr
    if code != expected or len(output) > 65536 or len(errors) > 65536:
        raise RuntimeError(f"{args[0]} exit {code}: {errors[:4096]}")
    return output.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--acknowledge-disposable-guest", required=True, action="store_true")
    parser.add_argument("--acknowledge-coordinated-engine-restart", action="store_true")
    parser.add_argument("--wait-for-engine-restart-barrier", action="store_true")
    parser.add_argument("--docker-host", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--worker-sha256", required=True)
    parser.add_argument("--engine-unit", required=True)
    parser.add_argument("--engine-unit-scope", choices=("user", "system"), required=True)
    args = parser.parse_args()
    barrier = BASE / "quota-engine-restart.go"
    if not args.acknowledge_coordinated_engine_restart and not args.wait_for_engine_restart_barrier:
        raise RuntimeError("coordinate the shared Engine restart or use the root-owned restart barrier")
    if args.wait_for_engine_restart_barrier and barrier.exists():
        raise RuntimeError("stale Engine restart barrier exists; inspect it before another proof")
    if os.geteuid() != 0 or not BASE.is_dir() or not BACKING.is_file():
        raise RuntimeError("prepared private disposable root guest fixture required")
    if 'VERSION_ID="26.04"' not in Path("/etc/os-release").read_text():
        raise RuntimeError("Ubuntu 26.04 disposable guest required")
    if run(["systemd-detect-virt", "--vm"]) != "kvm":
        raise RuntimeError("disposable KVM guest required")
    if not args.docker_host.startswith("unix:///run/") or not re.fullmatch(r"sha256:[0-9a-f]{64}", args.image):
        raise RuntimeError("exact private Engine socket and local content image required")
    if not re.fullmatch(r"[a-zA-Z0-9@_.-]+\.service", args.engine_unit):
        raise RuntimeError("invalid coordinated Engine unit")
    config = Path("/run/user/1001/doxa-quota-cli")
    config.mkdir(mode=0o700)
    os.chown(config, 1001, 1001)
    prefix = ["runuser", "-u", "engine", "--", "env", "XDG_RUNTIME_DIR=/run/user/1001",
              "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus", "docker", "--config", str(config),
              "--host", args.docker_host]

    def docker(*options: str, keep_stdin: bool = False) -> str:
        return run(prefix + list(options), keep_stdin=keep_stdin)

    security = json.loads(docker("info", "--format", "{{json .SecurityOptions}}"))
    if not any("rootless" in item for item in security):
        raise RuntimeError("Engine must be rootless")
    if docker("image", "inspect", "--format", "{{.Id}}", args.image) != args.image:
        raise RuntimeError("immutable fixture image identity differs")
    result = {"image": args.image, "worker_sha256": args.worker_sha256,
              "engine_security_options": security, "project_id": 5002, "hard_limit_bytes": 33554432}
    result["provision"] = json.loads(run([str(PROVISION), "create", ID, str(ROOT), "1000", "1001", "1001",
        "5002", "33554432", args.image, args.worker_sha256]))
    broker = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    broker.bind(str(ROOT / "broker/egress-claude.sock"))
    os.chown(ROOT / "broker/egress-claude.sock", 0, 1001)
    os.chmod(ROOT / "broker/egress-claude.sock", 0o660)
    broker.listen(8)
    broker.settimeout(0.2)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                client, _ = broker.accept()
            except socket.timeout:
                continue
            with client:
                client.sendall(b"quota-broker-ok")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    created = False
    try:
        options = ["create", "--pull", "never", "--name", NAME, "--label", "org.doxa.quota-proof=filtered",
                   "--network", "none", "--read-only", "--cap-drop", "ALL",
                   "--security-opt", "no-new-privileges:true", "--user", "0:0",
                   "--pids-limit", "64", "--memory", "512m", "--memory-swap", "512m", "--cpus", "1",
                   "--tmpfs", "/run/workertmp:rw,nosuid,nodev,size=16777216", "--env", "TMPDIR=/run/workertmp"]
        for source, target, readonly in [("checkout", "/workspace", False), ("home", "/home/doxa", False),
                                         ("cache", "/work-cache", False), ("broker", "/run/doxa/session", True)]:
            options += ["--mount", f"type=bind,src={ROOT / source},dst={target}" + (",readonly" if readonly else "")]
        options += ["--entrypoint", WORKER, args.image, "hold-hardened"]
        container_id = docker(*options)
        created = True
        result["container_id"] = container_id
        inspected = json.loads(docker("inspect", NAME))[0]
        if inspected["Id"] != container_id or inspected["Image"] != args.image:
            raise RuntimeError("fixture container/image identity mismatch")
        if not inspected["HostConfig"]["ReadonlyRootfs"] or inspected["HostConfig"]["NetworkMode"] != "none":
            raise RuntimeError("fixture worker boundary mismatch")
        actual = {(item["Source"], item["Destination"], item["RW"]) for item in inspected["Mounts"]}
        expected = {(str(ROOT / source), target, writable) for source, target, writable in
                    [("checkout", "/workspace", True), ("home", "/home/doxa", True),
                     ("cache", "/work-cache", True), ("broker", "/run/doxa/session", False)]}
        if actual != expected:
            raise RuntimeError("fixture exposes unexpected bind sources")
        docker("start", NAME)
        result["filter_probe"] = json.loads(docker("exec", NAME, WORKER, "filter-probe"))

        def workload(*options: str) -> dict:
            value = docker("exec", "-i", NAME, WORKER, "exec-hardened", "/usr/local/bin/claude",
                           *options, keep_stdin=True)
            return json.loads(value)

        worker_hash = docker("exec", NAME, "/usr/bin/sha256sum", WORKER).split()[0]
        if worker_hash != args.worker_sha256:
            raise RuntimeError("exact image-owned worker binary hash differs")
        result["mutation"] = workload("mutate")
        control = result["mutation"]
        if any(value != 1 for value in control["attempts"].values()) or not control["inherits_after"]:
            raise RuntimeError("worker-owned descendant mutation bypassed mandatory filter")
        if control["project_before"] != 5002 or control["project_after"] != 5002:
            raise RuntimeError("worker-owned descendant escaped its quota project")
        result["descendant_host_uid"] = (ROOT / "checkout/owner-filter-control").stat().st_uid
        if result["descendant_host_uid"] != 1001:
            raise RuntimeError("descendant did not belong to the separated Engine UID")
        result["broker"] = workload("broker")
        if result["broker"] != {"write_errno": 30, "reply": "quota-broker-ok"}:
            raise RuntimeError("worker broker bind was not read-only or socket failed")
        result["solo"] = {source: workload("solo", source, "67108864") for source in ("checkout", "home", "cache")}
        if any(item["errno"] != 122 or item["bytes_written"] <= 0 for item in result["solo"].values()):
            raise RuntimeError("each writable bind must produce positive writes ending in EDQUOT")
        aggregate = [workload("write", "checkout", "8388608"), workload("write", "home", "8388608"),
                     workload("write", "cache", "67108864")]
        if aggregate[0]["bytes_written"] != 8388608 or aggregate[1]["bytes_written"] != 8388608:
            raise RuntimeError("aggregate prefixes were not retained")
        if aggregate[2]["errno"] != 122 or not 0 < aggregate[2]["bytes_written"] < result["solo"]["cache"]["bytes_written"]:
            raise RuntimeError("aggregate project ceiling did not combine the bind sources")
        result["aggregate"] = aggregate
        result["sizes_before"] = workload("sizes")
        result["quota_before_restart"] = json.loads(run([str(PROVISION), "verify", ID]))
        docker("stop", "--time", "2", NAME)
        docker("start", NAME)
        result["after_container_restart"] = workload("append", "cache", "1048576")
        if result["after_container_restart"]["errno"] != 122 or result["after_container_restart"]["bytes_written"] != 0:
            raise RuntimeError("container restart lost the aggregate hard limit")
        if args.wait_for_engine_restart_barrier:
            (BASE / "quota-filtered-initial.json").write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps({"initial_quota_proof_ready": True, "container_id": container_id,
                              "restart_barrier": str(barrier), "admissible_as_hard_quota": False}), flush=True)
            deadline = time.monotonic() + 3600
            while True:
                try:
                    fd = os.open(barrier, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                except FileNotFoundError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("shared Engine restart was not coordinated before the deadline")
                    time.sleep(1)
                    continue
                with os.fdopen(fd, "r") as file:
                    metadata = os.fstat(file.fileno())
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_nlink != 1 \
                            or metadata.st_mode & 0o777 != 0o600 or file.read(256).strip() != container_id:
                        raise RuntimeError("Engine restart barrier does not identify this exact root-controlled run")
                barrier.unlink()
                break
        docker("stop", "--time", "2", NAME)
        unit_command = ["systemctl", "restart", args.engine_unit]
        show = ["systemctl", "show", args.engine_unit, "--property=MainPID", "--value"]
        if args.engine_unit_scope == "user":
            user_env = ["runuser", "-u", "engine", "--", "env", "XDG_RUNTIME_DIR=/run/user/1001",
                        "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus"]
            unit_command = user_env + ["systemctl", "--user", "restart", args.engine_unit]
            show = user_env + ["systemctl", "--user", "show", args.engine_unit, "--property=MainPID", "--value"]
        result["engine_pid_before"] = int(run(show))
        run(unit_command)
        result["engine_pid_after"] = int(run(show))
        if result["engine_pid_after"] <= 0 or result["engine_pid_after"] == result["engine_pid_before"]:
            raise RuntimeError("coordinated Engine restart did not change its PID")
        for _ in range(50):
            try:
                docker("info", "--format", "{{.ID}}")
                break
            except RuntimeError:
                time.sleep(0.2)
        docker("start", NAME)
        result["after_engine_restart"] = workload("append", "cache", "1048576")
        if result["after_engine_restart"]["errno"] != 122 or result["after_engine_restart"]["bytes_written"] != 0:
            raise RuntimeError("Engine restart lost the aggregate hard limit")
        (BASE / "quota-filtered-transition-partial.json").write_text(json.dumps(result, indent=2) + "\n")
        docker("stop", "--time", "2", NAME)
        # Stop the installed kernel-test helper holding mount FDs before the
        # root source remount. This is a separate non-authorizing fixture.
        run(["systemctl", "stop", "doxa-quota-enforced@quota-kernel-test.socket",
             "doxa-quota-enforced@quota-kernel-test.service"])
        # A pathname-bound host Unix listener retains its backing mount. Close
        # it before a real unmount, then recreate it under root control after
        # remount; its directory inode, owner/mode and project remain pinned.
        stop.set()
        thread.join(timeout=2)
        if thread.is_alive():
            raise RuntimeError("root broker listener did not quiesce for remount")
        broker.close()
        (ROOT / "broker/egress-claude.sock").unlink()
        run(["umount", str(MOUNT)])
        run(["mount", "-o", "loop,prjquota", str(BACKING), str(MOUNT)])
        broker = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        broker.bind(str(ROOT / "broker/egress-claude.sock"))
        os.chown(ROOT / "broker/egress-claude.sock", 0, 1001)
        os.chmod(ROOT / "broker/egress-claude.sock", 0o660)
        broker.listen(8)
        broker.settimeout(0.2)
        stop.clear()
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        run([str(PROVISION), "verify", ID], expected=2)
        result["stale_mount_policy_refused"] = True
        result["quota_after_rebind"] = json.loads(run([str(PROVISION), "refresh-after-remount", ID]))
        if result["quota_after_rebind"]["mount_id"] == result["quota_before_restart"]["mount_id"]:
            raise RuntimeError("source remount did not change its unique mount identity")
        docker("start", NAME)
        result["after_source_remount"] = workload("append", "cache", "1048576")
        if result["after_source_remount"]["errno"] != 122 or result["after_source_remount"]["bytes_written"] != 0:
            raise RuntimeError("source remount lost the aggregate hard limit")
        result["sizes_after"] = workload("sizes")
        if result["sizes_after"] != result["sizes_before"]:
            raise RuntimeError("persistent quota probe files changed across restarts/remount")
        result["broker_after_remount"] = workload("broker")
        if result["broker_after_remount"] != result["broker"]:
            raise RuntimeError("broker changed across source remount")
        if docker("inspect", "--format", "{{.Id}}", NAME) != container_id:
            raise RuntimeError("fixture container identity changed across transitions")
    finally:
        if created:
            docker("stop", "--time", "2", NAME)
            docker("rm", NAME)
        stop.set()
        thread.join(timeout=2)
        broker.close()
        (ROOT / "broker/egress-claude.sock").unlink(missing_ok=True)
    for source in ("checkout", "home", "cache"):
        (ROOT / source / MARKER).unlink(missing_ok=True)
    (ROOT / "checkout/owner-filter-control").rmdir()
    result["quota_after_cleanup"] = json.loads(run([str(PROVISION), "verify", ID]))
    result["container_and_marker_cleanup"] = True
    result["admissible_as_hard_quota"] = False
    result["live_provider_verified"] = False
    (BASE / "quota-filtered-proof.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
