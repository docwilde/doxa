# SPDX-License-Identifier: AGPL-3.0-only
"""Narrow native LORE sync adapter for retained Python consumers.

Configuration gates remain local. Canonical machine/project/status reads use
read-only native operations; only an explicitly configured create requests a
machine identity. There is no Python database or memory backend fallback.
"""

from __future__ import annotations

import os
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from . import native_lore

# sync.md's own names for the two classes DOXA owns. Passed to
# ``sync_oplog.class_enabled`` as CONFIG names (plural), not wire names:
# its ``CLASS_CONFIG_NAMES.get(x, x)`` maps the six built-in WIRE classes
# (singular -- memory, belief, skill) onto their config spelling and passes
# anything else through untouched, which is exactly what DOXA's two need,
# because they have no wire-class row in that map at all. They are the
# opt-in classes of sync.md's "Default class set" decision (#9): everything
# on by default is the same decision as syncing transcripts by default.
CLASS_TABSETS = "tabsets"
CLASS_WORKTREES = "worktrees"

#: Read once per process, never per repaint (the status chip's own cost
#: rule -- see :class:`doxa.ui.statusline.GitLine`'s no-timer discipline).
#: ``_PROBED`` is the NEGATIVE half of the same cache: a read-only probe
#: that found no machine row must not re-open the store on every refresh
#: just to be told "still nothing".
_MACHINE: "str | None" = None
_PROBED = False


def invalidate() -> None:
    """Drop the cached machine id -- for tests, which point ``LORE_ROOT``
    at a fresh directory per test and would otherwise inherit the previous
    one's identity. Nothing in the app calls this: a machine id does not
    change under a running process, which is the whole reason it is read
    once."""
    global _MACHINE, _PROBED
    _MACHINE = None
    _PROBED = False


def sync_url() -> str:
    """``LORE_SYNC_URL`` (or ``LORE_SYNC_PEER``, Transport B), as a string.

    Read from the environment DIRECTLY, and that is a gap rather than a
    preference: sync.md names ``LORE_SYNC_URL`` as the switch that decides
    whether sync happens at all, but lore_core 0.54.0 exposes no reader for
    it -- ``sync_oplog`` has ``sync_disabled``/``sync_classes``/
    ``class_enabled``/``hmac_key`` and nothing else, because the transport
    that consumes the URL is sync.md's PR 5 and has not landed. When it
    does, this function becomes a one-line delegation to lore_core's own
    reader and the env-var spelling stops being DOXA's business."""
    return (
        os.environ.get("LORE_SYNC_URL", "").strip()
        or os.environ.get("LORE_SYNC_PEER", "").strip()
    )


def sync_disabled() -> bool:
    """``LORE_DISABLE_SYNC``, sync.md's stage kill switch, with lore_core's
    own truthiness: ``""`` and ``"0"`` mean on, anything else means off.

    Read from the environment here rather than delegated, for the reason
    :func:`class_enabled` sets out below at length -- it has to answer on a
    lore_core that has no ``sync_oplog`` module at all, and a kill switch
    that silently stops being honoured because a library got older is the
    one kind of switch that must not."""
    return os.environ.get("LORE_DISABLE_SYNC", "") not in ("", "0")


def enabled() -> bool:
    """Is sync CONFIGURED on this machine at all?

    Configured, deliberately, and not "capable": this answers a question
    about the user's intent, which is a question about environment
    variables and nothing else. Whether the installed lore_core can
    actually carry an op log is a separate question, asked separately and
    later, by :func:`machine_id` and :func:`read_state` -- both of which
    fail closed. Folding the two together is what an earlier revision did,
    and it made "is sync switched on" unanswerable on exactly the machines
    where the answer matters.

    False is the default and the overwhelmingly common answer, and every
    caller below short-circuits on it BEFORE touching the store -- which is
    what makes "sync off costs nothing and changes nothing" a property of
    the code rather than a promise in a docstring."""
    if not sync_url():
        return False
    return not sync_disabled()


#: lore_core's own ``DEFAULT_SYNC_CLASSES``, verbatim. Neither ``tabsets``
#: nor ``worktrees`` is in it, which is what makes DOXA's two classes
#: opt-in by construction rather than by a rule written somewhere else
#: (sync.md's "Default class set" decision, #9).
DEFAULT_SYNC_CLASSES = "memory,filemap,beliefs,pending,skills,sessions"


def class_enabled(class_name: str) -> bool:
    """Is this opt-in class switched on, AND is sync configured at all?

    Both halves, because either one alone is a wrong answer: the class
    switch without :func:`enabled` paints state for a hub nobody set up,
    and :func:`enabled` without the class switch would stamp records the
    user deliberately left out of ``LORE_SYNC_CLASSES``.

    Parsed HERE rather than delegated to
    ``lore_core.sync_oplog.class_enabled``, and the reason is that the only
    two classes this is ever called with are DOXA's own. lore_core's
    ``CLASS_CONFIG_NAMES`` maps its six WIRE classes onto their config
    spelling and knows nothing about ``tabsets``/``worktrees`` -- its own
    comment says as much ("DOXA owns tabsets/worktrees"), and they reach
    its allow-list only through a ``.get(x, x)`` passthrough that would
    treat any typo identically. So delegating buys no shared knowledge,
    and it costs the answer ENTIRELY on a lore_core with no ``sync_oplog``
    module: ``doxa._lore_bootstrap`` prefers a plugin checkout over the
    pinned wheel, so a machine whose checked-out LORE predates the op log
    would quietly stop honouring a class its user had switched on. The
    spelling (comma list, plural) is sync.md's configuration table, and
    :data:`DEFAULT_SYNC_CLASSES` is lore_core's default copied whole."""
    if not enabled():
        return False
    raw = os.environ.get("LORE_SYNC_CLASSES", DEFAULT_SYNC_CLASSES)
    return class_name in {c.strip() for c in raw.split(",") if c.strip()}


def tabsets_enabled() -> bool:
    """sync.md item 1: may a tab set record carry ``project_key`` and
    ``machine_id``. OFF means :func:`doxa.tabsets.save` writes exactly the
    payload 1.9.2 wrote."""
    return class_enabled(CLASS_TABSETS)


def worktrees_enabled() -> bool:
    """sync.md item 2: may a worktree sidecar carry ``machine_id``. OFF
    means :func:`doxa.worktrees.create` writes exactly the four fields
    1.9.2 wrote."""
    return class_enabled(CLASS_WORKTREES)


def machine_id(*, create: bool = False) -> "str | None":
    """This machine's identity in the op log, or None when there isn't one.

    ``create=False`` (the default) is a pure SELECT: it reports an identity
    that already exists and MINTS NOTHING. That distinction is the whole
    reason this argument exists. :func:`doxa.tabsets.resolve` and
    :func:`doxa.worktrees.is_own_record` have to ask "is this record mine"
    on a record that names some other machine -- and they have to be able to
    ask it with sync switched off, because a record that arrived from
    elsewhere does not become this machine's tabs when the user turns sync
    off. A probe that minted an identity as a side effect would write to the
    store on a machine that never opted in, which is precisely the
    invisibility this module exists to keep.

    ``create=True`` is for the WRITE paths only, and only once they have
    already checked their class is on. ``get_or_create_machine`` deliberately
    never commits (it is built to run inside a caller's own transaction), so
    the commit is here.

    None means: sync is off, or lore_core has no op log, or the store has no
    ``sync_machine`` table, or nothing has ever minted an identity. Callers
    read None as "cannot prove this record is foreign" and KEEP -- the safe
    direction in every one of the three surfaces, the same way an unreadable
    worktree sidecar has always meant keep."""
    global _MACHINE, _PROBED
    if _MACHINE is not None:
        return _MACHINE
    if _PROBED and not create:
        return None
    if create and not enabled():
        # A write path that got here with sync off is a bug in the caller,
        # but minting an identity would make it a bug on the user's disk.
        return None
    try:
        found = native_lore.request("sync_machine_v1", create=bool(create))
        if found is not None and (not isinstance(found, str) or not found
                or len(found) > 128 or any(ord(char) < 32 for char in found)):
            found = None
    except native_lore.NativeLoreError:
        found = None
    if found:
        _MACHINE = str(found)
        return _MACHINE
    _PROBED = True
    return None


def project_key(cwd: str) -> "str | None":
    """The wire identity of the project ``cwd`` belongs to, or None.

    sync.md prerequisite (a): the slug stays the local name (it is also the
    transcript directory, the ``MEMORY.md`` directory and every
    ``project:<slug>`` belief subject), and a project gets a SECOND name
    used only on the wire, so two checkouts of one remote at two paths on
    two machines are one project. Resolved through the store's own
    ``sync_projects`` mapping rather than recomputed from the remote here,
    so DOXA and LORE cannot disagree about what a project is called."""
    if not cwd:
        return None
    try:
        key = native_lore.request("sync_project_v1", cwd=cwd)
        if key is not None and (not isinstance(key, str) or not key
                or len(key) > 128 or any(ord(char) < 32 for char in key)):
            return None
        return key
    except native_lore.NativeLoreError:
        return None


@dataclass(frozen=True)
class SyncState:
    """What the status chip says: how long since the last pull, how much is
    waiting to go out, and whether anything needs a human.

    A record, not four loose returns, for the reason
    :class:`doxa.session.chips.StatusChip` is one: the chip's text and its
    tooltip are built from the same reading, and two parallel values are
    two values that drift.

    ``last_pull_age_s`` is None for "never pulled", which is a DIFFERENT
    statement from "pulled a long time ago" and the chip spells it
    differently. ``conflicts`` and ``unverified`` are separate counts
    because they need separate actions: a conflict is two entries in a file
    waiting for the user to delete one, an unverified op is a bad MAC that
    was staged and never applied."""

    last_pull_age_s: "float | None"
    unpushed: int
    conflicts: int
    unverified: int

    @property
    def needs_attention(self) -> bool:
        """Is anything here waiting on a HUMAN, as opposed to merely
        describing the transport? Drives the chip's warning colour."""
        return bool(self.conflicts or self.unverified)


def _age_seconds(stamp: "str | None") -> "float | None":
    """Seconds since an ISO timestamp lore_core wrote, or None.

    Defensive about the spelling because DOXA does not own it: the peer
    rows this reads are written by sync.md's PR 5, which has not landed, so
    the exact format is not yet fixed on disk anywhere. A stamp this cannot
    parse reads as "never pulled" rather than as a crash -- and a stamp in
    the future (clock skew between two machines) clamps to zero rather than
    painting a negative age."""
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - parsed).total_seconds())


def read_state() -> "SyncState | None":
    """The whole chip's reading in one store visit, or None for NO CHIP.

    None -- never a zero, never an error block -- is the answer for all of:
    sync is off, this lore_core has no op log, this store predates the
    ``sync_*`` tables, the store cannot be opened at all. The status bar has
    no overflow behaviour and a row of chips is the most contended space in
    the interface; a chip that reads ``sync 0`` on every DOXA that never
    opted in would be spending that space to say nothing.

    BLOCKING -- several small SQLite reads. Callers run it through
    ``asyncio.to_thread`` (:meth:`doxa.session.runtime.PaneRuntimeMixin.
    _refresh_sync_state`), on the same rule every other store read in this
    app already follows."""
    if not enabled():
        return None
    try:
        value = native_lore.request("sync_state")
        if value is None or not isinstance(value, dict):
            return None
        age = value.get("last_pull_age_s")
        if age is not None and (type(age) not in (int, float) or not math.isfinite(age) or age < 0):
            return None
        counts = [value.get(name) for name in ("unpushed", "conflicts", "unverified")]
        if any(type(count) is not int or not 0 <= count < 2**64 for count in counts):
            return None
        return SyncState(age, *counts)
    except native_lore.NativeLoreError:
        return None
