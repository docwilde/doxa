#!/usr/bin/env python3
"""Installed zero-grant host proof, with separate opt-in signed TUI activation."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import stat
import sys
import time
import secrets

# Proof provenance includes ignored files: loading this helper must not dirty
# the clean source checkout with Python bytecode caches.
sys.dont_write_bytecode = True

COMMON_PATH = Path(__file__).resolve().with_name("plugin-delegated-host-proof.py")
spec = importlib.util.spec_from_file_location("plugin_delegated_host_proof", COMMON_PATH)
assert spec is not None and spec.loader is not None
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)

ROOT = Path(__file__).resolve().parent.parent
MAX_EXECUTABLE_BYTES = 256 * 1024 * 1024
EXTRA_CASES = {"module-error": "ModuleFailure(Trap)", "launch-error": "Refused"}
HOST_HEADER = "Read-only installed plugin host observation (TUI execution disabled)"
HOST_FOOTER = ("This does not verify namespace isolation, limits or descendant cleanup; "
               "no TUI authority was issued.")
AUTHORITY_PURPOSE = "doxa-installed-plugin-tui-v1"
ED25519_PUBLIC_DER_PREFIX = bytes.fromhex("302a300506032b6570032100")


def stable_metadata(meta: os.stat_result) -> tuple[int, ...]:
    """File identity excludes atime, which our own reads may legitimately update."""
    return (meta.st_dev, meta.st_ino, meta.st_uid, meta.st_mode, meta.st_nlink,
            meta.st_size, meta.st_mtime_ns, meta.st_ctime_ns)


def private_read(path: Path, limit: int = 65536) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        common.require(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid()
                       and before.st_mode & 0o077 == 0 and before.st_nlink == 1
                       and before.st_size <= limit, "operator state must be private, regular and singly linked")
        data = os.read(fd, limit + 1)
        after = os.fstat(fd)
        common.require(len(data) <= limit and stable_metadata(before) == stable_metadata(after)
                       == stable_metadata(path.lstat()),
                       "operator state changed during read")
        return data
    finally:
        os.close(fd)


def activation_binding(options: argparse.Namespace, identities: dict[str, object]) -> dict[str, object]:
    """Observe one live frontend; a receipt can never substitute for this step."""
    try:
        import tomllib
    except ImportError as exc:
        raise common.ProofError("signed activation requires Python 3.11 or newer") from exc
    home = options.home
    common.require(home is not None and home.is_absolute() and home == home.resolve(strict=True),
                   "--activate requires an absolute canonical private --home")
    metadata = home.lstat()
    common.require(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.geteuid()
                   and metadata.st_mode & 0o077 == 0
                   and not any((ancestor / ".git").exists() for ancestor in (home, *home.parents)),
                   "activation home must be owner-private and outside a repository")
    config_bytes = private_read(home / "config.toml")
    try:
        policy = tomllib.loads(config_bytes.decode("utf-8"))["native_plugin_host"]
    except (KeyError, UnicodeDecodeError, ValueError) as exc:
        raise common.ProofError("private config has no valid enrolled plugin host operator") from exc
    common.require(isinstance(policy, dict) and set(policy) == {"operator_public_key", "epoch"}
                   and re.fullmatch(r"[0-9a-f]{64}", str(policy["operator_public_key"])) is not None
                   and isinstance(policy["epoch"], int) and not isinstance(policy["epoch"], bool)
                   and 0 < policy["epoch"] < 2**64, "invalid operator enrollment key or epoch")
    common.require(options.tui_pid is not None and options.tui_pid > 0,
                   "--activate requires a live --tui-pid")
    proc = Path(f"/proc/{options.tui_pid}")
    common.require(proc.stat().st_uid == os.geteuid(), "target frontend belongs to another user")
    record = common.bounded_read(proc / "stat")
    fields = record.rpartition(")")[2].split()
    common.require(len(fields) >= 20 and fields[19].isdecimal(), "target frontend start identity is invalid")
    executable = (proc / "exe").resolve(strict=True)
    common.require(executable == options.frontend.resolve(strict=True)
                   and trusted_identity(executable) == identities["frontend"],
                   "target process is not the reviewed installed frontend")
    membership = common.bounded_read(proc / "cgroup")
    common.require(membership == common.bounded_read(Path("/proc/self/cgroup")),
                   "operator and target frontend must occupy the same delegated supervisor leaf")
    common.require(options.operator_key is not None and options.operator_key.is_absolute(),
                   "--activate requires an absolute private --operator-key")
    private_read(options.operator_key, 4096)  # never print private key bytes
    return {"instance": {"uid": os.geteuid(), "pid": options.tui_pid, "start_ticks": int(fields[19]),
                         "boot_id": common.bounded_read(Path("/proc/sys/kernel/random/boot_id")).strip(),
                         "home_device": metadata.st_dev, "home_inode": metadata.st_ino},
            "policy": policy, "config_sha256": hashlib.sha256(config_bytes).hexdigest()}


def sign_activation(options: argparse.Namespace, binding: dict[str, object],
                    identities: dict[str, object], cases: dict[str, object], output: bytes) -> None:
    common.require(activation_binding(options, identities) == binding,
                   "target frontend or enrolled operator changed during proof")
    common.require(set(cases) == common.PROOF_CASES | EXTRA_CASES.keys(),
                   "activation requires all nine fresh containment cases")
    common.require(1 <= options.lifetime_seconds <= 900, "authority lifetime must be 1–900 seconds")
    keyfd = os.open(options.operator_key, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    payloadfd = os.memfd_create("doxa-plugin-authority", os.MFD_CLOEXEC)
    try:
        before = os.fstat(keyfd)
        common.require(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid()
                       and before.st_mode & 0o077 == 0 and before.st_nlink == 1 and before.st_size <= 4096,
                       "opened operator signing key is not private and bounded")
        status, public = common.run_bounded(
            ["/usr/bin/openssl", "pkey", "-in", f"/proc/self/fd/{keyfd}", "-pubout", "-outform", "DER"],
            cwd=ROOT, environment=os.environ.copy(), limit=1024, timeout=5, pass_fds=(keyfd,))
        common.require(status == 0 and public.startswith(ED25519_PUBLIC_DER_PREFIX)
                       and len(public) == len(ED25519_PUBLIC_DER_PREFIX) + 32
                       and public[-32:].hex() == binding["policy"]["operator_public_key"],
                       "operator signing key does not match explicit owner enrollment")
        cgroup = identities["cgroup"]
        issued_at = int(time.time())
        issued_boottime_ms = time.clock_gettime_ns(time.CLOCK_BOOTTIME) // 1_000_000
        payload = json.dumps({"format_version": 1, "purpose": AUTHORITY_PURPOSE,
            "epoch": binding["policy"]["epoch"], "issued_at": issued_at,
            "expires_at": issued_at + options.lifetime_seconds, "instance": binding["instance"],
            "issued_boottime_ms": issued_boottime_ms,
            "expires_boottime_ms": issued_boottime_ms + options.lifetime_seconds * 1000,
            "host": {"parent": cgroup["parent"], "parent_device": cgroup["parent_device"],
                     "parent_inode": cgroup["parent_inode"], "supervisor_device": cgroup["supervisor_device"],
                     "supervisor_inode": cgroup["supervisor_inode"],
                     **{role: identities[role] for role in ("frontend", "worker", "bwrap")}},
            "cases": sorted(cases), "proof_log_sha256": hashlib.sha256(output).hexdigest()},
            sort_keys=True, separators=(",", ":")).encode()
        os.write(payloadfd, payload)
        os.lseek(keyfd, 0, os.SEEK_SET)
        status, signature = common.run_bounded(
            ["/usr/bin/openssl", "pkeyutl", "-sign", "-rawin", "-inkey", f"/proc/self/fd/{keyfd}",
             "-in", f"/proc/self/fd/{payloadfd}"], cwd=ROOT, environment=os.environ.copy(),
            limit=128, timeout=5, pass_fds=(keyfd, payloadfd))
        common.require(status == 0 and len(signature) == 64 and stable_metadata(before)
                       == stable_metadata(os.fstat(keyfd)) == stable_metadata(options.operator_key.lstat()),
                       "operator signing failed or private key changed")
    finally:
        os.close(keyfd)
        os.close(payloadfd)
    common.require(activation_binding(options, identities) == binding,
                   "target frontend or operator enrollment changed before authority publication")
    directory = options.home / "native-plugin-authority"
    directory.mkdir(mode=0o700, exist_ok=True)
    dirfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    temporary = ".new-" + secrets.token_hex(12)
    instance = binding["instance"]
    filename = f"{instance['pid']}-{instance['start_ticks']}.json"
    try:
        meta = os.fstat(dirfd)
        common.require(meta.st_uid == os.geteuid() and meta.st_mode & 0o077 == 0,
                       "authority directory must be owner-private")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=dirfd)
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps({"payload_hex": payload.hex(), "signature_hex": signature.hex()},
                                    sort_keys=True, separators=(",", ":")).encode() + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(temporary, filename, src_dir_fd=dirfd, dst_dir_fd=dirfd)
        os.fsync(dirfd)
    finally:
        try:
            os.unlink(temporary, dir_fd=dirfd)
        except FileNotFoundError:
            pass
        os.close(dirfd)
    print(json.dumps({"status": "instance-authority-issued", "path": str(directory / filename),
                      "expires_at": issued_at + options.lifetime_seconds}, sort_keys=True), flush=True)


def trusted_identity(path: Path) -> dict[str, int | str]:
    common.require(path.is_absolute(), "installed executable path must be absolute")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        common.require(stat.S_ISREG(before.st_mode) and before.st_uid in (0, os.geteuid())
                       and before.st_mode & 0o022 == 0 and before.st_mode & 0o111
                       and before.st_nlink == 1 and before.st_size <= MAX_EXECUTABLE_BYTES,
                       f"{path}: installed executable is not trusted, singly linked and bounded")
        digest = common.descriptor_sha256(fd, str(path))
        after = os.fstat(fd)
        pathname = path.lstat()
        common.require(stable_metadata(before) == stable_metadata(after) == stable_metadata(pathname),
                       f"{path}: installed executable changed during observation")
        return {"device": before.st_dev, "inode": before.st_ino,
                "bytes": before.st_size, "sha256": digest}
    finally:
        os.close(fd)


def parse_host_report(output: bytes) -> dict[str, object]:
    common.require(len(output) <= 4096, "installed frontend host-check exceeded 4 KiB")
    try:
        lines = output.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise common.ProofError("installed frontend host-check is not UTF-8") from exc
    common.require(len(lines) == 7 and lines[0] == HOST_HEADER and lines[-1] == HOST_FOOTER,
                   "installed frontend host-check has an unexpected contract")
    parent = re.fullmatch(r"delegated_parent=(\S+) device=([0-9]+) inode=([0-9]+)", lines[1])
    supervisor = re.fullmatch(r"supervisor_device=([0-9]+) supervisor_inode=([0-9]+)", lines[2])
    common.require(parent is not None and supervisor is not None,
                   "installed frontend cgroup identity is malformed")
    observed: dict[str, object] = {"cgroup": {
        "parent": parent.group(1), "parent_device": int(parent.group(2)),
        "parent_inode": int(parent.group(3)), "supervisor_device": int(supervisor.group(1)),
        "supervisor_inode": int(supervisor.group(2)),
    }}
    for role, line in zip(("frontend", "worker", "bwrap"), lines[3:6]):
        row = re.fullmatch(rf"{role} device=([0-9]+) inode=([0-9]+) bytes=([0-9]+) sha256=([0-9a-f]{{64}})", line)
        common.require(row is not None, f"installed frontend {role} identity is malformed")
        observed[role] = {"device": int(row.group(1)), "inode": int(row.group(2)),
                          "bytes": int(row.group(3)), "sha256": row.group(4)}
    return observed


def frontend_host_check(frontend: Path) -> tuple[dict[str, object], bytes]:
    fd = os.open(frontend, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        status, output = common.run_bounded(
            [str(frontend), "native-plugin", "host-check"], cwd=ROOT,
            environment=os.environ.copy(), limit=4096, timeout=10,
            executable=f"/proc/self/fd/{fd}", pass_fds=(fd,))
    finally:
        os.close(fd)
    common.require(status == 0, "installed frontend host-check refused this host")
    return parse_host_report(output), output


def observe(frontend: Path) -> tuple[Path, Path, str, dict[str, object], bytes]:
    parent, scratch, bwrap_version = common.preflight()
    common.require_no_plugin_cgroups(parent)
    worker = frontend.with_name("doxa-plugin-worker")
    identities = {role: trusted_identity(path) for role, path in (
        ("frontend", frontend), ("worker", worker), ("bwrap", common.BWRAP))}
    common.require(len({(item["device"], item["inode"]) for item in identities.values()}) == 3,
                   "installed frontend, worker and Bubblewrap must be distinct inodes")
    cgroup = common.cgroup_identity(parent)
    reported, raw_report = frontend_host_check(frontend)
    for role, identity in identities.items():
        common.require(reported[role] == identity,
                       f"installed frontend {role} identity disagrees with opened artifact")
    common.require(all(reported["cgroup"][key] == cgroup[key] for key in reported["cgroup"]),
                   "installed frontend observed a different delegated cgroup")
    common.require_no_plugin_cgroups(parent)
    common.require(common.cgroup_identity(parent) == cgroup,
                   "delegated cgroup changed during installed observation")
    common.require(all(trusted_identity(path) == identities[role] for role, path in (
        ("frontend", frontend), ("worker", worker), ("bwrap", common.BWRAP))),
        "installed binary identity changed during host observation")
    return parent, scratch, bwrap_version, {"cgroup": cgroup, **identities}, raw_report


def installed_cases(output: bytes) -> dict[str, dict[str, str]]:
    common.require(len(output) <= common.PROOF_LOG_LIMIT, "installed proof output exceeded 128 KiB")
    try:
        lines = output.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise common.ProofError("installed proof output is not UTF-8") from exc
    extras: dict[str, dict[str, str]] = {}
    seven: list[str] = []
    for line in lines:
        marker = "plugin-acceptance case="
        if marker not in line:
            seven.append(line)
            continue
        prefix, _, suffix = line.partition(marker)
        name = suffix.partition(" ")[0]
        if name not in EXTRA_CASES:
            seven.append(line)
            continue
        common.require(not prefix or prefix.startswith(
            "test native_plugins::runner_sandbox::acceptance::installed_host_lifecycle_acceptance ... "),
            "installed proof error case has unexpected test-runner prefix")
        fields: dict[str, str] = {}
        for word in (marker + suffix).split():
            key, separator, value = word.partition("=")
            if key == "plugin-acceptance":
                continue
            common.require(separator == "=" and key not in fields and value,
                           "installed proof error case has malformed fields")
            fields[key] = value
        common.require(name not in extras and set(fields) == {"case", "outcome", "elapsed_ms", "cleanup"}
                       and fields["case"] == name and fields["outcome"] == EXTRA_CASES[name]
                       and fields["elapsed_ms"].isdecimal() and fields["cleanup"] == "removed",
                       "installed proof error case failed or duplicated")
        extras[name] = fields
    common.require(set(extras) == set(EXTRA_CASES), "installed proof omitted an error cleanup case")
    return {**common.proof_cases(("\n".join(seven) + "\n").encode()), **extras}


def expected_digests(options: argparse.Namespace, identities: dict[str, object]) -> None:
    for role in ("frontend", "worker", "bwrap"):
        expected = getattr(options, f"expected_{role}_sha256")
        common.require(expected is not None and re.fullmatch(r"[0-9a-f]{64}", expected) is not None,
                       f"--run requires --expected-{role}-sha256 from operator review")
        common.require(expected == identities[role]["sha256"],
                       f"installed {role} digest differs from operator-reviewed value")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="read-only installed-host prerequisites")
    group.add_argument("--run", action="store_true", help="opt-in installed-host containment proof")
    parser.add_argument("--frontend", type=Path, required=True, help="absolute installed doxa-rs path")
    parser.add_argument("--receipt", metavar="NAME", help="new private receipt name in TMPDIR")
    parser.add_argument("--activate", action="store_true", help="sign a separate permit after a fresh nine-case run")
    parser.add_argument("--tui-pid", type=int)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--operator-key", type=Path)
    parser.add_argument("--lifetime-seconds", type=int, default=900)
    for role in ("frontend", "worker", "bwrap"):
        parser.add_argument(f"--expected-{role}-sha256")
    options = parser.parse_args()
    common.require(options.frontend.is_absolute(), "--frontend must be absolute")
    common.require(not options.activate or 1 <= options.lifetime_seconds <= 900,
                   "authority lifetime must be 1–900 seconds")
    common.require(options.run or not options.activate, "--activate requires a fresh --run; receipts cannot activate")
    common.require(options.activate or (options.tui_pid is None and options.home is None and options.operator_key is None),
                   "instance and operator key options require explicit --activate")
    if options.run:
        common.require(os.environ.get("DOXA_PLUGIN_INSTALLED_HOST_ACCEPTANCE") == "1"
                       and os.environ.get("DOXA_PLUGIN_OPERATOR_GO") == "1",
                       "--run requires DOXA_PLUGIN_INSTALLED_HOST_ACCEPTANCE=1 and DOXA_PLUGIN_OPERATOR_GO=1")
        target = common.check_target_dir(os.environ.get("CARGO_TARGET_DIR", ""))
        common.receipt_name(options.receipt, Path("/"))  # validate syntax before host observation
        for role in ("frontend", "worker", "bwrap"):
            common.require(re.fullmatch(r"[0-9a-f]{64}", getattr(options, f"expected_{role}_sha256") or "") is not None,
                           f"--run requires --expected-{role}-sha256 from operator review")
    else:
        common.require(options.receipt is None and all(getattr(options, f"expected_{role}_sha256") is None
                       for role in ("frontend", "worker", "bwrap")),
                       "--check records observations only; receipt and expected digests belong to --run")
    parent, scratch, version, identities, report = observe(options.frontend)
    print(json.dumps({"status": "prerequisites-observed", "tui_execution_authorized": False,
                      "installed": identities, "bwrap_version": version}, sort_keys=True), flush=True)
    if options.check:
        return 0
    expected_digests(options, identities)
    binding = activation_binding(options, identities) if options.activate else None
    receipt = common.receipt_name(options.receipt, scratch)
    common.require(not receipt.exists(), "installed proof receipt already exists")
    source = common.source_identity(ROOT)
    environment = os.environ.copy()
    environment["RUST_TEST_THREADS"] = "1"
    environment["CARGO_TERM_COLOR"] = "never"
    environment["DOXA_PLUGIN_CGROUP_ACCEPTANCE"] = "1"
    environment["DOXA_PLUGIN_INSTALLED_ACCEPTANCE"] = "1"
    environment["DOXA_PLUGIN_ACCEPTANCE_PARENT"] = str(parent)
    environment["DOXA_PLUGIN_ACCEPTANCE_PARENT_ID"] = (
        f"{identities['cgroup']['parent_device']}:{identities['cgroup']['parent_inode']}")
    environment["DOXA_PLUGIN_ACCEPTANCE_WORKER"] = str(options.frontend.with_name("doxa-plugin-worker"))
    build = common.build_identity(ROOT, environment)
    compile_status, compile_output = common.run_bounded(
        ["cargo", "test", "--locked", "-p", "doxa-tui", "--lib", "--no-run", "--message-format=json"],
        cwd=ROOT, environment=environment, limit=common.BUILD_LOG_LIMIT,
        timeout=common.BUILD_TIMEOUT_SECONDS)
    common.require(compile_status == 0, "Cargo failed to build installed-host proof executable")
    artifact = common.test_artifact(compile_output, target)
    proof_pid: int | None = None
    def remember_pid(pid: int) -> None:
        nonlocal proof_pid
        proof_pid = pid
    with os.fdopen(os.open(artifact, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC), "rb") as test_file:
        metadata = os.fstat(test_file.fileno())
        common.require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.geteuid()
                       and metadata.st_mode & 0o022 == 0 and metadata.st_mode & 0o111,
                       "installed proof executable is not owner-controlled and executable")
        test_sha256 = common.descriptor_sha256(test_file.fileno(), str(artifact))
        try:
            status, output = common.run_bounded(
                [str(artifact), "installed_host_lifecycle_acceptance", "--ignored", "--nocapture", "--test-threads=1"],
                cwd=ROOT, environment=environment, limit=common.PROOF_LOG_LIMIT,
                timeout=common.PROOF_TIMEOUT_SECONDS, executable=f"/proc/self/fd/{test_file.fileno()}",
                pass_fds=(test_file.fileno(),), process_started=remember_pid)
            sys.stdout.buffer.write(output)
            sys.stdout.flush()
            common.require(status == 0, "installed proof test failed")
            cases = installed_cases(output)
        except BaseException:
            if proof_pid is not None:
                common.stop_plugin_cgroups(parent, f"doxa-plugin-{proof_pid}-")
            raise
    common.require_no_plugin_cgroups(parent)
    common.require(common.cgroup_identity(parent) == identities["cgroup"],
                   "delegated cgroup changed during installed proof")
    common.require(all(trusted_identity(path) == identities[role] for role, path in (
        ("frontend", options.frontend), ("worker", options.frontend.with_name("doxa-plugin-worker")),
        ("bwrap", common.BWRAP))), "installed binary changed during proof")
    common.require(common.source_identity(ROOT) == source and common.build_identity(ROOT, environment) == build,
                   "proof source or build identity changed")
    common.require(common.file_sha256(artifact) == test_sha256,
                   "installed proof executable changed after run")
    common.write_receipt(receipt, {
        "format_version": 1, "purpose": "installed-host review only; no TUI execution authority",
        "tui_execution_authorized": False, "recorded_at": datetime.now(timezone.utc).isoformat(),
        "source": source, "installed": identities, "bwrap_version": version,
        "kernel": platform.release(), "uid": os.geteuid(), "build": build,
        "test_executable_sha256": test_sha256, "host_check_sha256": hashlib.sha256(report).hexdigest(),
        "proof_log_sha256": hashlib.sha256(output).hexdigest(), "cases": cases,
    })
    if options.activate:
        sign_activation(options, binding, identities, cases, output)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, common.ProofError) as exc:
        print(f"installed-plugin-proof unavailable: {exc}", file=sys.stderr)
        sys.exit(2)
