#!/usr/bin/env python3
"""Root-installed requestless helper controls, on the prepared private guest."""
import json
import os
from pathlib import Path
import subprocess

BASE = Path("/root/isolation-proof")
ID = "quota-kernel-test"
POLICY = Path(f"/etc/doxa/quota-enforced/{ID}.json")
SOCKET = f"/run/doxa/quota-enforced/{ID}.sock"
SERVICE = f"doxa-quota-enforced@{ID}.service"
CLIENT = r'''
import json, socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(3)
try:
    s.connect(sys.argv[1])
except OSError as cause:
    print(json.dumps({'connect_errno': cause.errno})); sys.exit(0)
if len(sys.argv) > 2:
    s.sendall(b'{"root":"/outside","project_id":9999,"admissible_as_hard_quota":true}\n')
body = b''
while not body.endswith(b'\n'):
    value = s.recv(4097 - len(body))
    if not value or len(body) + len(value) > 4096: raise RuntimeError('missing or oversized helper reply')
    body += value
peer_uid = int.from_bytes(s.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)[4:8], sys.byteorder)
result = json.loads(body); result['peer_uid'] = peer_uid; print(json.dumps(result))
'''


def query(user: str, payload: bool = False) -> dict:
    args = ["runuser", "-u", user, "--", "python3", "-c", CLIENT, SOCKET]
    if payload:
        args.append("ignored-body")
    return json.loads(subprocess.check_output(args, text=True, timeout=10))


def main() -> None:
    if os.geteuid() != 0 or not BASE.is_dir() or not POLICY.is_file():
        raise RuntimeError("prepared disposable guest root policy is required")
    result = {"initial": query("doxa"), "worker": query("engine")}
    assert result["initial"]["peer_uid"] == 0
    assert result["initial"]["kernel_quota_verified"]
    assert not result["initial"]["admissible_as_hard_quota"]
    assert result["worker"]["connect_errno"] == 13
    private_file = Path(json.loads(POLICY.read_bytes())["root"]) / "home/private-worker-data"
    subprocess.run(["runuser", "-u", "engine", "--", "python3", "-c",
                    "import os,sys; fd=os.open(sys.argv[1], os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600); os.write(fd,b'fixture'); os.close(fd)",
                    str(private_file)], check=True)
    try:
        result["private_worker_data"] = query("doxa")
        assert private_file.stat().st_uid == 1001 and private_file.stat().st_mode & 0o777 == 0o600
        assert result["private_worker_data"]["kernel_quota_verified"]
    finally:
        private_file.unlink()
    result["cannot_select_by_request_body"] = query("doxa", payload=True)
    assert result["cannot_select_by_request_body"]["project_id"] == 5001
    saved = POLICY.read_bytes()
    try:
        policy = json.loads(saved)
        policy["admissible_as_hard_quota"] = True
        POLICY.write_text(json.dumps(policy))
        result["tampered"] = query("doxa")
        assert not result["tampered"]["kernel_quota_verified"]
    finally:
        POLICY.write_bytes(saved)
    result["restored"] = query("doxa")
    assert result["restored"]["kernel_quota_verified"]
    provision = str(BASE / "quota-bin/doxa-quota-provision")
    subprocess.check_output([provision, "refresh-after-remount", ID], text=True)
    result["old_helper_policy_lease_refused"] = query("doxa")
    assert not result["old_helper_policy_lease_refused"]["kernel_quota_verified"]
    subprocess.run(["systemctl", "stop", SERVICE], check=True)
    result["after_reactivation"] = query("doxa")
    assert result["after_reactivation"]["kernel_quota_verified"]
    assert result["after_reactivation"]["policy_identity"]["inode"] != result["initial"]["policy_identity"]["inode"]
    result["admissible_as_hard_quota"] = False
    (BASE / "quota-helper-proof.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
