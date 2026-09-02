# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.collections -- user-named groupings of SESSIONS, for the rail.

Pure data and pure functions, the rule :mod:`doxa.layout` and
:mod:`doxa.ui.labels` already follow: the parts of a grouping that are
hard to get right (what serialises, what happens to a member whose
session died, what "at most one collection" means when a record says
otherwise) are the parts that must be testable without a running app.

**Why the word.** ``group`` was already taken, and means something else:
v0.97.0's :class:`doxa.ui.split.PaneGroup` is a REGION OF SCREEN owning
its own tab strip. This module groups sessions *by name*, regardless of
where they are shown. Two sessions in one collection may sit in different
``PaneGroup``s, and a ``PaneGroup`` may show tabs from three collections.
So: **collection**, in the code, in the record and in the UI, and never
``group`` alone.

**A collection is** a name the user typed, an ORDERED list of session
ids, and whether it is collapsed in the rail. Nothing else -- notably not
a colour, not a repo and not a rule: docs/plans/session-sidebar.md puts
auto-grouping by repo or branch explicitly out of scope, because a
collection is a thing the user *decides*, not a thing DOXA infers.

**A session belongs to at most one collection**, and that is enforced
HERE rather than trusted: :func:`assign` removes the session from every
other collection on the way in, and :func:`from_json` drops a second
mention of an id it has already seen. A record hand-edited to name one
session twice is a record, not an exception.

**Sessions in no collection are not a collection.** They render under an
implicit, unnamed, always-last heading (:func:`loose`), which is derived
at render time and never written down -- the moment it were persisted it
would be a collection with a reserved name, and renaming or deleting it
would be a case every function here would have to carry.

**Names are ids' companions, never their replacement.**
``SessionPane.display_name()`` is not stable -- it changes when a session
is renamed or when its first prompt lands -- so a collection stores
session IDS and the rail renders names fresh every time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

#: Longest collection name the record will carry. The rail is a rail --
#: a name past this is not a label any more, it is a paragraph in a
#: 22-column column (:data:`doxa.layout.SIDEBAR_WIDTH`), and the rail
#: would ellipsize it on every single render. Trimmed at the MODEL rather
#: than at the widget so the record can never hold a name no surface can
#: show, the same posture :func:`doxa.layout.clamp_prompt_ratio` takes on
#: a ratio no pane can render.
NAME_MAX = 48


@dataclass(frozen=True)
class Collection:
    """One user-named grouping of sessions.

    ``sessions`` is an ORDER, not a set: the user put them in that order
    and a rail that reshuffled them on every load would be a rail nobody
    trusts. Frozen and normalised at construction, like
    :class:`doxa.layout.Group` -- a duplicate id inside one collection is
    dropped here rather than at each of the three places that read it."""

    name: str
    sessions: "tuple[str, ...]" = ()
    collapsed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", clean_name(self.name))
        seen: "set[str]" = set()
        kept: "list[str]" = []
        for raw in self.sessions:
            session_id = str(raw or "").strip()
            if not session_id or session_id in seen:
                continue
            seen.add(session_id)
            kept.append(session_id)
        object.__setattr__(self, "sessions", tuple(kept))
        object.__setattr__(self, "collapsed", bool(self.collapsed))


def clean_name(name: Any) -> str:
    """A collection name as the record will carry it: stripped, collapsed
    to single spaces, cut at :data:`NAME_MAX`.

    Never raises and never returns ``None``: a name that cleans to the
    empty string is a name the caller must refuse (:func:`new` and
    :func:`rename` both do, by returning an explanation), not a state the
    record has to be able to hold."""
    text = " ".join(str(name or "").split())
    return text[:NAME_MAX]


def _key(name: Any) -> str:
    """Case-folded match key. Two collections called ``ampiric`` and
    ``Ampiric`` are one collection the user typed twice, and the rail
    would show them as two headings that both look right."""
    return clean_name(name).casefold()


# -- serialisation ----------------------------------------------------
#
# The wire shape is a list of flat dicts under the record's OWN top-level
# ``collections`` key -- beside ``tabs`` and ``layout``, never inside the
# layout node. That placement is the whole compatibility story and it is
# doxa.tabsets' rule restated: absence of the key is the migration, the
# flat ``tabs`` list stays authoritative, and ``layout.kind`` stays
# "tabs", so every reader from v0.23.0 on sees a record it fully
# understands and simply does not know about the grouping.


def to_json(items: "Sequence[Collection]") -> "list[dict]":
    """Collections as plain JSON-able dicts, empty ones dropped.

    An empty collection is not written, and that is deliberate rather
    than tidy: a collection whose every session died is indistinguishable
    from a heading the user forgot about, and a rail full of empty
    headings is the "placeholder row" this house does not ship. The user
    can always make it again; the record cannot decide for them which of
    the two an empty one was.

    ``collapsed`` is written only when true, so a record whose
    collections are all expanded is byte-identical to the obvious hand
    shape and diffs cleanly against the ones already on disk -- the same
    reason :func:`doxa.layout.to_json` omits a default ``view``."""
    out: "list[dict]" = []
    for item in items:
        if not item.name or not item.sessions:
            continue
        row: dict = {"name": item.name, "sessions": list(item.sessions)}
        if item.collapsed:
            row["collapsed"] = True
        out.append(row)
    return out


def from_json(data: Any) -> "tuple[Collection, ...]":
    """Read collections off a record. Never raises: anything malformed
    reads as "no collections", the answer :func:`doxa.tabsets.load` gives
    a malformed record and :func:`doxa.config.load` gives a broken
    settings file. A grouping is chrome; a corrupt one costs the user
    their arrangement, never their session.

    Enforces the two invariants on the way IN, so no reader downstream
    has to: names are unique (case-folded, first wins) and a session id
    appears in at most one collection (again, first wins)."""
    if not isinstance(data, list):
        return ()
    out: "list[Collection]" = []
    names: "set[str]" = set()
    placed: "set[str]" = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = clean_name(entry.get("name"))
        if not name or _key(name) in names:
            continue
        raw = entry.get("sessions")
        if not isinstance(raw, list):
            continue
        sessions = []
        for value in raw:
            session_id = str(value or "").strip()
            if not session_id or session_id in placed:
                continue
            placed.add(session_id)
            sessions.append(session_id)
        if not sessions:
            continue
        names.add(_key(name))
        out.append(
            Collection(name, tuple(sessions), bool(entry.get("collapsed")))
        )
    return tuple(out)


# -- queries ----------------------------------------------------------


def find(items: "Sequence[Collection]", name: Any) -> "Collection | None":
    """The collection by that name, matched case-insensitively."""
    key = _key(name)
    if not key:
        return None
    return next((c for c in items if _key(c.name) == key), None)


def collection_of(
    items: "Sequence[Collection]", session_id: str
) -> "Collection | None":
    """The one collection holding this session, or ``None`` for a loose
    one. Singular by construction -- see the module docstring."""
    if not session_id:
        return None
    return next((c for c in items if session_id in c.sessions), None)


def loose(items: "Sequence[Collection]", session_ids: "Iterable[str]") -> "list[str]":
    """The sessions in NO collection, in the order given.

    The implicit, unnamed, always-last heading of the rail, derived here
    and never persisted. The order is the caller's -- the rail passes
    strip order, so a loose session sits where the tab bar has it."""
    held = {s for c in items for s in c.sessions}
    return [s for s in session_ids if s and s not in held]


def prune(
    items: "Sequence[Collection]", keep: "Iterable[str]"
) -> "tuple[Collection, ...]":
    """The collections with every session not in ``keep`` dropped, and
    every collection that thereby lost all of them dropped with it.

    This is :func:`doxa.layout.prune` one shelf over, and it exists for
    the same reason: the saved record names sessions, and by the time it
    is read some of those sessions are gone. docs/plans/session-sidebar.md
    states the rule outright -- "a session id in a collection but not in
    ``tabs`` is dropped on load, the way ``prune`` already drops dead
    leaves"."""
    keep_set = {s for s in keep if s}
    out: "list[Collection]" = []
    for item in items:
        survivors = tuple(s for s in item.sessions if s in keep_set)
        if survivors or not item.sessions:
            # A collection that was ALREADY empty survives; one that LOST
            # every member does not. The distinction is load-bearing and
            # was found by a test rather than reasoned about: ``new``
            # makes an empty collection on purpose (the user is about to
            # move a session into it), and a prune that could not tell
            # "never had any" from "has none left" deleted it between the
            # two commands.
            out.append(Collection(item.name, survivors, item.collapsed))
    return tuple(out)


# -- edits ------------------------------------------------------------
#
# Every one of these returns ``(collections, note)``: the new tuple, and
# either None or the ONE line the user is told. A refusal that returns
# the collections unchanged and says why is the shape doxa.layout's
# split_refusal established -- the only kind of refusal a user can act
# on.


def new(
    items: "Sequence[Collection]", name: Any
) -> "tuple[tuple[Collection, ...], str | None]":
    """Make an empty collection. Empty is legal HERE and merely not
    PERSISTED (see :func:`to_json`): the user types ``/collection new
    ampiric`` and then moves sessions into it, and a collection that
    vanished between those two commands would be unusable."""
    clean = clean_name(name)
    if not clean:
        return tuple(items), "a collection needs a name"
    if find(items, clean) is not None:
        return tuple(items), f"there is already a collection called {clean!r}"
    return tuple(items) + (Collection(clean),), None


def rename(
    items: "Sequence[Collection]", old: Any, new_name: Any
) -> "tuple[tuple[Collection, ...], str | None]":
    """Rename one collection IN PLACE in the order -- the rail's heading
    order is the user's, and a rename is not a reorder."""
    target = find(items, old)
    if target is None:
        return tuple(items), f"no collection called {clean_name(old)!r}"
    clean = clean_name(new_name)
    if not clean:
        return tuple(items), "a collection needs a name"
    clash = find(items, clean)
    if clash is not None and clash is not target:
        return tuple(items), f"there is already a collection called {clean!r}"
    return (
        tuple(
            Collection(clean, c.sessions, c.collapsed) if c is target else c
            for c in items
        ),
        None,
    )


def delete(
    items: "Sequence[Collection]", name: Any
) -> "tuple[tuple[Collection, ...], str | None]":
    """Drop a collection. Its sessions are NOT touched -- they become
    loose and reappear under the implicit heading, which is the only
    honest reading of "delete the grouping": a grouping is a label, and
    deleting a label must never be a way to lose a session."""
    target = find(items, name)
    if target is None:
        return tuple(items), f"no collection called {clean_name(name)!r}"
    return tuple(c for c in items if c is not target), None


def assign(
    items: "Sequence[Collection]", name: Any, session_id: str
) -> "tuple[tuple[Collection, ...], str | None]":
    """Move ``session_id`` into the collection called ``name``, creating
    it if it does not exist.

    Creating on demand is the point of the command: "put this session in
    ampiric" is one intention whether or not ampiric already exists, and
    making the user run two commands to express it would be ceremony.

    **At most one collection** is enforced here and nowhere else: the id
    is removed from every other collection before it is appended to this
    one, so the invariant cannot be violated by a caller that forgot."""
    session_id = str(session_id or "").strip()
    if not session_id:
        return tuple(items), "no session to move"
    clean = clean_name(name)
    if not clean:
        return tuple(items), "a collection needs a name"
    target = find(items, clean)
    if target is not None and session_id in target.sessions:
        # Already there: a NO-OP that keeps its position, not a move to
        # the end. The order in a collection is the user's, and running
        # the same command twice must not quietly reorder it.
        return tuple(items), None
    items = tuple(items) if target is not None else tuple(items) + (
        Collection(clean),
    )
    target = find(items, clean)
    assert target is not None  # just created above if it was missing
    out: "list[Collection]" = []
    for item in items:
        if item is target:
            out.append(
                Collection(
                    item.name, item.sessions + (session_id,), item.collapsed
                )
            )
            continue
        survivors = tuple(s for s in item.sessions if s != session_id)
        out.append(Collection(item.name, survivors, item.collapsed))
    return tuple(out), None


def unassign(
    items: "Sequence[Collection]", session_id: str
) -> "tuple[tuple[Collection, ...], str | None]":
    """Take a session OUT of whatever collection holds it -- back under
    the implicit heading. The collection survives even if it is now
    empty, for the reason :func:`new` gives; only :func:`to_json` decides
    an empty one is not worth writing down."""
    session_id = str(session_id or "").strip()
    holder = collection_of(items, session_id)
    if holder is None:
        return tuple(items), "this session is not in a collection"
    return (
        tuple(
            Collection(
                c.name, tuple(s for s in c.sessions if s != session_id), c.collapsed
            )
            if c is holder else c
            for c in items
        ),
        None,
    )


def set_collapsed(
    items: "Sequence[Collection]", name: Any, collapsed: bool
) -> "tuple[Collection, ...]":
    """Collapse or expand one collection in the rail. No note and no
    refusal: a name that matches nothing is a click on a heading that has
    just gone away, which is a no-op and not an error."""
    target = find(items, name)
    if target is None:
        return tuple(items)
    return tuple(
        Collection(c.name, c.sessions, bool(collapsed)) if c is target else c
        for c in items
    )
