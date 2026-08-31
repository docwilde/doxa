# Pane groups — each region owns its own tabs

Status: **implemented in v0.97.0**, in scope. Written before the work
because it inverts a hierarchy that shipped three releases ago, and the
migration is the expensive half.

What shipped, and where it differs from what is written below:

- The model inversion, the per-group tab strip, per-group `Ctrl+←/→`,
  `Ctrl+1`…`Ctrl+9` + `/pane`, the number overlay, `/movepane <n>`, the
  three persistence eras, the focus rules. `doxa/layout.py`'s `Group`,
  `doxa/ui/split.py`'s `PaneGroup`, `tests/test_pane_groups.py`.
- **The composition rule this document left open.** It says how a LEAF
  reads in each era and not how N per-tab trees become the window's ONE
  tree. Implemented as: the window tree is the tree of the tab that was
  ACTIVE, and every other saved tab becomes a TAB of the group holding
  it. A pre-v0.91.0 record therefore restores as one group holding N
  tabs. Read the other way -- a group per saved tab -- five saved tabs
  would restore as a five-way split, 16 columns each on an 80-column
  terminal, below `MIN_LEAF_WIDTH` (34) and so below the width at which
  DOXA's own `split_refusal` will make a split at all. A restore that
  produces an arrangement the app refuses to produce interactively is not
  a migration. See `doxa/tabsets.py`'s module docstring.
- **Moving a tab still has no key.** The command shipped, as this
  document asks; the spelling stayed deferred.
- Out, as scoped: floating windows, detaching a pane to its own terminal,
  mouse drag of a tab, per-group status bars.

## What provoked it

Reported 2026-08-31, after using v0.91.0's splits: *"the new sessions have
no tab menu of their own… If i switch tabs, the split out sessions go with
the tab. Shouldn't the split out sessions be independent?"* — then the
shape: *"or…each pane has its own tab header bar?"*

That is the VS Code editor-group model, and it is a better fit than what
shipped.

## The inversion

```
today      window → tabs → each tab owns a layout tree of panes
proposed   window → layout tree of GROUPS → each group owns its own tabs
```

`PaneTab` stops being the root that holds a tree and becomes the thing a
**leaf** holds. `layout.Leaf` stops carrying a session and carries a
**group**: an ordered list of tabs plus which one is active.

## Why, concretely

DOXA's thesis is several agents working at once. The current model cannot
express the thing that makes that usable: **three sessions cycling on the
left while a fourth stays pinned on the right.** Every tab switch moves the
whole screen, because there is one tab strip and it owns everything.

It also dissolves three problems instead of patching them:

- **Independence** — the reported defect. Switching tabs in one group
  leaves every other group alone.
- **Breakout / join** become "move a tab between groups", ordinary
  drag-and-drop semantics, rather than two bespoke commands
  (`/breakout`, `/join`) that only exist because panes are trapped.
- **Closing a tab closes ONE session.** Today closing a tab closes every
  pane in it — with three panes that can end two sessions the user did not
  mean to end, and the only mitigation available is a confirm dialog.

Each pane also gets a visible handle, which is the half of the report that
was about discoverability rather than lifetime.

## What survives untouched

The tree machinery does not care what a leaf contains. Re-rooting, not
rebuilding:

- `doxa/layout.py`'s `Split`, weights, `neighbour`, `khop`-style walks
- `doxa/ui/split.py`'s `SplitBox`, the divider, `_pane_regions`
- Directional focus (`Ctrl+Shift+arrow`), grow (`Alt+arrow`), the in-pane
  divider (`Ctrl+Up/Down`)
- v0.92.0's `Leaf.view` — a diff is still a surface a leaf holds; it
  becomes one of the things a group's tab can be

## What changes

### The model

`Leaf` gains a group instead of a session:

```
Leaf(session_id, view, prompt_ratio, …)        # today
Group(tabs=[TabRecord, …], active=<index>)     # proposed
```

`TabRecord` is what the flat `tabs` row already carries — session id,
pinned name, cwd, view, prompt ratio. **This is deliberate**: the flat list
stays the authoritative migration path, and one reader still works against
either shape.

### Persistence — the third format, and the last one

v0.32.0 reserved `{"layout": {"kind": "tabs", "tabs": [...]}}`; v0.91.0
filled in `trees`. This adds `groups` on the same principle, and the same
rules hold:

- **`layout.kind` stays `"tabs"`.** Writing anything else is read by every
  DOXA from v0.32.0 to now as *nothing to restore*, which costs the user
  every tab. That has been true through two format changes and is not
  worth revisiting.
- **The flat `tabs` list stays authoritative**, carrying every session in
  layout order, so an old DOXA restores a grouped record as N flat tabs.
- **Absence of the key is the migration.** No version field: a record with
  `trees` but no `groups` reads as one single-tab group per leaf, exactly
  the v0.91.0 shape; a record with neither reads as one group per tab.

Three eras must round-trip, and the test must use **three or more** of
each — v0.91.0's own spec notes the old two-tab test passed only because
the saved tab happened to be last.

### Focus

v0.38.0's rule holds and is inherited again: focus follows explicit user
intent, a new surface mounts unfocused, and whatever creates it says where
the keyboard goes. What must be re-decided at group level:

- **Exactly one group holds keyboard focus**; the status bar reflects that
  group's active tab.
- **A group's active tab keeps running when the group is unfocused** — the
  v0.91.0 rule that visible and focused are different states, now one level
  up.
- **An inactive tab inside a visible group is neither visible nor
  focused.** It keeps running (it always did), but `-done-unseen`, the
  needs-input blink and the `-staged` tint must NOT clear for it. v0.91.0
  settled that a visible-but-unfocused pane is *not* seen; an invisible tab
  is a stronger case of the same thing.

### Keys

`Ctrl+←/→` already cycles tabs — it cycles **within the focused group**,
which is the whole point of the inversion. No change to its spelling.

**Jump to a group by position: `Ctrl+1` … `Ctrl+9`** (chosen by the owner,
2026-08-31). Groups are numbered in **reading order — left to right, then
top to bottom** — so in a 2×2, `Ctrl+1` is upper left, `Ctrl+2` upper
right, `Ctrl+3` lower left, `Ctrl+4` lower right. Position is predictable
in a way "next group" is not, which is the same argument that made focus
movement directional rather than cyclic in v0.91.0. Numbering is derived
from the painted rectangles (`_pane_regions`), not from tree order: what
the user counts is what is on screen.

**`Ctrl+<digit>` is unreachable under the legacy encoding** — `Ctrl` has a
C0 code only for the 26 letters and `@ [ \ ] ^ _ ? space`; a digit produces
no byte at all (`doxa/keyboard.py`). It therefore works on terminals
speaking the kitty protocol and does nothing elsewhere. That is
**acceptable and already precedented**: `Ctrl+,` (settings) and `Ctrl+Tab`
are in the same bucket and ship, registered in `unreachable_under_legacy`
so `/help` and `/doctor` say out loud where they do not work. These must be
registered the same way, and **`/pane <n>` is the door that always works.**

The two alternatives are worse, and were rejected rather than overlooked:
`Alt+<digit>` is terminal tab-switching in GNOME Terminal and others, and a
tmux-style prefix chord costs two keystrokes for a gesture meant to be
instant.

**The number overlay** (owner, 2026-08-31): pressing ANY `Ctrl+<digit>`
paints a brief overlay on every group showing its own number. The jump
still happens immediately — the overlay is feedback and teaching, not a
mode: DOXA does not wait for a second keystroke the way tmux's
`display-panes` does, because the numbering is meant to become muscle
memory and a prompt-then-wait gesture never lets it.

Consequences to settle:

- It fires on `Ctrl+<digit>` even when the digit names no group — pressing
  `Ctrl+7` in a two-group layout shows `1` and `2` and moves nothing. That
  is the case where the overlay earns the most: it answers "what are my
  choices" for a user who guessed.
- **It needs a one-shot timer to dismiss**, and DOXA has a no-timer rule.
  The rule's target is idle CPU (see v0.78.0, which amended it for the
  turn spinner on the grounds that a timer existing only during a turn
  spends nothing). A `set_timer` armed by a keystroke and fired once is
  the same bargain: no interval, nothing running while idle. Use
  `set_timer`, never `set_interval`, and cancel on any subsequent key.
- Drawn per group over its own region, from the same `_pane_regions`
  rectangles the numbering is derived from — so what is numbered and what
  is painted cannot disagree.
- A single-group window (no splits at all) shows nothing: there is no
  choice to make. Hide-at-zero, as everywhere else.

Moving a tab **between** groups still needs its own gesture. `Ctrl+Shift+←/→`
is taken by directional focus. Defer the spelling until v0.95.0 reports what
this terminal actually delivers, and give it a command (`/movepane <n>` or
similar) from the start regardless.

## Risks, named

1. **This is a refactor of a three-release-old feature.** The argument for
   doing it now rather than later is that the migration cost only grows
   once there are saved layouts anyone cares about.
2. **Textual cannot re-parent a mounted widget** — measured in v0.91.0,
   `mount` of a mounted widget is a silent no-op that orphans it. Moving a
   tab between groups therefore cannot move the widget; it must be
   re-created in the destination and torn down in the source, and the
   session (which lives in the daemon, not the widget) must survive that
   untouched. **This is the single hardest constraint in this document.**
3. **Two tab strips is more chrome.** At 80 columns a split is 40 wide and
   a tab strip inside it may be unreadable. Below a measured threshold a
   group should render its strip compactly or not at all — the same
   hide-at-zero discipline `CTX_ABSOLUTE_MIN_COLS` and
   `SIDE_BY_SIDE_MIN_COLS` (100) already apply.
4. **The status bar belongs to a pane, not the window.** Already true
   since v0.91.0; grouping does not change it but does make "which one"
   ambiguous more often.

## Scope

**In:** the model inversion, the group tab strip, per-group cycling, moving
a tab between groups, persistence across all three eras, focus rules.

**Out:** floating windows; detaching a pane to its own terminal; dragging a
tab with the mouse (keyboard first, mouse when the gesture is settled);
per-group status bars.

## The check this spec owes

v0.91.0 asked whether its layout could express its first consumer, and the
answer was *"nearly — the geometry worked, the model could not"*. The
equivalent question here: **can a group hold a diff leaf?** v0.92.0's diff
is a surface, not a session, and if a group's tab list cannot carry one
without a special case, the group model is wrong in the same way `Leaf`
was.

**Answered, and the answer is yes without qualification.** A group's tab
list is a list of SURFACES: `PaneTab` holds one surface, `PaneGroup.
layout_group()` asks each tab for its own `layout_leaf()`, and `Leaf.view`
— which v0.92.0 added for exactly this reason one level down — carries
which kind it is. `/diff` opens the diff as a tab of a new group beside
the session's, and nothing in `PaneGroup`, `layout.Group`, the persistence
reader or the tab-move path mentions diffs at all. The one place the word
appears is `_tab_for` in `compose`, which has to know which WIDGET to
build — the same single branch `_leaf` had in v0.91.0, not a new one.

The stronger form of the check, which is what makes it an answer rather
than a claim: `tests/test_pane_groups.py::test_a_group_can_hold_a_diff_
leaf` asserts the group's own model comes back with `is_diff` set, and
`tests/test_live_diff.py` reads the diff's position out of the WINDOW tree
through `layout.groups()` — the generic walk, with no diff-shaped
accessor.
