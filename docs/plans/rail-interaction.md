# The rail as a switcher — hover, headings, a moveable divider, and what a click reveals

Status: **approved, not implemented**. Item 3 resolved by the owner
2026-09-04: **option C** — entries stay pane groups, and their rows expand
to the group's tabs. Splits and the live-diff layout are kept; A and B are
closed.

## What provoked it

Reported 2026-09-04 after using v1.2.0's rail:

1. *"highlighting an entry by hovering over it with the mouse is missing"*
2. *"the group header label should be highlighted by having a background
   colour (and then text in black or white, depending on what is a better
   contrast)"*
3. *"the pane groups should be switched, not between panes shown at the
   same time, otherwise clicking on an entry on the tab groups only
   changes focus, but not what's visible"*
4. *"the divider between the tab group tree and the panes to the right
   should be moveable"*

Items 1, 2 and 4 are additive and uncontroversial. **Item 3 is a design
reversal** and the rest of this document is mostly about it.

## Items 1, 2, 4 — the cheap half

**Hover.** Rows are `Static`s with `can_focus = False` (v1.0.0, and that
stays — a focusable widget beside the prompt is a second place
`App.AUTO_FOCUS` can land, the v0.85.0 defect). Hover is CSS `:hover`, no
focus involved. Textual gives `Enter`/`Leave` if a class is needed.

**Heading contrast.** A background on the heading, with the text picked
for contrast rather than fixed. **Compute it, do not eyeball it**: relative
luminance per WCAG, black above the threshold and white below. The project
already refuses "looks fine to me" — v1.2.0's colours are a fixed palette
resolved by name so a theme change re-resolves. Contrast is the same rule
one level down, and a hardcoded `black` on a mid-tone project colour is
exactly the failure it prevents.

**The divider.** `Ctrl+Up/Down` already moves the IN-PANE divider and
`Alt+arrow` grows a leaf, both since v0.91.0. The rail's own edge has
neither, and it should behave like the dividers that already exist:
draggable with the mouse AND adjustable from the keyboard, because a
mouse-only control is unreachable for a keyboard user. Its width persists
in the settings registry beside the collapsed flag — rail chrome, not
layout. The measured floors are already there: `SIDEBAR_MIN_WIDTH` 19,
`SIDEBAR_MAX_WIDTH` 38, `SIDEBAR_MIN_COLS` 53, and `split_refusal`'s
narrowest-painted-group rule. **Dragging must refuse at the same floor it
refuses to open at**, or a drag can produce an arrangement the app will
not create.

## Item 3 — the reversal, stated plainly

**What v0.97.0 decided**: the window is a tree of pane GROUPS shown
*simultaneously*; each group owns its own tabs. It was built to answer a
reported defect — *"if i switch tabs, the split out sessions go with the
tab"* — and it fixed it.

**What v1.2.0 then did**: made a rail entry a pane group.

**The consequence the owner hit**: if every group is already on screen,
clicking its rail entry cannot reveal anything. `reveal_session` does call
`_switch_to_tab` (activate, marker, focus) — the code is not broken. There
is simply nothing to switch TO. The rail became a focus mover, which is not
what a session index is for.

Three ways out, and the spec must pick one rather than leave it:

- **A. Groups switch.** Only one group fills the window; the rail is the
  switcher. Simple, and it is what the report asks for — but it deletes
  side-by-side, which was itself built from a report, and takes the live
  diff (v0.92.0, session left / diff right) with it. **Rejected on that
  ground alone unless the owner wants it.**
- **B. The rail lists TABS, not groups.** Reverts v1.2.0's Part 1b. A
  click then reveals a genuinely hidden thing — the inactive tab of a
  group — which is the case the rail was built for. Loses the aggregate
  state per group, which was Part 1b's whole point.
- **C. Entries stay groups; rows expand to their tabs.** The heading is
  the group (aggregate state, colour, count — all of Part 1b kept), and
  its rows are the tabs, which ARE hidden and CAN be revealed. A click on
  a tab row switches that group's active tab; a click on the group
  heading focuses the group.

**C, chosen by the owner 2026-09-04.** It is the only one that keeps both
shipped features. It also makes the rail's own structure honest — a group
holding three tabs currently renders as one row with a count, which is
exactly the information a reader cannot act on.

What C requires, concretely:

- **The heading is the group**: its colour, its aggregate state (Part 1b's
  most-urgent-wins over members), its count, and a click that focuses the
  group without changing which tab is active.
- **The rows are its tabs**, each with its own state — and a tab's own
  marks, not the group's roll-up, or two rows under one heading would
  claim the same thing.
- **A click on a tab row switches that group's active tab** and focuses
  it. That is the reveal the rail has been unable to perform.
- **Expansion is per group and persists** beside the collapsed flag that
  already exists for collections.
- **A single-tab group does not grow a child row.** Hide-at-zero: one tab
  is the heading's own subject, and a row repeating it is noise.

## Risks

1. **Reordering under the pointer.** The rail already refreshes on every
   mark change; hover state and a moving list fight each other. Hover is
   presentation only and must not trigger a rebuild.
2. **Contrast computed per render is waste.** Resolve once per colour,
   cache by palette name — v1.2.0 measured that re-deriving in the rail
   cost +22% layout time.
3. **A drag that outruns the floor.** See above: same refusal as opening.
4. **C changes the row count**, and the rail's own width thresholds were
   measured against the current shape. Re-measure, do not assume.

## The check this spec owes

**With the rail collapsed, is every one of these still reachable?** The
rail is `F3`-toggleable and defaults hidden on a narrow window. If
switching a group's tab is only possible through the rail, a user who
closed it has lost a capability — which is what `/pane`, `/split` and
`Ctrl+←/→` exist to prevent. Every action this document adds needs a door
that does not depend on the rail being open.
