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
        "printf '%s\\n' \"$1\" >> \"$DOXA_TEST_UV_LOG\"\n"
        "case $1 in\n"
        "  venv) python3 -m venv --without-pip \"$4\" || exit 1\n"
        "    [ \"${DOXA_TEST_KILL_AFTER_VENV:-0}\" != 1 ] || kill -KILL \"$PPID\" ;;\n"
        "  sync)\n"
        "    [ \"${DOXA_TEST_FAIL_SYNC:-0}\" != 1 ] || exit 74\n"
        "    site=$(\"$VIRTUAL_ENV/bin/python\" -c 'import site; print(site.getsitepackages()[0])')\n"
        "    cp -R \"$7/doxa\" \"$7/lore_core\" \"$7/claude_agent_sdk\" \"$site/\" || exit 1\n"
        "    printf '#!%s/bin/python\\nimport doxa.engine\\n' \"$VIRTUAL_ENV\" > \"$VIRTUAL_ENV/bin/fixture-entrypoint\"\n"
        "    chmod 755 \"$VIRTUAL_ENV/bin/fixture-entrypoint\" ;;\n"
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
        "DOXA_TEST_UV_LOG": str(tmp_path / "uv.log"),
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


def test_upgrade_replaces_sidecar_symlink_to_directory(tmp_path):
    repo = _source_repo(tmp_path)
    first, home, _ = _run(tmp_path, repo)
    assert first.returncode == 0, first.stderr
    old_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    pointer = home / ".local/bin/.doxa-sidecar-current"
    assert os.readlink(pointer) == str(home / ".doxa/sidecars" / old_sha / "bin")

    (repo / "doxa/engine.py").write_text("# updated fixture engine\n")
    subprocess.run(["git", "-C", str(repo), "add", "doxa/engine.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test",
                    "-c", "user.email=test@example.com", "commit", "-qm",
                    "test: update sidecar fixture"], check=True)
    new_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    second, _, _ = _run(tmp_path, repo)
    assert second.returncode == 0, second.stderr
    assert os.readlink(pointer) == str(home / ".doxa/sidecars" / new_sha / "bin")
    assert not (home / ".doxa/sidecars" / old_sha / "bin/.doxa-sidecar-current").exists()


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


def test_retry_repairs_sigkill_incomplete_environment_at_permanent_path(tmp_path):
    repo = _source_repo(tmp_path)
    killed, home, _ = _run(tmp_path, repo, env_overrides={"DOXA_TEST_KILL_AFTER_VENV": "1"})
    assert killed.returncode == -9
    sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    original = home / ".doxa/sidecars" / sha
    assert (original / "bin/python").exists()
    assert not (original / ".doxa-install-sha").exists()
    assert not (home / ".local/bin/doxa").exists()

    repaired, _, _ = _run(tmp_path, repo, env_overrides={"PYTHONPATH": str(repo)})
    assert repaired.returncode == 0, repaired.stderr
    pointer = home / ".local/bin/.doxa-sidecar-current"
    repaired_env = Path(os.readlink(pointer)).parent
    assert repaired_env.name.startswith(f"{sha}.repair.")
    assert (repaired_env / ".doxa-install-sha").read_text().strip() == sha
    entrypoint = repaired_env / "bin/fixture-entrypoint"
    assert entrypoint.read_text().startswith(f"#!{repaired_env}/bin/python\n")
    assert subprocess.run([str(entrypoint)], cwd="/", capture_output=True).returncode == 0

    before = (tmp_path / "uv.log").read_text()
    repeated, _, _ = _run(tmp_path, repo, env_overrides={"DOXA_TEST_FAIL_SYNC": "1"})
    assert repeated.returncode == 0, repeated.stderr
    assert os.readlink(pointer) == str(repaired_env / "bin")
    assert (tmp_path / "uv.log").read_text() == before


@pytest.mark.parametrize("failure", ["sync", "install", "kill"])
def test_failed_repair_preserves_existing_pointer_and_binaries(tmp_path, failure):
    tmp_path = tmp_path / "quoted $cache"
    tmp_path.mkdir()
    repo = _source_repo(tmp_path)
    first, home, _ = _run(tmp_path, repo)
    assert first.returncode == 0, first.stderr
    bin_dir = home / ".local/bin"
    pointer = bin_dir / ".doxa-sidecar-current"
    original_bin = Path(os.readlink(pointer))
    site = subprocess.check_output([str(original_bin / "python"), "-c", "import site; print(site.getsitepackages()[0])"], text=True).strip()
    shutil.rmtree(Path(site) / "claude_agent_sdk")
    old_files = {name: (bin_dir / name).read_bytes() for name in ("doxa", "doxa-rs", "doxa-daemon-rs", "doxa-claude-sidecar.py")}
    options = {"DOXA_TEST_FAIL_SYNC": "1"} if failure == "sync" else {"DOXA_TEST_KILL_AFTER_VENV": "1"} if failure == "kill" else {}
    failed, _, _ = _run(tmp_path, repo, fail_install_name="doxa" if failure == "install" else None, env_overrides=options)
    assert failed.returncode != 0
    assert os.readlink(pointer) == str(original_bin)
    assert original_bin.is_dir()
    assert {name: (bin_dir / name).read_bytes() for name in old_files} == old_files
    repairs = list(original_bin.parent.parent.glob("*.repair.*"))
    if failure == "kill":
        assert len(repairs) == 1
        assert not (repairs[0] / ".doxa-install-sha").exists()
        retried, _, _ = _run(tmp_path, repo)
        assert retried.returncode == 0, retried.stderr
    else:
        assert not repairs


def test_complete_legacy_environment_reused_without_sync(tmp_path):
    repo = _source_repo(tmp_path)
    first, home, _ = _run(tmp_path, repo)
    assert first.returncode == 0, first.stderr
    env = Path(os.readlink(home / ".local/bin/.doxa-sidecar-current")).parent
    (env / ".doxa-install-sha").unlink()
    before = (tmp_path / "uv.log").read_text()
    repeated, _, _ = _run(tmp_path, repo, env_overrides={"DOXA_TEST_FAIL_SYNC": "1"})
    assert repeated.returncode == 0, repeated.stderr
    assert (tmp_path / "uv.log").read_text() == before


@pytest.mark.parametrize("candidate", ["outside", "symlink", "wrong-sha"])
def test_repair_does_not_adopt_untrusted_current_environment(tmp_path, candidate):
    repo = _source_repo(tmp_path)
    first, home, _ = _run(tmp_path, repo)
    assert first.returncode == 0, first.stderr
    pointer = home / ".local/bin/.doxa-sidecar-current"
    original = Path(os.readlink(pointer)).parent
    sha = original.name
    outside = tmp_path / "outside"
    shutil.copytree(original, outside, symlinks=True)
    if candidate == "outside":
        proposed = outside
    else:
        proposed = original.parent / f"{sha}.repair.abc123"
        if candidate == "symlink":
            proposed.symlink_to(outside, target_is_directory=True)
        else:
            shutil.copytree(outside, proposed, symlinks=True)
            (proposed / ".doxa-install-sha").write_text("wrong sha\n")
    pointer.unlink()
    pointer.symlink_to(proposed / "bin")
    site = subprocess.check_output([str(original / "bin/python"), "-c", "import site; print(site.getsitepackages()[0])"], text=True).strip()
    shutil.rmtree(Path(site) / "claude_agent_sdk")
    before = (tmp_path / "uv.log").read_text()
    repaired, _, _ = _run(tmp_path, repo)
    assert repaired.returncode == 0, repaired.stderr
    assert os.readlink(pointer) != str(proposed / "bin")
    assert (tmp_path / "uv.log").read_text() == before + "venv\nsync\n"
    assert (outside / ".doxa-install-sha").read_text().strip() == sha
