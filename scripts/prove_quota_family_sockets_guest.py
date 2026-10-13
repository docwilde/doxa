#!/usr/bin/env python3
"""Prove root-owned three-family broker entries on the private guest tree."""
import json
import os
from pathlib import Path
import socket
import subprocess

BASE = Path("/root/isolation-proof")
PROVISION = BASE / "quota-bin/doxa-quota-provision-family"
ROOT = Path("/srv/doxa-quota-proof/quota-kernel-test")


def verify(expected: int = 0) -> dict:
    result = subprocess.run([str(PROVISION), "verify", "quota-kernel-test"], text=True,
                            capture_output=True, timeout=10)
    if result.returncode != expected:
        raise RuntimeError(f"family quota verification exit {result.returncode}: {result.stderr}")
    return json.loads(result.stdout) if expected == 0 else {"refused": True}


def main() -> None:
    if os.geteuid() != 0 or not BASE.is_dir() or not ROOT.is_dir():
        raise RuntimeError("prepared private disposable guest root fixture required")
    if 'VERSION_ID="26.04"' not in Path("/etc/os-release").read_text():
        raise RuntimeError("disposable Ubuntu 26.04 guest required")
    broker = ROOT / "broker"
    if list(broker.iterdir()):
        raise RuntimeError("inspect nonempty private broker before the family proof")
    sockets = []
    result = {}
    try:
        for name in ("hook.sock", "egress-claude.sock", "egress-codex.sock"):
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(broker / name))
            os.chown(broker / name, 0, 1001)
            os.chmod(broker / name, 0o660)
            listener.listen(1)
            sockets.append((listener, broker / name))
        result["three_root_owned_family_sockets"] = verify()
        os.chmod(broker / "egress-codex.sock", 0o600)
        result["wrong_mode"] = verify(2)
        os.chmod(broker / "egress-codex.sock", 0o660)
        os.chown(broker / "egress-codex.sock", 1001, 1001)
        result["worker_owned_socket"] = verify(2)
        os.chown(broker / "egress-codex.sock", 0, 1001)
        # Replace one allowed family entry with the legacy name, preserving
        # its inode/ownership/mode so name scoping is the only changed property.
        os.rename(broker / "egress-codex.sock", broker / "egress.sock")
        try:
            result["legacy_name"] = verify(2)
        finally:
            os.rename(broker / "egress.sock", broker / "egress-codex.sock")
        extra = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        extra.bind(str(broker / "other.sock"))
        os.chown(broker / "other.sock", 0, 1001)
        os.chmod(broker / "other.sock", 0o660)
        try:
            result["extra_entry"] = verify(2)
        finally:
            extra.close()
            (broker / "other.sock").unlink()
        result["restored"] = verify()
    finally:
        for listener, path in sockets:
            listener.close()
            path.unlink()
    result["empty_after_cleanup"] = not list(broker.iterdir())
    result["admissible_as_hard_quota"] = False
    (BASE / "quota-family-sockets-proof.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
