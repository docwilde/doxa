#!/usr/bin/env python3
"""Build DOXA's pinned fail-closed Codex app server without replacing codex."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import selectors
import time
import subprocess
import tempfile
import platform
import urllib.request

SOURCE = "b412ff32c417f855c2b2d1581b77058eed87c84b"
CONTRACT = "doxa-precompact-fail-closed-v1"
PROVIDER = "codex-0.156.1-precompact-v1"
PATCH = Path(__file__).resolve().parent / "codex-protected/precompact.patch"
PATCH_SHA256 = "96a3c37b55f2bade5dc72f3ab344d30ed25c39c8fb42b42cfe5774fb7b210e6b"
AGENT_PREFIX = "doxa_codex_rs/0.156.1 (doxa-precompact-fail-closed-v1; "


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def private_directory(path):
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("provider directories must be absolute and not symlinks")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise ValueError(f"provider directory must be private and owned: {path}")


def run(arguments, **kwargs):
    subprocess.run(arguments, check=True, **kwargs)


def prepare_source(cache):
    if digest(PATCH) != PATCH_SHA256:
        raise ValueError("provider patch differs from its reviewed checksum")
    source = cache / "source"
    if not source.exists():
        run(["git", "clone", "--depth", "1", "--branch", "rust-v0.156.1",
             "https://github.com/openai/codex.git", str(source)])
    actual = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if actual != SOURCE:
        raise ValueError("provider source is not the pinned official commit")
    reverse = subprocess.run(["git", "-C", str(source), "apply", "--reverse", "--check", str(PATCH)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if reverse.returncode:
        if subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"]):
            raise ValueError("provider source has unrelated changes")
        run(["git", "-C", str(source), "apply", str(PATCH)])
        run(["git", "-C", str(source), "add", "-N", "codex-rs/core/src/doxa_precompact.rs"])
    actual_patch = subprocess.check_output(["git", "-C", str(source), "diff", "--binary"])
    if actual_patch != PATCH.read_bytes():
        raise ValueError("provider source changes do not match the reviewed patch")
    untracked = subprocess.check_output(["git", "-C", str(source), "ls-files", "--others", "--exclude-standard"])
    if untracked:
        raise ValueError("provider source has untracked files")
    return source


RUSTUP_URL = "https://static.rust-lang.org/rustup/archive/1.29.1/x86_64-unknown-linux-gnu/rustup-init"
RUSTUP_SHA256 = "dda7234360b7f578ca8b0ddcb80145646fa61a67c1720a5abc7051b35c9fcb71"


def toolchain(cache, cargo):
    if cargo:
        return cargo, {}
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("automatic private toolchain bootstrap supports Linux x86_64; pass --cargo for Rust 1.95.0")
    private_directory(cache / "toolchain")
    private_directory(cache / "cargo")
    environment = dict(RUSTUP_HOME=str(cache / "toolchain"), CARGO_HOME=str(cache / "cargo"),
                       RUSTUP_TOOLCHAIN="1.95.0")
    executable = cache / "cargo/bin/cargo"
    rustc = cache / "toolchain/toolchains/1.95.0-x86_64-unknown-linux-gnu/bin/rustc"
    if not executable.exists() or not rustc.exists():
        bootstrap = cache / "rustup-init"
        if not bootstrap.exists() or digest(bootstrap) != RUSTUP_SHA256:
            with urllib.request.urlopen(RUSTUP_URL, timeout=60) as response:
                with bootstrap.open("wb") as output:
                    shutil.copyfileobj(response, output)
            bootstrap.chmod(0o700)
        if digest(bootstrap) != RUSTUP_SHA256:
            raise ValueError("private Rust bootstrap differs from its pinned checksum")
        print("doxa-codex-install: installing isolated Rust 1.95.0 (no global toolchain changes)", flush=True)
        run([str(bootstrap), "-y", "--no-modify-path", "--profile", "minimal", "--default-toolchain", "1.95.0"],
            env=dict(os.environ, **environment))
    return str(executable), environment


def build(cache, cargo):
    source = prepare_source(cache)
    binary = cache / "target/dev-small/codex-app-server"
    fingerprint = cache / "build.json"
    identity = {"source_commit": SOURCE, "patch_sha256": PATCH_SHA256, "profile": "dev-small", "toolchain": "1.95.0"}
    if binary.is_file() and fingerprint.is_file():
        prior = json.loads(fingerprint.read_text())
        if prior == dict(identity, binary_sha256=digest(binary)):
            probe(binary)
            print("doxa-codex-install: reusing verified private app-server artifact", flush=True)
            return binary
    cargo, toolchain_environment = toolchain(cache, cargo)
    environment = dict(os.environ, **toolchain_environment, CARGO_TARGET_DIR=str(cache / "target"), CARGO_BUILD_JOBS="1",
                       RUST_TEST_THREADS="1", TMPDIR=str(cache / "scratch"), CARGO_HTTP_MULTIPLEXING="false")
    private_directory(cache / "scratch")
    command = [cargo, "build", "--locked", "--profile", "dev-small", "-p", "codex-app-server",
               "--bin", "codex-app-server", "-j", "1"]
    if shutil.which("systemd-run") and subprocess.run(["systemctl", "--user", "show-environment"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        command = ["systemd-run", "--user", "--scope", "-q", "-p", "MemoryMax=12G", "-p", "MemorySwapMax=0"] + command
    else:
        raise ValueError("a user systemd scope is required for the bounded provider build")
    run(command, cwd=source / "codex-rs", env=environment)
    probe(binary)
    fingerprint.write_text(json.dumps(dict(identity, binary_sha256=digest(binary)), sort_keys=True) + "\n")
    fingerprint.chmod(0o600)
    return binary


def install(binary, root, official_cli, launcher):
    probe(binary)
    private_directory(root)
    destination = root / PROVIDER
    if destination.exists():
        private_directory(destination)
    # Build in a sibling stage. An interrupted copy leaves the current provider intact.
    stage = Path(tempfile.mkdtemp(prefix=".codex-stage-", dir=root))
    try:
        shutil.copyfile(binary, stage / "codex-app-server")
        (stage / "codex-app-server").chmod(0o700)
        shutil.copyfile(launcher, stage / "codex")
        (stage / "codex").chmod(0o700)
        receipt = {"contract": CONTRACT, "source_commit": SOURCE, "patch_sha256": digest(PATCH),
                   "binary_sha256": digest(stage / "codex-app-server"), "official_cli": str(official_cli),
                   "profile": "dev-small", "toolchain": "1.95.0"}
        (stage / "receipt.json").write_text(json.dumps(receipt, sort_keys=True) + "\n")
        (stage / "receipt.json").chmod(0o600)
        # Existing installations are identical or retained until an explicit upgrade.
        if destination.exists():
            previous = json.loads((destination / "receipt.json").read_text())
            identity_keys = ("contract", "source_commit", "patch_sha256", "binary_sha256", "profile", "toolchain")
            if any(previous.get(key) != receipt[key] for key in identity_keys):
                raise ValueError("provider artifact differs from installed receipt; use a new --install-root for review")
            # Refresh the native dispatcher and official CLI path on DOXA upgrades.
            # The reviewed provider identity is unchanged; each file swap is atomic.
            os.replace(stage / "codex", destination / "codex")
            os.replace(stage / "receipt.json", destination / "receipt.json")
        else:
            stage.rename(destination)
        print(destination / "codex")
        print(json.dumps(receipt, sort_keys=True))
        return destination / "codex"
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def probe(binary):
    """Verify the compiled initialize identity; never start a thread or a turn."""
    request = json.dumps({"id": 1, "method": "initialize", "params": {
        "clientInfo": {"name": "doxa_install", "version": "1"},
        "capabilities": {"experimentalApi": True}}}) + "\n"
    process = subprocess.Popen([str(binary), "--listen", "stdio://"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    try:
        process.stdin.write(request)
        process.stdin.flush()
        pending = bytearray()
        total = 0
        deadline = time.monotonic() + 20
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("compiled provider initialize probe timed out")
                if not selector.select(remaining):
                    raise ValueError("compiled provider initialize probe timed out")
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise ValueError("compiled provider ended before initialize attestation")
                total += len(chunk)
                if total > 1024 * 1024:
                    raise ValueError("compiled provider initialize output exceeded its bound")
                pending.extend(chunk)
                while b"\n" in pending:
                    line, _, rest = pending.partition(b"\n")
                    pending = bytearray(rest)
                    try:
                        frame = json.loads(line)
                    except (ValueError, UnicodeError):
                        raise ValueError("compiled provider initialize output was invalid") from None
                    if not isinstance(frame, dict) or frame.get("id") != 1:
                        continue
                    result = frame.get("result")
                    agent = result.get("userAgent") if isinstance(result, dict) else None
                    if not isinstance(agent, str) or not agent.startswith(AGENT_PREFIX):
                        raise ValueError("built artifact did not attest the private compiled compaction contract")
                    return
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=Path.home() / ".cache/doxa/codex-protected")
    parser.add_argument("--install-root", type=Path, default=Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "doxa/providers")
    parser.add_argument("--official-cli", type=Path)
    parser.add_argument("--cargo", help="explicit Rust 1.95.0 cargo; default bootstraps a private pinned toolchain")
    parser.add_argument("--launcher", type=Path, required=True, help="native doxa-codex-protected binary built from this DOXA checkout")
    options = parser.parse_args()
    private_directory(options.cache)
    official = options.official_cli or (Path(found) if (found := shutil.which("codex")) else None)
    if official is None or not official.is_absolute() or not os.access(official, os.X_OK):
        parser.error("an installed official Codex CLI is required; pass --official-cli")
    official = official.resolve()
    binary = build(options.cache, options.cargo)
    if not options.launcher.is_absolute() or not os.access(options.launcher, os.X_OK):
        parser.error("--launcher must be an absolute native launcher executable")
    install(binary, options.install_root, official, options.launcher)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"doxa-codex-install: {error}")
