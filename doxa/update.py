# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.update -- ``/update`` for a checkout or the documented uv tool install.

Deliberately narrow. It runs ``git pull --ff-only`` from ``origin`` and
nothing else: no merge, no rebase, no force, no history rewritten, ever. A
tree that cannot fast-forward is a tree with local work in it, and a
terminal that quietly resolves that for you is a terminal that will one day
lose something.

An installed copy is updated only when its running interpreter is DOXA's
uv-tool environment and its receipt and wheel metadata both name this
project's Git source. An unpinned receipt (the installer default) or an
explicit main ref may advance; a pinned tag/branch/commit never silently
becomes main. ``uv tool upgrade --reinstall`` refreshes the Git source and
keeps the receipt's extras and other requirements.

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

import os
import json
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from email.parser import Parser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import version as version_mod

# A pull that touches either of these changed what DOXA depends on.
DEPENDENCY_FILES = ("pyproject.toml", "uv.lock")

GIT_TIMEOUT_SECS = 120.0
"""Ceiling for ``/update``'s OWN git work -- the fetch-and-fast-forward a
user explicitly asked for and is watching. Two minutes is generous
because that pull is the whole point of the command; abandoning it early
would be the worse failure."""

CHECK_TIMEOUT_SECS = 10.0
"""Ceiling for the boot-time :func:`check_for_update` probe, which nobody
asked for and nobody is watching. It ran on ``GIT_TIMEOUT_SECS`` through
v1.7.4, which meant a checkout whose remote was unreachable (a VPN not up
yet, DNS still settling, a laptop that woke on a captive portal) held a
worker thread and a live ``git`` child for two minutes past launch -- and,
because ``asyncio``'s default executor is joined at loop shutdown, could
push app exit out by the same amount. An advisory "there is something to
pull" that has not answered in ten seconds has already missed the moment
it was for."""

SYNC_TIMEOUT_SECS = 600.0


@dataclass(frozen=True)
class ToolInstall:
    prefix: Path
    version: str
    revision: str
    packages: tuple[tuple[str, str, str], ...]


def _doxa_git_url(value: str) -> bool:
    """Accept only DOXA's public Git source, without credentials or ports."""
    url = urlsplit(value)
    return (
        url.scheme == "https" and url.netloc == "github.com"
        and url.path.rstrip("/") in ("/docwilde/doxa", "/docwilde/doxa.git")
        and not url.fragment
    )


def _tool_install(run) -> "tuple[ToolInstall | None, str]":
    """Identify the *running* uv tool from its receipt and wheel provenance.

    A mere ``uv tool list`` entry could be a different DOXA from the one
    this process loaded. The prefix, uv's tool directory, loaded module,
    receipt and installed wheel must all point to the same copy before
    ``/update`` is allowed to mutate anything.
    """
    prefix = Path(sys.prefix).resolve()
    try:
        listed = run(["uv", "tool", "dir"], prefix, CHECK_TIMEOUT_SECS)
        tool_dir = Path(listed.stdout.strip()).resolve()
        if listed.returncode or not listed.stdout.strip():
            raise ValueError("uv tool dir did not return a directory")
        if prefix != (tool_dir / "doxa").resolve():
            raise ValueError("the running Python is not uv's DOXA tool")
        if not Path(__file__).resolve().is_relative_to(prefix):
            raise ValueError("the running DOXA package is outside that tool")

        receipt = tomllib.loads(
            (prefix / "uv-receipt.toml").read_text(encoding="utf-8")
        )
        requirements = receipt["tool"]["requirements"]
        sources = [r for r in requirements if r.get("name") == "doxa"]
        if len(sources) != 1 or not isinstance(sources[0].get("git"), str):
            raise ValueError("the uv receipt has no single DOXA Git source")
        source = sources[0]["git"]
        if not _doxa_git_url(source):
            raise ValueError("the uv receipt names a different Git source")
        query = parse_qs(urlsplit(source).query, keep_blank_values=True)
        if query not in ({}, {"rev": ["main"]}):
            ref = query.get("rev", ["a custom ref"])[0]
            raise ValueError(
                f"this uv tool install is pinned to {ref!r}; "
                "reinstall that ref explicitly to update it"
            )

        # uv records the resolved commit in PEP 610 direct_url.json. The
        # receipt alone describes intent; this file proves what is on disk.
        state = _tool_state(prefix)
        if state is None:
            raise ValueError("the installed DOXA wheel has no verifiable Git revision")
        return state, ""
    except (
        OSError, ValueError, KeyError, TypeError, AttributeError, IndexError,
        subprocess.SubprocessError,
    ) as exc:
        return None, str(exc)


def _tool_state(prefix: Path) -> "ToolInstall | None":
    infos = list(prefix.glob("lib/python*/site-packages/doxa-*.dist-info"))
    infos += list(prefix.glob("Lib/site-packages/doxa-*.dist-info"))
    if len(infos) != 1:
        return None
    try:
        metadata = Parser().parsestr(
            (infos[0] / "METADATA").read_text(encoding="utf-8"), headersonly=True
        )
        version = metadata.get("Version", "").strip()
        direct = json.loads(
            (infos[0] / "direct_url.json").read_text(encoding="utf-8")
        )
        vcs = direct.get("vcs_info") or {}
        revision = vcs.get("commit_id", "")
        if (
            not version or not _doxa_git_url(direct.get("url", ""))
            or vcs.get("vcs") != "git" or not isinstance(revision, str)
            or len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision.lower())
        ):
            return None
        packages = []
        for info in infos[0].parent.glob("*.dist-info"):
            data = Parser().parsestr(
                (info / "METADATA").read_text(encoding="utf-8"),
                headersonly=True,
            )
            name = data.get("Name", "").strip().lower()
            found_version = data.get("Version", "").strip()
            if not name or not found_version:
                return None
            url_file = info / "direct_url.json"
            direct_revision = ""
            if url_file.is_file():
                source = json.loads(url_file.read_text(encoding="utf-8"))
                direct_revision = (source.get("vcs_info") or {}).get("commit_id", "")
            packages.append((name, found_version, direct_revision))
        return ToolInstall(prefix, version, revision, tuple(sorted(packages)))
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _update_tool(run) -> "UpdateReport":
    before, reason = _tool_install(run)
    if before is None:
        return UpdateReport(
            status="refused",
            message=(
                "update: this DOXA is not a supported uv tool install — "
                f"{reason or 'reinstall it the way you installed it'}"
            ),
        )
    try:
        upgraded = run(
            ["uv", "tool", "upgrade", "--reinstall", "doxa"],
            before.prefix, SYNC_TIMEOUT_SECS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        upgraded = subprocess.CompletedProcess([], 1, "", str(exc))
    after = _tool_state(before.prefix)
    old = f"{before.version} ({before.revision[:7]})"
    new = f"{after.version} ({after.revision[:7]})" if after else "unverified"
    if upgraded.returncode:
        detail = (upgraded.stderr or upgraded.stdout).strip()
        return UpdateReport(
            status="refused",
            message=f"update: uv tool upgrade failed; before {old}, now {new}\n{detail}",
        )
    if after is None:
        return UpdateReport(
            status="updated",
            message=("update: uv tool upgrade completed, but the installed "
                     f"revision could not be verified; before {old}"),
        )
    if (
        before.revision == after.revision and before.version == after.version
        and before.packages == after.packages
    ):
        return UpdateReport(
            status="up-to-date",
            message=f"update: uv tool install already up to date ({new})",
        )
    if before.revision == after.revision and before.version == after.version:
        return UpdateReport(
            status="updated",
            message=f"update: uv tool refreshed dependencies; DOXA remains {new}",
        )
    return UpdateReport(
        status="updated",
        message=f"update: uv tool upgraded DOXA {old} → {new}",
        version_before=before.version,
        version_after=after.version,
    )


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


def check_for_update(root: "Path | None" = None, run=None) -> bool:
    """True when the checkout DOXA is running from has commits upstream it
    has not pulled yet -- the boot-time check behind the "DOXA update
    available" notification, deliberately read-only (a ``git fetch``
    updates remote-tracking refs, nothing local).

    Advisory only: EVERY failure -- not a checkout, no network, no
    upstream configured for the current branch, git missing -- reads as
    "nothing to report" rather than raising, because this runs from a
    background worker at boot and must never be the thing that makes
    startup noisy or slow over a flaky connection.

    ``DOXA_SKIP_UPDATE_CHECK`` is a kill switch on the same discipline as
    ``DOXA_SKIP_FIRST_RUN`` / ``LORE_DISABLE_REVIEW`` / ``DOXA_IMAGE_MODE``
    (set suite-wide by tests/conftest.py, honored explicitly here rather
    than sniffed for anywhere): the suite runs FROM a checkout, so before
    v1.7.5 every one of its several hundred ``DoxaApp`` mounts opened a
    real network ``git fetch`` against origin. Measured on
    tests/test_app.py: 12 live fetches for 13 tests, 17.5s of subprocess
    time inside a 24.4s module. That is a suite whose wall clock is set by
    somebody's network, and a suite that fails differently offline. It
    reads as "nothing to report", the same as every other reason this
    function declines to answer -- a checkout with the var set is not
    claiming to be current, it is declining to look.

    ``run`` resolves to :func:`_run` at CALL time rather than defaulting to
    it in the signature, which is not a style preference: a default
    argument is bound once when the ``def`` executes, so
    ``monkeypatch.setattr(update_mod, "_run", ...)`` -- the seam every
    test in this module believes it has -- silently did nothing, and a
    test asserting "no git subprocess was reached" passed while a real
    ``git fetch`` ran underneath it. Resolving here makes the module
    attribute the seam it reads as."""
    if os.environ.get("DOXA_SKIP_UPDATE_CHECK", "").strip():
        return False
    run = run or _run
    root = root or version_mod.source_root()
    if root is None or not (Path(root) / ".git").exists():
        return False
    root = Path(root)
    try:
        fetched = run(["git", "fetch", "--quiet"], root, CHECK_TIMEOUT_SECS)
        if fetched.returncode != 0:
            return False
        counted = run(
            ["git", "rev-list", "--count", "HEAD..@{upstream}"],
            root, CHECK_TIMEOUT_SECS,
        )
        if counted.returncode != 0:
            return False
        return int(counted.stdout.strip() or "0") > 0
    except Exception:  # noqa: BLE001 -- offline, no git binary, a timeout:
        # all the same "nothing to report" to a background boot check.
        return False


def update(root: "Path | None" = None, run=_run) -> UpdateReport:
    """Advance the running checkout or verified uv tool install."""
    root = root or version_mod.source_root()
    if root is None or not (Path(root) / ".git").exists():
        return _update_tool(run)
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
