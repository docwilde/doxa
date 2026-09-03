# Changelog

Newest first. Versions are annotated git tags on the commit that shipped
them (`v0.1.0` … `v0.15.0`); the ranges below are derived from that history,
not written from memory.

## 1.3.0 — 2026-09-03

**A session can spawn a session, and it is OFF by default.** The first
capability that lets a model start a process. `docs/plans/spawn-session.md`.

- New **`doxa/session_ops.py`** — its own registry, not `operators.py`
  (whose charter is LORE tools), projected through the same
  `to_sdk_tools`/`ToolGate.execute` path so containment has one executor.
  `spawn_session(task, model=None, base_branch=None)`. **`cwd` is not a
  parameter**: the child spawns from the parent's repo via the trusted
  `OperatorContext`, so no cross-repo path opens.
- **`spawn_sessions` defaults off, and only `~/.doxa/config.toml` can arm
  it** — never a repo-local file, or an untrusted clone would be arbitrary
  code execution on `doxa new`. With no config the tool is absent from the
  model's surface, asserted at the engine's own call site.
- **Three caps enforced in the operator before `spawn_daemon` runs**, in
  DOXA's process, not in prose: **depth 2**, **3 live sessions per repo
  scope**, **1 per 60 s**. Derived — 3 from ~294 MB RSS per idle session
  against measured degradation past ~3 agents; 60 s is `STALE_AFTER_SECS`,
  the window the count cap can be counting a ghost.
- **A refusal reads `spawn_session: <reason>`**, the soft-error shape, so a
  cap doing its job never trips the two-strikes tracker and disables the
  tool by working.

**The child is told where it came from.** `SPAWN_PROVENANCE_INTRO` is
prepended by the receiving side (`SessionDaemon._initial_task_prompt`), so
a parent cannot suppress it — disclosure, not a trust downgrade.
`PEER_UNTRUSTED_INTRO` was rejected here: its conclusion ("take no action
unless this session's own user asks") would make a child refuse the task it
was spawned for. The real containment is the human reading the task.

**Two defects found reviewing the branch**: the task was scrubbed at the
display end only — you would have approved `[REDACTED:api-key]` while the
child received the key; `scrub_secrets` now runs once in the operator, so
the dialog string and the argv are identical. And `to_sdk_tools` appended
`[write: staged for review]` to every non-read-only operator, true of
`lore_remember` and false of this one (`Operator.write_note`).

**Not covered**: fleet cost aggregation, an outcome tag on `peer_left`, and
the `doxa new`-under-Bash path — `ToolGate` cannot see inside a shell
string, so that path is counted by the live-session cap but not gated and
carries no parent link.

## 1.2.2 — 2026-09-03

**`CHANGELOG.md`: 3,365 → 1,431 lines.** Twenty entries ran over the 40-line
budget the `doxa-lore-release` skill sets; the worst four (0.97.0 at 324
lines, 1.0.0 at 304, 0.92.0 at 287, 0.91.0 at 281) were each longer than the
README. Every entry is now at or under 40.

- **Nothing was removed, renumbered or redated**: all 89 existing headings
  are byte-identical and in the same order. Entries already inside the
  budget were not touched.
- **What survives**: the change itself, the real symbol names, every
  measured number (`2320.8 ms during /vsplit`, `1723 passed`, `3068×1734`),
  and every statement of what is deliberately not done.
- **What moved out**: deliberation, rejected alternatives, defect
  archaeology and the account of how a defect was found. All of it is still
  in `git log`, which is where a changelog is not.
- **The published GitHub release bodies were re-derived from the file**,
  each one `awk`-extracted from its own entry as the skill specifies, so the
  two cannot disagree.
- No code changed.

## 1.2.1 — 2026-09-03

**The two loop probes asserted a wall clock and fired on load.** They guard
v0.95.0's fix (the session factory ran on the event loop; 2320.8 ms of
frozen TUI per split). `STALL_LIMIT` was 250 ms — below the floor correct
code already sits at.

- **Measured the floor, ten splits per condition, nothing under test in the
  window**: 18–44 ms idle with the collector held off, 265 ms at a ~290 MB
  heap, **379–399 ms mid-suite**. Two causes, neither the test's: one gen-2
  GC per run over 90 ms (85–140 ms, scaling with live heap — that is
  "fails at 85–95%, passes alone"), and Textual's aggregate mount cost,
  ~545 `Stylesheet.apply` and 8 `_refresh_layout` calls as one
  uninterrupted pump pass. **No single call is the stall.**
- **The probes now assert the mechanism.** `_SpawnProbe` records
  `threading.get_ident()`; the loop's id must not appear. `_Heartbeat`
  keeps tick timestamps and `ticks_within()` asks whether the loop woke
  *during* the factory's block — ~200 wakes threaded, 0 on-loop. A wake
  count, not a duration, so a mount burst cannot fail it.
- **The gap assertion is kept and re-derived**, because the first two watch
  only the factory: `_without_collector_pauses` removes the gen-2 pause,
  `STALL_LIMIT` 250 ms → **1000 ms** (2.5× the measured 399 ms ceiling) and
  `PROBE_BLOCK_SECS` 2.0 s, so the bar also sits at half the defect. +3 s.

**Verified against pre-fix code**, each assertion in isolation: factory on
the loop thread, 0 wakes in 2000 ms, worst gap 2011/2017 ms vs the 1000 ms
bar. Split probes then passed 4 consecutive full runs, one entirely
concurrent with another suite.

**Not fixed, adjacent**: `tests/test_tab_labels.py` has an under-wait
affecting three tests (~1-in-3), asserting a painted label one pause after
the write. Pre-existing; its own task.

## 1.2.0 — 2026-09-03

The rail now says which sessions need you and which project each belongs to.
Parts 0, 1 and 1b of `docs/plans/collection-triage.md`.

### Status glyphs — the two states worth interrupting for

- **`doxa/triage.py`**, new: pure data and pure functions, nothing in it
  needs a screen; `doxa.app` reads the facts off the widgets.
- **Two independent columns**: *needs input* → **`⏳`**; *ctx ≥
  `CTX_GLYPH_PCT` (50.0)* → **`⧉`**. Unknown ctx earns **no** glyph.
- **`SIDEBAR_MARK_GLYPHS["-attention"]` is `⏳`, not `!`.** The rail costs a
  column: widths 19/22/38 → **20/23/39**.

### Colour keyed to the project, glyph keyed to the state

- **`triage.colour_for(repo_root)`**: a `blake2b` digest modulo a fixed
  six-name palette (teal, sky, rose, clay, moss, mauve), never `hash()`.
- **The name is stored, never a hex**; the hexes live once in
  `doxa/theme.tcss`. Override by name in `~/.doxa/config.toml` `[projects]`
  — a hex is refused, not honoured.
- **Grey is the absence of a project colour, nothing else.** Age dims
  (`SidebarLine.-old`), never recolours; "old" is ENDED only. New
  **`GitLine.main_root`** gives a worktree session its repo's colour.

### A rail entry is a PANE, not a session

- **`triage.PaneEntry` / `triage.aggregate`, `Row.ENTRY`**: one entry per
  `PaneGroup`, state most-urgent-wins over every member including the
  invisible ones.
- **`·3`** — three tabs, state from the visible one. **`·2/3`** — state from
  the second, which you cannot see, and a click goes to *that* member. A
  one-tab pane gets no entry row.

### What is deferred

- **Part 2 (default labels)** — not shipped. **Part 3 (ordering by
  urgency)** — the *ranking* is here, the *reordering* is not.
- **Glyphs on the tab header** — deliberately deferred, so a user with the
  rail hidden is still blind to what the rail shows.

## 1.0.3 — 2026-09-03

**Three README claims went stale the moment v1.0.0 and v1.0.2 shipped.**
Caught by the owner, not by a test — the README's own promise is that
everything it describes "has shipped and is described as it behaves
today", and nothing enforces it.

- **"Specified, not built" listed nine plans; it is six.** `session-sidebar`
  shipped in v1.0.0 and `peer-publishing` in v1.0.2, and both were still
  named as designs with nothing behind them. The five that have now left
  that list are named with their versions. The remaining six were each
  re-checked against their own status lines: `plugin-api`, `remote`,
  `mermaid`, `code-graph`, `sandbox`, `model-registry` all still read
  "draft for review".
- **The peers bullet described discovery and `/msg` and stopped there**,
  a release after peers began publishing `provider`/`model`/`engine`. It
  now says what a peer publishes AND that none of it is believed — a
  self-description is displayed, never verified, and decides nothing.
- **`peers.gif`'s caption** predated the same release. It now names the
  one field that does not ride the 15-second heartbeat: a model change
  publishes immediately, because a stale model id is a wrong answer where
  a stale token total is only an old number.

## 1.0.2 — 2026-09-03

**A peer session now says what it is, not only where it is.** `PeerInfo`
gains `provider`, `model` and `engine` — optional, defaulting to `None`,
**advisory forever**. `docs/plans/peer-publishing.md` is no longer a draft.

- **Where the values come from**: new `doxa.providers.CLAUDE_PROVIDER_ID`
  and `doxa.engine.ENGINE_ID` (`"doxa"`), and the string
  `SessionEngine.model` already holds. No second source, no network call.
- **`model` writes on the switch, not on the next heartbeat**:
  `PeerHost.set_model()` writes immediately, called by
  `SessionEngine.set_model()` before it returns.
- **A session on the CLI's `--model` default publishes nothing**, then
  republishes from the `init` `SystemMessage` handler — never `"default"`,
  and an unmeasured value reads `?`.

**Untrusted, and built so that staying untrusted is the cheap path.**

- The rule is stated once, on `PeerInfo`: displayed and logged, never
  verified, and no surface may use a peer's self-reported model to route
  work or relax a check without a human in the loop.
- **They do not reach the model**, asserted by three tests:
  `doxa/operators.py` holds no reference to the peer layer, a live peer's
  self-description is absent from the turn's prompt and the connect-time
  options, and `PEER_UNTRUSTED_INTRO` is pinned verbatim.
- **Bounded, never validated.** `peers._self_desc` coerces to text, runs the
  same `scrub_secrets` pass, drops structural JSON, caps at 64 chars.
- **`/peers` names it as a claim**: `self-reported: sonnet via claude on
  doxa`, `?` for anything unsaid, `self-reported: unknown` for nothing.

**Schema evolution, in both directions.**

- The three are read with individual `.get()`s and never added to
  `_ENTRY_FIELDS`, so an older entry reads as three unknowns rather than the
  `KeyError` that reaps it. No version field.
- **Fix**: `doxa/client.py` rebuilt daemon-supplied peers with a bare
  `PeerInfo(**p)`, raising `TypeError` on the first unknown field a newer
  daemon sends. Both sites now go through `peers.peer_from_mapping()`.
- 21 new tests in `tests/test_peer_self_description.py`, 18 verified failing
  against pre-change code.

## 1.0.1 — 2026-09-03

The live diff (0.92.0) opened only on `f2` or `/diff` and `_tick_diff` did
nothing while no pane was open, so nothing said there were changes at all.

### The diff chip

- **`diff 3 files +42 −7` on the status bar**, right of `repo ⎇ branch`,
  clickable through the same toggle `f2` fires.
- **`DiffCounts.chip`** (`doxa/diff.py`): `vs HEAD` with no worktree base
  recorded, **absent** at zero, `diff ⚠ no base` when `base_ref == branch`,
  `diff ⚠ unreadable` when git refuses. Below `DIFF_CHIP_MIN_COLS` (110) it
  drops its noun; neither ⚠ state shortens.
- **It rides the existing tick**: `PaneRuntimeMixin._tick_diff` no longer
  returns early with no pane open, fires at `_boot`, and re-counts after a
  reject. `diff.counts` costs ~10 ms a tick on a ~700-file repo, 305 ms on a
  3000-modified one, `exclusive` in its `"diff-counts"` group.

### `auto diff` — opening it by itself, off by default

- **New setting `auto_diff` / `DOXA_AUTO_DIFF`** (`doxa/config.py`,
  Session), **off** — opening the diff halves the transcript's width.
- **On, it opens once per session**, on the first tree-touching edit;
  `SessionPane._auto_diff_done` holds that on the session pane.
- **It refuses rather than mangles**, hitting the same
  `layout.split_refusal` floor a hand-driven split does, and never takes the
  keyboard — the prompt keeps focus while the diff appears.

### The diff, in colour

- **Backgrounds, not foregrounds.** `_hunk_text` / `_side_by_side_text`
  (`doxa/ui/diffview.py`) paint removed rows `#3B211E`, added `#1E3222`,
  foregrounds `#F3D6CF` / `#DCEBD3`; context rows carry no wash.
- **Line numbers down the left**, walked against the `@@` ranges, outside
  the wash, gutter sized by `_gutter_digits` (3–7). Rows pad to width rather
  than truncate, which needed `HUNK_INSET_COLS` (6).
- **`+42 −7` is green-and-red in both places it appears**, from
  `DIFF_ADD_NUM` / `DIFF_DEL_NUM` (`doxa/ui/labels.py`); still Rich `Text`,
  never markup. The largest hunk paints **4.0 ms → 10.8 ms**, per expand.
- 26 new tests in `tests/test_diff_chip.py`, all failing against 0.99.0.

## 1.0.0 — 2026-09-03

**A permanent, collapsible rail down the left of the window, listing every
session it knows about — outside the layout tree.** `f3` or `/sidebar`.

### The boundary, and the record

- The rail is a **sibling of the window root** under a new `Horizontal
  #window-row`, so splits, focus and `_pane_regions` never see it.
- `doxa/tabsets.py` grows a top-level **`collections`** key beside `tabs`
  and `layout`; absence of the key is the whole migration.

### Collections

- New **`doxa/collections.py`**, pure data and pure functions: a name, an
  **ordered** list of session ids, a collapsed flag.
- **A session belongs to at most one**, enforced in `assign` and in
  `from_json`; `delete` drops the label, never the sessions.
- `prune` keeps an ALREADY-empty collection, drops one that LOST every
  member; sessions in none sit under `— ungrouped —`.

### What a row shows, from one derivation

- `display_name()` rendered fresh, the four tab marks at `TAB_STATE_MARKS`
  precedence via `labels.mark_over()`, and a glyph `✓ + ▸ !`.
- `_sidebar_order()` also lists sessions mounted nowhere — detached or ended
  this run — dimmed with `· closed`. A reaped session is absent.

### Width, keys, and what is not in this release

- `SIDEBAR_CHROME` 6, widths **19/22/38**, `SIDEBAR_MIN_COLS` 53, each
  derived from the tab strip's own constants.
- **`f3`**/**`/sidebar`**, **`/collection new|rename|delete|add|remove`**,
  settings **`sidebar`** (empty = auto) and **`sidebar_width`** (19–38).
- **Fix**: `set_tab_label` recorded its identity string with no `Tab` widget
  yet, so a tab kept its birth label. `_tab_label_painted` ends that.
- **Fix**: `run_test()` never ended a session — **32 live agent processes**
  by the 60% mark of a full run; `conftest.py` reaps per test.
- **Not here**: the rail is not focusable; no keyboard model, drag and drop,
  nesting or sharing. **32 new tests**; suite **1723 passed**.

## 0.99.2 — 2026-09-03

**The README, rewritten: 35,006 characters to 13,123.** Its longest prose
block ran **4,621** characters and now runs **661**; everything cut moved to
[`docs/manual.md`](docs/manual.md).

**Five claims were false, not merely verbose.**

- **The split and diff keys named the kitty-only aliases as the primaries.**
  The primaries are **`ctrl+n`**, **`ctrl+o`** and **`f2`**, as
  `doxa/app.py`'s `BINDINGS` have said since v0.95.0; the aliases are kept
  and marked conditional.
- **Nine specifications under `docs/plans/`, not eight under `docs/`** —
  `session-sidebar.md` was missing — and the "two" that had left the list
  were three: `split-panes`, `live-diff`, `pane-groups`.
- **There is one `CLAUDE_CONFIG_DIR`, not one per session**:
  `cli_config_dir()` takes no arguments and returns `$DOXA_HOME/claude-cli`.
  The isolation is from your `~/.claude`, which is now the claim made.
- **The desktop notification is opt-in** — `notify_needs_input` defaults to
  off (`config.py`). The dialog and the tab blink do ship.

**An unresolved merge had been shipping in the prose since v0.97.0**: a
bullet truncated mid-sentence and repeated with a different key, two
contradicting `<em>` captions on `split-panes.png`, and a Quickstart
paragraph broken off mid-clause.

**Every surviving claim re-derived from source**: **fifteen** tooltipped
chips (`PaneChipsMixin._status_chips`), `/context`'s **200 cells**,
`--linger` **120**, the **`0600`** daemon socket, the **two-strikes** gate,
the image ladder **kgp → sixel → halfblock → text**, the **15-second** peers
heartbeat, LORE's **five** verbs.

**Moved to the manual rather than cut**: `## Containment`, typed belief
edges under `## LORE integration`, and `## Screenshots` — eighteen rows, so
**no asset under `assets/shots/` is left unnamed by any document**. Cut as
duplicated: `How it works`, the v0.37.0 `lore_core` history, and the
config-precedence and command-registry invariants.

`<p align="center">` wrappers gave way to markdown image syntax. Licence,
trademark and AGPL notices are untouched, character for character.

## 0.99.1 — 2026-09-02

**Fix: tabs closed with Ctrl+Q came back on the next start.** **Ctrl+Q ends
it, Ctrl+W parks it** is the rule from here on.

- **Root cause**: `_close_pane`'s `terminate=True` branch recorded the
  session into `_ended_this_run`, and `_persist_tabset` folded that dict
  into every later snapshot. `finalize()` never removes the conversation
  from the CLI's history store, so the next launch's triage
  (`doxa.cli.ended_tab_spec` → `history_mod.resume_state`) answered
  `RESUME_OK` and handed the tab back **live**, not read-only. Only
  v0.85.0's `is_last` branch excluded it.
- **`DoxaApp._persist_tabset`**'s mounted-pane scan excludes a `_stopped`
  pane again, as it did before v0.60.0. That one choke point is the whole
  fix: `_close_pane`'s `is_last` and `not is_last` branches collapse into
  one rule, and `DoxaApp._stop_active` (the palette's "Quit: stop session")
  now delegates to `_close_pane` rather than re-deriving a subset of its
  disposition.
- **`_ended_this_run` keeps its other job**, dimming an ended session's row
  for the rest of the run. What changed is that `_persist_tabset` no longer
  reads it: survival to the next launch is decided by `pane._stopped` at the
  mounted-pane scan.
- **Nothing on disk is destroyed.** The transcript stays where
  `doxa.transcript` wrote it — `/search`, the resume picker and `--resume
  <id>` all still find a Ctrl+Q'd session. Only the auto-restore set
  changes; `doxa.tabsets`' module docstring now states the rule (**Stopped
  vs. detached vs. killed**).

## 0.99.0 — 2026-09-02

**Fix: a turn that finished in a background tab left the transcript parked
above it**, so a finished turn looked like a killed one. Nothing was ever
interrupted or lost — only the scroll was.

- **Root cause**: every append site ended in
  `block_list.scroll_end(animate=False)`, which does nothing in a background
  tab. A hidden `TabPane` gives its subtree no geometry, so `size` and
  `container_size` are `Size(0, 0)` and `max_scroll_y` reads 0 for that
  whole window; every scroll reported success and went to row 0. On return,
  layout recomputed and nothing re-issued the scroll — `0.0 of 78` in the
  pilot that reproduces it.
- **`SessionPane.scroll_transcript_to_end`** (`doxa/session/pane.py`) is now
  the one door for that intent, and it remembers: a pane with no box on
  screen sets `_tail_pending`, which `on_show` (and `on_resize`) spends. All
  fourteen call sites go through it.
- **The readiness test is `block_list.size`, never `container_size`**: only
  a layout pass rewrites `container_size` and `virtual_size`, so a hidden
  widget keeps reporting its last visible box (94x21 for a pane that is 0x0)
  and a guard on those never fires.
- **Not `Widget.anchor()`**: Textual 5.3's compositor writes the anchored
  offset with `set_reactive`, bypassing `validate_scroll_y`, so a short
  transcript gets `scroll_y` of **-20** on a 100x45 pane. Pinned by
  `test_a_short_transcript_is_not_pushed_down_when_a_tab_is_shown`.
- **Not a cancellation**, now asserted rather than assumed: Textual's
  exclusivity groups are node-scoped, so a second pane's `"turn"` worker
  cannot touch the first pane's.
- **The same answer was lost two other ways**, both through the identical
  `scroll_end`: a turn driven by another attached client (`_peer_pump`), and
  a `turn failed:` block.
- 4 new tests in `tests/test_turn_survives_new_tab.py`, driving a
  `HalfwayEngine` that stops mid-turn and asserting against the composited
  screen; 2 fail against 0.96.0.

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

**The hierarchy inverts**: a tab no longer owns a tree of panes; the window
owns one tree of GROUPS, each owning its tabs (`docs/plans/pane-groups.md`).

### The model

- **`layout.Group(tabs, active)`** is now what a leaf of the window's tree
  is; its tab records are `Leaf` values, unchanged.
- **`doxa/ui/split.py` gains `PaneGroup`**, one `TabbedContent` each;
  `PaneTab` is one surface again. `SplitBox` and `neighbour` untouched.
- **`#session-tabs` is a CLASS now**; the first group's strip keeps the id,
  so an unsplit window's DOM is unchanged. 26 `query_one` calls replaced.

### Independence, which is the reported defect

- **`Ctrl+←/→` cycles the FOCUSED GROUP's tabs**: `_cyclable_tabs()` returns
  one group's strip, not every `TabPane`.
- **Closing a tab closes ONE session**; through v0.95.0 a tab holding a
  three-way split ended three. `_close_pane` → `_close_group_tab`.
- **`/movepane <n>`** re-creates a tab in another group — Textual 5.3 cannot
  re-parent a mounted widget — via `release_engine()`/`adopt_engine()`.

### Jumping to a group

- **`Ctrl+1`…`Ctrl+9`** number groups in reading order from the painted
  rectangles; `Ctrl`+digit has no C0 code, so **`/pane <n>`** always works.
- **A number overlay** paints each group's number over its region, including
  when the digit names none. One `set_timer`, 1.2 s, not an interval.

### Persistence, focus, width

- **`layout.kind` stays `"tabs"`, the flat `tabs` list authoritative**; the
  tree rides in a new **`layout.groups`**, whose absence is the migration.
- **An inactive tab in a visible group is not seen**: the blink and
  `-staged` tint stay. Only a group's **second** `TabActivated` moves focus.
- **Fix**: `grow_pane_towards`, `focus_pane_towards` and
  `DiffPane.session_pane()` each walked the wrong tree and went dead.
- Strip rungs at **17**/**34** columns; **26 new tests**, suite **1643**.
  **Not done**: floating windows, detached panes, mouse drag, gallery regen.

## 0.96.0 — 2026-08-31

**A bound key a terminal cannot deliver now says so at startup.**
`doxa.keyboard` has known which bindings the legacy encoding drops since
v0.39.0, but only `/help` and `/doctor` said so, and only to someone who
went looking.

- **`doxa.ui.labels.unreachable_notice`** (new) is the one-line notice:
  *"this terminal can't deliver 2 bound keys: Ctrl+, (use /settings),
  Ctrl+Tab (use /mode) -- see /doctor for details"*. Past
  `NOTICE_SUMMARY_THRESHOLD` (3) it names the count instead.
- **Silent on a kitty-protocol terminal and on `UNKNOWN`**, the two cases
  `unreachable_bindings()` already was. UNKNOWN is a deliberate call — no
  evidence for the claim — pinned by
  `test_unreachable_notice_is_silent_when_the_protocol_was_never_measured`.
- **The door**: `unreachable_doors` / `_door_for` (new) resolve each dead
  key against the same registry `/help` reads — a direct
  `SlashCommand.binding` match, or another `DoxaApp.BINDINGS` row firing the
  identical action that is a command's binding (`ctrl+tab` → `/mode`).
  `unreachable_bindings()` is now a thin wrapper; its output is unchanged.
- **`key_notice`** (`doxa/config.py`, `DOXA_KEY_NOTICE`), a `bool_on` row
  beside `boot_banner` in Appearance, **default ON**. Off returns to plain
  silence; `/help` and `/doctor` report the same keys either way.
- **Wired into boot, not compose**: `PaneRuntimeMixin._boot` mounts a
  `SystemBlock#key-notice-block` after the identity block, gated on
  `notice_enabled()` alone — no separate tty check, since the notice is
  already empty on a headless run.
- 16 new tests in `tests/test_keyboard.py`, four of them real-pilot boot
  tests polling painted `SystemBlock` regions; 13 fail against pre-change
  code.

## 0.95.0 — 2026-08-31

Two defects from live use: a pair of hotkeys that did nothing, and a split
that stopped the application for two and a half seconds.

### Fix: a split froze the event loop

- **Root cause**: `SessionPane.on_mount` built its engine synchronously; in
  production that is `daemon.spawn_daemon`, which blocks on the loop thread.
- **Measured** with a 10 ms heartbeat across a `/vsplit`: idle 12.0 ms,
  **2320.8 ms during the split**, of which `spawn_daemon` blocks 2307 ms.
- **There is no busy loop**: idle cost stayed **0.8–2.0 % of one core**
  across eleven scenarios, and `Widget._arrange` ran 0 times after a split.
- **The fix was one method down**: `switch_engine` already used `await
  asyncio.to_thread(...)`. New `PaneRuntimeMixin._build_and_boot` does the
  same.
- `spawn_daemon`'s 60-second `wait_secs` is left alone: off the loop it
  costs a pane that says `connecting…`, not a stopped application.
- Two unbounded retry loops, `DiffPane._repaint` and `FileSection.build`,
  are bounded at three passes.
- `test_a_vsplit_never_blocks_the_event_loop` fails at **522 ms** pre-fix
  and asserts on the loop, not on a duration.

### Fix: `Alt+D` and `Alt+S` could not arrive

- **Textual has no ESC-prefix-to-Alt path**: `XTermParser().feed("\x1bs")`
  yields `Key('escape')`, `Key('s')`; `alt+<letter>` only ever fired under
  kitty.
- **`Ctrl+N` side by side, `Ctrl+O` stacked, `F2` for the live diff** — the
  only letters left after subtracting the terminal's, Textual's and
  DoxaApp's.
- **`alt+←/→/↑/↓` keeps its Alt**: a modified arrow is `CSI 1;3<final>`. The
  `alt+` letters stay as kitty-tier aliases, marked `✗` in `/help`.
- **`doxa/keyboard.py` was why nothing warned**: `unreachable_under_legacy`
  said `False` for `alt+<character>`. True now, still False for named keys.
- `tests/test_split_keys.py` drives `XTermParser` with the bytes a terminal
  sends, and one test generalises the rule to every primary binding.


## 0.94.0 — 2026-08-31

**Fix: the gallery has not been able to render since v0.91.0.** Every scene
raised `ValueError: No Tab with id '--content-tab-pane-1'` before writing a
file, and only running it regenerates it — last run at 0.87.0.

- The capture scripts set `TabbedContent.active` to a pane's own `id`; since
  v0.91.0 the pane id and the tab id differ. `pane.tab_id` is the answer.

**Three surfaces that shipped without a picture now have one**, plus 13
stills and 12 GIFs recaptured at `DOXA 0.94.0`; they read `0.87.0` before
this pass.

- **`live-diff.png`/`.svg`**: session left, diff right, `2 files changed, +9
  −1 against main`, and the amber `⏳ reject queued` badge.
- **`split-panes.png`/`.svg`/`.gif`** (5 frames, 602 KiB) and
  **`folder-chip.png`/`.svg`**: `dir design-notes` over `/dir` and a bare
  `/cd`.
- Both are the first scenes rooted **outside this checkout**
  (`Scene.cwd_factory`); `diff.compute` shells out to real `git diff`.
- Geometry re-verified: 16 SVGs at 3049x1682.6, 16 PNGs and 13 GIFs at
  **3068x1734**, every file non-empty and every SVG parsing as XML.

**"What you get" was eleven paragraphs wearing a hyphen: 3,717 → 1,884
characters**, eleven bullets averaging 338 becoming thirteen averaging 145.

- **Three claims were wrong, not merely long**: three human LORE write paths
  are not approvals; a `default` mode chip stands down below 110 columns;
  `UNASKED_MODES` is three.
- `docs/plans/live-diff.md` was still under "Specified, but not built" two
  releases after v0.92.0 shipped it.
- **Four sections are new in `docs/manual.md`**: the spawned CLI, the
  transcript's rendering, making a split, and `/dir`, `/cd` and the folder
  chip.
- **Four alt texts described something the image does not show**, and **ten
  scenes were referenced by nothing** — both now named in the gallery.

**Suite: 1,617 passed**, unchanged from the pre-pass baseline.

## 0.93.0 — 2026-08-30

**Where am I, and can I move.**

- New **`/dir`** (`SessionPane._cmd_dir`, `doxa/session/commands.py`): the
  session's working directory, plus the worktree sidecar's branch and base
  when there is one.
- New **`/cd <path>`** (`_cmd_cd`) — and it does **not** move this session.
  No SDK control request changes a running CLI subprocess's cwd, so it opens
  a new tab at the target (`DoxaApp.open_tab_at`) and says the current
  session is unchanged.

**A directory is not a repo, and the status bar now says which.**

- New **`GitLine.folder_label`** (`doxa/ui/statusline.py`) with its chip in
  `doxa/session/chips.py`: outside a git repo the bar shows `dir NAME`, a
  different shape from `repo ⎇ branch` rather than the same chip with a
  blank branch. Clicking it opens the same directory picker.

**A session that cannot be resumed is still readable.**

- New **`DoxaApp._resume_read_only`**: when the CLI refuses to resume a
  session found by `/search`, its surviving transcript opens read-only
  through the existing `ArchivedSessionTab` and
  `doxa.transcript.mount_transcript` instead of reporting an error. No
  second viewer.

**The resume picker joins the column grid.**

- **`_fmt_resume_row`** renders through the shared
  `format_picker_row`/`PICKER_PREFIX_WIDTH` grid, so a resume row starts its
  text at the same offset as a belief row and a pending row. Sort order was
  already newest-first; verified, not changed.

**`sub:raven`: nothing to fix.** The label was already corrected in
`266d8d3` and is pinned by
`tests/test_identity.py::test_an_unrecognised_subscription_type_is_not_rendered_as_a_plan`.

## 0.92.0 — 2026-08-30

**The live diff** (`docs/plans/live-diff.md`): session left, diff right,
live as edits land, any hunk rejectable and reverted with a message.

### Where the diff comes from

- New **`doxa/diff.py`**: `Hunk`, `FileDiff`, `DiffResult` and `parse()`
  over `git diff`'s own output. **No differ was written.**
- **The tool-result stream is the tick, not a watcher**: `_tick_diff` fires
  on an `Edit`, `Write`, `NotebookEdit`, `Task` or tree-touching `Bash`.
- **`diff.bash_touches_tree` is an ALLOW-list** of 32 verbs and 16 `git`
  subcommands; any `>` is a write. `exclusive=True` is the rate limit.

### What it shows, and what it refuses to show

- **"No changes" and "cannot tell" are different sentences**: `STATUS_OK`,
  `STATUS_NO_BASE` and `STATUS_ERROR` each render their own headline.
- **The one refusal is `base_ref == branch`** — nothing committed shows in a
  diff against its own branch. A missing sidecar reads `against HEAD`.
- **Collapsed per file**; binary and huge files named rather than rendered
  (`MAX_HUNK_LINES_PER_FILE = 2000`); capped at 200 files, 20,000 lines.
- Unified by default, side-by-side above `SIDE_BY_SIDE_MIN_COLS` (100).

### Reject: two actions, in that order

- `diff.revert_hunk` builds a one-hunk patch for `git apply --reverse
  --recount`, `--check` first; one that no longer applies changes nothing.
- **A rejection during a turn is queued until `turn_done` and visibly
  marked**, and its message is user-authored — no `PEER_UNTRUSTED_INTRO`
  framing.

### The tree, and what is not covered

- **`layout.Leaf.view`** (`VIEW_SESSION`/`VIEW_DIFF`) is the one new field,
  written only when non-default, so v0.91.0 records stay identical.
- **`/diff`** and **`Alt+G`**; the diff does **not** take the keyboard. One
  diff per session; a queued rejection does not survive a restart.
- **Not covered**: no divider drag, word-level highlighting, `git diff`
  options, remote worktrees, partial-hunk reject or staging. Suite **1600**.

## 0.91.0 — 2026-08-30

**Recursive split panes** (`docs/plans/split-panes.md`). A tab now owns a
layout tree; a tab whose tree is a single leaf is the tab it always was.

### The layout tree

- New **`doxa/layout.py`** (`Leaf`, `Split`, recursive) and
  **`doxa/ui/split.py`** (`SplitBox`, `PaneTab`), laid out in `fr` units.
- **`SessionPane` stopped being a `TabPane`** — a nested one reassigns
  `TabbedContent.active` to a non-tab id. Its subtree and ids are unchanged.
- **`SPLIT_SLOTS = 2`** caps interactive depth, since Textual 5.3 cannot
  re-parent a mounted widget; splitting past it is refused in words.
- **`MIN_LEAF_WIDTH = 34`, `MIN_LEAF_HEIGHT = 9`**; `layout.split_refusal`
  halves the real painted rectangle and names the floor.
- **`/split`** stacks below, **`/vsplit`** side by side — vim's sense, not
  tmux's — on **Alt+S**/**Alt+D**.

### Focus, marks, dividers

- **A new leaf mounts unfocused**; `active_pane` now means the focused leaf
  of the active tab, derived from `self.focused`.
- **Directional focus is geometric**: `ctrl+shift+arrow` runs
  `layout.neighbour` over the panes' real rectangles.
- Closing one leaf collapses the split and keeps the tab.
- **Visible is not seen**: the marks clear only for the pane that got the
  keyboard. They live on `SessionPane._marks`; the header shows the OR.
- **The status bar IS the in-pane divider** — **Ctrl+Up**/**Ctrl+Down** or a
  mouse drag, stored as `prompt_ratio`, never a row count.
- **Between leaves it is `alt+arrow`** (`grow_pane_towards`), `DIVIDER_STEP
  = 0.03` down to `MIN_WEIGHT = 0.15`; both persist the tab set.

### Persistence, and what is not covered

- The tree serialises as **`layout.trees`**; **`layout.kind` stays
  `"tabs"`**, so an older DOXA still restores N ordinary tabs.
- **Absence of the key is the migration**: a 0.23.0–0.88.0 record reads as
  one single-leaf tree per tab, and a malformed tree falls back per tab.
- **58 new tests**. **Not covered**: no mouse drag between leaves, depth
  capped at two splits, no per-leaf tab label, no archived tab in a split.

## 0.90.0 — 2026-08-29

**Fix: an unrecognised `/`-command produced total silence.**
`SessionPane.on_prompt_submitted` passed any `/`-line `commands.lookup()`
missed straight to `_run_turn` — correct for `/compact` and adopted plugin
rows, wrong for a typo or an un-adopted plugin, where the CLI's own parser
answers with nothing at all.

- New **`doxa.commands.is_reachable`/`unreachable_message`**: the one check
  between "not a registry row" and "ship it to the CLI". `is_reachable`
  reuses `names()`, the same membership the autocomplete dropdown and the
  Ctrl+P palette compute, so passthrough is provably untouched.
- Deliberately **not** a hardcoded allowlist of CLI-accepted commands: that
  set moves with the CLI version, the operator's own `~/.claude/commands`
  and whatever plugins are staged.
- Three message shapes, mounted as a `SystemBlock` through new
  `SessionPane._run_unreachable`: **blocked plugin** (the name before `:` is
  in `claude_plugins.BLOCKLIST`, pointing at `/beliefs` and `/pending` for
  `lore`); **near miss** (`difflib.get_close_matches` at cutoff `0.72`, one
  suggestion); **generic unknown**, which says plainly it may still be a
  CLI-native or un-adopted plugin command.
- 14 new tests in `tests/test_slash_guard.py`, 12 failing against pre-fix
  code; the other 2 pin `/compact` and an adopted plugin command as negative
  controls.
- **Not covered**: a hand-authored command under the operator's own
  `~/.claude/commands` stays invisible to this check — `doxa.cli_isolation`
  never carries that directory into the isolated CLI.

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

**The gallery is regenerated against the running product.** Every asset was
last captured at **0.67.0** and had rendered `DOXA 0.67.0` for twenty
releases since. 24 stills and 11 GIFs at the unchanged **3068×1734**.

**The in-flight marker, which 0.78.0 named as stale and left that way.**

- `markdown-stream.gif`, `tool-calls.gif` and `reasoning.gif` were baking
  **`⋯ thinking`** — `ThinkingMarker`'s un-armed construction text, a state
  no real turn is ever in, because a GIF scene mounts its `TurnBlock`
  directly and never reaches `ThinkingMarker.start()`.
- New **`record_gif._marker()`** paints the marker at a chosen elapsed
  second by assignment rather than by arming the real `Timer`, using the
  values the widget computes for itself, so second N renders what a real
  turn shows at second N.
- `tool-calls` now runs `⠹ generating (2s)` → `working` **5s → 9s → 14s →
  17s → 19s**, climbing across the dead air between a `tool_call` and its
  `tool_result`. All three GIFs now end on a frame with no marker at all.

**Two surfaces the gallery never had.**

- New scene **`context`**: `/context` as 0.81.0 redrew it — a fixed 10×20
  grid of 200 cells at 0.5% each. Captured in the default glyph tier, with
  `DOXA_CONTEXT_GRID` unset.
- New scene **`beliefs-picker`**: 0.77.0's fixed 50-column row prefix
  (`PICKER_STAMP_COL` 15 / `PICKER_STATUS_COL` 28 / `PICKER_AGE_COL` 7) and
  0.86.0's **`g graph`** beside `y`/`c`/`s`/`r`. `belief_count` is
  overridden on the engine INSTANCE, not in `tests/fakes.py`.

**`beliefs-browser.png`/`.svg` are deleted, not regenerated.** They showed
the standalone beliefs browser tab, a surface 0.69.0 removed and 0.73.0
finished removing; their generating scene went with the feature, which is
why nothing could refresh them for eighteen releases. Neither `README.md`
nor `docs/` ever referenced either path.

**README alt text, checked against the images rather than the captions.**
`hero`'s status-bar description omitted the **permission-mode chip**, which
0.50.0 puts first on the row; `markdown-stream` and `tool-calls` now carry
the ticker. Both new stills are placed in the gallery.

## 0.86.0 — 2026-08-28

**The beliefs picker gains a graph view.** 0.84.0 gave the MODEL the belief
graph and left the operator with nothing to look at.

- New row action **`g`** beside `y`/`c`/`s`/`r`
  (**`BELIEF_GRAPH_ROW_ACTION`**): that belief's graph neighbourhood.
- New setting **`graph_view`** (`DOXA_GRAPH_VIEW`, default `browser`):
  `ascii` folds `format_edges` under the row via new
  **`ChipPicker.expand_rows`**, `browser` opens LORE's mermaid page.
- **`g` writes nothing**, so it survives a session whose `lore_core` cannot
  record an outcome — which loses `y`/`c`/`s`/`r` entirely.
- New module **`doxa/beliefgraph.py`**; every store read and the browser
  launch go through `asyncio.to_thread`.

**Per belief, and there is deliberately no whole-graph view.**

- A whole-graph view fragments: **63 edges over 104 beliefs became 44
  disconnected clusters**, stacked to **1188×13814 pixels**.
- **Hidden at zero**: **745 of 799 active beliefs (93%)** on the live store
  have no row in `belief_edges`, and say `no relations recorded`.
- **One gate for both renderings** (**`beliefgraph.edge_block`**), so
  `ascii` and `browser` cannot disagree about what a belief has to show.

**Where the page lands, and how a browser reaches it.**

- **`$DOXA_HOME/graphs`** (0700), not `LORE_ROOT`: a rendered artifact of
  DOXA's UI is not memory. The path is printed into the transcript.
- **Served over loopback HTTP, not `file://`**, a null origin some browsers
  refuse the mermaid fetch from. **`beliefgraph.page_url`** serves 127.0.0.1
  on an ephemeral port.
- **That server is token-gated**: the page carries belief claims in full, so
  every request needs a per-process `?k=` token held in memory.

**DOXA draws none of it** — no mermaid source, no edge formatting, no
traversal. **`beliefgraph.graph_state(mode)`** measures capability off the
API per rendering, never off a version. `lore-core` moves 0.45.0 →
**0.48.2**, required rather than cosmetic. Full suite **1491 passed**, with
22 new tests in `tests/test_belief_graph_view.py`.

## 0.85.0 — 2026-08-28

**Session lifecycle and keybindings, four independent defects from live
use.**

- Fix **the desktop notification fired on turn-done, not on
  input-required**: `doxa.notify.notify_turn_done` and its call site are
  removed outright. `notify_needs_input` now defaults **OFF**
  (`kind="bool"`, was `bool_on`), and `doxa.notify.should_fire` reads each
  trigger's default off the registry (`_trigger_default`) instead of
  hardcoding one.
- Fix **Ctrl+C stole terminal copy**: the double-press quit binding is gone
  (`action_ctrl_c_quit`, `CTRL_C_DOUBLE_SECS`, `_ctrl_c_timer`), and
  `DoxaApp.__init__` pops Textual's own `system=True` `ctrl+c` out of
  `self._bindings`, since a same-key override only shadows it. Ctrl+Q and
  the palette cover what it did.
- Fix **closing the last tab did not start the next launch fresh**:
  `_close_pane` now excludes the closing session from the persisted restore
  set (`_persist_tabset`'s new `exclude_session_id`) when it is the last
  open tab, on either key. A Ctrl+W session still runs and is reattachable;
  a toast on close now says so, where it used to detach in silence.
- Fix **a background `AssertionError` on that same close**:
  `SessionPane._peer_pump` asserted `self.engine is not None` after awaiting
  `_engine_ready`, but `detach()`/`stop()` clear `self.engine` without
  cancelling the worker. Now a plain early return — nothing left to pump.
- Fix **Ctrl+Left/Right skipped read-only tabs**: `_cycle_tab` walked
  `panes()`, so an archived tab or an open subagent transcript was
  unreachable. New `_cyclable_tabs()` walks every `TabPane` in strip order,
  and `_focus_tab` gives the two read-only kinds their own `.scroll` focus
  target, closing an `AUTO_FOCUS` gap that reverted the cycle to a hidden
  pane's prompt.

Full suite: **1469 passed**.

## 0.84.0 — 2026-08-28

**The belief graph reaches the model.** LORE has carried typed relations
between beliefs since 0.41.0; nothing in DOXA could read them.

- **`lore_belief_show` gains its edges** (`lore_core.beliefs.belief_edges`):
  verb, direction, the other belief's id/claim/status, `source`, and the
  distinct-session `support` count. `"edges": []` for a belief with none.
- New operator **`lore_belief_neighbours`**, one traversal tool rather than
  five: `belief_id` + `hops` (≤2) gives the k-hop neighbourhood, `to_id` the
  most-confident path. Capped at `BELIEF_NEIGHBOUR_LIMIT` (20).
- **Structure earns no authority**: every belief returned carries its OWN
  `citation_status` (`steer`/`cite_only`). **Path confidence is the product
  over hops**, beside `hop_count`, so a long chain reads as weak.
- `co_derived` relations are projected from `belief_evidence` at read time
  and labeled `"projected": true`.

**The session is told the graph exists.**

- **`SessionEngine._graph_awareness_block`** appends a `[BELIEF GRAPH]`
  block after the LORE snapshot, naming the five verbs and stating that
  reachability is not authority. `/context` reports `graph_awareness_chars`.
- **Hidden at zero**: emitted only when the store carries traversable edges,
  so a store whose only relations are projected `co_derived` gets nothing.

**Graph-backed act-time context, off by default** — new `graph_context`
(`DOXA_GRAPH_CONTEXT`).

- **`SessionEngine._graph_context_block`** calls LORE's own
  `context_candidates`/`render_context_block` rather than a second ranking
  implementation.
- A stage separate from the plain-FTS consult note, toggling independently,
  gated as `graph_context_enabled()` AND `stage_disabled("beliefs")`.
  `/context` reports `graph_context_chars` as the last-injected size.

`lore-core` moves 0.42.1 → **0.45.0**; DOXA surfaces neither the mermaid
view nor the skills tier here.
`test_a_worktree_session_still_finds_its_project_memory` has its assertion
inverted and kept as a regression pin, since lore-core 0.41.0 resolves
through `--git-common-dir`. Full suite: **1465 passed**.

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

- **Tabs are real sessions.** `SessionPane` under a `TabbedContent`: N
  sessions, one engine handle each, worker groups per pane. Ctrl+T spawns,
  Ctrl+W detaches, Ctrl+Q ends.
- **Tab labels: `Opus@doxa:main`.** Truncation at 34 columns sacrifices the
  model first, the repo second, and protects the branch. Outside a repo a
  session names itself from its first turn.
- **Rename a tab in place** — double-click the header or `/rename`; a named
  tab is pinned against model and branch changes, and empty restores the
  automatic label.
- **`/search`** — full-text search over LORE's session index, live in a
  popup, debounced and sequence-guarded, with FTS5 snippets. Ctrl+R prefills
  it.
- **Terminal images** behind a KGP → sixel → half-block → text ladder, with
  a guaranteed text fallback.
- **Subagent trace tree** — a Task-spawned subagent's tool calls nest under
  its chip, foldable at every level.
- **Streaming deriver** (`DOXA_DERIVE_SECS`, opt-in): debounced mid-session
  LORE review, proposals still waiting for the same human gate.
- **Act-time belief consult** — a cite-only note on the prompt, FTS only,
  floor-gated (`DOXA_CONSULT_FLOOR`).
- **The command surface has one order**: autocomplete, the Ctrl+P palette
  and `/help` iterate the registry's own group sequence.
- **Status line**: context-pressure colour, subscription headroom, the git
  sha as a commit (`@a1b2c3d`), `⌁ session a1b2c3d`, **`peers N (2⌁)`**.
- **`/sessions`** — every live session with age and attached/detached state,
  plus `kill <prefix>` and `kill-detached`.
- **Settings modal** (Ctrl+,) over one precedence rule: environment >
  `~/.doxa/config.toml` > default. Version single-sourced from
  `pyproject.toml`.
- **`/model` `/effort` `/usage` `/clear` `/compact` `/help`**, only as far
  as the SDK goes — `/effort` is honest that the SDK sets it at connect time
  only. **`/login`/`/logout`** use the provider's auth CLI.
- **`/update`** fast-forwards this checkout from origin, never merging; it
  refuses a dirty tree and runs `uv sync` when dependencies moved.
  `--restart` relaunches.
- **Nothing animates.** The marker's 16 Hz repaint timer and Textual's
  tab-underline slide are gone (**~290–345 ms** per tab switch).

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
