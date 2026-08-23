"""doxa.update -- ``/update``: fast-forward this checkout, and say what moved.

Deliberately narrow. It runs ``git pull --ff-only`` from ``origin`` and
nothing else: no merge, no rebase, no force, no history rewritten, ever. A
tree that cannot fast-forward is a tree with local work in it, and a
terminal that quietly resolves that for you is a terminal that will one day
lose something.

Refusals come FIRST and are explicit -- a dirty tree, or a copy that is not
a git checkout at all (installed from a wheel, where `git pull` is
meaningless). Both name what to do instead.

When the pull moves ``pyproject.toml`` or ``uv.lock``, the dependencies
changed and ``uv sync`` RUNS -- printing "run uv sync yourself" is how a
version ends up with half its dependencies. Its output is streamed back to
the caller rather than swallowed.

Live sessions are never restarted behind the user's back: the report says
what to do, and ``/update --restart`` is the explicit opt-in that stops
this window's sessions and relaunches.

Everything shells out through one injectable ``run`` callable, which is
what lets the tests drive every branch (refusal, up-to-date, a real pull,
the uv-sync path) without a network or a second repository.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import version as version_mod

# A pull that touches either of these changed what DOXA depends on.
DEPENDENCY_FILES = ("pyproject.toml", "uv.lock")

GIT_TIMEOUT_SECS = 120.0
SYNC_TIMEOUT_SECS = 600.0


@dataclass
class UpdateReport:
    """What happened, in the words the block will print."""

    status: str
    """refused | up-to-date | updated"""

    message: str
    """One line for the headline -- always set, always the first thing."""

    commits: list[str] = field(default_factory=list)
    version_before: str = ""
    version_after: str = ""
    synced: bool = False
    sync_output: str = ""

    def text(self) -> str:
        lines = [self.message]
        if self.commits:
            noun = "commit" if len(self.commits) == 1 else "commits"
            lines.append("")
            lines.append(f"{len(self.commits)} {noun} pulled:")
            lines += [f"  {c}" for c in self.commits]
        if self.version_before and self.version_after:
            if self.version_before != self.version_after:
                lines.append("")
                lines.append(
                    f"version  {self.version_before} → {self.version_after}"
                )
            else:
                lines.append("")
                lines.append(f"version  {self.version_after} (unchanged)")
        if self.synced:
            lines.append("")
            lines.append("dependencies changed — uv sync:")
            lines += [f"  {line}" for line in self.sync_output.splitlines()]
        if self.status == "updated":
            lines.append("")
            lines.append(
                "running sessions keep the code they started with — restart "
                "this window to pick the update up (/update --restart does it "
                "for you, stopping this window's sessions first)"
            )
        return "\n".join(lines)


def _run(cmd: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(cmd), cwd=str(cwd), capture_output=True, text=True, timeout=timeout
    )


def check_for_update(root: "Path | None" = None, run=_run) -> bool:
    """True when the checkout DOXA is running from has commits upstream it
    has not pulled yet -- the boot-time check behind the "DOXA update
    available" notification, deliberately read-only (a ``git fetch``
    updates remote-tracking refs, nothing local).

    Advisory only: EVERY failure -- not a checkout, no network, no
    upstream configured for the current branch, git missing -- reads as
    "nothing to report" rather than raising, because this runs from a
    background worker at boot and must never be the thing that makes
    startup noisy or slow over a flaky connection."""
    root = root or version_mod.source_root()
    if root is None or not (Path(root) / ".git").exists():
        return False
    root = Path(root)
    try:
        fetched = run(["git", "fetch", "--quiet"], root, GIT_TIMEOUT_SECS)
        if fetched.returncode != 0:
            return False
        counted = run(
            ["git", "rev-list", "--count", "HEAD..@{upstream}"],
            root, GIT_TIMEOUT_SECS,
        )
        if counted.returncode != 0:
            return False
        return int(counted.stdout.strip() or "0") > 0
    except Exception:  # noqa: BLE001 -- offline, no git binary, a timeout:
        # all the same "nothing to report" to a background boot check.
        return False


def update(root: "Path | None" = None, run=_run) -> UpdateReport:
    """Fast-forward the checkout DOXA is running from and report on it."""
    root = root or version_mod.source_root()
    if root is None or not (Path(root) / ".git").exists():
        return UpdateReport(
            status="refused",
            message=(
                "update: this DOXA is not a git checkout (installed copy) — "
                "reinstall it the way you installed it; /update only "
                "fast-forwards a checkout"
            ),
        )
    root = Path(root)

    try:
        dirty = run(["git", "status", "--porcelain"], root, GIT_TIMEOUT_SECS)
    except Exception as exc:  # noqa: BLE001 -- a broken git is information
        return UpdateReport(status="refused", message=f"update: {exc}")
    if dirty.returncode != 0:
        return UpdateReport(
            status="refused",
            message=f"update: git refused to read the tree — {dirty.stderr.strip()}",
        )
    if dirty.stdout.strip():
        # Porcelain is "XY <path>": the two status columns are fixed-width
        # and the FIRST one is often a space, so the lines are split before
        # anything is stripped -- stripping first eats a column and
        # truncates the path by a character.
        changed = [
            line[2:].strip() for line in dirty.stdout.splitlines() if line.strip()
        ][:10]
        return UpdateReport(
            status="refused",
            message=(
                "update: the checkout has uncommitted changes — commit or "
                "stash them first, /update never touches your work:\n  "
                + "\n  ".join(changed)
            ),
        )

    before = run(["git", "rev-parse", "HEAD"], root, GIT_TIMEOUT_SECS).stdout.strip()
    version_before = version_mod.resolve_version()

    pulled = run(["git", "pull", "--ff-only", "origin"], root, GIT_TIMEOUT_SECS)
    if pulled.returncode != 0:
        return UpdateReport(
            status="refused",
            message=(
                "update: fast-forward refused — your checkout has diverged "
                "from origin, and /update will not merge or rebase for you:\n"
                + (pulled.stderr.strip() or pulled.stdout.strip())
            ),
        )

    after = run(["git", "rev-parse", "HEAD"], root, GIT_TIMEOUT_SECS).stdout.strip()
    if not after or after == before:
        return UpdateReport(
            status="up-to-date",
            message=f"update: already up to date ({version_before})",
        )

    log = run(
        ["git", "log", "--oneline", f"{before}..{after}"], root, GIT_TIMEOUT_SECS
    )
    commits = [line for line in log.stdout.splitlines() if line.strip()]

    names = run(
        ["git", "diff", "--name-only", before, after], root, GIT_TIMEOUT_SECS
    ).stdout.split()
    version_mod.source_sha.cache_clear()
    report = UpdateReport(
        status="updated",
        message=f"update: fast-forwarded {before[:7]} → {after[:7]}",
        commits=commits,
        version_before=version_before,
        version_after=version_mod.resolve_version(),
    )
    if any(name in DEPENDENCY_FILES for name in names):
        sync = run(["uv", "sync"], root, SYNC_TIMEOUT_SECS)
        report.synced = True
        report.sync_output = (sync.stdout + sync.stderr).strip() or "(no output)"
    return report
