# SPDX-License-Identifier: AGPL-3.0-only
"""Task dispatch checks with stub Cargo and compiled, owned native carriers."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from test_install_rust import _compile_native_stub

ROOT = Path(__file__).resolve().parents[1]


def fixture(tmp_path):
    source = tmp_path / "source with spaces"
    source.mkdir()
    shutil.copyfile(ROOT / "task", source / "task")
    for name in ("rust/doxa-tui/Cargo.toml", "rust/doxa-daemon/Cargo.toml"):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# native fixture\n")
    scripts = source / "scripts"
    scripts.mkdir()
    (scripts / "install_codex_protected.py").write_text("""import json,os,sys
print(json.dumps({'args':sys.argv[1:],'cache':os.environ.get('DOXA_CODEX_PROTECTED_CACHE')}))
""")
    (scripts / "install.sh").write_text("""#!/bin/sh
printf '%s\\n' "$DOXA_RUST_REPO_URL" "$1" "${DOXA_INSTALL_CODEX_PROTECTED:-}" > "$INSTALL_CAPTURE"
""")
    native = _compile_native_stub(tmp_path)
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    cargo = fakebin / "cargo"
    cargo.write_text("""#!/usr/bin/python3
import json,os,shutil,sys
from pathlib import Path
args=sys.argv[1:]
with open(os.environ['CAPTURE'],'a') as out:
    out.write(json.dumps({'args':args,'lore':os.environ.get('DOXA_LORE_RS'),'jobs':os.environ.get('CARGO_BUILD_JOBS')})+'\\n')
if '--bin' in args:
    binary=args[args.index('--bin')+1]
    if binary=='lore-rs' and os.environ.get('FAIL_NATIVE')=='1': sys.exit(71)
    target=Path(os.environ['CARGO_TARGET_DIR'])/('release' if '--release' in args else 'debug')
    target.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(os.environ['NATIVE_FIXTURE'],target/binary)
    (target/binary).chmod(0o700)
""")
    cargo.chmod(0o700)
    env = {**os.environ, "PATH": str(fakebin) + ":/usr/bin:/bin", "HOME": str(tmp_path),
           "TMPDIR": str(tmp_path), "DOXA_TASK_TARGET_DIR": str(tmp_path / "target"),
           "NATIVE_FIXTURE": str(native), "DOXA_LORE_RS": "/wrong-installed-carrier",
           "DOXA_DAEMON_BIN": "/wrong-installed-daemon", "CAPTURE": str(tmp_path / "cargo.jsonl"),
           "INSTALL_CAPTURE": str(tmp_path / "install.txt"), "CARGO_BUILD_JOBS": "1",
           "DOXA_TASK_PROFILE": "debug"}
    env.pop("DOXA_LORE_PYTHON", None)
    return source, env


def calls(env):
    path = Path(env["CAPTURE"])
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def run(source, env, *args):
    return subprocess.run(["sh", str(source / "task"), *args], env=env, capture_output=True, text=True, timeout=5)


@pytest.mark.parametrize("profile", ["debug", "release"])
@pytest.mark.parametrize("verb", ["run", "new", "doctor"])
def test_task_builds_and_passes_exact_native_carriers_and_arguments(tmp_path, profile, verb):
    source, env = fixture(tmp_path)
    env["DOXA_TASK_PROFILE"] = profile
    arguments = ["--engine", "claude", "--claude-bin", "/fixture/cli with spaces"]
    result = run(source, env, verb, *arguments)
    assert result.returncode == 0, result.stderr
    reply = json.loads(result.stdout)
    native = Path(env["DOXA_TASK_TARGET_DIR"]) / profile / "lore-rs"
    assert reply["lore"] == str(native) and native.read_bytes().startswith(b"\x7fELF")
    assert reply["daemon"] == str(native.with_name("doxa-daemon"))
    assert reply["python"] is None
    assert reply["args"] == ([] if verb == "run" else [verb]) + arguments
    builds = calls(env)
    assert len(builds) == 3 and all("--locked" in call["args"] for call in builds)
    assert all(call["jobs"] == "1" for call in builds)
    assert builds[-1]["args"][-4:] == ["--package", "lore-core", "--bin", "lore-rs"]
    assert all(("--release" in call["args"]) == (profile == "release") for call in builds)


def test_task_native_build_failure_never_launches_frontend(tmp_path):
    source, env = fixture(tmp_path)
    env["FAIL_NATIVE"] = "1"
    result = run(source, env, "run", "--engine", "claude")
    assert result.returncode == 71 and not result.stdout


def test_task_tests_build_carrier_before_crates_and_export_native_path(tmp_path):
    source, env = fixture(tmp_path)
    result = run(source, env, "test")
    assert result.returncode == 0, result.stderr
    invocations = calls(env)
    assert invocations[0]["args"][-4:] == ["--package", "lore-core", "--bin", "lore-rs"]
    assert len(invocations) == 13
    for call in invocations[1:]:
        assert call["args"][0] == "test" and "--locked" in call["args"]
        assert call["lore"] == str(Path(env["DOXA_TASK_TARGET_DIR"]) / "debug/lore-rs")
    for call in invocations[1:]:
        if any(name in call["args"][-1] for name in ("doxa-daemon", "doxa-vendors")):
            assert call["args"][2:4] == ["--features", "local-test-server"]


def test_task_protected_provider_builds_native_dispatcher_and_forwards_options(tmp_path):
    source, env = fixture(tmp_path)
    options = ["--cache", str(tmp_path / "provider cache"), "--official-cli", "/fixture/codex"]
    result = run(source, env, "codex-provider", *options)
    assert result.returncode == 0, result.stderr
    reply = json.loads(result.stdout)
    native = Path(env["DOXA_TASK_TARGET_DIR"]) / "release/doxa-codex-protected"
    assert reply["args"] == ["--launcher", str(native), *options]
    assert native.read_bytes().startswith(b"\x7fELF")
    build, = calls(env)
    assert build["args"][:3] == ["build", "--release", "--locked"]
    assert build["args"][-4:] == ["--package", "doxa-engines", "--bin", "doxa-codex-protected"]


def test_task_protected_help_does_not_build_or_download(tmp_path):
    source, env = fixture(tmp_path)
    result = run(source, env, "codex-provider", "--help")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["args"] == ["--help"]
    assert calls(env) == []


def test_task_install_uses_committed_head_and_preserves_opt_out(tmp_path):
    source, env = fixture(tmp_path)
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "test: committed task fixture"], check=True)
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    (source / "rust/doxa-tui/Cargo.toml").write_text("pending edits")
    env["DOXA_INSTALL_CODEX_PROTECTED"] = "0"
    result = run(source, env, "install")
    assert result.returncode == 0, result.stderr
    assert "working-tree changes are not included" in result.stderr
    assert Path(env["INSTALL_CAPTURE"]).read_text().splitlines() == [str(source), revision, "0"]
    assert calls(env) == []
