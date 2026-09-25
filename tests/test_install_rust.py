# SPDX-License-Identifier: AGPL-3.0-only
"""Offline installer checks with a local source ref and stubbed Cargo/uv."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "scripts/install.sh"


def _source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for name in ("rust/doxa-tui/Cargo.toml", "rust/doxa-daemon/Cargo.toml"):
        path = repo / name
        path.parent.mkdir(parents=True)
        path.write_text("[package]\nname = 'fixture'\nversion = '0.1.0'\n")
    script = repo / "rust/doxa-claude/claude_sidecar.py"
    script.parent.mkdir(parents=True)
    script.write_text("# Claude sidecar fixture\n")
    (repo / "pyproject.toml").write_text("[project]\nname = 'doxa'\nversion = '2.0.0'\n")
    (repo / "uv.lock").write_text("# locked fixture\n")
    assets = repo / "assets"
    assets.mkdir()
    (assets / "icon.png").write_bytes(b"fixture png")
    (assets / "icon.svg").write_text("<svg/>")
    package = repo / "doxa"
    package.mkdir()
    for name in ("__init__", "lore_bridge", "engine"):
        (package / f"{name}.py").write_text(f"# fixture {name}\n")
    for name in ("lore_core", "claude_agent_sdk"):
        folder = repo / name
        folder.mkdir()
        (folder / "__init__.py").write_text("# fixture\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "fixture"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "branch", "-M", "main"], check=True)
    return repo


def _run(tmp_path: Path, repo: Path, *args: str, fail_install_name: str | None = None, cargo: bool = True, env_overrides: dict[str, str] | None = None):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    if fail_install_name:
        move = fakebin / "mv"
        move.write_text(
            "#!/bin/sh\n"
            f"case $2 in */.doxa-install.*/{fail_install_name}) exit 73 ;; esac\n"
            "exec /usr/bin/mv \"$@\"\n"
        )
        move.chmod(0o755)
    rustc = fakebin / "rustc"
    rustc.write_text("#!/bin/sh\nprintf 'host: test-host-target\\n'\n")
    rustc.chmod(0o755)
    if cargo:
        cargo_script = fakebin / "cargo"
        cargo_script.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$DOXA_TEST_LOG\"\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  if [ \"$1\" = --manifest-path ]; then manifest=$2; fi\n"
            "  if [ \"$1\" = --target ]; then target=$2; fi\n"
            "  shift\n"
            "done\n"
            "dir=${manifest%/*}\n"
            "mkdir -p \"$CARGO_TARGET_DIR/$target/release\"\n"
            "case $dir in\n"
            "  */doxa-tui) cat > \"$CARGO_TARGET_DIR/$target/release/doxa-rs\" <<'SH'\n"
            "#!/bin/sh\n"
            "[ \"$(command -v python3)\" = \"$DOXA_LORE_PYTHON\" ] || exit 19\n"
            "python3 -c 'import doxa.lore_bridge, doxa.engine, lore_core, claude_agent_sdk' || exit 20\n"
            "printf 'rust frontend\\n'\n"
            "SH\n"
            "    ;;\n"
            "  */doxa-daemon) printf '#!/bin/sh\\n' > \"$CARGO_TARGET_DIR/$target/release/doxa-daemon\" ;;\n"
            "esac\n"
        )
        cargo_script.chmod(0o755)
    uv = fakebin / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        "case $1 in\n"
        "  venv) python3 -m venv --without-pip \"$4\" ;;\n"
        "  sync)\n"
        "    site=$($VIRTUAL_ENV/bin/python -c 'import site; print(site.getsitepackages()[0])')\n"
        "    cp -R \"$7/doxa\" \"$7/lore_core\" \"$7/claude_agent_sdk\" \"$site/\" ;;\n"
        "esac\n"
    )
    uv.chmod(0o755)
    path = f"{fakebin}:/usr/bin:/bin"
    if not cargo:
        utilities = tmp_path / "utilities"
        utilities.mkdir()
        for name in ("git", "mktemp", "cp", "mv", "rm", "mkdir", "chmod", "python3", "sed", "dirname", "cat", "ln", "sh"):
            (utilities / name).symlink_to(shutil.which(name))
        path = f"{fakebin}:{utilities}"
    log = tmp_path / "cargo.log"
    env = {
        **os.environ,
        "HOME": str(home),
        "DOXA_HOME": str(home / ".doxa"),
        "PATH": path,
        "TMPDIR": str(tmp_path),
        "DOXA_RUST_REPO_URL": str(repo),
        "DOXA_TEST_LOG": str(log),
        "XDG_DATA_HOME": str(home / ".local/share"),
        **(env_overrides or {}),
    }
    proc = subprocess.run(["sh", str(INSTALL_SH), *args], cwd=tmp_path, env=env, text=True, capture_output=True, timeout=40)
    return proc, home, log


def test_default_installs_rust_doxa_and_importable_sidecars(tmp_path):
    repo = _source_repo(tmp_path)
    proc, home, log = _run(tmp_path, repo)
    assert proc.returncode == 0, proc.stderr
    bin_dir = home / ".local/bin"
    assert (bin_dir / "doxa").is_file()
    assert (bin_dir / "doxa-rs").is_file()
    assert (bin_dir / "doxa-daemon-rs").is_file()
    assert (bin_dir / "doxa-claude-sidecar.py").is_file()
    assert (bin_dir / ".doxa-sidecar-current").is_symlink()
    assert "rust/doxa-tui/Cargo.toml --bin doxa-rs" in log.read_text()
    run = subprocess.run([str(bin_dir / "doxa"), "list"], cwd="/", env={**os.environ, "PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert "rust frontend" in run.stdout
    desktop = home / ".local/share/applications/doxa.desktop"
    entry = desktop.read_text()
    assert f"Exec={bin_dir / 'doxa'}\n" in entry
    assert "Terminal=true\n" in entry
    assert "X-DOXA-Version=0.1.0\n" in entry
    assert (home / ".local/share/icons/hicolor/512x512/apps/doxa.png").read_bytes() == b"fixture png"
    assert (home / ".local/share/icons/hicolor/scalable/apps/doxa.svg").read_text() == "<svg/>"


def test_shortcut_uses_installed_path_with_spaces_and_updates_on_reinstall(tmp_path):
    repo = _source_repo(tmp_path)
    custom_bin = tmp_path / "bin dir $draft %two"
    options = {"DOXA_RUST_BIN_DIR": str(custom_bin)}
    first, home, _ = _run(tmp_path, repo, env_overrides=options)
    assert first.returncode == 0, first.stderr
    desktop = home / ".local/share/applications/doxa.desktop"
    expected_exec = f'Exec="{custom_bin / "doxa"}"\n'.replace("$", r"\$").replace("%", "%%")
    assert expected_exec in desktop.read_text()
    desktop.write_text("stale entry\n")
    second, _, _ = _run(tmp_path, repo, env_overrides=options)
    assert second.returncode == 0, second.stderr
    assert expected_exec in desktop.read_text()


def test_shortcut_rejects_unrepresentable_launcher_path(tmp_path):
    repo = _source_repo(tmp_path)
    custom_bin = tmp_path / "bin\nInjected=true"
    proc, home, _ = _run(tmp_path, repo, env_overrides={"DOXA_RUST_BIN_DIR": str(custom_bin)})
    assert proc.returncode == 0, proc.stderr
    assert "could not install desktop shortcut" in proc.stderr
    assert not (home / ".local/share/applications/doxa.desktop").exists()


def test_shortcut_opt_out(tmp_path):
    repo = _source_repo(tmp_path)
    proc, home, _ = _run(tmp_path, repo, env_overrides={"DOXA_NO_LAUNCHER": "1"})
    assert proc.returncode == 0, proc.stderr
    assert not (home / ".local/share/applications/doxa.desktop").exists()


def test_install_replaces_old_python_launcher_and_rolls_back_on_failure(tmp_path):
    repo = _source_repo(tmp_path)
    bin_dir = tmp_path / "home/.local/bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "doxa").write_text("old Python launcher\n")
    (bin_dir / "doxa-rs").write_text("old Rust launcher\n")
    (bin_dir / ".doxa-sidecar-current").symlink_to("old-env")
    proc, _, _ = _run(tmp_path, repo, fail_install_name="doxa")
    assert proc.returncode != 0
    assert (bin_dir / "doxa").read_text() == "old Python launcher\n"
    assert (bin_dir / "doxa-rs").read_text() == "old Rust launcher\n"
    assert os.readlink(bin_dir / ".doxa-sidecar-current") == "old-env"
    assert not list(bin_dir.glob(".doxa-install.*"))
    assert not (tmp_path / "home/.local/share/applications/doxa.desktop").exists()


@pytest.mark.parametrize("ref", ["--upload-pack=evil", "../../etc", "rust/2.0;evil", "@{upstream}"])
def test_rejects_unsafe_refs(tmp_path, ref):
    repo = _source_repo(tmp_path)
    proc, home, _ = _run(tmp_path, repo, ref)
    assert proc.returncode != 0
    assert "invalid ref" in proc.stderr
    assert not (home / ".local/bin").exists()


def test_missing_cargo_fails_before_mutation(tmp_path):
    repo = _source_repo(tmp_path)
    proc, home, _ = _run(tmp_path, repo, cargo=False)
    assert proc.returncode != 0
    assert "cargo is required" in proc.stderr
    assert not (home / ".local/bin").exists()


@pytest.mark.parametrize("link", ["home", "sidecars"])
def test_rejects_symlinked_sidecar_directories(tmp_path, link):
    repo = _source_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    if link == "home":
        (home / ".doxa").symlink_to(outside, target_is_directory=True)
    else:
        (home / ".doxa").mkdir()
        (home / ".doxa/sidecars").symlink_to(outside, target_is_directory=True)

    proc, _, _ = _run(tmp_path, repo)
    assert proc.returncode != 0
    assert "symlink" in proc.stderr
    assert not (home / ".local/bin/doxa").exists()
