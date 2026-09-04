# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.tabsets -- item D: persist and restore a repo's tab set.

**This item's original spec text did not survive to this session.** What
follows is RE-DERIVED from the item's name ("tab restore") plus the
surrounding codebase -- most tellingly, ``doxa/naming.py`` already
forward-referenced it verbatim: "a restart (item D's window restore)
reuses the [cached tab] name rather than spending a second call on the
same session." See CHANGELOG.md's 0.23.0 entry for the judgment calls this
re-derivation had to make.

The gap it closes: sessions are daemons that outlive the TUI (doxa/
daemon.py), but ``doxa`` only ever reattaches to the SINGLE most recent
live session in a repo's scope (doxa.cli, the ``args.command is None``
branch) -- a multi-tab working set is lost on every restart even though
every daemon behind it is still alive and attachable. This module is the
persistence half; doxa.app wires the record's changes in, and doxa.cli
resolves it against the live peer registry on launch.

**Record**: one small JSON file per scope under ``$DOXA_HOME/tabsets/``
(default ``~/.doxa/tabsets/``), named by a truncated sha256 of the scope
key -- the scope key is a filesystem path (``peers.main_repo_root_of``,
the SAME key ``doxa.cli``'s spawn-or-attach and ``doxa.peers``' discovery
already group daemons by), which is not itself a safe filename on every
platform. ``{"scope_key", "active_session_id", "tabs": [{"session_id",
"pinned_name"}, ...]}`` -- order in ``tabs`` IS the saved tab-bar order.
Writes are atomic (tmp file + ``os.replace``), 0600, same discipline as
``doxa.config``'s settings file and ``doxa.peers``' registry entries.
Never raises: a missing, unreadable or malformed record reads as "nothing
to restore" to every caller, exactly like a broken config.toml costs the
user their settings, never their session (doxa.config.load's own rule).

**The layout node** (v0.32.0, filled in v0.91.0): the record ALSO
carries ``{"layout": {"kind": "tabs", "tabs": [...], "trees": [...]}}``.
That node was reserved three years of releases before it held anything --
"the day a split tree does exist the record grows a ``{"kind": "split",
...}`` node in the same slot instead of needing a format version and a
migration" -- and v0.91.0 is that day. Splits are carried in ``trees``:
one :mod:`doxa.layout` tree per TAB, in tab order, each a ``leaf`` or a
``split`` node.

The compatibility rule is the one that was written down then, and it
survives BOTH ways:

* ``tabs`` stays at the TOP level, flat, every leaf of every tree in
  layout order. It is the only shape v0.23.0-v0.31.0 knows how to read
  and the shape :func:`_layout_tabs` still prefers, so a record written
  by a DOXA with splits still restores under one without them -- as N
  ordinary tabs, which is the honest degradation, not as nothing.
* ``kind`` stays ``"tabs"``. Writing ``"split"`` there instead would be
  read by every DOXA from v0.32.0 to v0.88.0 as "nothing this version can
  lay out" -- correct, and it would cost the user every tab they had.
  An unrecognised kind still means that, and :func:`load` still returns
  ``None`` for one; nothing this version writes produces one.
* A record with NO ``trees`` key -- anything written before v0.91.0 --
  reads as one single-leaf tree per saved tab (:func:`_layout_trees`).
  The absence of the key IS the migration; there is no version field and
  no upgrade step.

**The third format, and the last one** (v0.97.0): ``groups``. The window
holds ONE tree now, its leaves are :class:`doxa.layout.Group` nodes, and
that tree rides in ``layout["groups"]`` beside the other two keys on
exactly the principle the slot was reserved with. Every rule above holds
unchanged -- ``kind`` stays ``"tabs"``, the flat top-level ``tabs`` list
stays authoritative and complete, and the absence of the key is again the
whole migration. :func:`_layout_groups` is the reader, and it answers for
all three eras:

* ``groups`` present -- v0.97.0 and later. That tree, as written.
* ``trees`` but no ``groups`` -- v0.91.0 to v0.95.0. Each saved tree's
  leaves read as **one single-tab group per leaf**
  (:func:`doxa.layout.groupify`), which is not a guess: a leaf held
  exactly one session in those releases, so it is the same statement in
  the new vocabulary.
* neither -- v0.23.0 to v0.90.0. The saved tabs were tabs.

The COMPOSITION rule the spec left open (how N per-tab trees become the
window's ONE tree) is answered here, and it is answered by asking what
was on the user's screen: the window tree is the tree of the tab that was
ACTIVE, because that is the arrangement the user was looking at when the
record was written, and every OTHER saved tab becomes a TAB of the group
that holds it -- in saved order, so nothing is lost and nothing moves.
A pre-v0.91.0 record therefore restores as ONE group holding N tabs,
which is exactly what N tabs were.

The literal alternative -- a group per saved tab -- was rejected after
measuring it: five saved tabs would restore as a five-way split, each
region 16 columns on a standard 80-column terminal, below
:data:`doxa.layout.MIN_LEAF_WIDTH` (34) and therefore below the width at
which DOXA's own :func:`doxa.layout.split_refusal` will create a split at
all. A restore that produces an arrangement the app refuses to produce
interactively is not a migration, it is a defect with a rationale.

**Writing back**: ``trees`` is still written, derived from the group tree
by taking each group's ACTIVE tab as that region's leaf
(:func:`_trees_from_groups`). A v0.91.0-v0.95.0 DOXA reading a v0.97.0
record therefore gets the geometry it can express, and picks the
remaining tabs up from the flat list as ordinary tabs -- the same honest
degradation the flat list has provided since v0.23.0, one format on.

**The fourth key** (v1.0.0): ``collections``, at the TOP level beside
``tabs`` and ``layout`` rather than inside the layout node::

    {"tabs": [...], "layout": {...}, "collections": [
      {"name": "ampiric", "sessions": ["abc", "def"], "collapsed": false}
    ]}

It is not in the layout node because it is not geometry: a collection
(:mod:`doxa.collections`) groups sessions BY NAME regardless of which
region shows them -- two members may sit in different ``PaneGroup``s, and
one ``PaneGroup`` may show tabs from three collections. Every rule that
held through the three format changes above holds again, unchanged:
``layout.kind`` stays ``"tabs"``, the flat top-level ``tabs`` list stays
authoritative and complete, and the absence of the key is the whole
migration. A session id in a collection but not in ``tabs`` is dropped --
at write time and again at read time -- the way :func:`doxa.layout.prune`
drops a leaf whose session is gone.

**Restore is a cross-check, not a replay**: :func:`resolve` reads the
saved record, then filters it against the LIVE daemon registry
(``doxa.peers.list_daemons``) for the same scope. A saved session id with
no live daemon behind it (finalized since, killed, machine rebooted) can
never be REATTACHED -- it must never spawn a replacement session (that
would not be the session the user left) and must never block startup.

Since v0.32.0 such a tab is no longer simply dropped: if the session left
a transcript behind (``doxa.transcript.exists``) it comes back ARCHIVED
-- read-only, its conversation rendered from disk, no engine behind it --
because "the tab and its content came back" is the point of the feature,
and a session that finalized on its linger timer while the window was
shut is the commonest way to lose one. Only a saved id with no daemon AND
no transcript is still dropped, and counted. The caller (doxa.cli)
reports all three counts, so a startup that quietly differs from what the
user left is never silent about it.

**Stopped vs. detached vs. killed** (v0.17's ``detached_on_purpose`` /
stop-path distinction, carried into the record; revised v0.60.0, then
v0.85.0, then v0.99.1): **Ctrl+Q ends it, Ctrl+W parks it.** A session
DETACHED (Ctrl+W, the palette's "Quit: detach") keeps running and STAYS
in the set, tab closed or not -- reattaching it next launch reconnects
the SAME daemon, still doing whatever it was doing. A session STOPPED
from inside this window (Ctrl+Q, the palette's "Quit: stop session")
LEAVES the set, tab closed or not, and does not come back -- not live,
not archived.

That was also true through v0.55.0, for a cruder reason: "the daemon is
gone" and "the tab is gone" were the same fact then, full stop. v0.56.0
broke that equivalence -- DOXA now pins its own session id to the CLI's
(``ClaudeAgentOptions.session_id``), so ``--resume`` CAN replay a
conversation DOXA itself ended -- and v0.60.0 read that capability as
license to keep a stopped session in the set too, on the theory that a
resumable transcript deserved a resumable tab. Reported live as a defect
instead: *"tabs that i had closed using CTRL+Q are resurrected on the
next start of DOXA anyway"* -- worse, LIVE rather than read-only, because
finalize() never removes the conversation from the CLI's own history
store, so doxa.cli's restore triage (:func:`ended_tab_spec`) found it,
answered RESUME_OK, and rebuilt it as an ordinary resumable tab. v0.99.1
is the reversal: ``DoxaApp._persist_tabset``'s own mounted-pane scan
excludes a ``_stopped`` pane again, the same rule v0.55.0 had, reached
now from every path that can stop a session (Ctrl+Q on any tab, the
palette's tab-scoped and all-tabs stop) rather than v0.85.0's one
carve-out for the LAST tab only. **Nothing on disk is destroyed** by any
of this -- the transcript stays exactly where :func:`doxa.transcript`
always wrote it, findable by ``/search`` and the resume picker alike; the
only thing v0.99.1 changes is whether a session's id is still IN this
record for a future :func:`resolve` to find in the first place. A session
whose id was never written here cannot reach :func:`ended_tab_spec` (the
archived-or-resumed fork below) at all -- it simply is not one of the
saved tabs, the same as if the user had never opened it in a
persisted-tabs window.

**One exception, now folded into the rule above** (v0.85.0, superseded by
v0.99.1's general Ctrl+Q exclusion for the STOPPED half): closing the
LAST open tab with Ctrl+W never writes that tab's session into the set
either, even though the session survives and Ctrl+Q's half of this
carve-out no longer needs one. Reported live in two parts: Ctrl+Q on the
last tab should start the next launch fresh (it used to come back
archived, read-only); Ctrl+W on the last tab should ALSO start fresh,
even though -- unlike Ctrl+Q -- the session is still running and
reattachable by NAME (``/attach``, the peers chip). A window with zero
tabs left has nothing left to restore automatically; that the Ctrl+W
session is still there to attach TO is a fact about the live daemon
registry (``doxa.peers``), never about this record. See
``DoxaApp._close_pane``'s own ``is_last`` branch for the mechanics.

Only an EXPLICIT reap (``/sessions kill <prefix>``, ``kill-detached``,
the palette's kill path) leaves the set for good otherwise: reaping is
the one gesture in this app that means "forget this conversation", so
doxa.app vetoes those ids at write time (``DoxaApp._killed_this_run``)
rather than letting them round-trip through here and get treated as just
another dead daemon. doxa.app is still the one place that knows which of the
three just happened; this module stays a plain record store either way.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import collections as collections_mod
from . import config as config_mod
from . import layout as layout_mod
from . import peers as peers_mod
from . import transcript as transcript_mod


def _bool(env_name: str, default: bool) -> bool:
    """Same four lines as doxa.clock._bool / doxa.worktrees._bool -- the
    house convention for a bool_on setting is its own tiny copy, not a
    shared import for one helper."""
    raw = config_mod.raw(env_name).strip()
    if not raw:
        return default
    return raw.lower() not in ("0", "false", "no", "off")


def enabled() -> bool:
    """Effective value of the ``restore_tabs`` setting -- DEFAULT ON, same
    default-on shape as ``worktree_per_session``. ``DOXA_RESTORE_TABS=0``
    (or the settings-modal equivalent) is the only way back to today's
    single-most-recent-session behavior."""
    return _bool("DOXA_RESTORE_TABS", True)


@dataclass(frozen=True)
class TabRecord:
    """One tab in the saved set: which session, the name the user pinned
    on it (``None`` for an automatic label -- that is derived fresh at
    restore time from the live engine/GitLine, never stored), and the
    session's own working directory.

    ``cwd`` is new in v0.32.0 and exists for exactly one reason: a saved
    tab whose daemon is GONE has no registry entry left to ask where it
    ran, and ``doxa.transcript`` needs that path to find the session's
    transcript file (the project slug is derived from it). With
    ``worktree_per_session`` on, a session's cwd is its own linked
    worktree, NOT the scope key -- so guessing the scope would look in the
    wrong project. ``None`` on every record written before v0.32.0, which
    restore falls back to the scope key for; a wrong guess costs an
    archived tab, never a wrong transcript (the file is keyed by session
    id, so a miss is a miss, never a mix-up)."""

    session_id: str
    pinned_name: "str | None" = None
    cwd: "str | None" = None


@dataclass(frozen=True)
class TabSetRecord:
    scope_key: str
    tabs: tuple[TabRecord, ...]
    active_session_id: "str | None"
    #: One :mod:`doxa.layout` tree per saved TAB, in tab order (v0.91.0).
    #: NEVER empty on a record this version reads: a record written before
    #: splits existed has no trees in it, and :func:`load` derives one
    #: single-leaf tree per saved tab instead -- "a new reader must
    #: restore old flat records as single-leaf trees", implemented once,
    #: here, so no caller has to know which kind of record it got.
    trees: "tuple" = ()
    #: The WINDOW's one layout tree, leaves holding
    #: :class:`doxa.layout.Group` (v0.97.0). Never ``None`` on a record
    #: this version reads: :func:`_layout_groups` derives one for all
    #: three eras, so no caller has to know which kind of record it got --
    #: the same promise ``trees`` makes one format down.
    groups: "Any" = None
    #: The user's SESSION COLLECTIONS (v1.0.0) -- see
    #: :mod:`doxa.collections`. A fourth format, and the first that does
    #: not touch the layout node at all: collections group sessions BY
    #: NAME regardless of where they are shown, so they are neither
    #: geometry nor a property of a region. ``()`` on every record written
    #: before v1.0.0, and the absence of the key is once again the whole
    #: migration.
    collections: "tuple[collections_mod.Collection, ...]" = ()
    #: The pane groups the user FOLDED SHUT on the rail (v1.5.0), by
    #: :attr:`doxa.ui.split.PaneGroup.entry_key`. The fifth key, at the
    #: top level beside ``collections`` and for the same reason: it is
    #: about the RAIL, not about geometry -- the group is on screen at its
    #: usual size either way, and only its tab rows are hidden. Written
    #: only when non-empty, so absence of the key is once again the whole
    #: migration, and a key naming a group this window no longer has is
    #: simply never asked about.
    rail_folded: "tuple[str, ...]" = ()


@dataclass(frozen=True)
class ResolvedRestore:
    """:func:`resolve`'s answer, in SAVED ORDER throughout -- never the
    order daemons happen to answer in.

    ``tabs``: saved tabs whose daemon is still live, paired with the peer
    registry entry to reattach through. ``archived``: saved tabs whose
    daemon is gone but whose transcript is still on disk -- they come back
    read-only rather than not at all (v0.32.0). ``skipped``: saved tabs
    with neither, the only ones that silently disappear. ``active_session_id``
    is the saved active tab if it survived as EITHER kind."""

    tabs: "list[tuple[TabRecord, peers_mod.PeerInfo]]"
    skipped: int
    active_session_id: "str | None"
    archived: "list[TabRecord]" = field(default_factory=list)
    entries: "list[tuple[TabRecord, peers_mod.PeerInfo | None]]" = field(
        default_factory=list
    )
    #: The saved layout trees (v0.91.0), one per saved TAB, in saved tab
    #: order and UNPRUNED -- which sessions survived is the caller's
    #: cross-check, already answered by ``tabs``/``archived``/``skipped``
    #: above, and pruning here would mean answering it twice. doxa.app's
    #: ``_restore_group_tree`` does the pruning against the specs it
    #: actually built.
    trees: "tuple" = ()
    #: The saved WINDOW tree (v0.97.0), UNPRUNED for the same reason
    #: ``trees`` is: which sessions survived is already answered above, and
    #: pruning here would mean answering it twice.
    groups: "Any" = None
    #: The saved collections (v1.0.0), already pruned to the flat ``tabs``
    #: list by :func:`load` -- which is a DIFFERENT pruning from the one
    #: this class declines to do for ``trees``/``groups``. That one is
    #: about which daemons are still alive, and is the caller's; this one
    #: is about the record being self-consistent, and belongs to whoever
    #: reads it, exactly once.
    collections: "tuple[collections_mod.Collection, ...]" = ()
    #: The rail's folded pane groups (v1.5.0), straight off the record.
    #: Not cross-checked against anything: an entry key names a widget in
    #: a window that no longer exists, so the only honest check is the one
    #: the rail itself makes -- does a group with this key exist NOW.
    rail_folded: "tuple[str, ...]" = ()

    def ordered(self) -> "list[tuple[TabRecord, peers_mod.PeerInfo | None]]":
        """Every surviving tab in SAVED ORDER, live and archived
        interleaved exactly as the strip had them -- ``None`` for the
        registry entry means archived. ``tabs`` and ``archived`` are the
        same set split by kind, for callers that only care about one; this
        is what the tab strip itself is rebuilt from, because a restore
        that reorders the user's tabs is not a restore.

        Falls back to live-then-archived when ``entries`` was not supplied
        (a ResolvedRestore built by hand rather than by :func:`resolve`)."""
        if self.entries:
            return list(self.entries)
        return [(tab, entry) for tab, entry in self.tabs] + [
            (tab, None) for tab in self.archived
        ]


def tabsets_dir() -> Path:
    """Create-and-return the per-scope-record directory, 0700 -- same
    same-user boundary as the peer registry and the settings file."""
    d = config_mod.doxa_home() / "tabsets"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _file_for(scope_key: str) -> Path:
    """A scope key is a filesystem path (a repo root, or a bare cwd
    outside a repo) -- not a safe filename on its own (slashes, length,
    platform quirks). A truncated sha256 sidesteps all of that; the
    record's own ``scope_key`` field keeps the mapping legible for anyone
    reading the directory by hand."""
    digest = hashlib.sha256(scope_key.encode("utf-8")).hexdigest()[:24]
    return tabsets_dir() / f"{digest}.json"


def _trees_from_groups(groups: "Any") -> "list":
    """The v0.91.0 ``trees`` shape for a v0.97.0 window tree: one tree per
    GROUP, in layout order, each region's leaf being that group's ACTIVE
    tab.

    This is what an older DOXA sees, and it is the most of this record's
    truth that shape can hold: v0.91.0-v0.95.0 leaves carry one session,
    so a group of three can only offer the one it is showing. The other
    two are not lost -- they are in the flat ``tabs`` list, which every
    reader since v0.23.0 consults first, and an older DOXA restores them
    as ordinary tabs beside the geometry.

    An empty group contributes nothing rather than an empty tree; if that
    empties a split, :func:`doxa.layout.from_json` collapses it on the way
    back in, which is the same rule :func:`doxa.layout.prune` applies."""
    if groups is None:
        return []

    def convert(node: "Any") -> "Any":
        if isinstance(node, layout_mod.Split):
            kids: "list" = []
            weights: "list[float]" = []
            for child, weight in zip(node.children, node.weights):
                converted = convert(child)
                if converted is not None:
                    kids.append(converted)
                    weights.append(weight)
            if not kids:
                return None
            if len(kids) == 1:
                return kids[0]
            return layout_mod.Split(node.orientation, tuple(kids), tuple(weights))
        return layout_mod.as_group(node).active_tab

    converted = convert(groups)
    return [converted] if converted is not None else []


def save(
    scope_key: str,
    tabs: "list[TabRecord]",
    active_session_id: "str | None",
    trees: "list | None" = None,
    groups: "Any" = None,
    collections: "Any" = None,
    rail_folded: "Any" = None,
) -> None:
    """Atomic write (tmp + ``os.replace``), 0600. Never raises: a
    persistence failure costs the user a future restore, never the
    running session -- the same posture ``doxa.config.save`` and
    ``doxa.peers.PeerHost._write_entry`` already take on their own state
    files."""
    if not scope_key:
        return
    rows = [
        {"session_id": t.session_id, "pinned_name": t.pinned_name, "cwd": t.cwd}
        for t in tabs
        if t.session_id
    ]
    layout: dict = {"kind": "tabs", "tabs": rows}
    if trees is None and groups is not None:
        # The caller gave the window tree and nothing else, which is what
        # doxa.app does: the older shape is DERIVED rather than tracked
        # separately, so the two halves of the record cannot drift.
        trees = _trees_from_groups(groups)
    if trees:
        # v0.91.0: one layout tree per TAB, in tab order -- the split
        # structure the flat list above cannot express. It rides INSIDE
        # the layout node rather than replacing its ``kind``, and that is
        # the whole compatibility story: every DOXA since v0.23.0 reads
        # the top-level ``tabs`` list first (_layout_tabs below), so a
        # record with splits in it still restores as N flat tabs under an
        # older DOXA instead of as nothing. The alternative -- writing
        # ``{"kind": "split"}`` in that slot -- would have been read by
        # v0.32.0-v0.88.0 as "nothing this version can lay out", which is
        # honest but costs the user every tab they had.
        layout["trees"] = [layout_mod.to_json(tree) for tree in trees]
    if groups is not None:
        # v0.97.0: the window's ONE tree, leaves holding groups. Same slot,
        # same principle, same compatibility story as ``trees`` above -- and
        # for the third time, the absence of this key on the next reader
        # that does not understand it is the whole of the migration.
        layout["groups"] = layout_mod.to_json(groups)
    payload = {
        "scope_key": scope_key,
        "active_session_id": active_session_id or None,
        # Written TWICE, on purpose -- see the module docstring's "layout
        # node". The top-level list is the only shape v0.23.0-v0.31.0 can
        # read and stays authoritative; the layout node is the slot the
        # split tree grew into, without a breaking format change.
        "tabs": rows,
        "layout": layout,
    }
    if collections:
        # v1.0.0: a TOP-LEVEL key, beside ``tabs`` and ``layout`` and
        # deliberately not inside the layout node. A collection is not
        # geometry: it groups sessions by NAME regardless of which region
        # shows them, so putting it in the node that describes regions
        # would be filing it under the one thing it is independent of.
        #
        # PRUNED to the flat list on the way out, so the two halves of the
        # record cannot disagree even for one write. A membership naming a
        # session no longer in ``tabs`` is the collection equivalent of a
        # tree naming a dead leaf, and the answer is the same one
        # :func:`doxa.layout.prune` gives.
        pruned = collections_mod.prune(
            collections, [row["session_id"] for row in rows]
        )
        written = collections_mod.to_json(pruned)
        if written:
            payload["collections"] = written
    if rail_folded:
        # v1.5.0. A plain list of strings and nothing richer: the ONLY
        # thing a fold is, is the absence of the default, and the default
        # is expanded. Written only when non-empty for the same reason
        # ``Collection.collapsed`` is written only when true.
        folded = sorted({str(key) for key in rail_folded if str(key or "")})
        if folded:
            payload["rail_folded"] = folded
    path = _file_for(scope_key)
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        return


def _layout_tabs(data: dict) -> "list | None":
    """The tab rows out of a raw record, top-level list FIRST.

    That order is the compatibility rule, not an accident: the top-level
    ``tabs`` list is the shape every DOXA since v0.23.0 writes and reads,
    so it is what a record from any version is guaranteed to have. The
    ``layout`` node is only consulted when the top-level list is absent or
    malformed -- which today can only happen for a record some FUTURE
    version wrote as a pure layout tree. A layout node whose ``kind`` this
    version does not recognise (``"split"``, when splits exist) reads as
    "nothing this version can lay out", which is the honest answer: better
    no restore than a split flattened into tabs behind the user's back."""
    raw = data.get("tabs")
    if isinstance(raw, list):
        return raw
    layout = data.get("layout")
    if not isinstance(layout, dict) or layout.get("kind") != "tabs":
        return None
    raw = layout.get("tabs")
    return raw if isinstance(raw, list) else None


def _layout_trees(data: dict, tabs: "list[TabRecord]") -> "tuple":
    """The saved layout trees, or one single-leaf tree per saved tab.

    The fallback IS the migration (v0.91.0). A record written by any DOXA
    from v0.23.0 to v0.88.0 has no ``trees`` key at all, and the honest
    reading of it is that every tab held exactly one pane -- which was
    true, because splits did not exist. So an old record restores as N
    single-leaf trees and behaves identically, and no version number, no
    schema field and no migration step is needed to tell the two apart:
    the absence of the key is the answer.

    A tree that reads as ``None`` (a kind this version does not know, a
    malformed child) falls back the same way, per tab, rather than
    discarding the whole record -- the flat list is still authoritative
    and still complete."""
    layout = data.get("layout")
    raw = layout.get("trees") if isinstance(layout, dict) else None
    if isinstance(raw, list):
        trees = [layout_mod.from_json(entry) for entry in raw]
        trees = [t for t in trees if t is not None]
        if trees:
            return tuple(trees)
    return tuple(
        layout_mod.Leaf(
            session_id=tab.session_id,
            pinned_name=tab.pinned_name,
            cwd=tab.cwd,
        )
        for tab in tabs
    )


def _layout_groups(
    data: dict,
    tabs: "list[TabRecord]",
    trees: "tuple",
    active_session_id: "str | None",
) -> "Any":
    """The WINDOW's layout tree, for a record from ANY of the three eras.

    See the module docstring for the rules and for why the composition
    rule is what it is. Returns a tree whose leaves are
    :class:`doxa.layout.Group` -- never ``None``, because the flat ``tabs``
    list is guaranteed non-empty by the time :func:`load` calls this, and
    "there is a record but no layout" is not a state any caller should have
    to handle.

    Every path ends in :func:`_fill_group`, which is what guarantees the
    invariant the two halves of the record depend on: **every session in
    the flat list is in exactly one group.** A saved tree that named only
    some of them (the ordinary case for eras 2 and 3) leaves the rest to be
    appended as tabs; a hand-edited tree that named one twice has the
    duplicate dropped."""
    layout = data.get("layout")
    raw = layout.get("groups") if isinstance(layout, dict) else None
    tree = layout_mod.from_json(raw) if isinstance(raw, dict) else None
    if tree is not None:
        return _fill_group(layout_mod.groupify(tree), tabs, active_session_id)
    # Era 2: one tree per TAB. The window tree is the ACTIVE tab's, so the
    # arrangement on screen survives; the rest become tabs of the group
    # that holds it.
    #
    # Read off the RAW record, never off the ``trees`` argument:
    # :func:`_layout_trees` already fell back to one single-leaf tree per
    # saved tab for a record that has no ``trees`` key at all, so that
    # argument is never empty and could not tell era 2 from era 1. Getting
    # this backwards put a pre-v0.91.0 record's ACTIVE tab first in its own
    # group's strip and every other tab after it -- a silent reorder of the
    # user's tab bar on the one path that has no geometry to justify it.
    saved_trees = layout.get("trees") if isinstance(layout, dict) else None
    if isinstance(saved_trees, list) and trees:
        chosen = trees[0]
        if active_session_id:
            for candidate in trees:
                ids = {leaf.session_id for leaf in layout_mod.leaves(candidate)}
                if active_session_id in ids:
                    chosen = candidate
                    break
        return _fill_group(layout_mod.groupify(chosen), tabs, active_session_id)
    # Era 3: no trees at all. N tabs were N tabs, one at a time, in one
    # region -- so one group holding all of them says exactly that.
    return _fill_group(None, tabs, active_session_id)


def _fill_group(
    tree: "Any",
    tabs: "list[TabRecord]",
    active_session_id: "str | None" = None,
) -> "Any":
    """Make every saved tab reachable from ``tree``, and every group's tab
    list free of duplicates.

    Sessions the tree already places keep their place. Sessions it does not
    are appended, in saved order, as tabs of the FIRST group -- first
    because reading order starts there and a tab has to land somewhere the
    user will look. A ``None`` tree means there is no geometry to preserve
    at all, so everything lands in one group.

    ``active_session_id`` is the saved active tab, and the group that ends
    up holding it is pointed AT it. That is load-bearing rather than tidy:
    the saved active session is the one thing about a restore a user
    notices immediately, and for an era-3 record (no geometry at all) the
    group's own ``active`` index is the ONLY place that fact can live --
    there is no tree to have carried it."""
    placed: "set[str]" = set()

    def rebuild(node: "Any") -> "Any":
        if isinstance(node, layout_mod.Split):
            kids = [rebuild(child) for child in node.children]
            kept: "list" = []
            weights: "list[float]" = []
            for child, weight in zip(kids, node.weights):
                if child is not None:
                    kept.append(child)
                    weights.append(weight)
            if not kept:
                return None
            if len(kept) == 1:
                return kept[0]
            return layout_mod.Split(node.orientation, tuple(kept), tuple(weights))
        group = layout_mod.as_group(node)
        kept_tabs: "list[layout_mod.Leaf]" = []
        active = 0
        was_active = group.active_tab
        for leaf in group.tabs:
            if leaf.session_id in placed:
                continue
            placed.add(leaf.session_id)
            if leaf is was_active:
                active = len(kept_tabs)
            kept_tabs.append(leaf)
        if not kept_tabs:
            return None
        return layout_mod.Group(tuple(kept_tabs), active)

    rebuilt = rebuild(tree) if tree is not None else None
    extra = [
        layout_mod.Leaf(
            session_id=tab.session_id,
            pinned_name=tab.pinned_name,
            cwd=tab.cwd,
        )
        for tab in tabs
        if tab.session_id not in placed
    ]
    if rebuilt is None:
        rebuilt = layout_mod.Group(tuple(extra), 0) if extra else None
    elif extra:
        def graft(node: "Any") -> "Any":
            if isinstance(node, layout_mod.Split):
                kids = list(node.children)
                kids[0] = graft(kids[0])
                return layout_mod.Split(
                    node.orientation, tuple(kids), node.weights
                )
            group = layout_mod.as_group(node)
            return layout_mod.Group(group.tabs + tuple(extra), group.active)

        rebuilt = graft(rebuilt)
    return _point_at(rebuilt, active_session_id)


def _point_at(tree: "Any", session_id: "str | None") -> "Any":
    """The tree with whichever group holds ``session_id`` showing it.

    Every OTHER group keeps the active tab the record gave it: a restore
    that reset four regions to their first tab because the user's keyboard
    had been in the fifth would lose four facts to record one."""
    if tree is None or not session_id:
        return tree
    if isinstance(tree, layout_mod.Split):
        return layout_mod.Split(
            tree.orientation,
            tuple(_point_at(child, session_id) for child in tree.children),
            tree.weights,
        )
    group = layout_mod.as_group(tree)
    for index, leaf in enumerate(group.tabs):
        if leaf.session_id == session_id:
            return layout_mod.Group(group.tabs, index)
    return group


def load(scope_key: str) -> "TabSetRecord | None":
    """The saved set for this scope, or ``None`` -- a missing file, a
    corrupt one, or one that resolves to zero usable tabs all read as
    "nothing to restore" alike, never a crash and never a distinction the
    caller has to make itself."""
    if not scope_key:
        return None
    path = _file_for(scope_key)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    raw_tabs = _layout_tabs(data)
    if raw_tabs is None:
        return None
    tabs: list[TabRecord] = []
    for entry in raw_tabs:
        if not isinstance(entry, dict):
            continue
        session_id = str(entry.get("session_id") or "").strip()
        if not session_id:
            continue
        pinned = entry.get("pinned_name")
        cwd = entry.get("cwd")
        tabs.append(
            TabRecord(
                session_id=session_id,
                pinned_name=str(pinned) if pinned else None,
                cwd=str(cwd) if cwd else None,
            )
        )
    if not tabs:
        return None
    active = data.get("active_session_id")
    active_id = str(active) if active else None
    trees = _layout_trees(data, tabs)
    return TabSetRecord(
        scope_key=str(data.get("scope_key") or scope_key),
        tabs=tuple(tabs),
        active_session_id=active_id,
        trees=trees,
        groups=_layout_groups(data, tabs, trees, active_id),
        # Absence of the key is the migration, for the fourth time: a
        # record with no ``collections`` reads as none, which is exactly
        # what every record written before v1.0.0 means. Pruned to the
        # flat list HERE as well as at write time -- a record can be
        # hand-edited, and a hand-edited one is still a record.
        collections=collections_mod.prune(
            collections_mod.from_json(data.get("collections")),
            [t.session_id for t in tabs],
        ),
        rail_folded=_rail_folded(data),
    )


def _rail_folded(data: Any) -> "tuple[str, ...]":
    """The rail's folded pane groups, out of whatever the file holds.

    Never raises and never validates beyond "it is a non-empty string":
    an entry key is a widget id in a window that is gone, so there is
    nothing here to check it against. A hand-edited record with a number
    in the list costs the user one fold, never a session -- the posture
    :func:`doxa.layout.normalise` takes on a corrupt weight list."""
    raw = data.get("rail_folded") if isinstance(data, dict) else None
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(
        dict.fromkeys(
            str(key).strip() for key in raw
            if isinstance(key, str) and str(key).strip()
        )
    )


def resolve(scope_key: str) -> "ResolvedRestore | None":
    """Cross-reference the saved record against the LIVE daemon registry
    for this scope. ``None`` means there is no saved record at all --
    every OTHER outcome (including "every saved tab is dead") is a real
    :class:`ResolvedRestore` so the caller can report the skip count
    rather than silently falling back to nothing.

    A session id the registry no longer knows about is never REATTACHED
    (that daemon is provably gone, and spawning a replacement would not be
    the session the user left). Since v0.32.0 it is looked up on disk
    instead: with a transcript behind it the tab comes back ARCHIVED,
    without one it is dropped and counted in ``skipped``. Neither path
    ever blocks startup and neither ever raises -- ``doxa.transcript``
    answers "no" to everything it cannot read."""
    record = load(scope_key)
    if record is None:
        return None
    live_by_id = {p.session_id: p for p in peers_mod.list_daemons(scope_key=scope_key)}
    live: "list[tuple[TabRecord, peers_mod.PeerInfo]]" = []
    archived: "list[TabRecord]" = []
    entries: "list[tuple[TabRecord, peers_mod.PeerInfo | None]]" = []
    skipped = 0
    for tab in record.tabs:
        entry = live_by_id.get(tab.session_id)
        if entry is not None:
            live.append((tab, entry))
            entries.append((tab, entry))
            continue
        # No daemon. The transcript is keyed by session id under the
        # session's OWN project slug, so the saved cwd is what finds it --
        # falling back to the scope key only for records written before
        # v0.32.0 started saving one.
        if transcript_mod.exists(tab.session_id, tab.cwd or scope_key):
            archived.append(tab)
            entries.append((tab, None))
        else:
            skipped += 1
    active = record.active_session_id
    if active is not None:
        survivors = {t.session_id for t, _ in live} | {t.session_id for t in archived}
        if active not in survivors:
            active = None  # the saved active tab is gone -- no forced pick
    return ResolvedRestore(
        tabs=live, skipped=skipped, active_session_id=active,
        archived=archived, entries=entries, trees=record.trees,
        groups=record.groups,
        # NOT re-pruned against the live registry, and that is the design
        # check docs/plans/session-sidebar.md asks for: a collection
        # member whose daemon is gone still restores as a member. It comes
        # back as an ARCHIVED tab if its transcript survived, and the rail
        # keeps a row for it either way -- a rail that could only list
        # what the tree already holds would be a second tab strip.
        collections=record.collections,
        rail_folded=record.rail_folded,
    )


def clear(scope_key: str) -> None:
    """Drop the saved record entirely -- not currently wired to any UI
    action (there is no "forget this repo's tabs" command yet), kept for
    tests and any future explicit-control surface; never raises."""
    if not scope_key:
        return
    with contextlib.suppress(OSError):
        _file_for(scope_key).unlink()
