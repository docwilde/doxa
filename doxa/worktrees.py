# SPDX-License-Identifier: AGPL-3.0-only
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
                                   base_oid, session_id} -- OUTSIDE the worktree's own
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
import fcntl
import stat
import threading
import uuid
import functools
import os
import re
import subprocess
from pathlib import Path

from . import config as config_mod
from . import lore_sync as lore_sync_mod
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


# Shared with Rust: retain each lock inode and hold it across daemon detach.
_lifecycle_files: dict[str, int] = {}
_lifecycle_mutex = threading.RLock()


def _serialized_lifecycle(fn):
    @functools.wraps(fn)
    def guarded(*args, **kwargs):
        with _lifecycle_mutex:
            return fn(*args, **kwargs)
    return guarded


def _acquire_lifecycle(target: Path) -> bool:
    key = str(target.absolute())
    with _lifecycle_mutex:
        if key in _lifecycle_files:
            return True
        fd = None
        try:
            for directory in (worktrees_root(), _meta_dir()):
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                st = directory.lstat()
                if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
                    return False
            path = _meta_dir() / f"{target.name}.lock"
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
            st, named = os.fstat(fd), path.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077 or (st.st_dev, st.st_ino) != (named.st_dev, named.st_ino):
                raise OSError("untrusted lifecycle lock")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _lifecycle_files[key] = fd
            return True
        except OSError:
            if fd is not None:
                os.close(fd)
            return False


def release_lifecycle(worktree_path: str) -> None:
    """Release at session end, never on client detach."""
    with _lifecycle_mutex:
        fd = _lifecycle_files.pop(str(Path(worktree_path).absolute()), None)
        if fd is not None:
            os.close(fd)


def _git_text(cwd: str, *args: str) -> str | None:
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10)
        return proc.stdout.strip() if proc.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _verified_record(path: str, meta: dict | None) -> bool:
    if not meta or not is_own_record(meta):
        return False
    named = record_machine(meta)
    if named and named != lore_sync_mod.machine_id():
        return False
    main, sid = str(meta.get("main_root") or ""), str(meta.get("session_id") or "")
    branch, pin = str(meta.get("branch") or ""), str(meta.get("base_oid") or "")
    base = str(meta.get("base_ref") or "")
    if not base or len(base) > 200 or base.startswith("-") or ".." in base or not re.fullmatch(r"[0-9A-Za-z_./-]+", base):
        return False
    try:
        expected = worktrees_root().resolve() / f"{Path(main).name}-{_short_id(sid)}"
        if Path(path).resolve() != expected or not sid or branch != f"doxa/{_short_id(sid)}" or meta.get("base_ref") == branch:
            return False
        if Path(peers_mod.main_repo_root_of(path) or "").resolve() != Path(main).resolve():
            return False
    except OSError:
        return False
    return (bool(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", pin))
            and _existing_worktree_path(main, branch) == path
            and _git_text(path, "symbolic-ref", "--quiet", "--short", "HEAD") == branch
            and _git_text(main, "merge-base", "--is-ancestor", pin, branch) is not None)


def managed_record_present(worktree_path: str) -> bool:
    """Detect a sidecar including unreadable records: startup must fail closed."""
    path = _meta_path(Path(worktree_path))
    return path.exists() or path.is_symlink()


@_serialized_lifecycle
def claim_lifecycle(worktree_path: str, session_id: str) -> bool:
    """Claim a verified existing checkout before a resumed daemon starts.

    False requires the caller to refuse using this managed checkout; a
    resume must never silently continue after failing its ownership lock.
    Plain directories are not claims and return False.
    """
    target = Path(worktree_path)
    held = str(target.absolute()) in _lifecycle_files
    if not _acquire_lifecycle(target):
        return False
    meta = read_meta(worktree_path)
    if meta and meta.get("session_id") == session_id and _verified_record(worktree_path, meta):
        return True
    if not held:
        release_lifecycle(worktree_path)
    return False


@_serialized_lifecycle
def create(cwd: str, session_name: str, base_branch: str | None = None) -> str | None:
    main = peers_mod.main_repo_root_of(cwd)
    if not enabled() or not main:
        return None
    target = worktrees_root() / f"{Path(main).name}-{_short_id(session_name)}"
    held = str(target.absolute()) in _lifecycle_files
    if not _acquire_lifecycle(target):
        return None
    try:
        result = _create_locked(cwd, session_name, base_branch)
        if result is not None:
            return result
    except BaseException:
        if not held:
            release_lifecycle(str(target))
        raise
    if not held:
        release_lifecycle(str(target))
    return None


def _write_meta(target: Path, **fields: str) -> None:
    tmp = _meta_dir() / f".{target.name}.{uuid.uuid4().hex}.tmp"
    fd = None
    try:
        st = _meta_dir().lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
            return
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = None
            json.dump(fields, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, _meta_path(target))
    except OSError:
        pass  # Never remove a checkout whose sidecar could not be verified.
    finally:
        if fd is not None:
            os.close(fd)
        with contextlib.suppress(OSError):
            tmp.unlink()


def read_meta(worktree_path: str) -> "dict | None":
    fd = None
    try:
        path = _meta_path(Path(worktree_path))
        named = path.lstat()
        if not stat.S_ISREG(named.st_mode) or named.st_uid != os.geteuid() or named.st_mode & 0o077 or named.st_size > 65536:
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        opened = os.fstat(fd)
        if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
            return None
        raw = os.read(fd, 65537)
        if len(raw) > 65536:
            return None
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    finally:
        if fd is not None:
            os.close(fd)
    return data if isinstance(data, dict) else None


def meta_file_path(worktree_path: str) -> Path:
    """Public wrapper on :func:`_meta_path` -- for a caller (``doxa.app``'s
    GitLine) that wants to mtime-guard its OWN re-reads of :func:`read_meta`
    the same way it already guards HEAD/ref reads, without re-deriving the
    sidecar path itself."""
    return _meta_path(Path(worktree_path))


def record_machine(meta: "dict | None") -> "str | None":
    """The machine a sidecar NAMES, or None when it names none.

    None is not "unknown machine", it is "this record predates the
    question" -- every sidecar written with sync off, which is all of them
    on a machine that never opted in."""
    if not isinstance(meta, dict):
        return None
    return str(meta.get("machine_id") or "") or None


def is_own_record(meta: "dict | None") -> bool:
    """Is this sidecar THIS machine's to act on? (sync.md's "## DOXA" item 2.)

    True in every case a 1.9.2 DOXA ever saw, and that is the point rather
    than an oversight: a sidecar with no ``machine_id`` is one written with
    sync off, answering False there would make :func:`finalize` stop
    cleaning up every worktree on every machine in the world, and
    ``tests/test_worktrees.py`` would say so immediately.

    False ONLY when the record names a machine AND this machine can prove
    it is a different one. A store with no op log, no ``sync_machine``
    table or no identity ever minted returns None from
    :func:`doxa.lore_sync.machine_id`, and unprovable reads as OURS --
    "keep" is the safe default here exactly as it is for a missing or
    unreadable sidecar (see :func:`finalize`'s meta-is-None case), because
    the cost of the two mistakes is not symmetric: wrongly keeping a
    worktree leaves a directory on disk, wrongly disowning one abandons a
    branch nobody will come back for."""
    named = record_machine(meta)
    if named is None:
        return True
    mine = lore_sync_mod.machine_id()
    return mine is None or named == mine


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


def current_branch(cwd: str) -> "str | None":
    """Public wrapper on :func:`_base_ref` -- the branch checked out at
    ``cwd`` right now (or the commit, detached). :func:`create` uses the
    private name for its own default; :func:`branch_status` uses this one
    for its "no session worktree" case, where the checked-out branch simply
    IS the base -- same as every session before v0.17."""
    return _base_ref(cwd)


def resolve_ref(main_root: str, ref: str) -> "str | None":
    """Validate ``ref`` as a spawn-time or switch-time base (item S): a
    local branch (checked directly), or a remote-tracking ref (``origin/
    foo``) resolved to the LOCAL semantics ``git worktree add``/``git
    rebase`` actually want.

    A bare ``origin/foo`` is a fine committish on its own, but basing a
    session off it directly forks from a detached, unnamed point with no
    branch to come back to later -- so when a LOCAL branch of the same
    short name already exists (``foo``), that is what gets returned
    instead. Only when no local branch exists at all does the
    remote-tracking ref itself come back, so the caller still has a real
    committish to hand to git rather than nothing.

    Returns the ref to actually use, or ``None`` when nothing matches --
    the caller's job is to fail with a message naming exactly what was
    tried, never to guess."""
    def _exists(candidate: str) -> bool:
        try:
            proc = subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", candidate],
                cwd=main_root, capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0

    if _exists(f"refs/heads/{ref}"):
        return ref
    if _exists(f"refs/remotes/{ref}"):
        short = ref.split("/", 1)[1] if "/" in ref else ref
        return short if _exists(f"refs/heads/{short}") else ref
    return None


def list_local_branches(main_root: str) -> list[str]:
    """Local branch names in display order (``git branch --format`` is
    already alphabetical) -- what ``/branch`` with no argument lists."""
    try:
        proc = subprocess.run(
            ["git", "branch", "--format=%(refname:short)"],
            cwd=main_root, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


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


def _create_locked(
    cwd: str, session_name: str, base_branch: "str | None" = None
) -> "str | None":
    """Give a session its own git worktree. Returns the worktree path to
    use as the session's cwd, or ``None`` -- the setting is off, ``cwd``
    is not a git repo (a worktree only means something inside one),
    ``base_branch`` was given but does not resolve (item S: ``doxa new
    --branch``), or ``git worktree add`` itself failed for a reason reuse
    doesn't already explain. ``None`` is always safe: the caller's
    fallback is running the session directly in ``cwd``, today's
    unchanged behavior -- ``doxa new --branch`` therefore validates the
    ref itself, up front, with its own actionable message BEFORE ever
    reaching this function (see doxa/cli.py), rather than relying on this
    permissive "None is safe" contract to explain an explicit flag's
    failure.

    ``base_branch``, when given, forks the worktree from THAT ref
    (resolved through :func:`resolve_ref`) instead of whatever ``cwd`` has
    checked out -- explicit spawn-time branch selection, item S #1."""
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
        meta = read_meta(existing)
        return existing if meta and meta.get("session_id") == str(session_name) and _verified_record(existing, meta) else None

    if _meta_path(target).exists() or _meta_path(target).is_symlink():
        return None

    if base_branch:
        base = resolve_ref(main_root, base_branch)
    else:
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
        return None

    fields = {
        "main_root": main_root, "branch": branch, "base_ref": base,
        "session_id": str(session_name),
        "base_oid": _git_text(str(target), "rev-parse", "HEAD"),
    }
    if lore_sync_mod.worktrees_enabled():
        # sync.md item 2: "the .meta sidecar gains machine_id". Written only
        # with the opt-in ``worktrees`` class switched on, so a 1.9.2 sidecar
        # and a sync-off sidecar written today are the same four fields --
        # the regression bar tests/test_worktrees.py holds. ``create=True``
        # for the reason doxa.tabsets.save gives at its own call: this is a
        # write path that has already proved the class is on.
        #
        # It rides along through update_base's whole-dict round trip for
        # free, and that is precisely why _write_meta stayed generic
        # (``**fields``) rather than growing a named parameter per key.
        mine = lore_sync_mod.machine_id(create=True)
        if mine:
            fields["machine_id"] = mine
    _write_meta(target, **fields)
    return str(target)


#: The subdirectories of a repository's COMMON git directory that a commit
#: made inside a linked worktree writes into -- the whole list, and no more
#: than the list. ``objects`` takes the new blob, tree and commit object;
#: ``refs`` takes the branch update; ``logs`` takes that update's reflog
#: entry. The per-worktree administrative directory (``index``,
#: ``index.lock``, ``HEAD``, ``COMMIT_EDITMSG``) is not spelled here
#: because :func:`external_git_roots` asks git where it is rather than
#: composing the path itself.
COMMIT_COMMON_SUBDIRS = ("objects", "refs", "logs")


def external_git_roots(cwd: str) -> list[str]:
    """Every directory OUTSIDE ``cwd`` that a ``git commit`` run inside
    ``cwd`` has to write to. Empty unless ``cwd`` is a linked worktree.

    A LINKED WORKTREE -- doxa's own (see :func:`create`) or one the user
    made by hand -- keeps its administrative files in the MAIN repository
    under ``.git/worktrees/<name>/``, with the object database and the
    branch refs one level above that. A sandbox whose writable root is the
    session's cwd therefore fails at ``index.lock`` before it ever reaches
    the commit (issue #57)::

        fatal: Unable to create '<main>/.git/worktrees/<n>/index.lock':
        Read-only file system

    The list is derived from git, never guessed: one ``git rev-parse``
    reports the per-worktree git directory and the common one, and the
    three names in :data:`COMMIT_COMMON_SUBDIRS` hang off the latter.
    Anything already inside ``cwd``, and anything not on disk, is dropped
    -- a root that does not exist is a rule a sandbox may refuse, and
    every one of these exists from the moment ``git worktree add -b``
    created the branch.

    **The common directory ITSELF is never returned, and that omission is
    the security boundary of this function.** Returning it would hand the
    session ``.git/hooks`` -- scripts the user's own next git command runs
    outside any sandbox -- and ``.git/config``, whose ``core.editor``,
    ``core.fsmonitor`` and credential-helper rows are execution channels
    of their own. What comes back instead can hold objects, move refs and
    write reflogs: destructive to history, recoverable from it, and
    incapable of running anything.

    **An ordinary checkout gets ``[]``, and not because it needs nothing.**
    Measured against codex-cli 0.144.4, a ``workspace-write`` sandbox marks
    the workspace's own git directory read-only even when everything around
    it is writable, so ``git add`` in a plain checkout fails the same way
    (``fatal: Unable to create '<repo>/.git/index.lock': Read-only file
    system``). There the index IS ``.git/index``, so the only grant that
    would fix it is ``.git`` whole -- hooks, config and all -- which is
    exactly the grant the paragraph above refuses. A linked worktree is the
    layout in which the narrow answer EXISTS, and that is why this function
    gives one only there. With ``worktree_per_session`` off a Codex session
    still cannot commit; the remedy is to leave worktrees on, not to widen
    further.

    One measured consequence of stopping at those four: ``packed-refs.lock``
    sits in the common directory, so git may warn that it cannot take it
    (a live fleet worker reported exactly that). The commit still lands --
    git writes a LOOSE ref under the granted ``refs/``, which is the normal
    path anyway -- and the warning is the honest price of not handing over
    the directory that also holds ``hooks``.

    ``[]`` on any failure at all -- no git, not a repository, an
    unparseable answer -- because the caller's fallback for ``[]`` is the
    sandbox it already had, and refusing to widen is always the safe
    direction."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute",
             "--git-dir", "--git-common-dir"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if len(lines) != 2:
        return []

    workspace = os.path.realpath(cwd)
    git_dir, common_dir = (os.path.realpath(line) for line in lines)

    def outside(path: str) -> bool:
        try:
            return os.path.commonpath([workspace, path]) != workspace
        except ValueError:
            return False  # unrelated roots cannot be compared; do not widen

    if not outside(git_dir):
        return []  # ordinary checkout: see the docstring's last paragraph
    candidates = [git_dir] + [
        os.path.join(common_dir, name) for name in COMMIT_COMMON_SUBDIRS
    ]
    return [
        path for path in dict.fromkeys(candidates)
        if outside(path) and os.path.isdir(path)
    ]


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


def commits_ahead(
    worktree_path: str, base_ref: str, branch: "str | None" = None
) -> "int | None":
    """How many commits ``branch`` carries beyond ``base_ref``.
    ``None`` when it cannot be measured (the base ref is gone, e.g.) --
    finalize treats that the same as "ahead", never as zero.

    ``branch`` is the SIDECAR'S recorded branch, and passing it is what
    makes the answer about the session's work rather than about wherever
    the worktree's HEAD happens to point. A ``git checkout`` inside the
    worktree moves HEAD off ``doxa/<short>`` -- detaching onto the base to
    read something, or switching to another branch -- and
    ``base_ref..HEAD`` then counts 0 while the session's commits sit safe
    on a branch nobody asked about. :func:`finalize` reads that 0 as
    "nothing unmerged" and REMOVES the worktree and deletes the branch,
    which is the one outcome this whole keep-or-remove decision exists to
    prevent.

    ``None`` (the default) keeps the old HEAD-relative meaning for a
    caller that genuinely means "here", and there is one: ``switch_base``
    asks about the checkout it is holding still."""
    target = branch or "HEAD"
    try:
        proc = subprocess.run(
            ["git", "rev-list", "--count", f"{base_ref}..{target}"],
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


@_serialized_lifecycle
def update_base(worktree_path: str, base_ref: str) -> bool:
    """Rewrite the sidecar's ``base_ref`` after a successful ``/branch``
    switch (item S #2) -- same atomic tmp+replace write :func:`_write_meta`
    already uses for ``create``, preserving every other field. Returns
    whether the write succeeded; a failure here just means the tab label
    lags until the sidecar is next touched -- never a reason to undo the
    git side, which has already landed by the time this is called."""
    held = str(Path(worktree_path).absolute()) in _lifecycle_files
    if not _acquire_lifecycle(Path(worktree_path)):
        return False
    try:
        return _update_base_locked(worktree_path, base_ref)
    finally:
        if not held:
            release_lifecycle(worktree_path)


def _update_base_locked(worktree_path: str, base_ref: str) -> bool:
    meta = read_meta(worktree_path)
    if not _verified_record(worktree_path, meta):
        return False
    meta["base_ref"] = base_ref
    meta["base_oid"] = _git_text(worktree_path, "rev-parse", base_ref)
    _write_meta(Path(worktree_path), **meta)
    return read_meta(worktree_path) == meta


def branch_status(cwd: str) -> dict:
    """``/branch`` with no argument (item S #2): every local branch, and
    which one is the CURRENT BASE -- the worktree sidecar's ``base_ref``
    inside a worktree-per-session session, or simply the checked-out
    branch otherwise (worktree_per_session off, or this cwd was never a
    doxa worktree: the checked-out branch just IS the base, same as every
    session before v0.17). Read-only; never mutates anything.

    The session's OWN branch is NOT among the candidates: ``doxa/<id>`` is
    session IDENTITY, never a base to fork from, and offering it was a
    data-loss defect (see :func:`switch_base`'s own guard, which is the
    load-bearing one -- this merely keeps the picker from showing a row
    that can only ever be refused). Outside a worktree-per-session session
    there is no such branch, so nothing is filtered and the checked-out
    branch keeps appearing in its own listing, marked as the base."""
    main_root = peers_mod.main_repo_root_of(cwd)
    if not main_root:
        return {"branches": [], "base": None, "checked_out": None}
    meta = read_meta(cwd)
    base_ref = str(meta.get("base_ref") or "") if meta else ""
    own = str(meta.get("branch") or "") if meta else ""
    checked_out = current_branch(cwd)
    return {
        "branches": [b for b in list_local_branches(main_root) if b != own],
        "base": base_ref or checked_out,
        "checked_out": checked_out,
    }


@_serialized_lifecycle
def switch_base(worktree_path: str, new_base: str) -> dict:
    if read_meta(worktree_path) is None:
        return _switch_base_locked(worktree_path, new_base)
    held = str(Path(worktree_path).absolute()) in _lifecycle_files
    if not _acquire_lifecycle(Path(worktree_path)):
        return {"ok": False, "base": None, "message": "worktree lifecycle lock unavailable"}
    try:
        if not _verified_record(worktree_path, read_meta(worktree_path)):
            return {"ok": False, "base": None, "message": "worktree ownership or pinned base cannot be verified"}
        return _switch_base_locked(worktree_path, new_base)
    finally:
        if not held:
            release_lifecycle(worktree_path)


def _switch_base_locked(worktree_path: str, new_base: str) -> dict:
    """``/branch <name>`` (item S #2): rebase the session's OWN worktree
    branch onto ``new_base``.

    FREE (a fast-forward, no history to replay) only when the worktree is
    CLEAN and carries ZERO commits ahead of its CURRENT base -- the exact
    same test :func:`finalize` already applies at session end, reused here
    for the same reason: dirty or committed-but-unmerged work is real
    work, and this command must never silently carry it across a base
    switch any more than finalize silently discards it. Both refusals
    point at that convention by name (``kept <branch> — merge when
    ready``) rather than inventing a second vocabulary for the same rule.

    Returns ``{"ok": bool, "message": str, "base": str | None}`` --
    ``message`` is always something the caller can show verbatim, success
    or refusal; ``base`` carries the new base ref only on success."""
    meta = read_meta(worktree_path)
    if meta is None:
        return {
            "ok": False, "base": None,
            "message": (
                "no doxa worktree here (worktree_per_session is off, or "
                "this session never got one) -- switching branch would "
                "move your ACTUAL checkout, which this command refuses "
                "to do silently; use `git checkout` directly instead"
            ),
        }
    main_root = str(meta.get("main_root") or "")
    branch = str(meta.get("branch") or "")
    old_base = str(meta.get("base_ref") or "")
    if not (main_root and branch and old_base):
        return {
            "ok": False, "base": None,
            "message": "worktree metadata incomplete -- cannot switch safely",
        }
    resolved = resolve_ref(main_root, new_base)
    if resolved is None:
        return {
            "ok": False, "base": None,
            "message": f"no such branch: {new_base!r}",
        }
    if resolved == branch:
        # A branch is never "ahead" of ITSELF, so accepting this would
        # write a sidecar whose base_ref can only ever measure zero --
        # and finalize's "clean and zero ahead" test would then read real,
        # unmerged commits as nothing to keep and `git branch -D` them at
        # session end. The whole point of this command's refusals is that
        # work is never lost silently; this is the one target that would
        # disarm them all, so it is refused before the rebase, not after.
        return {
            "ok": False, "base": None,
            "message": (
                f"{branch} is this session's own branch, not a base to "
                "fork from -- basing it on itself would leave nothing to "
                "measure unmerged work against, and session end would "
                "then delete that work as if it were already merged. Pick "
                f"the branch you want to be based ON (currently {old_base})."
            ),
        }
    if not is_clean(worktree_path):
        return {
            "ok": False, "base": None,
            "message": (
                f"{branch} has uncommitted changes -- switching base would "
                "carry them across silently. Commit or stash first (dirty "
                f"work is always kept, never carried -- same rule as "
                f"'kept {branch} — merge when ready' at session end)."
            ),
        }
    ahead = commits_ahead(worktree_path, old_base)
    if ahead != 0:
        note = "an unmeasurable number of commits" if ahead is None else f"{ahead} commit(s)"
        return {
            "ok": False, "base": None,
            "message": (
                f"{branch} is {note} ahead of {old_base} -- switching base "
                "would rebase real work without being asked. Merge it "
                f"first (same rule as 'kept {branch} — merge when ready' "
                "at session end)."
            ),
        }
    try:
        proc = subprocess.run(
            ["git", "rebase", resolved],
            cwd=worktree_path, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "base": None, "message": f"rebase failed: {exc}"}
    if proc.returncode != 0:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["git", "rebase", "--abort"], cwd=worktree_path,
                capture_output=True, text=True, timeout=10,
            )
        return {
            "ok": False, "base": None,
            "message": f"rebase onto {resolved} failed: {proc.stderr.strip()[:300]}",
        }
    update_base(worktree_path, resolved)
    return {
        "ok": True, "base": resolved,
        "message": f"{branch} now based on {resolved}",
    }


def _remove(main_root: str, worktree_path: str, branch: str) -> bool:
    before = _git_text(main_root, "rev-parse", "--verify", branch)
    if not before or _git_text(worktree_path, "status", "--porcelain", "--ignored", "--untracked-files=all") != "":
        return False
    if _git_text(main_root, "worktree", "remove", worktree_path) is None:
        return False
    return _git_text(main_root, "update-ref", "-d", f"refs/heads/{branch}", before) is not None


FINALIZE_RULE = (
    "a worktree that is completely clean with zero commits ahead of its "
    "base is removed with no trace when the session ends; any "
    "uncommitted change, or any committed-but-unmerged work, is kept for "
    "manual merge"
)
"""One sentence, the whole clean/ahead rule :func:`finalize` applies below
-- the SINGLE source ``doxa.engine``'s ``[SESSION WORKTREE]`` prompt block
quotes for its removal warning, so the two can never drift apart the way a
second hand-written copy of this rule would let them. ``tests/
test_worktrees.py::test_finalize_rule_text_matches_finalize_behavior``
exercises :func:`finalize` under all three outcomes this sentence claims
(clean+zero-ahead removed, dirty kept, clean-but-ahead kept) and fails if
the code and the sentence disagree -- so a change to the logic below that
is not matched by an edit HERE breaks a test before it ever reaches the
model's context, in engine.py or anywhere else that imports this name."""


@_serialized_lifecycle
def finalize(worktree_path: str) -> "str | None":
    meta = read_meta(worktree_path)
    if meta is None or not is_own_record(meta):
        release_lifecycle(worktree_path)
        return None
    if not _acquire_lifecycle(Path(worktree_path)):
        return "worktree lifecycle lock unavailable; kept it"
    try:
        if not _verified_record(worktree_path, read_meta(worktree_path)):
            return f"kept {meta.get("branch") or worktree_path} — merge when ready (ownership or pinned base cannot be verified)"
        return _finalize_locked(worktree_path)
    finally:
        release_lifecycle(worktree_path)


def _finalize_locked(worktree_path: str) -> "str | None":
    """A session's REAL end (never a mere detach): clean up its worktree,
    or say why it was kept. See the module docstring for the clean/dirty
    rule. ``None`` means "nothing to report" -- either the worktree was
    removed with no trace, or this was never a doxa-managed worktree
    (no metadata: the setting was off for this session, or the sidecar
    was lost) and is left completely untouched."""
    meta = read_meta(worktree_path)
    if meta is None:
        return None
    if not is_own_record(meta):
        # sync.md item 2: "finalize ignores records that are not its own."
        # Another machine's worktree is not on this disk -- the directory
        # its sidecar names lives over there -- so every git call below
        # would either fail outright or, far worse, act on an unrelated
        # directory of the same name that happens to exist here, and
        # ``git branch -D`` is not an operation to run on a guess. Left
        # completely alone with nothing reported, the same answer a
        # directory that was never a doxa worktree already gets.
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
    if base_ref == branch:
        # A sidecar that already records the branch as its own base --
        # written by the version that let /branch accept it. `rev-list
        # branch..HEAD` is structurally 0 there, which would read as
        # "nothing unmerged" and delete the branch outright. Unmeasurable
        # is the honest answer, and this function already treats that as
        # "keep", so an operator who hit the old bug still gets their work
        # back instead of losing it on the next session end.
        ahead = None
    else:
        # Counted from the RECORDED branch, not from HEAD: a checkout
        # inside the worktree moves HEAD off doxa/<short> and
        # base_ref..HEAD then reads 0 for a branch carrying real commits.
        # See commits_ahead -- this is the call whose wrong answer deletes
        # the work.
        ahead = commits_ahead(worktree_path, base_ref, branch)
    if clean and ahead == 0:
        if _remove(main_root, worktree_path, branch):
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
        if not is_own_record(data):
            # sync.md item 2: another machine's record is not an orphan
            # HERE, and -- the load-bearing half -- it is not a PATH here
            # either. It is reported by :func:`list_remote` instead, which
            # hands back no path at all. Filtered before the is_dir() test
            # below on purpose: a foreign record whose name collides with a
            # local directory would otherwise pass that test and be
            # reported as an openable orphan of this machine's.
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


def list_remote() -> list[dict]:
    """Every worktree record here that belongs to ANOTHER machine.

    sync.md item 2: "the sidebar may list a worktree belonging to another
    machine as *remote*, and must never offer it as a path to open." The
    second half is enforced STRUCTURALLY rather than by a rule a caller has
    to remember: these dicts carry no ``path`` key at all. A caller cannot
    offer what it was never handed, so a future picker row built from this
    cannot quietly become an open action in the hands of someone wiring it
    up without reading this docstring -- which is the same reasoning
    :func:`doxa.remote_policy.identity_decision` applies to an empty
    allow-list, and the opposite of the direction such things usually fail.

    The omission is not squeamishness, it is correctness: the directory the
    record names is on the other machine's disk. A path string here would
    be a path into THIS filesystem, which either does not exist or -- much
    worse, since the name is ``<repo>-<short>`` and both machines check out
    the same repos -- is an unrelated local directory of the same name.

    ``name`` is the sidecar's own stem, which is what the record is called
    on both machines; ``machine`` is the id to render the row as "on
    <machine>". Empty on every machine with sync off, because nothing
    writes a foreign record there -- so this costs a 1.9.2 DOXA exactly
    what the rest of this feature costs it, which is nothing."""
    meta_dir = _meta_dir()
    if not meta_dir.is_dir():
        return []
    remote: list[dict] = []
    for meta_path in sorted(meta_dir.glob("*.json")):
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or is_own_record(data):
            continue
        remote.append({
            "name": meta_path.stem,
            "branch": str(data.get("branch") or ""),
            "machine": record_machine(data) or "",
            "session_id": str(data.get("session_id") or ""),
        })
    return remote
