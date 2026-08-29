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

**The layout node** (v0.32.0, filled in v0.89.0): the record ALSO
carries ``{"layout": {"kind": "tabs", "tabs": [...], "trees": [...]}}``.
That node was reserved three years of releases before it held anything --
"the day a split tree does exist the record grows a ``{"kind": "split",
...}`` node in the same slot instead of needing a format version and a
migration" -- and v0.89.0 is that day. Splits are carried in ``trees``:
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
* A record with NO ``trees`` key -- anything written before v0.89.0 --
  reads as one single-leaf tree per saved tab (:func:`_layout_trees`).
  The absence of the key IS the migration; there is no version field and
  no upgrade step.

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
v0.85.0): a session DETACHED (Ctrl+W, the palette's "Quit: detach") keeps
running and STAYS in the set, tab closed or not. A session STOPPED from
inside this window (Ctrl+Q, the palette's "Quit: stop session") also
STAYS -- through v0.55.0 it did not, because "the daemon is gone" and
"the tab is gone" were the same fact. v0.56.0 broke that equivalence:
DOXA now pins its own session id to the CLI's (``ClaudeAgentOptions.
session_id``), so ``--resume`` can replay a conversation DOXA itself
ended, and a saved id with no live daemon behind it is resolved by THIS
function exactly like any other -- archived if the transcript survived,
dropped if it did not, with no memory of which of Ctrl+Q, a linger
timeout or ``doxa stop`` from another terminal put it there.

**One exception** (v0.85.0): closing the LAST open tab, by EITHER key,
never writes that tab's session into the set at all --
``DoxaApp._close_pane``'s own ``is_last`` branch, not this module.
Reported live in two parts: Ctrl+Q on the last tab should start the next
launch fresh (it used to come back archived, read-only); Ctrl+W on the
last tab should ALSO start fresh, even though -- unlike Ctrl+Q -- the
session is still running and reattachable by NAME (``/attach``, the
peers chip). A window with zero tabs left has nothing left to restore
automatically; that the Ctrl+W session is still there to attach TO is a
fact about the live daemon registry (``doxa.peers``), never about this
record.

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
    #: One :mod:`doxa.layout` tree per saved TAB, in tab order (v0.89.0).
    #: NEVER empty on a record this version reads: a record written before
    #: splits existed has no trees in it, and :func:`load` derives one
    #: single-leaf tree per saved tab instead -- "a new reader must
    #: restore old flat records as single-leaf trees", implemented once,
    #: here, so no caller has to know which kind of record it got.
    trees: "tuple" = ()


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
    #: The saved layout trees (v0.89.0), one per saved TAB, in saved tab
    #: order and UNPRUNED -- which sessions survived is the caller's
    #: cross-check, already answered by ``tabs``/``archived``/``skipped``
    #: above, and pruning here would mean answering it twice. doxa.app's
    #: ``_restore_trees_in_order`` does the pruning against the specs it
    #: actually built.
    trees: "tuple" = ()

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


def save(
    scope_key: str,
    tabs: "list[TabRecord]",
    active_session_id: "str | None",
    trees: "list | None" = None,
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
    if trees:
        # v0.89.0: one layout tree per TAB, in tab order -- the split
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

    The fallback IS the migration (v0.89.0). A record written by any DOXA
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
    return TabSetRecord(
        scope_key=str(data.get("scope_key") or scope_key),
        tabs=tuple(tabs),
        active_session_id=str(active) if active else None,
        trees=_layout_trees(data, tabs),
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
    )


def clear(scope_key: str) -> None:
    """Drop the saved record entirely -- not currently wired to any UI
    action (there is no "forget this repo's tabs" command yet), kept for
    tests and any future explicit-control surface; never raises."""
    if not scope_key:
        return
    with contextlib.suppress(OSError):
        _file_for(scope_key).unlink()
