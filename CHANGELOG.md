# Changelog

Newest first. Versions are annotated git tags on the commit that shipped
them (`v0.1.0` … `v0.15.0`); the ranges below are derived from that history,
not written from memory.

## 0.99.0 — 2026-09-02

**"When a request is running in one tab and i open another, and then i
switch back, the old request seem to have been interrupted and i dont see
its result."** Reported from live use. The turn was never interrupted, and
nothing it produced was ever lost — the answer was in the widget, in the
daemon and in the transcript on disk. What was lost was the SCROLL, which
made a finished turn indistinguishable from a killed one.

- **The mechanism.** Every site that appends to a pane's transcript ended
  in `block_list.scroll_end(animate=False)` — `_run_turn` once per engine
  event, `_peer_pump`, `_system`, the boot blocks, `/context`, `/img`,
  the shell block, the restored scrollback. That call does nothing at all
  in a background tab. Measured, not reasoned: while another tab is
  active a hidden `TabPane` gives its whole subtree no geometry, so the
  pane's own `size` and `#block-list`'s are both `Size(0, 0)`;
  `max_scroll_y` is `virtual_size.height - container_size.height` floored
  at zero, and those two go stale together at their last visible values,
  so it reads 0 for that entire window. Every scroll the streaming turn
  issued therefore went to row 0 and reported success. When the tab came
  back the layout recomputed, `max_scroll_y` jumped to the full height of
  the answer that had landed meanwhile — `0.0 of 78` in the pilot that
  reproduces it — and nothing re-issued the scroll. The transcript sat at
  the offset it held when the user walked away, with the whole reply below
  the fold: an answer one PageDown away, and nothing on screen saying so.
- **`SessionPane.scroll_transcript_to_end`** (`doxa/session/pane.py`) is
  now the one door for that intent, and the one thing it can do that a
  bare `scroll_end` cannot is REMEMBER. A pane with no box on screen sets
  `_tail_pending` instead of pretending; `on_show` (and `on_resize`, for a
  pane that gets its box back without a Show) spends it. Every one of the
  fourteen call sites above goes through it.
- **Two things this had to get right, both measured before they were
  written.** The readiness test is `block_list.size`, never
  `container_size`: only a layout pass rewrites `container_size` and
  `virtual_size`, so a hidden widget keeps reporting the box it had when
  it was last visible (94x21 for a pane that is 0x0) — a guard on those
  never fires and the flag is never set. And the flush does not have to
  hunt for the moment the content has been re-measured: on the refresh
  where the pane gets its box back `virtual_size` is still the stale
  pre-hide value, and it is one refresh later that `max_scroll_y` becomes
  real — which is exactly the gap `scroll_end` already closes on its own,
  by deferring its `max_scroll_y` read through `call_after_refresh`.
- **Not `Widget.anchor()`**, which looks like the platform answer and is
  not. Textual 5.3's compositor writes the anchored offset with
  `set_reactive`, bypassing `validate_scroll_y`, so a transcript SHORTER
  than its container gets a large negative `scroll_y` — measured at `-20`
  on a 100x45 pane, with the boot banner shoved off the top under a
  screenful of blank rows. `test_a_short_transcript_is_not_pushed_down_
  when_a_tab_is_shown` pins that against a later re-attempt.
- **Not a cancellation, and that is asserted rather than assumed.**
  Textual's exclusivity groups are node-scoped —
  `WorkerManager.cancel_group` filters on `worker.node == node` (textual
  5.3) and `on_prompt_submitted` runs its worker on the PANE — so a second
  pane starting anything in group `"turn"` cannot touch the first pane's.
  `SessionPane`'s docstring has claimed this since v0.34.0; it is now
  pinned by a test that watches the engine's own generator for an outside
  close, so if it ever stops being true the report comes back with a
  different cause instead of the same symptom.
- **The same answer was lost two ways the report did not mention**, both
  through the identical `scroll_end`: a turn another attached client of
  the same daemon drives (`_peer_pump`), which needs no keystroke at all,
  and a `turn failed:` block — a background pane's own error message
  landed below the fold too.
- 4 new tests in `tests/test_turn_survives_new_tab.py`, driving the report
  keystroke for keystroke through a `HalfwayEngine` whose `send()` stops
  mid-turn on an event the test holds (every other fake in this suite
  replays its whole script in one loop turn, which is the one shape that
  cannot reproduce this). They assert against the COMPOSITED screen, never
  `TurnBlock.assistant_text`, because the widget's own model says "the
  answer is here" in the broken world too. 2 fail against 0.96.0 and
  against unmerged `feat/pane-groups` alike; the fix applies cleanly to
  that branch and passes its own `tests/test_pane_groups.py`.

## 0.98.0 — 2026-09-01

**The streaming deriver is on by default, every 900 seconds.** Through
v0.97.0 `derive_secs` was opt-in and unset meant off, so review fired only
at `PreCompact` and at finalize — and a session that ran for hours and
ended without a clean finalize derived **nothing at all**.

- New **`doxa.engine.DERIVE_SECS_DEFAULT`** (900.0). `derive_interval()`
  now reads unset, empty AND unparseable as "take the default": silently
  disabling a feature the operator believes is on is the worse of the two
  failures.
- **Off is sayable**: `0`, `off`, `no`, `false` (any case) disable it and
  leave review where it was — `PreCompact` and session end, which always
  run regardless and honour `LORE_DISABLE_REVIEW`.
- Unchanged, and what makes the default affordable: `_maybe_schedule_derive`
  fires on turn-done, refuses while one review is in flight or the session
  is finalizing, and never blocks the turn path. A quiet session pays
  nothing; a busy one pays at most four reviews an hour. Each shells out to
  a headless `claude -p`, stated plainly in the manual because it is a real
  cost.
- `tests/test_derive.py::test_derive_off_by_default` renamed to
  `test_derive_can_be_turned_off_explicitly` and made to say `off`
  outright: after the flip it still PASSED, because `_last_derive` is
  stamped at construction so the first turn of any session sits inside the
  debounce either way. A test that cannot fail when the thing it names
  breaks is worse than no test. Its new sibling
  `test_derive_is_on_by_default_once_the_interval_has_passed` asserts the
  on path from the other side by ageing `_last_derive`.

## 0.97.0 — 2026-08-31

**An inversion of a hierarchy that shipped three releases ago.** Reported
after using v0.91.0's splits: *"the new sessions have no tab menu of
their own… if i switch tabs, the split out sessions go with the tab.
Shouldn't the split out sessions be independent?"* — then the shape:
*"or…each pane has its own tab header bar?"*

```
v0.91.0   window → tabs → each tab owns a layout tree of panes
v0.96.0   window → one layout tree of GROUPS → each group owns its tabs
```

The design is [docs/plans/pane-groups.md](docs/plans/pane-groups.md),
written before the work and now marked implemented.

### The model

- **`doxa/layout.py` gains `Group(tabs, active)`** and it is what a leaf
  of the window's tree now is. Its tab records are `Leaf` values,
  unchanged, field for field — which is not a convenience but the whole
  migration story: a group's tab and a flat `tabs` row carry the same
  five facts, so one reader works against either shape.
- `active` is an INDEX, not an id, clamped at construction. `prune` drops
  a group's dead tabs and keeps the region — three tabs of which one
  session survived is still a group, showing the survivor — and
  re-derives `active` from the tab that HELD it, so a survivor keeps the
  keyboard even when the deletion moved it two places left.
- New: `groups()` (regions, in reading order), `as_group()`,
  `groupify()`. `leaves()` still returns `Leaf` and deliberately so:
  every caller of it asks "which sessions are in this layout", which is a
  question about tab records, never about regions.
- **`doxa/ui/split.py` gains `PaneGroup`**, a container holding one
  `TabbedContent`. `PaneTab` goes back to holding exactly ONE surface —
  what it held through v0.88.0 — because the tree moved up to the window.
  `SplitBox`, the weights, `neighbour`, `rebuild_slots`, the owner-first
  invariant and `SPLIT_SLOTS` are untouched: this is a re-rooting, not a
  rebuild, which was the argument for doing it as one.
- `#session-tabs` became a CLASS in `doxa/theme.tcss` (11 selectors). An
  id cannot be in two places and a window now has N strips. The FIRST
  group's strip still carries the literal id, so an unsplit window's DOM
  is byte for byte the one every release before this produced.
  `DoxaApp._strip()` / `_strip_for(tab_id)` / `tabbed_holding(tab_id)`
  replaced 22 `query_one("#session-tabs")` calls in `doxa/app.py` and
  four more across `doxa/ui/labels.py`, `doxa/session/pane.py`,
  `doxa/session/runtime.py` and `doxa/ui/transcript.py`. The last three
  matter beyond mechanics: a subagent transcript now opens in the strip
  of the group that spawned it, and a background group's tab status
  lands on that group's header rather than the foreground one's.

### Independence, which is the reported defect

- **`Ctrl+←/→` cycles the FOCUSED GROUP's tabs.** `_cyclable_tabs()`
  returns one group's strip instead of every `TabPane` in the window.
  Three sessions cycling on the left while a fourth stays pinned on the
  right is the thing the old model could not express, and it is the whole
  of why the hierarchy was worth inverting.
- **Closing a tab closes ONE session.** Through v0.95.0 closing a tab
  that held a three-way split ended three, and the only available
  mitigation was a confirm dialog. `_close_pane` now hands off to
  `_close_group_tab`, which collapses in two ordered steps that are two
  different facts: a group with other tabs keeps its region and shows
  one; a group with none goes, and the split above it collapses by the
  rule v0.91.0 already wrote for a leaf. `_closest_group_heir` names
  where the keyboard goes, from the painted rectangles.
- `/breakout` and `/join` were never built and are not needed: moving a
  tab between groups is the general gesture they would have been two
  special cases of.

### Moving a tab between groups

**The single hardest constraint in the spec, and it is a framework one.**
Textual 5.3 cannot re-parent a mounted widget — `mount` of a mounted
widget is a silent no-op that ORPHANS it, measured in v0.91.0 — so a tab
that moves cannot take its widget with it.

- `/movepane <n>` re-creates the tab at the destination and tears the
  original down. `SessionPane.release_engine()` / `adopt_engine()` are
  the seam, and they are deliberately not `switch_engine`, which
  FINALIZES the outgoing engine — exactly what must not happen here.
- `_boot` skips `engine.start()` for an adopted handle. That one line is
  the difference between "the session moved" and "a second CLI is now
  writing this transcript": an `EngineClient` would open a second socket
  connection, an in-process `SessionEngine` would spawn a second CLI on a
  live conversation — the two-writers failure `resume_session` already
  refuses by name, arriving through the back door.
- The scrollback comes back from disk, through v0.32.0's own
  `_restore_transcript`. The widget is new, so the blocks it had painted
  went with it, and re-reading is the only honest way to put them back.
- Refused, changing nothing, when it is the group's last tab: that is a
  close and a move at once, and the two have different undo stories.
- **No key.** `Ctrl+Shift+←/→` is directional focus; the spec defers the
  spelling and the command is the form that will still work on the
  terminals where whatever key gets chosen cannot be sent.

`tests/test_pane_groups.py::test_moving_a_tab_between_groups_keeps_the_
SAME_SESSION_running` is the evidence: same engine OBJECT, same session
id, `finalized is False`, no second engine built, the source widget out
of `panes()` with `parent is None`, and the new one painted inside the
destination group's rectangle.

### Jumping to a group

- **`Ctrl+1` … `Ctrl+9`**, numbered in reading order — left to right,
  then top to bottom — derived from the painted rectangles
  (`_group_order`, over `_pane_regions`' own source), never from tree
  order: what the user counts is what is on screen. In a 2×2 that is
  upper-left, upper-right, lower-left, lower-right.
- **These nine keys cannot be sent under the legacy encoding.** `Ctrl`
  has a C0 code only for the 26 letters and ``@ [ \ ] ^ _ ?`` and space;
  a digit produces no byte at all. They ship anyway, registered so
  `doxa.keyboard.unreachable_under_legacy` answers True and `/help` and
  `/doctor` say where they do not work — the same bargain `Ctrl+,`
  (v0.39.0) and `Ctrl+Tab` (v0.42.0) already ship on.
  `tests/test_keyboard.py`'s `unreachable_bindings()` list goes from two
  entries to eleven. **`/pane <n>` is the door that always works**, and
  it declares no `binding` of its own: nine keys each name one group, so
  none of them is "the key for `/pane`", and putting one there would make
  `/doctor` read as though the command itself were unsendable.
  `Alt+<digit>` was rejected — it is terminal tab-switching in GNOME
  Terminal and others — and a tmux-style prefix chord costs two
  keystrokes for a gesture meant to be instant.
- **The number overlay.** Any `Ctrl+<digit>` paints each group's own
  number over its own region, from the same rectangles the numbering
  comes from, so what is numbered and what is painted cannot disagree.
  The jump still happens immediately: it is feedback and teaching, not a
  mode, and DOXA does not wait for a second keystroke the way tmux's
  `display-panes` does, because the numbering is meant to become muscle
  memory and a prompt-then-wait gesture never lets it.
  - It fires when the digit names NO group — `Ctrl+7` in a two-group
    window shows `1` and `2` and moves nothing. That is where it earns
    the most: it answers "what are my choices" for a user who guessed.
  - **One `set_timer`, never `set_interval`**, 1.2 s, cancelled before a
    new one is armed and by any subsequent key. DOXA's no-timer rule
    targets idle CPU, and v0.78.0 already amended it on exactly this
    reasoning for the turn spinner.
  - Nothing at all in a one-group window. Hide-at-zero, as everywhere.
  - The dismissal hangs off `DoxaApp.on_event`, not an `@on(events.Key)`
    handler: the focused prompt is a `TextArea` and stops the `Key`
    message dead, so the decorated form never fires for an ordinary
    letter. Written that way first, measured, moved.

### Persistence — the third format, and the last one

`layout.kind` stays `"tabs"` and the flat `tabs` list stays authoritative
and complete. The window's one tree rides in a new `layout.groups` node,
on the principle the slot was reserved with in v0.32.0. **Absence of the
key is the migration** — no version field, no upgrade step, for the third
format running.

| written by | carries | reads as |
|---|---|---|
| v0.23.0 – v0.90.0 | flat `tabs` only | one group holding all of them, showing the saved active tab |
| v0.91.0 – v0.95.0 | `trees`, one per tab | the ACTIVE tab's tree, one single-tab group per leaf; every other saved tab becomes a tab of the group holding the active session |
| v0.96.0 | `groups` | itself |

- **The spec left the composition rule open** — it says how a LEAF reads
  in each era, not how N per-tab trees become the window's ONE tree — and
  this is where that gap is filled, in `doxa/tabsets.py`'s own docstring.
  The rule is "what was on the user's screen stays on the user's screen".
  The literal alternative, a group per saved tab, was rejected after
  measuring it: five saved tabs would restore as a five-way split, 16
  columns each on an 80-column terminal, below `MIN_LEAF_WIDTH` (34) and
  therefore below the width at which DOXA's own `split_refusal` will make
  a split at all. **A restore that produces an arrangement the app
  refuses to produce interactively is not a migration.**
- `_fill_group` guarantees the invariant the two halves of the record
  depend on: every session in the flat list is in exactly one group, and
  a hand-edited tree that named one twice has the duplicate dropped.
  `_point_at` gives the saved active session's group to it while leaving
  every other group on its own saved tab — a restore that reset four
  regions to their first tab in order to record one would lose four facts
  to keep one.
- **Writing back is the same promise the other way.** A v0.96.0 record
  still carries `trees`, DERIVED from the group tree (`_trees_from_groups`
  — each region's leaf is that group's active tab) rather than tracked
  separately, so the two halves cannot drift. A v0.91.0–v0.95.0 DOXA
  reading a grouped record gets the geometry it can express and picks the
  remaining tabs up from the flat list.
- `ArchivedSessionTab` grew a `layout_leaf()`. Without it an archive
  would be in the flat list and in no group, and `_fill_group` would put
  it back at the end of the FIRST group's strip — so an archive parked in
  the right-hand group would migrate leftwards every restart.
- Tested with **four** tabs for era 1 (active second), **three** trees for
  era 2 (active in the second), and **six** sessions across three groups
  for era 3 — v0.91.0's own spec notes the old two-tab test passed only
  because the saved tab happened to be last.

### Focus, and what "seen" means one level up

- Exactly one group holds the keyboard (`focused_group()`, derived from
  `self.focused`, never from a flag this app maintains — the drift that
  produced the v0.32.0 restored-active-tab defect).
- **An inactive tab inside a VISIBLE group is neither visible nor
  focused.** It keeps running, and `-done-unseen`, the needs-input blink
  and the `-staged` tint do NOT clear for it. v0.91.0 settled that
  visible-but-unfocused is not seen; an invisible tab is the stronger
  case of the same thing, and it needed no new code — `_clear_seen_marks`
  already fires only for the pane that gets the keyboard.
- **Only a group's SECOND `TabActivated` onwards moves focus.** Every
  group posts one as it mounts (Textual's `Tabs` defaults itself to its
  first tab), so with N groups the handler fired N times during boot and
  the last one to land won the keyboard, whatever
  `_activate_initial_tab` had just said. Measured as a restore with the
  saved active session in the middle landing on the last group instead —
  the v0.23.0 "three restored tabs always land on the last one" defect,
  re-created one level up. Skipping only the first per group is what
  keeps the MOUSE path working, which is the only reason that handler
  focuses at all.
- Three call sites that walked the wrong tree and went silently dead,
  each found by a test rather than by reading:
  - `grow_pane_towards` (`Alt+arrow`) climbed from the SURFACE and found
    no `SplitBox` — the dividing boxes sit above the GROUP now.
  - `focus_pane_towards` (`Ctrl+Shift+arrow`) searched the active tab's
    surfaces instead of every group's.
  - `DiffPane.session_pane()` searched from the enclosing TAB, which used
    to contain both the diff and its session and no longer does. It walks
    to the top of its own parent chain now — still a DOM walk from
    `self`, so the `Widget.app` context-variable hazard its docstring
    warns about is untouched; only the ROOT of the walk moved.
- `active_pane` stopped cross-checking `focused_pane()` against the
  active tab for a pane in ANOTHER group (that check discarded the right
  answer once a diff could be a tab of its own group), and kept it INSIDE
  the focused group, where it has to stay: a read-only tab showing means
  there is no session pane here, and every caller reads that `None` as
  "ask `_close_read_only_tab` instead". Both halves are pinned by tests
  that failed at the intermediate state.
- **One change tried and reverted, recorded because the reasoning is the
  useful part.** `Widget.focus()` is deferred in Textual 5.3, so for one
  message-pump turn after a split `focused_group()` still answers with
  the group the user came FROM — and believing `_focus_tab`'s
  synchronously-recorded intent instead, the way `PaneTab.focused_leaf`
  is believed one level down, looked like the same fix. Measured, it
  costs more than it buys: an unpainted group has a zero-area rectangle,
  so a second `split_active_pane` in the same turn refuses with *"not
  enough height to split: each pane needs 9 rows and this one has 0"*,
  and `active_pane` answers with a pane from a group the keyboard has
  demonstrably not reached. The window it would have fixed is one
  transient record write that corrects itself the moment the new pane
  boots. `focused_group()` reads the DOM and keeps the remembered id only
  for the case focus is not in a group at all; the docstring carries the
  measurement so the next reader does not re-derive it.

### Two tab strips is more chrome

`PaneGroup` puts itself on one of three width rungs, measured rather than
chosen and restated in
`test_the_tab_strip_thresholds_are_the_measured_ones` so it fails if
either input moves:

- **below 17 columns, no strip at all** (`GROUP_STRIP_MIN_COLS`);
- **below 34, compact labels** (`GROUP_STRIP_COMPACT_COLS`);
- above that, unchanged.

A tab header costs its label floor — `TAB_MODEL_MIN (4) + " · " (3) +
TAB_REPO_MIN (6)` — plus the provider glyph and its space (2) and
Textual's own `Tab` padding of one column each side: 17 for one header,
34 for the two a strip is actually for. `MIN_LEAF_WIDTH` is 34 too, and
that is not a coincidence: its own comment already derives it from the
same two label floors, so the narrowest group DOXA will create sits
exactly on the compact boundary. `display: none` on the strip rather than
`height: 0`, so the row it gives back goes to the transcript — a
zero-height widget still holding its gutter is the invisible-button
defect in its layout form.

### The check this spec owed itself

**Can a group hold a diff leaf? Yes, with no special case.** A group's
tab list is a list of SURFACES and v0.92.0's diff is a surface;
`PaneGroup.layout_group()` asks each tab for its own `layout_leaf()` and
`Leaf.view` carries which kind it is. Nothing in `PaneGroup`,
`layout.Group`, `prune`, the persistence reader or the tab-move path
mentions diffs. The one place the word appears is the branch in `compose`
that has to know which WIDGET to build — the same single branch v0.91.0's
`_leaf` had, not a new one. `/diff` opens the diff as a tab of a new
group beside the session's, and `tests/test_live_diff.py` now reads its
position out of the window tree through the generic `layout.groups()`
walk.

### Coverage, and what is not covered

- **26 new tests** in `tests/test_pane_groups.py`. They were written
  AFTER the model, not before it, and the honest measure of them is what
  they caught rather than a claim about their order: **eight failed on
  their first run**, and four of those eight were real defects with fixes
  in this release — the era-1/era-2 record discrimination (a pre-v0.91.0
  record silently reordered the user's tab bar), the overlay's key
  dismissal (an `@on(events.Key)` handler that never fires), the group's
  own `TabActivated` stealing focus at boot, and
  `split.first_group` reading `children` where Textual 5.3 keeps
  constructor children in `_pending_children`. The other four were the
  tests themselves being wrong about the app.
- Three more real defects were caught by the EXISTING suite, not by the
  new file, and are the ones listed under **Focus** above:
  `grow_pane_towards`, `focus_pane_towards` and
  `DiffPane.session_pane()`. All three passed every structural assertion
  and were silently dead keys, which is the failure mode this codebase
  keeps naming. A fourth — the focus-intent change described under
  **Focus** — was caught only by the FULL suite, by
  `test_exactly_one_pane_per_window_holds_the_keyboard`, which is the
  argument for running the whole thing rather than the files you think
  you touched.
- Suite: **1643 tests, all passing** (1617 before this branch).
- Four existing tests changed their expectations because the model
  changed, and each says so in place: `test_a_vsplit_paints_two_panes_
  side_by_side` (a split makes a GROUP), `test_a_saved_split_restores_as_
  a_split_with_the_right_leaf_focused` and
  `test_a_restored_split_leaf_can_still_be_split` (a v0.91.0 tree
  migrates as one group per leaf), and two width assertions that read a
  tab's rectangle where they now have to read the window's.
- `scripts/screenshot.py` and `scripts/record_gif.py` re-checked, because
  this refactor moved what a tab is again and v0.94.0 found them broken
  for three releases over exactly that. Six `#session-tabs` lookups
  became `tabbed_holding(tab_id)`; screenshot.py's four gained a shared
  `_activate()` helper carrying both hard-won facts (`tab_id` not `id`;
  the strip is a question about which group).
- **Not covered**, and out of scope by the spec: floating windows;
  detaching a pane to its own terminal; dragging a tab with the mouse;
  per-group status bars. Also not done: **no key for `/movepane`**, and
  **moving a DIFF tab between groups is refused** — `move_tab_to_group`
  requires a session tab, because a diff belongs beside the session it is
  a diff of and the general case has no test behind it yet. The assets
  gallery was **not regenerated** for this release, so every still and
  GIF still shows `DOXA 0.94.0` and the v0.91.0 one-tab-two-panes layout.

## 0.96.0 — 2026-08-31

**The only way anyone found out a bound key was dead was pressing it and
getting nothing.** `doxa.keyboard` (v0.39.0) has known which bindings a
legacy-encoding terminal cannot deliver since the module existed, and
`/help`/`/doctor` have said so since v0.42.0 — but only to someone who
went looking. Reported by the owner from live use, twice in one evening
(`Alt+S`/`Alt+D`, then a `ctrl+shift` chord), both times found the only
way there is one: silence.

- **`doxa.ui.labels.unreachable_notice`** (new, `doxa/ui/labels.py`) is
  the one-line startup notice: on the current legacy-encoding binding set
  it reads *"this terminal can't deliver 2 bound keys: Ctrl+, (use
  /settings), Ctrl+Tab (use /mode) -- see /doctor for details"*. Past
  `NOTICE_SUMMARY_THRESHOLD` (3) affected bindings it stops naming them
  and names the count instead, pointing at `/doctor` for the full table —
  one line, not a lecture. Empty in exactly the two cases
  `unreachable_bindings()` already was: a kitty-protocol terminal
  (nothing lost), and one `doxa.keyboard.detect_protocol()` never
  measured (`UNKNOWN`) — the second is a deliberate call, not an
  oversight, pinned by
  `test_unreachable_notice_is_silent_when_the_protocol_was_never_measured`:
  this notice's whole claim is "these specific keys are dead", UNKNOWN
  means there is no evidence for that claim, and firing it anyway on
  every boot of a slow SSH hop or a multiplexer that ate the probe's
  reply would be the same false alarm `doxa/keyboard.py`'s docstring
  exists to prevent, just moved to the loud side.
- **The door.** `unreachable_bindings()` named the dead keys but not what
  to press instead, so `doxa.ui.labels.unreachable_doors`/`_door_for`
  (new) resolve each one against the SAME registry `/help` reads: a
  direct `SlashCommand.binding` match (`ctrl+comma` → `/settings`), or,
  when a key has none of its own, another `DoxaApp.BINDINGS` row that
  fires the identical Textual action and IS a command's binding
  (`ctrl+tab` shares `cycle_permission_mode` with `shift+tab`, which is
  `/mode`'s binding → `/mode`). `unreachable_bindings()` itself is now a
  thin wrapper over `unreachable_doors()`; its return value, and
  `/doctor`'s and `/help`'s output, are unchanged.
- **`key_notice`** (`doxa/config.py`, env `DOXA_KEY_NOTICE`), a
  `bool_on` row beside `boot_banner` in the Appearance tab, default ON.
  `doxa.keyboard.notice_enabled()` reads it the same way
  `history.resume_restored()` reads its own knob — off returns to plain
  silence; `/help` and `/doctor` report the same keys either way.
- **Wired into boot, not compose.** `PaneRuntimeMixin._boot`
  (`doxa/session/runtime.py`) mounts a `SystemBlock#key-notice-block`
  right after the identity block, gated on `keyboard_mod.notice_enabled()`
  alone — no separate tty check, because `unreachable_notice()` is
  already empty on a headless run (`_is_tty()` False → `UNKNOWN`) and on
  kitty, so the one gate covers both. Rides every session start on an
  affected terminal, same as the identity block it sits under.
- 16 new tests in `tests/test_keyboard.py` (function-level doors/notice/
  setting behavior, plus four real-pilot boot tests polling PAINTED
  `SystemBlock` regions, not mount): 13 fail against pre-change code, the
  other 3 are pure-absence assertions (kitty/unknown/setting-off) that
  are vacuously true before the feature exists and are pinned so a later
  change has to break them on purpose.

## 0.95.0 — 2026-08-31

Two defects from live use, in one tab: a pair of hotkeys that did
nothing, and a split that stopped the application for two and a half
seconds. The second one is the one that mattered.

**"After splitting the pane with vsplit, the whole TUI lags hard (maybe
CPU load loop wo wait or not async?)"** — two guesses in one sentence,
and the second one was right.

`SessionPane.on_mount` opened with a plain synchronous
`self.engine = self._engine_factory()`. In this suite that factory
returns a `FakeEngine` instantly. In production it is
`doxa.cli.new_session_factory` → `daemon.spawn_daemon`, which
`subprocess.Popen`s a fresh daemon and then blocks its caller in a
`while monotonic() < deadline: _time.sleep(0.1)` registry poll for up to
`wait_secs` — **60 seconds** as written. On the event loop thread. No
keys, no repaint, no message pump, no streaming in the pane you were
already looking at.

Measured with a 10 ms heartbeat task watching the loop across
`split_active_pane(ROW)`:

| session factory | idle before | **during `/vsplit`** | idle after |
|---|---|---|---|
| instant (what the suite uses) | 11.5 ms | **22.6 ms** | 11.5 ms |
| blocks 0.5 s | 11.3 ms | **513.8 ms** | 11.8 ms |
| blocks 2.0 s | 13.2 ms | **2028.2 ms** | 11.6 ms |
| the real `spawn_daemon` | 12.0 ms | **2320.8 ms** | 12.7 ms |

One unbroken ~2.3-second freeze, ~190× the idle gap, with four heartbeat
ticks landing in the whole window. A real `spawn_daemon()` blocks its
caller 2307 ms, of which 596 ms is `import doxa.daemon` alone.

- **There is no busy loop, and that is a measurement, not a shrug.** Idle
  cost stayed at **0.8–2.0 % of one core before AND after a split** over
  eleven scenarios — no split, vsplit, `/vsplit` as a command, hsplit,
  vsplit ×2 and ×3, vsplit then close, diff, vsplit+diff, vsplit+divider,
  vsplit+focus — and never diverged; confirmed again against the real
  Textual driver on a PTY (1.40 % → 1.80 %, with stdout bytes per 5 s
  actually *falling*, 2610 → 2030). v0.91.0's unbounded
  `call_after_refresh` focus retry **stayed fixed** (`retry=False` at
  `app.py:931` and `:1326`); v0.92.0's live-diff tick is not on the split
  path; `split.py`'s divider and `_pane_regions` do not re-trigger
  layout, and `Widget._arrange` ran 836 times *during* a split with six
  turn blocks and **0** times in every idle window after it. The app was
  never busy after a split. It was frozen during it.
- **The fix was already in the file, one method down.** `switch_engine`
  has built its engine with `await asyncio.to_thread(make_engine)` since
  it was written, and its docstring gives this exact reason — "off-loop —
  a daemon spawn blocks on subprocess+registry polling". `/model` and
  `/attach` were paying attention to it; the path **every** pane takes
  was not. New `PaneRuntimeMixin._build_and_boot` does the same, so
  `Ctrl+T` and every restored tab are fixed with the split.
- The window that opens — `pane.engine` is None between mount and the
  thread returning — is one the code already models (`_peer_pump`
  documents it; every chip reader guards it). `_run_turn` did not: it
  asserted `self.engine is not None` **before** awaiting `_engine_ready`,
  which was only ever true because `on_mount` was synchronous. Those two
  lines swapped. A pane closed mid-spawn now finalizes the engine the
  thread was still building, because `detach`/`stop` cannot clear a
  handle that does not exist yet.
- `spawn_daemon`'s 60-second `wait_secs` is left alone on purpose: off
  the loop it costs a pane that says `connecting…`, not an application
  that has stopped.
- **Two unbounded retry loops bounded on the way past**, found while
  ruling out the busy-loop theory. `DiffPane._repaint` →
  `call_after_refresh(_repaint_later)` → `run_worker(_repaint())`
  reschedules itself with no counter and no delay, and
  `FileSection.build` → `_build_later` → `build` is the same shape;
  either would spin the pump forever if its container never became
  mountable. Their own sibling `_apply_badges` has carried a `passes`
  counter from the day it was written, so the omission reads as an
  oversight. Neither could be provoked; both are bounded at three passes
  now anyway.
- **Four tests, and the reason none existed is the reason this shipped:**
  every test in `tests/test_split_panes.py` hands `DoxaApp` a factory
  that returns instantly, and a blocking factory is not an exotic case to
  simulate — it is the only kind that ships.
  `test_a_vsplit_never_blocks_the_event_loop` and its `Ctrl+T` twin watch
  a 10 ms heartbeat across the gesture and fail at **522 ms** and
  **513 ms** on the pre-fix code; they assert on the *loop*, not on a
  duration, because a split may take as long as spawning a session takes
  and may not stop the application while it does.
  `test_a_slow_spawn_still_produces_a_working_pane` pins the far side of
  the new window, and `test_the_idle_app_arms_nothing_new_when_it_splits`
  pins the reporter's first guess as the fact it turned out to be.

**"The hotkeys Alt+D and Alt+S are unresponsive."** `/split` and
`/vsplit` worked, so the actions and the layout were fine and the fault
was upstream of binding resolution. It was upstream of the terminal, too.

Measured against textual 5.3.0's own parser rather than its
documentation:

```
XTermParser().feed("\x1bs")       -> Key('escape'), Key('s')
XTermParser().feed("\x1b[115;3u") -> Key('alt+s')      # kitty
XTermParser().feed("\x1b[1;3D")   -> Key('alt+left')   # legacy, fine
```

The string `alt` appears **exactly once** in `textual/_xterm_parser.py`
(line 338), inside the CSI-u modifier table. There is no
ESC-prefix-to-Alt path in the parser at all, and `_ansi_sequences.py`
hand-maps a few two-byte ESC pairs (`\x1bf` → `ctrl+right`, `\x1bb` →
`ctrl+left`, `\x1b\x7f` → `ctrl+w`) and no letter to Alt.

- **v0.91.0's premise was true and its conclusion was wrong.** Every
  terminal *has* sent Alt as an ESC prefix since long before the kitty
  protocol — but a binding depends on what **Textual** decodes, not on
  what the terminal sends, and it decodes `ESC s` as Escape followed by a
  bare `s` that a focused prompt cheerfully types. `alt+s` only ever
  fired on a terminal that granted the kitty protocol. This is the third
  time this project has claimed a key it could not have: Ctrl+C (fixed
  v0.85.0), Ctrl+Shift+V (rejected 2026-08-29), and now Alt+letter —
  which was chosen as the *fix* for the second.
- **`Ctrl+O` splits stacked below, `Ctrl+N` side by side, `F2` opens the
  live diff.** Which letter was not a free choice. Subtracting from
  `ctrl+<letter>`: `h i m` have no distinct byte (their C0 code *is*
  backspace/tab/enter); `a c d e f k u v w x y z` are Textual's own
  `TextArea.BINDINGS`, and the prompt **is** a TextArea, so a
  `priority=True` binding would *take* the key from it rather than share
  it — v0.85.0's lesson applies to a widget as much as to a terminal;
  `c z s q l b` are the terminal's own (SIGINT, SIGTSTP, XOFF, XON,
  redraw, and tmux's default prefix, which a tmux user cannot press at
  all); `j` is literally the LF byte; `p r t w q ,` are already DoxaApp's.
  The remainder is exactly `ctrl+n` and `ctrl+o`. Deliberately not
  mnemonic, for the same reason S/D were not — no letter resolves the
  vim/tmux disagreement about which word means which direction — so every
  description and summary still spells the direction out in words.
- `/diff` moves too, because `alt+g` inherited the same defect and would
  otherwise stay documented and dead. **F2**, not a third ctrl+letter,
  because the subtraction above leaves none: an F-key arrives as `SS3 Q`
  / `CSI 12~`, older than the problem, claimed by neither App, Screen nor
  TextArea, passed through by tmux, and not one of the two most emulators
  bind (F10 menu, F11 fullscreen).
- **`alt+←/→/↑/↓` keeps its Alt**, re-checked rather than assumed: a
  modified *arrow* is `CSI 1;3<final>`, a different physical encoding
  from a modified *letter*, and Textual decodes it under both protocols.
- **`alt+s` / `alt+d` / `alt+g` stay bound** as kitty-tier aliases beside
  the new primaries — the arrangement `Ctrl+Tab` has had beside
  `Shift+Tab` since v0.42.0. They are real muscle memory on kitty,
  ghostty, WezTerm and foot, where they always worked, and `/help` now
  marks them `✗` where they cannot arrive.
- **`doxa/keyboard.py` was the reason nothing warned.**
  `unreachable_under_legacy` answered `False` for `alt+<character>` and
  said so in a comment — *"Alt is absent on purpose: it is sent as an ESC
  prefix and works fine"*. That is the one entry this module got **wrong**
  rather than merely omitted. It now answers True for `alt+<character>`
  and `ctrl+alt+<character>`, and still False for `alt+<named key>`;
  `/doctor` and `/help` list Alt+S, Alt+D and Alt+G on a legacy terminal.
- **The tests were green for three releases over a key that could not
  arrive, because none of them touched the encoding.** `pilot.press`
  feeds Textual a key *name* and the binding table resolves names.
  `tests/test_split_keys.py` grew the missing layer: it drives
  `XTermParser` with the bytes a terminal actually sends, and
  `test_no_primary_binding_is_unreachable_under_the_legacy_encoding`
  generalises the defect so the next release cannot repeat it in a new
  key. That last one found one pre-existing case and **names** it rather
  than filtering it out — `settings` on `Ctrl+,` has no legacy byte at
  all, is reached by the `/settings` command, and is marked in `/help`:
  the v0.39.0 arrangement working as designed.
- `scripts/screenshot.py` and `scripts/record_gif.py` drive the real key,
  so both now press `ctrl+n` and `f2`. GIF captions are metadata rather
  than burned pixels, so the committed assets are unaffected.


## 0.94.0 — 2026-08-31

**The gallery has not been able to render since v0.91.0, and nobody
knew.** Every scene in `scripts/screenshot.py` raised
`ValueError: No Tab with id '--content-tab-pane-1'` before it wrote a
single file.

- **`scripts/screenshot.py`, `scripts/record_gif.py`** set
  `TabbedContent.active` to a `SessionPane`'s own `id`. Correct until
  v0.91.0, when a pane stopped BEING the tab and became a leaf inside a
  `PaneTab` — after which the pane id and the tab id are different
  strings and the tab strip rejects the first. `pane.tab_id` is the
  property that answers the question the code was asking; six
  `TabbedContent.active` assignments across the two scripts, plus two
  `get_tab()` calls carrying the same assumption.
- Nothing regenerates this gallery except running it, and it was last
  run at 0.87.0 — so the break rode `main` through three releases
  unseen. This is the argument for regenerating assets every release
  rather than when they look wrong: a still cannot look wrong if it was
  never written.

**Three surfaces that shipped without a picture now have one.**

| asset | scene | shows |
|---|---|---|
| `live-diff.png` / `.svg` | **new** | session left, diff right, in one tab: `2 files changed, +9 −1 against main`, `doxa/auth.py` expanded to two side-by-side hunks (the second changed file sits one row below the fold, behind the scrollbar — the expanded section is 66 rows in a 67-row pane), the amber `⏳ reject queued — applies when this turn ends` badge over a disabled reject button, and the session's own turn still in flight |
| `split-panes.png` / `.svg` | **new** | one tab split down the middle: two identity blocks, two models, two transcripts, two status bars |
| `split-panes.gif` | **new** | `alt+d` and a pane arriving; a turn in it; `ctrl+shift+←` back; `alt+→` on the divider. 5 frames, 602 KiB |
| `folder-chip.png` / `.svg` | **new** | `dir design-notes` leading the status bar with no branch half, over `/dir`'s answer and a bare `/cd` explaining why a running session cannot move |
| 13 existing stills | recaptured | at `DOXA 0.94.0`; they read `0.87.0` before this pass, not `0.93.0` |
| 12 existing GIFs | recaptured | same |

- `live-diff` and `folder-chip` are the first scenes in this gallery
  rooted **outside this checkout** (new `Scene.cwd_factory`), and neither
  could be faked: `doxa.diff.compute` shells out to real `git diff`, so
  those hunks are hunks git produced; and the folder chip only exists
  where there is no `.git` above the session, which this repository can
  never be. Both build under the script's own throwaway temp root, the
  same isolation block that closed the `205 proposals` / `78 proposals`
  leak in v0.67.0.
- The pending badge is reached through a turn that genuinely never ends
  (`_DiffEngine` holds the second `send()` open), not by assigning
  `pane.turn_in_flight` — a picture of the feature rather than a picture
  of a flag.
- `belief_count` is still hardcoded to 3 in `tests/fakes.py`; the
  `beliefs-picker` scene keeps overriding it on the INSTANCE, because a
  screenshot may not move a number several hundred tests are written
  against.
- Geometry unchanged and re-verified: 16 SVGs at 3049x1682.6, 16 PNGs and
  13 GIFs at **3068x1734**, every file non-empty, every SVG parsing as
  XML.

**"What you get" was eleven paragraphs wearing a hyphen.**

- **3,717 → 1,884 characters.** Eleven bullets averaging 338 (longest
  563) became thirteen averaging 145, longest 153. Every bold lead-in
  kept — that half was working.
- Split panes and the live diff get lead-ins of their own; `/cd`, `/dir`
  and the folder chip fold into the status-bar bullet, where the chip
  actually lives.
- Three claims were **wrong**, not merely long, and are corrected rather
  than compressed:
  - *"one write path — a human approving a staged proposal"*. The MODEL
    has one (`lore_remember`, which only stages). A human has three more
    from inside DOXA that are not approvals: `retract_belief`,
    `record_belief_outcome`, `reject_pending`. The claim that survives is
    that nothing NEW reaches the model but a human approving one staged
    row.
  - *"the `mode:` chip ALWAYS shows it first"*. A `default` chip stands
    down below `MODE_CHIP_MIN_COLS` (110). Every non-default mode is
    painted at every width, which is the claim worth making.
  - *"`auto` and `bypassPermissions`"* as the complete list of modes that
    stop asking. `UNASKED_MODES` is three; `dontAsk` is the third. The
    `permission-mode.gif` caption carried the same two errors and is
    fixed with it.
- And one section-level claim: `docs/plans/live-diff.md` was still listed
  under **"Specified, but not built"** with *"Nothing implemented"*
  against it, two releases after v0.92.0 shipped it. Removed, with
  `split-panes.md` named beside it as the other plan that graduated. That
  section's own lead sentence said "Four documents" over eight bullets;
  it says Eight now.

**Nothing was deleted — `docs/manual.md` absorbed it, and mostly already
held it.** Checked before moving: eight of the eleven bullets were
already covered there in more depth than the README had them. Four
sections are new, because those were the four the manual genuinely
lacked:

- **The spawned CLI** — `CLAUDE_CONFIG_DIR` isolation appeared nowhere in
  the manual (grep-confirmed), and `/plugins` had a one-line command row
  with no prose.
- **The transcript** — there was no transcript-rendering section at all:
  no markdown streaming, no `✻ Reasoning (N chars)`, no `⚒ Tool calls
  (N)`, no chip-expands-to-`ARGS`-and-`RESULT`, no in-flight marker.
- **Split panes** — one paragraph existed about RESTORING a split layout
  and nothing about making one; `alt+s`/`alt+d`, the focus and divider
  keys, the depth cap and the 34x9 size floor were undocumented.
- **Where a session is** — `/dir`, `/cd` and the folder chip predated the
  manual entirely.

Plus: the folder chip joins the status-bar chip table; `/split`,
`/vsplit`, `/diff`, `/dir` and `/cd` join the command tables, none of
which had a row; and the Contents list gains the new sections along with
**The live diff** and **Restoring tabs**, which were reachable by anchor
and absent from the manual's own index.

**Three README alt texts described something the image does not show.**

- `context.png` — the alt named a `61k/180k tokens (33.8%)` headline. The
  block renders `in use 60,910 / 180,000 tokens · 33.8%`; that string was
  never on screen.
- `subagent-tracker.png` — the alt read the status ROW as saying
  `1 agent`. `⧉ 1 agent` is the status BAR's chip; the row beneath it
  carries the subagent's own description.
- `hero.png` — `Tool calls (1)` is `⚒ Tool calls (1)`, and the alt's chip
  inventory skipped the `sub:` tier chip sitting in the middle of the row
  it was listing.
- `beliefs-picker.png` — the group headers carry counts
  (`project (5 beliefs, 3 tested)`), and the user group is
  `user · stated`; both now named.

**Ten scenes were being generated and referenced by nothing.**
`trace.png`, `reasoning.gif`, `sessions.png`, `clock.png`,
`palette.gif`, `rename.gif`, `attention-blink.gif`, `image-support.png`,
`banner-blocks.png` and `transparent.png` were regenerated every pass and
named in no document — the exact condition that let `beliefs-browser.png`
rot for eighteen releases. They now get a named line at the foot of the
gallery rather than ten more full-width images in an already long one.

**Suite: 1,617 passed**, unchanged from the pre-pass baseline — the two
script edits touch nothing `tests/test_record_gif.py`'s registry checks or
`tests/test_banner.py`'s rendering assertions read differently.

## 0.93.0 — 2026-08-30

**Where am I, and can I move.** Reported: "we should also provide a /cd and
/dir command where /dir lists the cwd".

- New **`/dir`** (`SessionPane._cmd_dir`, `doxa/session/commands.py`): the
  session's working directory, plus the worktree sidecar's branch and base
  when there is one.
- New **`/cd <path>`** (`_cmd_cd`) — and it does NOT move this session.
  There is no SDK control request that changes a running CLI subprocess's
  cwd, so rather than appear to work it opens a new tab at the target
  (`DoxaApp.open_tab_at`) and says in its own output that the current
  session is unchanged. A command that silently lies about its effect is
  worse than one that refuses.

**A directory is not a repo, and the status bar now says which.** Reported:
"if i start in a non-repo dir, there is no folder/repo chip shown in the
status line".

- New **`GitLine.folder_label`** (`doxa/ui/statusline.py`) with its chip in
  `doxa/session/chips.py`: outside a git repo the bar shows `dir NAME`,
  deliberately a different shape from `repo ⎇ branch` rather than the same
  chip with a blank branch. Clicking it opens the same directory picker.

**A session that cannot be resumed is still readable.**

- New **`DoxaApp._resume_read_only`** (`doxa/app.py`): when the CLI refuses
  to resume a session found by `/search`, its surviving transcript opens
  read-only through the existing `ArchivedSessionTab` and
  `doxa.transcript.mount_transcript` instead of reporting an error and
  stopping. No second viewer.

**The resume picker joins the column grid.**

- **`_fmt_resume_row`** now renders through the shared
  `format_picker_row`/`PICKER_PREFIX_WIDTH` grid, so a resume row starts its
  text at the same offset as a belief row and a pending row — the third
  picker to be reported for uneven columns and the last one that was.
  Sort order was already newest-first; verified, not changed.

**`sub:raven`: nothing to fix.** Investigated rather than patched — the
label was already corrected in `266d8d3` and is pinned by
`tests/test_identity.py::test_an_unrecognised_subscription_type_is_not_rendered_as_a_plan`.
Recorded here because "we looked and it was already right" is a result.

## 0.92.0 — 2026-08-30

**The live diff, to `docs/plans/live-diff.md`.** While an agent edits
files in its worktree, the session sits on the left and a **live-synced
diff** sits on the right, red/green, updating as edits land. Any hunk can
be **rejected**, which reverts exactly that hunk and tells the session's
agent what was rejected and why. This is the review loop DOXA previously
made you leave the app for: read a tool-call fold, or open the repo
elsewhere and diff by hand.

It is the first concrete consumer of v0.91.0's layout tree, and the spec
posed that as a design check — *if the split cannot express session left,
diff right, both live, the split is wrong*. It could express the
geometry and could not express the model; see the last chapter.

### Where the diff comes from

- New `doxa/diff.py` (827 lines): the MODEL half — pure data, pure
  functions and one thin subprocess boundary, the rule `doxa/layout.py`
  already follows. `Hunk`, `FileDiff`, `DiffResult`, and `parse()`, which
  reads `git diff`'s unified output. **No differ was written**: the
  porcelain is stable, and a second differ would be a second source of
  truth. If this pane can show something git cannot, it is wrong.
- **The tool-result stream is the tick. Not a file watcher.** DOXA has a
  documented no-timer, no-per-frame rule, and `docs/plans/code-graph.md`
  already refused a watcher for the same reason — a second lifecycle to
  get wrong. `SessionPane._render_tool_result`
  (`doxa/session/runtime.py`) now calls `_tick_diff`, which recomputes
  when an `Edit`, `Write`, `NotebookEdit`, `Task` or tree-touching `Bash`
  result lands and at no other time. The same reasoning that gave
  v0.56.0's spinner zero idle cost: a token arriving is a tick, and it
  costs nothing when nothing is arriving.
- The tool INPUT is not on a `tool_result` event (only a 280-char result
  summary is), so the predicate reads the command off `ToolChip.tool_input`
  — the chip map `_render_tool_result` is already handed.
- **`diff.bash_touches_tree` is an ALLOW-list of 32 read-only verbs plus
  16 read-only `git` subcommands, not a deny-list of destructive ones**,
  and any redirection (`>`) makes a segment a write whatever the verb is.
  Deliberately over-inclusive: a false tick costs one `git diff` on a
  local tree, a missed tick costs a diff that silently disagrees with the
  disk, and only one of those is survivable on a live surface.
- Rate-limited by `exclusive=True` on its own worker group and by nothing
  else. A turn landing thirty edits fires thirty ticks, each cancelling
  the last in-flight git call; the user sees the state after the last
  edit rather than a queue of thirty stale ones. No interval to tune.
- Git runs in the TUI process against a worktree on the same machine, so
  the diff never crosses the daemon socket. `peers.MAX_FRAME_BYTES` and
  `daemon._fit_page` are therefore **not** on this path and `_fit_page`
  gains no caller — the same call `doxa/transcript.py` made and for the
  reason it gives: one implementation enforcing one budget, for the calls
  that really are frames.

### "No changes" and "cannot tell" are different sentences

- `diff.base_for(cwd)` reads the worktree sidecar (`doxa/worktrees.py`)
  and returns one of three answers, and `DiffResult.headline()` renders
  each differently. `STATUS_OK` with no files is **"no changes against
  main"**. `STATUS_NO_BASE` is **"cannot determine a base — …"**.
  `STATUS_ERROR` is **"cannot read the diff — …"**.
- **The one way to reach the refusal is v0.33.0's measured trap:
  `base_ref == branch`.** Nothing the session committed can appear in a
  diff against its own branch, so a session that committed all its work
  would render as untouched. That is the same emptiness that made
  `commits_ahead` structurally unmeasurable, read as zero, and force-
  deleted real commits; `worktrees.finalize` already refuses on the
  identical condition rather than trusting the number, and the diff now
  inherits the refusal instead of the defect.
- A MISSING sidecar is not that case — worktree-per-session may simply be
  off, and uncommitted work against `HEAD` is a real, smaller claim. It
  comes back as `BASE_HEAD` and the headline says *"against HEAD (no
  worktree base recorded)"* rather than passing the smaller claim off as
  the larger one.

### Rendering, and what it refuses to render

- New `doxa/ui/diffview.py` (650 lines): `DiffPane`, `FileSection`,
  `HunkView`. **Collapsed per file by default with the changed-line
  counts** on the fold — the `ToolCallsSection` pattern, with
  `ToolChip.format_body`'s lazy build on top, so forty hunks nobody
  expanded are not forty mounted widget trees. A twenty-file diff is
  twenty rows.
- **Binary and huge files are named, not rendered.** The size cutoff
  (`MAX_HUNK_LINES_PER_FILE = 2000`) is on the DIFF, not the file: a
  40 MB asset with a one-line change is cheap and a 3 MB generated file
  rewritten whole is not. A named file's hunks are DROPPED rather than
  hidden, so the reject button can never reach a patch nobody saw.
- Caps at 200 files and 20 000 body lines, and a result that hit either
  says so (`DiffResult.truncated`, `dropped_files`) rather than handing
  back a short answer that renders as whole.
- **Unified is the default; side-by-side is gated on a measured width.**
  `SIDE_BY_SIDE_MIN_COLS = 100`, the way `CTX_ABSOLUTE_MIN_COLS` gates the
  ctx chip. The spec's arithmetic backwards: at 80 columns a half-width
  pane is 40 and each side is 20, which is unreadable. Forwards: a legible
  side is ~44 columns (`layout.MIN_LEAF_WIDTH` plus a gutter), so
  2×(44+4)+1 = 97, rounded to 100. Unlike the ctx chip, an UNMEASURED
  width (0, the first paint) reads as *not* allowed — a chip appearing
  late is a flicker, a side-by-side that had to fall back after you read
  it is a page that changed under you.
- Hunk bodies are Rich `Text`, never console markup: a diff body is
  arbitrary source and source contains `[`.
- The pane's first paint is deferred one refresh (`call_after_refresh`)
  and `_repaint` retries rather than returning: a leaf created at runtime
  is mounted into a box made empty ahead of time, so its own `compose`
  children are still arriving when `on_mount` fires. Measured — without
  it the pane said "reading the diff…" forever. This is the same window
  v0.91.0 hit from the other side (`SessionPane._system`'s `MountError`),
  and both conditions are guarded, in order: `NoMatches` first, then
  `is_mounted`, because `query_one` succeeds for a node that is in the
  DOM and not yet mountable.

### Reject: two actions, in that order

- Rejecting is **the file going back AND the agent's belief about the
  file being corrected**. Doing only the first leaves the agent
  confidently wrong — it patches against a premise that is no longer
  true. Doing only the second leaves the bad code in the tree. The order
  matters because the message says the change is already gone.
- `diff.revert_hunk` builds a ONE-HUNK patch from the recorded header and
  body (`hunk_patch`) and runs `git apply --reverse --recount`, `--check`
  first. Two hunks in one file are independently rejectable and rejecting
  one does not discard the other — asserted against a real git worktree
  with two edits ten lines apart.
- **A patch that no longer applies changes nothing and says why**, in the
  pane's own words: *"that hunk no longer applies to f.py — nothing was
  changed. the file moved underneath it."* Never forced: a three-way
  apply is conflict resolution, and this is explicitly not a merge tool.
- **A rejection during a turn is QUEUED until `turn_done`, and visibly
  marked** — the spec weighed three answers and this is the one it chose,
  because a rejection the user has clicked and cannot see the effect of
  is the worst of the three. Reading the code added a second argument
  the spec did not have: `daemon._handle_prompt` REFUSES a second
  concurrent prompt outright (*"a turn is already running in this
  session"*), so applying immediately could not have delivered the
  message half of the pair even if the revert half landed. Applying
  immediately is not a race this app could win.
- The flush hangs off the tail of `SessionPane._run_turn`, **not**
  `_render_turn_done`: that renderer fires while `_run_turn` is still
  inside its `async for`, with `turn_in_flight` still True and the
  exclusive `"turn"` worker still running — and applying a rejection
  STARTS a turn, which from inside that worker would cancel the worker
  doing the telling. `call_after_refresh` puts it on the message pump one
  step outside that coroutine's lifetime.
- Queued rejections apply in click order, and a later one that no longer
  applies (because an earlier one moved the file) is REFUSED and reported
  rather than recounted into something the user did not ask for.
- Closing a diff that still holds queued rejections is refused with a
  count, rather than discarding them silently.
- **A `FileSection` defers its build until its own hunk container is
  mountable.** A `Collapsible` is handed its contents in `__init__`, so
  the container exists from the section's first line and is mounted only
  when the section composes — and `_remark_queued` builds a section it
  just mounted. Measured as `MountError: Can't mount widget(s) before
  Vertical(classes='diff-hunks') is mounted` on one full-suite run and on
  none of the targeted ones: a race, the same window v0.91.0 met in
  `SessionPane._system`. Deferred rather than dropped, since an unbuilt
  section is a fold that opens onto nothing; `_apply_badges` therefore
  retries, bounded at three passes.
- A lazily built `HunkView` is given its width rather than reading
  `self.size`, which is zero until the widget has been laid out — without
  it a hunk built on first expand rendered unified in a pane wide enough
  for two columns and stayed that way until the next resize.
- **The pending badge is put back after every tick.** Each recompute
  rebuilds the hunk widgets, so without `DiffPane._remark_queued` a
  queued rejection would be visibly marked only until the agent's next
  edit — exactly the interval during which it is queued, and the whole
  justification for queueing rather than refusing. The file it is in is
  re-expanded for the same reason (a badge inside a fold nobody opened is
  not a badge), and files the user had open stay open across a rebuild
  rather than folding shut mid-read.

### The message is user-authored, and that is the whole trust argument

- `diff.reject_message` names the file, the hunk's line range, up to 12
  quoted changed lines and the user's reason if one was typed — *"a
  rejection with a reason is worth far more than a bare revert, because
  it stops the agent re-making the same edit"*. With no reason it SAYS so
  rather than omitting it, because an agent told only "I rejected this"
  will guess, and a wrong guess is what makes it re-make the edit.
- It goes down `SessionPane._run_turn`, the same door
  `on_prompt_submitted` uses for a line you typed, and it carries no
  framing marker at all. That is the deliberate contrast with
  `peers.PEER_UNTRUSTED_INTRO`, which exists because ANOTHER AGENT wrote
  the text: a human clicking reject in their own session is the user
  speaking, and wrapping it as untrusted data would tell the agent to
  weigh its own user's instruction as hearsay. Asserted, not described —
  `tests/test_live_diff.py` checks the intro is absent.

### The layout tree learned that a leaf is not always a session

This is the design check the spec asked for, and the honest answer is
*nearly*. Everything about painting, focus and sizing worked unchanged —
an unfocused visible leaf keeps rendering, `SplitBox` is
orientation-agnostic, and **`Alt+←/→` already moved the divider**, so the
"sibling gesture" the spec asks for cost no new key. What did not work is
that `layout.Leaf` WAS a session:

- `layout.Leaf.view` (`VIEW_SESSION` / `VIEW_DIFF`) is the one new field.
  Without it `split._leaf_of` returns `None` for a diff child, the split
  node collapses to its one surviving child, `PaneTab.tree()` reports
  "no split" for a screen that plainly shows one, and the persisted
  record carries that lie into the next launch — the exact defect class
  `doxa/ui/split.py`'s own docstring warns about.
- It is written to JSON **only when it is not the default**, so every
  record v0.91.0 could produce is byte-identical and an unrecognised view
  from a future version degrades to the session rather than dropping the
  leaf.
- A diff leaf carries the `session_id` of the session it is a diff OF.
  Not a spare field: it is what keeps `layout.prune` correct without
  knowing anything about diffs — a diff whose session died is pruned by
  the same rule that prunes the session.
- `split._node_of` replaces the `isinstance` chain in `_tree_of`: a leaf
  answers for itself through `layout_leaf()`, so `doxa/ui/split.py` does
  not have to learn what a diff is (it imports `SessionPane` lazily to
  break a cycle, and a second such import per surface is that cycle
  waiting to come back).
- `PaneTab.surfaces()` is new beside `PaneTab.leaves()`, which is
  unchanged and still sessions-only. `DoxaApp._pane_regions` reads
  `surfaces()` so `ctrl+shift+→` can land on the diff; every caller that
  wants an ENGINE still reads `leaves()`. Conflating them is how a diff
  pane ends up handed to something expecting a session.
- `DoxaApp.focused_surface()` is the geometric twin of `focused_pane()`.
  `active_pane` still answers with a SESSION while the keyboard is in a
  diff — resolved to that diff's own session, which in a tab holding two
  sessions is a different answer from the tab's last focused leaf.
- `grow_pane_towards` reads `focused_surface()`, so `Alt+←` widens the
  DIFF when the keyboard is in it.
- `DiffPane.session_pane()` searches from the enclosing `PaneTab`, never
  from `self.app`. `Widget.app` reads a context variable that is only
  reliable while the app's own message pump is on the stack; measured, it
  made two tests pass alone and fail in file order. Walking the DOM is
  also the more correct search — the owner-first invariant puts a diff
  leaf in the same tab as the session it was opened from.

### Surfaces, keys and the open questions

- `/diff` (registry row, `Panes & tabs`) and `SessionPane._cmd_diff`;
  refusals land as a transcript block in the pane they are about, not a
  toast over some other pane, exactly like `/split`.
- **Alt+G**, joining `Alt+S`/`Alt+D` for the identical measured reason
  (`doxa/keyboard.py`): Alt goes out as an ESC prefix under BOTH
  encodings, where every `ctrl+shift+<letter>` chord collapses onto plain
  `ctrl+<letter>` and is undeliverable. `tests/test_split_keys.py`'s
  collision list now covers it, so the next release that adds a binding
  trips over it instead of shipping it.
- Opening the diff **does not move the keyboard**, unlike a split. A
  split spawns a session you asked to work in; a diff is something you
  asked to look at while you keep typing. "Visible and focused are
  different states" cuts both ways.
- **Open question 2 — untracked files: yes, included.** A created file is
  what a reviewer wants to see, and plain `git diff` has no hunk for it.
  Done with `git diff --no-index` against `/dev/null`, NOT `git add
  --intent-to-add`: this is explicitly not `git add -p`, staging is a git
  concept the user owns, and a review surface that writes the index has
  changed the thing it was reporting on. Capped at 50 files.
- **Open question 3 — per session.** One diff per session, matching the
  isolation model; a second `/diff` closes the one that is open.
- **Open question 4 — a queued rejection does NOT survive a restart.** It
  is held on the widget and the widget is new; the restored pane comes
  back showing the un-reverted hunk, which is the truth. Losing it
  silently would be the defect — losing it visibly, with the hunk still
  on screen and still rejectable, is not.
- **Open question 1 — rejecting does not stop the agent, and there is no
  "reject and interrupt".** Interrupting is a bigger act than rejecting
  and this release did not make it a side effect of one; it is also not
  offered as a separate action yet.

### What is not covered

- **No mouse drag on the divider** — `Alt+arrow` only, as in v0.91.0.
- **No word-level intra-line highlighting.** Side-by-side pairs removed
  and added runs positionally, which is a layout, not a second differ.
- **No `git diff` options surface**: no whitespace mode, no context-line
  control, no submodule handling beyond what git's default output says.
- **The diff does not cross the daemon socket**, so an attached TUI on a
  machine that is not the worktree's is not a case this handles. There is
  no remote case today (the daemon socket is a Unix socket) and this adds
  none.
- **Rejecting is per hunk only** — no partial-hunk, no line-level, no
  editing a hunk by hand. Reject or keep; a half-editor is worse than
  none.
- **No merge, no conflict resolution, no staging.** Unchanged from the
  spec's own "what this is not".
- 37 new tests in `tests/test_live_diff.py` (812 lines), against a real
  git worktree for the model half and a real `Pilot` for the leaf. Seven
  were verified failing first against the specific mechanism each pins,
  by reverting that mechanism and re-running. Full suite 1600, up from
  1563.

## 0.91.0 — 2026-08-30

**Recursive split panes, to `docs/plans/split-panes.md`.** A tab held one
session from Phase 3 to 0.88.0, and everything that *sounded* like a split
was a tab page. A tab now owns a **layout tree** and can show several
sessions at once — `/split`, `/vsplit`, directional focus between panes,
proportional dividers that persist — while a tab whose tree is a single
leaf is byte-for-byte the tab it always was, which is what keeps the
migration honest.

### The layout tree, and why the tab stopped being the pane

- New `doxa/layout.py` (396 lines): the MODEL half, pure data and pure
  functions, no widget and no `self` — the rule `doxa/ui/labels.py`
  already follows. `Leaf` (session id, pinned name, cwd, prompt ratio) and
  `Split` (orientation `row`/`column`, ordered children, per-child
  weights). Recursion is genuine: a split may contain a split.
- **Weights are proportional, never absolute.** `Split.__post_init__`
  runs every weight tuple through `normalise`, which returns `count`
  positive weights summing to 1.0 and degrades EVERY malformed input —
  wrong count, zero, negative, NaN, infinity, non-numbers — to the even
  split rather than raising. A layout is chrome; a corrupt one costs the
  user their proportions, never their session, the same posture
  `doxa.config.load` takes on a broken settings file.
- New `doxa/ui/split.py` (391 lines): the WIDGET half. `SplitBox` is one
  tree node — transparent while it holds one child, laying children out
  along its orientation with `fr` units once divided (never cells, which
  is what makes a terminal resize preserve the ratio for free). `PaneTab`
  is the tab.
- **`SessionPane` stopped being a `TabPane`; that is the whole of the
  structural change.** A `TabPane` inside a `TabPane` posts a `Focused`
  message that reassigns `TabbedContent.active` to an id that is not a
  tab, so the tab became `PaneTab` (a container of leaves) and the session
  surface became an ordinary `Vertical`. Its subtree, its ids, its
  `self.query_one("#block-list", …)` calls and every method's behaviour
  are unchanged; `SessionPane.tab` / `.tab_id` are how the label and
  status-class writers reach the tab that holds it. The tab keeps the id
  the pane used to carry (`_restore_pane_id`'s `restore-<session id>`,
  `DoxaApp._FALLBACK_PANE_ID`), so activation, the tab strip, the rename
  field and the persisted-set lookups still name the same strings.
- **Why every leaf is born inside two empty boxes.** Textual 5.3 cannot
  re-parent a mounted widget — a `mount` of an already-mounted widget is a
  silent no-op that orphans it, measured against 5.3.0 before this was
  designed. A split therefore cannot WRAP a pane that already exists, so
  the box it will need has to be on screen before the user asks.
  `layout.SPLIT_SLOTS = 2` is how many, and it is therefore both the
  interactive depth cap the spec asks for and the reason every split node
  this app produces has the pane that was split as its FIRST child. Two
  slots is exactly what a 2×2 grid costs: split one pane sideways, then
  split each half the other way.
- Splitting past the allowance is refused in words — *"this pane is
  already split as deep as DOXA goes (2 levels) — close a pane, or split
  one of its neighbours instead"* — and changes nothing. The cap is a
  constant, not an architectural limit.
- Minimum sizes: `MIN_LEAF_WIDTH = 34` columns, `MIN_LEAF_HEIGHT = 9` rows
  (derived, not chosen: one status bar + a bordered prompt at its one-row
  minimum + a transcript that can show a turn). `layout.split_refusal`
  halves the pane's REAL painted rectangle and refuses with a message
  naming the floor rather than performing an unusable sliver.
- `/split` (a second session STACKED BELOW this one) and `/vsplit` (SIDE
  BY SIDE) are registered in `doxa/commands.py` under the existing
  **Panes & tabs** group, so they reach `/help`, the palette and
  autocomplete like everything else, with handlers `_cmd_split` /
  `_cmd_vsplit` in `doxa/session/commands.py`. The bindings are
  **Alt+S** and **Alt+D** — Alt rather than a `ctrl+shift` chord because
  of what a terminal can actually deliver: under the legacy encoding
  `ctrl+shift+<letter>` sends the same byte as `ctrl+<letter>`, so every
  such binding is undeliverable there, while Alt goes out as an ESC prefix
  every terminal has sent since long before the kitty protocol. They join
  the family already present — `Alt+arrow` grows a pane. The COMMAND names
  follow **vim, not tmux**: `:split` is stacked and `:vsplit` side by side,
  where tmux's `split-window -h` means the opposite. Both the binding
  description and the registry summary spell the direction out in words,
  because no letter resolves that ambiguity for a reader who knows the
  other convention.

### Focus: splits inherit 0.38.0's rule rather than re-litigating it

- **A new leaf mounts unfocused, and whatever creates it says where the
  keyboard goes.** `DoxaApp.split_active_pane` focuses the NEW pane
  explicitly, the way `action_new_tab` focuses a new tab — a user who just
  asked for a second pane is asking to work in it.
- `DoxaApp.active_pane` now means *the focused leaf of the active tab*.
  With two panes visible, "the tab that is showing" stopped being an
  answer to "which session does this keystroke mean", and the pane holding
  the keyboard is one. `focused_pane()` derives it from `self.focused` —
  the widget Textual says has focus — rather than from a flag this app
  maintains, because a flag is a second answer to a question the framework
  already answers, and the two drifting apart is how the 0.32.0
  restored-active-tab defect happened. `PaneTab.focused_leaf` is the
  fallback for when focus is legitimately not in a pane at all (a modal,
  the palette, the rename field).
- `_focus_tab` accepts a leaf as well as a tab, and re-states the intent
  on the next refresh when the leaf's own subtree has not composed yet
  (`mount` resolves when the widget is in the DOM; its children arrive a
  message-pump turn later). Swallowing that miss — which the old
  `contextlib.suppress` did — would have left the keyboard in the pane the
  user split AWAY from, silently and only sometimes.
- Directional focus is **geometric, never "next pane"**: `ctrl+shift+←/→/
  ↑/↓` run `layout.neighbour` over the panes' real painted rectangles.
  Only panes strictly beyond the current edge are candidates; only ones
  whose perpendicular span overlaps (so moving right out of a 2×2's
  top-left cell cannot land in the bottom-right one); nearest edge wins,
  ties break deterministically. The overlap rule relaxes exactly once — if
  nothing overlaps, the nearest pane in that direction is taken anyway, so
  a narrow pane at the bottom of a column is never a dead end.
- Closing one leaf of a split collapses the split and keeps the tab
  (`DoxaApp._close_pane`'s new branch + `split.prune_boxes`); closing the
  last leaf closes the tab, exactly as 0.88.0's `_close_pane` did. The
  survivor nearest the closed pane on screen inherits the keyboard, named
  explicitly (`_closest_sibling`) for the same reason every other focus
  move here is: a leaf disappearing is not a user saying where to go next.
- `_switch_to_tab` resolves a LEAF id as well as a tab id, so the peers
  chip's "that session is already open here" jump lands on the pane it
  named rather than on whichever leaf its tab was last in.
- New `DoxaApp._activate_tab`, used by all four sites that add a tab.
  `TabbedContent.active` validates through `Tabs.validate_active`, which
  raises `ValueError: No Tab with id …` whenever the strip does not — yet,
  or any longer — hold a header for that pane. Measured on this branch in
  both states: `/attach` adds its tab from a worker and ran the next line
  before the header's mount landed, and the same worker can still be
  finishing while the app tears down under it (the 0.85.0 defect class,
  which `tests/conftest.py`'s `_errors_must_be_claimed` fixture turned
  into two real test failures rather than a silent error block). Retried
  once on the next refresh, then given up on — retried rather than
  suppressed, because a new tab that silently fails to activate is the
  "it arrived by accident" failure 0.38.0 removed.
- The `PaneTab` an ordinary tab is born with takes an id DERIVED from its
  leaf's (`tab-<pane id>`) rather than the same string. Textual only
  forbids duplicate ids among siblings, so a tab and the pane inside it
  could legally share one — and every id-selector query in the app would
  then resolve to whichever the breadth-first walk reached first, which is
  the tab, silently, for the rest of the release.

### Visible is not seen

- **The affordances no longer clear for a pane that is merely visible.**
  `-done-unseen`, the needs-input blink and the `-staged` tint cleared on
  tab ACTIVATION through 0.88.0 — the same event as "this pane got the
  keyboard", while a tab held one pane. It is not the same event any more,
  and the spec settles it against the old behaviour: the marker means *you
  have not looked at this*, and a pane in the corner of a 2×2 may
  genuinely be unread. `DoxaApp._clear_seen_marks` runs from `_focus_tab`,
  for the ONE pane that got the keyboard, and its siblings keep their
  marks.
- The state moved onto the pane (`SessionPane._marks`, `has_mark`),
  because one tab header cannot carry an answer for several panes. Each
  leaf now also wears its own CSS class, and `doxa/theme.tcss` gives it a
  one-cell left border in the tab strip's own vocabulary
  (`SessionPane.-done-unseen`, `.-attention`, `.-staged`). The header
  shows the **OR** over its leaves, so a tab whose corner pane finished
  still reads as "something happened in here" from the strip.
- An unfocused visible pane keeps rendering: pinned by mounting a block
  into a background leaf and asserting its PAINTED height and width, not
  its presence in the DOM.

### Dividers

- **Inside one pane the status bar IS the divider.** `SessionPane.compose`
  yields `VerticalScroll(#block-list)`, then `StatusBar`, then the popups,
  then `PromptInput` — the status line is literally the boundary. **Ctrl+Up**
  grows the transcript, **Ctrl+Down** grows the prompt area, and it works in
  a single-leaf tab with no splits at all, which is the case that provoked
  the request (reviewing 166 staged proposals in a surface too short for
  them). No "selected divider", no focus rule: the handle is always-present
  furniture.
- **The keys were re-verified free against the binding table as it
  resolves today**, not against the spec's 2026-08-25 check — the set
  changed in 0.85.0, when Ctrl+C was freed for terminal copy and popped
  out of Textual's own `system=True` binding. `tests/test_split_keys.py`
  asserts `ctrl+up`/`ctrl+down` resolve to exactly one action each, that
  both carry `priority=True` (the prompt is a `TextArea` and binds them to
  cursor movement of its own), and that the twelve keys this release
  claims do not intersect the ten it inherited.
- Dragging works too: `StatusBar.on_mouse_down/move/up` captures the
  mouse and moves the same divider, same sign convention as the keys.
  Requested in exactly those words — *"we should be able to drag the status
  line and resize the belief browser and input line"*.
- The position is a RATIO of the pane's height (`SessionPane.prompt_ratio`,
  `_apply_prompt_ratio`, re-applied on `on_resize`), never a row count, so
  it survives a terminal resize and a restore into a different window.
  `PromptInput.pinned_rows` is what it writes; `0.0` means "nobody has
  moved it" and restores to the content-driven auto height DOXA has always
  had.
- Floors, enforced rather than documented: `MIN_PROMPT_ROWS = 1` (a resize
  must never leave the input line too small to type into — the one region
  whose collapse makes DOXA unusable rather than awkward) and
  `MIN_TRANSCRIPT_ROWS = 3`. The ceiling arithmetic subtracts the status
  bar, the prompt's round border AND its one-row bottom margin
  (`#prompt-input { margin: 0 1 1 1 }`); leaving the margin out is exactly
  how a "3-row floor" renders as a 2-row transcript, which it did once.
- **The divider BETWEEN leaves got its own gesture, not an overload.** The
  spec's instruction is that Ctrl+Up/Down cannot mean two things: they act
  on the focused leaf's own status bar, and `alt+↑/↓/←/→`
  (`DoxaApp.grow_pane_towards`) moves the boundary between the focused
  pane and its neighbour, in steps of `DIVIDER_STEP = 0.03` down to
  `SplitBox.MIN_WEIGHT = 0.15`. Both write the tab set: a drag is a state
  change with no keystroke behind it and must survive restore like any
  other layout state.

### Persistence round-trips both ways

- The tree serialises into the `layout` node 0.32.0 reserved and left
  empty for fifty-seven releases — *"the day a split tree does exist the
  record grows a `{"kind": "split", …}` node in the same slot instead of
  needing a format version and a migration"*. This is that day, as
  `layout.trees`: one tree per TAB, in tab order.
- **`layout.kind` stays `"tabs"`, deliberately.** Writing `"split"` there
  would be read by every DOXA from 0.32.0 to 0.88.0 as "nothing this
  version can lay out" — correct, and it would cost the user every tab
  they had. The flat top-level `tabs` list stays authoritative and now
  carries every leaf of every tree in layout order, so a record written
  here still restores under an older DOXA as N ordinary tabs. That
  unknown-kind branch is unchanged and now pinned by a test of its own.
- **The absence of the key is the migration.** A record written by any
  DOXA from 0.23.0 to 0.88.0 has no `trees`, and `tabsets._layout_trees`
  reads it as one single-leaf tree per saved tab — which was true, because
  splits did not exist. No version field, no upgrade step. A malformed
  tree falls back the same way, per tab, rather than discarding the record.
- `doxa.cli` passes `resolved.trees` through as `DoxaApp(restore_layout=…)`;
  `_restore_trees_in_order` prunes each tree to the sessions that actually
  came back (`layout.prune` collapses splits that lose all but one child
  and re-normalises the survivors' weights), and `split.build` +
  `layout.rebuild_slots` put it back in the same widget shape the
  interactive gesture would have produced — so a restored pane keeps the
  slot allowance a never-saved one has and a restored layout is not a dead
  end.
- `_initial_active_tab_id` maps a saved session onto the tab that will
  HOLD it (its tree's first leaf). Without that indirection `initial=`
  named a tab that does not exist and Textual's `ContentSwitcher` hung
  waiting for it — measured as a Pilot timeout before a single assertion
  ran, not reasoned about. `_activate_initial_tab` then focuses the saved
  LEAF by its derived id, because "restore the saved active tab"
  under-specifies which of three panes in one tab gets the keyboard —
  the same defect the saved active TAB had from 0.23.0 to 0.32.0, one
  level down.
- **The restore test uses three leaves in one tab, saved active in the
  middle.** The old two-tab test passed only because the saved tab
  happened to be last, and a two-leaf layout hides the identical error.

### Tests

- **58 new tests**, in four files: `tests/test_layout_tree.py` (20, the
  model with no widget in sight), `tests/test_split_keys.py` (6, the
  binding table as it actually resolves), `tests/test_split_panes.py` (21,
  rendering/focus/marks/closing/refusals/dividers against a real Pilot),
  `tests/test_split_persistence.py` (11, both directions of the record).
- Every structural claim is paired with a rendered rectangle — the 0.28.0
  lesson, which the spec restates: a split that "exists" in the widget
  tree and paints nothing is the invisible-button defect again. The 2×2
  test asserts the four panes' real x/y/width/height before it presses a
  single direction key.
- The suite polls for the SETTLED state after a focus move. `Widget.focus()`
  is deferred in Textual 5.3 (it schedules `screen.set_focus` with
  `call_later`), so every focus move lands one message-pump turn after the
  handler returns.

### What this release does NOT cover

- **The divider between leaves is keyboard-only.** Alt+arrow moves it; there
  is no mouse drag on a boundary between two panes, because there is no
  widget on that boundary to hang a `MouseDown` on and inventing one is a
  larger change than this release earns. The IN-PANE divider (the status
  bar) drags with the mouse, which is the case that was actually reported.
- **Interactive depth is capped at two splits per pane** (`SPLIT_SLOTS`),
  and the shapes reachable through the UI are owner-first: every split node
  has the pane that was split as its first child. The model reads any tree
  `layout.from_json` accepts; a hand-written record nested deeper than the
  allowance degrades into the innermost box rather than being rejected.
- **A split leaf does not get its own tab-strip label.** One header, several
  panes: the tab is named by its first leaf, and the palette's tab section
  is where two sessions in one tab are told apart by id.
- **Archived (read-only) tabs cannot participate in a split.** An
  `ArchivedSessionTab` is still a `TabPane` of its own; only live sessions
  are leaves. Same for subagent transcript tabs.
- **Terminal-resize degradation is Textual's**, not a policy of ours: `fr`
  weights shrink with the surface and the tree is never rewritten, so
  enlarging the terminal restores the layout — but there is no explicit
  "below the total minimum, do X" behaviour beyond that.
- Floating windows (item W) and detaching a pane to its own terminal stay
  out of scope, as the spec says.

## 0.90.0 — 2026-08-29

**Reported: "plugin calls don't return anything, e.g. `/lore:pending
--cluster` does not work. Nothing happens. No error message."** Confirmed
against `SessionPane.on_prompt_submitted` (`doxa/session/pane.py`): a
`/`-prefixed line `commands.lookup()` did not recognize fell straight
through to `_run_turn` with no further check — correct for `/compact` and
every adopted plugin row (deliberately `passthrough`, and the literal
point of that convention) but wrong for everything else, because the CLI's
own local-command parser finds nothing staged for an unknown name and
answers with total silence: no DOXA error, no CLI text, no visible failure
of any kind. `/lore:pending` specifically is not missing by accident — the
LORE plugin is refused BY DESIGN (`claude_plugins.BLOCKLIST`, v0.74.0,
since `lore_core` already runs in-process inside DOXA) — but a typo
(`/setings`) or any command from an un-adopted plugin vanished the exact
same way.

- New **`doxa.commands.is_reachable`/`unreachable_message`**: the one new
  check between "not a DOXA registry row" and "ship it to the CLI as a
  turn". `is_reachable` reuses `names()` — the SAME membership the
  prompt's autocomplete dropdown and the Ctrl+P palette already compute —
  so `/compact` and every currently-adopted plugin row (`_plugin_rows`)
  are provably untouched; only a `/`-line neither surface would ever offer
  gets stopped. Deliberately not a hardcoded allowlist of CLI-accepted
  commands: that set moves with the CLI version, the operator's own
  `~/.claude/commands`, and whatever plugins happen to be staged, so a
  static list would be wrong the day it shipped.
- Three message shapes out of `unreachable_message`, all mounted as a
  `SystemBlock` through a new `SessionPane._run_unreachable`
  (`doxa/session/pane.py`) — the pane's own `_system()`, the same voice
  `_run_command`'s existing `"unknown command: {name}"` fallback already
  uses: (1) **blocked plugin** — the name before `:` matches a
  `claude_plugins.BLOCKLIST` entry (read from there, not re-hardcoded);
  states the reason (`BLOCKLIST_REASON`'s first clause) and points at
  DOXA's own equivalent, `/beliefs` and `/pending` for `lore`; (2)
  **near miss** — `difflib.get_close_matches` against `names()` at cutoff
  `0.72`, one suggestion (`/setings` → "did you mean /settings?"),
  nothing offered on a wide miss; (3) **generic unknown** — states plainly
  that this may still be a genuine CLI-native command or an un-adopted
  plugin's command, since a client-side check cannot tell those apart from
  a typo without the allowlist above ruled out.
- Tests: `tests/test_slash_guard.py`, 14 new — 12 fail against the
  pre-fix code (confirmed by temporarily reverting the two source files
  and re-running); the other 2 pin `/compact` and an adopted plugin
  command as negative controls and pass unmodified both before and after,
  proving passthrough survives untouched.
- Not covered: a hand-authored command under the operator's own
  `~/.claude/commands` (not a plugin, not a skill) stays invisible to this
  check exactly as before — `doxa.cli_isolation` never carries that
  directory into the isolated CLI at all, so such a command already failed
  silently ahead of this fix and still does, just without even a DOXA
  message, since the guard has no read path to a file it was never
  offered.

## 0.88.0 — 2026-08-29

**The one gallery item 0.87.0's regeneration pass flagged and could not
capture: the peers chip roster.** 0.79.0 replaced the peers chip's old
`/sessions` shortcut with a real dropdown (`PaneChipsMixin.
open_peers_picker`), but the roster only exists once the chip is
clicked — a still cannot show click-time behaviour, so it stayed
undocumented until now.

- New scene **`peers`** (`scripts/record_gif.py`, `assets/shots/peers.gif`):
  a real click on the status bar's `peers 3 (1⌁)` chip (three fabricated
  peers via new `_demo_peers()`, built on `scripts/screenshot._peer` the
  same way `_hero_engine` already does) opens the shared `ChipPicker`,
  showing each peer's first-prompt title and running token total —
  `86k tok`, `142k tok` — before arrowing to the third peer, which has not
  finished a turn yet: **`tok —`**, never `0 tok`, the exact distinction
  `PeerInfo.usage_tokens`'s own docstring calls out and 0.79.0 fixed a bug
  around. The note row states the figures are self-reported and up to
  `HEARTBEAT_SECS` (15s) stale. 4 frames, 3068×1734 (250×69 cells), 392 KiB.
- The click itself is driven by the exact rendered chip text
  (`"peers 3 (1⌁)"`), not a bare `"peers"` substring — measured on this
  branch's own worktree, whose `chore/peers-gif` name put a second, earlier
  `"peers"` inside the status bar's own git-identity text and landed the
  loose match on the git chip instead, opening the wrong picker with zero
  rows. Fixed before it could do the same on any future `peers`-named
  branch or checkout directory.
- Row selection is not exercised in the scene: selecting a peer attaches
  through `DoxaApp._cmd_attach` over a real Unix socket, and the fabricated
  peers' socket paths do not exist — arrowing to highlight the roster's
  three rows is the honest stopping point for a deterministic capture.
- Wired into `README.md`'s gallery with alt text describing the roster,
  the token figures, the detached marker and the staleness note — the
  condition `beliefs-browser.png` needed (unreferenced, for 18 releases)
  before this GIF is exempt from it.
- Version bumped to **0.88.0 before capture**, not after — the same
  ordering gap that left the whole gallery reading `DOXA 0.67.0` for
  twenty releases.
- Everything else 0.87.0 regenerated (24 stills, the other 11 GIFs) is
  unchanged here.

## 0.87.0 — 2026-08-29

**The gallery is regenerated against the running product.** Every asset in
`assets/shots/` was last captured at **0.67.0** and had been rendering
`DOXA 0.67.0` in its identity block ever since — twenty releases, so the
version string alone made the whole set false independent of any feature
drift. 24 stills (12 PNG/SVG pairs) and 11 GIFs, all at the unchanged
shared geometry of **3068×1734** (250×69 cells), verified per file rather
than assumed.

**The in-flight marker, which 0.78.0 named as stale and left that way.**

- `markdown-stream.gif`, `tool-calls.gif` and `reasoning.gif` were baking
  **`⋯ thinking`** — not the old spinner, but `ThinkingMarker`'s un-armed
  *construction* text, a state no real turn is ever in for a single frame.
  Every GIF scene mounts its `TurnBlock` directly (`_mount_bare_turn`,
  which is what buys a scene exact control over ordering) and so never
  reached `ThinkingMarker.start()`, the method a real turn calls from
  `_run_turn` and the two `_peer_pump` branches.
- New **`record_gif._marker()`** paints the marker at a CHOSEN elapsed
  second by assignment rather than by arming the real one-second `Timer` —
  this file's own determinism rule, not a shortcut around it, since a live
  timer would put a different count in the GIF on every run. The values
  are the ones the widget computes for itself (`_elapsed()` floors
  `monotonic() - _started_at`; `_tick` advances one frame per second from
  zero), so second N renders the frame a real turn genuinely shows at
  second N.
- `tool-calls` now runs `⠹ generating (2s)` → `working` **5s → 9s → 14s →
  17s → 19s`**, and the climb happens across the stretch between a
  `tool_call` and its `tool_result` where no delta arrives at all — the
  dead air that froze the pre-0.78.0 marker. `reasoning` shows both
  reversals that release made: the opening `⠋ thinking (0s)` (0.56.0 kept
  it frozen) and the `reasoning` → `generating` phase flip (0.25.0 hid the
  marker there instead). Verified frame by frame off the rendered SVGs.
- All three now END on a frame with no marker at all: `mark_done` calls
  `hide_thinking`, so a reader is left looking at a finished turn rather
  than a spinner stopped mid-flight.

**Two surfaces the gallery never had.**

- New scene **`context`**: `/context` as 0.81.0 redrew it — a fixed 10×20
  grid of 200 draughts cells at 0.5% each, model and headline beside the
  top rows, per-category legend beside the lower ones, source sections
  below. No asset had ever shown this: 0.75.0's proportional bar shipped
  and was retired inside four releases without a scene, so this is a first
  capture rather than a refresh. Captured in the DEFAULT glyph tier —
  `DOXA_CONTEXT_GRID` is left unset so the shot reads what a fresh install
  renders.
- New scene **`beliefs-picker`**: the beliefs surface that actually
  exists. Shows 0.77.0's fixed 50-column row prefix (`PICKER_STAMP_COL`
  15 / `PICKER_STATUS_COL` 28 / `PICKER_AGE_COL` 7) and 0.86.0's **`g
  graph`** action beside `y`/`c`/`s`/`r`. `belief_count` is overridden on
  the engine INSTANCE, not in `tests/fakes.py`: the fake hardcodes 3,
  which would put a `3 beliefs` chip above a picker listing seven, and a
  screenshot may not move a number the suite is written against.

**`beliefs-browser.png`/`.svg` are deleted, not regenerated.** They were
not stale — they were **wrong**: they showed the standalone beliefs
BROWSER TAB, a whole surface 0.69.0 removed and 0.73.0 finished removing,
down to the `lore_core 0.36.0 (plugin)` fixture string and an `a`/`r`
action vocabulary that no longer exists. Their generating scene was
deleted with the feature, which is why the two files sat in `assets/shots/`
for eighteen releases with nothing in either script able to refresh them.
Neither `README.md` nor `docs/` ever referenced either path.

**README alt text, checked against the images rather than the captions.**

- `hero`: the status-bar description listed model, repo/branch, headroom,
  ctx%, belief count, session handle and peers — and omitted the
  **permission-mode chip**, which 0.50.0 put first on the row and which is
  never hidden. The one chip a reader sees before any other was the one
  the description skipped.
- `markdown-stream`/`tool-calls`: both now carry the ticker, and in
  `tool-calls` it is half the point of the sequence.
- Both new stills are placed in the gallery. Being unreferenced is exactly
  the condition that let `beliefs-browser` rot unnoticed: an asset nothing
  points at is an asset nothing re-checks.

**Already correct, checked rather than assumed.** `/help` carries no
Ctrl+C (0.85.0) and no asset ever showed one; the picker column constants
were already fixed-width in code since 0.67.0/0.77.0, so the reported
"proposals view should have fixed columns" needed a fresh capture, not a
code change; and the shared 250×69 geometry 0.67.0 established was still
uniform across every file on disk, so "the screenshots have different
resolutions" was a defect the asset set had already outgrown — with
`beliefs-browser` the sole remaining odd one out, now gone.

## 0.86.0 — 2026-08-28

**The beliefs picker gains a graph view.** 0.84.0 gave the MODEL the belief
graph (`lore_belief_neighbours`, the `[BELIEF GRAPH]` block) and left the
operator with nothing to look at.

- New row action **`g`** on every beliefs-picker row, beside `y`/`c`/`s`/`r`
  (**`BELIEF_GRAPH_ROW_ACTION`**, `doxa/session/chips.py`): that belief's
  graph neighbourhood, reached by the bare letter or a click on the row's
  own action span, like every other inline action since v0.67.0.
- New setting **`graph_view`** (`DOXA_GRAPH_VIEW`, default `browser`) picks
  the rendering. `ascii` folds `lore_core.beliefs.format_edges`' own block
  in under the row; `browser` writes `lore_core.graph`'s pan/zoom mermaid
  page and opens it.
- New **`ChipPicker.expand_rows`** (`doxa/ui/dialogs.py`) inserts the ascii
  block as real rows beneath the belief — the SAME fold `Right` already
  gives the evidence trail, not a second one. One expansion slot per row:
  `g` replaces an open evidence trail, `Left` folds away whichever shows.
  `Right` still no-ops over an open expansion, unchanged.
- **`g` writes nothing**, so it is composed onto the action list separately
  from the four write verbs and survives a session whose `lore_core` cannot
  record an outcome — which loses `y`/`c`/`s`/`r` entirely (v0.69.0's "the
  control is gone, not merely inert"). Its label is `g graph`, 7 columns,
  shorter than two of the four verbs beside it: every label is a fixed
  column out of the same width budget `ChipPicker._action_reserve` trims
  the claim text against.
- New module **`doxa/beliefgraph.py`**; every store read and the browser
  launch go through `asyncio.to_thread`. `lore_core.graph.adjacency` builds
  the WHOLE store's adjacency before `khop` walks two steps of it, and
  `webbrowser.open` can fork — either on the event loop is a frozen
  terminal.

**Per belief, and there is deliberately no whole-graph view.** Measured,
not preferred.

- A whole-graph view filtered to asserted relations was built first and
  fragments: **63 edges over 104 beliefs resolved to 44 disconnected
  clusters**, which mermaid stacks vertically — **1188×13814 pixels**,
  aspect 0.09, fitting on screen at 5% and unreadable at every zoom above
  it. `khop` from one belief is connected by construction.
- **Hidden at zero**, and this is very nearly the only case: on the live
  store this was built against, **745 of 799 active beliefs (93%)** have no
  row in `belief_edges` at all, and **776 of 799 (97%)** have no *asserted*
  one — the store holds **121 structural edges against 13 derived**. Those
  say `no relations recorded` rather than opening an empty page.
- **One gate for both renderings**: `format_edges`' emptiness, read once
  and branched on (**`beliefgraph.edge_block`**), so `ascii` and `browser`
  can never disagree about whether a belief has anything to show.
  `co_derived` is a read-time projection and never counts as recorded.

**Where the page lands, and how a browser reaches it.**

- **`$DOXA_HOME/graphs`** (`~/.doxa/graphs`, 0700), deliberately not
  `LORE_ROOT`: the belief store is shared with the Claude Code LORE plugin,
  and a rendered artifact of DOXA's UI is not memory. The path is printed
  into the transcript on every open, whether or not a browser launched, so
  a headless box or an SSH session still ends up with a file to `scp`.
- **Served over loopback HTTP, not handed over as `file://`.** LORE's page
  imports mermaid from `cdn.jsdelivr.net` as an ES module, and a `file://`
  document is a null origin some browsers refuse that fetch from — a page
  that loads, throws nothing and draws nothing. **`beliefgraph.page_url`**
  starts a 127.0.0.1-only server on an ephemeral port over that one
  directory, lazily, and falls back to `file://` when it cannot; LORE's
  template explains the null-origin case in the page itself either way.
- **That server is token-gated.** `~/.doxa/graphs` is 0700, so a co-tenant
  cannot read a page off disk — but an HTTP server on 127.0.0.1 answers any
  *local* process regardless of whose it is, and a port is not a secret.
  The page carries belief claims in full, so every request needs a
  per-process token (`?k=`, in memory, never written to disk); anything
  else gets 404. The server's root is part of its cache key, so a moved
  `DOXA_HOME` restarts it rather than 404ing a page that is on disk.

**DOXA draws none of it.** No mermaid source, no edge formatting, no
traversal: `doxa/beliefgraph.py` selects a belief, reads the setting, and
puts LORE's output where a reader can see it.

- **`beliefgraph.graph_state(mode)`** measures capability off the API, PER
  RENDERING, never off a version — `ascii` needs only `format_edges`, so a
  checkout missing `mermaid_source` loses the browser half and keeps the
  TUI one.
- A version comparison would be wrong here specifically: `doxa/
  _lore_bootstrap.py` prefers a plugin **checkout** over the pinned wheel,
  so the loaded `lore_core` can be OLDER than `pyproject.toml`'s pin. A
  stale checkout is reported in the transcript by the missing function's
  name rather than raised, and `/about`'s `lore from` row says which copy
  that is.

**Dependency.**

- `lore-core` moves 0.45.0 → **0.48.2**, required rather than cosmetic:
  0.47.0's page loaded mermaid cleanly and drew nothing
  (`initialize({startOnLoad: true})` hooks `DOMContentLoaded`, which a
  dynamic `import()` always resolves after — fixed in 0.47.1 with an
  explicit `run()`), and nothing before 0.48.1 carries the cluster cap.
- Full suite: **1491 passed** (1469 on 0.85.0, plus the 22 new tests in
  `tests/test_belief_graph_view.py` — every one verified failing against
  pre-change code).

## 0.85.0 — 2026-08-28

**Session lifecycle and keybindings, four independent defects from live use.**

- Fix **desktop notification fires on turn-done, not on input-required**:
  `doxa.notify.notify_turn_done` and its call site
  (`SessionPane._on_turn_done_status`) removed outright — a finished
  response is never notification-worthy, only a turn genuinely blocked on
  the user is (`notify_needs_input`, unchanged wiring). `notify_needs_input`
  now defaults **OFF** (`kind="bool"` in `config.SETTINGS`, was `bool_on`);
  `doxa.notify.should_fire` reads each trigger's default straight off the
  registry (`_trigger_default`) instead of hardcoding one, so the setting
  and its runtime default can never drift apart.
- Fix **Ctrl+C stole terminal copy**: the app-level quit-detach/quit-stop
  double-press binding is gone (`action_ctrl_c_quit`, `CTRL_C_DOUBLE_SECS`,
  `_ctrl_c_timer` removed). `DoxaApp.__init__` also explicitly pops
  Textual's own default `ctrl+c` binding (`App.BINDINGS` carries one,
  `system=True`) out of `self._bindings` — a same-key override alone only
  shadows it, per `DOMNode._merge_bindings`. Ctrl+Q (per tab) and the
  command palette ("Quit: detach" / "Quit: stop session") cover what
  Ctrl+C used to; `/help` no longer mentions it.
- Fix **closing the last tab didn't start the next launch fresh**:
  `DoxaApp._close_pane` now excludes the closing session from the
  persisted restore set (`_persist_tabset`'s new `exclude_session_id`)
  when it is the LAST open tab, on either key — previously both Ctrl+Q
  (ended) and Ctrl+W (detached) sessions stayed in the record even with
  nothing else open, so the next launch came back to an archived
  read-only tab (Ctrl+Q) or a silently auto-reattached live one (Ctrl+W)
  instead of starting clean. Ctrl+W's session still runs and is
  reattachable by name (`/attach`, the peers chip) against the live
  daemon registry; a toast on close now names the tab and says so, where
  before it detached in total silence.
- Fix **a background `AssertionError` on that same last-tab close**,
  found while hardening the fix above: `SessionPane._peer_pump` asserted
  `self.engine is not None` right after awaiting `_engine_ready`, but the
  worker is created (`run_worker`, in `on_mount`) alongside `_boot` with
  no guaranteed ordering between them — `_boot` sets `_session_id` (what
  every close-path test waits on) *before* `_engine_ready` (a
  naming-cache lookup sits between the two). `detach()`/`stop()` clear
  `self.engine` immediately without cancelling this worker, so a close
  landing in that window let the assertion fire after `_engine_ready`
  released it, surfacing as a visible in-app error block on an ordinary
  Ctrl+Q/Ctrl+W. Pre-existing on both close paths, not introduced by the
  fix above — now a plain early return: nothing left to pump.
- Fix **Ctrl+Left/Right skipped read-only tabs**: `_cycle_tab` walked
  `panes()` (session tabs only); a finished archived tab or an open
  subagent transcript sitting right in the strip was never reachable by
  keyboard. New `_cyclable_tabs()` (every `TabPane`, strip order) fixes
  the walk; `_focus_tab` also now gives the two read-only tab kinds their
  own `.scroll` focus target, closing a Textual `AUTO_FOCUS` gap
  (`App.AUTO_FOCUS = "*"`, unscoped by which tab is visible) that
  silently reverted the cycle back to a SessionPane's prompt in a
  different, hidden tab.

Full suite: **1469 passed**.

## 0.84.0 — 2026-08-28

**The belief graph reaches the model.** LORE has carried typed relations
between beliefs since 0.41.0; nothing in DOXA could read them.

- **`lore_belief_show` gains its edges** (`lore_core.beliefs.belief_edges`):
  verb, direction, the other belief's id/claim/status, who asserted it
  (`source`), and the distinct-session `support` count. `"edges": []` for a
  belief with none — an absent block would be indistinguishable from a
  query that failed.
- New operator **`lore_belief_neighbours`**: one traversal tool, not five.
  `belief_id` + `hops` (≤2) returns the k-hop neighbourhood; adding `to_id`
  switches to the most-confident path between two beliefs
  (`lore_core.graph.khop`/`best_path`). Capped at `BELIEF_NEIGHBOUR_LIMIT`
  (20, `doxa/events.py`), truncating visibly rather than silently.
- **Structure earns no authority.** Every belief either tool returns —
  seed, neighbour, or a node on a path — carries its OWN `citation_status`
  (`steer`/`cite_only`), computed independently the way
  `lore_core.dialectic.cmd_consult` splits STEER from CITE ONLY (≥3
  outcome-ledger rows). A STEER belief never lends its status to a
  CITE-only neighbour, or the reverse.
- **Path confidence is the product over hops** (`best_path`'s contract —
  Dijkstra on `-log(weight)`), surfaced beside `hop_count` on every result,
  so a long chain reads as weak rather than as strong as its best hop.
- `co_derived` relations — projected from `belief_evidence` at read time,
  never a stored row — are labeled `"projected": true` wherever they
  appear, never presented as asserted.

**The session is told the graph exists.** A tool the model never thinks to
call is close to no tool: the LORE snapshot's retrieval ladder names the
snapshot, file map, belief store and session index, and stops there.

- **`SessionEngine._graph_awareness_block`** appends a `[BELIEF GRAPH]`
  block after the LORE snapshot and `[SESSION WORKTREE]`, naming the five
  verbs, pointing at `lore_belief_neighbours`, and stating that
  reachability is not authority.
- **Hidden at zero**: emitted only when the store carries traversable
  edges. A store whose only relations are projected `co_derived` gets
  nothing — a co-derived cluster is one session's beliefs joined pairwise,
  and pointing the agent at it teaches it to read coincidence as structure.
- `/context` reports it as `graph_awareness_chars`.

**Graph-backed act-time context, off by default.** New setting
`graph_context` (`DOXA_GRAPH_CONTEXT`).

- **`SessionEngine._graph_context_block`** calls LORE's OWN builder
  (`lore_core.graph.context_candidates`/`render_context_block`) rather than
  a second ranking implementation. A bespoke one-hop follow on
  `_consult_note` was built and then removed: LORE 0.44.0/0.45.0 already
  ships the ranked, budgeted, calibrated-first version, and a second
  implementation over the same store could only drift from it.
- A stage SEPARATE from the plain-FTS consult note (`_consult_note`/
  `consult_floor`); the two toggle independently. Gated the two ways LORE's
  own `LORE_GRAPH_CONTEXT` hook is: `graph_context_enabled()` AND
  `stage_disabled("beliefs")`.
- `/context` reports `graph_context_chars` as the LAST-INJECTED size, not a
  session constant — unlike `lore_snapshot_chars`/`worktree_notice_chars`,
  this rides the per-turn `additionalContext` path.

**Dependency and a test whose premise expired.**

- `lore-core` moves 0.42.1 → **0.45.0**: 0.43.0 mermaid/browser graph view,
  0.44.0 graph-backed context, 0.45.0 learned-skills tier. DOXA surfaces
  neither the mermaid view nor the skills tier in this release.
- `test_a_worktree_session_still_finds_its_project_memory` asserted a
  linked worktree gets a DIFFERENT project slug than its parent repo — the
  defect DOXA worked around. lore-core 0.41.0 resolves through
  `--git-common-dir` and the defect is gone at the source, so the
  assertion is inverted and kept as a regression pin: a future revert to
  `--show-toplevel` would silently cost every worktree session its project
  memory.
- Full suite: **1465 passed**.

## 0.82.0 — 2026-08-28
- `lore-core` moves 0.39.0 → 0.42.1 (`pyproject.toml`, `uv.lock`). No DOXA code changed: nothing DOXA imports (`engine.py`'s `beliefs`/`memory`/`deriver`/`pending`/`gate`/`context`/`store`, `peers.py`'s `scrub`, `operators.py`'s belief search/show) touches the new binding layer (`belief_edges`, `lore_core.graph`) or the deriver's `relates` schema field — those are internal to LORE's own worker process and CLI. Full suite: 1446 passed.
- One of the three releases in the jump is not inert for DOXA even so: 0.41.0's `project_identity_root` now resolves a linked worktree through `--git-common-dir` instead of minting a project slug per checkout. DOXA runs every session in its own git worktree by default, so every session on a repo was previously deriving beliefs under a *different* project slug than the repo itself — this pin silently reunifies them onto the parent repo's store.
- README: documents that the belief store now carries typed edges between beliefs (`depends_on`, `specializes`, `explains`, `contradicts`, `applies_when`), derived and support-counted the same way beliefs are, with a path's confidence the product of its hops — structure that earns no authority, so a belief reached through the graph is still CITE-only unless it earned STEER on its own. Stated plainly as not yet surfaced in DOXA's interface (no chip, picker or operator reads `belief_edges` today).

## 0.81.0 — 2026-08-27
- `/context` redrawn as a fixed 10×20 grid of 200 cells (0.5% each), replacing 0.75.0's proportional bar. Model and headline beside the top rows, per-category legend beside the lower rows.
- New **`context_grid_cells`**/**`context_grid_text`** (`doxa/ui/labels.py`): fixed geometry, drawn at 200 cells or not at all. Cell counts floor each category's cumulative share, so a category below one whole cell draws zero.
- Two cell styles, one geometry: draughts glyphs (⛀ ⛁ ⛶) by default, `[#]`/`[ ]` behind new setting `context_grid` (`DOXA_CONTEXT_GRID=ascii`). 3 columns wide either way, so switching never moves the layout.
- Category colour keyed by name (**`CONTEXT_GRID_CATEGORY_COLORS`**), not list position.
- New **`context_sources_text`**: MCP tools by server, agents, adopted-plugin skills; hidden at zero. `agents` added to `doxa.engine.context_breakdown`; skills is a bare count from `adopted_skill_summary`, gated on `adopt_plugins`.
- Heading is "Usage by category", not "Estimated usage by category".
- Tests: `tests/test_context.py` bar tests rewritten for the grid.

## 0.80.0 — 2026-08-27
- A session running in its own git worktree is now told so: **`SessionEngine._build_options`** appends a `[SESSION WORKTREE]` block after the LORE snapshot, naming the session's branch, base ref, main repo root and `FINALIZE_RULE`. Fixes agents pushing their private branch upstream, switching to `main`, or hunting for their own base.
- Facts come from **`doxa.worktrees.read_meta`**'s sidecar; a missing or unreadable sidecar appends nothing and the prompt is byte-identical to pre-0.80.0.
- `/context` reports the block separately as `worktree_notice_chars`.
- New constant **`doxa.worktrees.FINALIZE_RULE`**: the clean/ahead rule `finalize()` applies, read by both the prompt block and its tests.

## 0.79.0 — 2026-08-27
- The peers chip opens a roster instead of shortcutting to `/sessions`: **`ChipPicker`** lists every live peer on this repo with what it's working on and tokens consumed. Selecting a row reuses `DoxaApp._cmd_attach`; the current session is a no-op and a peer already open switches to its tab.
- New **`PeerInfo.usage_tokens`** (`doxa/peers.py`): input + output + cache read + cache create, the sum `/usage` prints. Flushed on the existing 15s heartbeat, so a count can be up to `HEARTBEAT_SECS` stale — stated in the picker's note row. `None` means unknown, never `0 tok`.
- Fix **`PeerInfo.title`**: fell back to the cwd basename at every call site through 0.78.0 because `PeerHost` was never wired to update it. **`PeerHost.set_title()`** now writes on the first turn, so `/peers`, the sessions picker and the peers picker all show the real first-prompt excerpt (capped 72 chars).
- `docs/plans/peer-publishing.md` corrected to ship `usage_tokens`; cost-so-far and context% stay local-only.

## 0.78.0 — 2026-08-27
- Fix **spinner freeze during dead air**: the in-flight marker only advanced on an SSE delta, so a 30s `Bash`, a slow `WebFetch` or a silent subagent froze it on the last frame.
- **`ThinkingMarker`** arms a per-second `Timer` (`start()`/`stop()`) for the life of a turn alongside its delta-driven `advance()`, showing elapsed since turn start — `⠋ working (14s)`.
- Armed at the three turn-start paths (`_run_turn`, the `turn_started` and orphaned-turn branches); cancelled from `hide_thinking()` on every completion path and by Textual's message-pump teardown.
- Amends the no-third-timer rule (0.56.0/0.28.0): the rule's target was idle CPU, and a timer that exists only during a turn spends none.
- `assets/shots/markdown-stream.gif` and `tool-calls.gif` show the old marker and are stale; not regenerated.
- Tests: `test_transcript_density.py`, `test_chrome.py` assert armed-during, gone-after, none-while-idle.

## 0.77.0 — 2026-08-27
- Fix **staged proposal lost its timestamp** on the pending picker: `_fmt_pending_row` read only the record's `created` field, so a proposal staged before that field existed rendered stamp and age blank.
- `PICKER_STAMP_COL` (15), `PICKER_STATUS_COL` (28), `PICKER_AGE_COL` (7) re-checked against both row types and `_fmt_age`'s real ceiling (`23h59m`, 6 columns); all already at their maxima, unchanged.
- New test: a belief row and a pending row rendered together start their text columns at the same offset.

## 0.76.0 — 2026-08-26
- Adopted Claude Code plugin commands now reach DOXA's `/` autocomplete, the Ctrl+P palette and `/help`.
- Fix: a user's own prompt in the turn fold's title was cut with a bare `[:70]` — no word boundary, no ellipsis, and unrelated to the terminal's actual width.

## 0.75.0 — 2026-08-26
- Fix `_boot` queried `#block-list` on a pane Textual had not composed, raising `NoMatches` from a background task and surfacing as a visible block during multi-pane restore.
- New **`context_bar_segments`**/**`context_bar_text`** (`doxa/ui/labels.py`): a proportional bar built from the same `categories` `context_breakdown_text` already reads.
- New **`doxa.ui.transcript.ContextBlock`** (a `SystemBlock` subclass) is what `/context` mounts, replacing a plain text block.
- `context_breakdown_text` untouched; every existing `/context` assertion passes unmodified.

## 0.74.0 — 2026-08-26
- New **`doxa/claude_plugins.py`**: discovers the operator's installed Claude Code plugins from `~/.claude/plugins/installed_plugins.json`, resolving each entry's `installPath`. Adopted per capability — commands, skills and agents are additive and inert until invoked; hooks and MCP servers execute unasked at session start and are refused unconditionally. A sanitized staged copy per plugin is passed as one `--plugin-dir` flag.
- The LORE plugin is blocked outright: `lore_core` already runs in-process, and LORE's commands shell out to a `bin/` path this module never stages.
- New setting `adopt_plugins` / `DOXA_ADOPT_PLUGINS`, default off.
- New `/plugins`: what was discovered, what is enabled in `~/.claude/settings.json`, what is adopted, and what is refused with the reason.
- New `/reload-plugins`: re-scans and re-stages without restarting; affects new sessions and tabs only.
- Plan: `docs/plans/plugins.md`.

## 0.73.0 — 2026-08-26
- Removed the standalone beliefs browser tab; `/beliefs` opens the chip picker instead. `_beliefs_tab` and its Ctrl+W/Ctrl+Q cases are gone.
- Evidence expands in place on the beliefs picker: `Right` fetches a belief's derivation trail and inserts it as rows beneath it, one per evidence event; `Left` folds them.
- Fix: the beliefs/proposals pickers' inline row actions (`y`/`c`/`s`/`r`, `a`/`r`) painted even when the loaded `lore_core` could not record the write, unlike the row's own sub-menu.
- Fix "underline runs past the word" on `approve`/`retract`: padding was `.ljust`ed inside the row's `[@click=...]` span, so a click just past the label armed it. Padding moved outside; armed labels shortened to their resting length (`⌫ RETRACT`, `✓ APPROVE`).
- Picker rows carry a column header (`date · status · age · text`), hidden under a typed filter.
- The prompt-as-filter debounces on both pickers, reusing `doxa.history.DEBOUNCE_SECS`.

## 0.72.0 — 2026-08-26
- Boot mark redrawn: grey ring dropped, triangle widened, ΔΟΞΑ spelled in full-block letters beside it in the same colour.
- Greek letters drawn from `█` rather than printed as Unicode — a monospace font's Greek coverage is not guaranteed.
- The Latin `DOXA` wordmark remains the fallback for terminals too narrow for the Greek word, and stands alone when the triangle does not fit.
- Row budget held at 9; Ο's sides narrowed to a single-cell stroke to match Δ and Α.
- Fix curated-memory chip (`mem u63% p39%`): the project half stayed absent after startup and never moved on a repo switch or resume — `PaneChipsMixin._lore_slug` resolved the slug from the pane's construction-time `cwd`.
- Fix: approving or rejecting a proposal, recording a belief outcome, or retracting a belief never refreshed the status bar, leaving the memory-fill, belief-count and proposal chips stale.

## 0.70.0 — 2026-08-26
- Fix a status refresh on a pane whose status bar had not mounted raising `NoMatches` from a background task, surfacing as a visible block.
- Fix the boot mark overflowing its column in CI: the transcript's scrollbar can appear after the banner's last resize without a `Resize` message following.
- Mark redrawn at nine rows with a real gap between ring and triangle; ring muted grey, triangle keeps the orange accent.
- The raster boot banner is gone — `logo.png` no longer draws on `kgp`/`sixel`; the drawn mark reads better even there.
- Fix `test_restore_tabs_open_in_saved_order_with_names_and_active_tab` failing in CI on a wrong active tab id.

## 0.67.0 — 2026-08-26
- Every asset in `assets/shots/` — 13 PNG/SVG pairs and 11 GIFs — renders at 3068×1734 (250×69 cells, 16:9 within 0.5%). Five different pixel sizes previously shipped side by side.
- Scenes with less content than the new frame now run alongside a real three-tab session mid-conversation rather than blank canvas.
- Fix `scripts/screenshot.py`/`scripts/record_gif.py` leaking real machine state into the gallery: `N proposals` and `mem u%p%` read off this machine's actual `lore_core` store.
- The beliefs and proposals pickers share one row shape; previously a `belief_stamp` join with no fixed columns and a `·`-joined proposal string that drifted with each field's length.
- Both pickers gained inline row actions — approve/reject on a proposal, confirmed/contradicted/stale/retract on a belief — via a click on the row's action span or the reserved letter.
- While a picker is open the prompt input filters its rows: typed text syncs live, Enter acts on the highlighted row, Escape closes and clears.
- `user`/`user-model` group headers carry LORE's channel tag: `user · stated` against `user-model · inferred`.

## 0.65.0 — 2026-08-26
- Fix `/peers` printing a peer's `title` and `cwd` raw — another process writes both, and the registry-read path was never scrubbed.
- All 11 PNG/SVG pairs and 10 GIFs re-captured. `banner-blocks` measured 1.51 against a 16:9 target and was fixed.
- Six status-bar scenes (`hero`, `trace`, `transparent`, `subagent-tracker`, `memory`, `sessions`, `image-support`) widened 172 → 250 columns.
- Three new pairs: `beliefs-browser`, `error-block`, and one more.
- Fix `beliefs-browser` rendering `lore_write_state_result`'s test fixture default (`lore_core 0.36.0 (package)`) into the image instead of calling `doxa.version.lore_core_version()`.

## 0.64.0 — 2026-08-26

- CHANGELOG entries rewritten to state what changed and why: 5139 lines to 947. Narrative, process accounts and quoted reports are gone.
- Reference material that only existed as changelog prose moves to `docs/manual.md` — tabs and keys, permission modes, worktrees and finalize, the daemon, status chips, LORE integration, commands and settings. Each claim checked against the source.
- README's "What you get" is short bullets linking into the manual; "How it works" follows it directly. The session walkthrough, permission-mode table and configuration reference move to the manual. 1076 lines to 310.

## 0.63.0 — 2026-08-26
- Relicensed from the DOXA Noncommercial License 1.0 to **AGPL-3.0-only**, dual with a commercial option.
- Fix `Ctrl+T` raising `NameError: name 'spawn_daemon' is not defined`, hanging the tab at "connecting…": 0.61.0 deferred that import and three call sites kept the bare name.
- CHANGELOG rewritten to state what changed and why: 5139 → 947 lines.
- New `docs/manual.md`: tab model and keys, permission modes, worktrees and finalize, the daemon, status chips, LORE integration and the review gate, commands, settings.
- README's "What you get" condensed to bullets linking into the manual.

## 0.62.0 — 2026-08-25
- `lore_core` moves 0.36.0 → 0.38.0. DOXA runs it in-process, so this changes what the terminal does.
- LORE 0.37.0 cuts the deriver's proposal ceiling 5 → 3 and states it is a ceiling, not a quota. Runs emitting exactly 5 were approved at 0.83% against 2.47%.
- LORE 0.38.0 separates the two user channels by a stated rule: the user said it → `user`, and a later session may act on it; you concluded it → `user-model`, which authorizes nothing.

## 0.61.0 — 2026-08-25
- `import doxa.app` no longer imports the Claude Agent SDK — 404 ms of its 546 ms (`mcp.types`'s pydantic models alone were 330 ms). Import drops to 168 ms; `doxa.client` 465 ms → 59 ms.
- New **`doxa/events.py`** holds the event vocabulary (`EngineEvent`, list caps, `PROTOCOL_VERSION`) with no SDK dependency; `doxa.engine` and `doxa.daemon` re-export it.
- `doxa.client`, `doxa.session.runtime` and `doxa.session.chips` import from `doxa.events`.
- `doxa.app` imports `SessionEngine` only inside its session factories and `doxa.cli` imports `spawn_daemon` at point of use, both via PEP 562 `__getattr__` so existing `monkeypatch.setattr` patches still work.
- Tests: `tests/test_import_cost.py` pins both directions in a subprocess.

## 0.60.0 — 2026-08-25
- Fix Ctrl+Q on a window's last tab wiping the saved tab set to `"tabs": []` — `_persist_tabset` excluded every stopped pane, correct before 0.56.0 pinned `ClaudeAgentOptions.session_id`.
- New `/attach [prefix]`: the in-app door to a live detached session, always opening a new tab, reusing the sessions-chip picker.
- Fix the sessions-chip attach: `_cmd_attach` swapped the active pane's engine in place instead of opening a tab and never set `_restore_transcript_wanted`.

## 0.59.0 — 2026-08-25
- Fix the startup banner drawing wider than its column with no resize correcting it: it fit to the terminal's raw width rather than the widget's content box.
- Fix `doxa launcher install` crashing on a machine without `desktop-file-utils`: the cache-refresh guard checked `shutil.which`, and the test stubbed `which` to answer for every name.
- Both defects were cases where the machine running the test decided the answer; CI was red on two shipped tags while the local suite (1307 tests) was green.

## 0.58.0 — 2026-08-25
- Fix launcher shortcuts pointing at the wrong DOXA: `Exec=doxa` resolved against the desktop session's PATH at click time, unrelated to the installing shell.
- New **`doxa/window.py`**: writes an OSC title-stack push/pop (`CSI 22;0t` / `23;0t`) around `DoxaApp.run()`, restoring the terminal's own title on quit, Ctrl+C or crash. Textual offers no API for a window title.
- Fix Ctrl+Q doing nothing on a read-only tab (`SubagentTranscriptTab`, `ArchivedSessionTab`, the beliefs browser): `_end_session` required `active_pane` to be a `SessionPane`.
- `bypassPermissions` removed from the mode cycle: the CLI refuses it unless the session launched with `--allow-dangerously-skip-permissions`, the only mode with a launch-time prerequisite.
- The startup mark is a ring around a triangle drawn only in `█` and spaces (7×13), superseding 0.55.0's quadrant-triangle glyph.

## 0.57.0 — 2026-08-25
- Staged LORE proposals get a status chip and picker; `/pending` was previously the only way in, and an operator could accumulate 175 unreviewed proposals with no visible signal.
- A `175 proposals` chip sits beside the belief count and memory fill, hidden at zero. Count and list both derive from one predicate, `engine.pending_visible`.
- Cached on the pending directory's mtime: 4.2 ms cold against 0.0062 ms warm.
- Proposals group by kind (memory/user, memory/project, filemap, belief, skill); selecting a row opens that proposal's approve/reject controls.
- Fix both pickers' "browse" doors reading "open the beliefs browser" when they led to different halves.
- Fix picker rows dropping the year from a timestamp, which moved the claim column row to row; rows now always show `YY-MM-DD HH:MM`, sized against the terminal's actual width.

## 0.56.0 — 2026-08-25
- **Resume**: sessions can be resumed after ending, and a restored tab continues its prior conversation. `_build_options` now passes `ClaudeAgentOptions.session_id` so DOXA's id and the CLI's agree — `--resume` against DOXA's own id always failed before.
- New `/resume [session-id]` opens the shared picker or resolves a prefix; always a new tab, never taking over the active one, and a still-running session is attached rather than forked. New setting `resume_restored`; off is byte-identical to pre-0.56.0 read-only restore. A resume that cannot happen degrades to a read-only tab naming why.
- **Transcript density**: Enter on a `/search` session header opens or resumes that conversation (folding moves to `←`/`→`). A three-call tool fold drops 15 rows → 4; chip borders and blank separators gone.
- **Spinner** during reasoning/generating, driven by the token-delta stream rather than a timer, floored at 0.1 s between advances. It trails the turn's output.
- The boot `lore` line adds pending count and user/project memory fill, reusing the cached values the chips read; one extra socket round trip at boot.
- **Failure containment**: four defects (an image-library timeout during paint, an unresponsive needs-input dialog, a dropped server-tool result, a half-drawn memory chip) shared one cause — nothing caught a failure and showed it. All now caught at `App._handle_exception`, rendered as a red-ruled block with a collapsed traceback, scrubbed of secrets and frame locals, logged to `~/.doxa/errors.log` (256 KiB × 2). A widget that raises while painting is quarantined (`display = False`). Repeats collapse into one block with a `×N` tally; past 25 the app exits with a report.

## 0.55.0 — 2026-08-25
- Fix a crash on GNOME Terminal/VTE: `textual-image`'s cell-size probe (`ESC[16t`) times out because VTE never answers, and the library logged the timeout with a full traceback to stderr, overwriting a full-screen TUI. That logger is silenced, the cell-size cache is always seeded so no in-render probe retries, and the image widget's width/height/render are wrapped to degrade to `[image: …]`.
- The startup banner defaults to a hand-drawn block-character mark: half-block rendering averages a 238-row image down to 6. New setting `boot_banner`: `auto`/`blocks`/`image`/`off`; the PNG raster is kept for kitty/sixel only.
- Fix the banner being clipped to a fixed 3-row CSS height regardless of content, and its crop/flatten step taking down the whole pane's boot.

## 0.50.0 — 2026-08-25
- The permission-mode cycler reaches `auto` and `bypassPermissions` (previously 3 of 6 modes). Cycle order: `default → acceptEdits → plan → auto → bypassPermissions → default`. `dontAsk` stays off the cycle, reachable only via `/mode dontAsk` with confirmation.
- The chip matches Claude Code's own glyphs and colours, read out of the installed CLI binary. It moved to first position so it never falls off an overflowing row, and is bold red for the two modes that stop asking.
- Entering a mode that stops asking writes a transcript line, not just a chip change.
- `/mode auto` and `/mode bypassPermissions` no longer confirm; `/mode dontAsk` still does.
- The persisted default setting still accepts only the three safest modes — cycling into bypass is per-session and never saved.
- Four constants replace two: `CYCLE_MODES`, `GATED_MODES`, `PERSISTABLE_MODES`, `UNASKED_MODES`.

## 0.48.0 — 2026-08-25
- Picker rows show `HH:MM` alongside the date (year dropped only for a current-year belief, to stay inside the 72-column floor); tested beliefs sort to the top of their scope group.
- Scope groups fold, with headers showing counts (`project (412 beliefs, 3 tested)`) — collapsed above the widget's max-height, expanded below it.
- Per-belief actions added: `confirmed`/`contradicted`/`stale`/`retract`, one keystroke each in the browser or via a per-belief menu in the picker.
- Retract requires arming; recording an outcome is a single action, since a later outcome can supersede it.
- Both surfaces degrade to read-only with a banner naming why when the loaded `lore_core` cannot record the write — measured by capability, not a version string.

## 0.47.0 — 2026-08-25
- Added a permission-mode chip and Shift+Tab cycle across the three safe modes (`default → acceptEdits → plan`). Ctrl+Tab is unsendable on terminals using the legacy key encoding.
- The cycle cannot reach `bypassPermissions`, `auto` or `dontAsk`; those need `/mode <name>` and a typed `y` to confirm.
- The mode syncs across daemon clients (`set_permission_mode` RPC, broadcast on change) and rides the hello frame, so a reattaching client sees it before painting.
- The persisted default accepts only the three cycle-safe modes.
- Fix the project memory-fill chip vanishing for every worktree-based session: it resolved the project slug from the raw cwd instead of the main repo root.
- Fix a release codename rendering as a subscription-plan name.

## 0.46.0 — 2026-08-25
- Added the beliefs browser: a full-height tab listing every belief and staged proposal, since the dropdown could not make hundreds of proposals reviewable.
- Belief rows show creation date plus LORE's last recorded verdict (`confirmed 2d`, `contradicted 2d`, `stale 40d`) rather than time-since-last-referenced.
- Tested beliefs sort first within their scope group, most-recently-tested first; never-tested beliefs form a stable bucket after them.
- Every proposal row shows its computed verdict up front (`add → memory/user`, `retract → belief #42`), derived from the function that applies it, so the verdict cannot disagree with the write.
- Approve/reject added per row; `/pending` had been read-only since 0.31.0.
- The browser degrades to read-only with a banner naming why when the loaded `lore_core` cannot record a write — measured by capability probe, never inferred from a version string.
- Claim text and evidence trails load on expand, capped at 40 rows.

## 0.44.0 — 2026-08-25

- Trimmed 4 of 6 blank rows padding every one-line transcript turn (kept
  the separator between turns and the internal spacing of multi-paragraph
  answers).
- Added a `mem u63% p39%` status chip for LORE's user/project memory
  fill — previously visible only via `lore status`, even though a write
  past the cap is refused outright. Percentages are counted in characters
  against the same file LORE's write path enforces the cap against,
  cached on mtime.
- Dropped "if API" from the cost chip's inline text to save 8 characters
  of status-bar width; the meaning survives in the existing `sub:` prefix
  and `≈` marker, and is spelled out in full in the tooltip and `/usage`.

## 0.43.0 — 2026-08-25
- Fix a web search appearing to hang with its permission dialog unresponsive to every key. Opening a blocking dialog now claims focus for that pane's prompt, but only when it is the active tab, so a background request does not take focus from someone typing elsewhere.
- Fix server-side tool calls (`ServerToolUseBlock`/`ServerToolResultBlock` — tools the API runs on the model's behalf) not being rendered at all.
- Scoped as 0.43.0, landed after 0.44.0; the number reflects when the work was scoped.

## 0.41.0 — 2026-08-25
- The startup banner draws the DOXA logo through the existing terminal-image ladder (kitty/sixel/half-block/text), exercising the image renderer on every launch.
- Logo width derives from the terminal's cell aspect ratio (~41 columns at a 6-row budget); height comes from the widget.
- Below 56 columns, or in text-only terminals, a hand-drawn block wordmark shows instead of the `[image: …]` fallback line.
- New setting `boot_banner` (default on) / `DOXA_BOOT_BANNER=0`.
- Fix the image library's RGB conversion discarding alpha instead of compositing it, producing a white slab on the dark theme.
- `/img` with no argument became the terminal-image diagnostic, reporting measured against inferred against never-asked capability.
- Pillow is now a declared runtime dependency.

## 0.39.0 — 2026-08-25
- Terminals using the legacy key encoding cannot send some combinations at all (`Ctrl+,`, bound to `/settings`). DOXA now detects this and says so instead of leaving a documented key dead.
- New **`doxa/keyboard.py`**: sends the kitty keyboard protocol's support query (`\x1b[?u`) plus Primary Device Attributes at startup and classifies the reply as kitty / legacy / unknown.
- `/about` gains a `keyboard` row; `/doctor`'s keyboard check measures instead of being a placeholder, treating legacy as a pass.
- No bindings changed. `DOXA_KEYBOARD_PROTOCOL` overrides detection as an env var only.

## 0.38.0 — 2026-08-25
- Fix focusing a pane's prompt on mount also activating that tab as a `TabbedContent` side effect, racing whatever else was deciding the active tab.
- Fix a restore forgetting which tab was active: `_persist_tabset` read `TabbedContent.active_pane`, which resolves asynchronously, so a save landing in that window wrote `null`.

## 0.37.0 — 2026-08-25
- `pyproject.toml` declares `lore-core` as a pinned git dependency; a bare clone previously could not run its own suite, resolving `lore_core` only through a separately-installed LORE plugin checkout.
- A LORE plugin checkout still wins over the pinned package when present; `DOXA_LORE_SOURCE=package` forces the dependency.
- `/about` gains a `lore from` row (`plugin` or `package`, with path), measured off `lore_core.__file__`.
- CI's workaround of checking LORE out on every leg is removed; two legs run the bare-clone case, one checks LORE out.

## 0.36.0 — 2026-08-25
- New `/context`: token counts by category (system prompt, tools, messages, free space), loaded `CLAUDE.md` files and per-MCP-tool cost. Every figure is the CLI's own `get_context_usage`, never estimated.
- New `!<command>`: runs a shell command in the session's directory with the user's full privileges — no sandbox, no allowlist, no confirmation. Deliberately not a slash command.

## 0.35.0 — 2026-08-25
- The ctx% chip's tooltip always shows the absolute token count (`24,000 of 200,000 tokens used, 176,000 left`); an inline `24k/200k` form is available via new setting `ctx_absolute` (off by default, hidden below 100 columns).
- New `/about`: version, Python/Textual/SDK versions, LORE plugin version and store path, platform, config path, repo and licence. Every row measured, omitted when unmeasurable, copyable via `c`.
- Fix the ctx chip's hover hint being keyed against its own markup while the lookup matched markup-stripped text, so the hint vanished at the coloured, highest-alert tiers.

## 0.34.0 — 2026-08-24

Pure refactor: `doxa/app.py` (6,415 lines, 36% of the package) split into
`doxa/ui/*` (labels, transcript, dialogs, statusline) and
`doxa/session/*` (commands, chips, runtime, pane), leaving `DoxaApp` and a
facade at 1,403 lines. No behavior change — the same 785 tests pass
unmodified, and every pre-split public name is still importable from
`doxa.app` for compatibility. The split follows the seams a future plugin
API (`docs/plans/plugin-api.md`) would extend: slash commands are now a
data table (`PANE_COMMANDS`), status chips are a list of records,
transcript event handling is a dispatch map (`EVENT_RENDERERS`).

## 0.33.0 — 2026-08-24

A session's own worktree branch could be listed as a valid base to rebase
onto; picking it silently zeroed `commits_ahead` (a branch can't be ahead
of itself), which made session-end cleanup treat real unmerged commits as
nothing to keep and delete the branch outright. Fixed in three places:
`switch_base` now refuses a target resolving to the session's own branch
(the load-bearing guard, also catches a hand-typed `/branch doxa/<id>`),
the branch picker no longer offers it, and `finalize` now treats an
already-corrupted `base_ref == branch` sidecar as unmeasurable (keep)
rather than zero (delete), so a sidecar already broken by this bug
recovers on its next session end instead of losing work.

## 0.32.0 — 2026-08-24
- Fix a restored tab reattaching to its live daemon replaying only the daemon's 512-frame in-memory ring, so a longer session came back empty.
- A saved session whose daemon has ended comes back as a read-only `ArchivedSessionTab` (same strip, same order, marked `⏺`) whenever a transcript exists on disk, instead of being dropped.
- Fix the saved active tab losing to whichever pane mounted last with three or more restored tabs.
- Fix an out-of-band event arriving before a pane finished composing killing that pane's event pump for the life of the tab.
- Restore renders capped content (40 turns, 20,000 chars per turn, 30 tool chips per turn) with an explicit "not shown" note.
- Splits are not restored; the persisted record reserves a `layout` slot.

## 0.31.0 — 2026-08-24
- New `notify_staged` (default on): fires only while the DOXA window is unfocused and tints the owning tab a steady muted violet.
- The notification block shows the actual staged proposal texts, diffed against the pre-review pending list, capped at 8 rows / 160 chars / 8 KB.
- New `/pending`: a read-only list and preview. The previous hint pointed at `/lore:pending`, a plugin command that does not exist inside DOXA.

## 0.29.0 — 2026-08-24

Added `background: transparent` (default `opaque`, unchanged look): stops
DOXA painting its own base color, letting an already-transparent terminal
show through. Uses Textual's `ansi_default` CSS keyword plus
`App.ansi_color = True`, the only mechanism that reaches the real
terminal rather than blending against Textual's own compositor — no
partial-transparency value exists, since SGR "default background" is a
binary reset with no blendable value. DOXA's chrome-tint ramp (status
bar, tool chips, etc.) stays opaque regardless of the setting; only the
base transcript background goes through. Intended for use over a dark
terminal/desktop background — DOXA has no light-mode palette, and body
text becomes near-invisible against a light one (measured contrast
1.1–1.6:1 vs. 13–17:1 on dark), which the README now says plainly.

## 0.28.0 — 2026-08-24
- Fix confirm dialogs (ctx% compact, Ctrl+Q with a turn running) having invisible buttons and dead Enter: `height: 1; padding-top: 1` rendered the button row at zero content height under Textual's border-box model.
- Fix clicking the beliefs chip erroring instead of opening its dropdown: a detached session's belief list crossed the daemon socket as one oversized frame (500 beliefs measured at 230 KB against a 64 KB cap).
- Fix picking a branch from the picker appearing to do nothing: the status bar showed the checked-out branch, but the picker changes the session's base branch.

## 0.27.0 — 2026-08-24
- The ctx% chip confirms before compacting; one click previously sent `/compact` with no undo.
- The session-handle chip opens a sessions picker (live and detached, current marked); copying moved to the picker's first row.
- The beliefs chip is clickable, opening a scope-grouped, filterable picker (`user`, `user model`, `project`).
- The repo-name chip becomes a directory-walking picker: a plain directory descends, a git repo root opens in a new tab.
- Every chip, including inert ones (cost, sha, headroom), gained a tooltip.

## 0.25.0 — 2026-08-24

Added a reasoning-stream fold: the model's own summarized thinking now
streams live into a collapsed per-turn `✻ Reasoning (N chars)` section,
mirroring the existing tool-calls fold. Gated by `show_reasoning` (default
on, connect-time only — the SDK has no live setter for it). Some current
models reject an explicit disable outright, so turning the setting off
means "DOXA stops asking to see it," not a guarantee reasoning is free.
Reasoning is display-only and never persisted to the LORE transcript.

## 0.23.0 — 2026-08-24

Added tab restore: closing and reopening the DOXA window now restores the
previous tab set (order, pinned names, active tab) instead of reattaching
only to the single most recent session. Persisted per repo scope in
`$DOXA_HOME/tabsets/<hash>.json`. A saved session the daemon registry no
longer recognizes (finalized, killed, machine rebooted) is dropped
silently and counted in a startup report. A session the user explicitly
stopped leaves the persisted set for good; one merely detached (Ctrl+W)
stays in it (v0.60.0 later found and fixed a gap in this exclusion logic).
New setting `restore_tabs` (default on) gates the read side only — the
record is always written.

## 0.22.0 — 2026-08-24

Status-bar chips for model and branch became clickable, opening a shared
dropdown picker (`ChipPicker`) instead of staying inert text next to a
`/branch`/`/model` command that already existed. Three chip tiers:
**selectors** (model, branch, effort) open the picker; **actionable**
chips (peers, ctx%, session handle) run an existing action directly;
everything else stays plain — deliberately not making every chip
clickable, since a blanket affordance stops meaning anything. Every
picker action calls the same coroutine the matching slash command already
used, so there's no second implementation of switching. Model list
source order: live Models API (verified unreachable under DOXA's normal
OAuth-only auth) → SDK-advertised catalog (currently none) → a small
static fallback, clearly marked as such in the picker.

## 0.21.0 — 2026-08-24

Search results (`/search`) now group into a two-level tree (a session
header, collapsed by default, with matching snippets nested under it)
once a result spans more than one session; a single-session result stays
flat. Enter on a snippet row inserts its excerpt into the prompt with a
provenance line (`[lore session <id> · <ts>]`), replacing the old bare
quoted insertion; large excerpts collapse to a placeholder the same way a
large paste does.

## 0.20.0 — 2026-08-24

Added explicit branch selection for worktree-per-session sessions
(previously a session's branch was implicitly forked from whatever the
launch directory had checked out, with no way to change it). `doxa new
--branch <name>` forks from a named branch at spawn; `/branch` lists or
switches live, free (fast-forward) only when the worktree is clean with
zero commits ahead of its current base, refusing otherwise with the same
message session-end cleanup uses. Also fixed two pre-existing
regressions found while wiring this in: the status bar's repo-name
segment showed the linked worktree's own directory name instead of the
main repo, and the tab label showed the session's own throwaway branch
instead of what it was based on.

## 0.19.0 — 2026-08-24

Added interactive permission handling (`can_use_tool`): previously the
SDK auto-denied any `AskUserQuestion` call or ambiguous permission
prompt silently, since DOXA had no callback wired for it. Such requests
now surface as `NeedsInputPopup` (arrow keys, number keys, Enter,
Esc-to-decline), blink the owning tab red, and fire a desktop
notification even with no client attached, parked in the daemon's event
ring until someone reattaches. This is the feature the tab-blink
infrastructure and `notify_needs_input` setting had shipped dormant for
in v0.11.0.

## 0.18.0 — 2026-08-24

Added a live subagent tracker: running Task-spawned subagents now show as
a second status-bar row (`⧉ <description>` per running subagent) with a
status chip count, clickable to open a read-only transcript tab that
mirrors the subagent's live trace and marks itself done when it finishes.
Built entirely on the engine's existing `parent_tool_use_id` tagging — no
new engine-side events.

## 0.17.0 — 2026-08-24

Added worktree-per-session (default on): each session in a git repo now
gets its own linked `git worktree` (`doxa/<id>` branch), so concurrent
sessions on the same repo/branch never conflict. At session end, a clean
worktree with zero commits ahead of its fork point is removed
automatically; a dirty or ahead one is kept, never auto-merged, with a
"kept doxa/<id> — merge when ready" notice. Setting: `worktree_per_session`
/ `DOXA_WORKTREE`. Also fixed: the scope key used to group sessions of
one repo together broke per-worktree (each worktree has its own `git
rev-parse --show-toplevel`) — now resolved through the shared `.git`
common directory instead; and the status-bar sha chip read the wrong,
worktree-private ref location and blanked out inside any worktree.

## 0.16.0 — 2026-08-24

Added animated GIF demos to the README gallery for interactive features
(tab lifecycle, tool-call fold, streaming markdown, tab rename, command
palette, search), generated via a scripted Pilot + FakeEngine driver,
replacing three of the previous static screenshots.

## 0.15.0 — 2026-08-24

Every tab now shows a provider glyph (`✳`, Claude-orange) ahead of the
model tier in its label; the status bar's git chip shows which linked
worktree a session is in (`repo ⎇ branch@worktree @sha`).

## 0.14.0 — 2026-08-24

Added `doxa launcher install|uninstall`: writes a per-user freedesktop
`.desktop` entry and icon (works across GNOME/KDE/XFCE/rofi via the same
two XDG files), no root required. Runs automatically after
`scripts/install.sh`; opt out with `DOXA_NO_LAUNCHER=1`. No-op with an
explanation on macOS, which has no start menu.

## 0.13.0 — 2026-08-24

Visual restyle: `TurnBlock`/`SystemBlock` lost their bordered boxes in
favor of background-tint role (raised tint for the user's prompt, base
tint for the agent's reply, dimmer tint for system lines) — long sessions
previously read as a stack of identical bordered crates. Tool calls
compact behind a collapsed "Tool calls (N)" fold instead of always-
expanded chips. Agent responses now render as streamed markdown (tables,
bold, code) via Textual's append-only markdown stream rather than plain
text.

## 0.12.0 — 2026-08-24
- Audited DOXA's cost figures against real API usage: `total_cost_usd` (server-computed) was confirmed authoritative and kept, matching hand-priced arithmetic on the unisolated engine path.

## 0.11.0 — 2026-08-24

Added per-status tab tinting: `-working` (amber) while a turn is in
flight on a background tab, `-done-unseen` (green) when a turn finishes
on a tab you're not looking at, clearing on activation (precedence:
attention > working > done-unseen). Added desktop notifications
(`doxa/notify.py`, `notify-send`-based) for turn-done and
update-available, gated by focus (`auto` mode fires only when the window
is unfocused). Attention-blink infrastructure for "needs input" was built
but left dormant, wired live in v0.19.0.

## 0.10.0 — 2026-08-24

The `claude` CLI process DOXA spawns per session now gets its own
isolated config directory (`CLAUDE_CONFIG_DIR`, `~/.doxa/claude-cli`)
instead of inheriting the operator's real `~/.claude` — previously a
session silently loaded the operator's plugins, hooks, and commands
(measured: 5 plugins, 16 hooks, 28 commands, one external MCP server) on
top of DOXA's own in-process LORE, which is what caused a session to
cite the LORE Claude Code plugin's own state instead of DOXA's.
Credentials are copied (not symlinked) into the isolated directory and
resynced on every session start and on first connect failure; learned
skills (`~/.claude/skills`) are symlinked through deliberately, since
they're human-approved rather than foreign automation.

## 0.9.0 — 2026-08-24

Replaced the single-line prompt `Input` with a multi-line `TextArea`: the
old widget silently dropped every line after the first on a multi-line
paste. Enter submits; Shift+Enter/Alt+Enter insert a literal newline. A
paste over 4 lines/4 KB collapses to a placeholder (`⧉ pasted N lines`),
expandable with Ctrl+G; the full text is always what's actually sent.
`Ctrl+V` is deliberately unbound, since Textual's own paste binding reads
from an in-app clipboard variable, not the live OS clipboard — bracketed
paste from the terminal is the real path.

## 0.8.0 — 2026-08-24

Added a clock chip (`doxa/clock.py`) at the tab bar's right edge —
configurable format/timezone/24h/seconds, defaults on, laid out on its
own compositing layer so it never displaces a tab. Uses exactly one
boundary-aligned timer (minute- or second-aligned depending on whether
seconds are shown), measured at indistinguishable idle CPU from off.

## 0.7.0 — 2026-08-24

Added `/doctor` and `doxa doctor`: read-only health checks (Python/DOXA
versions, CLI auth state, LORE store location/belief count, config
parse, live daemon count, terminal image protocol, MCP reachability)
with a pass/fail and fix command per check. Keyboard-enhancement grant
was reported as `?` at this point — real detection landed in v0.39.0.

## 0.6.0 — 2026-08-23

Added `/setup`: a first-run wizard that checks and fixes state one item
at a time (auth, LORE store location, migration, model/effort defaults),
each behind its own confirmation. Auto-runs once on first launch on a
machine (`~/.doxa/.setup-done` marker), rerunnable any time via
`/setup`.

## 0.5.0 — 2026-08-23

Added `scripts/install.sh`, a POSIX `curl | sh` installer: checks for
python3/uv/git/an authenticated `claude` CLI, installs via `uv tool
install --force git+https://github.com/docwilde/doxa` (never PyPI),
idempotent, pipe-safe (verified by truncating the script at multiple byte
offsets). `sh -s -- v0.5.0` installs a specific tag.

## 0.4.0 — 2026-08-23

- **Tabs are real sessions, and they say so.** `SessionPane` extraction
  under a `TabbedContent`: N sessions, one engine handle each, worker
  groups scoped per pane so a closed tab takes its workers with it.
  Ctrl+T spawns a fresh daemon in the same repo scope, Ctrl+W detaches
  it, Ctrl+Q ends it.
- **Tab labels: `Opus@doxa:main`** — model tier, repo, branch
  (`branch@worktree` in a linked worktree when the name adds something).
  Truncation at 34 columns sacrifices the model first, the repo second,
  and protects the branch. Outside a repo the session names itself from
  its first turn with one cached Haiku call.
- **Rename a tab in place** — double-click the header (or `/rename`),
  Enter commits, Esc cancels, empty restores the automatic label. A named
  tab is pinned: model switches and branch changes stop rewriting it.
- **`/search`** — full-text search over LORE's session index, live in a
  popup above the prompt from the moment you type `/search `. Debounced,
  sequence-guarded, FTS5 snippets. Empty query lists recent sessions.
  Replaced the Ctrl+R modal entirely; Ctrl+R now prefills the command.
- **Terminal images** behind a KGP → sixel → half-block → text ladder,
  with a guaranteed text fallback.
- **Subagent trace tree** — a Task-spawned subagent's tool calls nest
  under its chip, foldable at every level.
- **Streaming deriver** (`DOXA_DERIVE_SECS`, opt-in) — debounced
  mid-session LORE review; proposals still wait for the same human gate.
- **Act-time belief consult** — a cite-only note on the prompt, FTS only,
  floor-gated (`DOXA_CONSULT_FLOOR`).
- **Command surface has one order.** Every row of the slash registry
  declares a functional group; autocomplete, the Ctrl+P palette and
  `/help` all iterate the same sequence.
- **Status line**: context-pressure escalation by color, real
  subscription headroom, the git sha marked as a commit (`@a1b2c3d`), and
  a labelled detached-session handle (`⌁ session a1b2c3d`).
- **`peers N (2⌁)`** — live peer count and how many are running detached.
- **`/sessions`** — every live session with age and attached/detached
  state, `kill <prefix>` and `kill-detached`.
- **Settings modal** (Ctrl+,) over one precedence rule — environment >
  `~/.doxa/config.toml` > default.
- **`/model` `/effort` `/usage` `/clear` `/compact` `/help`** — only as
  far as the SDK actually goes: `/model` switches live, `/effort` is
  honest that the SDK sets it at connect time only.
- **`/login` / `/logout`** through the provider's own auth CLI.
- **`/update`** — fast-forward this checkout from origin, never merge,
  never rewrite; refuses a dirty tree or a non-checkout, runs `uv sync`
  when dependencies moved. `--restart` is the explicit opt-in that stops
  this window's sessions and relaunches.
- **Version is single-sourced** from `pyproject.toml`.
- **Nothing animates.** The in-flight marker's 16 Hz repaint timer and
  Textual's tab-underline slide are both gone (~290–345 ms saved per tab
  switch).

## 0.3.0 — 2026-08-23

- **Session daemon** — the engine moved out of the TUI into its own
  process, reachable over a Unix socket. Detach and reattach freely; a
  daemon outlives its last client by `--linger` seconds, then finalizes
  (LORE review + index) itself. `doxa`, `doxa new`, `doxa attach
  [prefix]`, `doxa stop [prefix]`.
- **Command palette** (Ctrl+P) with a DOXA provider and an attach picker
  fed by the shared registry.
- **History search** (Ctrl+R) — BM25 over LORE's session index,
  debounced as you type, inserting a text reference into the prompt
  rather than auto-sending anything.
- Ctrl+C quits: one press detaches, a second inside the window stops the
  sessions; the daemon's SIGINT path stays graceful.
- Idle CPU no longer grew with scrollback (hidden thinking indicators had
  kept their animation timers armed).

## 0.2.0 — 2026-08-23

- **Peer layer** — same-repo session discovery through a 0700 runtime
  registry, presence heartbeats, and scrubbed peer messaging (`/peers`,
  `/msg`). A message is scrubbed at the receiving choke point, never at
  the display.
- **Native LORE tools behind a registry** — belief search/show, session
  search, `lore_remember` (which stages a proposal and never writes
  memory), each declared as data with its cost and read-only status.
- **PreToolUse containment gate** — two strikes and the tool is disabled
  for the session, said out loud in the status bar.

## 0.1.0 — 2026-08-23

- **Session engine** wrapping the Claude Agent SDK with LORE wired
  in-process, event-stream API, host-driven session-end review (there is
  no SessionEnd hook — see `PHASE0_FINDINGS.md`).
- **Single-pane Textual shell** over it: foldable turns, tool chips that
  format their arguments and results lazily on first expand, streaming
  text.
- Dark surface ramp, Claude orange, round borders; logo and wordmark.
- Phase 0 spikes that decided the architecture: minimal agent loop,
  lifecycle-hook investigation, Textual + `claude-agent-sdk` asyncio
  coexistence.
