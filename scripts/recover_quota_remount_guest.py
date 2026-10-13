#!/usr/bin/env python3
"""Finish the retained legacy-endpoint fixture after its broker EBUSY control.

This recovery performs no further shared Engine restart. It preserves the
original aggregate files and uses the original content-pinned image/policy.
New normal proofs use the family-aware main harness instead.
"""
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import threading

BASE = Path("/root/isolation-proof")
ROOT = Path("/srv/doxa-quota-proof/quota-filtered-proof")
NAME = "doxa-quota-remount-recovery"
WORKER = "/usr/local/bin/doxa-isolation-worker"
PROVISION = BASE / "quota-bin/doxa-quota-provision"
spec = importlib.util.spec_from_file_location("quota_original", BASE / "prove_filtered_quota_guest.py")
original = importlib.util.module_from_spec(spec)
spec.loader.exec_module(original)
run = original.run


def main() -> None:
    if os.geteuid() != 0 or not BASE.is_dir() or not ROOT.is_dir():
        raise RuntimeError("retained private disposable root fixture required")
    initial = json.loads((BASE / "quota-filtered-initial.json").read_text())
    prefix = ["runuser", "-u", "engine", "--", "env", "XDG_RUNTIME_DIR=/run/user/1001", "docker",
              "--config", "/run/user/1001/doxa-quota-cli", "--host", "unix:///run/user/1001/doxa-proof/docker.sock"]

    def docker(*options, keep_stdin=False):
        return run(prefix + list(options), keep_stdin=keep_stdin)

    def workload(*options):
        return json.loads(docker("exec", "-i", NAME, WORKER, "exec-hardened", "/usr/local/bin/claude",
                                 *options, keep_stdin=True))

    options = ["create", "--pull", "never", "--name", NAME, "--network", "none", "--read-only",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--user", "0:0",
               "--pids-limit", "64", "--memory", "512m", "--memory-swap", "512m", "--cpus", "1",
               "--tmpfs", "/run/workertmp:rw,nosuid,nodev,size=16777216", "--env", "TMPDIR=/run/workertmp"]
    for source, destination, readonly in [("checkout", "/workspace", False), ("home", "/home/doxa", False),
                                          ("cache", "/work-cache", False), ("broker", "/run/doxa/session", True)]:
        options += ["--mount", f"type=bind,src={ROOT/source},dst={destination}" + (",readonly" if readonly else "")]
    options += ["--entrypoint", WORKER, initial["image"], "hold-hardened"]
    cid = docker(*options)
    result = {"initial": initial, "recovery_container_id": cid, "additional_engine_restart": False}
    broker = None
    thread = None
    stop = threading.Event()
    try:
        docker("start", NAME)
        result["after_engine_restart_explicit"] = workload("append", "cache", "1048576")
        assert result["after_engine_restart_explicit"]["errno"] == 122
        assert result["after_engine_restart_explicit"]["bytes_written"] == 0
        result["retained_sizes_before_remount"] = workload("sizes")
        assert result["retained_sizes_before_remount"] == initial["sizes_before"]
        docker("stop", "--time", "2", NAME)
        result["quota_before_remount"] = json.loads(run([str(PROVISION), "verify", "quota-filtered-proof"]))
        run(["umount", "/srv/doxa-quota-proof"])
        run(["mount", "-o", "loop,prjquota", str(BASE / "quota-proof.ext4"), "/srv/doxa-quota-proof"])
        run([str(PROVISION), "verify", "quota-filtered-proof"], expected=2)
        result["stale_quota_policy_refused"] = True
        result["quota_after_remount"] = json.loads(run([str(PROVISION), "refresh-after-remount", "quota-filtered-proof"]))
        assert result["quota_after_remount"]["mount_id"] != result["quota_before_remount"]["mount_id"]
        broker = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        broker.bind(str(ROOT / "broker/egress.sock"))
        os.chown(ROOT / "broker/egress.sock", 0, 1001)
        os.chmod(ROOT / "broker/egress.sock", 0o660)
        broker.listen(8)
        broker.settimeout(0.2)

        def serve():
            while not stop.is_set():
                try:
                    peer, _ = broker.accept()
                except socket.timeout:
                    continue
                with peer:
                    peer.sendall(b"quota-broker-ok")

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        docker("start", NAME)
        result["after_source_remount"] = workload("append", "cache", "1048576")
        assert result["after_source_remount"]["errno"] == 122
        assert result["after_source_remount"]["bytes_written"] == 0
        result["retained_sizes_after_remount"] = workload("sizes")
        assert result["retained_sizes_after_remount"] == initial["sizes_before"]
        result["broker_after_remount"] = workload("broker")
        assert result["broker_after_remount"] == {"write_errno": 30, "reply": "quota-broker-ok"}
        assert docker("inspect", "--format", "{{.Id}}", NAME) == cid
    finally:
        docker("stop", "--time", "2", NAME)
        docker("rm", NAME)
        stop.set()
        if thread:
            thread.join(timeout=2)
        if broker:
            broker.close()
        (ROOT / "broker/egress.sock").unlink(missing_ok=True)
    for source in ("checkout", "home", "cache"):
        (ROOT / source / ".quota-filtered-proof").unlink()
    (ROOT / "checkout/owner-filter-control").rmdir()
    result["quota_after_cleanup"] = json.loads(run([str(PROVISION), "verify", "quota-filtered-proof"]))
    result["containers_and_files_removed"] = True
    result["admissible_as_hard_quota"] = False
    result["live_provider_verified"] = False
    (BASE / "quota-remount-recovery-proof.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
