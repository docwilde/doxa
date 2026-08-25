# Recursive split panes — specification

Status: **draft for review**. Nothing implemented. Written before the work so
the layout format, the focus model and the restore path are agreed rather than
discovered mid-build.

## What exists today

DOXA has **tabs, not splits**. `SessionPane` is a `TabPane`; a window holds one
`TabbedContent` and exactly one pane is visible at a time. Everything that
sounds like a split today is not one:

| looks like a split | actually is |
|---|---|
| `SessionPane` | a tab page |
| `SubagentTranscriptTab`, `ArchivedSessionTab` | further tab pages |
| the status bar's two lines | one widget stack inside a pane |

v0.32.0 already put a forward-compatible node in the persisted tabset record:

```json
{"layout": {"kind": "tabs", ...}, "tabs": [...]}
```

The flat `tabs` list stays authoritative, and an unrecognised `kind` reads as
"nothing to restore" rather than a split flattened into tabs. That node is the
seam this feature lands in — the format was designed for it and does not need a
breaking change.

## The shape

A **layout tree** per window. Two node kinds:

- **leaf** — holds exactly one pane (a session, a subagent transcript, an
  archived transcript). Leaves are what tabs hold today.
- **split** — an orientation (`row` | `column`), an ordered list of children
  (each a leaf or a split), and per-child size weights.

Tabs do not disappear and are not replaced. **Each tab owns a layout tree.**
A tab whose tree is a single leaf is exactly today's tab, which is what keeps
the migration honest: existing records restore to single-leaf trees and behave
identically.

Recursion is genuine — a split may contain a split — but see "Depth" below.

## Keys and commands

Following the house convention that every action has a command and, where it
earns one, a binding:

- `/split` and `/vsplit`, registered in `doxa/commands.py` (so they reach
  `/help`, the palette and autocomplete like everything else)
- Bindings in the `ctrl+shift` range, chosen not to collide with the existing
  set (`ctrl+t` new tab, `ctrl+w` close, `ctrl+q` quit, `ctrl+p` palette,
  `ctrl+,` settings, `ctrl+left/right` cycle tabs)
- Focus movement between panes must be directional (left/right/up/down), not
  "next pane" — in a 2×2 grid "next" has no meaning a user can predict
- Closing the last pane in a split collapses the split; closing the last pane
  in a tab closes the tab, matching today's `_close_pane` semantics

## Focus

This is the part most likely to go wrong, and there is already a scar.

Focus and activation *were* **entangled**: `SessionPane.on_mount` focused its
prompt, and focusing a widget inside a `TabPane` activates that pane. The
activation was a side effect of the focus, not the cause. That single
mechanism produced the v0.32.0 restored-active-tab defect and the standing
flake in `tests/test_tab_status.py`.

**Splits must not be built on top of that.** With two panes visible at once,
"which pane is active" stops being derivable from "which tab is showing", and
an implicit mount-time focus becomes an unpredictable race between siblings.

**That prerequisite landed in v0.38.0.** Focus follows explicit user intent at
each handler — Ctrl+T, tab cycling, jump-by-id, `open_tab_at`, startup and
restore — through the one `DoxaApp._focus_tab`; `_on_tab_activated` is
retained only for mouse clicks, which have no key event; the mount-time focus
is gone, and a pane mounted without setting `active` now stays in the
background. `tests/test_focus_ownership.py` is the standing guard. Splits can
start from here, and inherit the rule rather than the race: a new leaf mounts
unfocused, and whatever creates it says where the keyboard goes.

Consequences to settle when it does:

- exactly one pane per window holds keyboard focus; the status bar reflects
  **that** pane
- an unfocused visible pane still renders live output — visible and focused are
  different states, and the transcript must not stall because focus moved
- the "you missed something" affordances (`-done-unseen`, the needs-input
  blink, the `-staged` tint) are cleared on *activation* today. With splits, a
  pane can be visible without being focused: decide whether visible-but-
  unfocused counts as seen. It should not — the marker means "you have not
  looked at this", and a pane in the corner of a 2×2 grid may genuinely be
  unread.

## Persistence and restore

The layout tree serialises into the existing `layout` node. Requirements:

- **Round-trip both ways.** A v0.32.0 reader must not choke on a tree it does
  not understand (it already reads unknown `kind` as nothing-to-restore), and a
  new reader must restore old flat records as single-leaf trees.
- **Sizes are proportional, never absolute.** A restore into a differently
  sized terminal must preserve ratios, not columns.
- Restore reuses v0.32.0's transcript machinery per leaf (`doxa/transcript.py`,
  `mount_transcript`). Splits change *where* a transcript mounts, not how it is
  read.
- The saved-focused leaf is restored focused — the same requirement the saved
  active tab has, and which was silently broken from v0.23.0 until v0.32.0
  because focus was implicit. There must be a test with **three or more leaves**;
  the old two-tab test passed only because the saved tab happened to be last.

## Minimum sizes and degradation

A pane has a floor below which it is not a pane. The status bar already carries
this problem (`TAB_MODEL_MIN`, `TAB_REPO_MIN` exist because chips contend for
width), and a split multiplies it.

- Define a minimum width and height per leaf. A split that would violate it is
  **refused with a message**, not performed into an unusable sliver.
- On terminal resize below the total minimum, degrade predictably and
  reversibly — the tree is not rewritten, only its rendering is constrained, so
  enlarging the terminal restores the layout.

## Depth

Recursion is in the model. Whether it is in the *user interface* is a separate
call: arbitrary nesting is easy to build and hard to navigate, and directional
focus movement in a deep tree stops being predictable.

Recommendation: implement the tree recursively, **cap interactive depth** (two
or three levels), and refuse deeper splits with a message. The cap is a
constant, not an architectural limit, so raising it later costs nothing.

## Out of scope

- **Floating windows (item W).** Overlapping, freely positioned windows are a
  different model from a tiling tree — the same word "window" covering two
  designs is how a layout system rots. W stays specified separately, and
  whichever ships first should not silently constrain the other.
- **Detaching a pane to its own terminal.** Sessions already survive detach via
  the daemon; that is a session-lifecycle feature, not a layout one.
- **Per-pane themes.** One theme per app.

## Testing bar

The v0.28.0 lesson applies with full force: assertions must be about what the
user sees.

- a split actually renders **two panes with non-zero width and height** — the
  invisible-button defect passed every structural assertion for a whole release
- directional focus movement lands on the geometrically correct pane in a 2×2
- output continues rendering into a visible, unfocused pane
- a layout with three or more leaves restores with the right leaf focused
- an old flat record restores as a single leaf, and a new record is readable by
  the old reader without error
- a split refused for minimum size says so and changes nothing

## Open questions

1. **Do splits belong to a tab or replace tabs?** This spec says tabs own
   layout trees. The alternative — one tree per window, tabs removed — is
   cleaner in the abstract and a much larger, more disruptive change.
2. **Does a split share one session or hold two?** Two independent sessions
   side by side is the obvious reading. A second *view* onto one session
   (transcript above, prompt below) is a different feature wearing the same
   gesture.
3. **Mouse resize of the divider?** Textual can do it; it adds a drag surface
   and a persistence question (weights change without a keystroke).
