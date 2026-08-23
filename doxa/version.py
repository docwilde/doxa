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
"""

from __future__ import annotations

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
