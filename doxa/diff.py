# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.diff -- the model half of the live diff.

Pure data, pure functions, and one thin subprocess boundary. The same
rule :mod:`doxa.layout` follows and for the same reason: the parts of a
diff that are hard to get right (which base, what "no changes" means as
against "cannot tell", what exactly a reverse patch of ONE hunk is) must
be testable without a running app -- and, for the reject path, against a
real git worktree rather than a mock, because the thing being asserted is
that git accepted the patch.

**Not a differ.** ``git diff``'s unified output is parsed here, never
re-implemented. The porcelain is stable, the alternative is writing a
differ, and a second differ would be a second source of truth: this
module renders what git says, and if it can show something git cannot, it
is wrong.

**The base is the worktree's, not HEAD's.** Every session runs in a
worktree on ``doxa/<session-id>`` (v0.17.0+) and the sidecar records the
branch it was cut from (:func:`doxa.worktrees.read_meta`). Diffing against
THAT is what a reviewer wants -- the session's own work, committed and
uncommitted alike -- and it is already the unit ``finalize`` reasons
about. See :func:`base_for` for the three answers that question has and
why two of them must not look alike on screen.

**Bounded, and it says when it truncated.** The diff never crosses the
daemon socket: git runs in the TUI process against a worktree on the same
machine, exactly like :mod:`doxa.transcript`'s restore reads the
transcript file directly. So ``peers.MAX_FRAME_BYTES`` and
``daemon._fit_page`` are not on this path -- there is no frame -- and
``_fit_page`` deliberately gains no caller here, for the reason that
module already gives: one implementation enforcing one budget, for the
calls that really are frames. What IS capped is the rendering, by
:data:`MAX_HUNK_LINES_PER_FILE` and :data:`MAX_FILES`, and a result that
hit either cap says so (:attr:`DiffResult.truncated`) rather than handing
back a short answer that renders as if it were whole.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from . import worktrees as worktrees_mod

#: Seconds any one git call gets. The same 10 :mod:`doxa.worktrees` uses
#: for ``status``/``rev-list``; a diff is not slower than a status on any
#: tree small enough to render.
GIT_TIMEOUT_SECS = 10

#: A file whose diff body is longer than this is NAMED, not rendered --
#: the spec's "binary and huge files are named, not rendered", with the
#: cutoff on the DIFF rather than on the file, because a 40 MB asset with
#: a one-line change is cheap and a 3 MB generated file rewritten whole is
#: not. Roughly a screenful times forty: past that nobody is reading it as
#: a diff, they are looking for the filename.
MAX_HUNK_LINES_PER_FILE = 2000

#: How many files one diff renders before it stops and says so. A branch
#: touching more than this is a branch you review with a real tool; the
#: pane's job is to stay honest about what it left out.
MAX_FILES = 200

#: Total diff-body lines across all files. The backstop that makes the
#: two caps above sufficient rather than merely likely -- two hundred
#: files of 1999 lines each is not a page.
MAX_TOTAL_LINES = 20000

#: How many untracked files are examined. Answering open question 2 (a
#: created file IS what a reviewer wants to see) costs one ``git diff
#: --no-index`` per file, so it is bounded like everything else. An
#: agent that created more than fifty files did something a diff pane is
#: not the right surface for.
MAX_UNTRACKED = 50

# -- the three answers to "what is the base" --------------------------
#
# These are a closed set and they are not interchangeable. v0.33.0
# measured the cost of conflating the last two: a base equal to the
# branch makes `commits_ahead` structurally unmeasurable, it read as
# zero, and finalize force-deleted real commits on the strength of it.
# The diff inherits the trap in a quieter form -- a diff against a base
# equal to the branch simply shows nothing -- so the states are named
# here and rendered differently by contract.

#: A base was determined and git answered. ``files`` may still be empty,
#: which means, and only means, NO CHANGES.
STATUS_OK = "ok"
#: No base could be determined. NOT the same statement as "no changes",
#: and the whole reason this constant exists rather than an empty file
#: list standing in for both.
STATUS_NO_BASE = "no-base"
#: git was asked and refused (a ref that no longer resolves, a cwd that
#: is not a repository, git missing). Also not "no changes".
STATUS_ERROR = "error"

#: The sidecar named a base and it is usable.
BASE_SIDECAR = "sidecar"
#: There is no sidecar -- worktree-per-session is off, or this cwd was
#: never a DOXA worktree. ``HEAD`` stands in, and the pane SAYS it is
#: standing in: uncommitted work against the current commit is a smaller
#: claim than the session's work against its branch point, and a reviewer
#: reading the wrong one silently is the failure this avoids.
BASE_HEAD = "head"


@dataclass(frozen=True)
class Hunk:
    """One ``@@`` block, exactly as git wrote it.

    ``lines`` are the raw body lines with their leading marker byte
    (``' '``, ``'+'``, ``'-'``, or ``'\\'`` for the no-newline note) still
    on them, because that is what a patch file is: reconstructing them
    from a parsed form is how a reverse patch stops applying."""

    header: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: "tuple[str, ...]" = ()

    @property
    def added(self) -> int:
        return sum(1 for ln in self.lines if ln.startswith("+"))

    @property
    def removed(self) -> int:
        return sum(1 for ln in self.lines if ln.startswith("-"))

    @property
    def label(self) -> str:
        """What the fold shows: the line range and the counts."""
        return (
            f"@@ -{self.old_start},{self.old_count} "
            f"+{self.new_start},{self.new_count} @@  "
            f"+{self.added} −{self.removed}"
        )


@dataclass(frozen=True)
class FileDiff:
    """One file's worth of diff.

    ``header`` is the preamble git emitted for this file -- the ``diff
    --git`` line through ``+++`` -- kept verbatim because it is the half
    of a patch that names the file, and :func:`hunk_patch` needs it back
    unchanged.

    ``skipped`` is non-empty when the file is NAMED rather than rendered:
    binary, or past :data:`MAX_HUNK_LINES_PER_FILE`. A skipped file still
    reports its path and, where git said so, its counts."""

    path: str
    header: "tuple[str, ...]" = ()
    hunks: "tuple[Hunk, ...]" = ()
    old_path: str = ""
    binary: bool = False
    untracked: bool = False
    skipped: str = ""

    @property
    def added(self) -> int:
        return sum(h.added for h in self.hunks)

    @property
    def removed(self) -> int:
        return sum(h.removed for h in self.hunks)

    @property
    def renamed(self) -> bool:
        return bool(self.old_path) and self.old_path != self.path

    def summary(self) -> str:
        """The collapsed line: what the user reads before expanding.

        A skipped file says WHY it is skipped instead of showing counts
        it does not have, because "binary" and "+0 −0" look the same at a
        glance and mean opposite things."""
        name = f"{self.old_path} → {self.path}" if self.renamed else self.path
        if self.skipped:
            return f"{name}  ({self.skipped})"
        mark = " (new file)" if self.untracked else ""
        return f"{name}{mark}  +{self.added} −{self.removed}"


@dataclass(frozen=True)
class DiffResult:
    """A whole diff, and the honest account of what it is.

    ``status`` is the load-bearing field. ``STATUS_OK`` with no files is
    "no changes"; ``STATUS_NO_BASE`` is "cannot tell". They are different
    statements and :meth:`headline` renders them differently."""

    status: str = STATUS_OK
    base: str = ""
    base_source: str = BASE_SIDECAR
    files: "tuple[FileDiff, ...]" = ()
    detail: str = ""
    truncated: str = ""
    dropped_files: int = 0

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def headline(self) -> str:
        """One line naming the state of the diff, never ambiguous between
        the two states the spec insists must differ."""
        if self.status == STATUS_NO_BASE:
            return f"cannot determine a base — {self.detail}"
        if self.status == STATUS_ERROR:
            return f"cannot read the diff — {self.detail}"
        against = (
            f"against {self.base}" if self.base_source == BASE_SIDECAR
            else f"against {self.base} (no worktree base recorded)"
        )
        if not self.files:
            return f"no changes {against}"
        added = sum(f.added for f in self.files)
        removed = sum(f.removed for f in self.files)
        n = len(self.files)
        return (
            f"{n} file{'s' if n != 1 else ''} changed, "
            f"+{added} −{removed} {against}"
        )


# -- the base ---------------------------------------------------------


def base_for(cwd: str) -> "tuple[str, str, str]":
    """``(base, source, refusal)`` for a session's working directory.

    ``refusal`` non-empty means there is no base and the pane must say
    "cannot determine a base" -- never "no changes". There is exactly one
    way to get there and it is the one v0.33.0 paid for:

    **the sidecar's ``base_ref`` equals the session's own branch.** A
    diff computed against a base equal to the branch cannot show anything
    the session COMMITTED -- ``base..HEAD`` is empty by construction, the
    same emptiness that made ``commits_ahead`` structurally unmeasurable
    and then force-deleted real commits. A session that committed its
    work would render as untouched. Refusing is the only honest answer,
    and :func:`doxa.worktrees.finalize` already refuses on the same
    condition rather than trusting the number.

    No sidecar at all is NOT that case: worktree-per-session may simply
    be off, and uncommitted work against ``HEAD`` is a real, smaller,
    perfectly reviewable claim. It comes back as :data:`BASE_HEAD` so the
    pane can label it as the smaller claim it is."""
    meta = worktrees_mod.read_meta(cwd)
    if not meta:
        return "HEAD", BASE_HEAD, ""
    base = str(meta.get("base_ref") or "").strip()
    branch = str(meta.get("branch") or "").strip()
    if not base:
        return "HEAD", BASE_HEAD, ""
    if branch and base == branch:
        return "", BASE_SIDECAR, (
            f"this worktree's recorded base is {base!r}, which is its own "
            "branch. nothing committed on it can appear in a diff against "
            "it, so an empty diff here would mean nothing at all. "
            "`/branch <ref>` sets a real base."
        )
    return base, BASE_SIDECAR, ""


# -- git --------------------------------------------------------------


def _git(cwd: str, *args: str) -> "tuple[int, str]":
    """One git call. ``(-1, message)`` when git could not be run at all,
    which is a different thing from git answering non-zero and is kept
    distinguishable for exactly that reason."""
    try:
        proc = subprocess.run(
            ["git", "--no-pager", *args],
            cwd=cwd, capture_output=True, text=True,
            timeout=GIT_TIMEOUT_SECS,
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, str(exc) or exc.__class__.__name__
    if proc.returncode not in (0, 1):
        return proc.returncode, (proc.stderr or "").strip()
    return proc.returncode, proc.stdout


def compute(cwd: str) -> DiffResult:
    """The session's diff, right now. The one entry point the pane calls.

    Blocking, deliberately: it is three subprocess calls on a local tree
    and it is driven by the tool-result stream, so it runs when an edit
    lands and at no other time -- DOXA's no-timer, no-per-frame rule means
    there is no frame budget to protect it from. The caller still runs it
    off a worker thread so a slow filesystem cannot stall the loop."""
    if not cwd or not os.path.isdir(cwd):
        return DiffResult(
            status=STATUS_ERROR, detail=f"{cwd or '(unset)'} is not a directory"
        )
    base, source, refusal = base_for(cwd)
    if refusal:
        return DiffResult(status=STATUS_NO_BASE, detail=refusal)
    code, out = _git(
        cwd, "diff", "--no-color", "--no-ext-diff", "--find-renames", base, "--"
    )
    if code < 0 or code > 1:
        return DiffResult(
            status=STATUS_ERROR, base=base, base_source=source,
            detail=out.splitlines()[0] if out else f"git exited {code}",
        )
    files = list(parse(out))
    files.extend(_untracked(cwd))
    return _bound(files, base, source)


def _untracked(cwd: str) -> "list[FileDiff]":
    """Created files, as diffs against nothing.

    Open question 2, answered: a file the agent CREATED is exactly what a
    reviewer wants to see, and plain ``git diff`` has no hunk for it. The
    obvious fix -- ``git add --intent-to-add`` -- is refused: this
    feature is explicitly "not ``git add -p``", staging is a git concept
    the user owns, and a review pane that quietly writes the index is a
    review pane that changed the thing it was reporting on.
    ``--no-index`` reads the file and touches nothing."""
    code, out = _git(cwd, "ls-files", "--others", "--exclude-standard", "-z")
    if code != 0 or not out:
        return []
    paths = [p for p in out.split("\0") if p][:MAX_UNTRACKED]
    made: "list[FileDiff]" = []
    for path in paths:
        # --no-index against the null device: exit 1 IS the success case
        # (differences found), which _git already treats as output.
        code, text = _git(
            cwd, "diff", "--no-color", "--no-ext-diff", "--no-index",
            "--", os.devnull, path,
        )
        if code < 0 or not text:
            continue
        for fd in parse(text):
            made.append(
                FileDiff(
                    path=path, header=fd.header, hunks=fd.hunks,
                    binary=fd.binary, untracked=True,
                    skipped=fd.skipped or ("binary" if fd.binary else ""),
                )
            )
    return made


def _bound(
    files: "list[FileDiff]", base: str, source: str
) -> DiffResult:
    """Apply the caps and report what they cost.

    A cap that silently drops is the defect; a cap that says "and 41 more
    files" is a bounded page. Skipping a file for size keeps the file in
    the list -- named, not rendered -- because its NAME is the part a
    reviewer still needs."""
    notes: "list[str]" = []
    kept: "list[FileDiff]" = []
    total = 0
    dropped = 0
    for fd in files:
        if len(kept) >= MAX_FILES or total >= MAX_TOTAL_LINES:
            dropped += 1
            continue
        body = sum(len(h.lines) for h in fd.hunks)
        if fd.binary:
            kept.append(fd if fd.skipped else _skip(fd, "binary"))
            continue
        if body > MAX_HUNK_LINES_PER_FILE:
            kept.append(_skip(fd, f"{body} changed lines — too large to render"))
            continue
        total += body
        kept.append(fd)
    if dropped:
        notes.append(
            f"{dropped} more file{'s' if dropped != 1 else ''} not shown "
            f"(the diff is capped at {MAX_FILES} files / "
            f"{MAX_TOTAL_LINES} lines)"
        )
    return DiffResult(
        status=STATUS_OK, base=base, base_source=source,
        files=tuple(kept), truncated="; ".join(notes), dropped_files=dropped,
    )


def _skip(fd: FileDiff, why: str) -> FileDiff:
    """The file, named but not rendered. Hunks are dropped rather than
    kept-and-hidden: a hunk that is never shown must not be rejectable
    either, and keeping it would make the reject button reachable for a
    patch the user never saw."""
    return FileDiff(
        path=fd.path, header=fd.header, old_path=fd.old_path,
        binary=fd.binary, untracked=fd.untracked, skipped=why,
    )


# -- parsing ----------------------------------------------------------


def _hunk_range(spec: str) -> "tuple[int, int]":
    """``-12,7`` / ``+3`` -> ``(12, 7)`` / ``(3, 1)``. An absent count is
    1, which is git's own shorthand and the case a one-line hunk hits."""
    body = spec[1:] if spec[:1] in "-+" else spec
    start, _, count = body.partition(",")
    try:
        return int(start), (int(count) if count else 1)
    except ValueError:
        return 0, 0


def _path_of(line: str) -> str:
    """The path out of a ``+++ b/foo`` or ``--- a/foo`` line, with git's
    ``a/``/``b/`` prefix removed and ``/dev/null`` read as absent."""
    raw = line[4:].strip()
    if raw == "/dev/null":
        return ""
    if raw[:2] in ("a/", "b/"):
        raw = raw[2:]
    return raw


def parse(text: str) -> "list[FileDiff]":
    """Unified ``git diff`` output as files and hunks.

    A hand-rolled scanner rather than a regex sweep, because the one
    thing this must never do is mis-attribute a body line: a line
    starting ``--- `` INSIDE a hunk body is content (a removed line of a
    file that itself contains a diff), and only the fact that we are
    between ``@@`` and the next ``diff --git`` tells the two apart."""
    files: "list[FileDiff]" = []
    header: "list[str]" = []
    hunks: "list[Hunk]" = []
    hunk_header = ""
    hunk_lines: "list[str]" = []
    ranges = (0, 0, 0, 0)
    path = ""
    old_path = ""
    binary = False

    def close_hunk() -> None:
        nonlocal hunk_header, hunk_lines
        if hunk_header:
            hunks.append(
                Hunk(
                    header=hunk_header,
                    old_start=ranges[0], old_count=ranges[1],
                    new_start=ranges[2], new_count=ranges[3],
                    lines=tuple(hunk_lines),
                )
            )
        hunk_header, hunk_lines = "", []

    def close_file() -> None:
        nonlocal header, hunks, path, old_path, binary
        close_hunk()
        if path or old_path:
            files.append(
                FileDiff(
                    path=path or old_path, old_path=old_path,
                    header=tuple(header), hunks=tuple(hunks),
                    binary=binary, skipped="binary" if binary else "",
                )
            )
        header, hunks, path, old_path, binary = [], [], "", "", False

    for line in text.split("\n"):
        if line.startswith("diff --git "):
            close_file()
            header = [line]
            # `diff --git a/x b/x` names the file even when the file is
            # binary and no ---/+++ pair follows.
            parts = line.split(" b/", 1)
            if len(parts) == 2:
                path = parts[1]
                old_path = parts[0].split(" a/", 1)[-1]
            continue
        if not header:
            continue  # preamble before the first file: not ours
        if hunk_header:
            if line[:1] in (" ", "+", "-", "\\"):
                hunk_lines.append(line)
                continue
            if line == "":
                # A context line for a genuinely empty line arrives as a
                # bare "" only from a trailing split; a real one is " ".
                continue
            close_hunk()
        if line.startswith("@@"):
            end = line.find("@@", 2)
            spec = line[2:end].split() if end > 0 else []
            hunk_header = line
            ranges = (
                *(_hunk_range(spec[0]) if len(spec) > 0 else (0, 0)),
                *(_hunk_range(spec[1]) if len(spec) > 1 else (0, 0)),
            )
            hunk_lines = []
            continue
        header.append(line)
        if line.startswith("--- "):
            old_path = _path_of(line) or old_path
        elif line.startswith("+++ "):
            path = _path_of(line) or path
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            binary = True
    close_file()
    return files


# -- reject -----------------------------------------------------------


def hunk_patch(fd: FileDiff, hunk: Hunk) -> str:
    """A one-hunk patch: this file's header, this hunk, nothing else.

    Why one hunk and not the file: two hunks in one file must be
    independently rejectable, and rejecting one must not discard the
    other. A whole-file restore cannot express that, which is why the
    header is kept verbatim from the diff that produced it rather than
    rebuilt -- a rebuilt header is a header that can be subtly wrong
    exactly when the file was renamed."""
    lines = [*fd.header, hunk.header, *hunk.lines]
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class RevertOutcome:
    """What a reject did, and what to say about it.

    ``applied`` False always means NOTHING CHANGED: ``git apply`` is
    all-or-nothing by construction, and the ``--check`` pass below makes
    that a checked claim rather than an inherited one."""

    applied: bool
    message: str
    stderr: str = ""


def revert_hunk(cwd: str, fd: FileDiff, hunk: Hunk) -> RevertOutcome:
    """Undo exactly this hunk in the working tree.

    ``git apply --reverse`` against the recorded hunk -- the spec's own
    mechanism, and the only one that leaves a sibling hunk in the same
    file untouched.

    **If it no longer applies, nothing changes and this says why.** The
    file moving underneath a recorded hunk is the ordinary case, not the
    exotic one: the agent edits while you read. ``--check`` runs first so
    the refusal is reported from a call that could not have written
    anything, and ``--reverse`` is atomic anyway, so the two together
    mean a failed reject is a no-op twice over. Forcing was never an
    option -- a three-way apply is conflict resolution, and this is
    explicitly not a merge tool."""
    if fd.skipped:
        return RevertOutcome(
            False,
            f"{fd.path} is shown by name only ({fd.skipped}) — there is no "
            "recorded hunk to reverse",
        )
    patch = hunk_patch(fd, hunk)
    for check in (True, False):
        args = ["apply", "--reverse", "--recount"]
        if check:
            args.append("--check")
        args.append("-")
        try:
            proc = subprocess.run(
                ["git", "--no-pager", *args],
                cwd=cwd, input=patch, capture_output=True, text=True,
                timeout=GIT_TIMEOUT_SECS, errors="replace",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return RevertOutcome(
                False, f"could not run git apply: {exc}", str(exc)
            )
        if proc.returncode != 0:
            err = (proc.stderr or "").strip().splitlines()
            return RevertOutcome(
                False,
                f"that hunk no longer applies to {fd.path} — nothing was "
                "changed. the file moved underneath it; expand the file "
                "again for the current diff.",
                err[0] if err else "",
            )
    return RevertOutcome(
        True, f"reverted one hunk in {fd.path} (+{hunk.added} −{hunk.removed})"
    )


# -- what the agent is told -------------------------------------------

#: How much of the rejected hunk is quoted back to the agent. Enough to
#: be unambiguous about WHICH edit, short of pasting a patch into a
#: prompt: the agent can read the file, and what it cannot recover is
#: the fact that a human disagreed.
REJECT_QUOTE_LINES = 12


def reject_message(
    fd: FileDiff, hunk: Hunk, reason: str = ""
) -> str:
    """The line the agent is told, in the user's own voice.

    **This is user-authored, so it is trusted input** -- it goes down the
    same path a typed prompt takes (``SessionPane._run_turn``), and it
    carries no framing marker at all. That is the deliberate contrast
    with :data:`doxa.peers.PEER_UNTRUSTED_INTRO`, which exists because
    ANOTHER AGENT wrote the text; a human clicking reject in their own
    session is the user speaking, and wrapping it in an untrusted-data
    paragraph would tell the agent to weigh the user's own instruction as
    hearsay.

    It says what was rejected in terms the agent can act on -- the file,
    the hunk's line range, a short quote, and the reason if one was typed
    -- because a rejection with a reason is worth far more than a bare
    revert: it is what stops the agent re-making the same edit."""
    quoted = [ln for ln in hunk.lines if ln[:1] in "+-"][:REJECT_QUOTE_LINES]
    more = ""
    changed = sum(1 for ln in hunk.lines if ln[:1] in "+-")
    if changed > len(quoted):
        more = f"\n… and {changed - len(quoted)} more changed lines"
    body = [
        f"I rejected one of your edits to `{fd.path}` and reverted it on "
        f"disk. Do not re-apply it.",
        "",
        f"The hunk, at lines {hunk.new_start}–"
        f"{hunk.new_start + max(hunk.new_count, 1) - 1} "
        f"(+{hunk.added} −{hunk.removed}):",
        "",
        "```diff",
        *quoted,
        "```" if not more else f"```{more}",
    ]
    if reason.strip():
        body += ["", f"Why: {reason.strip()}"]
    else:
        body += [
            "",
            "I did not give a reason. Ask me before redoing that part.",
        ]
    body += [
        "",
        "The file on disk no longer contains that change — re-read it "
        "before your next edit rather than patching against what you "
        "remember writing.",
    ]
    return "\n".join(body)


@dataclass
class PendingRejection:
    """A reject the user clicked while a turn was in flight.

    **Queued, not applied, and visibly marked** -- the spec weighs three
    answers and this is the one it lands on. Applying immediately races
    the agent's own write to the same file and produces a conflict
    neither side understands; refusing outright is honest but leaves the
    user holding a decision the app made them re-make later. Queuing
    costs the user a wait they can SEE, and a rejection the user has
    clicked and cannot see the effect of is the worst of the three.

    Mutable, unlike everything else in this module, because ``failure``
    is written after the fact by the flush."""

    path: str
    hunk_label: str
    reason: str
    file_diff: FileDiff
    hunk: Hunk
    failure: str = ""

    def mark(self) -> str:
        """The badge the diff shows while this is waiting."""
        return f"⏳ reject queued — applies when this turn ends"


def flush(
    cwd: str, queued: "list[PendingRejection]"
) -> "tuple[list[PendingRejection], list[PendingRejection]]":
    """Apply every queued rejection. ``(applied, refused)``.

    Order is the order they were clicked, which matters: two rejections
    in one file are recorded against the SAME diff, so the second one's
    recorded line numbers are stale the moment the first lands. That is
    not a bug to paper over with ``--recount`` alone -- a refusal here is
    the correct outcome and the user is told which one did not take."""
    applied: "list[PendingRejection]" = []
    refused: "list[PendingRejection]" = []
    for item in queued:
        outcome = revert_hunk(cwd, item.file_diff, item.hunk)
        if outcome.applied:
            applied.append(item)
        else:
            item.failure = outcome.message
            refused.append(item)
    return applied, refused


# -- the tick ---------------------------------------------------------
#
# The tool-result stream is the tick, and there is no other one. DOXA has
# a documented no-timer, no-per-frame rule, and docs/plans/code-graph.md
# already refused a file watcher for the same reason -- a second
# lifecycle to get wrong. An edit landing IS the event; recompute then
# and only then, the same reasoning that gave v0.56.0's spinner zero idle
# cost.

#: Tools whose completion means the tree changed, by definition.
TREE_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})

#: Leading words of a shell command that cannot change a tracked file.
#: Deliberately a SHORT ALLOW-list of read-only verbs rather than a
#: deny-list of destructive ones: the asymmetry is measured in what each
#: mistake costs. A false tick costs one ``git diff`` on a local tree; a
#: MISSED tick costs a diff pane that silently disagrees with the disk,
#: which is the one failure a live diff may not have. So anything not
#: recognisably read-only counts as a write.
READ_ONLY_COMMANDS = frozenset({
    "awk", "basename", "cat", "cd", "cut", "date", "df", "diff", "dirname",
    "du", "echo", "env", "file", "find", "grep", "head", "less", "ls",
    "md5sum", "nl", "od", "printenv", "printf", "ps", "pwd", "readlink",
    "realpath", "rg", "sort", "stat", "tail", "tree", "uniq", "wc",
    "whereis", "which", "who", "xxd",
})

#: ``git`` subcommands that only read. ``git`` is the one binary common
#: enough in an agent's Bash calls to be worth splitting rather than
#: treating whole.
READ_ONLY_GIT = frozenset({
    "blame", "branch", "cat-file", "config", "describe", "diff", "grep",
    "log", "ls-files", "ls-tree", "rev-list", "rev-parse", "shortlog",
    "show", "status", "symbolic-ref", "tag",
})


def bash_touches_tree(command: str) -> bool:
    """Could this shell command have changed a file? Over-inclusive on
    purpose -- see :data:`READ_ONLY_COMMANDS`.

    Only the FIRST word of each ``;``/``&&``/``|``-separated segment is
    examined, and any segment that is not recognisably read-only makes
    the whole command a write. Redirection makes a segment a write no
    matter what the verb is: ``echo x > f`` is a write performed by a
    read-only command, which is exactly the case a verb-only check gets
    wrong."""
    text = (command or "").strip()
    if not text:
        return False
    for sep in ("&&", "||", ";", "|", "\n"):
        text = text.replace(sep, "\0")
    for segment in text.split("\0"):
        words = segment.split()
        if not words:
            continue
        if ">" in segment:
            return True
        verb = words[0].rsplit("/", 1)[-1]
        if verb in ("sudo", "time", "nice", "command", "env"):
            words = words[1:]
            verb = words[0].rsplit("/", 1)[-1] if words else ""
        if verb == "git":
            sub = next((w for w in words[1:] if not w.startswith("-")), "")
            if sub in READ_ONLY_GIT:
                continue
            return True
        if verb in READ_ONLY_COMMANDS:
            continue
        return True
    return False


def is_tick(tool_name: str, tool_input: "dict | None" = None) -> bool:
    """Did this finished tool call change the worktree?

    The one predicate the runtime asks. A ``Task`` is a tick too: a
    subagent's own edits arrive as ITS tool calls, tagged with a
    ``parent_id``, and the parent's own result landing is the moment
    everything it did is on disk."""
    if tool_name in TREE_TOOLS or tool_name == "Task":
        return True
    if tool_name != "Bash":
        return False
    data = tool_input or {}
    return bash_touches_tree(str(data.get("command") or ""))


# -- width ------------------------------------------------------------

#: Below this many columns the diff pane renders UNIFIED, whatever the
#: user asked for. Measured, not chosen, and the arithmetic is the
#: spec's own: at 80 columns a half-width split pane is 40, side by side
#: inside that is 20 per side, and 20 columns of source is unreadable.
#:
#: Running it forward instead: a side is legible at roughly 44 columns
#: (the width :data:`doxa.layout.MIN_LEAF_WIDTH` already calls the floor
#: for a pane, plus the gutter), two sides plus a one-column separator
#: and the two four-column line-number gutters is 2*(44+4)+1 = 97. Round
#: to 100 -- the same number :data:`doxa.ui.labels.CTX_ABSOLUTE_MIN_COLS`
#: landed on for the ctx chip, for the same kind of reason, and gating
#: this the same way is the pattern the spec asks for by name.
SIDE_BY_SIDE_MIN_COLS = 100

#: Columns one side of a side-by-side view needs before it is worth
#: calling one. Used to decide, not to lay out -- the layout is fr units.
SIDE_MIN_COLS = 44


def side_by_side_allowed(width: int) -> bool:
    """Is this pane wide enough for two columns of source?

    Unmeasurable width (0, the first repaint) reads as NOT allowed, the
    opposite of the ctx chip's answer to the same question and
    deliberately so: the chip appearing late is a flicker, whereas a
    side-by-side that had to fall back to unified after the user read it
    is a page that changed under them. The narrower default is the one
    that never has to be taken back."""
    return width >= SIDE_BY_SIDE_MIN_COLS


def split_columns(width: int) -> int:
    """Columns per side once the separator is taken out."""
    return max(1, (width - 1) // 2)
