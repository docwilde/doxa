# The session sidebar — a permanent rail beside the whole window

Status: **draft for review**. Nothing implemented. Written before the work
because it is the first chrome that is NOT part of the layout tree, and
getting that boundary wrong is how it ends up inside a split.

## What provoked it

Requested 2026-09-02: *"a permanent, collapsible pane on the left across
split panes (unaffected by pane splits), allowing to group sessions with
editable session group labels."*

Two things at once, and they are separable:

1. **A rail that is not a pane.** Always on the left, spanning the full
   height, unaffected by every split and by which group has focus.
2. **User-named groupings of sessions**, listed in that rail.

## The boundary that makes it work

`DoxaApp.compose` yields the window as `split_mod.chain(...)` — a chain of
`SplitBox`es whose outermost is `_window_root()`. **Every split, every
group, every pane lives strictly inside that root.**

The sidebar goes **beside** it, as a sibling in a horizontal container:

```
Screen
└── Horizontal
    ├── SessionSidebar        ← this document
    └── SplitBox (window root)  ← v0.97.0's tree, untouched
```

That one placement is the whole design. It means:

- `_window_root()` still returns the outermost `SplitBox` — the sidebar is
  not one, so the accessor needs no change and no `isinstance` special case
- splits, `Alt+arrow` growth, directional focus and `_pane_regions` all
  operate on the tree and never see the rail
- collapsing the rail changes the tree's width and nothing else

**The trap this avoids:** making the sidebar a `Leaf` with a new `view`
kind. That is the cheap-looking route — v0.92.0 added `Leaf.view` for the
diff and it worked — and it is wrong here. A leaf can be split, closed,
moved between groups and persisted per group; a rail must be none of those.
The first `Alt+D` on the sidebar would prove it.

## Groups of sessions, and the word "group"

**`group` is already taken** and means something else: v0.97.0's `PaneGroup`
is a region of the screen owning its own tab strip. This feature groups
sessions *by name*, regardless of where they are shown. Two sessions in the
same **collection** may sit in different `PaneGroup`s, and a `PaneGroup` may
show tabs from three collections.

Use **collection** throughout — in the code, the record and the UI — and
never `group` alone. If a better word turns up during the build, use it, but
it must not be `group`.

A collection is:

- a **name**, user-editable
- an **ordered list of session ids**
- **collapsed or expanded** in the rail

A session belongs to **at most one** collection. Sessions in none appear
under an implicit, unnamed, always-last heading — not a real collection, and
not persisted as one. Hide-at-zero applies: with no collections and one
session, the rail has nothing to say and should default hidden.

## The rail's contents

Per row, a session: its `display_name()`, and the state marks the tab strip
already carries — `-done-unseen`, the needs-input blink, the `-staged` tint.
**Those marks are the reason this feature is worth building**: a session in
a background tab of an unfocused group is invisible today, and the rail is
the one surface that can show every session at once.

Read how the tab header renders them (v0.97.0 ORs them over a group's
leaves) and reuse it. Do not re-derive the meaning of a mark in a second
place — that is how two surfaces come to disagree.

Selecting a row reveals that session: focus its group, activate its tab. If
it is not currently mounted anywhere (an archived record, a detached peer),
the row says so rather than pretending it can be focused.

## Keys and commands

`Ctrl+B` for toggle — the conventional sidebar key (VS Code, tmux's prefix
notwithstanding), and free here: measured against `DoxaApp.BINDINGS` at
v0.98.0, `ctrl+b` is unbound. **Re-verify at build time** — the binding set
moved three times in this release series.

`ctrl+<letter>` is deliverable under both keyboard encodings (`doxa/keyboard.py`),
unlike `ctrl+<digit>` and `alt+<letter>`, both of which this project chose
and had to walk back. Do not repeat that.

Commands, because every action needs one that does not depend on an
encoding: `/sidebar` (toggle), `/collection new|rename|delete`, and moving a
session into a collection. Match the `SlashCommand` registry's existing
shape, **including a `binding=` field** — three commands shipped without one
in v0.92.0 and their keys were invisible to `/help` and to the key notice.

## Persistence

The rail's own state — collapsed or not, its width — is **window chrome, not
layout**, and belongs in the settings registry beside `context_grid` and
`adopt_plugins`, not in the tabset record.

Collections ARE session state and belong in the tabset record, in their own
top-level key beside `tabs` and `layout`:

```json
{"tabs": [...], "layout": {...}, "collections": [
  {"name": "ampiric", "sessions": ["abc", "def"], "collapsed": false}
]}
```

The rules that held through three format changes hold again: **absence of
the key is the migration** (no `collections` reads as none), the flat `tabs`
list stays authoritative, and `layout.kind` stays `"tabs"` so older readers
see nothing they must understand. A session id in a collection but not in
`tabs` is dropped on load, the way `prune` already drops dead leaves.

## Risks, named

1. **Width at 80 columns.** A rail costs columns the tree cannot use. At 80
   with one split, each group is already at `MIN_LEAF_WIDTH` (34). The rail
   must have a minimum width, and below some total width it must refuse to
   open rather than squeeze the tree under its own minimum —
   `SIDE_BY_SIDE_MIN_COLS` (100) and the group tab-strip thresholds (34/17)
   are the precedent, and the number must be **measured, not chosen**.
2. **Textual cannot re-parent a mounted widget** (measured, v0.91.0).
   Wrapping the existing root in a new `Horizontal` at runtime is therefore
   impossible; the container must exist from `compose`, with the rail
   hidden when off. Same reason `split_mod.chain` pre-makes empty boxes.
3. **A second place that renders session state.** The rail and the tab
   strips will disagree the day one is updated and the other is not. One
   source, read twice — decide where it lives and say so in the code.
4. **`display_name()` is not stable** — it changes when a session is renamed
   or its first prompt lands. The record stores session **ids**; names are
   rendered fresh every time.

## Scope

**In:** the rail, collapse/expand, collections with editable names,
membership, reveal-on-select, persistence, the width refusal.

**Out:** drag and drop; nesting collections; collections shared between
windows or machines; auto-grouping by repo or branch (tempting, and a
different feature — a collection is a thing the user *decides*, not a thing
DOXA infers).

## The check this spec owes

v0.91.0 asked whether its layout could express its first consumer and found
the model could not. v0.97.0 asked whether a group could hold a diff leaf
and found it could. The question here: **can the rail show a session that is
not mounted in any group?** A detached peer, an archived transcript, a
collection member whose tab was closed. If the rail can only list what the
tree already contains, it is a second tab strip rather than a session index,
and the design is wrong.
