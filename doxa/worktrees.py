"""doxa.worktrees -- one git worktree per session, so two sessions on the
same repo+branch never stomp each other's edits.

The constraint that shapes this module is git's own: the same branch
cannot be checked out in two worktrees at once. So a session does not get
a *copy* of the repo, it gets its own linked worktree
(``git worktree add``) on its own throwaway branch, ``doxa/<short>``,
forked from whatever the launching cwd has checked out. Two sessions in
the same repo (even on the same branch) each get an isolated working tree
and index; neither can see the other's uncommitted edits, and neither can
block the other from checking out anything.

``<short>`` is the session id's own first 8 characters -- stable from the
moment the session is minted (worktree/branch names are decided at spawn
time), unlike the Haiku-generated title (doxa.naming), which only exists
once the first turn has been named and would make renaming the branch
mid-session pure churn for zero benefit. See naming.py's own docstring for
the same "cheap, once, cached" discipline applied to a *different* handle.

Layout, under ``$DOXA_HOME`` (default ``~/.doxa``), a sibling of the
peer/session state doxa.config already keeps there::

    worktrees/<repo>-<short>/     the linked worktree itself
    worktrees/.meta/<repo>-<short>.json
                                   sidecar: {main_root, branch, base_ref,
                                   session_id} -- OUTSIDE the worktree's own
                                   tree deliberately, so it can never show
                                   up as an untracked file and make an
                                   otherwise-clean worktree look dirty.

Lifecycle, all in :func:`finalize`, called once at a session's REAL end
(never at a mere detach -- a daemon-hosted session lingers with its
worktree intact while it can still be reattached, see doxa/daemon.py):

* CLEAN (``git status --porcelain`` empty) and ZERO commits ahead of the
  branch it forked from -> the worktree and its branch vanish with no
  trace (``git worktree remove`` + ``git branch -D``).
* Anything else -- a dirty tree, or committed-but-unmerged work -- is the
  user's work. It is NEVER destroyed and NEVER auto-merged: kept, and
  :func:`finalize` returns the message saying so (``kept doxa/<short> --
  merge when ready``) for the caller to show wherever a "session ended"
  message belongs -- an attached client's SystemBlock, or, headless (the
  daemon's own finalize can run with nobody watching), a log line.

Every git call here degrades to "leave it alone" on failure: a worktree
this module cannot prove is safe to remove is a worktree it keeps, and a
cwd it cannot resolve to a repo just means the caller's fallback (run the
session directly in ``cwd``, today's unchanged behavior) applies.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
from pathlib import Path

from . import config as config_mod
from . import peers as peers_mod


def _bool(env_name: str, default: bool) -> bool:
    """Same vocabulary as config._coerce's bool kind, able to default ON --
    identical in shape to doxa.clock._bool / doxa.notify._bool, kept as its
    own four lines rather than a cross-module import for one helper (the
    house convention those two already established)."""
    raw = config_mod.raw(env_name).strip()
    if not raw:
        return default
    return raw.lower() not in ("0", "false", "no", "off")


def enabled() -> bool:
    """Effective value of the worktree_per_session setting -- DEFAULT ON,
    per the user's own framing: "whenever a session starts in a repo
    branch". DOXA_WORKTREE=0 (or the settings-modal equivalent) is the only
    way back to today's behavior."""
    return _bool("DOXA_WORKTREE", True)


def worktrees_root() -> Path:
    return config_mod.doxa_home() / "worktrees"


def _meta_dir() -> Path:
    return worktrees_root() / ".meta"


def _meta_path(target: Path) -> Path:
    return _meta_dir() / f"{target.name}.json"


def _write_meta(target: Path, **fields: str) -> None:
    try:
        _meta_dir().mkdir(parents=True, exist_ok=True)
        os.chmod(_meta_dir(), 0o700)
        tmp = _meta_path(target).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(fields, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, _meta_path(target))
    except OSError:
        pass  # best-effort: a missing sidecar just means finalize can't
        # tell this worktree apart from one the user made by hand, so it
        # leaves it alone -- "keep" is always the safe default (see
        # finalize()'s meta-is-None case).


def read_meta(worktree_path: str) -> "dict | None":
    try:
        data = json.loads(
            _meta_path(Path(worktree_path)).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _drop_meta(target: Path) -> None:
    with contextlib.suppress(OSError):
        _meta_path(target).unlink()


def _short_id(session_name: str) -> str:
    """The stable dir/branch handle: the session id's own first 8
    hex-ish characters, sanitized defensively (a UUID needs none of this,
    but a caller passing something else must not be able to smuggle a
    path separator or shell metacharacter into a git ref/dirname)."""
    return re.sub(r"[^0-9A-Za-z]", "", str(session_name or ""))[:8] or "session"


def _base_ref(cwd: str) -> "str | None":
    """What the new worktree's branch forks FROM: the branch currently
    checked out at ``cwd``, or -- detached HEAD -- the commit itself.
    ``None`` only when git cannot resolve anything at all."""
    try:
        proc = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() or None


def _existing_worktree_path(main_root: str, branch: str) -> "str | None":
    """`git worktree list`'s own answer to "does a worktree for this
    branch already exist". Checked before -- and again after a failed --
    ``git worktree add``, so a second call for the same session (a daemon
    restart reusing a session id, a startup race) lands on the SAME
    worktree instead of erroring or trying to double it."""
    try:
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=main_root, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    target_ref = f"refs/heads/{branch}"
    path = None
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip()
        elif line.startswith("branch ") and line[len("branch "):].strip() == target_ref:
            return path
    return None


def create(cwd: str, session_name: str) -> "str | None":
    """Give a session its own git worktree. Returns the worktree path to
    use as the session's cwd, or ``None`` -- the setting is off, ``cwd``
    is not a git repo (a worktree only means something inside one), or
    ``git worktree add`` itself failed for a reason reuse doesn't already
    explain. ``None`` is always safe: the caller's fallback is running the
    session directly in ``cwd``, today's unchanged behavior."""
    if not enabled():
        return None
    main_root = peers_mod.main_repo_root_of(cwd)
    if not main_root:
        return None
    short = _short_id(session_name)
    repo = Path(main_root).name
    branch = f"doxa/{short}"
    target = worktrees_root() / f"{repo}-{short}"

    existing = _existing_worktree_path(main_root, branch)
    if existing is not None:
        return existing

    base = _base_ref(cwd)
    if base is None:
        return None

    try:
        worktrees_root().mkdir(parents=True, exist_ok=True)
        os.chmod(worktrees_root(), 0o700)
    except OSError:
        return None

    try:
        proc = subprocess.run(
            ["git", "worktree", "add", "-q", "-b", branch, str(target), base],
            cwd=main_root, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        # A concurrent create (or a previous crashed attempt) may have won
        # the race between our reuse check above and this add -- check
        # once more before reporting failure.
        return _existing_worktree_path(main_root, branch)

    _write_meta(
        target, main_root=main_root, branch=branch, base_ref=base,
        session_id=str(session_name),
    )
    return str(target)


def is_clean(worktree_path: str) -> bool:
    """No uncommitted changes at all -- tracked or untracked -- in the
    worktree. Anything unreadable reads as DIRTY, the safe direction: a
    finalize that cannot prove a tree is clean must never remove it."""
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree_path, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    return not proc.stdout.strip()


def commits_ahead(worktree_path: str, base_ref: str) -> "int | None":
    """How many commits the worktree's branch carries beyond ``base_ref``.
    ``None`` when it cannot be measured (the base ref is gone, e.g.) --
    finalize treats that the same as "ahead", never as zero."""
    try:
        proc = subprocess.run(
            ["git", "rev-list", "--count", f"{base_ref}..HEAD"],
            cwd=worktree_path, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


def _remove(main_root: str, worktree_path: str, branch: str) -> None:
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["git", "worktree", "remove", "--force", worktree_path],
            cwd=main_root, capture_output=True, text=True, timeout=30,
        )
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=main_root, capture_output=True, text=True, timeout=10,
        )


def finalize(worktree_path: str) -> "str | None":
    """A session's REAL end (never a mere detach): clean up its worktree,
    or say why it was kept. See the module docstring for the clean/dirty
    rule. ``None`` means "nothing to report" -- either the worktree was
    removed with no trace, or this was never a doxa-managed worktree
    (no metadata: the setting was off for this session, or the sidecar
    was lost) and is left completely untouched."""
    meta = read_meta(worktree_path)
    if meta is None:
        return None
    target = Path(worktree_path)
    if not target.is_dir():
        _drop_meta(target)
        return None
    main_root = str(meta.get("main_root") or "")
    branch = str(meta.get("branch") or "")
    base_ref = str(meta.get("base_ref") or "")
    if not (main_root and branch and base_ref):
        return f"kept {branch or worktree_path} — merge when ready"
    clean = is_clean(worktree_path)
    ahead = commits_ahead(worktree_path, base_ref)
    if clean and ahead == 0:
        _remove(main_root, worktree_path, branch)
        _drop_meta(target)
        return None
    return f"kept {branch} — merge when ready"


def list_orphans() -> list[dict]:
    """Every doxa-managed worktree whose session has no live daemon in the
    peer registry right now -- doctor's read-only survey, never a
    mutation. Covers both a genuinely crashed session (killed before it
    could finalize) and a deliberately KEPT one (dirty or unmerged,
    waiting on the user): doctor cannot tell those apart and does not try
    to -- both are directories sitting around with no session watching
    them, which is exactly what the report says."""
    root = worktrees_root()
    meta_dir = _meta_dir()
    if not root.is_dir() or not meta_dir.is_dir():
        return []
    live_ids = {p.session_id for p in peers_mod.read_registry(reap=False)}
    orphans: list[dict] = []
    for meta_path in sorted(meta_dir.glob("*.json")):
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        wt_path = root / meta_path.stem
        if not wt_path.is_dir():
            continue
        session_id = str(data.get("session_id") or "")
        if session_id and session_id in live_ids:
            continue
        orphans.append({
            "path": str(wt_path),
            "branch": str(data.get("branch") or ""),
            "session_id": session_id,
        })
    return orphans
