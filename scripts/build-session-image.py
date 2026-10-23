#!/usr/bin/env python3
"""Build the reviewed session image from an explicit, credential-free context.

The Debian/Ubuntu base is pinned by registry digest; apt inputs are fixed to a
reviewed dated snapshot and direct package manifest. The resulting local
content ID is the runtime pin. Provider packages keep their verified receipts.
No host home, login file or host configuration is copied.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

REPO = Path(__file__).resolve().parents[1]
CODEX_FILES = {
    "codex-app-server": "binary_sha256",
    "codex-code-mode-host-payload": "code_mode_host_sha256",
    "codex-code-mode-host": "code_mode_host_dispatcher_sha256",
    "codex": "code_mode_host_dispatcher_sha256",
}
CONTRACT = "doxa-precompact-fail-closed-v1"
SOURCE = "6aaac694803004a63c6f393d153c96d22521a6b9"
PATCH = "516e4acc50647d5061a099a1a1900e802555e15c1892270332922eccb05109a0"
MAX_BINARY = 1024 * 1024 * 1024
APT_SNAPSHOT = "20260901T000000Z"
APT_PACKAGES = {"bash", "ca-certificates", "git", "python3"}
APT_FAMILIES = {"debian-bookworm", "ubuntu-noble"}
APT_ARCHITECTURES = ("amd64", "arm64")


def owned_file(path: Path, maximum: int, *, private: bool = False,
               single_link: bool = True) -> bytes:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        import stat
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or (single_link and before.st_nlink != 1) or before.st_size > maximum
                or (private and before.st_mode & 0o077)):
            raise ValueError(f"unsafe provider artifact: {path.name}")
        data = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if len(data) > maximum or identity(before) != identity(after):
            raise ValueError(f"artifact changed while reading: {path.name}")
        return data


def copy_executable(source: Path, destination: Path) -> str:
    source = source.resolve(strict=True)
    if not os.access(source, os.X_OK):
        raise ValueError(f"not executable: {source.name}")
    # Cargo exposes the explicitly selected worker via a hardlink to deps/.
    # Copy stable bytes into the context; protected package artifacts below
    # still require private, single-link files and their receipt hashes.
    data = owned_file(source, MAX_BINARY, single_link=False)
    destination.write_bytes(data)
    destination.chmod(0o755)
    return hashlib.sha256(data).hexdigest()


def copy_codex_package(source: Path, destination: Path) -> dict:
    source = source.resolve(strict=True)
    metadata = source.stat()
    if not source.is_dir() or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise ValueError("Codex provider directory must be private and owned")
    raw = owned_file(source / "receipt.json", 16384, private=True)
    receipt = json.loads(raw)
    if (receipt.get("contract") != CONTRACT or receipt.get("source_commit") != SOURCE
            or receipt.get("patch_sha256") != PATCH
            or receipt.get("code_mode_host_source_commit") != SOURCE):
        raise ValueError("Codex receipt does not match the reviewed provider contract")
    for name, key in CODEX_FILES.items():
        data = owned_file(source / name, MAX_BINARY, private=True)
        if hashlib.sha256(data).hexdigest() != receipt.get(key):
            raise ValueError(f"Codex artifact differs from receipt: {name}")
        (destination / name).write_bytes(data)
        (destination / name).chmod(0o700)
    (destination / "receipt.json").write_bytes(raw)
    (destination / "receipt.json").chmod(0o600)
    return {"source_commit": SOURCE, "receipt_sha256": hashlib.sha256(raw).hexdigest()}


def copy_apt_inputs(context: Path, base_family: str = "debian-bookworm") -> dict:
    if base_family not in APT_FAMILIES:
        raise ValueError("unsupported apt base family")
    source_path = REPO / f"containers/session/apt/{base_family}.sources"
    packages_path = REPO / f"containers/session/apt/{base_family}.packages"
    sources = source_path.read_bytes()
    packages = packages_path.read_bytes()
    source_text = sources.decode("ascii")
    if base_family == "debian-bookworm":
        snapshot_pattern = r"snapshot\.debian\.org/archive/(?:debian|debian-security)/(\d{8}T\d{6}Z)"
    else:
        snapshot_pattern = r"snapshot\.ubuntu\.com/ubuntu/(\d{8}T\d{6}Z)"
    snapshots = re.findall(snapshot_pattern, source_text)
    if len(snapshots) != 3 or set(snapshots) != {APT_SNAPSHOT}:
        raise ValueError(f"{base_family} apt sources do not match the reviewed snapshot")
    package_rows = packages.decode("ascii").splitlines()
    parsed = []
    for row in package_rows:
        match = re.fullmatch(r"([a-z0-9][a-z0-9+.-]*)=([A-Za-z0-9:~.+-]+)", row)
        if not match:
            raise ValueError(f"{base_family} apt lock contains a malformed package pin")
        parsed.append(match.group(1))
    if len(parsed) != len(set(parsed)) or set(parsed) != APT_PACKAGES:
        raise ValueError(f"{base_family} apt lock must pin the reviewed direct package set exactly")
    shutil.copytree(REPO / "containers/session/apt", context / "apt")
    return {
        "apt_family": base_family,
        "apt_snapshot": APT_SNAPSHOT,
        "apt_supported_architectures": list(APT_ARCHITECTURES),
        "apt_sources_sha256": hashlib.sha256(sources).hexdigest(),
        "apt_packages_sha256": hashlib.sha256(packages).hexdigest(),
        "dockerfile_sha256": hashlib.sha256((REPO / "containers/session/Dockerfile").read_bytes()).hexdigest(),
    }


def populate_context(context: Path, worker: Path, claude: Path | None,
                     codex: Path | None, base_family: str = "debian-bookworm") -> dict:
    providers = context / "providers"
    providers.mkdir(mode=0o700)
    package = context / "codex-provider"
    package.mkdir(mode=0o700)
    manifest = {"worker_sha256": copy_executable(worker, context / "worker")}
    if claude is not None:
        manifest["claude_sha256"] = copy_executable(claude, providers / "claude")
    if codex is not None:
        manifest["codex"] = copy_codex_package(codex, package)
    shutil.copyfile(REPO / "containers/session/Dockerfile", context / "Dockerfile")
    manifest.update(copy_apt_inputs(context, base_family))
    (context / "build-manifest.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
    return manifest


def run(command: list[str]) -> str:
    return subprocess.run(command, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-image", required=True, help="reviewed Debian/Ubuntu image NAME@sha256:DIGEST")
    parser.add_argument("--base-family", required=True, choices=sorted(APT_FAMILIES),
                        help="must match the digest-pinned image's Debian/Ubuntu release")
    parser.add_argument("--worker", required=True, type=Path)
    parser.add_argument("--claude-bin", type=Path)
    parser.add_argument("--codex-provider", type=Path, help="installed protected provider directory; credentials are never copied")
    parser.add_argument("--tag", default="doxa-session:local")
    parser.add_argument("--docker-host", default=os.environ.get("DOXA_DOCKER_HOST", os.environ.get("DOCKER_HOST", f"unix:///run/user/{os.getuid()}/docker.sock")))
    parser.add_argument("--cache", type=Path, default=REPO / "target/isolation-image")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[0-9a-f]{64}", args.base_image):
        parser.error("--base-image must use an exact sha256 registry digest")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./:_-]*", args.tag):
        parser.error("invalid image tag")
    if (not args.docker_host.startswith("unix:///")
            or args.docker_host in {"unix:///var/run/docker.sock", "unix:///run/docker.sock"}):
        parser.error("only a local rootless Docker Unix socket is supported")
    docker = ["docker", "--host", args.docker_host]
    security = json.loads(run(docker + ["info", "--format", "{{json .SecurityOptions}}"] ))
    if "name=rootless" not in security:
        parser.error("Docker Engine must report rootless mode")
    args.cache.mkdir(parents=True, exist_ok=True)
    args.cache.chmod(0o700)
    with tempfile.TemporaryDirectory(prefix="context-", dir=args.cache) as directory:
        context = Path(directory)
        manifest = populate_context(context, args.worker, args.claude_bin, args.codex_provider, args.base_family)
        subprocess.run(docker + ["build", "--build-arg", f"BASE_IMAGE={args.base_image}",
                                "--build-arg", f"BASE_FAMILY={args.base_family}",
                                "--tag", args.tag, str(context)], check=True)
        image = run(docker + ["image", "inspect", args.tag, "--format", "{{.Id}}"])
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise ValueError("Docker did not return an exact image content ID")
        manifest.update({"base_image": args.base_image, "image": image})
        record = args.cache / "last-build.json"
        record.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        record.chmod(0o600)
        print(f"DOXA_DOCKER_IMAGE={image}")
        print(f"Build evidence: {record}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"doxa-session-image: {error}") from error
