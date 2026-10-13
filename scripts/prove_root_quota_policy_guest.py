#!/usr/bin/env python3
"""Opt-in root quota installation proof in a disposable Ubuntu QEMU guest.

The first phase verifies real kernel provisioning, authority separation,
immutable bind roots, owner-mutable descendant refusal and stale/remount policy
handling. The Docker phase must be run separately with the actual filtered
worker image. No fixture receipt enables admission.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess

BASE = Path("/root/isolation-proof")
IMAGE = BASE / "quota-proof.ext4"
MOUNT = Path("/srv/doxa-quota-proof")
SESSION = "quota-kernel-test"
ROOT = MOUNT / SESSION
POLICY = Path("/etc/doxa/quota-enforced") / f"{SESSION}.json"
PROVISION = BASE / "quota-bin/doxa-quota-provision"


def command(args: list[str], expected: int = 0) -> str:
    result = subprocess.run(args, text=True, capture_output=True, timeout=90,
                            env={**os.environ, "TMPDIR": str(BASE)})
    if result.returncode != expected:
        raise RuntimeError(f"{args[0]} exit {result.returncode}: {result.stderr.strip()}")
    return result.stdout.strip()


def verify(expected: int = 0) -> dict:
    output = command([str(PROVISION), "verify", SESSION], expected)
    return json.loads(output) if expected == 0 else {"refused": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--acknowledge-disposable-guest", action="store_true", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--worker-sha256", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or not args.acknowledge_disposable_guest:
        raise RuntimeError("explicit disposable guest root acknowledgement is required")
    release = Path("/etc/os-release").read_text()
    if 'VERSION_ID="26.04"' not in release or command(["systemd-detect-virt", "--vm"]) != "kvm":
        raise RuntimeError("this proof is restricted to the disposable Ubuntu 26.04 KVM guest")
    if not BASE.is_dir() or BASE.is_symlink() or BASE.stat().st_uid != 0:
        raise RuntimeError("root-controlled guest proof directory is absent")
    if IMAGE.exists() or ROOT.exists() or POLICY.exists():
        raise RuntimeError("proof artifacts already exist; inspect them before another run")
    BASE.chmod(0o700)
    MOUNT.mkdir(mode=0o755)
    with IMAGE.open("xb") as file:
        file.truncate(1024 * 1024 * 1024)
    IMAGE.chmod(0o600)
    command(["mkfs.ext4", "-q", "-F", "-O", "project,quota", str(IMAGE)])
    command(["mount", "-o", "loop,prjquota", str(IMAGE), str(MOUNT)])
    request = [str(PROVISION), "create", SESSION, str(ROOT), "1000", "1001", "1001",
               "5001", "33554432", args.image, args.worker_sha256]
    result = {"provision": json.loads(command(request))}
    result["initial"] = verify()
    command(["install", "-D", "-m", "0755", str(PROVISION), "/usr/libexec/doxa/doxa-quota-provision"])
    result["caller_cannot_load_root_authority"] = command(
        ["runuser", "-u", "doxa", "--", "/usr/libexec/doxa/doxa-quota-provision", "verify", SESSION], 2) == ""
    # A separate allocated project must be rejected before installing a policy.
    duplicate = request.copy()
    duplicate[2] = "quota-kernel-duplicate"
    duplicate[3] = str(MOUNT / duplicate[2])
    command(duplicate, 2)
    result["duplicate_project_refused"] = True
    (MOUNT / duplicate[2]).rmdir()
    original_policy = json.loads(POLICY.read_text())
    wrong_boot = dict(original_policy)
    wrong_boot["boot_id"] = "00000000-0000-0000-0000-000000000000"
    POLICY.write_text(json.dumps(wrong_boot))
    verify(2)
    POLICY.write_text(json.dumps(original_policy))
    result["foreign_kernel_boot_policy_refused"] = True
    os.chmod(ROOT / "cache", 0o777)
    verify(2)
    os.chmod(ROOT / "cache", 0o770)
    result["mutable_bind_root_refused"] = True
    changed = dict(original_policy)
    changed["bindings"] = dict(original_policy["bindings"])
    changed["bindings"]["home"] = dict(changed["bindings"]["home"])
    changed["bindings"]["home"]["inode"] += 1
    POLICY.write_text(json.dumps(changed))
    verify(2)
    POLICY.write_text(json.dumps(original_policy))
    result["directory_identity_tamper_refused"] = True
    # Control: root-owned bind attrs refuse, but unfiltered owner-created
    # descendants can clear inheritance. A snapshot alone is insufficient.
    code = r'''
import errno, fcntl, json, os, struct, sys
root = sys.argv[1]
directory = root + '/checkout/owner-control'
os.mkdir(directory)
def change(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    value = bytearray(28)
    fcntl.ioctl(fd, 0x801c581f, value, True)
    before = struct.unpack('IIIII8s', value)
    fields = list(before); fields[0] &= ~0x200; fields[3] = 0
    try:
        fcntl.ioctl(fd, 0x401c5820, struct.pack('IIIII8s', *fields))
        err = None
    except OSError as cause:
        err = cause.errno
    fcntl.ioctl(fd, 0x801c581f, value, True)
    after = struct.unpack('IIIII8s', value)
    os.close(fd)
    return {'errno': err, 'before_project': before[3], 'after_project': after[3],
            'before_inherits': bool(before[0] & 0x200), 'after_inherits': bool(after[0] & 0x200)}
print(json.dumps({'bind_root': change(root + '/checkout'), 'descendant': change(directory)}))
'''
    control = json.loads(command(["runuser", "-u", "engine", "--", "python3", "-c", code, str(ROOT)]))
    if control["bind_root"]["errno"] != 1 or control["descendant"]["after_inherits"]:
        raise RuntimeError(f"unexpected project attribute control: {control}")
    verify(2)
    (ROOT / "checkout/owner-control").rmdir()
    result["unfiltered_descendant_control"] = control
    verify()
    # Kernel quota survives source remount; old policies are deliberately stale.
    command(["umount", str(MOUNT)])
    command(["mount", "-o", "loop,prjquota", str(IMAGE), str(MOUNT)])
    verify(2)
    result["stale_mount_policy_refused"] = True
    result["rebound"] = json.loads(command([str(PROVISION), "refresh-after-remount", SESSION]))
    if result["rebound"]["mount_id"] == result["initial"]["mount_id"]:
        raise RuntimeError("remount did not change the mount identity")
    result["final"] = verify()
    result["admissible_as_hard_quota"] = False
    result["container_edquot_proved"] = False
    (BASE / "quota-policy-proof.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
