# SPDX-License-Identifier: AGPL-3.0-only
"""Offline integration checks for the opt-in Rust preview installer."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "scripts" / "install.sh"


def _source_repo(tmp_path: Path, *, daemon: bool = False, tui: bool = True) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    if tui:
        manifest = repo / "rust/doxa-tui/Cargo.toml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("[package]\nname = 'doxa-tui'\nversion = '0.1.0'\n")
    if daemon:
        manifest = repo / "rust/doxa-daemon/Cargo.toml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("[package]\nname = 'doxa-daemon'\nversion = '0.1.0'\n")
    sidecar = repo / "rust/doxa-claude/claude_sidecar.py"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("# test Claude sidecar\n")
    (repo / "README.md").write_text("source")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "fixture"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "branch", "rust/2.0"], check=True)
    return repo


def _run(
    tmp_path: Path,
    repo: Path,
    *args: str,
    cargo: bool = True,
    fail_install_name: str | None = None,
    extra_env: dict[str, str] | None = None,
):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    if fail_install_name:
        mv_script = bindir / "mv"
        mv_script.write_text(
            "#!/bin/sh\n"
            "case $2 in\n"
            f"  */.doxa-install.*/{fail_install_name}) exit 73 ;;\n"
            "esac\n"
            "exec /usr/bin/mv \"$@\"\n"
        )
        mv_script.chmod(0o755)
    rustc_script = bindir / "rustc"
    rustc_script.write_text("#!/bin/sh\nprintf 'host: test-host-target\\n'\n")
    rustc_script.chmod(rustc_script.stat().st_mode | stat.S_IXUSR)
    if cargo:
        cargo_script = bindir / "cargo"
        cargo_script.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$DOXA_TEST_LOG\"\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  if [ \"$1\" = --manifest-path ]; then manifest=$2; fi\n"
            "  if [ \"$1\" = --target ]; then target=$2; fi\n"
            "  shift\n"
            "done\n"
            "[ -n \"${manifest:-}\" ] && [ -n \"${target:-}\" ] || exit 3\n"
            "dir=${manifest%/*}\n"
            "mkdir -p \"$CARGO_TARGET_DIR/$target/release\"\n"
            "case $dir in\n"
            "  */doxa-tui) printf '#!/bin/sh\\n' > \"$CARGO_TARGET_DIR/$target/release/doxa-rs\" ;;\n"
            "  */doxa-daemon) printf '#!/bin/sh\\n' > \"$CARGO_TARGET_DIR/$target/release/doxa-daemon\" ;;\n"
            "esac\n"
        )
        cargo_script.chmod(cargo_script.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "cargo.log"
    path = f"{bindir}:/usr/bin:/bin"
    if not cargo:
        utilities = tmp_path / "utilities"
        utilities.mkdir()
        for name in ("git", "mktemp", "cp", "rm", "mkdir", "chmod", "sh"):
            (utilities / name).symlink_to(shutil.which(name))
        path = f"{bindir}:{utilities}"
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": path,
        "TMPDIR": str(tmp_path),
        "DOXA_RUST_REPO_URL": str(repo),
        "DOXA_TEST_LOG": str(log),
    }
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["sh", str(INSTALL_SH), "--rust", *args],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    return proc, home, log


def test_rust_switch_builds_and_installs_only_preview_binary(tmp_path):
    repo = _source_repo(tmp_path)
    proc, home, log = _run(tmp_path, repo)
    assert proc.returncode == 0, proc.stderr
    assert (home / ".local/bin/doxa-rs").is_file()
    assert not (home / ".local/bin/doxa").exists()
    assert not (home / ".doxa").exists()
    assert not (home / ".local/bin/doxa-daemon-rs").exists()
    assert "--release --locked --target test-host-target --manifest-path" in log.read_text()
    assert "rust/doxa-tui/Cargo.toml --bin doxa-rs" in log.read_text()
    assert not list(tmp_path.glob("tmp.*"))  # checkout removed


def test_rust_switch_installs_daemon_when_present(tmp_path):
    repo = _source_repo(tmp_path, daemon=True)
    proc, home, log = _run(tmp_path, repo, "rust/2.0")
    assert proc.returncode == 0, proc.stderr
    assert (home / ".local/bin/doxa-rs").is_file()
    assert (home / ".local/bin/doxa-daemon-rs").is_file()
    assert log.read_text().count("--release --locked") == 2


def test_rust_switch_ignores_cargo_target_overrides(tmp_path):
    repo = _source_repo(tmp_path, daemon=True)
    proc, home, log = _run(
        tmp_path,
        repo,
        extra_env={
            "CARGO_TARGET_DIR": str(tmp_path / "other-target"),
            "CARGO_BUILD_TARGET": "another-target",
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert (home / ".local/bin/doxa-rs").is_file()
    assert (home / ".local/bin/doxa-daemon-rs").is_file()
    assert log.read_text().count("--target test-host-target") == 2
    assert not (tmp_path / "other-target").exists()


def test_rust_switch_replaces_preview_links_without_overwriting_python_doxa(tmp_path):
    repo = _source_repo(tmp_path, daemon=True)
    bin_dir = tmp_path / "home/.local/bin"
    bin_dir.mkdir(parents=True)
    python_doxa = bin_dir / "doxa"
    python_doxa.write_text("existing Python launcher\n")
    (bin_dir / "doxa-rs").symlink_to(python_doxa)
    (bin_dir / "doxa-daemon-rs").symlink_to(python_doxa)

    proc, _, _ = _run(tmp_path, repo)
    assert proc.returncode == 0, proc.stderr
    assert python_doxa.read_text() == "existing Python launcher\n"
    assert not (bin_dir / "doxa-rs").is_symlink()
    assert not (bin_dir / "doxa-daemon-rs").is_symlink()
    assert (bin_dir / "doxa-rs").is_file()
    assert (bin_dir / "doxa-daemon-rs").is_file()


@pytest.mark.parametrize("failed_name", ["doxa-claude-sidecar.py", "doxa-daemon-rs"])
def test_rust_install_failure_restores_all_previous_files_and_links(tmp_path, failed_name):
    repo = _source_repo(tmp_path, daemon=True)
    bin_dir = tmp_path / "home/.local/bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "doxa-rs").write_bytes(b"old tui")
    (bin_dir / "doxa-claude-sidecar.py").write_bytes(b"old sidecar")
    (bin_dir / "old-daemon").write_bytes(b"old daemon")
    (bin_dir / "doxa-daemon-rs").symlink_to("old-daemon")

    proc, _, _ = _run(tmp_path, repo, fail_install_name=failed_name)
    assert proc.returncode != 0
    assert (bin_dir / "doxa-rs").read_bytes() == b"old tui"
    assert (bin_dir / "doxa-claude-sidecar.py").read_bytes() == b"old sidecar"
    assert (bin_dir / "doxa-daemon-rs").is_symlink()
    assert os.readlink(bin_dir / "doxa-daemon-rs") == "old-daemon"
    assert not list(bin_dir.glob(".doxa-install.*"))


def test_rust_install_without_daemon_removes_obsolete_daemon(tmp_path):
    repo = _source_repo(tmp_path)
    bin_dir = tmp_path / "home/.local/bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "doxa-daemon-rs").write_bytes(b"obsolete")
    proc, _, _ = _run(tmp_path, repo)
    assert proc.returncode == 0, proc.stderr
    assert not (bin_dir / "doxa-daemon-rs").exists()


@pytest.mark.parametrize("ref", ["--upload-pack=evil", "../../etc", "rust/2.0;evil", "@{upstream}"])
def test_rust_switch_rejects_unsafe_refs(tmp_path, ref):
    repo = _source_repo(tmp_path)
    proc, home, log = _run(tmp_path, repo, ref)
    assert proc.returncode == 1
    assert "invalid Rust ref" in proc.stderr
    assert not (home / ".local/bin").exists()
    assert not log.exists()


def test_rust_switch_reports_missing_cargo(tmp_path):
    repo = _source_repo(tmp_path)
    proc, home, _ = _run(tmp_path, repo, cargo=False)
    assert proc.returncode == 1
    assert "cargo is required" in proc.stderr
    assert not (home / ".local/bin").exists()


def test_rust_switch_reports_missing_tui_and_cleans_checkout(tmp_path):
    repo = _source_repo(tmp_path, tui=False)
    proc, home, _ = _run(tmp_path, repo)
    assert proc.returncode == 1
    assert "has no rust/doxa-tui/Cargo.toml" in proc.stderr
    assert not (home / ".local/bin").exists()
    assert not list(tmp_path.glob("tmp.*"))
