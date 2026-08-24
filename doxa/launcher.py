"""doxa.launcher -- the start-menu entry, on distros that have one.

`doxa launcher install` writes exactly two files, both per-user, no root:

* ``$XDG_DATA_HOME/applications/doxa.desktop`` -- the freedesktop entry
  every major desktop reads (GNOME's grid, KDE's kickoff, XFCE's whisker,
  rofi/dmenu). ``Terminal=true`` delegates the terminal choice to the
  desktop environment instead of this module guessing an emulator that
  may not be installed.
* ``$XDG_DATA_HOME/icons/hicolor/512x512/apps/doxa.png`` -- the icon,
  shipped INSIDE the package (``doxa/assets/icon.png``) precisely so the
  curl-piped installer does not need a second network fetch to place it.

Not distro-dependent: the XDG spec is the one cross-distro surface there
is. What it IS is desktop-dependent -- a bare tiling WM with no
.desktop-aware launcher simply never shows the entry, which is the same
non-failure as a terminal that never reports focus. macOS has no start
menu; install() says so and does nothing rather than pretending an app
bundle into existence.

`install` is idempotent (rewrites both files in place) and `uninstall`
removes exactly what install wrote -- nothing else, so a hand-edited
entry dies on reinstall but a foreign file never does.
"""

from __future__ import annotations

import importlib.resources
import os
import shutil
import subprocess
import sys
from pathlib import Path

DESKTOP_ID = "doxa.desktop"
ICON_NAME = "doxa"

DESKTOP_ENTRY = """\
[Desktop Entry]
Type=Application
Name=DOXA
Comment=Terminal for Claude agents -- where belief earns knowledge
Exec=doxa
Icon=doxa
Terminal=true
Categories=Development;Utility;
Keywords=claude;agent;terminal;lore;memory;
"""


def data_home() -> Path:
    """$XDG_DATA_HOME with the spec's own fallback -- resolved at call
    time, not import time, so tests (and stubborn shells) can point it
    elsewhere."""
    env = os.environ.get("XDG_DATA_HOME", "").strip()
    return Path(env) if env else Path.home() / ".local" / "share"


def desktop_path() -> Path:
    return data_home() / "applications" / DESKTOP_ID


def icon_path() -> Path:
    return data_home() / "icons" / "hicolor" / "512x512" / "apps" / f"{ICON_NAME}.png"


def install() -> str:
    """Write the entry + icon. Returns a one-line human message."""
    if sys.platform == "darwin":
        return "launcher: macOS has no start menu -- nothing to install (run `doxa` from a terminal)"
    if not sys.platform.startswith("linux"):
        return f"launcher: unsupported platform {sys.platform!r} -- nothing installed"

    dp = desktop_path()
    dp.parent.mkdir(parents=True, exist_ok=True)
    dp.write_text(DESKTOP_ENTRY, encoding="utf-8")

    ip = icon_path()
    ip.parent.mkdir(parents=True, exist_ok=True)
    # Installed wheel: the force-include put the icon at doxa/assets/.
    # Repo checkout: it lives at the repo's own assets/ -- same file,
    # mapped at build time (see pyproject), so fall through to it.
    src = importlib.resources.files("doxa") / "assets" / "icon.png"
    if not src.is_file():
        src = Path(__file__).resolve().parent.parent / "assets" / "icon.png"
    ip.write_bytes(src.read_bytes())

    # Cache refresh is best-effort: desktops rescan on their own schedule
    # and both tools are absent on plenty of working systems.
    for cmd in (
        ("update-desktop-database", str(dp.parent)),
        ("gtk-update-icon-cache", "-q", str(data_home() / "icons" / "hicolor")),
    ):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, check=False, capture_output=True)

    return f"launcher: installed {dp} (+ icon)"


def uninstall() -> str:
    """Remove exactly what install() wrote."""
    removed = []
    for path in (desktop_path(), icon_path()):
        try:
            path.unlink()
            removed.append(str(path))
        except FileNotFoundError:
            pass
    if not removed:
        return "launcher: nothing installed"
    return "launcher: removed " + ", ".join(removed)
