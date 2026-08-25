"""doxa.errors -- what a failure IS, before anything decides how to show it.

Four defects reached the user on one day and not one of them arrived as a
legible error: a ``TimeoutError`` raised out of ``textual_image`` while
Textual was PAINTING a widget took the whole app down to a bare terminal
traceback; the needs-input dialog stopped answering keys and said nothing;
server-tool results vanished with no trace that a search had even been
attempted; the memory chip drew half of itself and never mentioned the
other half. The through-line is not four bugs, it is one property --
**DOXA fails invisibly or fatally, rarely legibly** -- and this module is
the first half of fixing that. :class:`doxa.ui.transcript.ErrorBlock` is
the second half (what a failure LOOKS like), and
``DoxaApp.report_failure`` is the third (where failures arrive from).

Three things are deliberately separate here:

**A failure is not an exception.** :class:`Failure` is the general record
and an exception is only one way to build one (:func:`from_exception`).
The other is :func:`policy_failure`, which exists because
``docs/plans/plugin-api.md`` already promises a third failure state that never
raises at all -- a status chip whose ``text()`` blows its time budget is
disabled "loudly", and a surface that can only represent exceptions has
nowhere to put that. The naming will outlive this release, so it says
"failure" rather than "error" everywhere the distinction is real.

**Attribution is part of the record, not a guess the reader has to make.**
``origin`` says WHO failed. A traceback whose deepest interesting frame is
in ``textual_image`` is a different user action ("that terminal-image tier
is broken here") from one in ``doxa/`` ("file a DOXA bug") and a different
one again from ``lore_core`` ("the belief store"). :func:`origin_of` reads
it off the traceback rather than making every call site pass it, and an
explicit ``origin=`` always wins -- which is exactly the hook a future
plugin loader needs: it knows it is calling into ``plugin:jira`` and can
say so, instead of letting a plugin's crash read as a DOXA bug.

**Failing is a STATE, not just a message.** :class:`FailureLog` keeps the
per-origin tally the plugin spec's "disabled for the run" rule needs.
Nothing in this release disables anything -- there is no loader to disable
-- but the trace of a failure must not be a widget somewhere in a
scrollback, because a settings modal cannot read a scrollback.

**Everything shown is scrubbed first.** A traceback carries locals, paths,
environment and, often enough, credentials --
``rich.traceback.Traceback(show_locals=True)`` is what Textual prints on
the way out, and it prints exactly that. So every string this module
produces goes through ``lore_core.scrub.scrub_secrets``, the same choke
point ``doxa.engine`` runs on everything model-adjacent and
``doxa.peers`` runs on everything peer-adjacent. A crash report that
leaks a token is a worse defect than the crash it describes.

No timers, no polling, no per-frame cost: this module does work only when
something has already gone wrong, which is the discipline
:class:`doxa.ui.statusline.GitLine` and ``_refresh_status`` exist to
protect.
"""

from __future__ import annotations

import os
import sysconfig
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from lore_core.scrub import scrub_secrets

# Namespaces that are never the CULPRIT, only the messenger. Textual is the
# harness every DOXA frame runs inside, asyncio is how it runs them, and
# rich is what Textual paints with -- an exception passing through them says
# nothing about whose defect it is. Stripped deepest-first by origin_of()
# so attribution lands on the innermost frame that somebody actually owns.
INFRASTRUCTURE = ("textual", "asyncio", "rich", "concurrent", "contextlib")

#: The origin string for DOXA's own code.
DOXA = "doxa"
#: The origin string for the in-process belief store. ``lore_core`` runs
#: inside DOXA (see doxa/_lore_bootstrap.py) and a LORE-side raise has had
#: nowhere legible to land for as long as that has been true.
LORE = "lore"
#: What origin_of() answers when a traceback names nobody at all.
UNKNOWN = "unknown"

#: A failure with a stack (:func:`from_exception`).
KIND_EXCEPTION = "exception"
#: A failure with no exception behind it -- a broken promise rather than a
#: raise. The plugin spec's ``text()`` time budget is the motivating case.
KIND_POLICY = "policy"

#: Detail is bounded before it is stored, logged or drawn. A recursion
#: error's traceback is megabytes of identical frames, and neither a
#: transcript block nor a rotating log is the right place to find that out.
DETAIL_MAX_CHARS = 16_000

#: The log file, under DOXA's own directory -- never the repo, never a
#: temp dir that a reboot eats before the user gets round to reporting.
LOG_NAME = "errors.log"
#: Rotated at this size to ``errors.log.1``, ONE generation, so the whole
#: on-disk cost of this feature is bounded at twice this and needs no
#: sweeper, no timer and no config knob. 256 KiB holds hundreds of scrubbed
#: tracebacks, which is far more history than a bug report ever quotes.
LOG_MAX_BYTES = 256 * 1024

_STDLIB = sysconfig.get_paths().get("stdlib", "")


class FatalFailure(Exception):
    """What ``DoxaApp`` puts in ``App._exception`` when it exits on a
    failure it decided it could not survive.

    Textual's own ``_handle_exception`` stores the raising exception there
    so ``App.run_test`` can re-raise it at teardown and a test framework
    learns the app died. DOXA's override still exits on some failures --
    one with no surface to draw itself on, one that repeats without end --
    and those must keep failing a suite. A test that quietly passes through
    a fatal crash would make this whole module a place errors hide, which
    is the opposite of why it exists.

    Carries the failure's headline, already scrubbed, and no traceback of
    its own: the real one is in the :class:`Failure` and in the log."""


def scrub(text: str) -> str:
    """``lore_core.scrub.scrub_secrets``, and the reason every string in
    this module goes through it exactly once, at construction, rather than
    at each of the three places a failure is consumed.

    Display, the log file and the clipboard are three doors out of the
    process, and scrubbing at the door means a fourth door added later
    starts out leaking. Scrubbing at the SOURCE means the unscrubbed text
    never exists as a :class:`Failure` field at all -- the same argument
    ``doxa.engine``'s docstring makes for its own choke point, one layer
    up."""
    return scrub_secrets(text)


def _module_of(frame: "Any") -> str:  # noqa: F821 -- frame objects
    return str(frame.f_globals.get("__name__") or "")


def _is_infrastructure(module: str, filename: str) -> bool:
    root = module.split(".", 1)[0]
    if root in INFRASTRUCTURE:
        return True
    # The standard library is infrastructure too, and it cannot be listed
    # by name: `json`, `socket`, `subprocess` and two hundred others are
    # all places a defect passes THROUGH. Compared by path rather than by
    # a name list so it stays right on any interpreter.
    return bool(_STDLIB) and filename.startswith(_STDLIB)


def origin_of(error: BaseException) -> str:
    """Whose code this is, read off the traceback deepest-frame-first.

    The deepest frame that is not :data:`INFRASTRUCTURE` and not the
    standard library is the one that owns the failure: for the reported
    render crash that is ``textual_image._terminal``, which is the answer a
    user needs (a terminal-image tier misbehaving on THIS terminal), not
    ``textual.app`` (where it surfaced) and not ``doxa.ui.transcript``
    (which merely asked for a widget).

    ``doxa.*`` collapses to :data:`DOXA` and ``lore_core.*`` to
    :data:`LORE` -- those two are products, not modules, and a block
    saying "doxa.session.runtime" would be naming an implementation detail
    at somebody who wants to know which project to file against. Everything
    else answers with its top-level distribution-ish name, which is what a
    user would type to uninstall it.

    Never raises and never returns "": a failure whose attribution failed
    is still a failure that has to be shown, so the floor is
    :data:`UNKNOWN`."""
    tb = getattr(error, "__traceback__", None)
    frames = []
    while tb is not None:
        frames.append(tb.tb_frame)
        tb = tb.tb_next
    for frame in reversed(frames):
        try:
            module = _module_of(frame)
            filename = str(frame.f_code.co_filename)
        except Exception:  # noqa: BLE001 -- a frame we cannot read is not one we blame
            continue
        if not module or _is_infrastructure(module, filename):
            continue
        root = module.split(".", 1)[0]
        if root == "doxa":
            return DOXA
        if root == "lore_core":
            return LORE
        return root
    return UNKNOWN


def _bound(text: str) -> str:
    if len(text) <= DETAIL_MAX_CHARS:
        return text
    dropped = len(text) - DETAIL_MAX_CHARS
    return text[:DETAIL_MAX_CHARS] + f"\n… {dropped:,} more characters not shown"


@dataclass(frozen=True)
class Failure:
    """One thing that went wrong, already scrubbed, ready to be shown.

    Frozen and field-documented, matching ``commands.SlashCommand`` and
    ``config.Setting``: it DESCRIBES a failure, it does not handle one.
    Who handles it is ``DoxaApp.report_failure``, and keeping the two apart
    is what lets the log, the transcript block and (eventually) the
    settings modal all read the same record instead of three parallel
    almost-truths."""

    #: Who failed -- :data:`DOXA`, :data:`LORE`, a third-party package
    #: name, or ``plugin:<name>`` once a loader exists to say so.
    origin: str
    #: ONE line. What broke, in the words the block header will use.
    summary: str
    #: The whole story -- a scrubbed traceback, or a policy explanation.
    #: Folded away by default; the block header is what gets read.
    detail: str = ""
    #: :data:`KIND_EXCEPTION` or :data:`KIND_POLICY`.
    kind: str = KIND_EXCEPTION
    #: Whether the app can keep running. BOTH are shown; a fatal one is
    #: also printed to the terminal on the way out (see
    #: ``DoxaApp._handle_exception``), because the user who has to report
    #: it will no longer have a TUI to read it in.
    fatal: bool = False
    #: What DOXA was doing. Free text from the reporting boundary
    #: ("worker: engine", "rendering a widget"), not from the exception.
    context: str = ""
    #: ``time.time()`` at construction. Wall clock, for the log's stamp.
    at: float = field(default_factory=time.time)

    @property
    def signature(self) -> str:
        """What makes two failures THE SAME failure.

        A widget that raises while painting raises again on the next paint,
        and the next, for as long as it is on screen. Counting repeats
        against this key is what turns an unbounded stream of identical
        blocks into one block with a tally -- and what lets
        ``DoxaApp`` notice a failure that will never stop and escalate
        rather than spin.

        Origin plus summary, deliberately, and not the full detail: two
        paints of the same broken widget produce byte-different tracebacks
        (different frame ids, different locals) and must still count as one
        thing."""
        return f"{self.origin}\x00{self.kind}\x00{self.summary}"

    def headline(self) -> str:
        """The one line a user reads without opening anything."""
        where = f" while {self.context}" if self.context else ""
        return f"{self.summary}{where}  ·  {self.origin}"

    def log_text(self) -> str:
        """This failure as the log file records it -- a stamped header
        line, then the detail. Already scrubbed (everything here is), so
        the writer never has to remember to."""
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.at))
        head = (
            f"--- {stamp}  origin={self.origin}  kind={self.kind}  "
            f"fatal={'yes' if self.fatal else 'no'}"
        )
        body = self.detail.rstrip("\n")
        return f"{head}\n{self.headline()}\n{body}\n" if body else f"{head}\n{self.headline()}\n"


def _summarize(error: BaseException) -> str:
    """``TimeoutError: timed out`` -- the type always, the message when it
    has one and only its first line. An exception whose ``str()`` is a
    paragraph (a subprocess dump, a JSON body) must not push the rest of
    the header off the terminal; the whole of it is one keystroke away in
    the fold."""
    name = type(error).__name__
    text = str(error).strip().splitlines()
    first = text[0].strip() if text else ""
    if not first:
        return name
    if len(first) > 160:
        first = first[:157] + "…"
    return f"{name}: {first}"


def from_exception(
    error: BaseException,
    *,
    origin: "str | None" = None,
    context: str = "",
    fatal: bool = False,
) -> Failure:
    """A :class:`Failure` built from a raise, scrubbed at construction.

    ``origin`` overrides :func:`origin_of` -- which is the ONLY thing a
    future plugin loader has to pass to make a plugin's crash read as the
    plugin's rather than as DOXA's.

    The traceback is formatted WITHOUT locals, unlike the
    ``rich.traceback.Traceback(show_locals=True)`` Textual prints on a
    panic. Locals are where the credentials are, scrub_secrets is
    pattern-based rather than omniscient, and a stack that names the file,
    the line and the source line answers "where" without handing the frame
    contents to a screenshot in a public issue."""
    detail = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    return Failure(
        origin=origin or origin_of(error),
        summary=scrub(_summarize(error)),
        detail=_bound(scrub(detail)),
        kind=KIND_EXCEPTION,
        fatal=fatal,
        context=context,
    )


def policy_failure(
    origin: str, summary: str, detail: str = "", *, fatal: bool = False,
) -> Failure:
    """A failure with no exception behind it.

    ``docs/plans/plugin-api.md``'s third failure state: a chip whose ``text()``
    overran its time budget did not raise -- it broke a promise -- and it
    is disabled for the run just as loudly as one that crashed. Same
    record, same block, same log; only :attr:`Failure.kind` differs, so
    nothing downstream needs a second code path to show it."""
    return Failure(
        origin=origin,
        summary=scrub(summary),
        detail=_bound(scrub(detail)),
        kind=KIND_POLICY,
        fatal=fatal,
    )


class FailureLog:
    """Every failure this RUN has seen, by origin -- the queryable state
    behind the visible blocks.

    ``docs/plans/plugin-api.md``'s failure policy is written in terms of state,
    not messages: a plugin that raises in a hook is "disabled for the
    run", and every such state is "visible in the settings modal". A
    widget in a scrollback cannot answer "is this plugin disabled"; this
    can. Nothing disables anything yet -- there is no loader to disable,
    and building one is explicitly not this release's work -- but the
    record a loader would read exists, is populated by the same call that
    paints the block, and is one attribute (``app.failures``) away from
    the settings modal that will want it.

    Bounded: the tally is per SIGNATURE and unbounded only in the number
    of DISTINCT failures, and :attr:`recent` keeps at most
    :data:`RECENT_MAX` whole records. A render loop that fires the same
    failure ten thousand times costs one counter increment each time, not
    ten thousand retained tracebacks."""

    #: How many whole records to keep. The counts are kept for all of them.
    RECENT_MAX = 50

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._by_origin: dict[str, int] = {}
        self.recent: "list[Failure]" = []

    def record(self, failure: Failure) -> int:
        """Take one failure in; return how many times its signature has now
        been seen (1 the first time). The caller uses that count to decide
        between a new block and a tally on the existing one -- and, past a
        threshold, that this failure is never going to stop."""
        count = self._counts.get(failure.signature, 0) + 1
        self._counts[failure.signature] = count
        self._by_origin[failure.origin] = self._by_origin.get(failure.origin, 0) + 1
        if count == 1:
            self.recent.append(failure)
            if len(self.recent) > self.RECENT_MAX:
                del self.recent[0]
        return count

    def count(self, failure: Failure) -> int:
        """How many times this signature has been recorded."""
        return self._counts.get(failure.signature, 0)

    def origins(self) -> "dict[str, int]":
        """``{origin: failures this run}`` -- what a settings modal reads
        to say which plugin is disabled and why there is a number next to
        it. A copy: callers must not be able to edit the tally."""
        return dict(self._by_origin)

    def failed(self, origin: str) -> bool:
        """Has ``origin`` failed at all this run? The predicate a loader's
        "disabled for the run" rule is written against."""
        return self._by_origin.get(origin, 0) > 0

    def total(self) -> int:
        return sum(self._by_origin.values())


def log_path() -> Path:
    """``$DOXA_HOME/errors.log`` -- under DOXA's own directory, beside the
    config the ``/about`` screen already tells a bug reporter to quote."""
    from . import config as config_mod

    return config_mod.doxa_home() / LOG_NAME


def rotated_path() -> Path:
    """The ONE previous generation. See :data:`LOG_MAX_BYTES`."""
    return log_path().with_name(LOG_NAME + ".1")


def append(failure: Failure) -> "Path | None":
    """Persist one failure, rotating first if the file has grown past
    :data:`LOG_MAX_BYTES`. Returns where it went, or None when nothing
    could be written.

    Bounded by SIZE and not by age, with exactly one previous generation:
    the file is for a user who is writing a bug report now and wants the
    last thing that broke, not an archive. Two generations of 256 KiB is
    the whole on-disk cost of this feature, which is why it needs no
    sweeper, no timer and no setting.

    Returning None rather than raising is the one swallow this module
    allows itself, and it is not a silent one: the caller
    (``DoxaApp.report_failure``) has ALREADY painted the block by the time
    it gets here, so a full disk costs the persisted copy and never the
    visible one. The reverse order -- log first, draw second -- would let
    a read-only home directory hide a crash."""
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size >= LOG_MAX_BYTES:
            os.replace(path, rotated_path())
        with path.open("a", encoding="utf-8") as handle:
            handle.write(failure.log_text())
        return path
    except Exception:  # noqa: BLE001 -- see the docstring: the block is already up
        return None
