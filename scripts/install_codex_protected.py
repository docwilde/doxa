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
import stat
import fcntl
import tomllib
import re
from contextlib import contextmanager

SOURCE = "b412ff32c417f855c2b2d1581b77058eed87c84b"
CONTRACT = "doxa-precompact-fail-closed-v1"
PROVIDER = "codex-0.156.1-precompact-v1"
PATCH = Path(__file__).resolve().parent / "codex-protected/precompact.patch"
PATCH_SHA256 = "23bccbb08344fc70d1fd8a482b19814ca7f8edff07d478091b8c703e37a7ec6d"
LEGACY_PATCH_SHA256 = "d6c8a41c0370c12dcace10d6babe13de7852f0095fed7b46289b38e7a6cd0f4b"
AGENT_PREFIX = "doxa_codex_rs/0.156.1 (doxa-precompact-fail-closed-v1; doxa-midturn-auto-v1; "


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


def private_read(path, limit, *, hash_only=False, cargo_output=False):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        # Cargo outputs can be 0755 or hard-linked inside the owned private
        # build cache. Verify their bytes against private build fingerprints;
        # copied installed artifacts and receipt files still require 0700/0600
        # and a single link to their immutable inode.
        forbidden = 0o022 if cargo_output else 0o077
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_mode & forbidden or (not cargo_output and metadata.st_nlink != 1)
                or metadata.st_size > limit
                or (cargo_output and not metadata.st_mode & stat.S_IXUSR)):
            kind = "cached Cargo output" if cargo_output else "installed provider file"
            raise ValueError(f"{kind} is not owned, regular and bounded with safe permissions")
        if hash_only:
            return hashlib.file_digest(stream, "sha256").hexdigest()
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("installed provider file exceeded its bound")
        return data


@contextmanager
def locked_directory(directory):
    private_directory(directory)
    descriptor = os.open(directory / ".install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077 or metadata.st_nlink != 1):
            raise ValueError("provider installer lock is not private and regular")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def run(arguments, **kwargs):
    # Keep newly built artifacts private even under a group-writable shell umask.
    kwargs.setdefault("umask", 0o077)
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
    previous_patch = subprocess.check_output(["git", "-C", str(source), "diff", "HEAD", "--binary"])
    if hashlib.sha256(previous_patch).hexdigest() == LEGACY_PATCH_SHA256:
        if subprocess.check_output(["git", "-C", str(source), "ls-files", "--others", "--exclude-standard"]):
            raise ValueError("provider source has untracked files")
        # Preserve the exact reviewed legacy checkout, its index and artifacts.
        # A patch-specific worktree lets the default cache upgrade without
        # resetting source files or accepting arbitrary local edits.
        updated_source = cache / ("source-" + PATCH_SHA256)
        if not updated_source.exists():
            run(["git", "-C", str(source), "worktree", "add", "--detach", str(updated_source), SOURCE])
        source = updated_source
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
    # Include staged changes as well as the worktree. Intent-to-add retains the
    # new gate in this complete HEAD comparison; unrelated staged code fails.
    actual_patch = subprocess.check_output(["git", "-C", str(source), "diff", "HEAD", "--binary"])
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
        # Preserve rustup proxy basename: resolving cargo's symlink to rustup
        # changes argv[0] dispatch and loses the sibling rustc proxy.
        executable = Path(cargo).absolute()
        rustc = executable.parent / "rustc"
        environment = {"RUSTC": str(rustc), "RUSTUP_TOOLCHAIN": "1.95.0"}
        for binary, prefix in [(executable, "cargo 1.95.0 "), (rustc, "rustc 1.95.0 ")]:
            version = subprocess.check_output([str(binary), "--version"], text=True, env=build_environment(environment), timeout=10)
            if not version.startswith(prefix):
                raise ValueError("--cargo and its sibling rustc must both be Rust 1.95.0")
        return str(executable), environment
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
    environment["RUSTC"] = str(rustc)
    for binary, prefix in [(executable, "cargo 1.95.0 "), (rustc, "rustc 1.95.0 ")]:
        version = subprocess.check_output([str(binary), "--version"], text=True, env=build_environment(environment), timeout=10)
        if not version.startswith(prefix):
            raise ValueError("private provider toolchain is not Rust 1.95.0")
    return str(executable), environment


def build_environment(overrides):
    environment = dict(os.environ)
    for key in list(environment):
        if key.startswith(("RUSTY_V8_", "V8_", "CARGO_PROFILE_")):
            environment.pop(key)
    for key in ("RUSTC", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS",
                "RUSTUP_TOOLCHAIN", "RUSTDOC", "RUSTDOCFLAGS", "DOCS_RS", "DENO_TRYBUILD", "CARGO_BUILD_TARGET"):
        environment.pop(key, None)
    environment.update(overrides)
    return environment


def verified_download(path, url, expected_digest, limit):
    if path.exists() and private_read(path, limit, hash_only=True) == expected_digest:
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".v8-download-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            hasher = hashlib.sha256()
            total = 0
            with urllib.request.urlopen(url, timeout=60) as response:
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > limit:
                        raise ValueError("official V8 input exceeded its download bound")
                    stream.write(chunk)
                    hasher.update(chunk)
            if hasher.hexdigest() != expected_digest:
                raise ValueError("official V8 input differs from its pinned checksum")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def v8_inputs(cache, source, target):
    """Trust manifests from the already verified official source, never mirror/env overrides."""
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", target):
        raise ValueError("invalid helper target")
    lock = tomllib.loads((source / "codex-rs/Cargo.lock").read_text())
    versions = {package["version"] for package in lock["package"] if package["name"] == "v8"}
    if versions != {"150.4.0"}:
        raise ValueError("reviewed source no longer has the expected locked V8 version")
    version = "150.4.0"
    manifest_name = f"rusty_v8_ptrcomp_sandbox_release_{target}.sha256"
    trusted = source / "third_party/v8/rusty_v8_150_4_0_release_manifests.sha256"
    manifest_pins = dict((name, checksum) for checksum, name in (line.split() for line in trusted.read_text().splitlines()))
    if manifest_name not in manifest_pins:
        raise ValueError("the pinned source has no approved sandbox V8 inputs for this target")
    directory = cache / "v8" / target
    private_directory(directory)
    base = f"https://github.com/openai/codex/releases/download/rusty-v8-v{version}"
    manifest = directory / manifest_name
    verified_download(manifest, f"{base}/{manifest_name}", manifest_pins[manifest_name], 65536)
    suffix = ".lib.gz" if "windows" in target else ".a.gz"
    archive_name = ("rusty_v8_" if "windows" in target else "librusty_v8_") + f"ptrcomp_sandbox_release_{target}{suffix}"
    binding_name = f"src_binding_ptrcomp_sandbox_release_{target}.rs"
    pins = dict((name, checksum) for checksum, name in (line.split() for line in private_read(manifest, 65536).decode().splitlines()))
    if set(pins) != {archive_name, binding_name} or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in pins.values()):
        raise ValueError("approved V8 manifest does not bind the exact archive/binding pair")
    archive, binding = directory / archive_name, directory / binding_name
    verified_download(archive, f"{base}/{archive_name}", pins[archive_name], 256 * 1024 * 1024)
    verified_download(binding, f"{base}/{binding_name}", pins[binding_name], 1024 * 1024)
    identity = {"version": version, "target": target, "manifest_sha256": manifest_pins[manifest_name],
                "archive_sha256": pins[archive_name], "binding_sha256": pins[binding_name]}
    return {"RUSTY_V8_ARCHIVE": str(archive), "RUSTY_V8_SRC_BINDING_PATH": str(binding)}, identity


def build(cache, cargo):
    source = prepare_source(cache)
    binary = cache / "target/dev-small/codex-app-server"
    fingerprint = cache / "build.json"
    identity = {"source_commit": SOURCE, "patch_sha256": PATCH_SHA256, "profile": "dev-small", "toolchain": "1.95.0"}
    if binary.is_file() and fingerprint.is_file():
        prior = json.loads(private_read(fingerprint, 16384))
        if prior == dict(identity, binary_sha256=digest(binary)):
            probe(binary)
            print("doxa-codex-install: reusing verified private app-server artifact", flush=True)
            return binary
    cargo, toolchain_environment = toolchain(cache, cargo)
    environment = build_environment(dict(toolchain_environment, CARGO_TARGET_DIR=str(cache / "target"), CARGO_BUILD_JOBS="1",
                       RUST_TEST_THREADS="1", TMPDIR=str(cache / "scratch"), CARGO_HTTP_MULTIPLEXING="false"))
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


def build_code_mode_host(cache, cargo):
    """Build the required helper from the same reviewed source; no prebuilt shortcut."""
    source = prepare_source(cache)
    binary = cache / "target/dev-small/codex-code-mode-host"
    fingerprint = cache / "code-mode-host-build.json"
    cargo, toolchain_environment = toolchain(cache, cargo)
    target = subprocess.check_output([toolchain_environment["RUSTC"], "--print", "host-tuple"],
        text=True, env=build_environment(toolchain_environment), timeout=10).strip()
    native_environment, native_identity = v8_inputs(cache, source, target)
    identity = {"source_commit": SOURCE, "patch_sha256": PATCH_SHA256,
                "profile": "dev-small", "toolchain": "1.95.0", "product": "codex-code-mode-host", "v8_inputs": native_identity}
    if binary.is_file() and fingerprint.is_file():
        previous = json.loads(private_read(fingerprint, 16384))
        if previous == dict(identity, binary_sha256=digest(binary)):
            print("doxa-codex-install: reusing verified code-mode host artifact", flush=True)
            return binary, identity
    environment = build_environment(dict(toolchain_environment, **native_environment, CARGO_TARGET_DIR=str(cache / "target"), CARGO_BUILD_JOBS="1",
                       RUST_TEST_THREADS="1", TMPDIR=str(cache / "scratch"), CARGO_HTTP_MULTIPLEXING="false"))
    private_directory(cache / "scratch")
    if not shutil.which("systemd-run") or subprocess.run(["systemctl", "--user", "show-environment"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        raise ValueError("a user systemd scope is required for the bounded code-mode host build")
    command = ["systemd-run", "--user", "--scope", "-q", "-p", "MemoryMax=12G", "-p", "MemorySwapMax=0",
               cargo, "build", "--locked", "--profile", "dev-small", "-p", "codex-code-mode-host",
               "--bin", "codex-code-mode-host", "-j", "1"]
    run(command, cwd=source / "codex-rs", env=environment)
    fingerprint.write_text(json.dumps(dict(identity, binary_sha256=digest(binary)), sort_keys=True) + "\n")
    fingerprint.chmod(0o600)
    return binary, identity


def verified_artifacts(cache, binary, code_mode_host, helper_identity):
    """Bind incoming bytes to both independently checked build fingerprints."""
    identity = {"source_commit": SOURCE, "patch_sha256": PATCH_SHA256,
                "profile": "dev-small", "toolchain": "1.95.0"}
    server = dict(identity, binary_sha256=private_read(binary, 1024 * 1024 * 1024, hash_only=True, cargo_output=True))
    if (any(helper_identity.get(key) != value for key, value in identity.items())
            or helper_identity.get("product") != "codex-code-mode-host"):
        raise ValueError("code-mode helper build identity differs from reviewed source")
    helper = dict(helper_identity, binary_sha256=private_read(code_mode_host, 1024 * 1024 * 1024, hash_only=True, cargo_output=True))
    if (json.loads(private_read(cache / "build.json", 16384)) != server
            or json.loads(private_read(cache / "code-mode-host-build.json", 16384)) != helper):
        raise ValueError("provider artifacts differ from verified build fingerprints")
    return {"binary_sha256": server["binary_sha256"],
            "code_mode_host_sha256": helper["binary_sha256"], **identity,
            "code_mode_host_v8_inputs": helper_identity["v8_inputs"]}


def verify_installed(destination, previous):
    """Never repair or replace an installation whose own receipt does not match."""
    expected = {"contract": CONTRACT, "source_commit": SOURCE,
                "profile": "dev-small", "toolchain": "1.95.0"}
    if (any(previous.get(key) != value for key, value in expected.items())
            or previous.get("patch_sha256") not in {PATCH_SHA256, LEGACY_PATCH_SHA256}):
        raise ValueError("installed provider provenance differs from reviewed source")
    helper_keys = ("code_mode_host_sha256", "code_mode_host_source_commit", "code_mode_host_dispatcher_sha256")
    if any(key in previous for key in helper_keys) and not all(key in previous for key in helper_keys):
        raise ValueError("partial code-mode helper provenance differs from installed receipt")
    files = [("codex-app-server", "binary_sha256")]
    if all(key in previous for key in helper_keys):
        if previous["code_mode_host_source_commit"] != SOURCE:
            raise ValueError("code-mode helper source differs from reviewed source")
        files += [("codex-code-mode-host-payload", "code_mode_host_sha256"),
                  ("codex-code-mode-host", "code_mode_host_dispatcher_sha256"),
                  ("codex", "code_mode_host_dispatcher_sha256")]
    else:
        raise ValueError("legacy installed receipt lacks complete helper provenance; use a fresh install root")
    for name, key in files:
        if private_read(destination / name, 1024 * 1024 * 1024, hash_only=True) != previous.get(key):
            raise ValueError("installed provider artifact differs from its receipt; publication refused")


def active_installation(root):
    active = root / "codex-current"
    if active.is_symlink():
        target = Path(os.readlink(active))
        if target.is_absolute() or len(target.parts) != 1 or not target.name.startswith(PROVIDER + "-"):
            raise ValueError("active provider pointer is not a reviewed local artifact directory")
        destination = root / target
        if not destination.is_dir():
            raise ValueError("active provider artifact directory is missing")
        private_directory(destination)
        receipt_bytes = private_read(destination / "receipt.json", 16384)
        if destination.name != PROVIDER + "-" + hashlib.sha256(receipt_bytes).hexdigest():
            raise ValueError("active provider receipt differs from immutable artifact identity")
        return destination
    if active.exists():
        raise ValueError("active provider pointer must be a symlink")
    legacy = root / PROVIDER
    if legacy.exists():
        private_directory(legacy)
        return legacy
    return None


def install(binary, root, official_cli, launcher, code_mode_host, helper_identity=None, verified=None):
    probe(binary)
    private_directory(root)
    current = active_installation(root)
    previous = None
    if current is not None:
        previous = json.loads(private_read(current / "receipt.json", 16384))
        verify_installed(current, previous)
    # Build in a sibling stage. An interrupted copy leaves the current provider intact.
    stage = Path(tempfile.mkdtemp(prefix=".codex-stage-", dir=root))
    try:
        shutil.copyfile(binary, stage / "codex-app-server")
        (stage / "codex-app-server").chmod(0o700)
        shutil.copyfile(launcher, stage / "codex")
        (stage / "codex").chmod(0o700)
        shutil.copyfile(launcher, stage / "codex-code-mode-host")
        (stage / "codex-code-mode-host").chmod(0o700)
        shutil.copyfile(code_mode_host, stage / "codex-code-mode-host-payload")
        (stage / "codex-code-mode-host-payload").chmod(0o700)
        receipt = {"contract": CONTRACT, "source_commit": SOURCE, "patch_sha256": digest(PATCH),
                   "binary_sha256": digest(stage / "codex-app-server"), "official_cli": str(official_cli),
                   "code_mode_host_sha256": digest(stage / "codex-code-mode-host-payload"),
                   "code_mode_host_dispatcher_sha256": digest(stage / "codex-code-mode-host"),
                   "code_mode_host_source_commit": SOURCE,
                   "profile": "dev-small", "toolchain": "1.95.0"}
        if helper_identity is not None:
            receipt["code_mode_host_v8_inputs"] = helper_identity["v8_inputs"]
        (stage / "receipt.json").write_text(json.dumps(receipt, sort_keys=True) + "\n")
        (stage / "receipt.json").chmod(0o600)
        # A byte-changing rebuild requires independently verified build fingerprints.
        identity_keys = ("contract", "source_commit", "patch_sha256", "binary_sha256", "profile", "toolchain", "code_mode_host_sha256")
        if previous is not None and any(previous.get(key) != receipt[key] for key in identity_keys):
            keys = ("source_commit", "patch_sha256", "profile", "toolchain", "binary_sha256", "code_mode_host_sha256", "code_mode_host_v8_inputs")
            if verified is None or any(verified.get(key) != receipt.get(key) for key in keys):
                raise ValueError("provider artifact differs from installed receipt; verified build provenance required")
        # Receipt identity includes dispatcher and helper bytes. Never mutate a
        # directory a running provider may still reference for helpers/attestation.
        artifact = hashlib.sha256((stage / "receipt.json").read_bytes()).hexdigest()
        destination = root / (PROVIDER + "-" + artifact)
        if destination.exists():
            private_directory(destination)
            existing = json.loads(private_read(destination / "receipt.json", 16384))
            if existing != receipt:
                raise ValueError("immutable provider receipt differs from staged artifact")
            verify_installed(destination, existing)
        else:
            stage.rename(destination)
        # Atomic pointer handoff affects only new launches; current_exe resolves
        # inside the immutable directory and old installations remain available.
        pointer = root / (".codex-current-" + artifact)
        try:
            pointer.symlink_to(destination.name)
            os.replace(pointer, root / "codex-current")
        finally:
            if pointer.is_symlink():
                pointer.unlink()
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
    with tempfile.TemporaryDirectory(prefix=".codex-probe-", dir=binary.parent) as scratch:
        home = Path(scratch)
        (home / "codex").mkdir(mode=0o700)
        environment = {"HOME": str(home), "CODEX_HOME": str(home / "codex"), "PATH": "/usr/bin:/bin",
                       "TMPDIR": str(home), "LANG": "C.UTF-8", "DO_NOT_TRACK": "1"}
        _probe(binary, request, home, environment)


def _probe(binary, request, home, environment):
    process = subprocess.Popen([str(binary), "--listen", "stdio://"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True,
        env=environment, cwd=home)
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
        process.stdin.close()
        process.stdout.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=Path(os.environ.get("DOXA_CODEX_PROTECTED_CACHE", str(Path.home() / ".cache/doxa/codex-protected"))))
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
    if not options.launcher.is_absolute() or not os.access(options.launcher, os.X_OK):
        parser.error("--launcher must be an absolute native launcher executable")
    with locked_directory(options.cache):
        binary = build(options.cache, options.cargo)
        code_mode_host, helper_identity = build_code_mode_host(options.cache, options.cargo)
        verified = verified_artifacts(options.cache, binary, code_mode_host, helper_identity)
        with locked_directory(options.install_root):
            install(binary, options.install_root, official, options.launcher, code_mode_host, helper_identity, verified)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"doxa-codex-install: {error}")
