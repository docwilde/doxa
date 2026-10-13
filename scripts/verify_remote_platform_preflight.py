#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Observe remote QA prerequisites without credentials, writes, or provider turns.

Readiness does not establish FCM delivery, recovery, or authenticated lifecycle.
All release gates remain unknown. stdout contains only allowlisted metadata.
"""

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import time

LIMIT = 65_536
PACKAGE = "ai.ampiric.doxa.remote"


def bounded(command, timeout=12):
    """Bound wall time and combined stdout/stderr before retaining subprocess data."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    output, total = bytearray(), 0
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    for pipe in (process.stdout, process.stderr):
        selector.register(pipe, selectors.EVENT_READ)
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            for key, _ in selector.select(min(remaining, .2)):
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total += len(chunk)
                if total > LIMIT:
                    raise ValueError("output_limit")
                if key.fileobj is process.stdout:
                    output.extend(chunk)
        code = process.wait(timeout=max(.001, deadline - time.monotonic()))
        if code != 0:
            raise ValueError("command_failed")
        return output.decode("utf-8", errors="strict").strip()
    finally:
        selector.close()
        # Pipe-holding descendants prevent EOF, so timeout/error cleanup occurs
        # before wait() reaps the leader. Its PID still reserves this group ID.
        # Normal completion already reaped it; never signal a reusable PGID.
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()
        process.stdout.close()
        process.stderr.close()


def observe(command, runner=bounded):
    try:
        return runner(command)
    except Exception:
        return None


def tailscale_state(raw):
    result = {"online": False, "user_owned": False}
    try:
        value = json.loads(raw)
        own = value["Self"]
        result["online"] = value.get("BackendState") == "Running" and own.get("Online") is True
        result["user_owned"] = (type(own.get("UserID")) is int and own["UserID"] > 0
                                and own.get("Tags") in (None, []))
    except (TypeError, ValueError, KeyError, AttributeError):
        pass
    return result


def host_observation(runner=bounded):
    # SSH transports this exact source. Neither env nor account files are read.
    raw = observe(["cat", "/etc/machine-id"], runner) if sys.platform == "linux" else None
    # Fail closed when distinct hosts cannot be identified. Hash, never emit IDs.
    identity = raw if raw and re.fullmatch(r"[0-9a-f]{32}", raw) else None
    if sys.platform == "darwin":
        raw = observe(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], runner)
        found = re.search(r'"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]{36})"', raw or "")
        identity = found.group(1).lower() if found else None
    tail = shutil.which("tailscale")
    state = tailscale_state(observe([tail, "status", "--json"], runner)) if tail else tailscale_state(None)
    return {"platform": sys.platform if sys.platform in ("linux", "darwin") else "other",
            "machine_hash": hashlib.sha256(identity.encode()).hexdigest() if identity else None,
            "tailscale_installed": bool(tail), "tailscale": state,
            "doxa_installed": bool(shutil.which("doxa")),
            "hub_installed": bool(shutil.which("doxa-hub")),
            "claude_cli_installed": bool(shutil.which("claude"))}


def valid_alias(value):
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value))


def remote_observation(alias, runner=bounded):
    if not valid_alias(alias):
        return {"status": "unknown", "reason": "invalid_ssh_alias"}
    # Embed maintained collector source rather than trusting arbitrary remote JSON.
    source = Path(__file__).read_text()
    # Import-style execution bypasses the CLI, without installing code remotely.
    command = "python3 -c " + shlex.quote("__name__ = 'doxa_remote_observer'\n" + source +
        "\nprint(json.dumps(host_observation(), sort_keys=True))\n")
    raw = observe(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                   "-o", "StrictHostKeyChecking=yes", alias, command], runner)
    try:
        value = json.loads(raw)
        if not valid_host(value):
            raise ValueError
        return {"status": "observed", **value}
    except (TypeError, ValueError):
        return {"status": "unknown", "reason": "ssh_observation_unavailable"}


def valid_host(value):
    fields = {"platform", "machine_hash", "tailscale_installed", "tailscale",
              "doxa_installed", "hub_installed", "claude_cli_installed"}
    if type(value) is not dict or set(value) != fields:
        return False
    identity = value["machine_hash"]
    if identity is not None and not (type(identity) is str and re.fullmatch(r"[0-9a-f]{64}", identity)):
        return False
    return (value["platform"] in ("linux", "darwin", "other")
            and all(type(value[key]) is bool for key in fields - {"platform", "machine_hash", "tailscale"})
            and type(value["tailscale"]) is dict
            and set(value["tailscale"]) == {"online", "user_owned"}
            and all(type(item) is bool for item in value["tailscale"].values()))


def android_observation(adb, serial, runner=bounded):
    result = {"status": "unknown", "reason": "explicit_device_serial_required"}
    if not serial:
        raw = observe([adb, "devices"], runner)
        if raw is None:
            result["reason"] = "adb_unavailable"
        else:
            result["attached_devices"] = sum(bool(re.fullmatch(r"\S+\s+device", row.strip()))
                                               for row in raw.splitlines()[1:])
        return result
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}", serial):
        return {"status": "unknown", "reason": "invalid_device_serial"}
    def shell(*args):
        return observe([adb, "-s", serial, "shell", *args], runner)
    if observe([adb, "-s", serial, "get-state"], runner) != "device":
        return {"status": "unknown", "reason": "device_not_authorized_or_online"}
    qemu = shell("getprop", "ro.kernel.qemu")
    boot = shell("getprop", "sys.boot_completed")
    sdk = shell("getprop", "ro.build.version.sdk")
    def installed(package):
        raw = shell("pm", "path", package)
        return raw is not None and raw.startswith("package:")
    # Neither notification bodies, app preferences, nor FCM tokens are collected.
    return {"status": "observed", "device_hash": hashlib.sha256(serial.encode()).hexdigest(),
            "physical_candidate": not serial.startswith("emulator-") and qemu in ("", "0"),
            "boot_completed": boot == "1", "sdk": int(sdk) if sdk and len(sdk) <= 3 and sdk.isdecimal() else None,
            "play_services_installed": installed("com.google.android.gms"),
            "tailscale_installed": installed("com.tailscale.ipn"),
            "app_installed": installed(PACKAGE)}


def make_receipt(android, hosts):
    live = [host for host in hosts if host.get("status") == "observed"]
    private = [host for host in live if host["tailscale_installed"]
               and host["tailscale"]["online"] and host["tailscale"]["user_owned"]
               and host["doxa_installed"] and host["machine_hash"]]
    identities = {host["machine_hash"] for host in private}
    ready_android = (android.get("status") == "observed" and android.get("physical_candidate") is True
                     and android.get("boot_completed") is True
                     and all(android.get(key) is True for key in
                             ("play_services_installed", "tailscale_installed", "app_installed")))
    return {"schema": "doxa-remote-platform-preflight-v1", "status": "unknown",
            "observed_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "collector_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "android": android, "hosts": hosts,
            "prerequisites": {"android_candidate": ready_android,
                "two_user_owned_tailnet_hosts": len(identities) >= 2,
                "macos_claude_cli": any(host["platform"] == "darwin" and host["claude_cli_installed"]
                                        for host in live)},
            "release_gates": {"physical_android_fcm_delivery": "unknown",
                "two_host_tailnet_recovery": "unknown", "authenticated_macos_lifecycle": "unknown"},
            "reason": "prerequisites_do_not_prove_delivery_recovery_or_authentication"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", default=shutil.which("adb") or "adb")
    parser.add_argument("--android-serial", help="explicit owner-approved provisioned device; never auto-selected")
    parser.add_argument("--ssh-host", action="append", default=[], help="existing trusted SSH alias; repeat at most four times")
    args = parser.parse_args(argv)
    if len(args.ssh_host) > 4 or any(not valid_alias(alias) for alias in args.ssh_host):
        parser.error("provide at most four normal SSH aliases")
    hosts = [{"status": "observed", **host_observation()}]
    hosts.extend(remote_observation(alias) for alias in args.ssh_host)
    result = make_receipt(android_observation(args.adb, args.android_serial), hosts)
    print(json.dumps(result, sort_keys=True))
    return 2  # Even complete prerequisites cannot close an empirical release gate.


if __name__ == "__main__":
    sys.exit(main())
