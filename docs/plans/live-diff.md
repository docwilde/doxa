# Live diff, side by side, with per-hunk reject — specification

Status: **shipped in v0.92.0**, in a split pane rather than in a tab, as
`doxa/diff.py` (the model) and `doxa/ui/diffview.py` (the leaf). The four
open questions at the bottom are answered in the CHANGELOG entry and in
the code; what is NOT built is noted there too. The design check this
document asks for is recorded below.

**The design check, run.** *Session left, diff right, both live* —
v0.91.0's split could express the GEOMETRY of it and could not express
the MODEL of it. Everything about painting, focus and sizing worked
unchanged: `SplitBox` is orientation-agnostic, an unfocused visible leaf
keeps rendering, and `Alt+←/→` already moved the divider (the "sibling
gesture" asked for below needed no new key). What did not work is that a
`layout.Leaf` WAS a session — `split._leaf_of` returned `None` for any
child that was not a `SessionPane`, so a diff leaf would have been
dropped from `PaneTab.tree()`, the split would have collapsed to one
child in the record, and the persisted layout would have said "one pane"
for a screen showing two. One field (`Leaf.view`) and three call sites
(`split._node_of`, `PaneTab.surfaces`, `DoxaApp._pane_regions`) closed
it. So: not wrong, but incomplete in exactly the way a first consumer
finds.

## The idea

While an agent edits files in its worktree, the session sits on the left and a
**live-synced diff** sits on the right — red/green, updating as edits land. Any
hunk can be **rejected**, which tells the session's agent what was rejected and
why.

This is the review loop that DOXA currently makes you leave the app for: today
you read a tool-call fold, or you open the repo elsewhere and diff by hand.

## Prerequisite: split panes

This needs a vertical split, and DOXA has **tabs, not splits** —
`docs/plans/split-panes.md` specifies the layout tree and is unbuilt. So this feature
is downstream of that one, and it is the first concrete consumer of it, which
makes it useful as a design check: if the split spec cannot express
*session left, diff right, both live*, the split spec is wrong.

Two things it needs from that spec specifically:

- **an unfocused visible pane keeps rendering** — the diff updates while you
  type in the session, which that spec already requires ("visible and focused
  are different states, and the transcript must not stall because focus moved")
- **the divider is adjustable** — a diff wants width; `Ctrl+Up/Down` is
  specified for the in-pane status-bar divider, and a left/right split needs
  the sibling gesture

An interim version in a **tab** rather than a split is worth considering: it
delivers the diff and the reject loop without waiting for the layout tree, and
loses only the side-by-side. If the tab version ships first, it must not grow a
second implementation when the split arrives.

## Where the diff comes from

**Not a file watcher.** DOXA has a documented no-timer, no-per-frame rule, and
`docs/plans/code-graph.md` already refused a watcher for the same reason: a second
lifecycle to get wrong. There is a better signal already flowing.

**The tool-result stream is the tick.** `tool_result` events for `Edit`,
`Write`, `NotebookEdit` and a `Bash` call that touched the tree already arrive
in `doxa/session/runtime.py`'s dispatch (`EVENT_RENDERERS`). An edit landing IS
the event; recompute then and only then. The same reasoning that gave v0.56.0's
spinner zero idle cost — *a token arriving is a tick, and it costs nothing when
nothing is arriving*.

**Diffed against the worktree's base.** Every session runs in a worktree on
`doxa/<session-id>` (v0.17.0+), and the sidecar records `base_ref`
(`doxa/worktrees.py`). So the diff is the session's own work against the branch
it started from — which is exactly what a reviewer wants, and is already the
unit `finalize` reasons about.

One measured trap to inherit: **v0.33.0 found that `base_ref == branch` makes
`commits_ahead` structurally unmeasurable**, and that defect force-deleted real
commits. A diff computed against a base equal to the branch shows nothing and
would read as "no changes" rather than "cannot tell". It must say which.

## Rendering

- **Per file, per hunk**, red/green, with the hunk header. `git diff` unified
  output parsed into hunks, not re-implemented — the porcelain is stable and
  the alternative is writing a differ.
- **Collapsed per file** by default with the changed-line counts, the same
  pattern `ToolCallsSection` uses. A twenty-file diff must not be a wall.
- **Side-by-side within the diff pane is a second question** from the split
  itself. At 80 columns a split pane is 40 columns wide; side-by-side inside
  that is 20 columns per side, which is unreadable. **Unified is the default**;
  side-by-side is a mode that earns its place only above a measured width
  threshold, the way `CTX_ABSOLUTE_MIN_COLS` gates the ctx chip.
- **Binary and huge files are named, not rendered.** A 40 MB asset has a
  diff nobody wants and a frame cost nobody budgeted.
- Bounded like every other page: the 64KB frame cap and the shared `_fit_page`
  budget apply if the diff crosses the daemon socket, and a truncated diff must
  say it was truncated.

## Reject — the part that needs care

"Reject this hunk" is two actions and they can disagree:

1. **the file on disk** goes back to what it was
2. **the agent's belief about the file** must be corrected, or its next edit is
   built on a premise that is no longer true

Doing only (1) leaves the agent confidently wrong — it will read the file later
and find its work gone, or worse, not read it and patch against a stale mental
model. Doing only (2) leaves the bad code in the tree until the agent gets
round to it. **Both, in that order, and atomically from the user's point of
view.**

**A rejection while a turn is in flight is the dangerous case.** The agent may
be mid-edit on the same file; reverting under it produces a conflict neither
side understands. Options, and this needs deciding rather than discovering:

- **queue the rejection until `turn_done`**, applying it then and telling the
  agent as part of the next turn — safe, and the user waits
- **apply immediately and interrupt** — fast, and it races
- **refuse while a turn runs**, with the button disabled and a reason —
  honest, and mildly annoying

My reading is queue-until-`turn_done` with the pending rejection visibly marked
in the diff, because a rejection the user has clicked and cannot see the effect
of is the worst of the three.

**The message to the agent is user-authored, so it is trusted input** — unlike
a peer message, which crosses `PEER_UNTRUSTED_INTRO` precisely because another
agent wrote it. It should state what was rejected in terms the agent can act
on: the file, the hunk, and — if the user typed one — a reason. A rejection
with a reason is worth far more than a bare revert, because it stops the agent
re-making the same edit.

**Revert must be exact.** Applying a reverse patch of that hunk, not a
whole-file restore: two hunks in one file must be independently rejectable, and
rejecting one must not discard the other. `git apply --reverse` against the
recorded hunk is the mechanism; if it fails to apply because the file moved
underneath, **say so and change nothing** rather than forcing.

## What this is not

- **Not a merge tool.** No conflict resolution, no three-way anything.
- **Not `git add -p`.** Staging is a git concept; this is about what the agent
  did to the tree, before any commit exists.
- **Not an editor.** Reject or keep. Editing a hunk by hand belongs in an
  editor, and a half-editor is worse than none.
- **Not a second source of truth**: it renders `git diff`, and if it can show
  something git cannot, it is wrong.

## Testing bar

- a hunk rejected while no turn runs reverts exactly that hunk, leaving a
  second hunk in the same file intact — asserted against a real git worktree
- the agent receives a message naming the file and hunk, and it is delivered as
  user-authored rather than through the untrusted-peer path
- a rejection clicked during a turn is visibly pending and applies at
  `turn_done` (or is refused with a reason — whichever is chosen)
- a reverse patch that no longer applies changes nothing and says why
- a diff pane keeps updating while focus is in the session pane
- `base_ref == branch` renders "cannot determine a base", never "no changes"
- a binary file is named, not rendered
- the pane renders with non-zero height at 80 columns — the v0.28.0 defect
  (widgets present in the DOM, drawn nowhere) is the failure mode for any new
  surface here

## Open questions

1. **Does rejecting also stop the agent?** A user rejecting three hunks in a row
   is disagreeing with the direction, not the lines. Offering "reject and
   interrupt" as a distinct action may be the honest reading — but interrupting
   is a bigger act than rejecting and should not be a side effect of it.
2. **Does the diff include untracked files?** An agent creating a new file
   produces no `git diff` hunk without `--intent-to-add`. A created file is
   exactly the kind of thing a reviewer wants to see.
3. **One pane per session, or one per repo?** Two sessions in worktrees off the
   same branch have two different diffs. Per-session is the simpler answer and
   matches the isolation model; a combined view is what the unbuilt merge-queue
   (queue item 6) would want.
4. **Does a rejection survive restore?** v0.32.0 restores tabs and transcripts;
   a queued-but-unapplied rejection is state that would need persisting, or
   explicitly discarding with a message saying so.
