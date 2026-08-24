"""doxa.launcher -- the XDG start-menu entry.

Every test points XDG_DATA_HOME at tmp_path: the module resolves it at
call time (not import time) precisely so these tests never touch the
operator's real ~/.local/share.
"""

import sys

import pytest

from doxa import launcher


@pytest.fixture
def data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path


def test_install_writes_entry_and_icon(data_home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    msg = launcher.install()
    entry = data_home / "applications" / "doxa.desktop"
    icon = data_home / "icons" / "hicolor" / "512x512" / "apps" / "doxa.png"
    assert entry.is_file() and icon.is_file()
    text = entry.read_text(encoding="utf-8")
    # The load-bearing keys: Exec is the PATH command, Terminal delegates
    # emulator choice to the desktop, Icon matches the installed name.
    assert "Exec=doxa" in text
    assert "Terminal=true" in text
    assert "Icon=doxa" in text
    assert icon.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert str(entry) in msg


def test_install_is_idempotent(data_home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    launcher.install()
    launcher.install()  # second run rewrites in place, no error
    assert (data_home / "applications" / "doxa.desktop").is_file()


def test_uninstall_removes_exactly_what_install_wrote(data_home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    foreign = data_home / "applications" / "other.desktop"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("[Desktop Entry]\n")
    launcher.install()
    msg = launcher.uninstall()
    assert not (data_home / "applications" / "doxa.desktop").exists()
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
