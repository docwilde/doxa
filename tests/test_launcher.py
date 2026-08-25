# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.launcher -- the XDG start-menu entry.

Every test points XDG_DATA_HOME at tmp_path: the module resolves it at
call time (not import time) precisely so these tests never touch the
operator's real ~/.local/share.

The v0.58.0 block is about ONE reported defect -- `doxa launcher install`
from a current checkout producing a menu entry that reported 0.8.0 -- and
its two causes: an entry that recorded no version at all, and an
``Exec=doxa`` resolved against the desktop session's PATH rather than
against the DOXA that wrote it.
"""

import sys
from pathlib import Path

import pytest

from doxa import launcher
from doxa import version as version_mod


def _fake_install(root: Path, version: str, *, script: bool = True) -> Path:
    """A believable DOXA install on disk: a bin/doxa console script whose
    shebang names bin/python, and a matching doxa-<version>.dist-info in
    that interpreter's site-packages. What `uv tool install` leaves
    behind, and what launcher.version_at reads WITHOUT running any of
    it."""
    (root / "bin").mkdir(parents=True, exist_ok=True)
    interpreter = root / "bin" / "python"
    interpreter.write_text("", encoding="utf-8")
    site = root / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    (site / f"doxa-{version}.dist-info").mkdir(exist_ok=True)
    if not script:
        return interpreter
    exe = root / "bin" / "doxa"
    exe.write_text(f"#!{interpreter}\n# console script\n", encoding="utf-8")
    exe.chmod(0o755)
    return exe


@pytest.fixture
def data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path


def test_install_writes_entry_and_icons(data_home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    msg = launcher.install()
    entry = data_home / "applications" / "doxa.desktop"
    icon = data_home / "icons" / "hicolor" / "512x512" / "apps" / "doxa.png"
    svg = data_home / "icons" / "hicolor" / "scalable" / "apps" / "doxa.svg"
    assert entry.is_file() and icon.is_file() and svg.is_file()
    text = entry.read_text(encoding="utf-8")
    # The load-bearing keys: Terminal delegates emulator choice to the
    # desktop, Icon matches the installed icon name.
    assert "Terminal=true" in text
    assert "Icon=doxa" in text
    assert icon.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert svg.read_bytes().lstrip().startswith(b"<")
    assert str(entry) in msg


def test_the_entry_records_the_version_doxa_version_reports(data_home, monkeypatch):
    """THE defect. `pyproject.toml` is the single source of truth and
    `doxa.version` is the only road from it, so the version in the entry
    is the version the running DOXA reports -- never a literal in this
    module, and never whatever an older install left on disk."""
    monkeypatch.setattr(sys, "platform", "linux")
    launcher.install()
    text = (data_home / "applications" / "doxa.desktop").read_text(encoding="utf-8")
    current = version_mod.resolve_version()
    assert f"{launcher.VERSION_KEY}={current}" in text
    assert launcher.installed_version() == current
    # And it is on the line the desktop shows on hover, which is where the
    # user read the wrong number in the first place.
    assert f"(v{current})" in text


def test_the_message_names_the_version_it_just_wired_up(data_home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert f"DOXA {version_mod.resolve_version()}" in launcher.install()


def test_exec_resolves_to_the_doxa_the_install_was_run_from(data_home, monkeypatch):
    """THE defect. A .desktop ``Exec`` is resolved against the DESKTOP
    SESSION's PATH at click time -- a different PATH from the shell that
    ran the install, with no venv on it. ``Exec=doxa`` therefore launched
    whatever ancient `uv tool install`ed copy was lying around in
    ~/.local/bin, so a shortcut installed from a current checkout started
    0.8.0 instead.

    The entry now names an absolute path, and that path is THIS DOXA: the
    console script beside the running interpreter, or the interpreter
    itself. Pinned deliberately -- `doxa launcher install` run from a tree
    means a shortcut to that tree."""
    monkeypatch.setattr(sys, "platform", "linux")
    launcher.install()
    text = (data_home / "applications" / "doxa.desktop").read_text(encoding="utf-8")
    exec_line = next(l for l in text.splitlines() if l.startswith("Exec="))
    value = exec_line[len("Exec="):]

    assert value != "doxa"  # never again a bare name resolved through PATH
    assert value.lstrip('"').startswith("/"), value

    # It resolves back to the running interpreter's own environment, and
    # the entry's recorded version is that install's version.
    target = launcher.installed_exec()
    assert target == launcher.exec_target()
    assert target.exists()
    assert target.parent == Path(sys.prefix) / "bin"
    assert launcher.version_at(target) == version_mod.resolve_version()
    assert launcher.installed_version() == launcher.version_at(target)


def test_exec_is_anchored_on_sys_prefix_not_sys_executable(monkeypatch, tmp_path):
    """The SECOND bug this had. Under `uv run doxa`, sys.executable is the
    base interpreter uv resolved the environment from -- a python that
    cannot import doxa at all -- while sys.prefix is the project venv. An
    Exec built from sys.executable fails with ModuleNotFoundError, which
    is worse than starting the wrong version."""
    venv = tmp_path / "project" / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python3").write_text("", encoding="utf-8")
    base = tmp_path / "uv-python" / "bin" / "python3.12"
    base.parent.mkdir(parents=True)
    base.write_text("", encoding="utf-8")

    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(sys, "executable", str(base))
    assert launcher.exec_target() == venv / "bin" / "python3"
    assert launcher.launch_command() == f"{venv}/bin/python3 -m doxa.cli"


def test_the_venv_interpreter_symlink_is_never_resolved(monkeypatch, tmp_path):
    """<venv>/bin/python is a SYMLINK to the base interpreter. Following
    it produces a path whose sys.prefix is the base environment -- the
    same ModuleNotFoundError by another road."""
    base = tmp_path / "base" / "bin" / "python3"
    base.parent.mkdir(parents=True)
    base.write_text("", encoding="utf-8")
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python3").symlink_to(base)

    monkeypatch.setattr(sys, "prefix", str(venv))
    assert launcher.exec_target() == venv / "bin" / "python3"
    assert base not in launcher.exec_target().parents


def test_the_report_names_the_exec_path_and_its_version(data_home, monkeypatch):
    """A mismatch has to be readable at install time, in the output of the
    command that caused it -- not discovered weeks later from a
    wrong-looking version banner."""
    monkeypatch.setattr(sys, "platform", "linux")
    msg = launcher.install()
    assert f"Exec = {launcher.exec_target()}" in msg
    assert f"DOXA {version_mod.resolve_version()}" in msg
    assert str(launcher.icon_path()) in msg


def test_exec_quotes_a_path_that_needs_it(monkeypatch, tmp_path):
    weird = tmp_path / "my venv"
    (weird / "bin").mkdir(parents=True)
    (weird / "bin" / "python3").write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "prefix", str(weird))
    assert launcher.launch_command() == f'"{weird}/bin/python3" -m doxa.cli'


def test_exec_prefers_the_console_script_over_the_interpreter(monkeypatch, tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python3").write_text("", encoding="utf-8")
    script = tmp_path / "bin" / "doxa"
    script.write_text("#!/x/python\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    assert launcher.exec_target() == script
    assert launcher.launch_command() == str(script)  # no `-m doxa.cli`


def test_exec_falls_back_to_this_interpreter_when_the_prefix_has_no_bin(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "nothing-here"))
    assert launcher.exec_target() == Path(sys.executable)


def test_install_is_idempotent(data_home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    launcher.install()
    launcher.install()  # second run rewrites in place, no error
    assert (data_home / "applications" / "doxa.desktop").is_file()


# -- the OTHER doxa: a stale `uv tool install` on PATH -------------------


def test_version_at_reads_an_install_without_running_it(tmp_path):
    """Read, never run: a launcher command must not spawn an unknown
    binary to write a text file, and a stale install is exactly the copy
    most likely to hang. The console script here is not even executable
    Python -- if version_at tried to run it, this would fail."""
    exe = _fake_install(tmp_path / "old", "0.8.0")
    assert launcher.version_at(exe) == "0.8.0"


def test_version_at_is_none_when_it_cannot_be_measured(tmp_path):
    """None means "could not measure", printed as such. A launcher that
    guessed a version here would be repeating the original defect."""
    lonely = tmp_path / "bin" / "doxa"
    lonely.parent.mkdir(parents=True)
    lonely.write_text("#!/nowhere/python\n", encoding="utf-8")
    assert launcher.version_at(lonely) is None
    assert launcher.version_at(None) is None


def test_version_at_our_own_target_is_doxa_version(tmp_path):
    """pyproject.toml is the single source of truth; for OUR install the
    answer is doxa.version's and is never re-derived from a dist-info that
    may have been built from an earlier tree."""
    assert launcher.version_at(launcher.exec_target()) == version_mod.resolve_version()


def test_no_shadow_when_nothing_named_doxa_is_on_path(monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
    assert launcher.shadowing_install() is None


def test_no_shadow_when_the_path_doxa_is_this_doxa(monkeypatch):
    monkeypatch.setattr(
        launcher.shutil, "which", lambda name: str(launcher.exec_target())
    )
    assert launcher.shadowing_install() is None


def test_a_different_doxa_on_path_is_detected_with_its_version(monkeypatch, tmp_path):
    """The condition that produced the report: a stale `uv tool install`
    coexisting with the checkout the user works in."""
    old = _fake_install(tmp_path / "old", "0.8.0")
    monkeypatch.setattr(launcher.shutil, "which", lambda name: str(old))
    assert launcher.shadowing_install() == (old, "0.8.0")


def test_install_reports_the_other_doxa_and_changes_nothing_about_it(
    data_home, monkeypatch, tmp_path
):
    """Reported, never touched. Rewriting somebody's `uv tool install` is
    not a side effect a shortcut command gets to have, and a stable tool
    install beside a dev checkout is how most people work."""
    monkeypatch.setattr(sys, "platform", "linux")
    old = _fake_install(tmp_path / "old", "0.8.0")
    before = old.read_bytes()
    # Answer for `doxa` ONLY. A blanket `which` also answered for
    # update-desktop-database and gtk-update-icon-cache, so install()'s
    # best-effort cache refresh really executed them -- present on a
    # developer's desktop, absent on a CI runner, which is where this
    # failed with FileNotFoundError while passing locally.
    monkeypatch.setattr(
        launcher.shutil, "which", lambda name: str(old) if name == "doxa" else None
    )

    msg = launcher.install()
    assert str(old) in msg
    assert "DOXA 0.8.0" in msg
    assert "DIFFERENT install" in msg
    assert "uv tool install --force" in msg
    # And the shortcut still points at THIS DOXA, not at the one it found.
    assert launcher.installed_exec() == launcher.exec_target()
    assert launcher.installed_exec() != old
    assert old.read_bytes() == before  # untouched
    assert old.exists()


def test_install_says_nothing_about_path_when_there_is_no_other_doxa(
    data_home, monkeypatch
):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
    assert "DIFFERENT install" not in launcher.install()


def test_doctor_fails_when_the_exec_target_has_gone_away(data_home, monkeypatch, tmp_path):
    """The cost of pinning an absolute path, paid loudly: a shortcut to a
    checkout dies when the checkout moves, and that is a check with a fix
    rather than a menu entry that silently does nothing when clicked."""
    from doxa import doctor

    monkeypatch.setattr(sys, "platform", "linux")
    gone = _fake_install(tmp_path / "moved", "0.57.0")
    monkeypatch.setattr(launcher, "exec_target", lambda: gone)
    launcher.install()
    assert doctor._launcher_check().status == doctor.STATUS_PASS

    import shutil as shutil_mod

    shutil_mod.rmtree(tmp_path / "moved")
    check = doctor._launcher_check()
    assert check.status == doctor.STATUS_FAIL
    assert "no longer exists" in check.detail
    assert check.fix == "doxa launcher install"


# -- staleness ----------------------------------------------------------


def test_nothing_installed_is_not_stale(data_home):
    """Never running `doxa launcher install` is a normal way to use DOXA
    -- a tiling WM has no menu to put an entry in -- and must not read as
    a fault."""
    assert launcher.stale_entry() is None


def test_a_current_entry_is_not_stale(data_home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    launcher.install()
    assert launcher.stale_entry() is None


def test_an_entry_from_an_older_doxa_is_stale_and_names_its_version(
    data_home, monkeypatch
):
    monkeypatch.setattr(sys, "platform", "linux")
    launcher.install()
    entry = data_home / "applications" / "doxa.desktop"
    entry.write_text(
        entry.read_text(encoding="utf-8").replace(
            f"{launcher.VERSION_KEY}={version_mod.resolve_version()}",
            f"{launcher.VERSION_KEY}=0.8.0",
        ),
        encoding="utf-8",
    )
    assert launcher.installed_version() == "0.8.0"
    assert launcher.stale_entry() == "0.8.0"


def test_a_pre_0_57_entry_with_no_version_key_is_stale(data_home):
    """Every entry written before v0.58.0 -- including the one in the
    report -- carries no version at all AND the ``Exec=doxa`` that made
    the version wrong. It is stale by construction."""
    entry = data_home / "applications" / "doxa.desktop"
    entry.parent.mkdir(parents=True)
    entry.write_text(
        "[Desktop Entry]\nType=Application\nName=DOXA\nExec=doxa\n", encoding="utf-8"
    )
    assert launcher.installed_version() is None
    assert launcher.stale_entry() == launcher.UNVERSIONED


def test_reinstalling_over_a_stale_entry_makes_it_current(data_home, monkeypatch):
    """The chosen answer for an already-installed stale entry: overwrite
    it. install() already promised idempotence, DOXA wrote every byte of
    the file, and refusing would leave the user holding the broken entry
    that made them run the command."""
    monkeypatch.setattr(sys, "platform", "linux")
    entry = data_home / "applications" / "doxa.desktop"
    entry.parent.mkdir(parents=True)
    entry.write_text("[Desktop Entry]\nExec=doxa\nX-DOXA-Version=0.8.0\n",
                     encoding="utf-8")
    assert launcher.stale_entry() == "0.8.0"
    launcher.install()
    assert launcher.stale_entry() is None
    assert launcher.installed_version() == version_mod.resolve_version()


def test_doctor_fails_on_a_stale_entry_with_the_command_that_fixes_it(
    data_home, monkeypatch
):
    from doxa import doctor

    monkeypatch.setattr(sys, "platform", "linux")
    launcher.install()
    entry = data_home / "applications" / "doxa.desktop"
    entry.write_text(
        entry.read_text(encoding="utf-8").replace(
            f"{launcher.VERSION_KEY}={version_mod.resolve_version()}",
            f"{launcher.VERSION_KEY}=0.8.0",
        ),
        encoding="utf-8",
    )
    check = doctor._launcher_check()
    assert check.status == doctor.STATUS_FAIL
    assert "0.8.0" in check.detail
    assert check.fix == "doxa launcher install"

    launcher.install()
    assert doctor._launcher_check().status == doctor.STATUS_PASS


def test_doctor_passes_when_no_entry_is_installed(data_home, monkeypatch):
    from doxa import doctor

    monkeypatch.setattr(sys, "platform", "linux")
    check = doctor._launcher_check()
    assert check.status == doctor.STATUS_PASS
    assert check.fix == ""


# -- removal, platforms, XDG -------------------------------------------


def test_uninstall_removes_exactly_what_install_wrote(data_home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    foreign = data_home / "applications" / "other.desktop"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("[Desktop Entry]\n")
    launcher.install()
    msg = launcher.uninstall()
    assert not (data_home / "applications" / "doxa.desktop").exists()
    assert not (
        data_home / "icons" / "hicolor" / "scalable" / "apps" / "doxa.svg"
    ).exists()
    assert foreign.exists()  # never touches files it did not write
    assert "removed" in msg


def test_uninstall_with_nothing_installed(data_home):
    assert launcher.uninstall() == "launcher: nothing installed"


def test_macos_installs_nothing(data_home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    msg = launcher.install()
    assert "no start menu" in msg
    assert not (data_home / "applications").exists()


def test_xdg_default_falls_back_to_local_share(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    # Path.home() reads HOME on POSIX -- the fallback must land under it.
    assert launcher.data_home() == tmp_path / ".local" / "share"


def test_cli_launcher_command(data_home, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    from doxa import cli

    assert cli.main(["launcher"]) == 0
    assert (data_home / "applications" / "doxa.desktop").is_file()
    assert cli.main(["launcher", "uninstall"]) == 0
    assert not (data_home / "applications" / "doxa.desktop").exists()
    assert cli.main(["launcher", "sideways"]) == 2
    out = capsys.readouterr()
    assert "unknown action" in out.err
