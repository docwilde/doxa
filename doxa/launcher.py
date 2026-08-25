"""doxa.launcher -- the start-menu entry, on distros that have one.

`doxa launcher install` writes three files, all per-user, no root:

* ``$XDG_DATA_HOME/applications/doxa.desktop`` -- the freedesktop entry
  every major desktop reads (GNOME's grid, KDE's kickoff, XFCE's whisker,
  rofi/dmenu). ``Terminal=true`` delegates the terminal choice to the
  desktop environment instead of this module guessing an emulator that
  may not be installed.
* ``$XDG_DATA_HOME/icons/hicolor/512x512/apps/doxa.png`` -- the raster
  icon, shipped INSIDE the package (``doxa/assets/icon.png``) precisely
  so the curl-piped installer does not need a second network fetch to
  place it.
* ``$XDG_DATA_HOME/icons/hicolor/scalable/apps/doxa.svg`` -- the same
  mark as vector. A panel asking for 22px off a 512px PNG downsamples;
  hicolor's lookup prefers an exact raster size and falls back to
  scalable, so shipping both means the launcher grid gets the PNG and a
  small panel slot gets a rendering instead of a smudge.

Not distro-dependent: the XDG spec is the one cross-distro surface there
is. What it IS is desktop-dependent -- a bare tiling WM with no
.desktop-aware launcher simply never shows the entry, which is the same
non-failure as a terminal that never reports focus. macOS has no start
menu; install() says so and does nothing rather than pretending an app
bundle into existence.

`install` is idempotent (rewrites all three files in place) and
`uninstall` removes exactly what install wrote -- nothing else, so a
hand-edited entry dies on reinstall but a foreign file never does.

**Two things the entry has to get right about WHICH DOXA it launches**,
both fixed in v0.58.0 after `doxa launcher install` from a current
checkout produced a menu entry reporting 0.8.0:

* ``Exec`` names the interpreter THIS process is running under, not the
  bare word ``doxa``. A bare name is resolved against the *desktop
  session's* PATH at click time, which is a different PATH from the shell
  that ran the install and typically finds an ancient ``uv tool
  install``ed copy -- or, on a machine that only ever ran DOXA from a
  checkout, nothing at all. :func:`launch_command` measures the answer off
  ``sys.executable`` so the entry launches the DOXA that wrote it.
* The version is WRITTEN INTO the entry, from :mod:`doxa.version` and
  nowhere else, as ``X-DOXA-Version`` and in the ``Comment`` the desktop
  shows on hover. That makes staleness visible instead of silent, and
  gives :func:`stale_entry` something to compare -- see the ``launcher``
  check in :mod:`doxa.doctor`, which is what tells a user their menu entry
  has fallen behind. This module never carries a version literal of its
  own; ``pyproject.toml`` is the single source of truth and
  ``doxa.version`` is the only road from it.
"""

from __future__ import annotations

import contextlib
import importlib.resources
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import version as version_mod

DESKTOP_ID = "doxa.desktop"
ICON_NAME = "doxa"

#: The key the entry records its version under. ``X-`` prefixed because
#: the freedesktop spec reserves plain keys and its own ``Version`` key
#: means the version of the SPEC the entry conforms to, not the version of
#: the application -- writing 0.57.0 there would be a lie in a
#: standardised field.
VERSION_KEY = "X-DOXA-Version"

#: What :func:`stale_entry` reports for an entry written before v0.58.0,
#: which carries no version key at all. Distinct from "no entry" and from
#: a known-but-different version: it is the state the original defect
#: report was in, and it is fixed the same way.
UNVERSIONED = "unversioned"

DESKTOP_TEMPLATE = """\
[Desktop Entry]
Type=Application
Name=DOXA
GenericName=Agent terminal
Comment=Terminal for Claude agents -- where belief earns knowledge (v{version})
Exec={exec_command}
Icon={icon}
Terminal=true
Categories=Development;Utility;
Keywords=claude;agent;terminal;lore;memory;
{version_key}={version}
"""

# freedesktop.org Desktop Entry Specification, "The Exec key": a value
# containing any of these must be quoted. Inside the quotes, backslash,
# double quote, backtick and dollar are backslash-escaped.
_EXEC_RESERVED = set(" \t\n\"'\\><~|&;$*?#()`")


def _quote_exec(word: str) -> str:
    """One Exec word, quoted the way the spec asks when it has to be.

    Unconditional quoting would be simpler and is what most generators
    do; it also makes the common entry -- an absolute path with no spaces
    in it -- unreadable to the human who opens the file to find out what
    their menu is about to run. Quote only what needs it."""
    if not any(char in _EXEC_RESERVED for char in word):
        return word
    escaped = word
    for char in ("\\", '"', "`", "$"):
        escaped = escaped.replace(char, "\\" + char)
    return f'"{escaped}"'


def exec_target() -> Path:
    """The absolute path the entry will launch: THIS DOXA, on disk.

    Never the bare word ``doxa``. A .desktop ``Exec`` is resolved against
    the PATH of the *desktop session* -- the one the display manager
    exported at login, which has no venv on it, frequently no
    ``~/.local/bin`` either, and no relationship at all to the shell that
    typed ``uv run doxa launcher install``. The bare name therefore
    resolved to whatever old ``uv tool install`` happened to still be
    lying around: the reported defect exactly, a shortcut installed from a
    current checkout that started 0.8.0 from ``~/.local/bin``.

    Anchored on ``sys.prefix``, NOT on ``sys.executable``. That is not a
    style preference, it is the second bug this function had: measured
    under ``uv run doxa launcher install``, ``sys.executable`` is the BASE
    interpreter uv resolved the environment from
    (``~/.local/share/uv/python/cpython-3.12…/bin/python3.12``) while
    ``sys.prefix`` is the project venv. An ``Exec`` built from the former
    names a python that cannot import :mod:`doxa` at all -- a shortcut
    that fails with ``ModuleNotFoundError`` instead of merely starting the
    wrong version, which is worse than the defect it was fixing.
    ``sys.prefix`` is by definition the environment whose site-packages
    this code was imported from.

    Two forms, in order:

    * the console script in that environment (``<venv>/bin/doxa``), which
      is what ``uv tool install`` and ``uv sync`` both create, and which
      reads plainly in the file; or
    * that environment's own interpreter, run as ``-m doxa.cli`` (see
      :func:`launch_command`) -- an install with no console script still
      has an interpreter that can import :mod:`doxa`, and
      ``doxa.cli.main`` is the same entry point the script wraps.

    **Nothing here is ``resolve()``d**, deliberately. ``<venv>/bin/python``
    is a SYMLINK to the base interpreter; following it produces a path
    whose ``sys.prefix`` is the base environment, which is the same
    ModuleNotFoundError by another road. The venv path has to stay the
    venv path.

    **THE DESIGN CHOICE, stated rather than defaulted into.** The
    alternatives were "whatever ``doxa`` is on PATH" (what it did, and
    what broke) and "the install this command was run from" (this). The
    second wins because ``doxa launcher install`` is not a request for a
    shortcut to some DOXA -- the user runs it from a specific tree, having
    just built or updated that tree, and means *this one*. PATH
    resolution's advantage is that it follows a later ``uv tool install``
    upgrade for free; its cost is that it is unobservable, which is the
    entire defect. An entry that pins is at least a wrong answer you can
    read.

    Pinning has a real cost and it is paid rather than hidden: a shortcut
    to a checkout dies when the checkout moves. Three things make that
    loud instead of mysterious -- :func:`install` PRINTS the path it wrote
    and the version that path reports, the entry records both, and
    :func:`doxa.doctor` fails the ``launcher`` check when the recorded
    path has stopped existing, naming ``doxa launcher install`` as the
    fix."""
    bin_dir = environment_bin()
    script = bin_dir / "doxa"
    if script.is_file() and os.access(script, os.X_OK):
        return script
    for name in ("python3", "python"):
        interpreter = bin_dir / name
        if interpreter.is_file():
            return interpreter
    return Path(sys.executable)


def environment_bin() -> Path:
    """``<sys.prefix>/bin`` -- the script directory of the environment
    :mod:`doxa` was imported from."""
    return Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin")


def launch_command() -> str:
    """:func:`exec_target` as a quoted, spec-legal ``Exec`` value."""
    target = exec_target()
    quoted = _quote_exec(str(target))
    # The console script IS `doxa`; anything else we picked is the
    # interpreter, which needs the module spelled out.
    return quoted if target.name == "doxa" else f"{quoted} -m doxa.cli"


def _site_packages_of(interpreter: Path) -> "list[Path]":
    """``<venv>/lib/python*/site-packages`` for an interpreter path."""
    prefix = interpreter.parent.parent
    return sorted(prefix.glob("lib/python*/site-packages"))


def version_at(executable: "Path | None") -> "str | None":
    """The DOXA version an executable ON DISK belongs to, or None.

    Read, never run. Two reasons this does not just execute the thing and
    ask it: a launcher command must not spawn an unknown binary to write a
    text file, and a stale install is exactly the copy most likely to be
    broken in a way that hangs. So: follow the script's shebang to its
    interpreter, then read the version out of the ``.dist-info`` directory
    name in that interpreter's ``site-packages`` -- the same file
    ``importlib.metadata`` would read, without importing anything.

    None means "could not measure", which :func:`install` prints as such.
    A launcher that guessed a version here would be repeating the original
    defect in a new place."""
    if executable is None:
        return None
    executable = Path(executable)
    if executable == exec_target():
        # Our own: doxa.version is the single source of truth and it is
        # already resolved. Never re-derive what pyproject.toml says.
        return version_mod.resolve_version()
    interpreter = executable
    try:
        with executable.open("rb") as fh:
            first = fh.readline(1024)
        if first.startswith(b"#!"):
            shebang = first[2:].strip().decode("utf-8", "replace")
            # "#!/usr/bin/env python" has no path to follow; a direct
            # "#!/path/to/python" does.
            candidate = Path(shebang.split()[0]) if shebang.split() else None
            if candidate is not None and candidate.is_absolute():
                interpreter = candidate
    except OSError:
        return None
    for site in _site_packages_of(interpreter):
        for dist in sorted(site.glob("doxa-*.dist-info")):
            name = dist.name[len("doxa-"):-len(".dist-info")]
            if name:
                return name
    return None


def path_doxa() -> "Path | None":
    """The ``doxa`` a bare command would find on PATH right now, or None.
    What ``Exec=doxa`` used to launch.

    Reported UNRESOLVED -- as the user would type it. Whether two paths
    are the same install is :func:`os.path.samefile`'s question, not
    something to settle by rewriting one of them into a form nobody
    recognises."""
    found = shutil.which("doxa")
    return Path(found) if found else None


def shadowing_install() -> "tuple[Path, str | None] | None":
    """A ``doxa`` on PATH that is NOT the one this process is, as
    ``(path, version-or-None)`` -- otherwise None.

    This is the condition that produced the report, and the launcher is
    the right place to notice it: it is the one command whose whole job is
    "wire up a way to start DOXA", so it is looking at both answers
    already. It REPORTS and does nothing else. Rewriting or removing
    somebody's ``uv tool install`` is not a side effect a shortcut command
    gets to have, and the two installs coexisting is a perfectly
    legitimate arrangement -- a stable tool install plus a dev checkout is
    how most people work."""
    found = path_doxa()
    if found is None:
        return None
    mine = exec_target()
    with contextlib.suppress(OSError):
        if found.samefile(mine):
            return None
    if found == mine:
        return None
    return found, version_at(found)


def desktop_entry() -> str:
    """The full text of the entry, for the version of DOXA running now.

    A function rather than the module-level constant it was through
    v0.56.0: both interesting fields -- the version and the interpreter --
    are measurements of the running process, and a constant evaluated at
    import time is exactly the shape of thing that goes stale."""
    return DESKTOP_TEMPLATE.format(
        version=version_mod.resolve_version(),
        version_key=VERSION_KEY,
        exec_command=launch_command(),
        icon=ICON_NAME,
    )


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


def svg_icon_path() -> Path:
    return data_home() / "icons" / "hicolor" / "scalable" / "apps" / f"{ICON_NAME}.svg"


def _asset(name: str) -> bytes:
    """One shipped asset's bytes, wherever this DOXA is running from.

    Installed wheel: pyproject's ``force-include`` put it at
    ``doxa/assets/``. Repo checkout: it is still only at the repo's own
    top-level ``assets/`` -- same file, mapped at build time -- so fall
    through to that. The same two-tier lookup :mod:`doxa.banner` uses for
    the logo, and it stays on the ``importlib.resources`` traversable
    rather than a ``Path`` so a zipped install reads instead of raising."""
    packaged = importlib.resources.files("doxa") / "assets" / name
    if packaged.is_file():
        return packaged.read_bytes()
    return (Path(__file__).resolve().parent.parent / "assets" / name).read_bytes()


def installed_version() -> "str | None":
    """The version recorded in the entry ON DISK, or None when there is no
    entry or it predates :data:`VERSION_KEY`.

    Parsed by hand rather than with ``configparser``: a .desktop file is
    INI-shaped but not INI (values are not unescaped the same way, and a
    hand-edited one may carry duplicate keys that configparser refuses
    outright), and this only ever needs one key."""
    try:
        text = desktop_path().read_text(encoding="utf-8")
    except OSError:
        return None
    prefix = f"{VERSION_KEY}="
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip() or None
    return None


def installed_exec() -> "Path | None":
    """The executable path the entry ON DISK will launch, or None.

    The ``Exec`` value minus the spec's quoting and minus a trailing ``-m
    doxa.cli``, so it is the thing to check for existence. A pre-v0.58.0
    entry says ``Exec=doxa``, a bare name with no path in it at all, and
    that returns None -- there is nothing to check, which is the point."""
    try:
        text = desktop_path().read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if not line.startswith("Exec="):
            continue
        value = line[len("Exec="):].strip()
        if value.startswith('"'):
            end = value.find('"', 1)
            value = value[1:end] if end > 0 else value[1:]
            for char in ("\\", '"', "`", "$"):
                value = value.replace("\\" + char, char)
        else:
            value = value.split(" ", 1)[0]
        return Path(value) if value.startswith("/") else None
    return None


def stale_entry() -> "str | None":
    """What the installed entry claims to be, when that disagrees with the
    DOXA running now -- otherwise None.

    Three states collapse to None (nothing installed, and installed-and-
    current) or to a string worth printing:

    * no entry at all -> None. Never installing the launcher is a normal
      way to use DOXA, not a fault.
    * an entry with no version key -> :data:`UNVERSIONED`. Every entry
      written before v0.58.0 is one of these, including the one that
      produced the original report, and it is stale by construction: it
      also carries the ``Exec=doxa`` that made the version wrong.
    * a version that differs -> that version, so the message can say what
      the menu is actually going to launch."""
    if not desktop_path().is_file():
        return None
    found = installed_version()
    if found is None:
        return UNVERSIONED
    return None if found == version_mod.resolve_version() else found


def install() -> str:
    """Write the entry + both icons. Returns the human report.

    Unconditional overwrite, including over an entry from an older DOXA.
    That is the deliberate choice for the stale-entry case: this function
    already promised idempotence, the file is one DOXA wrote and knows the
    full contents of, and the alternative -- refusing, or merging -- would
    leave the user holding the broken entry that made them run the command
    in the first place. Nothing in it is user data; a hand-edit is the
    documented casualty of running install again.

    **The report is part of the fix, not decoration.** It names the
    ABSOLUTE PATH written into ``Exec`` and the version that path reports,
    because the defect this closes was invisible for exactly as long as it
    was: the command said "installed", the entry looked fine, and the
    disagreement between the checkout and the thing the shortcut started
    surfaced weeks later as a wrong-looking version banner. A mismatch is
    now readable at install time, in the output of the command that caused
    it."""
    if sys.platform == "darwin":
        return "launcher: macOS has no start menu -- nothing to install (run `doxa` from a terminal)"
    if not sys.platform.startswith("linux"):
        return f"launcher: unsupported platform {sys.platform!r} -- nothing installed"

    version = version_mod.resolve_version()

    dp = desktop_path()
    dp.parent.mkdir(parents=True, exist_ok=True)
    dp.write_text(desktop_entry(), encoding="utf-8")

    for path, asset in ((icon_path(), "icon.png"), (svg_icon_path(), "icon.svg")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_asset(asset))

    # Cache refresh is best-effort: desktops rescan on their own schedule
    # and both tools are absent on plenty of working systems.
    for cmd in (
        ("update-desktop-database", str(dp.parent)),
        ("gtk-update-icon-cache", "-q", str(data_home() / "icons" / "hicolor")),
    ):
        if shutil.which(cmd[0]):
            # OSError as well as a non-zero exit: `which` answering and the
            # exec succeeding are two claims, and a PATH entry can go away
            # between them. Best-effort means best-effort -- a cache the
            # desktop rebuilds on its own schedule anyway must never be the
            # thing that fails an install.
            with contextlib.suppress(OSError):
                subprocess.run(cmd, check=False, capture_output=True)

    lines = [
        f"launcher: installed {dp}",
        f"launcher:   Exec = {exec_target()}  (DOXA {version})",
        f"launcher:   icon = {icon_path()}  (+ {svg_icon_path()})",
    ]

    # The other DOXA. Reported, never touched -- see shadowing_install().
    shadow = shadowing_install()
    if shadow is not None:
        other, other_version = shadow
        named = f"DOXA {other_version}" if other_version else "an unknown version"
        lines += [
            "launcher:",
            f"launcher: note: `doxa` on your PATH is a DIFFERENT install --",
            f"launcher:   {other}  ({named})",
            "launcher:   The shortcut does NOT use it: it launches the path "
            "above, which is",
            "launcher:   the DOXA you just ran this command from. Nothing "
            "has been changed",
            "launcher:   about the one on PATH. To make the `doxa` command "
            "agree with it,",
            "launcher:   reinstall that: uv tool install --force "
            f"{source_hint()}",
        ]
    return "\n".join(lines)


def source_hint() -> str:
    """What to hand ``uv tool install --force`` to make the PATH command
    match this DOXA -- the checkout's own directory when there is one, the
    project URL when there is not. Named separately because the answer is
    genuinely different for the two ways DOXA gets installed, and printing
    the wrong one is worse than printing nothing."""
    root = version_mod.source_root()
    return str(root) if root is not None else f"git+{version_mod.REPO_URL}"


def uninstall() -> str:
    """Remove exactly what install() wrote."""
    removed = []
    for path in (desktop_path(), icon_path(), svg_icon_path()):
        try:
            path.unlink()
            removed.append(str(path))
        except FileNotFoundError:
            pass
    if not removed:
        return "launcher: nothing installed"
    return "launcher: removed " + ", ".join(removed)
