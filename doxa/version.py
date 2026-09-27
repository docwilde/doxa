# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.version -- ONE version, wherever DOXA is running from.

``pyproject.toml`` is the source of truth. Everything else derives:

* **A source checkout** (the repo is right there, next to the package) reads
  ``pyproject.toml`` directly. It must never fall back to "unknown" -- the
  file that defines the version is on disk, and a terminal that cannot say
  what it is is a terminal you cannot file a bug against.
* **An installed copy** has no pyproject, so it reads the distribution
  metadata that was built FROM that same pyproject
  (``importlib.metadata``).

Order matters: the checkout wins. Running `uv run doxa` from a tree whose
pyproject says 0.5.0 while an older wheel is installed in the environment
must report 0.5.0 -- what is EXECUTING is the checkout.

The git identity of a source checkout (short sha, plus ``+`` when the tree
is dirty) is available too, and is shown only where it says something the
status bar's git chip does not -- see ``SessionPane._identity_text``.

Item Z widened this module from "the version" to "which install is this":
:func:`about_rows` / :func:`about_text` are what ``/about`` renders, and
they belong here because every one of those rows is the same kind of fact
as the version itself -- measured off the running thing, never configured
and never guessed. A row whose source cannot answer is omitted, on the
same rule the identity block follows: a screen whose job is to be quoted
into a bug report may not contain a plausible-looking constant.
"""

from __future__ import annotations

import importlib
from contextlib import closing
import platform
import subprocess
import tomllib
from functools import lru_cache
from pathlib import Path

DIST_NAME = "doxa"


def source_root() -> "Path | None":
    """The checkout DOXA is running FROM, or None when it is installed.

    Identified by a ``pyproject.toml`` beside the package that actually
    declares this project -- a pyproject belonging to something else (a
    vendored copy inside another repo) is not our checkout."""
    root = Path(__file__).resolve().parent.parent
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        with pyproject.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return None
    if (data.get("project") or {}).get("name") != DIST_NAME:
        return None
    return root


def _from_pyproject() -> "str | None":
    root = source_root()
    if root is None:
        return None
    try:
        with (root / "pyproject.toml").open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return None
    version = (data.get("project") or {}).get("version")
    return str(version) if version else None


def _from_metadata() -> "str | None":
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version(DIST_NAME)
    except Exception:  # PackageNotFoundError and any importlib oddity
        return None


def resolve_version() -> str:
    """The version string every surface shows. Checkout first, installed
    metadata second; "unknown" only if a copy is BOTH not a checkout and
    not an installed distribution, which is a broken install, not a
    supported way to run."""
    return _from_pyproject() or _from_metadata() or "unknown"


@lru_cache(maxsize=1)
def source_sha() -> "str | None":
    """Short sha of the checkout DOXA is running from, or None.

    Read from ``.git`` directly (HEAD, then the ref it names, then
    packed-refs) rather than by running git: this is called while a TUI is
    starting, and the app's own GitLine established that a couple of file
    reads beat a subprocess on that path. Cached: the code that is running
    cannot change under itself."""
    root = source_root()
    if root is None:
        return None
    git = root / ".git"
    if git.is_file():  # worktree/submodule pointer
        try:
            for line in git.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("gitdir:"):
                    candidate = Path(line.split(":", 1)[1].strip())
                    git = candidate if candidate.is_absolute() else (root / candidate)
                    break
        except OSError:
            return None
    try:
        head = (git / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not head.startswith("ref:"):
        return head[:7] or None
    ref = head.split(":", 1)[1].strip()
    try:
        return (git / ref).read_text(encoding="utf-8", errors="replace").strip()[:7]
    except OSError:
        pass
    try:
        for line in (git / "packed-refs").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            if line.endswith(f" {ref}"):
                return line.split(" ", 1)[0].strip()[:7]
    except OSError:
        pass
    return None


def source_dirty() -> bool:
    """Does the checkout have uncommitted changes? One `git status
    --porcelain`, and any failure reads as "not dirty" -- a version line
    must never be the thing that breaks a session."""
    root = source_root()
    if root is None:
        return False
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and bool(proc.stdout.strip())


def native_lore_info() -> "dict | None":
    """Measured native runtime metadata, using one short owned carrier."""
    from . import _lore_bootstrap
    from .native_lore import Carrier, NativeLoreError

    try:
        _lore_bootstrap.export_sticky_lore_root()
        with closing(Carrier(timeout=1.0)) as client:
            value = client.request("runtime_config_v1")
            if not isinstance(value, dict):
                raise NativeLoreError("invalid_native_frame")
            for key in ("root", "projects_dir"):
                raw = value.get(key)
                if (not isinstance(raw, str) or not 0 < len(raw) <= 4096
                        or any(ord(char) < 32 or ord(char) == 127 for char in raw)):
                    raise NativeLoreError("invalid_native_frame")
                if not Path(raw).is_absolute():
                    raise NativeLoreError("invalid_native_frame")
            version = value.get("version")
            stages = value.get("disabled_stages")
            if (not isinstance(version, str) or not 0 < len(version) <= 128
                    or any(ord(char) < 32 or ord(char) == 127 for char in version)
                    or not isinstance(stages, list) or len(stages) > 5
                    or any(stage not in ("inject", "index", "review", "beliefs", "skills") for stage in stages)):
                raise NativeLoreError("invalid_native_frame")
            process = client.process
            source = process.args[0] if process is not None else None
            if (not isinstance(source, str) or not 0 < len(source) <= 4096
                    or any(ord(char) < 32 or ord(char) == 127 for char in source)):
                raise NativeLoreError("invalid_native_frame")
            return {"root":value["root"], "projects_dir":value["projects_dir"],
                    "version":version, "disabled_stages":stages,
                    "source":str(Path(source).absolute())}
    except Exception:  # broken/missing native runtime leaves measured rows empty
        return None


def lore_core_version() -> "str | None":
    """Native LORE version, or None when the selected carrier cannot answer."""
    info = native_lore_info()
    return info["version"] if info is not None else None


def _dep_version(module_name: str, dist_name: str) -> "str | None":
    """A dependency's version: its own ``__version__`` first, the installed
    distribution metadata second, None if neither answers. Both tiers are
    needed -- ``textual`` and ``claude_agent_sdk`` both expose the
    attribute today, but a wheel that stops doing so should degrade to the
    metadata rather than blanking the row of a bug-report screen."""
    try:
        module = importlib.import_module(module_name)
    except Exception:  # noqa: BLE001 -- an unimportable dep is a row, not a crash
        module = None
    declared = getattr(module, "__version__", None) if module is not None else None
    if declared:
        return str(declared)
    try:
        from importlib.metadata import version as dist_version

        return str(dist_version(dist_name))
    except Exception:  # noqa: BLE001 -- PackageNotFoundError and friends
        return None


# The repository and licence a bug report needs to know it may quote code
# at all. Public repo, AGPL-3.0-only with a commercial option -- stating
# that on the about screen is the same honesty the README's badge row
# already carries, not a legal notice bolted on.
REPO_URL = "https://github.com/docwilde/doxa"
LICENCE = "AGPL-3.0-only (commercial licence available)"


def about_rows(
    update_available: "bool | None" = None,
) -> "list[tuple[str, str]]":
    """``(label, value)`` for ``/about`` -- the version, and everything
    else a bug report has to state before anyone can reproduce it.

    Every row is MEASURED at call time from the thing itself: the running
    interpreter, the imported packages, the resolved config path. A row
    whose source cannot answer is omitted rather than filled with a
    plausible-looking constant, on the same rule the identity block
    follows -- an about screen that guesses is worse than one with a gap,
    because its whole job is to be quotable.

    The sha is ALWAYS shown here, unlike :func:`version_line`, which hides
    it when the surrounding view already carries it. That suppression
    exists because the identity block sits directly above a git chip
    printing the same hex string; ``/about`` is its own screen with no
    such neighbour, and "which commit is this code" is the second thing a
    bug report needs after the version.

    ``update_available`` is threaded in by the caller rather than checked
    here: ``doxa.update.check_for_update`` runs a ``git fetch``, DoxaApp
    already runs it once per boot off a worker
    (``DoxaApp._check_for_update``), and a modal must not open a network
    call on the UI thread to decorate one line. ``None`` means "nobody has
    looked", which prints nothing at all -- distinct from "looked, nothing
    to pull"."""
    from . import config as config_mod

    version = resolve_version()
    sha = source_sha()
    if sha:
        version += f" ({sha}{'+' if source_dirty() else ''})"
    if update_available:
        version += "  · update available (/update)"
    rows: "list[tuple[str, str]]" = [("doxa", version)]
    rows.append((
        "python",
        f"{platform.python_version()} ({platform.python_implementation()})",
    ))
    for label, module_name, dist_name in (
        ("textual", "textual", "textual"),
        ("agent sdk", "claude_agent_sdk", "claude-agent-sdk"),
    ):
        found = _dep_version(module_name, dist_name)
        if found:
            rows.append((label, found))
    # Native runtime is the authority for version and root. A missing
    # carrier cannot be replaced with Python package or environment guesses.
    info = native_lore_info()
    if info is not None:
        rows.append(("lore", f"{info['version']}  {info['root']}"))
        rows.append(("lore from", f"native  {info['source']}"))
    rows.append((
        "platform",
        f"{platform.system()} {platform.release()} ({platform.machine()})",
    ))
    # Keyboard protocol (item O). This row is ALWAYS present, including
    # when the answer is "not measured" -- the one deliberate exception to
    # the omit-what-you-cannot-answer rule above, and it earns the
    # exception. The row exists to settle "is this key dead because of
    # DOXA or because of my terminal", and an ABSENT row cannot be told
    # apart from a DOXA old enough never to have looked; "not measured" is
    # an observation about this run, not the plausible-looking constant
    # that rule forbids.
    from . import keyboard as keyboard_mod

    rows.append(("keyboard", keyboard_mod.describe()))
    config_file = config_mod.config_path()
    rows.append((
        "config",
        f"{config_file}" + ("" if config_file.exists() else "  (not written yet)"),
    ))
    rows.append(("repo", REPO_URL))
    rows.append(("licence", LICENCE))
    return rows


def about_text(update_available: "bool | None" = None) -> str:
    """:func:`about_rows` as the block of text the dialog shows and its
    copy door puts on the clipboard -- one function, so what a user pastes
    into an issue is byte-for-byte what they were looking at."""
    rows = about_rows(update_available)
    width = max(len(label) for label, _value in rows)
    return "\n".join(f"{label:<{width}}  {value}" for label, value in rows)


def version_line(head_sha: "str | None" = None) -> str:
    """`DOXA 0.4.0`, or `DOXA 0.4.0 (a1b2c3d+)` when the sha says something
    the surrounding view does not.

    ``head_sha`` is what the status line's git chip is already showing for
    THIS session's repo. When the code running and the repo on screen are
    the same commit, repeating the sha would put two identical hex strings
    in one view -- exactly the confusion the `@sha` labelling fixed. So the
    sha appears when it DIFFERS (a different repo, or DOXA installed from
    elsewhere), or when the checkout is dirty, which no other chip says."""
    version = resolve_version()
    sha = source_sha()
    if not sha:
        return f"DOXA {version}"
    dirty = source_dirty()
    if head_sha and sha == head_sha and not dirty:
        return f"DOXA {version}"
    return f"DOXA {version} ({sha}{'+' if dirty else ''})"
