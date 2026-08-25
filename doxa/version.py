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
import json
import os
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


def lore_core_version() -> "str | None":
    """The version of the ``lore_core`` this process LOADED, or None.

    Two tiers, and both are load-bearing:

    * ``lore_core.__version__`` (LORE 0.35.1 and later). It resolves the
      same way this module does -- plugin manifest when the package sits
      inside a plugin checkout, wheel metadata when it does not -- so it
      is right for whichever carrier DOXA ended up with, including the
      installed distribution that has no manifest to read at all.
    * The plugin manifest at the bootstrap's own location. Every LORE
      before 0.35.1 shipped only inside the plugin and carried no version
      attribute; those installs are still out there, and for them
      ``.claude-plugin/plugin.json`` beside the package is the only file
      that declares a version.

    Any failure is None -- an /about row that cannot be filled is omitted,
    never guessed."""
    from . import _lore_bootstrap

    _lore_bootstrap.ensure_importable()
    try:
        import lore_core

        declared = getattr(lore_core, "__version__", None)
        if declared:
            return str(declared)
    except Exception:  # noqa: BLE001 -- an unimportable lore_core is a row, not a crash
        pass
    manifest = (
        _lore_bootstrap._lore_core_parent() / ".claude-plugin" / "plugin.json"
    )
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = (data or {}).get("version") if isinstance(data, dict) else None
    return str(version) if version else None


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
# at all. Public repo, and a NONCOMMERCIAL licence -- stating that on the
# about screen is the same honesty the README's badge row already carries,
# not a legal notice bolted on.
REPO_URL = "https://github.com/docwilde/doxa"
LICENCE = "DOXA Noncommercial License 1.0"


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
    # The store PATH comes from lore_core itself (``lore_core.ROOT``, the
    # same attribute SessionEngine.lore_root reports), not from re-reading
    # LORE_ROOT: lore_core resolves that variable once at ITS import and a
    # later change to the environment would make this row disagree with
    # the store actually in use. The env var is only the fallback for a
    # machine where lore_core is not importable at all.
    lore_version = lore_core_version()
    lore_root = ""
    try:
        import lore_core

        lore_root = str(lore_core.ROOT)
    except Exception:  # noqa: BLE001 -- no lore_core at all: the row degrades
        lore_root = os.environ.get("LORE_ROOT", "").strip()
    lore_bits = [bit for bit in (lore_version, lore_root) if bit]
    if lore_bits:
        rows.append(("lore", "  ".join(lore_bits)))
    # WHICH lore_core answered. Since v0.37.0 there are two places one can
    # come from -- the declared ``lore-core`` dependency, and a LORE plugin
    # checkout, which still wins when present (see
    # ``doxa._lore_bootstrap``) -- so the version above is no longer
    # enough to identify what is running. A user chasing a LORE-behaviour
    # difference must not have to guess which copy DOXA loaded, and
    # ``resolved_source`` measures it off ``lore_core.__file__`` rather
    # than restating the precedence rule.
    from . import _lore_bootstrap

    source = _lore_bootstrap.resolved_source()
    if source is not None:
        kind, location = source
        rows.append(("lore from", f"{kind}  {location}"))
    rows.append((
        "platform",
        f"{platform.system()} {platform.release()} ({platform.machine()})",
    ))
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
