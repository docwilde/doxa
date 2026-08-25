# Changelog

Newest first. Versions are annotated git tags on the commit that shipped
them (`v0.1.0` … `v0.15.0`); the ranges below are derived from that history,
not written from memory.

## 0.58.0 — 2026-08-25

**Three reports about DOXA's own identity: which DOXA the start-menu
shortcut actually launches, what the terminal window is called, and a tab
you could not close.**

### The shortcut launched a different DOXA

*"the shortcut uv run launcher install creates is serving a stale version
(0.8.0) … not the one in /repos/doxa."*

**The defect was never a version string.** Nothing in `doxa/launcher.py`
carried one — the entry it wrote recorded no version at all. `Exec=doxa`
was a *bare name*, and a `.desktop` `Exec` is resolved against the PATH of
the **desktop session** at click time: the one the display manager
exported at login, which has no venv on it, frequently no `~/.local/bin`
either, and no relationship whatsoever to the shell that typed the install
command. So `launcher install`, run from a current checkout, wrote a
shortcut that started whatever old `uv tool install`ed copy was still
lying around. Both are called `doxa`. They are different programs. The
0.8.0 the user read was a correct rendering of the wrong install.

- **`Exec` is now an absolute path to the DOXA that wrote the entry.**
  `launcher.exec_target()` — the console script in this environment
  (`<venv>/bin/doxa`), or that environment's own interpreter run as
  `-m doxa.cli` when there is no script.
- ***Judgment call, stated rather than defaulted into: the shortcut
  pins.*** The alternatives were "whatever `doxa` is on PATH" (what it
  did) and "the install this command was run from" (this). PATH
  resolution has a real advantage — it follows a later `uv tool install`
  upgrade for free — and one disqualifying property: it is unobservable,
  which is the entire defect. `doxa launcher install` is not a request
  for a shortcut to *some* DOXA; the user runs it from a specific tree,
  having just built or updated that tree, and means *this one*. An entry
  that pins is at least a wrong answer you can read.
- **The cost of pinning is paid loudly, not hidden.** A shortcut to a
  checkout dies when the checkout moves. Three things make that legible:
  `install` prints the path and the version it reports, the entry records
  both, and `doxa doctor`'s new `launcher` check **fails** with
  `doxa launcher install` as the fix when the recorded path has stopped
  existing.
- **A second bug, found by running the command rather than reasoning
  about it.** The first fix anchored on `sys.executable`. Under
  `uv run doxa launcher install` that is the *base* interpreter uv
  resolved the environment from
  (`~/.local/share/uv/python/cpython-3.12…/bin/python3.12`), while
  `sys.prefix` is the project venv. An `Exec` built from the former names
  a python that cannot import `doxa` **at all** — a shortcut that fails
  with `ModuleNotFoundError`, which is strictly worse than one that
  starts the wrong version. `exec_target()` anchors on `sys.prefix`, the
  environment this code was imported from by definition. Nothing is
  `resolve()`d either: `<venv>/bin/python` is a symlink to the base
  interpreter and following it is the same crash by another road.
- **The install report is part of the fix.** It names the absolute `Exec`
  path, the version that path reports, and both icon paths. The defect
  was invisible for exactly as long as it was because the command said
  "installed", the entry looked fine, and the disagreement surfaced weeks
  later as a wrong-looking banner.
- **The other DOXA is reported, never touched.** When a *different* `doxa`
  is on PATH, `install` names it and its version and says the shortcut
  does not use it. Its version is **read, not run**:
  `launcher.version_at()` follows the console script's shebang to its
  interpreter and reads the `.dist-info` directory name out of that
  environment's site-packages. A launcher command must not spawn an
  unknown binary to write a text file, and a stale install is precisely
  the copy most likely to be broken in a way that hangs. Unmeasurable
  reads as "an unknown version" — a launcher that *guessed* one would be
  repeating the original defect in a new place. Rewriting somebody's
  `uv tool install` is not a side effect a shortcut command gets to have,
  and a stable tool install beside a dev checkout is how most people work.
- **Secondary, and still true: every version anywhere comes from
  `doxa/version.py`.** The entry records `X-DOXA-Version` and shows the
  version in the `Comment` the desktop puts on hover. `X-` prefixed
  because the freedesktop spec's own `Version` key means the version of
  the *spec* the entry conforms to; writing `0.57.0` there would be a lie
  in a standardised field. `DESKTOP_ENTRY` was a module-level constant
  and is now `desktop_entry()`, a function — both interesting fields are
  measurements of the running process, and a constant evaluated at import
  time is exactly the shape of thing that goes stale.
- **An already-installed stale entry is overwritten.** `install()` already
  promised idempotence, DOXA wrote every byte of the file, and refusing
  would leave the user holding the broken entry that made them run the
  command. An entry with no version key at all — every entry written
  before this release — reads as `unversioned` and is stale by
  construction: it also carries the `Exec=doxa` that made the version
  wrong.

### The terminal window and the taskbar entry

*"can we change the terminal title to 'DOXA' and add the app icon to the
planck icon and taskbar window title?"*

**What Textual actually offers: nothing.** Measured against the installed
5.3.0 rather than assumed — the only OSC the library writes is `OSC 52`
for the clipboard (`textual/app.py`, `_set_clipboard`). There is no
`set_terminal_title`, no driver hook, and `App.TITLE` / `App.title` are a
plain reactive consumed by the `Header` widget, which never leaves the
process. DOXA already set `TITLE = "DOXA"` and it had never reached a
window manager. So `doxa/window.py` writes the sequence itself.

- **Setting it is the easy half; giving it back is the point.** A
  terminal title is process-global state with no owner and **no query** —
  OSC 21 is disabled by default in every terminal that ever shipped it,
  because a program that can read the title can read what the previous
  program left there. There is nothing to save and restore by hand, and an
  app that merely sets a title leaves the user's window called "DOXA"
  until they notice, which they then cannot undo because they do not know
  the escape either.
- **So: the title stack.** `CSI 22;0t` asks the *terminal* — the one
  process that does know — to push the current window and icon titles;
  `CSI 23;0t` pops them back. `window.terminal_title()` is a context
  manager around that pair with the pop in a `finally`.
- **Restore-on-exit, and exactly which exits it covers.** Normal quit:
  `App.run()` returns. **Ctrl+C**: two cases, both held — DOXA binds
  `ctrl+c` itself (`action_ctrl_c_quit`, `priority=True`) so the ordinary
  press is a quit action that returns normally, and a press arriving
  before the app is live raises `KeyboardInterrupt` straight out of
  `run()` and through the `finally` (it inherits from `BaseException`, so
  a bare `except Exception` would have missed it). **A crash**: any
  exception unwinds through the `finally` before the traceback prints.
  All three are separate tests. What it does **not** survive is `SIGKILL`
  and a hard `SIGTERM`, and this deliberately does not chase them:
  Textual installs its own signal handlers, a second one racing them is
  how a shutdown path acquires a Heisenbug, and a terminal that lost its
  title to `kill -9` gets it back from the next shell prompt anyway. A
  terminal without the stack ignores both sequences and recovers the same
  way — the floor `vim`, `htop` and `tmux` all accept, for the same
  reason.
- **Wrapped around `DoxaApp.run()`, one seam.** Not `on_unmount` (it does
  not fire on every way out of a TUI, and when it does it fires while
  Textual still owns the screen — the restore must be the *last* thing
  written) and not at each of `doxa.cli`'s four call sites. `doxa new`,
  `doxa attach`, a tabset restore and `--in-process` all come through
  `run()`, and so will the next entry point, which is what stops that one
  shipping without the restore. `run_test()` does not come through it, so
  the suite emits no escapes into its own captured output.
- ***Judgment call: the title is `DOXA — <project>`, not bare `DOXA`.***
  "DOXA" is the floor the user asked for and is what a run outside any
  project gets. The project is added because of *where the string lands*:
  a taskbar button and a terminal tab exist to tell two windows apart, and
  what distinguishes two DOXA windows is never the application — it is
  always the repository. Three buttons all reading "DOXA" is a taskbar
  that has stopped working as a taskbar. It deliberately does **not**
  carry the active session: the title would then change on every tab
  switch, and a taskbar label that moves under the pointer is worse than a
  stale one. The session is already named on the tab strip, inside the
  window, where the user is looking at it. The window title answers
  "which window", once.
- **Three refusals.** Not a terminal (an escape into a pipe is corruption
  of somebody's data), `TERM` unset or `dumb`, or `DOXA_NO_TERMINAL_TITLE`
  set — an env var rather than a config setting because "my multiplexer
  owns the title" is a property of the environment, not of the DOXA.
  Directory names are scrubbed of control characters and bounded: a `BEL`
  inside the payload would terminate the OSC early and spray the rest
  across the screen, and a directory name is attacker-influenced input on
  a machine that clones repositories.
- **The icon was already in the wheel; verified, not assumed.**
  `pyproject.toml`'s `force-include` has mapped `assets/icon.png` to
  `doxa/assets/icon.png` since v0.41.0, and the entry has always said
  `Icon=doxa` against a 512×512 PNG in `hicolor`. What was missing is the
  **scalable** icon: a panel asking for 22px off a 512px PNG downsamples,
  and hicolor's lookup prefers an exact raster size and falls back to
  scalable. `install` now also writes
  `icons/hicolor/scalable/apps/doxa.svg` from `assets/icon.svg`, so the
  launcher grid gets the PNG and a small panel slot gets a rendering
  instead of a smudge. `uninstall` removes it too.

### Ctrl+Q did nothing on a read-only tab

*"CTRL+Q doesnt work with read-only session tabs…"*

`action_end_session` is "end this session (finalize now) and close its
tab". `_end_session` asked for `self.active_pane`, which is `SessionPane`
-only, got `None` on every read-only tab, and **returned** — so the user
sat on a tab they could not close with the key they had been taught closes
tabs. Same defect class as the beliefs browser and Ctrl+W in v0.46.0,
which "had no branch and would have been unclosable"; it now takes the
same shared answer.

- **`DoxaApp._close_read_only_tab()`** is that answer, extracted from
  `action_close_tab` so **both** keys reach it. It returns a `bool`, and
  that is the load-bearing part of the signature: it lets a caller tell
  "closed a read-only tab" from "there was nothing here I know how to
  close", which is what the *next* tab kind will hit.
- **Ctrl+W and Ctrl+Q still read correctly.** The distinction between them
  is about the SESSION — Ctrl+W leaves it running, Ctrl+Q finalizes it —
  and on a tab with no session there is no distinction left to draw: the
  archive's session ended before the window opened, the subagent's
  transcript is a copy, the browser holds no engine. Two keys agreeing
  where the difference is meaningless is not ambiguity, it is the absence
  of a trap. What *would* be wrong is Ctrl+Q reaching past the visible tab
  to end its owning session — each of the three new tests asserts that it
  does not (the owning pane stays mounted, its engine unfinalized, its
  turn still in flight).

**Every tab kind, every close key.** This table is the artifact that stops
the next tab kind shipping unclosable:

| tab kind | Ctrl+W — *close-detach* | Ctrl+Q — *end the session* |
|---|---|---|
| `SessionPane` (live) | Tab closes. The daemon **keeps running**; reattach via the palette or `doxa attach`. Asks first if a turn is in flight. | Session **finalized now** (LORE review + index run daemon-side), socket closed, presence file removed, daemon reaped; tab closes. Asks first if a turn is in flight. |
| `SessionPane` (the last one) | Same, and the window closes on detach semantics. | Same, and the window closes on stop semantics. |
| `SubagentTranscriptTab` | Tab closes; the owning pane drops its reference. No engine, no daemon, no dialog. | **Identical** — there is no session here to end. The owning session is untouched. |
| `ArchivedSessionTab` | Tab closes, and that is the one way to take it out of the persisted tab set. | **Identical** — the session ended before this window opened. |
| beliefs browser (`BeliefsBrowserTab`) | Tab closes; the owning pane drops its reference, so reopening builds a fresh one. Never persisted. | **Identical** — it holds no engine. |

App-level quitting is unchanged and is still Ctrl+C: one press detaches
every tab, a second within two seconds stops every session.

## 0.57.0 — 2026-08-25

*(Numbered 0.57.0 rather than the 0.52.0 this work was assigned: 0.55.0
and 0.56.0 both released while it was in flight, and tags ascend in
time.)*

**Staged proposals get their own chip.** v0.48.0 settled that approve and
reject belong to *proposals* rather than to beliefs. The consequence is
this: proposals need their own way in. Until now the staged pile was
reachable only by knowing `/pending` existed, which is how an operator ends
up with **175 proposals they have not looked at all day**.

- **A `175 proposals` chip beside the belief count and the memory fill.**
  Those three are the same question — what does LORE hold for this session —
  and this is the only one *waiting on the user* rather than describing the
  store. **Hidden at zero**, the convention the subagent and peer chips
  already follow.
- **The count agrees with the list by construction.** Both walk
  `lore_core.pending.load_pending` through one predicate,
  `doxa.engine.pending_visible`. That is one function rather than two
  because of a defect this release nearly shipped: the count was first
  written on `lore_core.deriver.pending_texts`, which returns
  `item["text"] or item["name"]` and silently drops anything carrying
  neither — so every **filemap** proposal vanished from the count while
  staying in the list, and a live spool of 59 rendered a chip reading
  **5**. Caught by rendering both against the real store, not by a test;
  the test pinning them equal over a mixed spool came after.
- **Cached on the pending directory's mtime.** `_refresh_status` runs on
  every event-driven refresh; scoping means opening every staged file. A
  directory's mtime changes exactly when an entry is added or removed, so
  an unchanged spool costs one `stat`. Measured on the live store:
  **4.2 ms cold, 0.0062 ms warm — a ~670× saving.** Read locally, the way
  `memory_fill` reads the file the daemon also writes.
- **Grouped by KIND** — `memory/user`, `memory/project`, `filemap`,
  `belief`, `skill` — because kind is what the **verdict acts on**. The
  skill lane falls out for free, and that is the point: LORE's own
  `/lore:pending` keeps skills out of memory clustering, because judging an
  installable script with the same glance as a remembered sentence is how a
  bad skill gets in.
- **No row in the list acts on a proposal.** Selecting one opens *that
  proposal's* named verbs. **Approve arms; reject is one act** — approving
  writes into the model's context, rejecting archives a file that stays on
  disk. These use the **wider** gate (`lore_write_state`, LORE 0.36.0): a
  new entry with no `via` label is what that ledger exists to prevent. One
  pid per call, no bulk form under any spelling.

**Four corrections to the pickers, all reported after using v0.48.0.**

- **`YY-MM-DD HH:MM`, always.** v0.48.0 dropped the year from a belief
  derived in the current one to buy back a column. The user asked for it
  back and wrote the format out — and the better reason is the second one:
  a stamp that is 11 characters for some rows and 14 for others makes the
  *claim column start in a different place down the list*. Fixed width
  beats one saved column. The browser still spells the century out; both
  forms are fixed-width, neither carries seconds.
- **Rows use the terminal they have, and `PICKER_ROW_WIDTH` is now only a
  floor.** It was a constant 72 — what fits an 80-column terminal — so a
  claim on a 160-column terminal was cut at 72 anyway. Rows are trimmed by
  the **widget**, against `scrollable_content_region` (which excludes the
  scrollbar) falling back to `content_size` and only then to the constant.
  *Not guessed:* v0.49.0's banner work already paid for guessing chrome,
  and a scrollbar moves the budget by two. A resize re-renders. **And the
  filter got better for free:** the matcher now scores the whole row rather
  than a string the formatter had already cut, so a word past the visible
  edge is findable again.
- **The door has no fold around it.** It had been given a group of its own
  purely so the header machinery would not paint a bare `▎`; once groups
  folded that became a fold around a single row whose only effect was
  hiding the way out. `ChipPicker` now renders an **ungrouped** row where
  the caller put it, with no header and no fold.
- **Each door names where it leads, and lands you there.** *This was the
  real fix.* Both rows read "open the beliefs browser" — including the one
  in the **proposals** picker, which sent a reader looking for
  approve/reject to a door labelled beliefs. The door did not say where it
  led because it led to two places at once. *Judgment call:* the tab keeps
  **both halves** — they are one session's LORE state and splitting them
  would duplicate the surface rather than clarify it — so instead the
  beliefs door reads *evidence trails, outcomes, retract*, the proposals
  door reads *approve or reject, one at a time*, the tab is renamed from
  `beliefs` to `lore` (a tab titled for one of its two halves is the same
  misleading label one level up), and each door opens it **focused on its
  own half**.

- **A leak this chip made visible, three hundred tests away from its
  cause.** `tests/test_operators.py` stages real proposals through
  `lore_remember` into the store `conftest.py` shares session-wide and
  never removed them. Invisible until something counted staged proposals —
  then every leaked proposal widened the status bar in every later test,
  pushing the last two clickable chips past the click offsets
  `tests/test_status_chips.py` computes. Two failures in a module that
  passes cleanly alone.
- Tests: 18 new in `tests/test_beliefs_browser.py`. Two older ones were
  restated rather than dropped: the year-elision test (superseded by the
  format the user asked for) and `/pending`'s scope boundary, whose real
  property — **no row in the list acts on a proposal** — has never moved
  through v0.31.0, v0.40.0 and now, and is asserted directly.
- **One behaviour is verified by probe rather than pinned by a test.**
  Re-entering an *already open* browser from a picker row and landing on
  the other half is a three-way race between ChipPicker's focus hand-off to
  the prompt, `TabbedContent.active` being a reactive, and its
  `_on_tab_pane_focused` snap-back. It works — measured end to end with a
  probe — but driving that sequence headlessly is not the same thing as a
  user clicking, so the test asserts the recorded `focus_target` (which is
  what the focus logic reads) rather than a focus outcome that was flaky to
  reproduce. Said here rather than left as a green test that proves less
  than it looks like it proves.

Three features built in parallel on separate branches and landed together, so they share one version rather than three tags on one tree. The numbers 0.45.0, 0.51.0 and 0.53.0 were reserved for them while they were in flight and were never cut; their work is below, unchanged.

## 0.56.0 — 2026-08-25

### Restored tabs continue their conversation; `/search` and `/resume` reopen any other

**Sessions can be resumed, and restored tabs resume themselves.**
Reported twice, and the second report is the one that matters: *"Do we
have a 'resume' command analog to Claude Code?"*, then — *"as long as a
tab was open, when DOXA is started again, the tab should be resumed
automatically, not via hotkey"*. Both had the same honest answer: no.
`doxa attach` reattaches to a session that is still *running*; v0.32.0's
`ArchivedSessionTab` shows an ended one *read-only*. Neither continues a
finished conversation, and the SDK option that does
(`ClaudeAgentOptions.resume`) was wired nowhere.

**Restore now means restore.** A saved tab whose daemon no longer answers
comes back as a **live session continuing its conversation** — no
gesture, no key, nothing to discover. A daemon finalizing on its linger
timer while the window is shut is the *ordinary* way a session ends, so
the read-only dead end was the ordinary result of a restart: restore
meant *display*. `doxa.cli.ended_tab_spec` asks
`history.resume_state` about each such tab before deciding its kind, and
read-only is now the **fallback**, carrying the reason it happened.
Deliberate and asked-for: `enter` on a `/search` row and `/resume` remain,
for conversations whose tab you closed months ago.

**The crux was an id space, and it was measured, not assumed.** DOXA
minted its own session uuid in `SessionEngine.__init__` and named its
LORE transcript — and therefore every `/search` row, every tab record and
every registry entry — after it. The spawned `claude` CLI, handed no id of
its own, minted a **second** uuid and wrote its store under that. Probed
live against a real CLI under `cli_isolation.spawn_env` before a line of
this was written: DOXA sid `360a8897…`, CLI sid `f45bce98…` (reported in
the init `SystemMessage`, which DOXA read for the model name and dropped
on the floor); `resume=<CLI sid>` replayed the conversation, `resume=<DOXA
sid>` failed the turn outright with `No conversation found with session
ID`. **Every id this feature is reached by was an id `--resume` would have
rejected.** A resume built on it would have been broken for every session
ever recorded, in a way no test without a live CLI could catch.

The fix is to stop having two spaces rather than to map between them:
`_build_options` now sends `ClaudeAgentOptions.session_id` — measured,
the CLI honours it exactly and writes its store under our uuid — so from
this release the id in the search list **is** the id `--resume` takes.

- **Enter on a `/search` conversation header changed meaning**, and this
  is the note it deserves. It used to toggle that header's fold, on
  reasoning the code stated outright: *"A header row is never itself an
  excerpt, so this is the ONLY thing Enter can mean here."* True of the
  meanings available then; not true once a header names something you can
  reopen. Nothing was lost — `→` already expands a fold and `←` already
  collapses it (item I bound both in v0.21.0), so the toggle keeps two
  keys and `enter` now means what it means everywhere else in this app:
  activate the highlighted row. **Enter on a snippet row is unchanged**
  and pinned by its own test: it still copies the excerpt into the
  prompt, which is what most `/search` traffic is. Clicking a row follows
  the key, as it always has.
- **Every revealed row carries when it happened.** Child rows spend the
  six blank columns they already spent on indentation on that message's
  own age instead, so an opened fold can be read in order and the excerpt
  beside it loses **nothing** — a sixteen-column ISO date on every
  snippet row would have been the regression the status bar's own history
  warns about. Session headers carry **both** clocks: the absolute date
  (orderable and citable by eye) and the age beside it (scannable), which
  they can afford because a header has no excerpt competing for the row.
  That is the whole judgment: the row with something to protect gets the
  cheap answer, the row without gets both. The age is `_fmt_age`, still
  the one age format in the app — v0.46.0's beliefs browser had already
  given it the day tier this needed (session history hits the same wall
  a four-month-old belief does: the hour tier alone renders last Tuesday
  as `168h0m`), and its `days < 10` cut-off is what makes five columns a
  real ceiling rather than a hope.
- **A new tab, not this pane.** *Judgment call, argued:* a resumed
  conversation is a different conversation from the one the active pane
  holds — its own history, cost and transcript — and taking the pane over
  would end or orphan that session on a keystroke whose stated subject
  was some other session entirely. DOXA already has a verb for "replace
  what is in this tab" (`/clear`, which says so and finalizes first) and
  one for "go somewhere else" (the repo picker's open-in-a-new-tab, whose
  mount/activate/focus order this mirrors). Resume is the second kind,
  and it is the reversible kind: `ctrl+w` undoes it, an in-pane takeover
  has no undo.
- **A running session is attached, never forked.** Resuming means handing
  `--resume` to a second CLI while the first is still alive on that
  conversation: two writers on one transcript, two daemons under one
  registry id. So the peer registry — the same reaped view `doxa attach`
  and `/sessions` read — is consulted first, and a live session is
  *attached* in a new tab with a message saying that is what happened. An
  in-process session with no daemon socket has nothing to attach to and
  is refused in words rather than quietly resumed.
- **A resumed tab shows its prior conversation.** Reusing v0.32.0's own
  machinery rather than a second copy: same `doxa.transcript` reader,
  same `mount_transcript`, same render caps and the same on-screen
  honesty when they bite. `_restore_transcript` gained one argument for
  it — a reattach may only draw from disk once its daemon has agreed to
  skip replaying its ring, and a resume has no such daemon, so the
  precondition belongs to one caller and not to the method. A model
  silently holding history the user cannot see is the wrong failure for a
  tool whose premise is auditable memory.
- **Three refusals, before anything is spawned.** `history.resume_state`
  answers "may this be resumed, and if not, why" from local file and
  registry reads only — no subprocess, nothing that can block a
  keystroke. Still running; the cwd is gone; or the CLI has no history
  under this id. That last one is **every conversation recorded before
  this release**, whose `/search` row looks exactly like a resumable
  one — so the dialog says so, naming the version and saying what still
  works (readable, searchable), rather than letting the user discover it
  one prompt into a conversation they believed they had reopened.
- **`ResumeConfirm` is the fourth member of the existing modal family**
  (`CloseWithTurnRunning`, `CompactConfirm`, `AboutDialog`): a focused
  `ModalScreen`, a title row, a body, doors that each name their own key,
  `esc` cancels. Its body **states what will happen** — new tab, history
  reloaded, prior turns re-rendered, this tab untouched — rather than
  asking "are you sure?"; on a refusal it has exactly **one** door,
  because a confirm offering a "resume" button that cannot resume is
  worse than no button. v0.28.0's defect is pre-empted rather than
  rediscovered: `height: 1` + `padding-top: 1` under Textual's box model
  draws buttons at *zero* height, present in the DOM and visible nowhere,
  and that shipped for a full release because the tests asserted the
  modal was pushed and never that anything was drawn. This one ships with
  tests asserting rendered height, hit-testability and on-screen text.
- **`/resume [session-id]`** joins the one registry every surface reads,
  so `/help`, the palette and autocomplete get it for free. Bare it
  offers the recent conversations in the shared `ChipPicker`; with an
  argument it resolves an id by prefix, and an **ambiguous** prefix is
  answered with the candidates rather than by taking the first — resuming
  the wrong conversation is not a mistake anyone notices quickly. It is
  also the only route to a resume for a single-session search result,
  which by v0.21.0's "no pointless fold" rule stays flat and has no
  header to press `enter` on. *Judgment call:* that rule was left
  standing; overturning a deliberate decision from another release to
  add an affordance a command already provides is not a trade this one
  makes.
- **Eager, not deferred, and the cost argument is the reason.** A resumed
  tab costs one *process*, not tokens: the CLI reads that conversation out
  of its own store at connect and DOXA sends nothing until you type. That
  is the same per-tab cost restore **already** pays for every tab whose
  daemon is alive — a spawn or an attach each — so deferring the spawn to
  first activation would have bought a second, subtler tab lifecycle in
  exchange for a cost the existing one already accepts. Anyone who would
  rather not pay it has `resume_restored`.
- **`resume_restored` is its own switch, not a clause of `restore_tabs`.**
  It is the one part of restore that starts a *process*, and a machine
  coming back to six restored tabs starts six of them. Off is v0.32.0
  byte for byte — read-only over the transcript, marked — and
  deliberately with **no** reason line: the setting doing what it says is
  not a failure, and explaining it would be explaining the user's own
  choice back to them.
- **A resume that cannot happen degrades to today's tab, never to an
  error or an empty pane** — plus a line naming which of the three
  reasons it was. The big one, for a while, is the last: every
  conversation DOXA recorded before this release has an id the CLI never
  knew, so those tabs come back exactly as they do now and say so. Strictly
  better than the current behaviour, never worse, which is the bar a
  fallback has to clear.
- **The restore report counts resumed tabs separately** from restored
  ones. "Resumed" is a bigger claim than "restored" — those are live
  sessions continuing conversations that had ended — and it must not hide
  inside the other count.
- **A resumed daemon never creates a worktree.** `--resume` is resolved
  by the CLI against directories keyed by the cwd a session ran in;
  substituting a freshly-created worktree would hand it a cwd the
  conversation was never recorded under. A resume enters the cwd LORE
  recorded — which *is* its worktree, when it had one.

### A condensed tool-calls fold, a timerless spinner, and a `lore` line that says what LORE holds

Three refinements to what a session shows you, all reported from using
it: the tool-calls fold was mostly chrome, a running turn had no sign of
life once its first word arrived, and the opening block's `lore` line
named a store without saying what was in it.

#### The tool-calls section, condensed

Reported verbatim: *"condense the Tool calls collapsible section: remove
the boxes around and remove the empty line in between."*

**Measured before cutting, the way v0.44.0 measured the turn body.** An
expanded three-call section cost **15 rows**, and four of them said
anything:

| rows | what they were |
|---|---|
| 1 | the `⚒ Tool calls (3)` header |
| 1 | blank — `ToolCallsSection > Contents`' top padding |
| 12 | three chips × (border top, title, border bottom, margin blank) |
| 1 | blank — the section's own trailing `margin-bottom` |

**It now costs 4**, one header and one line per call, and every one of
the four carries text. `tests/test_transcript_density.py` pins the number
by measuring `outer_size.height` and by reading the composited screen rows
back as strings — not by checking that a CSS rule exists. Eleven rows of
chrome removed from the commonest thing a turn contains; a twelve-call
turn was spending 48 rows to show twelve lines.

- **The border went because of its ratio, not its looks.** Two rows to
  draw a frame around one row of text is 3:1 chrome-to-content. `ToolChip`
  had been the deliberate exception to this transcript's unboxed rule
  since v0.13.0 ("a tool call reads as a nested artifact"); the exception
  is withdrawn, and the transcript is now unboxed all the way down.
- **What separates one chip from the next, at zero rows:** the fold arrow
  Textual already draws on every `CollapsibleTitle` (a leading glyph DOXA
  did not have to invent), one cell of indentation under the header, and
  the brightness step from the header's muted `#8A8073` to the chips'
  `#D8CDBB`. **Indentation and a glyph were chosen over a blank row
  deliberately**: a separator costing a row is paid once per chip, so a
  twelve-call turn pays it twelve times, where indentation is free at any
  count.
- **The two blank rows are two different judgment calls, and both follow
  v0.44.0's rule** — blank rows BETWEEN paragraphs are readability, blank
  rows at the END are waste. The section's trailing `margin-bottom` is
  waste: `.turn-tools` is the last thing in a turn and `TurnBlock`
  already carries `margin-bottom: 1`, so it was a second blank row at the
  end rather than a separator between anything. The `Contents` top
  padding is the closer call, and it went too: **a fold and its list are
  one unit, not two paragraphs**, and with one row per chip that pad read
  as a hole under the header rather than as breathing room.
- **One blank row inside an expanded chip is kept, and a different one is
  removed.** The blank between `ARGS:` and `RESULT:` stays — that is
  between paragraphs, and it is the whole reason the dump is legible. The
  one that went was invisible in origin: `ToolChip`'s subagent-output
  `Static` is empty on every chip that never spawned a subagent, and an
  empty `Static` is still one row. It is now hidden at zero and mounts
  itself back the moment there is narration to show, which is the same
  hide-at-zero convention `ToolCallsSection`, `ReasoningSection` and the
  status chips already follow.
- **`ReasoningSection` was left alone.** It has the same `Contents` pad,
  and cutting it would have been consistent — but reasoning is prose,
  where a leading blank reads as a paragraph break rather than a hole,
  and the report named one section. Consistency is not by itself a reason
  to restyle a surface nobody complained about.

#### A spinner while reasoning or generating

Reported: *"i would like to have a spinner while reasoning or generating
the output."*

**The naive version of this is a regression this app already paid to
shed**, and saying so is the design. `ThinkingMarker` exists because it
replaced a `LoadingIndicator` whose 16 Hz auto-refresh armed a repaint
tick on every in-flight turn; `doxa/ui/statusline.py`'s `GitLine`
documents a no-timer, no-per-frame rule; `tests/test_chrome.py` asserts
that **zero** timers are armed while a turn is in flight. A `set_interval`
spinner fails that test on the way in.

**So the spinner is driven by the delta stream instead.** A token
arriving *is* a tick: `text_delta` advances it into the `generating`
phase, `reasoning_delta` into `reasoning`, a `tool_call` into `working`.
When nothing is arriving nothing ticks — which is exactly the wanted
behaviour, because an idle DOXA has no turn, no deltas and no repaints.
There is no clock anywhere in it.

- **Measured, both ends.** Idle, with 20 completed turns of scrollback
  and a 20-second sampling window: **0.14–0.17 s CPU after** against
  **0.18–0.22 s before** — the same range, noise-dominated, no measurable
  idle cost, and `ClockChip` remains the only armed timer in the app on
  both sides. In flight, a 700-delta answer: **0.39–0.41 s CPU after**
  against **0.37–0.69 s before**, also within noise.
- **The other failure mode was a repaint rate set by the model.** Ticking
  on every delta would have traded a fixed 16 Hz for something worse — a
  700-delta answer buying 700 repaints. `SPINNER_MIN_INTERVAL` floors it
  at 0.1 s, and that same 700-delta answer measurably advances the glyph
  **4 times**. A phase CHANGE always gets through the floor: the switch
  from reasoning to generating is the information, not the motion.
- **No third widget saying "working".** `ThinkingMarker` already said it;
  it has been given the whole turn instead of its first second. **This
  reverses a v0.25.0 decision**, deliberately: that release had the first
  `reasoning_delta` hide the marker, on the grounds that a live
  "Reasoning (N chars)" header is itself the sign of life. It is — but
  only while reasoning is what is happening, and a header whose count has
  stopped moving reads exactly like a finished one. The phase after it, a
  streaming answer, offers no progress signal at all, because the text is
  what the reader is trying to read.
  `test_reasoning_arrival_hides_the_thinking_marker` was rewritten rather
  than deleted, and carries the argument.
- **The marker moved to the bottom of the turn.** A spinner nobody can
  see is not a spinner: the block list scrolls to the end after every
  event, so a marker pinned above a streaming answer leaves the viewport
  inside a paragraph. It now trails the output — "here is what arrived,
  and here is DOXA still working".
- **Braille glyphs (`⠋⠙⠹…`), not one of DOXA's own marks.** `▎ ✻ ⚒ ⧉` all
  *name a kind of block*; reusing one as motion would make a running turn
  look like a section header. The braille cycle is also one cell wide in
  every font in wide use, so the label does not jitter sideways on each
  tick.
- **It disappears on every exit.** turn_done, the error path in
  `_run_turn` (a refused turn, a dropped daemon connection) and restore
  all route through the same `mark_done` → `hide_thinking`, which also
  reasserts `auto_refresh = None`. Each is asserted as a pair — that the
  marker *ran*, and then went — because "gone" on its own also passes on
  a marker that never showed.
- **Gone means gone, and on a restore it means never-shown.** `advance()`
  is a no-op on a hidden marker: the peer pump replays engine events, so a
  delta can be routed at a turn *after* its turn_done, and that must not
  raise a spinner on a turn which has already printed its cost. And
  `mount_transcript` hides the marker *before* writing the first restored
  word rather than only afterwards — restore replays a finished answer
  through the same `append_text` a live turn uses, so without that the
  scrollback would tick every restored turn into "generating" on the way
  past.

#### The `lore` line says what LORE holds

Reported: *"the 'lore' line in the status/welcome box on startup should
also show how many pending, how many in user/project context and how full
each one is."* It read `lore  /home/…/.claude/lore · 518 beliefs`; it now
also carries `· 3 pending · user 14 entries 63% · project 9 entries 39%`.

**Nothing here is newly derived.** The percentages are v0.44.0's
`labels.memory_fill` — the exact character count read from the file
`lore_core` itself writes, cached on mtime — so this line and the status
bar's `mem u63% p39%` chip cannot quote different numbers at each other.
The entry counts go through `lore_core`'s own `read_entries` over that
same file, because counting `- ` lines in DOXA would be reimplementing
LORE's storage format, which is how two readers of one file drift apart.
The staged count is v0.31.0's paged `list_pending`.

- **The project slug resolves through `peers.main_repo_root_of`**, not
  the raw cwd. That is the v0.47.0 fix, and reproducing its bug in a new
  place was the live risk here: every session runs in a worktree, a
  worktree's own slug owns no `MEMORY.md`, and the project half would
  have silently vanished for the normal case exactly as it did in the
  memory chip. The guard is a real git repo with a real worktree, not a
  stubbed mapping — a mocked one passes on the broken code.
- **One socket round trip, at boot, and nowhere else.** The opening block
  is drawn once, before the first prompt, on a pane that has just spent a
  connect and a git subprocess; `_refresh_status` runs on every peer event
  and every turn-done under the rule `GitLine` documents. So `_boot`
  counts the staged proposals once into `_pending_count`, and the line
  reads that. `test_derive.py`'s cost assertion was tightened rather than
  relaxed: it now pins the count at exactly one after boot **and** that
  five status refreshes do not add a sixth.
- **`0 pending` is stated, not hidden.** Hide-at-zero is the status bar's
  convention, where a chip competes for width. This is a boot report, and
  a reader who cannot tell a clear queue from a failed lookup has been
  told less than nothing. A scope with no file on disk is still omitted —
  `project 0 entries 0%` would be a measurement nobody took, and this
  block's own rule is that absent fields are omitted, never invented.
- **A daemon that cannot answer costs the fact, never the boot.** The
  count falls out of the line and everything else stays.

### What broke, shown inside DOXA, highlighted — and never fatally by default

**DOXA fails invisibly or fatally, rarely legibly.** Four defects reached
the user in one day and not one of them arrived as an error they could
read. A `TimeoutError` out of `textual_image`, raised while Textual was
*painting* a widget, killed the app to a bare terminal traceback. The
needs-input dialog stopped answering keys and said nothing — the session
simply wedged. Server-tool results vanished with no trace, so there was no
way to tell whether a web search had happened at all. The memory chip drew
half of itself and never mentioned the other half. Four bugs, one
property, and this release fixes the property.

- **An error block in the transcript.** Its own kind, on the rule
  `ShellBlock` established: a failure must never be mistakable for the
  assistant's words or for doxa's ordinary chatter, so it wears a red left
  rule — `#D9534F`, this app's one "stop and look" colour — and neither
  the `▎` turn accent nor the `▎ doxa` prefix. One line saying what broke
  and *who* broke it; the whole scrubbed traceback behind a fold that
  starts **collapsed**, the same `Collapsible` pattern `ToolCallsSection`
  and `ReasoningSection` already use. Seeing that something broke must not
  cost a wall of text, and reaching all of it must not cost more than a
  keystroke.
- **Caught at the boundary that actually fires — which turned out to be
  one boundary.** Textual 5.3.0 funnels *everything* through
  `App._handle_exception`: message handlers, `compose`/mount, idle
  handlers, `call_later` callbacks, the compositor's paint loop, and a
  failed worker (wrapped in `WorkerFailed`, because `run_worker`'s
  `exit_on_error` defaults to True). Its own docstring says "Always
  results in the app exiting". So there is exactly one method to override,
  and it is overridden.
- **Worker deaths were the loudest of the quiet failures.** DOXA starts a
  worker for nearly everything — `_boot`, `_peer_pump`, every slash
  command, the update check — and a worker that raised took the whole
  window with it, indistinguishable from "DOXA crashed". Now it is one
  block, and the session stays usable.
- **Render-time containment, because Textual offers none.**
  `textual/_compositor.py` contains no `except` at all: a widget that
  raises while rendering does not fail alone, it takes the whole *frame*,
  every frame, forever. Merely surviving the raise would leave an app
  alive and unable to draw. So the culprit widget is read off the
  traceback and **quarantined** — `display = False`, which ends the loop
  at its source — and the block says which widget was hidden and why.
  Half a widget silently missing is one of the four defects above; a whole
  widget silently missing would be the same defect wearing a fix. Render
  failures only: hiding an arbitrary widget because a keystroke handler
  threw would be a second defect, not containment.
- **Scrubbed before display, and scrubbed at the source.**
  `lore_core.scrub.scrub_secrets` runs over every traceback at
  construction rather than at each of the three doors out of the process,
  so a fourth door added later does not start out leaking. Frame locals
  are dropped entirely — Textual's own fatal path prints
  `Traceback(show_locals=True)`, and the locals are where a credential
  actually lives. A crash report that leaks a token is a worse defect than
  the crash it describes.
- **Fatal is still possible, and still seen.** A failure with no surface
  to draw itself on, or one that repeats without end, exits — printing the
  same information to the terminal on the way out. That report is
  `/about`'s own block (`version.about_text`): version, sha, interpreter,
  textual, agent SDK, which `lore_core` answered and from where, platform,
  keyboard protocol, config path. A user who has to file a bug should not
  have to reconstruct any of that from a Python traceback, and the crash
  report is now byte-for-byte what the about dialog's copy door already
  puts on the clipboard.
- **A log to point at: `~/.doxa/errors.log`.** Bounded by size with one
  previous generation (`errors.log.1`), 256 KiB each — so the whole
  on-disk cost is half a megabyte and it needs no sweeper, no timer and no
  setting. Written *after* the block is mounted, deliberately: a read-only
  home directory may cost the persisted copy, never the visible one.
- **One swallow removed, and it is defect two of the four.** Delivering
  the user's answer to a blocking question was wrapped in
  `contextlib.suppress(Exception)` — and by the time it ran the popup had
  already closed and the needs-input flag had already cleared, so a failed
  delivery left the agent blocked forever on a question the user *had*
  answered. A wedged session that looked exactly like an idle one. It now
  reports, and says what to do about it.
- **Repeats collapse; they do not accumulate.** A widget raising on every
  paint becomes one block with a `×N` tally — a title rewrite, as cheap as
  `ToolCallsSection`'s own live count — and only the first is written to
  the log. Past 25 the failure is by definition not recoverable, and the
  app exits with a report rather than spinning.
- **Nothing animates and nothing polls.** No timer is armed and no
  per-frame cost is added: this surface does work only once something has
  already gone wrong. The idle-CPU regression `GitLine` and
  `_refresh_status` exist to warn about is not reopened, and there is a
  test that says so.

**Also a prerequisite for the plugin loader, which is why it is shaped the
way it is.** `docs/plugin-api.md`'s failure policy promises three states —
not loaded, disabled for the run, over its `text()` time budget — and none
of them had a mechanism. So a failure carries an **origin** (pass
`origin="plugin:jira"` and the block says so; omit it and the deepest
non-infrastructure frame is read off the traceback, which already tells
DOXA apart from `lore_core` apart from a third-party package — the
reported render crash attributes to `textual_image`, which is a different
user action from a DOXA bug). Failing is **state**
(`app.failures.failed(origin)`), not a message that scrolls away. And the
third state is not an exception at all, so the record is a `Failure` with
a `kind`, and a chip overrunning its budget lands in the same block as a
crash. The loader, the allowlist and any disabling are explicitly *not* in
this release — there is nothing loadable to disable — and the doc now
states exactly what a future loader calls.

*Judgment call:* the surface is named for **failure** and not for
exception, throughout. The naming outlives the release, a time-budget
overrun is a broken promise rather than a raise, and a surface that could
only hold exceptions would have sent that third state back to nowhere.

*Layering, for the record:* the specific cause of the reported crash —
textual-image probing stdin for the terminal's cell size during a paint at
all — is fixed where it belongs, in `doxa.images`/`doxa.banner`. This
release owns the general containment: whatever the cause, a render raise
must not be fatal. The two do not overlap, and the crash is reproduced as
a test here in the shape it arrived in.

*Not swept in:* an audit of every `except Exception` and
`contextlib.suppress` in `doxa/` found roughly a dozen more that hide real
failures — a suppressed `finalize()` on stop and on detach, a suppressed
`index_live` that reports "could not index" as "nothing to index", two
`_on_can_use_tool` paths that *allow* a call when the permission prompt
itself breaks, a shell reader whose lost output is presented as no output,
and `list_beliefs` rendering a broken store as "you have no beliefs".
They are reported rather than quietly rerouted into the new surface:
routing them without saying which would be the same silence in a new coat.

## 0.55.0 — 2026-08-25

**A crash on Linux Mint's default terminal, and it was DOXA's own probe
that caused it.** Reported as *"doxa crashed while using it"*, with a
`TimeoutError: Timeout waiting for data` traceback, in a session restoring
a tab. Reproduced in a pty configured the way VTE reports itself, and the
mechanism is not what the traceback makes it look like.

- **Nothing was raising.** `textual-image` probes the terminal's cell size
  with `ESC[16t`. VTE — GNOME Terminal, and so Mint's default — does not
  implement that window-op and never answers, so the read times out.
  Upstream *catches* its own timeout, which is correct, and then reports
  it with `logger.warning(..., exc_info=e)`. With no logging configured,
  Python's last-resort handler writes WARNING and above to **stderr**. In
  a full-screen app stderr *is* the screen, so a handled fallback printed
  a message and a full traceback over the TUI exactly as it was taking the
  terminal over. Indistinguishable from a crash from where the user sat.
- **The caller was `DoxaApp.__init__`** — DOXA's own pre-`App.run()` cell
  probe, added in v0.41.0. The ordering in the report proves it: the
  `doxa: restoring 1 tab(s)…` line is printed by the CLI before the app
  starts, and the warning follows it immediately. *The late-widget theory
  was wrong and the measurement says so*: instrumented in a pty, the
  library's cache is populated before `App.run()` and `/img` afterwards
  never re-probes.
- **Fixed three ways, because one of them is only the symptom.** The
  `textual_image` logger gets a `NullHandler` and `propagate = False` — a
  library's idea of a warning is a TUI's idea of corrupted output, and
  what the user actually needs from that probe is a *reported* value,
  which `/img` already prints and already labels as defaulted. The
  library's cell-size cache is now **always** seeded, even when our probe
  raised or never ran, so no later caller can re-probe: every later caller
  is a widget painting itself, after `App.run()`, where Textual owns stdin
  and the reply can never arrive — a probe guaranteed to burn its timeout
  and fail, from inside a render.
- **And `widget_for`'s promise now covers painting.** It has always said
  "never an exception"; that covered *construction* only, which is the
  easy half. Textual asks a widget for its width, its height and its
  content long afterwards, from the compositor, with no caller left to
  catch anything — and this library's `get_cell_size` divides by a
  terminal-reported column count and raises `ZeroDivisionError` when that
  count is zero, which is measurable in a pty. Those three methods are now
  wrapped, and so is the Rich renderable `render()` returns, since that is
  where the drawing actually happens. A failure degrades to the
  `[image: …]` line. The height fallback is 1 and never 0: a widget
  measuring to zero rows is in the DOM, invisible on screen, and passes
  every structural assertion — the v0.28.0 defect, which images make cheap
  to reproduce.

**"quite pixelated — then i would prefer to just show it as unicode/ASCI
blocks", "and the real logo png just in terminals that support it."** The
opening banner's ladder is inverted. A drawn mark and plain text are now
the normal path; `logo.png` is the exception, for terminals with a real
pixel protocol.

The report arrived first as "the logo image is not rendering", which cost
this release a detour. A screenshot settled it: the logo *was* rendering —
mark, wordmark, colour, correctly placed — on a stock Linux terminal at
the half-block tier. The complaint was quality, and it was correct.

- **The arithmetic behind it.** Half-block is not a pixel protocol with
  fewer pixels; it is a 2×-vertical approximation made of `▀`. Six rows of
  it is twelve vertical samples for a 238-row image. A downscale
  *averages*, and averaging at that ratio is mush — the asset's own
  strapline came out as a grey smear. v0.41.0 said as much in its own
  changelog and shipped it anyway, on the theory that the mark carried the
  banner. In front of the person who sees it every launch, it did not.
- **The drawn form, which is now the common one.** A triangle authored
  cell by cell — every cell *chosen*, none averaged, so it is exactly as
  sharp as the font — with the wordmark and strapline beside it as
  **plain text**. Four rows, two fewer than the raster it replaces.
  *Judgment call:* the wordmark is no longer glyph art either. v0.41.0
  drew "DOXA" in blocks; four ordinary capitals are simply legible where
  stylised ones are something to squint at, and the font already knows how
  to draw an A.
- **The mark was chosen by drawing candidates and looking at them**, in a
  real monospace font at true cell metrics, then again through the actual
  renderer. Two were rejected on sight. A **ring** is impossible at this
  size — a one-subpixel outline renders as horizontal bars, not a circle.
  A triangle built from `▀▄█` alone renders as a **stepped pyramid**,
  because every row is a solid rectangle and the eye reads three stacked
  bars. The quadrant triangles `◢`/`◣` are what give the edge a real
  slope. Smaller and sharp beat bigger and mushy, which is the same rule
  the rest of the module follows.
- **Two known limitations, both shown to the user and both accepted.**
  First: at four rows the mark reads as a **stepped, stacked shape**
  rather than as the smooth triangle-in-a-ring of the PNG — there are not
  enough rows for the ring, and the tiers are visible. The alternative
  offered was dropping the mark and keeping the wordmark alone; the glyph
  was chosen. Second: `◢`/`◣` are U+25E2/U+25E3, in Unicode's Geometric
  Shapes rather than the Block Elements the rest of the drawing uses, so a
  font without Geometric Shapes coverage shows **tofu** where the plain
  `▀▄█` set would not. That is a real cost of the sloped edge and is
  written down here rather than discovered later; `boot_banner=image` (on
  a pixel tier) or `off` are the ways out.
- **The raster is kept where it is genuinely good.** `kgp` and `sixel`
  carry an actual bitmap, and there `logo.png` looks like the brand asset
  it is. Nothing about that path changed.
- **`boot_banner` becomes a choice: `auto` · `blocks` · `image` · `off`.**
  `auto` is the rule above; `blocks` pins the drawn form everywhere;
  `image` pins the raster wherever any pixel tier exists at all, which is
  v0.41.0's behaviour kept reachable; `off` removes the banner. The knob
  shipped as a bool, so `1` and `0` still read as `auto` and `off` — a
  `config.toml` written by the old settings modal keeps meaning what it
  meant, and a test pins that.
- **No new dependency, no figlet.** The mark is a tuple of four strings.

Three genuine defects surfaced while chasing the wrong diagnosis, and are
fixed here rather than left for the next report:

- **The drawn banner could be silently clipped, and no height assertion
  could ever have caught it.** Its CSS pinned it to three rows, so content
  too wide for the column was *clipped to exactly the height a passing
  test expected*. The fit was also computed from the terminal width minus
  a guessed chrome constant, and the real chrome is not constant — the
  transcript's scrollbar moves it by two. It is now fitted to the widget's
  own `content_size`, dropping the strapline, then the mark, as the column
  narrows. The test asserts that no rendered row is wider than the column
  it goes into; asserting height is what let this through.
- **The banner path could raise, and would have taken the pane boot with
  it.** `doxa.images.widget_for` is documented never to raise, but the
  crop-and-flatten step added in v0.41.0 is DOXA's own code on the near
  side of that guarantee and runs inside `BootBanner.compose`. Both
  `from PIL import Image` and the asset lookup sat *outside* the `try`.
  Pillow is a declared dependency, so the import "cannot" fail — precisely
  the class of assumption that produces a bug report, and it cost one
  indent to stop making it.
- **When the raster is *asked for* and cannot be drawn, the banner says
  so** in one muted line naming the cause and pointing at `/img`. Only
  under `image`, never under `auto`: announcing "logo not drawn" over a
  banner drawing exactly as designed would be noise on most sessions.

Ruled out by measurement, recorded so the next report starts further
along: **not** an app-size race — under a real Textual driver in a pty the
banner is constructed with the true terminal width. **Not** packaging —
built as a wheel, installed into a clean virtualenv and run from a
directory with no checkout on `sys.path`, the asset resolves and prepares
to 928×238. **Not** a mount failure — the banner mounts with non-zero
height on every tier and width tested, 20 columns upward. Worth knowing
for remote sessions: `textual-image` queries for kitty and sixel support
with a **0.1 s** timeout where DOXA's own keyboard probe deliberately uses
0.3 s, documented as "long enough for a round trip through tmux and an ssh
hop" — so a remote terminal is likely to have its graphics query time out
and be recorded as half-block, which as of this release is a tier that
draws the mark and looks right.

## 0.50.0 — 2026-08-25

**The permission-mode cycler now reaches `auto` and `bypassPermissions`,
and the chip is Claude Code's, not DOXA's.** Three user decisions, taken
in that order, two of which overrule what v0.42.0 shipped and argued for.

**On the two that were overruled, plainly, because the reasoning should
survive.** v0.42.0 kept `auto`, `bypassPermissions` and `dontAsk` off the
hotkey and behind a confirmation, on the argument that a key you tap to
move between conveniences must not be able to land on a mode where
nothing asks you. The user read that and asked for `auto` on the cycler;
then, separately, for `bypassPermissions` as well. Both are built. What
they mean in practice:

- **`auto`** — a model classifier approves or denies each tool call
  instead of you. A gate still exists; the person behind it does not.
- **`bypassPermissions`** — every tool call runs unapproved at your full
  privileges. Nothing checks, and nothing asks.

`dontAsk` was **not** requested and is **not** on the cycler. Two explicit
decisions about two named modes are not a general licence over the sixth,
and reading one into them would be inventing consent rather than
following it. It stays on `/mode dontAsk` behind its confirmation. *If
that inconsistency is unwanted, it is a one-line change — but it is the
user's line, not this branch's.*

**The invariant that used to say "no keystroke reaches a mode where
nothing asks" is now false by design.** It was not deleted and it was not
weakened into nothing; it was rewritten as what is still true and still
worth guarding, which is a sharper claim than the old one:

- the set a keystroke can reach is **exactly** `CYCLE_MODES` — not a
  subset, not a superset — asserted both against the constant and against
  the five names spelled out, so editing the constant alone cannot make
  the test pass;
- `dontAsk` is unreachable by any number of presses from any starting
  point;
- `next_cycle_mode` is a **total function** over that set: no input, state
  or configuration produces anything outside it.

That still stops a future edit from quietly putting a sixth mode on the
keyboard, and it makes the set something somebody has to change on
purpose. Cycle order is most-oversight-to-least and wraps home —
`default → acceptEdits → plan → auto → bypassPermissions → default` — so
one more press is always the way *out* of the most permissive mode rather
than a dead end. The first four are, in that order, exactly Claude Code's
own cycler; `bypassPermissions` is appended rather than inserted because
the CLI's own permissiveness ranking puts it at the top.

**The chip now uses Claude Code's glyphs and colours, and they were read
out of the installed CLI rather than guessed.** A safety indicator whose
colour means one thing in one client and something else in another is
worse than no convention at all. `claude` 2.1.228 is a bun-compiled ELF;
its canonical permission-mode table survives in the bundle as a plain
object literal keyed by the same mode names the SDK uses, which makes
this a lookup rather than an interpretation:

| mode | glyph | colour name | dark-theme value |
|---|---|---|---|
| `default` | `⏸` U+23F8 | `inactive` | `#999999` |
| `plan` | `⏸` U+23F8 | `planMode` | `#48968C` |
| `acceptEdits` | `⏵⏵` U+23F5×2 | `autoAccept` | `#AF87FF` |
| `auto` | `⏵⏵` | `warning` | `#FFC107` |
| `bypassPermissions` | `⏵⏵` | `error` | `#FF6B80` |
| `dontAsk` | `⏵⏵` | `error` | `#FF6B80` |

The glyph division is the real one: `⏸` for the two modes that pause and
ask, `⏵⏵` for the four that run something without stopping. The colour
*names* resolve through four theme tables in the same bundle — light,
light colour-blind, dark, dark colour-blind; the dark one is identifiable
without guessing because its `text` is white and its `claude` is
rgb(215,119,87), the orange DOXA's own accent already tracks. Contrast
was measured, not assumed: against the status bar's `#221F1A` (which
stays opaque in **both** background modes — the bar does not read
`$doxa-base`) every value lands between **4.70 and 10.07**, all of them
above WCAG AA and all better than DOXA's own `CTX_RED` at 4.14. A test
pins each hex and each glyph literally, so a future `claude` changing its
palette is something DOXA finds out about.

**One deliberate divergence from what was measured, and it is a product
difference rather than taste.** Claude Code's cycler has four entries and
cannot reach `bypassPermissions` at all; DOXA's now can. An "error red"
calibrated for a mode you had to go out of your way to select is not
calibrated for one a mistyped keystroke lands on. So `bypassPermissions`
and `dontAsk` keep the measured hue and glyph and gain one step of
weight — **bold** — which costs no columns and survives every width.
Everything else is the measured value untouched.

- **The chip moved to first on the row, ahead of the model.** The status
  bar has no overflow behaviour: a chip that does not fit is not
  truncated or scrolled, it is *gone*. Position is therefore the only
  real guarantee, and this is the one chip that must never be what falls
  off the end. Verified down to 40 columns, where it shrinks to
  `⏵⏵ mode:bypass` and stays put while everything to its right goes.
- **Entering a mode that stops asking now writes a line in the
  transcript**, not just a chip. The chip is persistent but peripheral,
  and a user who did not mean to press the key is by definition not
  looking at the corner of the screen; a transcript block is transient
  but lands in the same column as the work. It names what *stopped* —
  "there is nothing left to decline" — and says the mode was not saved.
  A merely-narrowing switch stays a quiet one-liner.
- **`/mode auto` and `/mode bypassPermissions` no longer confirm.** A
  dialog in front of a mode a keystroke already reaches cannot prevent
  anything, and after the second dismissal it teaches people to hit the
  accepting key without reading. `dontAsk` still confirms.
- **The persisted default did not move, and this is the one line held
  against the trend.** `permission_mode` / `DOXA_PERMISSION_MODE` still
  accepts only `default`, `acceptEdits` and `plan`. Cycling into bypass is
  per-session, visible and announced, in a session someone is looking at.
  A *stored* bypass is silent, unbounded in time, and applies to sessions
  opened in repositories nobody has read yet, possibly by somebody who
  never set it. Those are different decisions and only the first was
  made. `PERSISTABLE_MODES` is now its own constant precisely because it
  is no longer the same set as anything else.

Four sets now name what used to be two, because the questions came apart:
`CYCLE_MODES` (what a keystroke reaches), `GATED_MODES` (what confirms),
`PERSISTABLE_MODES` (what a file may store), `UNASKED_MODES` (what the
chip must shout about). Conflating any two would be a bug.

41 tests in `tests/test_permission_mode.py`, of which **13 were verified
failing** against v0.47.0 — every new or changed assertion. The rest are
unchanged behaviour this release did not touch. Suite: **1132 passed** on the
tree this lands on.

## 0.48.0 — 2026-08-25

**The beliefs chip becomes a surface you can work in.** Five things the
user asked for after living with v0.46.0: the minute a belief was derived,
tested beliefs at the top, the browser one selection away, something to
*do* on a belief row, and scope groups that fold with their counts in the
header.

- **`HH:MM`, and what gave way for it.** v0.46.0 showed the date alone and
  argued the clock was noise "in a column that has to fit beside a claim".
  The user overruled the first half; the second half is why the fix is not
  simply six wider columns. `PICKER_ROW_WIDTH` is 72 because that is what
  fits an 80-column terminal inside a bordered dropdown, and the row string
  is *exactly* what ChipPicker's type-to-filter matches — so columns taken
  for a stamp are columns of claim that stop being findable. *Judgment
  call:* the picker drops the **year** from a belief derived in the current
  one. `08-25 14:23` costs one column more than `2026-08-25` did; a belief
  from an earlier year keeps its year and costs the full six, which is
  exactly where the year is worth having. The same convention `ls -l` has
  used for decades. Seconds are still noise and are still absent. The
  browser's own rows have the width and always render
  `YYYY-MM-DD HH:MM` — both surfaces show the clock, only the picker infers
  the year.
- **Tested-first in the picker — a real defect, not a new feature.**
  `belief_sort_key` was written for the browser in v0.46.0 and the picker
  never called it: `chips.py` sorted by scope label *alone*. Tested beliefs
  now rise to the top of their group there too, most recently tested first,
  never-tested following as a stable bucket — without disturbing the
  contiguous grouping the header insertion depends on.
- **Selecting the browse row already opened the browser in a tab, and
  still does.** Verified rather than assumed: that behaviour shipped in
  v0.46.0 and has a test pinning it. The user was confirming an
  expectation, not reporting a bug. Nothing changed, and the test is now
  named for the expectation so it stays confirmed.

**Scope groups fold, and say how much they are hiding.** `project (412
beliefs, 3 tested)` — count from the widget, which has the rows; the
"tested" note from the caller, because how many beliefs reality has tested
is a fact about beliefs and a generic dropdown has no business knowing it.
The labels are whatever `_belief_scope_label` emits, so `user model` stays
its own group rather than being folded into plain `user`.

- **Folded by default on a large list, expanded on a small one.**
  *Judgment call.* 635 active beliefs is the reported store; a picker that
  opens fully expanded is a wall, not a glance, and that is the thing the
  fold exists to fix. But folding three rows behind three headers is
  strictly worse than showing them, so below `#chip-picker`'s own
  `max-height` every group opens and the feature is invisible. The
  threshold is the widget's real number, not a taste.
- **The counts are what make a folded list an answer.** `project (412
  beliefs) · user (83 beliefs) · user model (12 beliefs)` *is* the rough
  shape of the store — which is what the chip is for — and one selection
  opens the part you came for.
- **Group ORDER stays alphabetical and does not react to test counts.**
  *Judgment call, and a deliberate refusal.* A group that outranked another
  because it happened to gain an outcome would move between openings, and
  this house's own rule is that a status surface whose contents shift is
  worse than one that omits something. Scope is a taxonomy, not a ranking.
  The tested count goes in the header instead, where it informs without
  making the layout jump; tested-first stays a *within*-group ordering,
  which is where it was asked for.
- **Filtering is untouched by the fold, by construction.** The matcher has
  always scored the complete row set rather than what is on screen, and a
  typed filter already dropped group headers entirely — so a folded view is
  simply not the view being filtered. A user typing a word finds a belief
  inside a folded group with no auto-expand rule, no re-fold bookkeeping
  and no special case, and clearing the filter returns to exactly the fold
  state that was there before. Asserted.
- **The door never folds.** The "open the beliefs browser" row lives in its
  own group, and a group of doors rather than data starts open — caught by
  building it: on a 635-belief store the first version hid the way out of
  the picker behind the very fold that made the picker usable.

**And the browse row is *more* necessary now, not less.** A navigable
picker settles what each surface is for: the picker answers *what is in
here and roughly how much*, and the browser owns the things a dropdown
still cannot render — evidence trails, tooltips carrying the whole claim,
and two independent controls on one row.

**What a belief row can actually DO — and why it is not "approve".** The
user asked for an Approve/Reject button on every belief row. Those two are
not operations on a belief, and shipping them under those names would have
taught the wrong model of the system:

- **Approving applies a staged PROPOSAL** — an entry that does not exist in
  the store yet. Every proposal already carries approve and reject, per
  row, in the browser (v0.46.0).
- **A belief is a claim already in the store and already steering the
  model.** What LORE can do to one is record what reality did to it
  (`belief_outcomes`: `confirmed` / `contradicted` / `stale`) and end it
  (`retract`). So those are the verbs, spelled LORE's way — read off the
  `CHECK` constraint and off the transition `pending.apply_item` performs
  for an approved retract, not invented here.

Recording an outcome is the high-value one and the reason the affordance is
worth the keystroke: **97.6% of a live store has never been tested by
anything**, and every `calibrated_confidence` in the product reads a curve
built on that nothing. `source="user"` is not a label DOXA chose — it is
what `lore_core.beliefs.cmd_outcome`, LORE's own *"manual/pushback path"*,
passes, and a human selecting a verdict in a DOXA row **is** that path. A
test reads it back out of the store.

- **In the browser: real controls, one per row.** `✓ confirmed`,
  `✗ contradicted`, `⌛ stale`, `⌫ retract…`, each in the colour its
  verdict already wears, plus `c` / `x` / `d` / `Esc` on the focused row.
  This is the surface that *can* have a button per row — one widget per row
  is why it exists.
- **In the dropdown: the row's actions become the row set.** *Judgment
  call.* `ChipPicker` is an `OptionList`, and an `Option` has no widgets, no
  tooltip and exactly one click target — a button per row is not something
  it can be made to do without rebuilding the widget five other pickers
  share. What it *can* do is reopen itself against a new row set, which is
  the pattern the repo picker has used to descend a directory since
  v0.22.0. So selecting a belief opens **that belief's** named verbs. That
  shape is the safer one anyway rather than a consolation for the wrong
  one: a dropdown row is one Enter from whatever the highlight is sitting
  on, which makes it a *more* accidental surface than a full-height tab,
  not less — so nothing here acts on the belief you selected; it shows you
  what can be done to it.
- **Retract arms, on both surfaces.** It is the destructive verb: the
  belief leaves the working set and the model's context. The browser row
  arms and repaints; the dropdown re-words the row and requires a second,
  differently-worded selection. An outcome appends to a ledger and can be
  answered by the opposite verdict tomorrow; a retraction takes the belief
  out today. Retracting is **not** deleting — the row survives with
  `status='retracted'` and its evidence and outcome ledger intact, asserted.
- **LORE's dormancy trigger fires and the user is told.** `record_outcome`
  retires a claim after `CONTRADICTIONS_TO_DORMANT` contradictions. DOXA
  drives that function rather than routing around it, so the second
  contradiction retires the belief exactly as `lore outcome` would — and
  the reply says so instead of a row quietly vanishing.
- **One belief per call, no bulk form.** No list parameter on either
  engine, no bulk RPC, no multi-select, under any spelling — asserted at the
  API and at the protocol, the same way approve and reject already are.
- **A narrower capability gate than approving, deliberately.** *Judgment
  call.* `lore_write_state` requires LORE 0.36.0 because approving a
  proposal WRITES an entry and an entry with no `via` label is what that
  gate exists to prevent. These write somewhere else: an outcome is a row
  in `belief_outcomes`, which has carried its own provenance in `source`
  and `agent` since the ledger landed — well before 0.36.0 — and a retract
  is a status transition on a row that already exists, creating nothing for
  a provenance column to label. Gating them on 0.36.0 would refuse a
  perfectly recordable outcome on a store that can record it, which is a
  different dishonesty from the one that gate prevents. New
  `belief_action_state` asks the only question that matters, measured off
  the API: are `record_outcome` and `belief_supersede` here to be called.
  Both surfaces degrade to read-only with their own banner and their own
  reason when they are not.

- Tests: 25 new — 24 in `tests/test_beliefs_browser.py` (76 there in
  total) and one in `tests/test_daemon.py` — each verified failing against
  `origin/main`.
  Folding, counts, the tested-first order, filtering reaching a folded
  belief, the door never folding, both arming paths, and the outcome and
  retract round trips read back out of the real store.
- Three tests in `tests/test_status_chips.py` needed updating rather than
  fixing, and both changes are the point of the release: a group header now
  carries a row id because folding is its affordance (it was a disabled
  separator with an empty one), and selecting a belief opens that belief's
  actions rather than spilling its claim — "show the full claim" is the
  first of them, so the old behaviour is one selection away and the tests
  now walk that path.
- **A race caught by building it.** Reopening `ChipPicker` from inside a
  row callback races Textual's queued `Blur` delivery and closes the picker
  right back. `_select_repo_row` documented this in v0.22.0 and fixes it
  with `call_after_refresh`; the belief action menu needed the same fix and
  now carries the same note pointing at it.

## 0.47.0 — 2026-08-25

Three workstreams that finished together and ship as one release: the
permission-mode surface below, the needs-input/server-tool defect fix
(originally numbered 0.43.0, section further down), and two status-line
fixes found by using it — the project memory-fill that vanished on every
worktree session, and a release codename rendering as a subscription plan.

### Permission mode — 2026-08-25

**The session now says whether it is still asking you.** Claude Code has
an indicator for its permission mode and a key that cycles it; DOXA had
neither, so "does this session stop before it edits my files?" was a
question with no answer anywhere on screen. This adds the chip, the
hotkey and `/mode` — and, because three of the six modes turn the
approval gate off, a line down the middle of them.

**On the keycap, because it is not the one that was asked for.** The
request was `Ctrl+Tab`. `doxa/keyboard.py` — this project's own
measurement of what a terminal can physically send, shipped in v0.39.0 —
answers `unreachable_under_legacy('ctrl+tab') → True`: under the legacy
key encoding, modified `Tab` carries no modifier information, so no byte
exists for it and a binding on it never fires. `unreachable_under_legacy
('shift+tab') → False` — back-tab (`CSI Z`) predates the whole problem
and every terminal sends it. Shipping `Ctrl+Tab` alone would have
produced the exact failure v0.39.0 exists to close: a documented key that
does nothing, silently, for everyone outside a kitty-protocol terminal.
That is also, almost certainly, why Claude Code uses Shift+Tab. So
**Shift+Tab is the primary binding**, and **Ctrl+Tab is bound as well**
rather than dropped — it costs one row, it works wherever the terminal
supports it, and `/help` already marks it `✗` with a footnote where it
does not. `tests/test_keyboard.py` now pins that mark as part of the
deal: the second binding is only defensible because the app says out loud
where it fails.

- **The cycle covers three modes and cannot reach the other three.**
  `default → acceptEdits → plan → default`, the same three surfaces
  Claude Code's own Shift+Tab walks. What separates them from
  `bypassPermissions`, `auto` and `dontAsk` is not how advanced they are,
  it is whether the approval gate still reaches *you*: `acceptEdits`
  widens only file edits (which git can undo), `plan` narrows, and both
  still raise DOXA's permission dialog for everything else. The other
  three each remove the human from a loop they are in today — one checks
  nothing, one puts a model classifier where the person was, one denies
  silently instead of asking. A key tapped to move between conveniences
  must not be able to land on any of them, so `engine.next_cycle_mode` is
  a **total function over the safe three**: no input, no configuration
  and no state makes it return a gated mode. That is the misclick
  asymmetry v0.28.0 refused for `/compact`, and it is asserted as a
  security property — an exhaustive reachability closure from every mode,
  plus `None`, plus strings no code path produces.
- **A session parked on a gated mode cycles HOME, not onward.** One press
  from `bypassPermissions` lands on `default`, with no confirmation in
  the way: narrowing permissions never asks.
- **`/mode <name>` is the only door to the other three, and it confirms
  first.** `PermissionModeConfirm` follows `CompactConfirm`'s shape and
  states what *stops happening* — "every tool call runs unapproved;
  nothing asks you… there is no prompt left to decline, because nothing
  will ask" — rather than asking "are you sure?". One dialog, three
  bodies, because a generic permissions warning would be equally useless
  for all three. **Enter does not accept it.** A deliberate break from
  CompactConfirm, where Enter completes an action the user's own click
  already requested; here the dialog is the last thing between a
  keystroke and an unattended agent, so the accepting key is `y` and the
  reflex key cancels. Declining issues no control request and moves
  nothing.
- **The chip, and what it costs.** `mode:<name>` sits beside the model —
  those two decide how the session behaves; everything right of them
  reports what it has done. Uncolored at `default`, **amber** at
  `acceptEdits`/`plan` ("not the posture you started in"), **red with
  `⚠`** at the three that stop asking. *Measured, and it changed the
  design*: an unconditional chip pushed the reattach handle off an
  80-column terminal, and the status row has no overflow behaviour — a
  chip that does not fit is not truncated, it is gone. So below 110
  columns the chip shrinks (`⚠ mode:bypass`), and a chip that would only
  have said `default` stands down entirely. A mode that has stopped
  asking is painted at **every** width, short-form if it must be,
  because it is the only place that fact appears at all. The chip's key
  carries the plain text and its markup carries the color, which is
  v0.35.0's tooltip defect — a chip keyed by its own escape codes matched
  nothing in the markup-stripped lookup and silently lost its hint at
  exactly the tier that mattered — written down rather than repeated.
- **Session-scoped, and the settings row is deliberately narrower than
  the command.** `/mode` and the hotkey never write the settings file.
  `/model` saves because a model is a preference; a permission mode is a
  posture adopted for a piece of work, and one Shift+Tab tap silently
  rewriting the default for every future session is not what that
  keystroke means. The persistent default (`permission_mode` /
  `DOXA_PERMISSION_MODE`) accepts **only the three cycle-safe modes**: a
  stored `bypassPermissions` is a standing hazard, an unattended setting
  that disarms the gate of sessions opened in repositories nobody has
  read yet, possibly by somebody who never set it. An out-of-subset value
  is ignored and bare `/mode` says so out loud, because a settings row
  that cannot take effect must not sit there looking like it did.
  `/clear` and `ctrl+t` build a fresh engine, so a gated mode cannot be
  inherited by a session that did not choose it.
- **Daemon parity, and one place it matters more than `set_model` does.**
  `set_permission_mode` is a real RPC: the daemon owns the SDK control
  request and broadcasts `permission_mode_changed` to every attached
  client. Two tabs disagreeing about a model name is untidy; two tabs
  disagreeing about whether the session still asks before it acts is a
  status line misreporting a safety property. The mode rides the status
  **and** the hello frame, so a client reattaching to a session someone
  left running is told what it is actually doing before it paints
  anything — `EngineClient.attach` runs its first status refresh under
  `contextlib.suppress`, and a safety indicator must not have a
  guess-shaped hole in it.
- **What taking Shift+Tab costs, measured rather than assumed.** Textual's
  `Screen` binds it to `app.focus_previous`; claiming it app-level with
  `priority=True` (which the prompt, a focused `TextArea`, makes
  necessary) removes reverse focus traversal. Forward `Tab` is untouched
  and wraps, so no focusable widget becomes unreachable — there is a test
  that presses the key and proves it. On a three-widget pane that is a
  cheap trade; on a form it would not be.

**The binding was verified in a real terminal, not only in a test.** A
pilot keypress proves dispatch once Textual has already decided a key
arrived; it says nothing about whether a terminal can produce that key,
which is the entire Ctrl+Tab question. So the app was run under a pty with
the actual back-tab bytes (`ESC [ Z`) written into it, and the rendered
status line moved `mode:default → mode:acceptEdits → mode:plan`. A test
pins the decoding half of that: `ESC [ Z` and `ESC [ 9;2u` both decode to
`shift+tab`, while `ctrl+tab` exists only as `ESC [ 9;5u` — the kitty
form, and no other — which is v0.39.0's predicate stated positively.

37 new tests. The 35 behavioural ones were each verified failing against
pre-change code, and against a *naive* implementation of the literal
request (all six modes on the hotkey, no confirmation, an uncolored chip)
which the security assertions reject. The other two are the parser
measurement above and a completeness check on the chip's short labels;
both pin facts rather than behaviour this change introduced. Suite:
**1088 passed** on the tree this branch actually lands on (1051 without
these 37). The total moves with every concurrent workstream that merges
ahead of this one; the 37 is the part that belongs to this change.

*On the number.* This workstream was assigned 0.42.0 and ran alongside
0.41.0 and 0.44.0, both of which landed first — hence a section numbered
below the two beneath it, the same ship-order-not-number ordering this
file already uses for those two. `pyproject.toml` is deliberately left at
`0.44.0`: the package version is what `/about`, `/update` and an install
pin read, and moving it *backwards* to match this heading would make a
released DOXA look older than the one it replaced. Tagging is the
operator's call, not this branch's.

## 0.46.0 — 2026-08-25

**The beliefs browser** — lettered item V. Every belief LORE holds and
every proposal waiting on your approval, in one full-height tab: what a
belief claims, how confident it is, when it was created, how long it has
sat untouched, where it came from, what it was derived from — and, for a
staged proposal, exactly what approving it would do, with its own approve
and reject controls on its own row.

**Provenance, stated up front.** The lettered spec for item V was lost
before this work started. What shipped is re-derived from the item's name
(*the beliefs browser*), from the codebase — v0.27.0's picker says in its
own docstring that item V "still owns the real browser (evidence trails,
approve/reject)", and v0.31.0's `/pending` says the write half was held
back pending a security review — and from the user's own words: timestamps
and age in the rows, the proposed verdict for each staged proposal, the
full belief text on hover, per-row approve/reject buttons, and a surface
"a bit bigger" than a dropdown. Every judgment call is marked below.

- **A tab, not a dropdown and not a modal.** `SubagentTranscriptTab` and
  `ArchivedSessionTab` are the house precedents for a non-session tab, and
  this follows them: a plain `TabPane` in the same `#session-tabs` strip,
  no engine, no prompt. *Judgment call, settled by measurement:* this
  operator's store holds 619 active beliefs and 166 staged proposals. A
  ten-row picker cannot make 166 proposals reviewable, which is the whole
  point of a verdict column. Reviewing has a beginning and an end; a modal
  that blocks the session is the wrong shape and one that closes when you
  glance away loses your place.
- **The chip picker stays, and gains a door.** *Judgment call.* Both
  surfaces rather than one replacing the other, because "roughly what does
  LORE believe about me" and "which of these 619 beliefs is stale" are not
  the same question. The dropdown is the glance; its first row opens the
  browser. That row names what the browser *has* — evidence, timestamps,
  provenance — never a verb it performs: a row reading "approve" inside a
  dropdown is exactly the accidental-click surface the gate exists to
  prevent.
- **No drag-resize.** *Judgment call, deliberately deferred.* Draggable
  dividers between the transcript, the prompt and the status bar are a
  general layout capability, and `docs/split-panes.md` now owns them as a
  requirement rather than an open question — provoked by exactly this
  surface, and specified there instead of here. The browser is
  full-height, divides its own space with a fixed split, and ships no
  handle of its own; it inherits real dividers when that lands. Building
  the mechanism twice, differently, is how a layout system rots.

**Which timestamp, and what staleness actually is.** The belief store keeps
three timestamps on the belief row (`created`, `updated`,
`last_referenced`), and the first draft of this work built the staleness
column out of `coalesce(last_referenced, updated)` — LORE's own
dormancy-sweep expression. **That was the wrong clock**, and the correction
came from the user before this shipped: *"staleness is rather indicated by
whether or not the belief was confirmed … recently or not"*.

`last_referenced` moves when a belief is merely **injected or cited**. The
agent reading a claim back to itself is not evidence the claim is still
true. What makes a belief still-true is that reality **tested** it, and LORE
keeps that somewhere else entirely: `belief_outcomes`, one append-only row
per verdict, `event` CHECK-constrained by `lore_core.store` to
`confirmed` / `contradicted` / `stale`. That ledger is the ground truth
`calibrated_confidence` calibrates the deriver's self-report against, and it
is now what the browser paints.

So a belief row carries `created` as an absolute date — the one fact here
that never moves — and then **LORE's last verdict on it**: `confirmed 2d`,
`contradicted 2d`, `stale 40d`. The verdict is shown, never just the age:
"confirmed 2d" and "contradicted 2d" are opposite facts about the same
belief, so they also differ by colour (green / red / amber), with the words
carrying it on a terminal that has none.

- **"Never tested" is a state, not a large age**, and this is the
  measurement that forced the design: **31 outcome rows against 628 active
  beliefs** on this operator's store — roughly **95% of a real belief store
  has never been tested at all**. Re-measured while building it, the number
  is starker still: those 31 rows are carried by 29 beliefs, only 15 of
  which are still `active`, against 635 active beliefs — **97.6% of the
  live working set has never been tested by anything**. Rendering one of
  those as `120d idle`
  asserts something false: nothing went stale, nothing was ever checked. It
  renders as the plain words `never tested`, in the muted body colour
  because it is an *absence* of signal rather than a bad one, with no digits
  and no unit so it cannot be misread as a duration. The tooltip says it in
  a sentence.
- **It does not sort as though it were merely old, either.** Inside a scope
  group, beliefs reality has tested sort first, most recently tested first;
  never-tested follows as a *bucket*, keeping `list_beliefs`' own
  `updated DESC` order (Python's sort is stable). With 31 outcomes against
  628 beliefs the tested ones are needles, and a list that scattered them
  through six hundred untested claims would have hidden the only evidence
  it holds.
- **`last_referenced` came off the row and moved to the tooltip.** *Judgment
  call.* It is not worthless — "cited three days ago and never once
  confirmed" is a real and interesting state — but it is the
  third-most-important thing on the line, and two age-shaped numbers side by
  side, only one of which means anything, is precisely the confusion this
  correction removes. In the tooltip it sits *below* the outcome and is
  labelled for what it is: *cited, not confirmed*.
- **A record that predates the ledger gets no column at all**, the same rule
  a NULL `via` follows. `outcomes` is always present and is `0` when the
  ledger was read and found empty; an *absent* key means the record came
  from something older than this column. A zero is a measurement, an absent
  key is an admission, and neither is a guess.
- **The ledger rides in the page, inside the shared `_fit_page` budget** —
  unlike the evidence trail, which is fetched per belief on expand. An
  outcome summary is a few short fixed-size fields where a trail is
  unbounded, so it belongs where its bytes can be measured. It is nearly
  free in practice: the ~95% of rows with no verdict carry the single field
  `outcomes: 0` and nothing else.
- **Two set queries, not 2N, and pinned to LORE's own definition.**
  `lore_core.beliefs.outcome_counts` *is* the definition of a belief's
  tally, and `doxa/operators.py` already calls it once per search hit. It is
  the wrong shape for the list: `belief_outcomes` carries no index on
  `belief_id`, so per-row is one full scan per belief across a list capped
  at 2000. The counts are computed set-wise with the same
  `sum(event = …)` expressions, with no bound parameters (so no
  `SQLITE_MAX_VARIABLE_NUMBER` cliff on a 2000-id `IN` list) — and a test
  asserts this function's answer equals `outcome_counts`' for **every**
  belief in the store, which is what stops the two drifting.

*Skills are out of scope here.* The same correction mentioned skill usage;
LORE tracks that separately (`lore_core.context.load_skill_usage`) and no
skills surface exists in this item. Not built.

- **One age format, extended rather than duplicated.** `_fmt_age` gained a
  day tier (`3d4h`, `120d`). Beliefs are months old and `2904h0m` is
  arithmetic homework. Everything under a day renders exactly as before, so
  every existing caller is untouched — which is what keeps this one
  function instead of two.
- **A belief with no timestamps renders exactly as it used to.** No
  placeholder column for a fact the store does not carry.
- **The chip picker's rows did not get wider to make room.** *Judgment
  call.* 72 columns is what fits an 80-column terminal inside a bordered
  dropdown, so the stamp takes its space out of the claim rather than out
  of the terminal. The picker is the glance; the browser is the
  full-width surface, and a tooltip carries the claim whole on both.

**The proposed verdict.** `/pending` listed proposals; it did not say what
approving one would change, and a row that does not is not reviewable. Every
row now leads with the verdict — `add → memory/user`, `replace →
memory/project:doxa`, `retract → belief #42`, `retire → skill/foo` — plus
what it would supersede, how long it has waited, and, when LORE's write gate
staged it, which untrusted context wrote it. The verdict vocabulary is read
off `lore_core.pending.apply_item`, the function that actually performs each
of these, rather than invented alongside it, so a verdict and the write it
predicts cannot disagree.

- **Proposals became records.** `SessionEngine.list_pending` and the daemon's
  `pending` RPC now serve the whole staged item, not `item["text"]`: a
  proposal has to carry the pending id there is nothing to approve without,
  and the fields a verdict is computed from. *Judgment call:* a row that
  still arrives as a bare string — from a daemon on the older build, which
  installing a new DOXA does not restart — renders without a verdict rather
  than with a guessed one. A write path is the wrong place to guess.

**Approve and reject, per row.** v0.31.0 shipped neither, because the write
path into curated memory was under security review (`docs/plugin-api.md` §6,
LORE issue #43). LORE **0.36.0** concluded it: the write gate classifies
every CLI write by caller, and the provenance ledger records who wrote each
entry and whether it came through approval. That gate is CLI-layer only and
DOXA holds `lore_core` in-process, so it does not apply here — what makes
approving from DOXA defensible is that it is a human acting in a UI,
recorded as such.

- **DOXA labels nothing.** Approve calls `lore_core.pending.apply_item`,
  which passes `via="approved"` into `memory_add`/`memory_replace`/
  `filemap_add`/`filemap_replace`/`belief_insert`, then
  `lore_core.pending.archive(pid, "approved")`. Reject is
  `archive(pid, "rejected")`. Not one line of the write is reimplemented
  here: the label on an approved entry is the label LORE puts there for an
  approval, and a test reads it back out of the store rather than trusting
  what DOXA intended.
- **Nothing is approved without an explicit per-item action.** One id per
  call, no list parameter on either engine, no bulk RPC, no multi-select, no
  "approve all". Approve **arms** on the first press or click and applies on
  the second, on that same row; arming any row disarms every other. Reject
  is one action. *Judgment call — misclick asymmetry:* approve writes into
  the model's context, reject archives a file that stays on disk, so the
  irreversible one is the one that costs two deliberate acts. It is an
  in-row arm, not a modal confirm — a dialog on every approve would defeat
  the per-row button the user asked for.
- **Enter is not bound to either.** Enter expands a belief's evidence trail
  and does nothing else anywhere in this browser, because Enter is the key a
  hand rests on.
- **Keyboard parity, not a click-only control.** `a` arms and applies, `r`
  rejects, `Esc` disarms, `↑`/`↓` move between rows — all on the focused
  row, which is the only row marked as focused. A control reachable only by
  mouse is unreachable for most of how this app is used.
- **Neither outcome is silent.** The row settles into a named resolved state
  (`✓ approved` / `✗ rejected` / `✗ NOT applied`) and stays in the list —
  a queue that swallows the line you just acted on gives you nothing to
  check yourself against — and the owning session gets a block naming the
  proposal, its verdict and the provenance label, because the browser may
  not be the tab you are looking at when a write lands.

**The pinned dependency is LORE 0.36.0** (bumped on `main` by
`.github/workflows/lore-bump.yml` and taken here on rebase), so a bare
clone gets the write gate and the provenance ledger and the browser is
fully live out of the box. The degradation below is for the other case,
which is real rather than theoretical.

**Read-only on an older lore_core, and it says why.** The Claude Code plugin
checkout wins over the pinned wheel (`doxa/_lore_bootstrap.py`), so what is
loaded on a given machine is not what `pyproject.toml` says. When the loaded
copy cannot record an approval honestly, the browser renders a banner naming
the version, the carrier and the reason, and renders no approve or reject
control at all — and the engine refuses the write even if asked directly.
*Judgment call:* the capability is **measured, not inferred from a version
string** — `lore_core.gate` must import, `pending.load_pending`/`apply_item`/
`archive` must exist, and `belief_insert`/`memory_add` must accept `via=`.
A copy whose writers take no `via` cannot record the label however new it
claims to be. The banner reads the same measurement `/about`'s `lore from`
row does, so a user chasing a difference is never told two things.

**Full claim text on hover.** Each row is its own widget, so each carries its
own tooltip — impossible on the `OptionList` the chip picker is built from,
where a tooltip is a widget attribute and an `Option` has none. The tooltip
is set from the same record, in the same constructor, that built the visible
line: the v0.35.0 defect (a hint keyed by markup while the lookup ran against
markup-stripped text, so it vanished at two tiers) cannot recur because there
is no lookup. *Judgment call:* the tooltip carries the whole claim, its
confidence, its provenance and its timestamps, but **not** the evidence
trail — that is unbounded and lazily fetched, and a tooltip that waits on a
query flickers. The trail is one keystroke away instead.

**Evidence trails, without blowing the frame cap.** *Judgment call.* The
belief page carries an evidence **count**, never the trail; the trail is
fetched for the one belief a reader expanded, capped at 40 rows, over a new
`belief_evidence` RPC that runs through the same shared `_fit_page` byte
budget `beliefs` and `pending` already use — a third caller, not a third
budget. Putting 619 trails through a 64KB frame is the v0.28.0 defect
waiting to happen again.

- `/beliefs` joins the registry, so it reaches `/help`, the Ctrl+P palette
  and autocomplete like every other command. `/pending` keeps its dropdown
  and stays write-free there; its summary now says where review happens.
- Tests: 43 new — 40 in `tests/test_beliefs_browser.py`, three more in
  `tests/test_daemon.py` for the new RPCs over a real socket. Rows are asserted to
  have non-zero size and their age/verdict/provenance text read back off the
  rendered widget, not off the formatter that fed it — the v0.28.0
  invisible-buttons defect passed every structural assertion for a full
  release. The security assertion drives the whole surface with everything a
  careless hand plausibly hits and asserts the engine's ledger stayed empty.
  Approve, reject and the already-resolved race run against the real
  `lore_core` and read the provenance back out of the store — behind a
  snapshot-and-restore fixture, because `conftest.py` shares one throwaway
  belief store across the whole session and a stray claim left in it makes
  `tests/test_consult.py`'s "an unrelated prompt matches nothing" quietly
  false. That fixture clears `belief_outcomes` too, and not for tidiness:
  `beliefs.id` is an `INTEGER PRIMARY KEY`, so SQLite hands a deleted id
  straight back to the next insert and an orphaned outcome row silently
  re-attaches itself to an unrelated belief in the next test — caught
  exactly that way.

## 0.44.0 — 2026-08-25

- **The transcript spent four blank rows on every one-line answer.** Measured
  on a rendered turn: title 1, `TurnBlock > Contents` padding-top 1, the text
  itself 1, Textual's own markdown block margin-bottom 1, `.turn-body`
  padding-bottom 1, `TurnBlock` margin-bottom 1 — six rows to say one line, and
  only the last of them separates one turn from the next. Dropped the Contents
  padding and the body's trailing padding, and zeroed the markdown bottom
  margin on the LAST block only (`.turn-body > *:last-of-type`). A one-line
  turn is now 2 rows plus the one separator; a multi-paragraph answer keeps its
  internal rhythm, verified as bottom margins `[1, 1, 0]` across three
  paragraphs. Blank rows between paragraphs are readability; blank rows at the
  end of a turn are waste stacked on the turn's own separator.
- **The curated-memory caps had no indicator anywhere.** LORE injects user and
  project memory into every session and enforces a hard cap on each: past it a
  write is REFUSED and the entries are listed for consolidation, rather than
  the store degrading quietly. That makes the fill the one LORE number worth
  seeing before it bites, and it was visible only by running `lore status`. A
  `mem u63% p39%` chip now sits next to the belief count, with the raw
  `2824/4500 · 3471/8800` in its hint. Two percentages rather than one merged
  figure: the caps are separate and fill at different rates -- user memory
  holds facts that never stop being true and creeps up forever, project memory
  rotates with the repo -- so merging them would hide whichever is about to
  start refusing. Counted in CHARACTERS from the file lore_core itself writes,
  not `st_size`, so the chip agrees with the cap the write path enforces (a
  test pins this with deliberately multi-byte content); cached on mtime, since
  `_refresh_status` already pays for a belief `COUNT(*)` per refresh and this
  bar runs under a no-timer rule. An unreadable store, or an older lore_core,
  omits the chip rather than degrading the bar.
- **`if API` left the cost chip.** The status bar's scarcest resource is row
  width, and the phrase cost eight characters of it on every session. The
  meaning stays where it always was: `sub:` already says this session bills no
  dollars and `≈` marks the figure an estimate, and the tooltip now spells out
  that it is what the session WOULD have cost on API pricing. `/usage` and the
  turn title keep the full wording — neither is width-constrained, and `/usage`
  is prose where spelling it out is the point.

## 0.43.0 — 2026-08-25

*Scoped and written as 0.43.0, landed after 0.44.0. `pyproject.toml` keeps
the higher number — a declared version may not go backwards — and this
entry sits where its own number belongs.*

**A permission dialog that answered to no key at all, and the web search
that looked broken because of it.** Reported as two defects: a web search
whose invocation appeared and then nothing ever happened, and a permission
dialog — blinking tab, multiple-choice menu, all of it on screen — that
ignored `↑`/`↓`, ignored `1`/`2`/`3`, ignored Enter. They are ONE defect.
The web search was not broken; it was *parked*, waiting on a permission
request the user could not answer — and neither could anyone else: `Esc`,
the documented way out, had gone deaf with every other key. A tab in that
state has no way forward at all.

**Reproduced before anything was changed**, in a driven app and against
the real SDK, because the shape of the fix depended on which of the two
stories was true:

- A real turn through `SessionEngine` and the installed CLI: `WebSearch`
  arrives as an ordinary `ToolUseBlock`/`ToolResultBlock` pair, DOXA
  renders the call, `can_use_tool` fires with `display_name` set, and the
  turn then blocks inside the SDK's permission round-trip until the
  dialog is answered. That is the whole of "I see the tool invocation but
  nothing happens afterwards" — the missing render was a missing
  keystroke.
- The dialog is `can_focus = False` by design and driven entirely through
  `PromptInput`'s key protocol, so it answers a key only while the PROMPT
  holds focus. Nothing guaranteed that. Three ordinary gestures broke it,
  each measured in a headless pilot: **clicking the blinking tab when it
  is already the active tab** (Textual focuses the tab strip and posts no
  `TabActivated`, so `_on_tab_activated` — the only hook the mouse path
  has — never runs); **clicking the transcript** to scroll back and read
  before deciding (`#block-list` is a focusable `VerticalScroll`, and its
  own up/down bindings then eat the arrows); and a stray **Tab** (the
  prompt's `tab_behavior` is `"focus"`).

- **A blocking request now claims the keyboard.** Opening the dialog
  focuses that pane's prompt — but only when the pane is the tab the user
  is actually looking at. Focusing a widget inside a `TabPane` activates
  that pane, so doing it unconditionally would yank a background
  request's tab out from under someone typing in another one; the blink
  is that case's whole signal, and `_focus_tab` already focuses the
  prompt when they come over to answer.
- **And a net under it, at the app level.** While the active pane has a
  dialog open, focus landing anywhere else on that screen returns to the
  prompt. This is **not** a retreat from v0.38.0's focus ownership: focus
  still moves only on explicit intent, and a request that has stopped the
  session *is* intent — the rule names one more site rather than
  reinstating the mount-time focus that release removed. Narrow on
  purpose: only while a dialog is genuinely open, only for the active
  pane, only on that pane's own screen (a pushed modal keeps its own
  focus), and `ChipPicker` — the one widget here that deliberately takes
  focus — is exempt. Mouse-wheel scrolling never needed focus and is
  unaffected.
- Seven tests assert the user-visible outcome rather than the mechanism:
  the dialog answers to `↑`/`↓`, to a number key and to Enter after each
  of the three gestures; `Esc` still declines; the request that arrives
  while the pane is in the **background** — the reported path — answers
  after the user comes over and reads the transcript first; and, as the
  guard against over-fitting, with no dialog open the transcript still
  takes focus when you click it and keeps it. All seven fail against
  pre-fix code.

**Separately: a server-side tool's result was silently dropped.** Not
what the user hit — `WebSearch`/`WebFetch` are client-side tools the CLI
runs itself, measured, not assumed — but a real hole one block type away
from it. Tools the API runs on the model's behalf (`advisor`, and
whatever else joins `claude_agent_sdk.ServerToolName`) arrive as
`ServerToolUseBlock` and `ServerToolResultBlock`, and the engine handled
neither: the call never drew a chip and the answer vanished with no
error, which is worse than a tool that fails, because nothing on screen
says whether it ran.

- Both blocks now render, onto the **existing** `tool_call`/`tool_result`
  events and the same chip. Deliberately not a new event vocabulary and
  no new `EVENT_RENDERERS` row: it *is* a tool call, and a second
  spelling of one idea would have to be learned by the pane, the daemon's
  frame replay and every plugin. The tool name is the discriminator for
  anyone who cares which side ran it.
- The result rides the **assistant** message, not the user message a
  client-side result comes back on — which is precisely why the
  `UserMessage` branch never saw it. It is persisted through
  `_persist_tool_results` all the same, because `doxa.transcript`'s
  replay reads results from the user-role record only, and a restore that
  dropped them would reintroduce the same vanished-result bug one launch
  later.
- The SDK types a server tool's result content as an opaque dict on
  purpose (every server tool has its own schema, and the set grows
  without the SDK changing), so `_server_tool_result_text` reads it
  defensively: an error code if the call failed, ordinary text parts if
  there are any, compact JSON otherwise. That last tier is the point — a
  shape nobody has seen yet still renders as *something* a reader can
  judge.

**One latent suite flake, surfaced by adding to the suite.**
`test_recent_sessions_are_the_empty_query_answer` seeded its two sessions
with fixed 2026-08-2x timestamps, while every engine-driven test in the
suite indexes a real session stamped at run time — always newer. Since
`recent_sessions()` orders by `last_ts` and pages at `RESULT_LIMIT` (20),
the test was really asserting "fewer than twenty other tests have run
first", and four more engine tests were enough to make it answer no. The
seed is now stamped ahead of any clock this suite can run under, so the
test asks about the query rather than about its neighbours; the seeded
`msg` rows keep their fixed dates, which the search, label and excerpt
assertions quote literally.

## 0.41.0 — 2026-08-25

**The logo is now the first thing a session shows, and `/img` will tell
you why it looks the way it does.** DOXA has had a terminal-image ladder
since v0.13.0 — kitty graphics, sixel, half-block cells, then the
`[image: …]` line — and almost nothing on the default path ever exercised
it. The opening block now draws the README's own banner through that same
ladder, so the renderer is under test on every launch on every terminal,
and `/img` with no argument reports what this terminal actually granted
and then demonstrates it.

- **The banner is `assets/logo.png`, whole, at 41 columns.** *Judgment
  call:* the wide logo rather than the square `icon.png`, because a banner
  is a wide thing and the square mark drops the wordmark that makes it
  read as DOXA rather than as a triangle. *Judgment call:* the width is
  derived, not chosen — `columns = rows × cell_aspect × logo_aspect =
  6 × 2 × 3.4375 ≈ 41` — because a 1100-pixel image means nothing to an
  80-column terminal and the only unit that does is cells. 41 is half of
  an 80-column terminal and a third of a 120, which reads as deliberate at
  every common width instead of as an image that happened to fit.
- **Six rows is the budget, and it is the number the module defends.** The
  opening block is the first thing you see and the transcript scrolls past
  it; a banner costing fifteen rows is a nuisance by the third session of
  the day. Six is a quarter of a classic 24-row terminal and about the
  height of the identity block's own field list directly beneath it, so
  the banner never outweighs what it introduces. Only WIDTH is pinned —
  height comes from the image widget, which derives it from *this*
  terminal's cell aspect, so a terminal whose cells are not 2∶1 gets the
  right number of rows rather than a letterboxed six.
- **The text tier gets a wordmark, not `[image: doxa logo]`.** *Judgment
  call, and the one this feature would have been worst without.* The
  fallback line exists to say that an image you asked for could not be
  drawn; it is not fit to be the permanent first line of every session. So
  the `text` tier — and any terminal under 56 columns, where 41 cells is
  most of the line and the identity block starts wrapping — gets three
  rows of half-block glyphs spelling DOXA over the logo's own tagline.
  Hand-rolled, fifteen columns wide, in one place, no new dependency, and
  drawn from the same glyph vocabulary the half-block tier uses, so the
  small sibling looks related to the big one.
- **On by default, `boot_banner` / `DOXA_BOOT_BANNER=0` to turn it off.**
  *Judgment call:* default-on rests on the degrade path rather than on the
  picture — there is no terminal and no width at which this costs more
  than three rows of something legible — and a default-off banner would
  also mean the image renderer ships untested on every machine that never
  finds the switch. Off is genuinely off: no widget, and the rows come
  back, which a test asserts by measuring where the identity block lands
  with the banner on versus off.
- **Half-block was checked by looking at it, not by assuming.** The logo
  was down-sampled to the exact cell grid at five candidate sizes and
  eyeballed. The ΔΟΞΑ wordmark holds up; the tagline set into the asset
  becomes a soft grey rule at any size a terminal can afford. That is the
  asset's own design rather than damage introduced here, and it is why the
  wordmark fallback repeats the tagline as real text.
- **The first screenshot found two defects a green suite had not.** Both
  are the reason "render it and look at it" is part of shipping an image
  feature. First: `logo.png` is RGBA with a fully transparent background,
  and textual-image normalizes with PIL's `convert("RGB")`, which
  *discards* alpha rather than compositing it — so every transparent pixel
  came out as the white hiding underneath, and the banner was a glaring
  white slab on a dark theme. `doxa.banner` now flattens onto the theme's
  own `#171512` before the widget sees the image. Second: 15% of the
  asset's width and 26% of its height is transparent margin — page layout
  for a README, and pure waste inside a six-row budget — so the image is
  cropped to its alpha bounding box first. That is a third more resolution
  for the wordmark at no cost in rows, and it is why the banner is 47
  columns rather than 41: the geometry follows the *inked* aspect,
  928 ∶ 238, which a test re-measures off the real file so the constant
  cannot drift away from the asset it describes.
- **Pillow is now a declared runtime dependency, and is still not a new
  one.** textual-image has always required it and it has always been on
  disk; what changed is that DOXA imports it directly, for the compositing
  above. The line moves up out of the dev group on the rule that group's
  own comment already stated — code importing `PIL.Image` earns its own
  declaration instead of riding another package's coattails.
- **A cell size textual-image *defaulted* is never reported as one it
  measured.** When neither `ioctl` nor the escape query answers, upstream
  returns a VT340 constant indistinguishable from a real reading, so the
  showcase labels that value instead of reprinting it. Under-claiming by
  one line of text is the side of the trade `doxa/keyboard.py` already
  argued for. The same guard catches a real upstream crash: on a pty that
  reports zero columns, `get_cell_size` divides by that zero and raises
  `ZeroDivisionError` out of its own except clause, which lands here as
  "not measured" rather than as a broken session.
- **`/img` with no argument is now the showcase.** *Judgment call on where
  it lives.* Not `/doctor`: that is a text report with an exit code —
  `scripts/install.sh` runs `doxa doctor` headless and reads pass/fail out
  of it — and a check that must mount image widgets to mean anything
  cannot live there. Not a new `/image` either: `/img` already exists, its
  registry summary already called it a "terminal image-support probe", and
  a second command one letter away from it is a coin flip at the
  autocomplete. `/img <path>` is untouched.
- **The showcase separates measured, inferred and never-asked, and never
  collapses the third into the first two.** It reports the detected mode
  and whether it was probed or forced, whether a terminal actually
  *answered* (a settled "text" is silence, not a terminal that said no),
  `textual-image`'s version, and the effective cell size. Then it renders
  the same asset in every tier it may honestly draw: the detected one plus
  half-block and text, which need nothing from the terminal. A tier the
  ladder short-circuited past — sixel, on a terminal that answered for
  kitty — is named as never asked and is **not drawn**. Pushing a graphics
  escape at a terminal that has none does not produce a picture, it
  produces litter in the transcript, and a showcase implying kitty support
  where there is none is worse than no showcase. A mutation run proves the
  rule earns its place: relax it and the suite captures raw `_Gi=…`
  payload in its own stdout.
- **No new probe, and no new cost at boot.** The banner reads the ladder
  result `DoxaApp.__init__` already settled. The one addition to that
  startup window is the terminal's cell size, which `textual-image`
  resolves with an `ESC[16t` query whenever `ioctl` cannot answer — the
  same read-stdin hazard `doxa/images.py` and `doxa/keyboard.py` are both
  built around, so it is settled in the same place, for the same reason,
  behind the same tty short-circuit. Headless it returns `None` without
  writing a byte, and it is asked at most once.
- **An installed DOXA carries the logo.** `pyproject.toml` maps
  `assets/logo.png` into the wheel at `doxa/assets/logo.png`, on exactly
  the terms `assets/icon.png` has been mapped since the launcher shipped:
  one file in git, no duplicate under `doxa/`. Without that line the
  banner would be a source-checkout-only feature and silently nothing for
  everyone who installed the documented way.

## 0.39.0 — 2026-08-25

**A key that does nothing now says why.** DOXA binds `Ctrl+,` to
`/settings`. On a terminal speaking the legacy key encoding there is no
byte for `Ctrl+,` — the combination is not merely unbound, it is
*unsendable* — so the documented key did nothing, forever, silently, with
no way for the user to tell whether DOXA or the terminal was at fault.
Same for `Shift+Enter` at the prompt, which is why `Alt+Enter` has always
been bound beside it.

**Provenance, stated up front.** The lettered spec for item O was lost
before this work started; what shipped is re-derived from the item's name
(*keyboard-protocol detection*) plus the codebase, which had already
written down what it was waiting for: `doxa/doctor.py` carried a
placeholder check deferring the measurement to "item O", and both
`doxa/ui/prompt.py` and the README said item O "will one day tell you"
which newline key your terminal grants. Every judgment call is marked
below.

- **Textual reports nothing, so DOXA asks the terminal.** Textual 5.3.0's
  Linux driver *requests* the kitty keyboard protocol unconditionally —
  `linux_driver.py:276` writes `\x1b[>1u`, `:373` disables it again — and
  never asks whether the request was granted. There is no `App`
  attribute, no `Driver` property and no message carrying it. The
  contrast sits two lines away: in-band window resize IS queried
  (`_query_in_band_window_resize`, `:149`), answered through
  `messages.InBandWindowResize`, and remembered on both the driver
  (`_in_band_window_resize`, `:64`) and the app
  (`App.supports_smooth_scrolling`, `app.py:822`). The parser will decode
  a `CSI u` key if one arrives (`_xterm_parser.py:326`), but that is a
  parse, not a report — it can only tell you anything *after* the user
  pressed a key you might not be able to receive. So new
  `doxa/keyboard.py` sends the protocol's own support query, `\x1b[?u`,
  followed by Primary Device Attributes, and classifies the reply.
- **Silence is never read as "legacy".** The DA sentinel is the whole
  honesty argument: a terminal that answers DA and *not* the `u` query is
  measurably legacy; one that answers nothing at all was not listening to
  us — headless, a pipe, or a Textual reader thread that already owns
  stdin — and that says nothing about the keyboard, so DOXA says nothing
  about it. Third state, `unknown`, surfaced as such everywhere. The
  probe follows `doxa/images.py`'s discipline for the same reason and
  with the same failure mode: it runs at most once, cached, settled by
  `DoxaApp.__init__` while this process still owns the terminal, and
  short-circuits without writing a byte when stdin and stdout are not
  both a tty. No new dependency — `termios`/`tty`/`select` and nothing
  else.
- **`/about` gains a `keyboard` row** — the bug-report screen, where
  someone chasing a dead key looks first, and it copies out with the rest
  under `c`. *Judgment call:* the row is present even when the answer is
  "not measured", the one deliberate exception to that screen's
  omit-what-you-cannot-answer rule. An absent row cannot be told apart
  from a DOXA old enough never to have looked, and "not measured" is an
  observation about this run rather than the plausible-looking constant
  that rule forbids.
- **`/doctor` stops being a placeholder.** The keyboard-enhancement check
  now measures, and names the bindings actually lost. *Judgment call:* a
  legacy terminal is a **pass**, not a fail — nothing is wrong with the
  terminal or the install, and `doxa doctor` exits non-zero on failures
  (`scripts/install.sh` runs it). `unknown` remains for the case a
  measurement cannot cover. *Judgment call:* `/doctor` is an
  **additional** home, not the better one. It is where you go when you
  suspect something is broken, and a dead key does not feel broken, it
  feels like a misremembered shortcut. `/about` is where a bug report
  starts, so it carries the row; `/doctor` carries the detail, including
  the list of lost bindings.
- **`/help` marks a binding this terminal cannot send.** A `✗` after the
  key, and a footnote — appearing only when something was marked — that
  says what happened, points at the slash command reaching the same
  place, and names terminals that would deliver it. On a real machine
  today that is exactly one binding, `Ctrl+,`. *Judgment call:* the
  annotation ships, but only off a positive measurement.
  `keyboard.is_unreachable` is False whenever the protocol is `unknown`,
  so on any terminal DOXA could not ask, `/help` is byte-identical to
  what it always was. A false "this key is dead" sends a user into their
  terminal settings after a bug that is ours, which is worse than the
  silence it replaced. The reachability predicate is under-claimed by
  construction for the same reason: True only for combinations whose
  legacy encoding is *known* (Ctrl+punctuation with no C0 code,
  Ctrl+I/M/H/[ which arrive as Tab/Enter/Backspace/Escape, modified
  Enter/Tab/Escape/Backspace/Space, Ctrl+Shift+letter, and
  Super/Hyper/Meta), and False — assume it works — for everything else,
  including every modified cursor and function key, Alt+anything, and
  Shift+Tab.
- **No binding changed, and none had to.** This item reports what a
  terminal can do; it does not re-map keys around it. `Alt+Enter` was
  already bound beside `Shift+Enter`, and `/settings` was already the
  slash form of `Ctrl+,` — the annotation points at doors that were
  always there.
- *Judgment call:* `DOXA_KEYBOARD_PROTOCOL` (`kitty`/`legacy`/`unknown`)
  overrides detection, but is an environment variable only — no
  `config.toml` setting and no settings-modal row. A saved protocol is a
  claim about a terminal the user may not be sitting at any more, and
  persisting a wrong claim is the exact failure this item exists to
  prevent.
- Tests: `tests/test_keyboard.py` (68). The predicate as a truth table in
  both directions, including every case it must *not* claim; the probe
  driven through a real pty with a thread playing the terminal, covering
  kitty, legacy, total silence, a truncated reply, and that raw mode is
  restored afterwards; the cache running at most once; headless
  degradation writing no byte and raising nothing; the `/about` row
  rendered on the real modal at non-zero geometry with its own text;
  `/help` marking, not marking on kitty, and not marking on unmeasured;
  and all three `/doctor` branches.
## 0.38.0 — 2026-08-25

**Two tab races, both fixed at the mechanism rather than the symptom.**
Which tab is active, and which prompt has the keyboard, were decided by
Textual's scheduling rather than by anything the user did; and a restore
could silently forget which tab you were on. Neither was a new bug — both
had a workaround shipped in front of them, one in the app and one in CI,
and this release removes both workarounds along with the causes.

### Focus and activation are no longer the same event

- **The defect.** `SessionPane.on_mount` focused its own prompt, and
  focusing a widget inside a `TabPane` **activates** that pane
  (`TabbedContent._on_tab_pane_focused`). So activation was a *side
  effect of mounting*, landing whenever Textual got round to the mount —
  which is a race against every other decision about which tab is active.
  It had already produced two visible failures. v0.23.0's restore opened
  three saved tabs and always landed on the last one to mount regardless
  of the record, because three panes each focusing on mount meant the
  last focus won; v0.32.0 patched that narrowly, with a `_focus_on_mount`
  flag that let exactly *one* restored pane focus itself so that exactly
  one activation-by-side-effect happened. And
  `tests/test_tab_status.py::test_done_unseen_marks_a_background_tab_and_clears_on_activation`
  was flaky: after `ctrl+t` then `ctrl+left`, the second pane's mount-time
  focus could steal `active_pane` back *after* the key press, so
  `_on_turn_done_status` read `active=True` for the wrong pane and never
  set `-done-unseen`. Measured on this machine at **7 failures in 40**
  standalone runs (the earlier estimate was ~5%).
- **The fix: focus follows explicit user intent, at each site.** The
  mount-time focus is gone, and `DoxaApp._focus_tab` is now the single
  place that puts the keyboard into a tab. Every path that moves the user
  on purpose calls it and says so in its own body: `action_new_tab`
  (Ctrl+T and the palette's *New tab*) mounts, activates, then focuses,
  in that order; `_cycle_tab` (Ctrl+←/→) focuses the tab it lands on
  rather than leaving it to an event a pump-turn later; `_switch_to_tab`
  (the palette's open-tab entries, and a peer chip's jump to a session
  already open in this window) does the same; `open_tab_at` (the repo
  picker's *open in a new tab*) does the same; and startup/restore now
  choose their tab in one explicit place, `_activate_initial_tab`.
- **`_on_tab_activated` keeps focusing, for exactly one reason.** A
  **mouse click on a tab header** produces no key event and runs no
  action of ours — Textual activates the tab and that message is all we
  hear. It is the one path with no explicit handler to hang focus on, so
  the event stays its handler. For every other caller it is now a no-op
  refocus of a prompt that is already focused.
- **A pane that mounts in the background now stays there.** This is the
  behaviour change that falls out of the above, and it is deliberate:
  `add_pane` used to switch the user to the new tab as a side effect of
  the pane focusing itself, so *any* code that added a pane also stole the
  window whether it meant to or not. Nothing in the tree relied on that —
  every mount site was audited and every one of them sets `active`
  explicitly (Ctrl+T, `open_tab_at`, the subagent transcript tab, which
  has focused its own scroll container on open since it shipped) or is
  compose-time and now routed through `_activate_initial_tab` (the
  ordinary launch's single pane; every restored `SessionPane` and
  `ArchivedSessionTab`). The palette's *attach* and *new session* entries
  mount nothing at all: they swap the engine inside the already-active
  pane (`switch_engine`).
- **Startup was tested, not assumed.** With the mount-time focus removed,
  the first pane's focus depended on Textual posting `TabActivated` for
  the *initially* active tab — which it need not, since nothing changed.
  Measured: it does (`Tabs._on_mount` picks the first tab and its watcher
  posts the message), so the prompt would in fact end up focused on its
  own. Startup focuses it explicitly anyway. "The first prompt is focused
  because a widget we do not own happens to announce itself" is the same
  implicitness this release exists to remove, and `docs/split-panes.md`
  names an explicit startup leaf as a prerequisite: with two panes visible
  at once, an implicit mount-time focus is a race between siblings.
- **Restore's tab choice is stated once.** `_activate_initial_tab` takes
  the saved active tab when the record named one — live pane or archived
  tab alike — and otherwise the first *session* pane in the strip. That
  second rule reproduces what the old mount-time focus picked, including
  its one non-obvious case: when every restored tab was an archive, the
  window came up on the fresh pane `compose()` adds beside them rather
  than on the first archive, because the archives have no prompt to focus.
  `_focus_on_mount` is deleted.
- **CI's retry scaffolding is deleted with the flake.** The workflow
  deselected the done-unseen test from the suite and re-ran it alone with
  three attempts. It is deterministic now — **40 of 40** standalone runs
  after the fix, against 33 of 40 before — so it goes back in the suite
  with the same zero retry budget as everything else. A retry left
  wrapped around a fixed test only hides the next regression in it.

### A restore no longer forgets which tab you were on

- **The defect, and why it is a different bug.** `_note_pane_booted`
  counts restored panes down and fires `_persist_tabset()` the instant the
  last one reports its session id. `_persist_tabset` asks
  `TabbedContent.active_pane` which tab is active — and that resolves
  **asynchronously**: `active` is a reactive that starts as the empty
  string and is only filled in when the inner `Tabs` widget's own mount
  picks a tab. A pane that boots inside that window means no tab matches
  `pane is active_tab`, so `active_session_id` is written as `null` — and
  nothing writes again until the tab set next changes, so that one racy
  write is what lands on disk. The tabs restore complete and correctly
  ordered; only the memory of which one you were on is gone, silently, on
  the *next* launch. Observed directly (`TabbedContent.active` read as
  `''` at persist time) and measured as **1 failure in 80** runs of
  `tests/test_tabsets.py::test_restore_tabs_open_in_saved_order_with_names_and_active_tab`
  with four suites in parallel. Its signature is `assert None == 'sid-2'`
  — a *null* id, never a wrong one, which is what distinguishes it from
  the focus race above.
- **The fix: `_persist_tabset` will not write an id it has not resolved.**
  When the active tab comes back `None` *and* activation is still pending,
  it falls back to the id the restore came from. In exactly that window
  that id cannot be stale — no tab is active yet, so the user cannot have
  switched away from one. The pending check is what keeps the fallback
  from outliving its window: once activation has resolved, a `None` active
  id is a real answer (a subagent transcript tab is active and no session
  tab is) and is written as one.
- **Fixing the focus race did not fix this one**, which was checked rather
  than assumed: with the focus fix in and the fallback removed, the
  deterministic case still fails, and the loaded 80-run still fails 1 in
  80. Same family — asynchronous activation — but a different failure
  mode, and write-ordering is not something focus ownership can reach.
- Tests: `tests/test_focus_ownership.py` (13, new) — startup opens with a
  focused prompt and types into it; Ctrl+T activates *and* focuses the new
  prompt and typing lands only there; a pane mounted without setting
  `active` stays in the background; a pane mounting *after* a Ctrl+←
  cannot take the activation back (the done-unseen flake, made
  deterministic); Ctrl+←/→ and a jump-by-id carry the focus with them;
  `open_tab_at` lands the same way as Ctrl+T; a three-tab restore with the
  saved active tab in the *middle* (the shape that exposed the v0.23.0
  defect — two tabs hid it) activates and focuses that one; a restore with
  no saved active tab lands on the first live pane even when the strip
  starts with an archive; an all-archived restore lands on the fresh pane;
  a restore whose saved tab is an archive comes up on the archive. Plus 2
  in `tests/test_tabsets.py` — the active id survives a persist that beats
  activation, and a later tab switch still wins over the restored id. Of
  the 15, the 3 that encode the two races fail against pre-change code
  (including the exact `assert None == 'sid-2'` signature); the other 12
  assert outcomes the old mechanism also produced, and are here because
  the mechanism producing them changed.

## 0.37.0 — 2026-08-25

**A bare clone of this repo now works.** `uv sync && uv run pytest` on a
machine that has never seen the LORE Claude Code plugin is the whole
setup; it was not, and the failure mode was as bad as they get.

- **`lore_core` is a declared dependency.** DOXA's memory model *is*
  `lore_core` — `doxa.engine`, `doxa.peers`, `doxa.operators` and
  `doxa.transcript` all import it — and it was declared nowhere.
  `doxa/_lore_bootstrap.py` resolved it by reaching into a LORE plugin
  checkout on the machine, so a clone without the plugin failed **41 of 52
  test modules at collection**: not a red suite, a suite that never ran far
  enough to say what was wrong. `pyproject.toml` now carries
  `lore-core @ git+https://github.com/docwilde/LORE@<commit>` — a git URL
  because neither project is on PyPI, pinned to a commit rather than a
  branch, because a branch pin is a subscription rather than a dependency.
  This required packaging LORE, which had no `pyproject.toml` of any kind;
  that shipped as LORE 0.35.1, packaging only the importable `lore_core`
  and leaving the plugin install path byte-identical.
- **A LORE plugin checkout still wins over the pinned copy, on purpose.**
  Both point at the same `~/.claude/lore` store and the same `state.db`,
  and the plugin is the busier writer of the two — a hook fires on every
  Claude Code session start, end and compaction. Letting the installed
  wheel win would mean aiming two different `lore_core` versions at one
  SQLite file and hoping the older one reads what the newer one migrated.
  It also keeps the property the shim was written for: a user editing
  their LORE checkout sees those edits in DOXA. Reproducibility loses that
  argument and gets an escape hatch instead —
  `DOXA_LORE_SOURCE=package` ignores any checkout and takes the pinned
  dependency, which is how you reproduce a bug against exactly what CI
  runs. `DOXA_LORE_CORE_PATH` still relocates the checkout.
- **`/about` says which one it loaded.** New `lore from` row: `plugin` or
  `package`, with the directory. Measured off `lore_core.__file__` after
  the import rather than restated from the precedence rule, so a copy that
  arrived some way the bootstrap did not arrange — `PYTHONPATH`, an
  editable install — is reported as what it is. The `lore` version row now
  reads `lore_core.__version__` (LORE 0.35.1 and later resolve their own
  version correctly in either carrier) and falls back to the plugin
  manifest for the older installs that carry no version attribute at all.
- **CI stops pretending.** The workflow used to check out `docwilde/LORE`
  alongside DOXA and point `DOXA_LORE_CORE_PATH` at it on every leg —
  scaffolding that hid the missing dependency rather than testing
  anything. The two Python legs now run the bare-clone case with no LORE
  checkout at all, and a third leg checks LORE out deliberately to
  exercise the *other* branch of the precedence, which is the
  configuration most real machines are in (the `LORE_REF` hatch stays and
  still tracks `main`, so a LORE change that breaks DOXA turns that leg
  red before a user finds it). Every leg asserts which `lore_core` it
  loaded before spending nine minutes on tests.
- Tests: `tests/test_lore_dependency.py` (13) — the declaration itself and
  that its pin is not a moving ref, the distribution being installed
  independently of any plugin, both precedence branches and both env-var
  hatches, a typo'd `DOXA_LORE_SOURCE` degrading to `auto` rather than to
  "no memory system", the `lore from` row moving when the source moves,
  and the version resolving for a 0.35.1-and-later package as well as for
  a pre-0.35.1 plugin that has only a manifest.

## 0.36.0 — 2026-08-25

Two things you can now do at the prompt: find out what is actually
occupying the context window, and run a shell command without leaving the
TUI or spending a model turn.

**Provenance, stated up front.** The lettered specs for both items were
lost before this work started. What shipped is re-derived from each item's
name and from the codebase as it stood, and every place that required a
judgment call is named below rather than presented as if a spec had settled
it. Nothing here invents scope past what the two names plainly imply.

**A collision with v0.35.0, and how it was resolved.** Items X and K were
built in parallel and both noticed the same thing: `SessionEngine` was
asking the CLI for a full context reply once a turn and discarding all but
one float. Both widened it, to different depths — X to
`(percentage, used, limit)` for the status chip, K to the entire breakdown
for `/context`. Shipping both would have meant two calls and two caches of
one measurement, which is exactly the duplication each item set out to
remove and would let the chip and `/context` come to disagree about the
same session. So there is now **one call and one cache**:
`_safe_context_usage` measures and caches the whole reply,
`_safe_ctx_usage` keeps its v0.35.0 name, signature and contract but reads
the triple off that cache, and `_safe_ctx_percentage` is gone, superseded.
Item X's `_as_tokens` rule — a non-numeric, negative or absent count is
*unknown*, never a confident `0`, and an absent window size is never
defaulted to 200000 — now governs `/context`'s figures too, so both
surfaces apply one honesty rule rather than two similar ones. Every
v0.35.0 test still passes unchanged, including the real-socket assertion
that a reattaching client gets the absolute pair from the status reply
alone; a new test counts control requests to prove a finished turn asks
once, and asserts the chip's three numbers equal the three the breakdown
carries. `labels.context_text` was renamed `context_breakdown_text` on the
way in, because v0.35.0 landed `labels.ctx_text` for the chip's words and
two near-identical names in one module is a trap.

### `/context` — the number behind the number

The status bar has shown a context percentage since v0.22.0, and it has
been one opaque figure the whole time: 73% of *what*, spent on *what*.
`/context` is the breakdown — categories in tokens (system prompt, tools,
messages, free space), the window they sit in, the `CLAUDE.md` files that
were loaded, and what each MCP tool costs, each with its share of the
window. It is registered in `doxa/commands.py`, so `/help`, the Ctrl+P
palette and the `/` autocomplete all got it for free, which is what that
registry is for.

- **Nothing on that screen is estimated, and that constraint shaped the
  feature.** Every token figure is the `claude` CLI's own accounting of
  its own request, taken from `ClaudeSDKClient.get_context_usage`. DOXA
  runs no tokenizer, holds no per-component model, and prints no row it
  was not given a number for — a category the CLI sends without a count
  is omitted rather than rendered as `0`, and a session that cannot be
  asked at all prints one sentence saying so and no digits whatsoever. An
  invented number in a diagnostic surface is worse than a missing row: it
  is a wrong answer that looks like a measurement.
- **One accounting path, not two** — see the collision note above.
  `_safe_context_usage` is the single place this session asks the CLI and
  the single place the answer is cached; the chip's percentage, item X's
  absolute pair and `/context`'s breakdown are all reads of that one
  reply. A refactor of existing machinery, deliberately, rather than a
  second call with its own failure modes.
- **The one thing DOXA measures itself is reported in characters.** The
  LORE snapshot is appended to the system prompt at connect, so the CLI
  counts its tokens inside the system-prompt category and cannot separate
  DOXA's appendix from the preset. DOXA knows the snapshot's exact length
  in *characters* and nothing more, so that is what it says, with a note
  about where the tokens landed. Dividing by four and printing a token
  figure would have been the estimate this whole surface refuses.
- **Judgment call — what the breakdown carries.** The SDK's reply also
  offers per-agent, per-system-tool, per-system-prompt-section, slash
  command and skill decompositions, plus `gridRows`, a pre-rendered pixel
  grid of the categories. `/context` shows categories, memory files and
  MCP tools; the rest is dropped. Reasons, not brevity: `gridRows` is by
  far the largest field and duplicates data DOXA draws itself, and the
  remaining lists are further decompositions of a category already shown
  whole. Those three survive because they are the three an operator can
  act on — what the window went to, which memory files loaded, and what
  DOXA's own in-process LORE tools cost.
- **Judgment call — a live call, not the cached one.** `/context` asks
  the CLI fresh rather than serving the last turn's snapshot: tool results
  have very likely landed since `turn_done`, and a user typing `/context`
  is asking about *now*. The cached snapshot is the fallback when the live
  request cannot be made.
- **Over the daemon socket too.** Daemon mode is the mode DOXA ships in,
  and only the daemon holds the SDK client, so there is a `context` RPC
  and an `EngineClient.context_usage` beside it. It needs no pager (unlike
  `beliefs` and `pending`): `engine.context_breakdown` drops `gridRows`
  and caps every list, and a test encodes a full reply to prove it fits
  `MAX_FRAME_BYTES`.

### `!` — a shell command, from the prompt

A prompt line starting with `!` runs as a shell command in the session's
own directory and renders its output in the transcript. `!git status`
reports on the worktree the agent is editing. The exit code and duration
are always shown, including for a command that printed nothing.

**This executes arbitrary commands with the user's full privileges. There
is no sandbox, no allowlist and no confirmation step.** `!rm -rf ~` deletes
the home directory. That is the intended semantics of a shell escape, and
it is safe for exactly one reason, which is written into
`doxa/shell.py`'s docstring as a rule rather than left as a convention:

> Nothing the model can produce, and nothing that arrives from outside
> this window, may reach the executor.

- **It is not a slash command, and that is the security decision, not a
  style one.** The slash registry is the one command surface dispatched
  *by name* from somewhere other than a keystroke — a status-chip click
  runs a registry row today, and docs/plugin-api.md §1 proposes
  third-party rows tomorrow. A `/shell` row would put an arbitrary-command
  executor behind a dispatcher that takes a string. So: `!`-prefixed only,
  no `/shell`, no palette entry, no autocomplete entry, no name for
  anything to pass. `/help` documents it in prose, in its own section,
  precisely because the registry must not carry it as data.
- **It is not a tool.** Absent from `doxa/operators.py`, so `to_sdk_tools`
  cannot project it onto the in-process MCP server and the model has no
  call that lands there.
- **Exactly one module imports the executor**, `doxa/session/pane.py`,
  which owns the prompt's submit handler. A test parses every module in
  the package and asserts that set is exactly `{session/pane.py}`, so
  wiring a second route in fails there rather than shipping quietly. A
  companion test asserts `PromptInput.Submitted` has exactly one producer.
  Both were checked against a deliberately unsafe build (a `/shell` row
  plus a handler) and both went red.
- **Judgment call — the output does NOT enter the model's context**, and
  is not persisted to the session transcript either, so it does not
  survive a tab restore and does not reach LORE's deriver. `!` is the
  user's private side-channel; a user who wants the model to see the
  output pastes it in. The alternative (feeding it back as context) would
  have made a keystroke-only surface into a context-injection surface,
  which is a strange thing to build directly beside the rule above.
- **Its own block kind**, with its own theme rule: a green left rule and a
  `❯` command line, and neither the `▎` accent a turn wears nor the
  `▎ doxa` prefix a system block wears. Confusing shell output with the
  assistant's words is the specific failure that styling exists to
  prevent, and a test asserts the block is not a `SystemBlock` and does
  not carry that prefix.
- **Containment.** stdin is `/dev/null` (a TUI has no terminal to hand
  over mid-command, so `!git commit` fails immediately instead of hanging
  on an editor it can never get); stderr merges into stdout in order;
  output is capped at 64 KB with the dropped byte count reported rather
  than silently truncated; a command still alive after 120 seconds has its
  whole process group killed, which is why the child gets
  `start_new_session` — killing only the `sh` would orphan the pipeline
  behind it. The reader keeps draining past the cap, because a reader that
  stops reading turns "printed too much" into "hung".
- **Judgment call — it waits for the session to boot** before running, the
  same as every slash command does, so the cwd is the session's real
  worktree rather than wherever DOXA was launched from. Correct directory
  beat instant availability; in practice the wait is a no-op.

### Tests

42 new tests: 21 in `tests/test_shell.py`, 19 in `tests/test_context.py`,
plus 2 daemon round-trips in `tests/test_daemon.py`. They assert what is on
screen — the output rendered, the exit code shown, the category names and
token counts in the block, the absence sentence with no digits in it — not
that a function returned. The security assertions read as security
assertions and were verified against an unsafe build rather than merely
against the pre-change tree, where a "the model cannot reach it" test
passes vacuously for want of anything to reach. No v0.35.0 test was
changed to make the merge fit; two of the new ones exist specifically to
pin the reconciliation described above.

One defect was caught by its own test during development: the normalizer
defaulted a missing token count to `0`, which would have rendered a row the
CLI never measured as a measured zero. Defaults are `None` now, and the
renderer drops a row it has no number for.

## 0.35.0 — 2026-08-25

Two lettered items: **X (ctx absolute)** and **Z (about)**.

**A note on provenance, because it changes how this entry should be
read.** The written specs for both items were lost before the work
started. What shipped is re-derived from each item's name plus the
codebase as it stood at v0.34.0, and every place the name alone did not
settle a question is a judgment call flagged below rather than presented
as a requirement. Nothing here goes beyond what the two names plainly
imply; two further letters (K, Q) were in flight elsewhere and W is on
hold, and none of them are touched.

### X — ctx absolute

The `ctx` chip could only ever say a percentage, which cannot answer the
one question anyone asks it: 12% of a 200k window and 12% of a 1M window
are different situations, and DOXA drives models with both.

- **One reading, not two.** The SDK's `get_context_usage()` already
  returns `totalTokens` and `maxTokens` in the same reply the percentage
  comes from, so `SessionEngine._safe_ctx_percentage` became
  `_safe_ctx_usage` and returns all three from the one call it was already
  making. No second accounting path, no extra round trip, and the three
  numbers cannot disagree because nothing computes them separately.
  `turn_done`, `usage_summary()`, the daemon's `status` reply and
  `EngineClient`'s cache all carry the pair alongside the percentage, so a
  detached session's status bar is not poorer than an in-process one.
- **Judgment call: the tooltip is the guarantee, the inline segment is
  opt-in.** The item's name says make the absolute numbers reachable; it
  does not say where, and four readings were open (replace the percentage,
  sit beside it, tooltip only, or a setting). The chip's tooltip now
  carries `24,000 of 200,000 tokens used, 176,000 left`
  **unconditionally**, because that costs the most contended row in the
  app exactly zero columns; the inline `24k/200k` form is a new
  `ctx_absolute` setting, off by default, so nobody's status bar changes
  width without them asking for it. Replacing the percentage was rejected
  outright — the README calls the escalating percentage "a containment
  signal, not decoration", and a raw token count is a worse signal for
  that job.
- **Judgment call: degrade by dropping, not by truncating.** With the
  setting on, the segment is omitted below 100 columns
  (`CTX_ABSOLUTE_MIN_COLS`). The status bar does not scroll — overflow
  pushes the chips to the right of it off the end of the row — so the
  segment that is a convenience gives way to the chips that are
  information. It is re-evaluated on the ordinary event-driven refreshes
  (boot, turn done, peer events) and deliberately **not** from a resize
  hook: `_refresh_status` runs a belief `COUNT(*)`, and hanging that off
  every frame of a mouse-drag resize would recreate the idle-CPU
  regression this app already paid to shed.
- **Judgment call: an unknown limit stays unknown.** A window size the CLI
  never reported prints `?` in the chip and "window size not reported" in
  `/usage`, and the tooltip says the limit "is not something this
  session's CLI reported". There is no fallback constant: a prior
  measurement in this project found the Models API unreachable under
  OAuth-only auth, so a hardcoded 200000 would be a number DOXA invented,
  printed in the same sentence as two it measured.
- **A defect found on the way in.** The ctx chip's tooltip was keyed by
  its own **markup**, while `StatusBar._tooltip_for_x` resolves a hover by
  finding the chip's text inside the bar's markup-*stripped* string — so
  at the amber and red tiers, where the chip carries a color span, the
  lookup could never match and the hint silently vanished. Exactly the
  tiers where a reader most wants to know how many tokens are left. The
  chip is now keyed by its plain text (`labels.ctx_text` builds the words,
  `ctx_chip` colors them, one function each), with a test pinning the hint
  at 93%.
- `/usage` gained the exact figures, with separators, on the same row as
  the percentage.

### Z — about

`/about`: a focused `ModalScreen` carrying the version and the rest of
what a bug report has to state — Python, Textual, Claude Agent SDK, the
LORE plugin version and store path, the platform, the config file in
force, and the repo/licence line (public repo, Noncommercial 1.0). Esc
closes. Registered in `doxa/commands.py`, so `/help`, the palette and
autocomplete all get it from the one registry, and in `PANE_COMMANDS` on
the executor side; the closure test that keeps those two honest passes
unchanged.

- **Heeding the v0.28.0 defect.** `#compact-confirm-buttons { height: 1;
  padding-top: 1 }` under Textual's border-box model rendered buttons at
  **zero** height — present in the DOM, drawn nowhere — and shipped that
  way for a full release because the tests asserted the modal had been
  *pushed*, never that anything was *visible*. `#about-buttons` is
  `height: auto` for that reason, and `tests/test_about.py` asserts the
  button row's rendered height, hit-tests both doors at their own centres,
  and reads the body's text off the widget that actually drew.
- **Judgment call: a modal, not a transcript block.** `/about` describes
  the installation, not the conversation. A `SystemBlock` would scroll
  away by the next turn and then be fed back to the model as context it
  has no use for, and it has nowhere to put a copy door.
- **Judgment call: reuse the boot-time update check, add nothing.**
  `DoxaApp._check_for_update` already runs one `git fetch` per launch on a
  worker; it now records its *answer* (`update_available`) as well as
  firing its notification, and `/about` reads that. The dialog opens no
  network call of its own — a modal that fetches on the UI thread is a
  modal that hangs. Three states, and the third is load-bearing: `True`,
  `False` (checked, nothing to pull) and `None` (nobody looked, or the
  check failed the silent way it is designed to). `None` prints nothing,
  because "up to date" and "unchecked" are different claims.
- **Every row is measured.** The interpreter reports its own version, the
  dependencies report theirs (`__version__` first, distribution metadata
  second), the config path is resolved rather than assumed. `lore_core`
  carries no `__version__` at all, so the LORE row reads the plugin
  manifest beside it (`.claude-plugin/plugin.json`) rather than DOXA
  inventing a version attribute in somebody else's read-only repo — and
  the store path comes from `lore_core.ROOT` itself, not from re-reading
  `LORE_ROOT`, which lore_core resolves once at its own import and which
  could since have drifted. A row whose source cannot answer is
  **omitted**, never filled with a plausible constant: the whole job of
  this screen is to be quotable.
- `c` copies the exact visible text to the clipboard through the app's own
  `copy_to_clipboard`, the same door the sessions picker already uses.
- **Judgment call: the sha is always shown here.** `version_line` hides it
  when the surrounding view already carries it, because the identity block
  sits directly above a git chip printing the same hex string. `/about` is
  its own screen with no such neighbour, and "which commit is this code"
  is the second thing a bug report needs after the version.
- **Judgment call: the settings modal's "About" tab was left alone.** It
  already existed and answers a different question — account, plan,
  organization — and merging the two would be scope this item's name does
  not imply. It gains one sentence pointing at `/about` for the build
  report, so the app has two surfaces and one pointer rather than two
  half-answers to "what am I running".

### Tests

**807 green** (785 before, 22 new: 13 for X, 9 for Z). Every new test was
run against pre-change code first and fails there — the point of the bar
v0.28.0 set is that a test which cannot fail is not evidence. The
assertions are user-visible outcomes throughout: the status bar's
markup-stripped text, the tooltip the bar hands back for a real hover
coordinate, rendered widget height, the screen's own hit test, the
generated `/help` text, and a real daemon socket for the engine/client
parity.

`tests/test_tab_status.py::test_done_unseen_marks_a_background_tab_and_clears_on_activation`
remains the known flake (mount-time prompt focus activates a tab
asynchronously); focus ownership is a queued decision and was not touched.

## 0.34.0 — 2026-08-24

`doxa/app.py` came apart. It was 6,415 lines — 36% of the package, larger
than the next six modules combined, and the file every feature of the last
several releases landed in, which is also why three consecutive rebases
conflicted there. It is now 1,403 lines: `DoxaApp` and a facade.

This is a refactor and nothing else. No feature, no defect fix, no renamed
user-visible string. The proof is the suite it did not touch: **785 tests
green, the same 785, unchanged** — no test was edited to accommodate the
split, and the one place where that was a live risk is called out below.

The shape is not arbitrary. `docs/plugin-api.md` was written *before* this
split for exactly this reason: the four things that spec says a plugin
would most obviously want to add were the four things `app.py` hardcoded,
so each extension point is the seam the split follows. The file came apart
along the lines a later plugin API would attach to, rather than along
whatever boundary happened to be convenient this week.

- **Where things went.** Widgets to `doxa/ui/`, one module per surface:
  `labels.py` (431 lines — the ~20 pure formatters and the constants they
  read; imports nothing else in the package's UI layer, which is what
  keeps the graph a tree), `transcript.py` (811 — the blocks a turn
  renders into, plus `mount_transcript`, which builds a restored tab out
  of exactly those same classes), `dialogs.py` (716 — every surface the
  user answers), `statusline.py` (535 — `StatusBar`, `GitLine`,
  `ClockChip`), `prompt.py` (396 — `PromptInput` alone, because it is the
  single arbiter of what a keystroke means with three popups open above
  it, and filing the arbiter under one of the things it arbitrates
  between would be the wrong shelf). `SessionPane`'s 2,404 lines went to
  `doxa/session/`: `commands.py` (653), `chips.py` (833), `runtime.py`
  (659), and `pane.py` (634) for what remains.
- **Mixins, not helper objects.** All 95 of `SessionPane`'s methods read
  and write pane state through `self`. Three plain mixins move that code
  with **zero call-site churn**; helper objects would have meant rewriting
  hundreds of call sites, which is how a refactor stops being reviewable.
  `SessionPane` is still one class with one name. Textual's own machinery
  is untouched by the extra bases: `_css_bases` walks `__bases__` for the
  first `DOMNode` subclass and these mixins are plain objects, so the
  pane's CSS type names are byte-for-byte what they were.
- **`theme.tcss` needed no edit, and that was checked rather than hoped.**
  Textual matches CSS TYPE selectors on the class NAME, never the module
  path, so moving `SessionPane`, `TurnBlock`, `ToolChip`, `CompactConfirm`
  and the rest between files is CSS-safe by construction. The file is
  unchanged in this release.
- **`@on`-decorated handlers stayed in `pane.py`, for a mechanical
  reason.** Textual's `MessagePumpMeta` collects decorated handlers out of
  the class body it is *constructing*, so a decorated handler written in a
  plain mixin would never be dispatched. Handlers found by naming
  convention do work from anywhere in the MRO, but "handlers live in
  pane.py" is a rule worth having whole rather than half.
- **The facade is the contract.** 39 modules, scripts and tests import
  from `doxa.app` by name — 49 distinct names between them — and 54 files
  reference the module in one form or another. Not one of them was edited.
  `doxa/app.py` re-exports every name it exported before, and its import
  block is kept whole for the same reason: a module namespace other
  modules read is a compatibility surface, and trimming it to "what
  `DoxaApp` still uses" would break importers this file has no business
  knowing about. Verified mechanically rather than by eye — `dir()` of the
  post-split module is compared against `dir()` of the pre-split one and
  is identical, and every imported name is resolved against it.
- **Seam 1, slash commands.** `_command_handlers`'s literal 20-row dict is
  now `session.commands.PANE_COMMANDS`, an ordered tuple of frozen
  `CommandBinding(name, method, args)` records the method binds against
  the pane. `/login` and `/logout` being one handler with two spellings is
  now written down as data instead of two `partial` calls in a dict
  literal. The closure test — `_command_handlers().keys() ==
  commands.interactive_names()` — is as true of a built dict as it was of
  a literal one, and is unchanged.
- **Seam 2, status chips.** `_refresh_status` was 157 lines appending
  markup to one list and tooltip rows to another, keeping the two in step
  by hand across twelve conditional appends. It is now four lines over
  `_status_chips()`, which returns `list[StatusChip]` in paint order. Each
  record carries its own markup **and** its own hints, so the two cannot
  drift; a chip that owns pre-built markup (the git chip, the
  pressure-colored ctx chip) says so through `StatusChip.raw` rather than
  being wrapped in an escaper that would mangle its own brackets. Every
  chip's tier, hide-at-zero rule and hint text is character-for-character
  what it was.
- **Seam 3, transcript blocks.** `_handle_event`'s `if/elif` over six
  event types is now `EVENT_RENDERERS`, a module-level dispatch map, one
  method per type. Module-level and not pane state deliberately: that is
  where the spec's rule that a plugin may *add* an event type but never
  *replace* a built-in renderer will be enforced — a plugin that can
  silently redraw `tool_result` can lie to the user about what a tool did.
  An unknown event type is still ignored, exactly as the old chain's
  missing `else` did.
- **Seam 4 came back short, and is reported rather than papered over.**
  `providers.ModelProvider` is the right shape for the *catalog* half of
  the engine-provider point: the picker asks a provider what it can offer
  and never branches on who the provider is. The *session* half is not in
  it at all — spawn, send, interrupt and the event stream are what
  `SessionEngine` and `EngineClient` agree on informally, by both exposing
  the same async-iterator surface, with no Protocol naming it. That is a
  second Protocol and it is feature work for multi-provider engines, not
  something a refactor gets to invent. The finding is recorded in the
  Protocol's own docstring.
- **No loader, and no groundwork for one.** Entry-point discovery, the
  allowlist, `Plugin`/`PLUGIN`, third-party loading, plugin settings rows:
  none of it shipped, none of it is stubbed. This release gained no way to
  load code it did not already load. What it gained is four structures
  such a loader would attach to, each useful on its own terms today.
- **Judgment call: `_stop_session` did not move.** Its only caller did
  (`/sessions kill`), but it is the app-scope stop primitive — the same
  one quit-stop and `doxa stop` reach — and the suite swaps it by patching
  `doxa.app._stop_session`. Moving the definition would have left that
  patch pointing at a name nothing reads, and a monkeypatch that silently
  stops applying does not fail loudly, it fails as a test that still
  passes while testing nothing. It stays in `doxa/app.py`; `_kill_sessions`
  imports it per call, so the patch keeps working. Three other deferred
  imports back onto `doxa.app` exist for the same class of reason and are
  each commented where they sit (`app_bindings` reading the live
  `DoxaApp.BINDINGS`, and two `isinstance(app, DoxaApp)` checks).
- **Docs.** `docs/plugin-api.md`'s status line no longer says nothing is
  implemented — the four extension points now each name the structure that
  exists, and the "where it is hardcoded today" table gained a column
  saying where the seam is instead.
- **Tests: 785 green, zero edited.** Every definition that moved was
  diffed by AST against its pre-split self — 359 of them, of which 13
  differ, and every one of the 13 is named above or is a relative-import
  depth fix (`from . import doctor` reads one package deeper now). Nothing
  moved silently.

## 0.33.0 — 2026-08-24

A session's own branch was on the list of branches it could be based on,
and picking it deleted work. Queue item S (branch switch / branch
selection) is re-visited here: the item's original lettered spec was lost
before this session and was re-derived a second time from the item name
plus the shipped surface, which is worth saying out loud because the
re-derivation is what found this — the spec text ships nothing new, the
audit against it does.

Item S shipped whole in 0.20.0 (`doxa new --branch <name>`, the `/branch`
command and its daemon RPC), gained a status-bar picker in 0.27.0 and a
label fix in 0.28.0. Everything that spec asks for was already in place
and is unchanged here. What no version of it ever said is which branches
are NOT bases, and that omission had teeth:

- **A session could be based on itself, and then lose its commits.**
  `/branch` (and the chip picker built from the same listing) offered
  every local branch, which inside a worktree-per-session session
  includes the session's OWN `doxa/<id>` — identity, never something to
  fork from. Selecting it did not fail; it reported `doxa/<id> now based
  on doxa/<id>` and wrote a sidecar whose `base_ref` equalled its
  `branch`. From that moment the session's safety rail was disarmed:
  `git rev-list <branch>..HEAD` is structurally zero when the two are the
  same ref, so `commits_ahead` could only ever answer 0, and
  `worktrees.finalize`'s "clean and zero commits ahead" test — the one
  deciding between removing a spent worktree and keeping real work — read
  genuine unmerged commits as nothing to keep and ran `git worktree
  remove --force` plus `git branch -D` over them at session end. The
  branch and its commits were unreachable, with `finalize` returning
  `None` ("nothing to report") rather than the `kept doxa/<id> — merge
  when ready` this exact situation exists to produce. Fixed in three
  places, because the defect had three surfaces and only fixing the
  visible one would leave the others live:
  - `worktrees.switch_base` REFUSES a target that resolves to the
    session's own branch, before the rebase — the load-bearing guard,
    since it also covers a hand-typed `/branch doxa/<id>`, which no
    amount of filtering the picker would catch. The refusal names the
    base being kept, matching the actionable voice of the dirty-tree and
    commits-ahead refusals beside it.
  - `worktrees.branch_status` no longer OFFERS it, so the picker and the
    no-argument `/branch` listing stop showing a row whose only possible
    outcome is that refusal. The filter keys off the sidecar's `branch`,
    so a session with no worktree (`worktree_per_session` off) is
    untouched and still lists the branch it is on — there it IS the base,
    and is marked as such.
  - `worktrees.finalize` treats `base_ref == branch` as UNMEASURABLE
    rather than zero. This is the one that matters to anyone who already
    hit the bug: a corrupted sidecar is sitting on their disk right now,
    and unmeasurable has always meant keep, so their next session end
    returns their work instead of destroying it.
- Judgment call: OTHER sessions' `doxa/<id>` branches stay in the
  listing. Basing a session on what another session built is a real thing
  to want, and it fails safe on its own — when that branch is later
  removed, `commits_ahead` cannot measure and `finalize` keeps, which is
  the correct end of the trade. Only the self-reference is unsound, and
  only it is refused.
- Judgment call: the `branch` RPC is still UNPAGED, deliberately, against
  the `_fit_page` budget `beliefs` (0.28.0) and `pending` (0.31.0) share.
  Measured rather than assumed: those two page because their rows are
  free text and one real store blew the 64KB frame at ~517 rows. Branch
  names are short and bounded, and a reply needs roughly 3,800 of them to
  reach the same cap. A third budget implementation would be carried for
  a frame that does not overflow.
- VERIFIED, not rebuilt: 0.32.0 persists a `cwd` per restored tab and
  nothing else about its base, and a base switch moves the worktree's
  branch pointer without ever moving the worktree directory — so a
  restored tab re-reads the new base from the same sidecar and needs no
  change here.
- Tests: real git repos and real worktrees throughout (house pattern, no
  mocking of git) in `tests/test_worktrees.py` — the own-branch refusal
  and that it leaves the sidecar's base intact, the listing dropping it
  while a no-sidecar checkout keeps listing its own branch, and the two
  outcome tests that state the actual stake: a real commit still KEPT at
  session end after a refused self-switch, and a sidecar already recording
  its own branch as base surviving finalize with its branch and commits
  reachable. Each of the four regression tests was confirmed to FAIL
  against the pre-change module. 785 tests green (780 baseline + 5).
- README: the `/branch` line in the daemon/worktree section now says which
  branches it offers. No screenshot changed — the palette gallery shot
  drives a plain checkout with no worktree in play, where the listing is
  exactly what it was.

## 0.32.0 — 2026-08-24

Restore brings back the VIEW, not just the tab list. Reported: "i meant to
restore the view, also any prior vertical or horizontal panel split and
amount of opened tabs and their content, after it was closed". Written
defect-then-fix, with the measurement that identified each cause.

- **A restored tab came back EMPTY when the session was longer than the
  daemon's replay ring.** THE DEFECT: v0.23.0's tab restore reattached
  every saved tab to its still-live daemon and let the daemon replay its
  event ring (`doxa/daemon.py`, `EventRing.since`) to the fresh client.
  That worked for a SHORT session, which is why it read as "content
  restores" and shipped. THE CAUSE, measured against a real daemon over a
  real Unix socket before anything was changed: the ring holds
  `RING_CAPACITY` = 512 frames and one `text_delta` is one frame, so a
  single 700-delta answer pushes `turn_started` off the far end. Once it
  is gone, `SessionPane._peer_pump`'s `if self._oob_turn is not None`
  guard has no `TurnBlock` to render the survivors into and drops every
  one of them on the floor. The numbers from that run: ring `next_seq`
  702, oldest buffered seq 190, restored tab rendered **zero** turn
  blocks — beside a live daemon that still held the whole conversation —
  and said nothing about it. THE FIX, in two parts. (1) A restored pane's
  scrollback now comes from the session's own persisted transcript
  (`doxa/transcript.py`, new): `$LORE_PROJECTS_DIR/<slug>/<session_id>.
  jsonl`, the file `SessionEngine._persist` already writes at the scrub
  choke point and `lore_store.index_live` already indexes for `/search`.
  It is complete where the ring is a tail, it is already scrubbed, it
  outlives the daemon, and it is on the same machine as the TUI. (2) The
  orphan case is no longer silent anyway: a turn event with no
  `turn_started` in front of it (a reattach landing mid-turn) opens an
  unattributed turn block that says `(turn already in progress)` instead
  of vanishing.

- **How the 64KB frame cap is handled here: by not putting a transcript
  on the wire at all.** `doxa.daemon.encode_frame` enforces
  `peers.MAX_FRAME_BYTES` (64KB) and v0.28.0 had to page the beliefs RPC
  for exactly that reason (500 beliefs measured at 230KB, 3.6x the cap);
  v0.31.0 then generalized that into the shared `_fit_page` byte budget
  and put the new `pending` RPC through it too. A transcript is far bigger
  than either, so a `transcript` RPC would have needed the same paging —
  and would have been a THIRD caller of that budget, for bytes that never
  had to move at all. The daemon socket is a Unix socket: client and
  daemon are the same user on the same machine, so the TUI reads the file
  directly. There is no frame on this path, so `_fit_page` is deliberately
  not involved and gains no third caller: one implementation, one budget,
  for the things that really do cross the wire. What IS capped here is the
  RENDER, and every cut says so on screen: 40 turns
  (`transcript.DEFAULT_TURN_LIMIT`, earlier ones announced as "N earlier
  turns not shown — the full transcript is on disk (/search)"), 20,000
  characters of prose per turn (marked "…answer truncated for restore"),
  30 tool chips per turn (a counted chip for the rest). A truncated
  transcript never renders as if it were complete. Turns mount in batches
  of six with a yield between them so a long restore cannot freeze the
  first paint of the other tabs.

- **The ring is no longer replayed on top of what was just drawn.**
  `EngineClient(sock, skip_backlog=True)` attaches at the daemon's CURRENT
  ring head, read from the `next_seq` the hello frame has advertised since
  the protocol's first version — so no protocol change, no version bump,
  and no `doxa stop` forced on anyone. A daemon that does not advertise a
  head refuses the skip, and a pane that could not skip renders nothing
  from disk: v0.31.0's replay-only behaviour, unchanged, rather than a
  doubled transcript.

- **A session whose daemon had ended did not come back at all.** THE
  DEFECT: `tabsets.resolve` cross-checked every saved tab against the live
  daemon registry and dropped whatever no longer answered, leaving the
  user one line of arithmetic ("skipped 1 session no longer running") in
  place of the tab and everything in it. Correct as far as it went — a
  dead session must never be replaced by a fresh one wearing its tab — but
  a daemon finalizing on its linger timer while the window is shut is the
  ORDINARY way a session ends, so "the tabs come back" quietly meant "some
  of them do". THE FIX: a saved tab with no daemon but a transcript on
  disk now comes back as an `ArchivedSessionTab` — same strip, same order,
  same pinned name, the whole conversation rendered by the same code path
  a live restore uses, and no engine, no prompt, no way to type into a
  session that does not exist. Deliberately not a `SessionPane`, exactly
  like `SubagentTranscriptTab` before it: a prompt box that refuses every
  prompt is worse than no prompt box. **The user can always tell which
  they got** — the report distinguishes "restored 2 tabs" from "1
  read-only transcript (session ended)", the tab wears a `⏺`, and the
  tab's first block says the session has ended and where the text came
  from. An archived tab stays in the persisted set (it survived one
  restart, it survives the next); Ctrl+W closes it and takes it out. A
  saved id with neither a daemon nor a transcript is still dropped and
  still counted. An all-archived restore always opens one live session
  beside the archives, so the window is never left with nothing to type
  into.

- **With three or more restored tabs, the saved ACTIVE tab lost to
  whichever pane mounted last.** THE DEFECT, latent since v0.23.0 and
  found while measuring this work: `SessionPane.on_mount` focuses its
  prompt, and focusing a widget inside a `TabPane` activates that pane —
  so every restored pane doing it left the LAST one active whatever
  `DoxaApp.on_mount` had set from the record. The existing two-tab test
  never caught it because its saved active tab happened to BE the last
  one, so the wrong mechanism produced the right answer. Measured with
  three tabs and a middle active id: landed on tab three every time. THE
  FIX: during a restore exactly one pane — the saved active one — focuses
  on mount; `_on_tab_activated` focuses the rest when they are activated,
  as it always did.

- **An event arriving before a pane finished composing killed its
  out-of-band pump for the life of the tab.** Found the same way: the
  trailing `self._refresh_status()` in `_peer_pump` raised `NoMatches` on
  `#status-bar` and took the worker down with it. That loop is the ONLY
  renderer of replayed history, another client's turn, and `needs_input`,
  so one unlucky moment left the pane deaf. A status-bar repaint is now
  never allowed to end the pump — skipping one is invisible and the next
  event does it again.

- **Splits are NOT restored, because DOXA has no splits.** The report
  asked for "any prior vertical or horizontal panel split" too. There is
  no vertical or horizontal split layout in DOXA to restore: `SessionPane`
  is a `TabPane` and the window is a tab strip. Recursive split panes are
  a separate, unspecced feature, and **split restore waits on split panes
  existing**. What this release does do is make the record able to carry
  one later without a breaking change: alongside the flat `tabs` list it
  now writes `{"layout": {"kind": "tabs", "tabs": [...]}}`, the slot a
  `{"kind": "split", ...}` node would grow into. The flat list stays at
  the top level and stays authoritative, so a record written here still
  restores under v0.23.0–v0.31.0, and `load` still prefers it, so a record
  written there restores here. A `layout` node whose `kind` this version
  does not recognise reads as "nothing to restore" rather than a split
  silently flattened into tabs. Cost: one dict on write, one branch on
  read.

- Saved tab records now carry the session's own `cwd`. A dead session has
  no registry entry left to ask where it ran, and its transcript lives
  under the project slug of that directory — which, with
  `worktree_per_session` on, is its linked worktree and not the repo root.
  Absent on every record written before this version, which falls back to
  the scope key; a wrong guess costs an archived tab, never a wrong
  transcript (the file is keyed by session id, so a miss is a miss).

- Startup behaviour is unchanged for existing users: `restore_tabs` still
  defaults ON and still gates whether a launch reads the record at all,
  and an ordinary launch in a directory that HAS a transcript still opens
  an empty pane. Restore is the only thing that reads a transcript back.

## 0.31.0 — 2026-08-24

The streaming background reviewer had been staging memory proposals into
the approval gate for several releases without any reliable way to find
out. Reported: *"we are still missing doxa internal notifications for
streaming background reviewer (e.g. when the deriver extracted sth that is
not rejected immediately)"*. Three separate defects sat behind that one
sentence; each is written defect-then-fix below. Nothing here touches the
write path — see the scope note at the end.

- **The announcement was invisible unless you were already looking at the
  right pane.** THE DEFECT: `SessionEngine._derive_once` emitted a
  `derive_done` event and `doxa/app.py` had exactly one consumer for it —
  it mounted a `SystemBlock` into that pane's `#block-list` and stopped
  there. No tab-status change, no desktop notification. The two other
  "something happened while you were elsewhere" events in this app
  (turn-done, needs-input) both get a `notify.notify_if` banner AND a tab
  affordance; a background reviewer that runs *by definition* while your
  attention is somewhere else got neither, so on a live config with
  `derive_secs = 77` the reviewer could stage all session and say so to an
  empty room. THE FIX: the same event now drives three surfaces. A new
  `notify_staged` trigger (`DOXA_NOTIFY_STAGED`, defaults on, its own row
  in the settings modal) routes through the same `notify_if` gate every
  other trigger uses, so it fires only while the DOXA window is unfocused
  on the default `auto` mode — a staged proposal is never urgent and must
  never interrupt someone already looking. The tab gains a `-staged`
  class, written through the SAME `_set_tab_class` door as
  `-working`/`-done-unseen`/`-attention` rather than a second mechanism,
  and it is a STEADY muted-violet tint, not a blink. That is a
  deliberate reservation: blinking says "this session is stopped until you
  act", which is true of a permission prompt and false of a staged
  proposal — nothing is blocked, nothing expires, nothing reaches curated
  memory without an explicit approval. It also costs no `set_interval`,
  and this state can legitimately persist for a whole session. Both the
  tint and the block clear the way `-done-unseen` does: the moment you
  look at the tab.

  One consequence, handled rather than shipped: `lore_core`'s own
  `deriver.notify_staged` already fired a banner off this same review
  path, focus-unaware. Two notifiers for one event is two banners, so
  `notify.sync_lore_notify_env` now silences `LORE_NOTIFY` while DOXA's
  own trigger is on — closing the gap that function's docstring had been
  recording as out of scope since it was written ("closing that gap needs
  `doxa/engine.py` to call through `doxa.notify` itself"). Turning
  `notify_staged` off hands the job straight back to `notify_lore`, so
  nobody ends up with silence they did not ask for.

- **A count is not information.** THE DEFECT: the event carried
  `{"staged": N}` and the block read `N proposals staged`. That answers
  "did something happen" and nothing else — you could not tell a batch
  worth approving from three restatements of something the store already
  knows without leaving DOXA entirely. The proposal TEXTS were reachable
  in-process the whole time (`_pending_count` was already calling
  `lore_deriver.pending_texts(slug)` and throwing the strings away to
  return a length). THE FIX: `derive_done` carries the texts. They are
  diffed as a MULTISET against the pending list from before the review, so
  the preview shows what *this* review added rather than the tail of a
  queue that may be mostly weeks old — and two genuinely distinct
  proposals that happen to share byte-identical text no longer collapse
  into one, which a set difference would have done silently. Every text
  routes through `lore_core.scrub.scrub_secrets` (staged proposals are
  transcript-derived, so `doxa/engine.py`'s secret-scrub choke point
  applies), is collapsed to one line and ellipsized.

  THE FRAME CAP, handled at the producer this time rather than after a
  report. `doxa.daemon.encode_frame` answers an EVENT frame over
  `peers.MAX_FRAME_BYTES` (64KB) by replacing its whole payload with
  `{"truncated": True}` — silent from the TUI's side, where it would
  render as nothing at all. v0.28.0 paged the beliefs RPC for the
  non-event half of exactly this problem. So the payload is bounded by
  three independent limits before it is ever queued: 8 rows, 160
  characters each, and an 8KB byte backstop that has the last word (eight
  rows of 160 characters cannot reach it even when every character escapes
  to a six-byte `\uXXXX` sequence). Whatever the caps drop is COUNTED and
  said out loud — the block ends `… and N more` — because a partial list
  shown as a whole one is the one thing a list must not do. Pinned by a
  test that feeds 400 proposals of 4000 multi-byte characters each through
  the real `encode_frame` and asserts the wire line stays under the cap
  with no truncation marker in it.

- **The hint pointed at a command that does not exist here.** THE DEFECT:
  the block said `/lore:pending`. That is a Claude Code *plugin* command.
  It is not in `doxa/commands.py`, so typing it inside DOXA does not list
  anything — it goes to the model as prompt text. The one actionable
  sentence in the notification was a dead end. THE FIX: DOXA gets its own
  native surface. `/pending` is a real registry row (Memory group, on the
  palette, closed over by the same `_command_handlers` ==
  `interactive_names()` assertion every other command is), opening the
  shared `ChipPicker` the beliefs chip already uses — no new widget kind —
  with each staged proposal as an ellipsized row and a selection spilling
  the full text into a system block. The notification block itself is now
  a door rather than a signpost: its trailing line is a live
  `[@click=…]` span onto that same list, using the click-action pattern
  `StatusBar`/`SubagentLine` established. Over the daemon split the list
  comes from a new `pending` RPC, PAGED from day one — a staged proposal
  is free text of unbounded length, and `_fit_belief_page` was generalized
  into a shared `_fit_page` so the beliefs and pending RPCs enforce one
  byte budget with one implementation.

**Scope, stated rather than implied: this release is read-only.** There is
no approve/reject button, no approve/reject RPC, and no plan for one here.
The write path into curated memory and beliefs is under active security
review (`docs/plugin-api.md` §6, LORE issue #43), and the approval gate
must not gain a second door before that concludes. Listing and reading
staged proposals touches none of it; approving them stays with LORE's own
`/lore:approve` / `/lore:reject`. Two tests pin the boundary — one that
the picker offers no such row, one that the daemon refuses the method.

## 0.29.0 — 2026-08-24

- **Transparency-capable background (the `background` setting), default
  `opaque` — today's look, byte-identical.** THE CONSTRAINT, stated
  plainly because it is easy to overclaim: a terminal application cannot
  make the terminal WINDOW transparent — that is the terminal emulator's
  or compositor's job (kitty's `background_opacity`, WezTerm's
  `window_background_opacity`, a macOS Terminal profile, and so on). What
  an app controls is only whether it PAINTS its own background or leaves
  cells at the terminal's default. DOXA has painted a literal `#171512`
  on the screen since v0.13.0's restyle, which forces opacity no matter
  what the terminal is configured to do. `background: transparent` (new
  setting, Appearance category, `DOXA_BACKGROUND`) stops DOXA painting
  that base, so a terminal already configured transparent shows through.
  On an opaque terminal, this changes nothing visible — and the README
  says so, not just this note.
  - **What Textual 5 actually offers, checked against the installed
    version rather than assumed.** CSS `background: transparent` (alpha
    0) blends against the widget's own PARENT chain inside Textual's own
    compositor — it never reaches the real terminal, because some
    ancestor (Screen, by default) still resolves to an explicit painted
    RGB. The mechanism that DOES reach the terminal is the CSS keyword
    `ansi_default` (`Color(ansi=-1)`), which Rich renders as the raw SGR
    "default background" reset — `ESC[49m` — instead of any RGB, letting
    an already-transparent terminal show through underneath. This is not
    a guess: `Style.parse("on default")` was rendered through a real Rich
    console and the literal byte sequence checked. It is also not novel —
    it is exactly the mechanism Textual's own built-in `&:ansi`
    pseudo-class already uses on `App`/`Screen`, gated by the `ansi_color`
    reactive that its own `"textual-ansi"` built-in theme flips on.
  - **The non-obvious second half: `ansi_default` alone is not enough.**
    With `App.ansi_color` left at its default `False`, Textual's own
    `ANSIToTruecolor` filter rewrites an ansi-default background into an
    *approximated opaque RGB* pulled from the active terminal theme
    (confirmed empirically: it came back `on #0c0c0c`, near-black — the
    opposite of transparent, and it would have painted silently). Setting
    `App.ansi_color = True` disables that filter for anything carrying an
    ANSI color, which is what actually lets `ansi_default` reach the
    terminal unconverted (confirmed: `on default`, `ESC[49m`). DOXA never
    touches `App.theme`, so flipping this reactive on its own doesn't pull
    in Textual's `"textual-ansi"` theme or its 16-color palette — verified
    directly against the installed Textual that a CSS_PATH rule for a
    given widget+property always outranks the matching built-in `&:ansi`
    DEFAULT_CSS rule regardless of pseudo-class specificity, so every
    color theme.tcss already states explicitly stays exactly what it says.
  - **No partial-alpha "semi-transparent" middle value.** Investigated and
    rejected, not merely skipped: `Color.rich_color` never reads the alpha
    channel for an ansi-type color — `ansi_default 60%` collapses to
    exactly the same `ESC[49m` as `ansi_default` alone, ignoring the
    percentage. SGR "default background" is a binary reset, not a
    blendable value; there is no per-cell partial-alpha escape code for a
    real terminal to composite against. Two values ship: `opaque` and
    `transparent`.
  - **The v0.13.0 restyle's role tints survive, on purpose.** That release
    made background TINT carry role (raised prompt / base body / dimmer
    system-chrome / bordered chip), replacing block borders. Only the
    BASE token moved to the new `$doxa-base` CSS variable
    (`DoxaApp.get_theme_variable_defaults`, theme.tcss's one indirection);
    the other four rungs of the ramp (`#1D1B17` dimmer, `#221F1A` raised,
    `#2A251E` chips, `#3A3429` borders) stay literal, unconditionally
    painted hex — so a status bar, a tool-calls section or a tool chip
    reads as its own step against whatever the terminal shows through as,
    dark or light. The five modal washes (Ctrl+W confirm, v0.28.0's ctx%
    compact confirm, command palette, settings, `/setup` — all
    `#171512 60%`) are the deliberate
    exception: since ansi colors ignore alpha entirely (previous point),
    pairing `ansi_default` with a percentage would silently drop the
    dimming veil behind a modal, so those five keep the literal hex
    regardless of the setting. Every popup and dropdown in the house
    (settings panel, command palette, `/search`, the slash and chip
    pickers, the needs-input popup) already lived on the raised or dimmer
    tint rather than the base, so none of them needed a code change to
    stay opaque — audited selector by selector, not assumed.
  - **Legibility, checked, not assumed — and disclosed honestly.**
    DOXA's palette has never had a light-mode counterpart; that becomes
    visible, not caused, once the base stops supplying its own dark
    backdrop. Verified two ways: WCAG contrast computed for the body/
    secondary text against representative dark (13–17:1, comfortably
    above the AA floor) and light (1.1–1.6:1, effectively invisible)
    terminal backgrounds; and a real render of the actual app in
    transparent mode, with `ansi_default` cells forced through Textual's
    own terminal-theme approximation to black and then to white to see
    the two cases directly — role-tint chrome (status bar, tool-calls
    section, chips, the prompt) stayed fully legible in both; base-level
    body text was fully legible on the dark simulation and read as
    near-invisible ghost text on the light one, exactly matching the
    contrast numbers. Transparent mode is meant to sit over a dark
    terminal background or desktop, same as the rest of DOXA's chrome —
    the README says this plainly rather than leaving it to be discovered.
  - **Live, not boot-only.** Saving the setting in the modal re-reads it
    immediately (`DoxaApp._apply_background` + `refresh_css`), the same
    "takes effect without a new session" contract the clock chip already
    has — no restart needed either direction.
  - One new gallery asset, `assets/shots/transparent.svg`/`.png` (the
    trace scene, replayed through the live-toggle path, not a fresh app)
    — added because a static SVG export cannot show genuine terminal
    pass-through (Rich still has to bake SOME concrete color for a
    "default" cell), so this shot exists to show the structural claim —
    tool-calls section, tool chips and the status bar still read as
    distinct, painted steps — rather than the pass-through itself. The
    existing gallery stays on `opaque`, unchanged.
  - **Version/sequencing note, and what four releases of drift actually
    cost.** Prepared on `feat/transparent-bg` against v0.22.0 `main`
    (`233ed81`, 627 tests), originally numbered v0.26.0. Renumbered to
    v0.29.0 and rebased onto v0.28.0 `main` (`6a4ffa0`, 710 tests) —
    v0.24.0 and v0.26.0 stay DELIBERATE gaps, not renumbered to close
    them, so the v0.27.0 entry below still reads as what was true when it
    shipped. Four conflicts, none resolved by taking a side: the
    `background` and v0.25.0 `show_reasoning` Setting rows landed on the
    same tuple slot in `doxa/config.py` (both kept), and
    `pyproject.toml` / `uv.lock` / the CHANGELOG heading took the new
    number.
    - **Two defects the merge would have introduced silently, both caught
      by test rather than by eye.** (1) `theme.tcss`'s `.turn-reasoning`
      did not exist when this branch was cut — v0.25.0 added it,
      hardcoding the same `#171512` this branch had just routed through
      `$doxa-base` everywhere else. Auto-merged clean, and wrong: it would
      have painted an opaque slab across the reasoning fold while the turn
      body around it went transparent, a visible seam mid-transcript. It
      reads `$doxa-base` now, like every other base-level surface. (2)
      v0.28.0 fixed an operator-reported defect in this same file —
      `#compact-confirm-buttons` / `#close-confirm-buttons` were
      `height: 1; padding-top: 1`, which under Textual's border-box model
      spent the whole declared row on padding and laid both confirm
      dialogs' buttons out at zero rows, drawing nothing. This branch
      rewrites `theme.tcss` underneath that fix, so `height: auto` is now
      re-asserted from inside the transparency suite, in transparent mode,
      instead of being trusted to the merge.
    - **The mechanism re-verified on this tree, not carried forward on the
      strength of the original write-up.** Against the installed Textual
      5.3.0 / Rich 15.0.0: `ansi_default` parses to `Color(0, 0, 0,
      ansi=-1)`, whose `rich_color` is `Color('default')`, which a real
      truecolor console emits as `ESC[49m`; CSS `background:
      ansi_default 60%` still parses to that same ansi color with the
      percentage discarded (confirming the no-partial-alpha finding);
      `background: transparent` is still alpha-0 RGB that never leaves the
      compositor. `ANSIToTruecolor` is gated by `.enabled` at the CALL
      site (`_styles_cache.py`), not inside `apply`, and with it enabled
      an ansi-default background comes back as an opaque approximation —
      so `App.ansi_color = True` remains load-bearing.
    - **A new end-to-end check at the only layer that can settle it: the
      bytes.** The suite previously stopped at Textual's style objects,
      which cannot see the filter half at all. Two tests now render a
      real base-tinted widget through `Widget.render_lines` (the path that
      reaches `StylesCache.render_widget(filters=app._filters)`) out to a
      truecolor Rich console and assert on the escape sequence: `opaque`
      emits `ESC[48;2;23;21;18m` and never `ESC[49m`; `transparent` emits
      `ESC[49m` and never any RGB paint. Both were mutation-checked —
      removing `App.ansi_color = True`, reverting a base selector to the
      literal hex, restoring `height: 1`, or restoring `.turn-reasoning`'s
      literal each turn the relevant test red.
    - Also re-checked across paths that did not exist when the branch was
      cut: the setting holds on v0.23.0's session-restore launch (a
      different `compose()` branch mounting N panes from
      `RestoreTabSpec`s — `_apply_background` runs off `on_mount`, ahead
      of both branches), and reaches the settings modal and
      `config.py` round-trip the same way every other knob does.
    724 tests green, full suite (710 on `main` at rebase time plus this
    branch's own 14, plus 5 added for the reconciliation above and the
    byte-level proof — 19 in `tests/test_background.py`).

## 0.28.0 — 2026-08-24

Three operator-reported defects in v0.27.0's status-bar chip work, all
found in the same click-a-chip-and-nothing-good-happens region. Each is
written defect-then-fix, with the measurement that identified the cause.

- **The confirm dialogs had no visible buttons, and Enter was dead.**
  THE DEFECT, as reported: "clicking on ctx chip show a modal message, but
  no button to continue, no OK, enter does nothing". Reproduced and
  measured: `#compact-confirm-buttons` laid out at `Size(width=58,
  height=0)` and the "[ compact ]" Static inside it at `Size(width=0,
  height=0)`; the label never reached the screen; Enter did nothing and
  only `y` dismissed the dialog. THE CAUSE, two independent bugs in one
  sentence of the report. (1) `doxa/theme.tcss` styled the button row
  `height: 1; padding-top: 1`. Textual's box model is border-box, so a
  padded box spends its DECLARED height on the padding first — one row of
  padding on a one-row box leaves a content box of exactly zero rows. The
  buttons existed in the DOM and would have answered a click if one could
  have landed on them, but they were drawn nowhere. (2) `CompactConfirm.
  BINDINGS` bound only `escape`, and its `on_key` handled `y`/`c`/`n`;
  Enter — the key anyone presses at a confirm — was bound to nothing.
  THE FIX: the row is `height: auto` (padding PLUS content, so two real
  rows), Enter now takes the action the click that OPENED the dialog
  already asked for (the operator clicked "compact"), Esc still cancels,
  and both labels name their own key — `[ compact · enter ]`,
  `[ cancel · esc ]` — so the dialog says what to press instead of leaving
  it to be guessed. `#close-confirm-buttons` (`CloseWithTurnRunning`,
  Ctrl+Q with a turn running) carried the IDENTICAL css and therefore the
  identical latent defect: three invisible doors, never reported because
  the ctx% twin was hit first. Fixed the same way, with the same
  self-describing labels and the same Enter rule — Ctrl+Q means "close
  this tab", whose non-destructive reading is DETACH (the tab closes, the
  turn survives, `/sessions` re-attaches it), so that is what Enter takes;
  terminate stays a deliberate `t` and is never a default.

- **The beliefs chip errored instead of opening its dropdown.** THE
  DEFECT, as reported: "clicking on 'beliefs' chip leads to error message
  'too much for a message'", and "it was supposed to be shown in an
  autocomplete dropdown". THE CAUSE: `SessionEngine.list_beliefs` returns
  beliefs WITH their claim bodies. In a DETACHED (daemon-backed) session
  that whole list crossed the socket as ONE reply, and
  `doxa.daemon.encode_frame` enforces `peers.MAX_FRAME_BYTES` (64KB) by
  replacing an oversize NON-event reply wholesale with `{"ok": false,
  "error": "reply exceeded the frame cap"}` — `EngineClient` raised that,
  and `open_beliefs_picker`'s except-arm printed it as a system message
  where the dropdown should have been. MEASURED against the reporting
  operator's live store: 500 active beliefs serialize to 235,839 bytes
  (230.3 KB), **3.6x the cap**, at an average claim of 201 chars. That
  measurement also rules out the cheaper fix — trimming every claim to 120
  chars still comes to 115,105 bytes, **1.75x the cap**. Trimming alone
  cannot work here, so paging is not a preference. THE FIX: the `beliefs`
  RPC is PAGED. The daemon serves a conservative 100 rows per frame (~472
  bytes/row measured, so ~139 would have exactly filled 64KB — the smaller
  page leaves real headroom for a store with longer claims), with a byte
  budget as the backstop for claims that run long, plus the offset to
  resume from; `EngineClient.list_beliefs` loops until the store is
  exhausted and hands the app one complete list.
  **Paged at the TRANSPORT, never at the scroll position** — the decision
  that matters. Loading pages lazily as the list scrolls was considered and
  rejected: `ChipPicker`'s type-to-filter matches across the entire row
  set, so with only the first page resident, typing a term matching a
  belief further down would show nothing and the picker would actively
  assert that belief does not exist. A slow open beats a lying filter, and
  230KB over a local unix socket in a handful of frames is imperceptible.
  Every belief is therefore resident before the user can type — pinned by a
  test that seeds a store LARGER than the real one and then filters for a
  belief from the last page.
  Ellipsizing claim text daemon-side was rejected for a second reason
  besides the arithmetic: it would make the two engines return different
  data for the same call, and `doxa.app` reaches both through one
  `getattr(engine, "list_beliefs")` and cannot tell them apart — that
  parity is now pinned by its own test.
  Honesty, since a cap still exists: `engine.BELIEF_LIST_LIMIT` (raised
  from an implicit 500, which would have silently dropped 17 of this
  operator's beliefs, to 2000) is checked against `belief_count()` — the
  SAME `status='active'` COUNT(*) the list selects over — and a list that
  ended because of the cap SAYS so in the picker's own note row rather
  than passing for the whole store. The one claim too large for a single
  frame even alone goes out cut and flagged, and the detail view says
  "claim truncated" rather than showing the remnant as if it were whole.
  The in-process path never hit the cap and is unchanged.

- **Picking a branch appeared to do nothing.** THE DEFECT, as reported:
  "when i chose a branch/dir and click on one, it is not changed". THE
  CAUSE, and it is not where the report points. Measured end to end
  against real git: a real mouse click on the picker row DID reach the
  callback, `worktrees.switch_base` DID run, the worktree WAS rebased and
  the sidecar's `base_ref` rewritten from `main` to `develop`, and the tab
  label DID follow. The status bar did not move one byte — before and
  after, `myrepo ⎇ doxa/abc123de@myrepo-abc123de @5016a09`. `GitLine.
  render` built its branch half from `branch_label()`, the branch actually
  CHECKED OUT here, while the branch picker changes the BASE: inside a
  worktree-per-session session those are different strings, and a base
  switch rebases the session's throwaway `doxa/<id>` branch without ever
  renaming what HEAD points at. So a switch that fully SUCCEEDED was
  invisible in the one place the user was looking. THE FIX: the chip shows
  the base — the same string the tab shows, which is what `render`'s own
  docstring has claimed since item S moved tabs to `tab_branch()` and left
  the status bar behind. This deliberately overrides v0.17's "the status
  bar keeps the session branch, because that IS session identity": the
  segment is a SELECTOR now, and a selector has to display the thing it
  selects. Nothing is lost — the checked-out branch moves into that
  segment's tooltip, and the status bar already carries the session handle
  in its own chip (dropping a third printing of the session id from one
  bar, the same argument item S applied to tab labels).
  Both other halves of that report were investigated by test rather than
  assumed, and both already worked under a real click: selecting a
  directory descends the picker (the `call_after_refresh` reopen survives
  the close/blur/focus hand-off), and selecting a repo root opens it in a
  new tab (`new_session_factory_at` is threaded through every `doxa.cli`
  launch path, and `DoxaApp` defaults it for `--in-process`). Both are now
  pinned by real-click tests — every v0.22.0 selection test called
  `select_row(i)` directly and so never proved a click reaches the
  callback at all. One genuine dead end was found next to them and closed:
  "· open here" was offered only when the current directory WAS a git repo
  root, so descending into an ordinary directory left a listing in which
  every row went up or went deeper and nothing opened anything. It is
  offered for any directory now — `open_tab_at` always accepted one — with
  the ⎇ marker still reserved for actual repo roots.

## 0.27.0 — 2026-08-24

- **Status-bar chip revisions** (operator-reported, three wrong or missing
  actions from v0.22.0's chip work, plus two follow-up asks in the same
  region) — one unit, all five in the shared status-bar/`ChipPicker`
  surface.
  - **ctx% chip now CONFIRMS before compacting — a real defect fix, not a
    preference.** THE DEFECT: through v0.22.0 a single click on the
    context-window chip sent `/compact` immediately. Compaction is lossy
    (the transcript is summarized; the PreCompact review that runs first
    does not change that) and there is no undo, so one misclick silently
    discarded conversation detail with no warning at all — the same class
    of harm CloseWithTurnRunning (v0.19.0-era Ctrl+W-with-a-turn-running
    confirm) exists to prevent for a running turn. THE FIX:
    `doxa.app.CompactConfirm`, a new `ModalScreen[bool]`, pushed via
    `push_screen_wait` from a worker (`SessionPane._confirm_and_compact`)
    before the turn is ever sent. Esc / "cancel" / a click elsewhere on
    the two buttons declines — no compaction, no turn sent, status bar
    unchanged; only an explicit "compact" sends `/compact`, unchanged from
    before. **House precedent chosen, and why**: `CloseWithTurnRunning`,
    not `NeedsInputPopup` — the latter is PROMPT-driven (`can_focus =
    False`, answered through `PromptInput`'s own key protocol) because it
    exists to answer an `ask_user`/permission request the ENGINE is
    genuinely waiting on; a compact confirm has nothing on the other end
    waiting, so it is a plain UI yes/no, the exact shape
    `CloseWithTurnRunning` already established (a focused `ModalScreen`,
    two doors instead of three). The body states what is actually at
    stake — the CURRENT ctx% and that compacting discards earlier detail
    — not a bare "are you sure?".
  - **Session-handle chip opens a SESSIONS dropdown instead of copying.**
    v0.22.0 made it ACTIONABLE by copying the handle to the clipboard on a
    bare click; the operator wants the full picture — every session in
    SCOPE, including detached ones, clearly marked. `SessionPane.
    open_sessions_picker` reads `doxa.peers.list_daemons(scope_key=...)`
    (the same `main_repo_root_of(cwd) or cwd` scope key `PeerHost` itself
    computes) and renders one `ChipPicker` row per live daemon-hosted
    session, `⌁ detached` appended when `PeerInfo.clients == 0` — the SAME
    field the peers chip's own `(N⌁)` suffix already reduces to, reused,
    not re-derived. The current session is marked with `ChipPicker`'s own
    `▸` current-id marker (no new marking mechanism). Selecting: the
    current row is a no-op (per spec); a detached daemon is attached to
    via `DoxaApp._cmd_attach` — the SAME path `doxa attach` and the
    palette's own "Attach: …" entries already use, no second attach
    implementation. **Judgment call, flagged**: a session already open in
    ANOTHER tab of this window switches to that tab
    (`DoxaApp._switch_to_tab`) instead of attaching a second client to it
    from here — the palette's own Attach section makes the identical
    exclusion for the identical reason (a session with a tab already open
    gets a tab-switch entry, not a second "Attach:" row). **Clipboard
    capability kept, not dropped**: the picker's first row is
    `⧉ copy this session's handle`, calling the SAME `copy_session_handle`
    method the old bare click used — a real row rather than a
    modifier-click, which would be less discoverable and does not fit the
    picker's existing mouse+keyboard model.
  - **Beliefs chip is clickable — filtered, scope-grouped.** v0.22.0 left
    it plain ("no `/beliefs`-ish surface exists to route to", its own
    release notes said). **Scope vocabulary, verified against the
    installed `lore_core` (0.32.0) rather than assumed**: the `beliefs`
    table has no `scope` column — `lore_core.beliefs.belief_subject`
    writes one of `"user"`, `"user-model"`, or `"project:<slug>"` into
    `subject`. `doxa.app._belief_scope_label` derives the picker's GROUP
    from that string (`"user-model"` → `"user model"`, its own group,
    never folded into plain "user" — the same distinction
    `belief_subject`'s own docstring draws; `"project:<slug>"` →
    `"project"`; anything else falls through to its own prefix) — data-
    driven, not a hardcoded two-way branch, so LORE issue #41's proposed
    (open, UNIMPLEMENTED) `machine` scope would slot into its own group
    the day `lore_core` starts writing a `"machine:<id>"` subject, with NO
    change to this function. No `machine` group is fabricated here — there
    is nothing behind one yet. **`ChipPicker` grew group-header support**
    (`open(..., groups=...)`) rather than a second widget: an optional
    `rid -> group label` map renders a dim `▎ <group>` disabled separator
    row whenever the group changes, walking rows in caller-given order —
    the SAME disabled-separator-row convention `doxa.palette.
    DoxaPalette._refresh_command_list` already established for the
    command palette's own section headers, reused rather than invented
    twice. A typed filter collapses the groups, same as the palette's own
    filtered search drops its headers. **Selection**: shows the belief's
    full claim + confidence inline (a `SystemBlock`) — the least-
    surprising small thing a claim-summary row can do. **Judgment call,
    flagged, and scope-bounded on purpose**: this is a LIGHTWEIGHT viewer,
    NOT lettered item V (the full beliefs browser — evidence trails,
    approve/reject flows), which stays unbuilt; item V still owns that
    surface. **Cost discipline, asserted by a test**: `belief_count()`
    (the chip's own number, called on EVERY status refresh) is unchanged
    and stays free; belief BODIES are a new `list_beliefs()` method
    (`SessionEngine` — direct sqlite read; `EngineClient` — a new
    `"beliefs"` daemon RPC method; both async, mirroring `switch_branch`'s
    own "list, then let the picker render it" shape) called ONLY from
    `open_beliefs_picker`, never from `_refresh_status`
    (`tests/test_status_chips.py::test_beliefs_chip_never_loads_bodies_on_status_refresh`
    pins it).
  - **Repo-name chip becomes a SELECTOR — overrides v0.22.0's "repo name
    is INERT" call.** A directory-walking `ChipPicker`
    (`SessionPane.open_repo_picker`/`_repo_picker_rows`), starting at the
    session's own cwd: typing filters the CURRENT listing (`ChipPicker`'s
    existing type-to-filter, unchanged); selecting a plain directory
    DESCENDS into it; selecting a directory marked as a git repo (`⎇`,
    via `peers.main_repo_root_of(path) == path` — reused, not
    re-derived, the SAME function `PeerHost`'s own scope key and the
    spawn-or-attach reuse path already call) opens it in a NEW TAB. An
    unreadable/nonexistent directory (a stale race between listing and
    clicking) reports a plain message, never a crash, never a
    half-created tab. **Descend mechanics, a real ordering bug found and
    fixed along the way**: `ChipPicker.select_row` already closes the
    picker and hands focus to the prompt BEFORE calling the row's
    callback, and Textual's own Blur delivery for the picker's just-lost
    focus is QUEUED, not immediate — reopening the same instance
    synchronously from inside the callback raced that queued Blur, which
    then landed after the reopen and closed it right back (observed,
    reproduced in a test, not theoretical). Fixed by deferring the
    reopen one refresh cycle (`app.call_after_refresh`), which lets the
    close/blur/focus cycle fully settle first. **Semantics, a judgment
    call, flagged**: a running session's cwd is fixed once connected, so
    picking a different repo must not mutate the session underneath it —
    the least-surprising reading is that it opens the SAME spawn-or-
    attach path `ctrl+t` / `doxa` in that directory would take, in a NEW
    tab, never the current one. **No second spawn implementation**:
    `DoxaApp.open_tab_at` is a thin wrapper over a NEW, path-parametrized
    `_new_session_factory_at` — the exact same closure SHAPE
    `doxa.cli._run_attached`'s existing `new_session_factory` (spawn at a
    FIXED cwd) already used, just taking the path as an argument instead
    of closing over one; `doxa.daemon.spawn_daemon`/`SessionEngine`
    remain the one underlying spawn primitive either way. Defaults to an
    in-process `SessionEngine` at the chosen path when no daemon-flavored
    factory was supplied (`--in-process` mode, and every existing test's
    bare `DoxaApp(...)` construction), so the repo picker works rather
    than silently dead-ending there. **Rebase-time gap closed**: tab
    restore's `_run_restored` (item D, v0.23.0) builds `DoxaApp` at TWO
    call sites, neither of which passed `new_session_factory_at` — the
    common case now that `restore_tabs` defaults on, meaning the picker
    would have silently fallen back to an IN-PROCESS engine (not a real,
    detachable daemon) on a restored launch specifically, the one case
    most likely to have several tabs open already. `doxa/cli.py` now
    threads a `new_session_factory_at` closure through both.
  - **Tooltips on every chip, INERT ones included.** **Design choice,
    with evidence, and a deliberate departure from the suggested
    default**: a per-chip-widget refactor (splitting the one `Static`
    status bar into N widgets, each carrying Textual's native
    `tooltip` property) was considered and rejected in favor of keeping
    the SINGLE `Static` and adding a dynamic resolver instead.
    Evidence: `Widget.tooltip` is a plain attribute Textual's OWN hover
    timer (`Screen._handle_tooltip_timer`) re-reads on every mouse move,
    and the setter re-triggers `Screen._update_tooltip` immediately when
    the widget is already the one being shown — so ONE widget can serve a
    DIFFERENT tooltip for different chips under the cursor, the identical
    trick the bar already uses to serve a different CLICK action per
    chip (`[@click=...]` markup spans). `StatusBar._on_mouse_move` maps
    the region-relative `x` (padding-adjusted, the SAME `- 2` convention
    `tests/test_status_chips.py`'s own `_offset_of` established for click
    coordinates) against `(plain_text, tooltip)` pairs
    `SessionPane._refresh_status` now builds alongside the markup string
    itself, in the SAME order, so the two can never drift apart. This
    changes NOTHING about how the bar's markup is built — chip order,
    spacing, hide-at-zero rules and existing colors are untouched by
    construction, not by discipline, and every pre-existing click/order
    test in `tests/test_status_chips.py` and `tests/test_statusline.py`
    needed no changes at all. Content: one sentence per chip in house
    voice, stating what the number MEANS (the cost chip's
    subscription-vs-API distinction, ctx% as percentage of the context
    window, the peers chip's `(N⌁)` detached-count basis, effort's
    connect-time-only scope) rather than restating the chip's own text —
    including the INERT chips (cost, sha, headroom), which get exactly
    the same treatment; a chip that cannot be clicked can still be
    explained.
  - **Overlap with the tab-restore item (`feat/tab-restore`, landed as
    v0.23.0 below, ahead of this branch)**: both branches touch
    `doxa/app.py`'s session/tab/daemon-attach region and `doxa/peers.py`,
    and this branch was rebased onto v0.23.0's own `app.py` changes
    (`doxa.tabsets`, `RestoreTabSpec`, restore-aware `compose()`/
    `on_mount`, the `_detached_this_run` side-map) — reconciled by hand
    at rebase time, not resolved by taking either side wholesale (see the
    rebase note in this session's own report for exactly what that
    touched). This branch adds `DoxaApp._new_session_factory_at`/
    `_make_pane_at`/`open_tab_at` (new methods, additive) and calls the
    EXISTING `_cmd_attach`/`_switch_to_tab` for the sessions picker's own
    attach/switch rows rather than adding a second implementation of
    either.
  - Tests: `tests/test_status_chips.py` — ctx-click opens the confirm and
    sends nothing until accepted (decline via button AND Esc send
    nothing), the confirm body states the live percentage; the sessions
    picker lists live + detached with the correct markers, marks the
    current session via `ChipPicker`'s own `▸`, the copy row still
    copies, a detached row's selection asserts the `_cmd_attach` CALL
    (not just the UI); the beliefs picker groups by the REAL verified
    scopes, filters on claim text, and asserts `list_beliefs()` is never
    called by a status refresh; the repo picker lists directories from
    cwd, marks real repos (reusing `main_repo_root_of`, not re-deriving
    it), filters on type, descends without closing, reports an invalid
    path cleanly, and asserts the spawn call through `open_tab_at`
    without spawning a real subprocess; every chip's tooltip is asserted
    non-empty and chip order/click behavior is re-asserted unchanged.
    Also updated: the v0.22.0 repo-name/sha test (now the repo half opens
    the picker; the sha half is still pinned inert).
  - Assets: `chip-picker.gif` NOT regenerated — its own scene (a click on
    the BRANCH chip) shows behavior this release did not touch; none of
    the five changes above are visible in that specific recording.
  - **Version/sequencing note**: prepared against pre-v0.23.0 `main`
    (`233ed81`, 627 tests) on `feat/chip-actions`, originally targeting
    v0.24.0. Renumbered to v0.27.0 mid-flight (spawning-session
    instruction) and rebased twice as `main` moved ahead of it: first
    tab restore landed as v0.23.0, then reasoning stream (below) as
    v0.25.0 — v0.24.0 and v0.26.0 are DELIBERATE gaps (0.26.0 belongs to
    a still-unmerged transparency branch), not renumbered to close them.
    Also reconciled against tab restore's `_persist_tabset`/`_close_pane`
    (item D, v0.23.0): a repo-picker-opened tab can now be the FIRST tab
    in this app's history rooted in a DIFFERENT repo than the window's
    own scope, an invariant `doxa.tabsets`' persistence assumed always
    held — both methods now exclude a cross-scope pane/detach record
    rather than writing an entry `doxa.tabsets.resolve` could only ever
    silently skip (its own scope cross-check already made this safe, not
    wrong, just dead weight; excluded outright instead of relying on
    that fallback). 670 tests on `main` at rebase time (`be46893`,
    v0.25.0) plus this branch's own 21 = 691 green, full suite, post-
    rebase.

## 0.25.0 — 2026-08-24

- **Reasoning stream: the model's own summarized thinking, in a collapsed
  per-turn fold** — mirrors the v0.13.0 "Tool calls (N)" fold exactly:
  collapsed by default, created lazily on first content, hide-at-zero for a
  turn with none, live-count title rewrite, never auto-collapses once the
  operator opens it.
  - **PRE-WORK FINDING, before any of this was built**: does the installed
    `claude_agent_sdk` (0.2.144) even expose reasoning text at all? YES, but
    only opted in. `ClaudeAgentOptions.thinking` (a `ThinkingConfig` —
    adaptive/enabled/disabled, `types.py:2281`) exists; its optional
    `display` field (`types.py:1782-1784`) defaults to `"omitted"` on every
    current model — an EMPTY `thinking` string — unless a caller explicitly
    requests `"summarized"`. Confirmed against Anthropic's own streaming
    docs (not guessed): a `thinking_delta` content-block delta looks like
    `{"delta": {"type": "thinking_delta", "thinking": "..."}}`, distinct
    from `text_delta`'s `{"delta": {"type": "text_delta", "text": "..."}}`.
    And: **doxa.engine already had a code path receiving these events and
    dropping them on the floor** — `send()`'s `StreamEvent` branch
    (`content_block_delta`) only ever read `delta.get("text")`, which a
    thinking delta never sets. Fixed by branching on `delta["type"]` before
    deciding which field to read.
  - **`show_reasoning`** (Appearance, `DOXA_SHOW_REASONING`, `bool_on`,
    default ON) — read once at connect (`doxa.engine.show_reasoning()`,
    same connect-time-only shape as `effort`: the SDK has no live setter for
    `thinking` either). ON asserts `thinking={"type": "adaptive", "display":
    "summarized"}` — the documented way to get visible reasoning across the
    current model line (Opus/Sonnet 5, Fable 5, Mythos 5/Preview, and
    Opus/Sonnet 4.6+ all support adaptive thinking). **OFF does NOT assert
    `{"type": "disabled"}`** — Claude Fable 5, Claude Mythos 5 and Claude
    Mythos Preview reject an explicit disable outright (thinking cannot be
    turned off on those models at all), and `self.model` is usually still
    `None` at options-build time (the real model is only known from the
    CLI's own init message, after connect), so there's no way to
    special-case around it. OFF means "DOXA stops asking to see it," not
    "thinking is guaranteed free" — see the README's new Reasoning section
    for the honest version of that claim, and `config.py`'s `show_reasoning`
    `Setting.note` for the same caveat in the settings modal itself.
  - **`doxa.app.ReasoningSection`** — a `Collapsible` titled `"✻ Reasoning
    (N chars)"`, mounted in a `TurnBlock.reasoning_holder` ABOVE `.body`
    (reasoning precedes the answer), created lazily on the first
    `reasoning_delta`. Streams via the SAME `Markdown.get_stream` append-
    only path the response body already uses (v0.13.0) — reasoning is
    prose that can carry light formatting, and a second streamed-text idiom
    next to an already-tested one earns nothing. Unlike `ToolChip`'s lazy
    args/result formatting (deferred to first expand), this section writes
    LIVE even while collapsed, because the spec is explicit that collapsed
    must not mean paused: the header's count and an expand at any point
    both need current content. `mark_done()` stops this section's stream
    exactly like it already stops `.body`'s — no background write task
    survives a finished turn (asserted in tests, the same way v0.13.0's own
    body-stream teardown is).
  - **`ThinkingMarker` decision: subsumed, not replaced.** `hide_thinking()`
    now fires on the first `reasoning_delta`, exactly like it already fires
    on the first `text_delta`/`tool_call` — a live "Reasoning (N chars)"
    header IS the "something is happening" signal at that point, so a
    static `⋯ thinking` above it would be redundant. The marker itself
    stays: it's still the only sign of life before ANYTHING arrives (a
    turn with `show_reasoning` off, or one the model answers without
    thinking first, has no reasoning to hide it early).
  - **Engine event**: new `reasoning_delta` (`{"text": ..., "parent_id":
    ...}` — same optional-`parent_id` shape as `text_delta`), routed the
    same way through `SessionPane._handle_event` and the out-of-band
    (multi-attached-client) dispatch tuple. A subagent's OWN reasoning
    (carries `parent_id`) has no separate fold on its `ToolChip` — out of
    scope here — so it joins the same trace buffer its spoken text already
    uses (`append_subagent_text`) rather than being dropped.
    `tests/fakes.py`'s `FakeEngine` needed no code change: it replays
    scripted `EngineEvent`s verbatim, so a script including
    `reasoning_delta` already exercises the real dispatch path.
  - **13 new tests** (`tests/test_reasoning.py`): options wiring (on/off),
    `thinking_delta` → `reasoning_delta` translation (incl. an empty-string
    delta yielding no event, and subagent scrub/parent-id parity with
    `text_delta`'s own test in `test_trace.py`), hide-at-zero, collapsed by
    default + mounted above the body, live title updates while collapsed,
    stays-expanded, `ThinkingMarker` subsumption, `mark_done` stream
    teardown (with and without any reasoning), and one full FakeEngine
    end-to-end turn through the real `DoxaApp` dispatch path.
  - **`assets/shots/reasoning.gif`** — new `scripts/record_gif.py` scene
    (`reasoning`, `SIZE_WIDE`, deterministic FakeEngine-driven Pilot
    capture, no live model needed): `⋯ thinking` → the collapsed
    "Reasoning (N chars)" header ticking up → expanded mid-turn and staying
    that way as more streams in → the response landing below it once
    thinking finishes. 6 frames, 2117×1197 (1.769, within 2% of 16:9), 411
    KiB.
  - **What did NOT ship**: reasoning is never persisted to the LORE
    transcript (`SessionEngine._persist_assistant_blocks`) — display-only,
    same as it was silently before this feature (a `ThinkingBlock` in the
    final `AssistantMessage.content` was, and still is, skipped by that
    loop).

## 0.23.0 — 2026-08-24

- **Tab restore (queue item D)** — **this item's original spec text did
  not survive to the session that built it.** What follows is RE-DERIVED
  from the item's name plus the surviving codebase — most tellingly,
  `doxa/naming.py` already forward-referenced it verbatim, from a much
  earlier version: "a restart (item D's window restore) reuses the [tab]
  name rather than spending a second call on the same session." Every
  judgment call this re-derivation required is flagged below.
  - **THE DEFECT**: sessions are daemons that outlive the TUI (the whole
    point of the daemon split) — closing the window and reopening it does
    NOT bring the tab set back. Plain `doxa` only ever reattaches to the
    SINGLE most recent live session in a repo's scope
    (`doxa.cli`'s spawn-or-attach), so a multi-tab working set was lost on
    every restart even though every daemon behind it was still alive and
    attachable.
  - **New `doxa/tabsets.py`**: one small JSON record per repo scope under
    `$DOXA_HOME/tabsets/<sha256-of-scope-key>.json` (the scope key is the
    same `peers.main_repo_root_of` key spawn-or-attach and peer discovery
    already group daemons by, hashed because a filesystem path is not a
    safe filename) — ordered session ids, each with its pinned name (if
    any), and which one was active. Atomic writes (tmp file + rename),
    0600, same discipline as `doxa.config`'s settings file; a missing,
    unreadable, or malformed record degrades to "nothing to restore,"
    never a crash — `doxa.config.load`'s own rule, applied here.
  - **Restore is a cross-check, not a replay**: `tabsets.resolve()` reads
    the saved record and filters it against the LIVE daemon registry
    (`peers.list_daemons`) for that scope. A saved session id the registry
    no longer knows about (finalized, killed, machine rebooted) is dropped
    SILENTLY and counted — it must never spawn a replacement (that would
    not be the session the user left) and must never block startup.
    `doxa.cli` reports the outcome in a `SystemBlock` on the first
    restored tab ("tab restore: restored 3 tabs, skipped 1 session no
    longer running.") rather than differing silently from what the user
    left; when EVERY saved session turns out to be dead, `doxa` still
    spawns exactly one fresh tab (never zero) and the report still lands
    on it.
  - **Stopped vs. detached, made consistent with the record**: v0.17's
    `detached_on_purpose` / stop-path split already distinguished these; a
    session the user explicitly STOPPED (`doxa stop`, Ctrl+Q, "Quit: stop
    session") leaves the persisted set for good (`SessionPane.stop`
    now marks the pane `_stopped` before anything else, and
    `_persist_tabset` excludes it), while a session merely DETACHED
    (Ctrl+W, Ctrl+C once, "Quit: detach") STAYS in the set even though its
    tab leaves the strip — tracked in a small `DoxaApp._detached_this_run`
    map so a closed-but-still-running tab does not silently drop out of
    the file it is no longer mounted in.
  - **New setting `restore_tabs`** (`DOXA_RESTORE_TABS`, category Session,
    `bool_on`, default **ON** — the house pattern `worktree_per_session`
    already established). **Judgment call**: the toggle gates only the
    READ side — whether a launch RESTORES from the saved set. Persisting
    the record happens unconditionally (same posture as `naming.py`'s
    name cache), so turning the setting back on has real history to
    restore from immediately, and turning it off never needs a separate
    "forget my tabs" action.
  - **Judgment call, flagged as asked**: whether a plain `doxa` with a
    saved set restores the WHOLE set or still does today's
    single-most-recent spawn-or-attach. This ships restore-the-set — that
    IS the feature — with `doxa attach <prefix>` staying the single-session
    path either way, and `doxa new` always forcing exactly one fresh tab
    and never consulting the saved set at all (though its own tab
    still gets persisted like any other, for the NEXT restore).
  - **Judgment call**: "tab reordering" is named in the item's title but
    there is no drag-or-move UI for tabs anywhere in DOXA today (Textual's
    `Tabs` widget exposes no such affordance here) — the persisted order
    is simply read from the live tab-bar order at each save point (open,
    rename, close, exit), which already tracks any future reorder feature
    for free without a dedicated hook.
  - **Judgment call**: a multi-tab restore's saved order is guaranteed
    even though the resolved daemons attach concurrently and in whatever
    order they happen to answer — `DoxaApp._restore_pending` counts every
    restored pane's boot down to zero before the FIRST persisted write,
    so a mid-connect crash can never truncate the file to whichever tab
    happened to answer first.
  - **doxa.app wiring**: `SessionPane` gained `_session_id` (cached
    outside `self.engine` so it survives `detach()`/`stop()` clearing that
    handle — `_persist_tabset` reads this, never `engine.session_id`),
    `_stopped`, and restore-only `_initial_pinned_name`/`_boot_report`.
    `DoxaApp` gained `RestoreTabSpec`, `restore_tabs`/`restore_active_id`/
    `restore_report` constructor params, and `_persist_tabset`/
    `_note_pane_booted`, called from every tab-lifecycle site: boot
    completion, `/rename`, `_close_pane`, `_stop_active`, `action_quit`,
    `action_quit_stop`.
  - **Tests**: `tests/test_tabsets.py` (22 — save/load round-trip, atomic
    write + 0600, corrupt/missing/empty record all degrade to
    "nothing to restore," `resolve()`'s dead-session filtering and
    saved-order preservation, scope isolation, and the full `DoxaApp`
    wiring: persist on open/rename/detach/stop/quit, detached-stays-
    stopped-leaves, and a multi-tab restore's order/names/active tab/
    report) plus `tests/test_cli_restore.py` (8 — `doxa`'s restore-vs-
    single-attach decision, the toggle, `doxa new`/`doxa attach` bypassing
    restore, and `_run_restored`'s own plumbing). 657 tests green (627
    baseline + 30).
  - **README**: the daemon/quickstart section gains a paragraph on
    restore, and the configuration table gains the `restore_tabs` row.
    **Assets**: no shot or GIF regenerated — nothing in the tab strip's
    visual chrome (labels, colors, the rename editor) changed; the only
    new visible surface is a one-line `SystemBlock` report on restore,
    which is plain scrolled text, not a scene the existing gallery frames.

## 0.22.0 — 2026-08-24

- **Clickable status-bar chips: a shared dropdown picker, model and
  branch (queue item Y)** — THE DEFECT (operator-reported): "the branch
  selector is just a command, if i click the branch in the status line,
  no dropdown autocomplete menu opens as specced." v0.20.0 shipped
  `/branch` (list + switch) and a palette entry; the status bar itself
  stayed inert text the whole bar LOOKED clickable next to, which is the
  same defect class as a button that doesn't respond to a click. Same
  request for a model picker (item Y), delivered here alongside it since
  the two share one implementation.
  - **Three tiers of chip, not one "everything is a button" pass** — the
    operator's own follow-up ("for every chip?") answered explicitly, and
    enforced in code, not just in this note: **SELECTORS** (model,
    branch, effort) open the new shared picker; **ACTIONABLE** chips run
    something that already exists with no popup (`peers N` → `/sessions`,
    the context-window chip → `/compact`, the session handle → clipboard);
    everything else (cost, repo name, sha, subscription headroom) stays
    plain. Giving every chip the same affordance would make the
    affordance stop meaning anything — the same defect the report named,
    just moved one level down. Only the git chip's BRANCH segment is
    clickable, never the repo name or the sha beside it.
  - **Affordance color** — the click spans (`[@click=...]` markup,
    `doxa.app.GitLine.render`/`SubagentLine`'s own precedent from v0.18)
    wear `#D97757`, Claude-orange — NOT a new color: the same accent
    theme.tcss already uses for the active tab, palette matches and
    `#slash-complete`'s highlighted row. `doxa.app.CLICKABLE_CHIP_ACCENT`
    names it under its own alias so the intent ("this opens something")
    reads at the call site without re-deriving it from
    `PROVIDER_GLYPH_COLOR`.
  - **`doxa.app.ChipPicker`** — ONE reusable `OptionList` popup for all
    three selector chips, mounted in the same "above the prompt" slot the
    three prompt-driven popups (`SlashComplete`/`SessionSearch`/
    `NeedsInputPopup`) already use. Judgment call, and a departure from
    those three: this one takes REAL focus the moment it opens (nothing
    else needs the caret while a chip menu is up), which is what lets
    `OptionList`'s OWN bindings (arrows, home/end, enter, and its built-in
    mouse-click-to-select — confirmed: `action_cursor_up`/`_down` already
    skip disabled rows via `find_next_enabled`) work completely
    unmodified; the class adds only Esc-to-close and type-to-filter (the
    "autocomplete" the report asked for), the latter via the exact
    pattern `textual.widgets.Input._on_key` uses for printable characters.
    Closes on Esc, on a row selected (mouse or Enter — both post the same
    `OptionList.OptionSelected`), or on a click anywhere else in the pane
    (`SessionPane`'s new pane-level `Click` handler; a click ON one of
    the status bar's own `[@click=...]` spans never reaches it —
    `Widget.broker_event` stops the event first) or a focus change away
    from it (`ChipPicker._on_blur`, for a tab switch or another focusable
    widget — deliberately does NOT refocus the prompt in that case,
    unlike Esc/selection, which do: focus already went somewhere real).
  - **No second switch implementation** — every picker's `on_select`
    callback calls the SAME coroutine the matching slash command already
    uses (`_cmd_model`/`_cmd_branch`/`_cmd_effort`), so the refusal
    messages, the settings-file write, the `base_changed`/`model_changed`
    broadcast — none of it is reimplemented for the click path. The
    branch picker's candidate list comes from the SAME no-argument
    listing call `/branch` makes (`engine.switch_branch(None)`), not a
    second query.
  - **Model list source (the provider seam)** — new `doxa/providers.py`:
    a `ModelProvider` Protocol (`list_models`, `provider_display_name`,
    `default_model`) and one `ClaudeProvider` implementation, so a second
    provider (DeepSeek, Codex — the planned multi-provider engines) is a
    new Protocol implementation later, never a change to the picker's own
    code. Resolution order, most authoritative first: (1) the Anthropic
    Models API (`client.models.list()`) — **VERIFIED EMPIRICALLY
    UNREACHABLE** under DOXA's normal auth posture: DOXA authenticates
    through the `claude` CLI's own OAuth session and deliberately never
    reads that token out of the CLI's keychain (`doxa/auth.py`: "DOXA
    never handles a credential" — the same posture that rejected
    `--bare`'s forced `ANTHROPIC_API_KEY` auth back in v0.10.0's
    `cli_isolation.py`). A live probe (`anthropic.Anthropic()`, no key
    configured, this repo's own venv) fails at CLIENT CONSTRUCTION,
    before any network call: `TypeError: Could not resolve authentication
    method. Expected one of api_key, auth_token, or credentials to be
    set...`. This tier is written defensively (guarded import, guarded
    `ANTHROPIC_API_KEY` presence check, guarded call) and used
    opportunistically — DOXA's own process env is untouched by
    `cli_isolation.py` (which isolates only the SPAWNED engine
    subprocess), so an operator whose shell happens to export the key
    gets it live; the documented posture skips the attempt entirely
    rather than guaranteeing a failed round trip on every picker open.
    (2) whatever the installed `claude-agent-sdk` advertises — CHECKED:
    no model catalog anywhere in the package, `set_model` accepts an
    arbitrary string; a structural no-op today, kept as its own method so
    a future SDK release only needs one method body filled in. (3) a
    small STATIC fallback, clearly marked (`ModelInfo.source ==
    "fallback"`) both in code and in the picker itself (a note-row
    caveat) — the same four aliases `doxa.app.MODEL_ALIASES` already
    used pre-chips (`haiku`/`sonnet`/`opus`/`fable`, from the installed
    `claude` CLI's own `--model` help text). `MODEL_ALIASES` now POINTS
    AT `providers.FALLBACK_MODEL_ALIASES` instead of keeping its own
    copy — one list, not two that happen to agree today. `anthropic` is
    deliberately NOT a `pyproject.toml` dependency (judgment call: lazy,
    guarded import only) — pulling real weight into every install for a
    tier that is structurally unreachable for DOXA's primary
    (subscription/OAuth) audience would be the wrong trade; `pip install
    anthropic` into this venv is how an operator who genuinely wants it
    live gets it.
  - **Model-switch timing, verified (not assumed)**: `/model` is a REAL,
    LIVE control request (`ClaudeSDKClient.set_model` — no reconnect,
    confirmed against the existing v0.12.0-era test coverage and
    `SessionEngine.set_model`'s own docstring) — unlike `/effort`, which
    the SDK sets at connect time only. The model picker therefore applies
    immediately, same as `/model` always has; the EFFORT picker (included
    here too, judgment call — it is a SELECTOR by the same three-tier
    rule and shares the picker at near-zero marginal cost, hide-at-zero
    same as its status-bar chip already was) carries an upfront note-row
    caveat instead of silently no-opping: "applies to NEW sessions only
    (connect-time) — this one keeps `<level>`."
  - **Session-handle chip** — the `⌁ session <id>` chip is now
    ACTIONABLE (copies the full id via `App.copy_to_clipboard`, an OSC 52
    write); its dim `#8A8073` treatment is replaced with the same click
    accent every other actionable/selector chip wears, since a clickable
    chip needs to look like the others, not stay deliberately quiet.
  - Tests: `tests/test_status_chips.py` (new) — click-opens-picker for
    model and branch, click on repo name/sha opens nothing, keyboard nav
    + type-to-filter narrowing, Enter/click-select invoking the SAME
    `_cmd_model`/`_cmd_branch` coroutine (assert the call, not just the
    UI), Esc closing, a dirty-worktree branch-switch refusal surfacing
    through the picker, the effort picker's honesty note, the three
    ACTIONABLE chips (peers/ctx/handle), and `doxa.providers.
    ClaudeProvider`'s fallback + caching + no-key-no-probe behavior.
    Two pre-existing tests updated for the new click-markup wire format
    (`tests/test_statusline.py::test_status_line_chip_order`,
    `tests/test_app.py::test_status_line_shows_repo_and_branch` — both
    asserted the OLD plain-text chip format directly; `.renderable` on a
    `Static` is the RAW string handed to `update()`, so either the exact
    new markup or `Content.from_markup(...).plain` is needed now). 627
    tests green (612 baseline, post-v0.21.0 + 15 new).
  - Assets: `assets/shots/chip-picker.gif` (new, via
    `scripts/record_gif.py chip-picker` — a REAL click on the branch
    chip, same "exercise the actual trigger" choice the v0.16.0 `rename`
    scene already made with its double-click; 1482×831, 1.783 vs. the
    16:9 target's 1.778, 4 frames, 167 KiB) plus every OTHER gallery
    GIF regenerated (`tab-lifecycle`/`tool-calls`/`markdown-stream`/
    `rename`/`palette`/`search`/`attention-blink`/`needs-input`): the
    model chip is the FIRST chip in every scene's status bar, so its
    color/underline changed in all of them — a real visible-content
    change, not a no-op regeneration. `search.gif` in particular carries
    BOTH this item's chip color AND v0.21.0's result-tree content in the
    one regeneration, rather than fighting over which change "owns" the
    file.
  - **Version note**: this branch (`feat/status-chips`) was prepared
    against pre-v0.21.0 `main` and rebased onto `origin/main` once
    `v0.21.0` (the concurrently-developed search-tree item) landed there
    — this CHANGELOG entry was reordered above 0.21.0's during that
    rebase, and the test count above already reflects the post-rebase
    612-test baseline, not the pre-rebase 604 the branch started from.

## 0.21.0 — 2026-08-24

- **Search result tree + excerpt insertion** (queue items I/J — always
  queued as a pair, shipped as one) — this item's spec text was lost
  before it reached this session; what ships here is RE-DERIVED from two
  surviving fragments (the queue's own "search tree (I/J)" label, and
  `doxa/paste.py`'s 0.9.0 docstring already naming item J as a second
  caller of its placeholder/expansion machinery — it was built for this
  before this existed) plus the current `/search` popup, `paste.py`, and
  the trace tree's own `Collapsible` fold, following existing conventions
  wherever a call had to be made (each flagged below).
  - **I — tree, not a flat list.** `SessionSearch` (`doxa/history.py`)
    restructures a result set into a two-level tree the moment it spans
    MORE than one session: a session header (title, date, hit count),
    collapsed by default, over its matching snippets. A single-session
    result set has nothing to fold against and stays exactly the flat
    list this popup has always shown — no pointless fold. Judgment call:
    this applies uniformly to BOTH a real search and the empty-query
    "recent sessions" listing (same code path, one behavior) rather than
    special-casing recents to stay flat even when it spans several
    sessions — a header's hit-count (matches in this session) and a
    recents child row's "N messages" (the session's total size) are
    different numbers, so the fold is not pure redundancy even there, and
    two visually different behaviors for the same widget depending on
    invisible internal state (was a term typed) would be the more
    surprising choice.
  - **Keyboard.** ↑/↓ move through VISIBLE rows only (a collapsed
    session's hidden snippets are not skipped over — they are not rows).
    Judgment call, flagged: the surviving spec fragment asks for
    "left/right collapse/expand… match the trace tree's existing key
    handling" — but the trace tree's ONLY convention (Textual's
    `Collapsible`, confirmed in the current codebase) is Enter-toggles;
    there is no left/right anywhere in this app to match. Resolved by
    doing both readings honestly rather than picking one silently: Enter
    on a header toggles it (the trace tree's actual, sole convention,
    reused rather than inventing a second one), while → explicitly opens
    a collapsed header and ← closes one — or, from a snippet row,
    collapses its parent and lands the highlight back on the header (the
    usual tree "go up a level" move). Enter on a snippet activates it
    (item J, below); a header is never itself an excerpt, so Enter can
    only mean "fold" there.
  - **J — excerpt insertion.** Enter on a snippet inserts its excerpt in
    place of the `/search …` line that found it: one provenance line
    (`[lore session <id> · <ts>]`) naming which session and when, then the
    de-marked snippet — an excerpt with no origin is a quote with no
    citation. Staged through the SAME collapse decision a real clipboard
    paste uses (`PromptInput._stage_pasteable`, factored out of the
    existing `_on_paste` so item N and item J share the one seam
    `paste.py`'s own docstring already expected): past the threshold it
    collapses to the `⧉ pasted N lines (X KB)` placeholder, Ctrl+G
    expands it in place, and the full text goes out on submit either way
    — no new machinery, the item N path exactly. Supersedes the old
    one-line quoted `hit_reference()` (removed; nothing else called it).
    Judgment call: the excerpt is two lines (provenance, then body)
    rather than one long quoted run-on, so paste.py's LINE-count trigger
    sees a large excerpt the same way it sees a large paste, instead of
    a single line that could dodge the threshold by construction.
  - Tests: tree grouping vs. flat single-session, ↑/↓ over visible rows
    only, →/← expand/collapse (including the snippet-row "collapse
    parent" case), Enter-toggle on a header, highlighting surviving into
    a nested snippet, small-excerpt-inserts-literally vs.
    large-excerpt-collapses, Ctrl+G expansion, and full-text-on-submit
    regardless of whether it was ever expanded — all in
    `tests/test_history.py`. 612 tests green (604 baseline + 8 net new).
  - README: the `/search` section gains the tree/excerpt-insertion
    behavior. `assets/shots/search.gif` regenerated
    (`scripts/record_gif.py`) — the old scene showed a flat three-hit
    list; it now shows the same three (now three-SESSION) hits collapsed
    to headers, one expanded, a snippet highlighted and inserted as an
    excerpt — item J is visible in the same recording, so no second GIF
    was needed.

## 0.20.0 — 2026-08-24

- **Branch switch, explicit branch selection** (queue item S) — this
  item's spec text was lost; what ships here is RE-DERIVED from the
  item's name plus the v0.19.0 codebase, following existing conventions
  wherever a call had to be made (each flagged in its own bullet below).
  Since v0.17, worktree-per-session forks every session's own branch
  (`doxa/<id>`) from whatever the launch cwd happened to have checked
  out — implicit, and un-overridable mid-session. This makes that
  selection explicit, both at spawn and live.
  - **Spawn-time** — `doxa new --branch <name>` forks the session's
    worktree from `<name>` instead of the launch checkout. New
    `doxa.worktrees.resolve_ref(main_root, ref)` validates it as a local
    branch, or a remote-tracking ref (`origin/foo`) resolved to local
    semantics (a local `foo` if one exists, else the remote-tracking ref
    itself) — `cli.py` checks this BEFORE ever spawning a daemon, with an
    actionable `doxa: no such branch: 'foo'` rather than silently falling
    back to `worktrees.create()`'s own permissive "cwd is fine too"
    contract. Threaded through `spawn_daemon` (a subprocess arg, `--base-
    branch`, since the daemon is a separate process) → `SessionDaemon`
    → `worktrees.create(base_branch=...)`. With `worktree_per_session`
    OFF, `--branch` refuses by default — silently moving the user's REAL
    checkout is exactly what this feature must never do — unless
    `--checkout` is also given, and even then only on a clean tree.
  - **In-session** — `/branch` (no args: every local branch, current base
    marked; an argument switches) joins the registry
    (`doxa/commands.py`) and the palette (`Branch: switch`, not
    prefilled — judgment call: it runs directly like `/model`, not like
    `/msg`'s prefill, since the no-arg form is itself a useful answer).
    Switching is FREE (a fast-forward rebase, no history to replay) only
    when the session's worktree is clean and carries zero commits ahead
    of its CURRENT base — the exact test `worktrees.finalize` already
    applies at session end, reused rather than reinvented. Anything else
    refuses in that same voice: `"... has uncommitted changes ... same
    rule as 'kept doxa/<id> — merge when ready' at session end."` The
    engine's cwd never moves; only the worktree's branch pointer does.
    Judgment call: with NO session worktree at all (`worktree_per_session`
    off, or a cwd that never got one) `/branch` refuses a switch outright
    rather than falling back to a real `git checkout` — same "never move
    the real checkout silently" rule the spawn-time flag follows, applied
    symmetrically in-session; listing still works there (harmless,
    read-only), reading whatever real repo the cwd sits in.
  - **Daemon protocol** — a `"branch"` RPC, same shape as `set_model`/
    `answer_needs_input`: the daemon owns the git operation
    (`doxa.worktrees.branch_status`/`switch_base`, run off the loop), a
    successful SWITCH broadcasts a `base_changed` event to every attached
    client (not just whichever one asked), same "everyone learns it"
    rule `model_changed` already follows. `doxa.engine.SessionEngine`
    gets a matching `switch_branch` for `--in-process` mode, delegating
    to the identical `doxa.worktrees` functions so the two paths share
    one implementation and one set of refusal messages.
  - **Status bar and tab label** — VERIFIED the existing
    `GitLine`/`branch_label()` path updates in place rather than being
    rebuilt: a new `GitLine.base_branch()` re-reads the worktree
    sidecar's `base_ref`, mtime-guarded exactly like the branch/sha
    fields beside it, so a live `/branch` switch (which rewrites that
    sidecar, new `worktrees.update_base`) is visible on the next event-
    driven render with no polling and no reconstructing the pane's
    `GitLine`.
  - **Two pre-existing regressions, found and fixed while wiring this
    in** (operator-reported, folded into this release rather than
    shipped separately since both sit in the exact file region this item
    already touches):
    - **The repo slot named the worktree, not the repo.** Since v0.17,
      every session's cwd IS a linked worktree, and
      `GitLine.repo = Path(repo_root).name` read `git rev-parse
      --show-toplevel`'s answer — the WORKTREE's own directory
      (`doxa-f13526d4`), not the repo (`doxa`). The tab read
      `Opus@doxa-f13526d4:doxa/f13526d4` and the `/about` `repo` row
      matched: the session id printed twice. Fixed by resolving the repo
      name through `GitLine._commondir` instead (already computed, pure
      filesystem reads, for the sha fix `_read_sha` needed for the same
      reason back in v0.17) — its parent IS the main repo root, worktree
      or not, costing no additional subprocess call.
    - **The tab label showed the session's OWN branch, not what it was
      based on.** A second-order effect of the same v0.17 change: once
      the repo slot was fixed, the branch half still read
      `branch_label()` — the worktree's own throwaway branch
      (`doxa/f13526d4`) — which is session IDENTITY, not the base the
      operator actually orients by (`main`, or whatever `doxa new
      --branch` forked from). `GitLine.tab_branch()` now answers that
      question instead (falling back to `branch_label()` when there is
      no worktree sidecar — `worktree_per_session` off reads exactly as
      it always did); `branch_label()` itself is untouched and keeps
      backing the status bar and `/about`, where session identity
      belongs. A worktree-isolated tab gets one more character saying so
      (`TAB_ISOLATION_MARKER`, the same `⎇` glyph `render()` already
      uses) appended ONLY when it fits the existing width budget for
      free — the base branch itself never gives up a character to make
      room for it, and the four-tabs-at-80-columns invariant
      (`TAB_LABEL_MAX`) is unchanged.
  - Tests: real git worktrees throughout (house pattern, no mocking of
    git) — `resolve_ref`/`list_local_branches`/`branch_status`/
    `switch_base` in `tests/test_worktrees.py`; `--branch`/`--checkout`
    (alternate-branch spawn, invalid-ref message, toggle-off refusal, the
    checkout path on clean and dirty trees) in the new
    `tests/test_cli_branch.py`; the `branch` RPC round-trip, its cross-
    client broadcast, and spawn-time `base_branch` threading in
    `tests/test_daemon.py`; the tab label showing the base (not the
    session branch), the status bar keeping the session branch, and the
    label following a live switch without rebuilding `GitLine`, in
    `tests/test_tab_labels.py`; the command surface and the non-repo
    message in the new `tests/test_branch_command.py`; the repo-name and
    tab-label regression fixes (and their now-corrected dedup behavior)
    in `tests/test_statusline.py`/`tests/test_tab_labels.py`. 604 tests
    green (567 baseline + 37).
  - README: the daemon/worktree paragraph gains the base-branch/isolation
    framing, `--branch`/`--checkout` and `/branch` semantics; the
    Quickstart command block gains `doxa new --branch <name>`.
    `palette.gif` regenerated (`/branch` is a new row in the palette's
    Session group, shifting which command a fixed sequence of arrow-down
    presses lands on) — every other shot drives `DoxaApp` directly
    against a plain repo checkout with no worktree in play, so nothing
    else in the gallery could have moved.

## 0.19.0 — 2026-08-24

- **Interactive permission** (queue item 5, the last feature item) —
  `ClaudeAgentOptions.can_use_tool` wired on `doxa.engine.SessionEngine`,
  closing the gap 0.11.0's entry named outright: "the engine has no
  `can_use_tool`/permission-prompt path today, so the trigger lands with
  that plumbing (phase 2)." It has now landed — the attention-blink timer
  that shipped dormant in 0.11.0, and the `notify_needs_input` setting
  reserved in the same release, both fire for real as of this version.
  - **What was actually missing**: without a `can_use_tool` callback, the
    SDK auto-denies anything the `claude` CLI would otherwise show
    interactive UI for — an `AskUserQuestion` call, or a permission
    prompt for a tool call it isn't sure about — silently, with no signal
    DOXA could act on. `doxa.gate.ToolGate`'s own `PreToolUse` hook stays
    the containment layer (its allow/deny decisions are unchanged by any
    of this); the callback's job is narrower, covering only the two
    genuinely interactive cases.
  - **The SDK contract, read from the installed package, not guessed**:
    `claude_agent_sdk` 0.2.144's `CanUseTool` is
    `Callable[[str, dict, ToolPermissionContext], Awaitable[PermissionResult]]`,
    invoked by `_internal/query.py`'s control-request dispatch for EVERY
    tool call a `PreToolUse` hook didn't deny and `allowed_tools`/
    `permission_mode` doesn't shadow (`types.py`'s own
    `_get_can_use_tool_shadowed_warning` documents the shadowing rules) —
    which is also exactly why the callback defaults to allow rather than
    prompting on everything: it fires for calls that flow through
    silently today too, and a bare `PermissionResultAllow()` is what
    keeps those unchanged. `ToolPermissionContext.title`/`display_name`/
    `decision_reason` are populated by the CLI specifically for a call it
    would have shown its OWN interactive prompt for (`title`'s own
    docstring: "the full permission prompt sentence... use this instead
    of reconstructing one") — the signal this callback reads to tell
    "would have prompted" apart from "flows through silently," with no
    local reimplementation of the CLI's own dangerous-action classifier.
    `AskUserQuestion` itself is undocumented in the Python SDK (a CLI-side
    tool, not an SDK type) — confirmed instead from the installed
    `claude` binary: its input schema carries an optional `answers` field
    described as "User answers collected by the permission component"
    (this callback), keyed by each question's own text; the model sees
    its answer back as an ordinary allowed tool call with `updated_input`
    carrying that map.
  - **Engine**: `_on_can_use_tool` routes `AskUserQuestion` to
    `_ask_user_question` and everything else with a populated
    title/display_name/decision_reason to `_request_permission`; both
    queue a `needs_input` `EngineEvent` on the SAME out-of-band queue
    `tool_disabled` already uses (this runs from inside the SDK's own
    control-request dispatch, never from `send()`'s yield points) and
    block on an `asyncio.Future` until `answer_needs_input(id, answer)`
    resolves it — no local timeout; a parked question waits exactly as
    long as nobody answers it, per the task's own no-auto-deny-on-timeout
    call. Resolution fires a matching `needs_input_resolved` event.
  - **Daemon protocol**: `needs_input`/`needs_input_resolved` ride the
    existing out-of-band event stream (`_peer_pump`, the ring, replay —
    nothing new there); the client answers with a `{"type": "call",
    "method": "answer_needs_input", "params": {"id", "answer"}}` RPC,
    matching `set_model`'s own shape. **Detached-session case**: a
    `needs_input` fired with NO client attached at all parks in the ring
    for free (`EventRing`/`_publish` already buffer everything regardless
    of who's listening) and fires the desktop notification itself
    (`doxa.daemon._peer_pump`, always the unfocused gate — there is no
    window to be focused) rather than waiting on a TUI that may not
    exist for hours; an attached client's own `app_has_focus` handles the
    common case as usual. Answering broadcasts `needs_input_resolved` to
    EVERY attached client, the same "everyone learns" convention
    `model_changed` already follows for `/model`.
  - **UI**: `NeedsInputPopup`, mounted above the prompt at the same
    position and under the same "never takes focus" discipline as
    `SlashComplete`/`SessionSearch` — checked FIRST in `PromptInput.
    on_key`, ahead of both. Up/down + Enter, number keys 1-9, Esc
    declines gracefully (a real `PermissionResultDeny`, never a silent
    hang). While pending: the tab blinks red (`set_needs_input(True)`,
    the exact mechanism 0.11.0 shipped dormant), `notify_needs_input`
    fires (focus-gated like every other trigger), and the status bar
    shows `⚑ needs input`. The blink (and its timer) clears on tab
    activation, the SAME existing convention `-done-unseen` already
    follows — the dialog itself does NOT auto-close on activation; only
    an actual answer or decline does.
  - **Zero-regression discipline**: every existing fake-engine-driven
    test still passes unmodified (543 baseline, none touched) — proof
    nothing that flowed through silently before gained a new prompt. New
    tests additionally assert the negative directly: an ordinary tool
    call with nothing in `ToolPermissionContext` populated is a bare
    allow with nothing queued.
  - 24 new tests (567 total: 543 baseline + 24) —
    `tests/test_needs_input.py` (engine-level: the zero-regression
    default, `AskUserQuestion` surfacing + answer/decline round-trip,
    permission-request surfacing + allow/deny round-trip, the
    scrubbed-summary assertion, idempotent/unknown-id `answer_needs_input`,
    a bare `decision_reason` alone triggering the permission path),
    `tests/test_daemon.py` (socket round-trip, detached-parking +
    notification + replay-on-reattach, multi-client resolution broadcast,
    unknown-id RPC failure), `tests/test_notify.py` (the same gating
    matrix every other trigger gets), `tests/test_needs_input_ui.py`
    (popup open/answer/decline via key and click, cross-client
    `needs_input_resolved` closing a stale dialog without re-answering,
    tab-activation clearing the blink but leaving the dialog open).
  - **Assets**: `attention-blink.gif` (built dormant in 0.16.0) finally
    embedded in the README, alongside a new `needs-input.gif`
    (`scripts/record_gif.py`'s `needs-input` scene, same fake-event-
    injection technique the attention-blink scene already used, driving
    the REAL dialog this time) showing the question opening, an arrow-key
    highlight move, and Enter resolving it — the other six GIFs and five
    stills are untouched.

## 0.18.0 — 2026-08-24

- **Subagent tracker** (queue item 4) — live view of Task-spawned
  subagents while they're still running, not just after they land in the
  trace tree. Built on the engine's existing `parent_tool_use_id`
  convention (every subagent event arrives tagged with the Task call's
  own tool_use id; a Task chip without its `tool_result` yet is a running
  subagent) — nothing new on the engine side, this is entirely a
  `doxa/app.py` surface over events the trace tree already carries.
  - **Registry**: `SessionPane._subagents`, a plain `dict[tool_use_id,
    ToolChip]` — a second INDEX into the same chip widgets the trace tree
    already mounts, not a copy of their state. Added on any `tool_call`
    named `"Task"` (top-level or nested — a subagent's own Task is
    tracked exactly like a top-level one), popped on that same id's own
    `tool_result`. Arrival order (plain dict insertion order) is the only
    ordering promised anywhere; no wall clock is kept.
  - **Status chip**: `⧉ N agents` in the status bar, hidden at zero — the
    same convention the peers chip already uses.
  - **Second status row**: `SubagentLine`, mounted directly below the
    status bar the moment the registry stops being empty, unmounted the
    moment it empties again (mount/unmount, never a display toggle — zero
    cost at idle). One `⧉ <label>` per running subagent (its own `description`
    input, ellipsized to ~24 cells), each a Textual click-action span
    (`[@click=open_transcript('id')]`) resolved against the row itself.
  - **Transcript tab**: `SubagentTranscriptTab`, a plain `TabPane` — no
    engine, no prompt — living alongside the session tabs in the same
    `#session-tabs` strip. Opened by clicking a running subagent's row;
    `replay()` mirrors whatever the live Task chip already buffered
    (narration text + its direct subcall chips, cloned via a new
    `_clone_chip`, never reparented out of the trace tree — the original
    widgets stay exactly where they were), then `SessionPane._handle_event`
    routes further matching events into the open tab AS WELL AS into the
    live chip for as long as it stays open. Marks itself `✓` on
    completion and, the same convention every other tab status follows,
    picks up `-done-unseen` if it isn't the active tab when that happens.
    No provider glyph (it never goes through `SessionPane.set_tab_label`
    at all — its own `_set_title` is a separate, deliberately glyph-free
    door onto the tab strip).
  - **Ctrl+W** on a transcript tab just removes it — no engine to stop,
    so `action_close_tab` now falls through to a `SubagentTranscriptTab`-only
    branch when the active tab isn't a `SessionPane`, skipping the
    detach/stop path entirely (`CloseWithTurnRunning` is Ctrl+Q-only and
    was never reachable from Ctrl+W regardless). Closing a SESSION tab
    takes its own still-open transcript tabs down with it — they have
    nothing left to show once the session that spawned their subagents is
    gone.
  - **Fixed along the way**: opening any new non-`SessionPane` tab while
    the previous tab's prompt input held focus tripped a genuine Textual
    behavior — `TabbedContent._on_tab_pane_focused` snaps `.active` back
    to whichever pane holds the currently-focused descendant, and a
    read-only transcript tab claims nothing on its own the way a fresh
    `SessionPane` claims its own prompt input on mount. Fixed by focusing
    the transcript tab's own (focusable) scroll container right after
    activating it — which also means the arrow keys/PageUp/PageDown a
    reader would reach for just work.
  - 10 new tests (`tests/test_subagent_tracker.py`): registry add/remove
    on the Task call's own lifecycle, the status chip hidden-at-zero/
    shown-at-N, the second line mounting and unmounting, a real pilot
    click opening a transcript tab with replayed content (and the mirror
    proven distinct from the live chip, still exactly where the trace
    tree put it), re-clicking a running subagent focusing rather than
    duplicating its tab, live events routing into an already-open tab,
    completion marking `✓` + `-done-unseen` in the background (cleared on
    activation), Ctrl+W closing a transcript tab with no stop dialog and
    the underlying turn left running, no provider glyph on a transcript
    tab, and closing a session tab cascading to its own open transcript
    tabs. 543 tests green (533 baseline + 10).
  - README: the trace tree section gains a paragraph on the tracker,
    with a new still (`assets/shots/subagent-tracker.png`/`.svg`, via a
    new `scripts/screenshot.py` scene) showing the second line, the
    status chip, and the extra tab side by side with the running turn —
    the existing trace assets only ever show a FINISHED subagent, so
    nothing already in the gallery communicated the "still running"
    state this feature is entirely about.

## 0.17.0 — 2026-08-24

- **Worktree-per-session** (queue item 3) — every session started inside a
  git repo now gets its own linked `git worktree`, so two sessions on the
  same repo, even the same branch, never stomp each other's edits. New
  `doxa/worktrees.py`: `create(cwd, session_id)` runs `git worktree add
  ~/.doxa/worktrees/<repo>-<id> -b doxa/<id> <branch-checked-out-at-cwd>`
  (`<id>` is the session id's own first 8 characters — stable from spawn,
  unlike the Haiku-generated tab name, which would make renaming the
  branch mid-session pure churn) and returns the worktree path; a non-repo
  cwd, or the setting off, returns `None` and behavior is exactly today's.
  Wired in at `doxa.daemon.SessionDaemon.serve()`, before the engine is
  built, so the substitution is invisible downstream — the engine's own
  `cwd`, the socket's `hello` frame, `EngineClient.cwd`, and the tab's
  `GitLine` all just see a cwd that happens to be a worktree.
  - New setting `worktree_per_session` (`DOXA_WORKTREE`, category
    Session, **default ON** — the user's own framing: "whenever a session
    starts in a repo branch"). `0` returns to today's behavior exactly.
  - **Lifecycle**, all in `worktrees.finalize()`, run once at a session's
    REAL end (never at a mere detach — a lingering daemon keeps its
    worktree intact so it can still be reattached): a CLEAN tree with
    ZERO commits ahead of the branch it forked from is removed with its
    throwaway branch, no trace left; a dirty tree, or one carrying
    unmerged commits, is kept — NEVER auto-merged — and the daemon's
    `stop` reply carries a `kept doxa/<id> — merge when ready` note the
    TUI surfaces as a toast (`self.notify`, since the tab that ended is
    already gone by the time a block mounted inside it would be seen);
    headless finalize (linger expiry, SIGTERM/SIGINT) logs the same line
    to the daemon's own log instead.
  - **Scope-key fix**: `git rev-parse --show-toplevel` from inside a
    linked worktree names the WORKTREE, not the main repo — measured
    against a real `git worktree add`. Left as-is, `doxa.peers.PeerHost`'s
    scope key (what makes two sessions of one repo find each other, and
    what `doxa`'s spawn-or-attach reuse path groups by) would fracture
    per worktree, one project silently reading as many. New
    `peers.main_repo_root_of()` resolves through `git rev-parse
    --git-common-dir` instead (the one shared `.git` directory every
    worktree of a repo agrees on) and now backs `PeerHost.repo_root` and
    `doxa.cli`'s spawn-or-attach scope calc; `/peers` and `/sessions`
    keep grouping one repo as one project across every worktree.
  - **Pre-existing bug fixed**: `GitLine._read_sha()` (the status bar's
    `@sha` chip) stated the checked-out branch's ref file under the
    worktree's own PRIVATE gitdir (`.git/worktrees/<name>/refs/heads/…`),
    which never has it — a linked worktree's branch ref lives in the MAIN
    repo's `.git/refs/heads/`, reached only through the worktree's own
    `commondir` pointer file. `_read_sha`/`_read_packed_sha` now resolve
    through a new `GitLine._resolve_commondir` before reading the ref;
    the pinning test in `tests/test_statusline.py` that documented the
    gap (`None` sha in a worktree) now asserts the fixed, correct
    behavior instead. With worktree-per-session default-on, this bug
    would otherwise have blanked the sha for every session's status bar.
  - `doxa doctor` gains a `worktrees` check: the worktrees dir
    exists/writable when the setting is on, and any worktree with no live
    daemon watching it (a crash before finalize, or one deliberately kept
    for merge — doctor doesn't try to tell those apart) is listed,
    report-only, with `git worktree prune` as the fix hint.
  - Tests against real git repos throughout (`tmp_path` fixtures, real
    `git worktree`/`git rev-parse` calls — this feature IS git behavior,
    mocking it would test nothing): create/reuse, non-repo passthrough,
    clean-finalize removal, dirty-finalize keep-with-note (both an
    uncommitted change and a clean-but-unmerged commit), the toggle off,
    scope-key agreement across a repo's worktrees, the `_read_sha` fix
    inside a real linked worktree, the doctor check, and the daemon-level
    wire-in (substitution on spawn, clean/dirty stop, detach leaves the
    worktree untouched). 533 tests green (506 baseline + 27).
  - README: the daemon section gains a paragraph describing the default
    worktree, the branch naming, and the keep-vs-remove rule; the
    settings table gains the `worktree_per_session` row. Screenshots
    unchanged — none of the gallery's scenes run through the daemon path
    `create()` is wired into (they drive `DoxaApp` directly against a
    `FakeEngine`), so nothing in a shot's status bar or tab strip could
    have moved.

## 0.16.0 — 2026-08-24

- **Animated demos for the interactive features** (queue item 2.5). A
  still can't show an interaction, and three of the gallery's stills were
  standing in for exactly that: `scripts/record_gif.py` extends
  `scripts/screenshot.py`'s own Pilot + FakeEngine approach one step
  further -- each scene scripts a SEQUENCE of steps instead of one,
  snapshotting an SVG per step (`app.save_screenshot`), rasterizing every
  frame to PNG with the same `inkscape --export-type=png` the static
  gallery already relies on, then assembling the sequence into a looping
  GIF with Pillow (already on disk transitively via `textual-image`, now
  also declared directly in the dev group -- a script that imports
  `PIL.Image` earns its own line rather than riding an app dependency's
  coattails). Six scenes ship, one deliberately unlisted:
  - **tab-lifecycle.gif** -- a second tab starts a turn (amber
    `-working`), switching away leaves it amber in the background, the
    turn finishes there (green `-done-unseen`), switching back clears it
    -- the same `_set_tab_class` toggles tests/test_tab_status.py already
    exercises, now driven end to end through a real Pilot instead of
    asserted in isolation.
  - **tool-calls.gif** -- a turn's "Tool calls (N)" fold ticking 1 → 2 → 3
    live as chips mount, the fold opening, then one chip opening onto its
    ARGS/RESULT -- `TurnBlock.add_tool_chip`/`ToolChip` driven directly,
    the same shape tests/test_restyle.py already exercises.
  - **markdown-stream.gif** -- an agent reply's markdown assembling
    incrementally: prose, then a three-row table filling in row by row
    across deliberately awkward chunk boundaries, closing on a bold total
    with an inline-code tool name -- `TurnBlock.append_text` fed the same
    shape tests/test_restyle.py's own chunked-markdown test uses.
  - **rename.gif** (replaces `rename.png`) -- a REAL double-click
    (`pilot.click(tab, times=2)`, the actual `event.chain == 2` path, not
    the direct `_start_rename` call the old static shot took), the inline
    editor appearing seeded with the old label, typing a new one, Enter
    committing it.
  - **palette.gif** (replaces `palette.png`) -- Ctrl+P opening the
    palette, arrowing down through New tab / open tabs / grouped
    commands, Esc closing it.
  - **search.gif** (replaces `search.png`) -- typing `/search deploy`
    opens the popup on a partial match, completing the query brings up
    all three hits, arrow keys move the highlighted row. The real session
    index is never touched: the old static shot skipped straight to
    `SessionSearch._render`; this scene additionally disarms the debounce
    timer `sync()` arms and patches `search_sessions`/`recent_sessions`
    as a second guard, since a multi-frame scene stays open long enough
    for the real 0.13s debounce to actually fire, which the old
    single-shot scene never risked.
  - Byte budget: every GIF is palette-quantized to ONE shared adaptive
    palette (built off every frame stacked into a strip, not just the
    first, so a color that only shows up once a tab flips amber still
    makes the 256-entry table) before Pillow writes the looping GIF. All
    seven land between 143 KiB and 357 KiB, comfortably under the 1MB
    target.
  - **attention-blink.gif** built and tested (`pane.set_needs_input(True)`
    driven directly, 4 alternating frames off the same timer
    tests/test_tab_status.py's attention-blink tests cover) but NOT
    embedded in the README: nothing in the shipped app calls
    `set_needs_input(True)` yet -- it is dormant phase-2 infrastructure,
    and documenting a demo of it would contradict the README's own "no
    animated chrome, exactly two timers" claim for a feature nobody can
    actually trigger.
  - `scripts/record_gif.py`'s own scene run against every scene IS the
    test, same footing as `scripts/screenshot.py`'s stills;
    `tests/test_record_gif.py` adds six fast, render-free checks against
    the scene registry itself -- unique non-empty names, every scene
    declaring more than one frame, every declared widget a real class,
    every scene sized within 2% of 16:9. 506 tests green (500 baseline +
    6).
  - `scripts/screenshot.py` lost the three scenes these GIFs superseded
    (`rename`, `palette`, `search`) and their now-dead drive functions --
    one source of truth per feature, so nobody re-generates a static PNG
    the gallery no longer shows; `SEARCH_HITS` moved into
    `scripts/record_gif.py`, its only remaining consumer.
  - README: the hero/memory/trace stills stay static on purpose (a crisp
    first impression, no autoplay noise at the top of the page); three
    GIFs replace their static equivalents where those features are
    already described, and two new bullets -- **Markdown responses** and
    **Tool-call compaction** -- cover ground the README never documented
    before, each with its own new demo.

## 0.15.0 — 2026-08-24

- **A provider glyph on every tab** (queue item 2, part 1) — ✳, Claude-
  orange, prepended to every tab's label ahead of the model tier
  (`✳ Sonnet@doxa:main`). Multi-provider engines are planned but not
  shipped, so `PROVIDER_GLYPHS` is a one-row table (`"claude": "✳"`) built
  so a future provider is a second row, not a branch in the display
  logic. The glyph is display-only: it paints onto the actual tab header
  and the palette's tab listing, but never becomes part of a tab's
  IDENTITY string, so a pinned (user-renamed) tab still gets the glyph —
  provider identity is orthogonal to the name — and the rename field
  still seeds from the plain name, never `"✳ my old name"`.
  Confirmed empirically before committing to color: Textual 5's `Tab`
  renders its label through `Content.from_markup` by default, so
  `[#D97757]✳[/]` paints real color rather than a literal bracket.
  `TAB_LABEL_MAX` moves from 34 to 32 to make room — the glyph and its
  separating space cost 2 cells that were not budgeted before, and the
  cut keeps the four-tabs-at-80-columns target the constant always
  documented.
- **The status bar's git chip says which worktree you're in** (queue item
  2, part 2) — `repo ⎇ branch@worktree @sha` inside a linked worktree,
  the same `branch@worktree` a tab label already showed. `GitLine.render`
  now builds its branch half from `branch_label()` instead of a raw HEAD
  read, so the dedup rule (append the worktree suffix only when it says
  something the branch and the repo slot beside it do not already say)
  is inherited from one place rather than re-implemented for the status
  bar. The `@sha` placement and its hex-collision handling are untouched.

## 0.14.0 — 2026-08-24

- **Start-menu launcher** (`doxa launcher install|uninstall`) — a per-user
  freedesktop entry, because "which distro?" turned out to be the wrong
  question: every major desktop (GNOME grid, KDE kickoff, XFCE whisker,
  rofi) reads the same two XDG files. `install` writes exactly those two —
  `~/.local/share/applications/doxa.desktop` (`Terminal=true`, so the
  DESKTOP picks the terminal emulator rather than DOXA guessing one) and
  the 512×512 icon into the hicolor theme — then refreshes the caches
  best-effort. No root anywhere.
  - The icon is the repo's own `assets/icon.png`, mapped into the wheel at
    build time (hatch force-include → `doxa/assets/icon.png`, read via
    `importlib.resources` with a repo-checkout fallback) — one file in
    git, and the curl-piped installer needs no second network fetch.
  - `scripts/install.sh` runs `doxa launcher install` best-effort after a
    successful install; `DOXA_NO_LAUNCHER=1` opts out. macOS: no start
    menu — the command says so and writes nothing. `uninstall` removes
    exactly what install wrote, never a foreign file (tested against a
    neighboring `.desktop`).

## 0.13.0 — 2026-08-24

- **Visual restyle: boxes to background tints, tool-call compaction,
  markdown responses** (queue item 1). The transcript had grown three
  separate legibility problems as sessions got longer, all traced to the
  same root cause -- every `TurnBlock`/`SystemBlock`/`ToolChip` was a
  bordered box, so a long session read as a stack of identical crates
  rather than a conversation:
  - **Boxes → tints.** `TurnBlock`/`SystemBlock` lose their round border
    entirely; role is now carried by position on the existing surface
    ramp instead of a border -- the turn's fold header (the user's
    prompt) sits on the RAISED tint (`#221F1A`, one step up from the
    screen), the agent's response sits on the screen's own BASE tint
    (`#171512`, so a reply reads as written on the surface rather than
    boxed on top of it), and doxa's own system/TUI-internal lines sit on
    a DIMMER tint (`#1D1B17`, one step down -- the same step
    `PeerMessageBlock` already used) with muted text. No new colors:
    every value is an existing stop on the ramp. `ToolChip` keeps its
    bordered chrome throughout -- a tool call reads as a nested artifact,
    not a transcript entry, at every fold level of a trace tree.
  - **Tool-call compaction.** A turn's top-level tool chips (the trace
    tree's own subagent nesting is untouched) now compact behind ONE
    `ToolCallsSection` fold, "Tool calls (N)", collapsed by default and
    created lazily on the first chip -- a turn with none grows no section
    at all (hide-at-zero, same convention as the status bar's optional
    chips). N updates live as chips mount mid-turn, a cheap title
    rewrite; if the user expands the section mid-turn it stays expanded
    as further chips arrive -- nothing in `add_chip` ever touches
    `.collapsed` itself.
  - **Markdown responses.** The agent's streamed response now renders as
    markdown -- tables, bold, fences, inline code -- via
    `Markdown.get_stream` (textual 5's append-only path built for LLM
    deltas: each `text_delta` chunk is fed straight to the stream, no
    full-document re-parse). Verified against chunk boundaries that split
    mid-table-row and mid-bold-span, the real shape of an LLM stream, not
    just whole-message renders. The user's own prompt (the fold header)
    and a subagent's trace narration are deliberately left as literal
    plain text -- typed text must not reflow, and the trace tree stays
    out of scope. `TurnBlock.mark_done` stops the stream's one background
    task -- an event-driven `asyncio.Task`, not a Textual `auto_refresh`
    timer, so it needed its own idle-CPU test alongside the existing
    armed-timer guards: a finished turn must not leave it running any
    more than it may leave a timer running.
  - **DEFECT found regenerating the gallery, then fixed**: `.turn-tools`
    (the `Vertical` holding a turn's tool chips) never set its own
    height, so it inherited Textual's `Vertical` default of `height:
    1fr` -- inside an auto-height `Contents`, inside a scrolling block
    list, that resolves against the viewport and stretches the container
    to fill it, padding every turn out with a screen's worth of empty
    space below its real content. Pre-existing (not introduced by this
    item), and invisible on `main` by coincidence: the old bordered
    `TurnBlock` spent two extra rows on its own top/bottom border edges,
    which happened to keep `scroll_end`'s math from clipping anything.
    Once the border came off, that margin was gone and the fold header
    of the `hero` scene's own turn scrolled a row past the fold on a
    32-row terminal -- caught by eyeballing the regenerated screenshot,
    not by any test. Fixed with an explicit `height: auto` on
    `.turn-tools`.
  - **Screenshots regenerated** (`scripts/screenshot.py`: `HERO_SCRIPT`
    now streams a markdown table + bold text across deliberately awkward
    chunk boundaries so the gallery actually shows (c) working, not just
    prose; `_drive_trace`/`_drive_memory` now open the turn's
    `ToolCallsSection` before expanding a chip inside it, since an
    unopened section hides its whole `Contents` -- nested chips
    included, same as any other collapsed `Collapsible`): all nine
    `assets/shots/*.svg` + `.png` re-rendered against the restyled
    chrome and reviewed by eye, not just regenerated.
  - 484 tests green (was 476 pre-restyle): 8 new -- hide-at-zero for the
    tool-calls section, live count + stays-expanded-on-mount, the trace
    tree's nesting unaffected by compaction, markdown surviving chunked
    table/fence/bold, the user prompt staying literal, no stream for a
    text-free turn, and the stream's background task gone after
    `mark_done`. Two existing trace-tree tests updated for the new
    `TurnBlock.tool_section` structure (chips no longer mount directly
    into `TurnBlock.tools`).

## 0.12.0 — 2026-08-24

- **Cost display audit, then build** (item T) — measured DOXA's cost
  numbers against real API usage before touching the display code, per
  0.10.0's own note that item T owes item AA a byte-priced isolated-vs-
  unisolated comparison. THE AUDIT (real subscription-OAuth turns, Claude
  Haiku 4.5, one-off scratchpad scripts, never committed):
  - **Hand-priced reconciliation.** A cold-cache, no-env-override turn
    (`ClaudeAgentOptions.env` unset — the pre-item-AA shape) reconciled
    to the cent: `usage` reported input 10 + cache_creation 23,297 (1h
    TTL) + output 79 tokens; at Haiku 4.5's published $1/$5 per MTok with
    the documented 2× 1h-cache-write multiplier, hand-priced arithmetic
    gives $0.046999 — `ResultMessage.total_cost_usd` reported exactly
    $0.046999. Repeated on DOXA's real, isolated engine path
    (`SessionEngine._build_options()`, LORE snapshot + native tools +
    hooks) at full cache warmth (cache_read 26,755, cache_creation 0,
    output 38–48 across four separate turns): hand-priced arithmetic
    (input $1 + cache-read 0.1× + output $5 per MTok) undershoots
    `total_cost_usd` by a consistent ~32–34% ($0.0029 hand-priced vs.
    $0.0038–0.0039 reported, every time) — small in absolute terms
    (under a tenth of a cent) but reproducible across all four isolated
    runs, while the SAME formula matched the unisolated path exactly on
    two further warm-cache turns. No corrupted or nonsensical values were
    observed anywhere (no negative costs, no missing fields) — the
    divergence looks like the published "cache reads are ~0.1× input"
    figure being an approximation, not a bug in what the SDK reports.
    **Conclusion: `total_cost_usd` is authoritative and kept as-is** —
    it is computed server-side from metered usage, not from a
    client-side guess, and hand-priced arithmetic is not a more correct
    number to substitute in.
  - **Isolated vs. unisolated, controlled and byte-priced** (the
    comparison 0.10.0 deferred to this item, superseding that release's
    own uncontrolled number). Same `SessionEngine._build_options()` —
    same system-prompt append, same native LORE MCP tools, same hooks —
    with `env` as the ONLY variable (item AA's real fix vs. `env={}`,
    the pre-fix shape that inherits the operator's own `~/.claude`).
    Cache effects controlled for by repeating each side to full warmth:
    unisolated settled at 26,779 total prompt tokens (cache_read +
    cache_creation), isolated at 26,755 — a 0.09% difference, i.e.
    isolation's byte/cost impact on THIS prompt is a rounding error, not
    a saving. (An earlier, uncontrolled comparison — a bare
    `ClaudeSDKClient` with no system-prompt append at all against the
    full DOXA engine — swung by double digits in both directions
    depending on cache state; it is not reported as a finding because it
    wasn't holding the prompt constant.) This confirms 0.10.0's own
    caveat in code: item AA's value is closing the foreign hook/plugin/
    command channel (5 plugins, 16 hooks, 28 commands, one external MCP
    server, structurally to zero) and the LORE-citation-contamination
    defect it caused — not a token-cost saving.
  - **Subscription-vs-API discriminator verified, not rebuilt** — per
    the item's own constraint. `engine.account` (captured at `start()`
    via `get_server_info()`) and `doxa.identity`'s
    `organizationRateLimitTier` reads were exercised live: on this
    account (`Claude Max`, `organizationRateLimitTier:
    default_claude_max_20x`), `identity.account_tier()` correctly
    resolves the precise `"max 20x"` label ahead of the SDK's coarser
    `"Claude Max"` string, exactly as designed. Left untouched.
  - **One real divergence found and fixed**: the status bar
    (`sub:<tier> (≈$X if API)`) and `/usage` (`plan  tier  (≈$X if
    API)`) already demoted subscription cost to an explicit what-if —
    audited and confirmed correct, no change needed. The per-turn cost
    line in each `TurnBlock`'s title (`doxa.app.TurnBlock.mark_done`)
    did NOT: it rendered a bare `$0.0043` unconditionally, subscription
    or not — a real-looking per-turn bill sitting directly under a
    status bar that just said the account pays no dollars. Fixed to
    take the same `account_tier` lookup the other two surfaces already
    use and render `≈$0.0043 if API` on subscription auth, unchanged
    `$0.0043` on API-key auth (or before account info arrives).
  - **Amendment — effort status-bar chip.** `/effort` (`doxa/
    commands.py`) has always been able to set the CLI's `--effort`
    level, but nothing showed what a RUNNING session actually asserted
    at connect. `SessionEngine._build_options()` now records what it
    asserts as `self.effort` (mirrors `self.account`/`self.model`'s
    connect-time-capture shape) — deliberately the CONNECT-TIME value,
    not a live re-read of `/effort`'s config, since a mid-session
    `/effort` change is explicit that it never reaches the already-
    running session. `_refresh_status` renders it as an `effort:<level>`
    chip beside model/ctx/cost, hidden entirely when no level was
    asserted (the CLI-default case) — the same hide-at-zero convention
    every other optional chip on that line already follows.
  - `tests/fakes.py::FakeEngine` gained the matching `effort` attribute
    (constructor kwarg, default `None`) in lockstep, per the engine-
    surface-parity rule the fake's own docstring states. 476 tests green
    (was 472 on `main` post-0.11.0, itself up from 430 at 099edca): 4
    new here — connect-time effort capture, the chip's hidden/shown
    states, and the turn-cost-line fix.

## 0.11.0 — 2026-08-24

- **Per-status tab colors** — the tab strip now says what each session is
  doing without switching to it. Before this, a background tab looked the
  same whether its agent was mid-turn, finished, or idle — the only signal
  was the active tab's orange accent.
  - `-working` (amber) while a turn is in flight; `-done-unseen` (green)
    when a turn finishes on a tab that is not the active one, cleared the
    moment that tab is activated (or a new turn starts on it). The active
    tab never shows done-unseen by construction — you are already looking
    at it.
  - `-attention` blink infrastructure (0.5 s class toggle) for
    "the agent needs input": the timer exists ONLY between
    `set_needs_input(True)` and its matching `False` — zero timers while
    idle, per this app's idle-CPU discipline. Nothing sets it yet: the
    engine has no `can_use_tool`/permission-prompt path today, so the
    trigger lands with that plumbing (phase 2), which will also wire the
    reserved `notify_needs_input` setting below.
  - Precedence on an inactive tab: attention > working > done-unseen.
- **Desktop notifications** (`doxa/notify.py`) — `notify-send`-based, the
  same shape (and silent no-op degradation) as LORE's own notifier; icon
  override via `DOXA_NOTIFY_ICON`. New "Notifications" settings category:
  master `notify` = `auto`/`always`/`off` (`auto` fires only when the
  terminal window is NOT focused, tracked via Textual's
  `AppFocus`/`AppBlur`; a terminal that never reports focus never blurs,
  so `always` is the escape hatch), plus per-trigger toggles.
  - Wired: turn-done (fires with the pane's display name when a response
    lands), update-available (a startup background worker runs
    `git fetch` + `rev-list HEAD..@{upstream}` in the checkout DOXA runs
    from; any failure — offline, not a checkout — is silent, and the
    notification fires at most once per app run, pointing at `/update`).
  - Inherited from LORE: the in-process deriver's staged-proposal
    notification already fires today (`lore_core` reads `LORE_NOTIFY`
    fresh on every call), so `notify_lore=off` now sets `LORE_NOTIFY=0`
    for the process — no engine change needed. What it does NOT get yet
    is DOXA's focus-gating: lore_core's notifier is a blunt on/off;
    routing it through `doxa.notify` needs the phase-2 engine touch.
  - Reserved, unwired: `notify_needs_input` (see above).

## 0.10.0 — 2026-08-24

- **Engine CLI isolation** (item AA) — the `claude` process the engine
  spawns now gets its OWN config directory (`doxa.cli_isolation`,
  `~/.doxa/claude-cli`), never DOXA's own process environment. THE DEFECT
  (operator-reported, then measured): with no `ClaudeAgentOptions.env` set,
  the spawned CLI inherited the SDK's default env verbatim and read the
  operator's real `~/.claude` — plugins and all. Measured live on a real
  machine: a bare, otherwise-default spawn loaded 5 plugins, registered 16
  plugin hooks and 28 plugin commands (LORE's own `SessionStart`/
  `UserPromptSubmit`/`PreCompact` hooks among them), and started an
  external MCP server — ON TOP OF, not instead of, DOXA's own in-process
  LORE snapshot. That is what produced the reported symptom: a session
  citing the LORE *plugin*'s own pending count and `/lore:pending`, a
  command that has nothing to do with DOXA.
  - `CLAUDE_CONFIG_DIR` now points every spawned engine CLI (and
    `doxa.naming`'s headless namer call) at a directory DOXA owns
    outright, with an explicit empty `settings.json` (no `hooks`, no
    `enabledPlugins`, no `plugins`) and `LORE_SKIP=1` as belt-and-braces
    (the same self-suppression `lore_core.context`/`lore_core.deriver`
    already honor for `doxa.naming`'s call). Measured: a fresh
    `CLAUDE_CONFIG_DIR` alone drops plugin/hook/command loading to zero —
    no extra CLI flag needed. `--bare`/`CLAUDE_CODE_SIMPLE=1` was measured
    and rejected: it also forces API-key-only auth, silently logging out
    every subscription session, which item AA explicitly forbids shipping.
  - **Auth**: a fresh `CLAUDE_CONFIG_DIR` is a logged-out CLI. Credentials
    are copied (never symlinked) from the operator's real
    `~/.claude/.credentials.json` into the isolated directory, resynced at
    every session start and once more, forced, on the first connect
    failure — closing the "token rotated, isolated copy is stale" window
    without turning every OTHER connect failure into a retry loop.
  - **Learned skills carry through, deliberately**: `~/.claude/skills` is
    symlinked into the isolated directory (measured: the CLI resolves
    `<CLAUDE_CONFIG_DIR>/skills` for its own "skill dir commands" and
    follows a symlink there exactly like a real directory) — skills are
    human-approved artifacts, not the foreign-hook channel this item
    closes, and cutting them with the rest of the plugin channel would
    have been a silent regression.
  - **Two config directories, two consumers, unchanged for one of them**:
    `doxa.identity` keeps reading the REAL `~/.claude` directly (this
    process's own environment, never touched by `doxa.cli_isolation`) for
    the identity block and the subscription-usage chip — that stays the
    operator's own account, exactly as before.
  - `doxa.doctor` gains an `engine CLI isolation` check: directory
    provisioned, `settings.json` carries none of `hooks`/`enabledPlugins`/
    `plugins`, N learned skills visible, spawned session authenticates.
  - Measured, real first-turn usage on this repo, one trivial prompt,
    same account (prompt-cache noise applies — this is not a controlled
    A/B, see item T for a byte-priced comparison): unisolated
    input 10 + cache_read 21043 + cache_creation 7778 vs isolated
    input 10 + cache_read 18145 + cache_creation 9506 — isolated total
    ~4% lower, dominated by fewer available-command/skill descriptions in
    the CLI's own system context. The defect this item fixes is the
    foreign hook/plugin/command channel itself (structurally: 5 plugins /
    16 hooks / 28 commands to zero), not primarily token count.

## 0.9.0 — 2026-08-24

- **Multi-line prompt and clipboard paste** (item N) — the prompt is now a
  `TextArea` (`doxa.app.PromptInput`), not the single-line `Input` it was
  through 0.8.0. The forcing bug: `Input._on_paste` keeps only
  `event.text.splitlines()[0]` on a bracketed paste — every line after the
  first was silently dropped, no error, nothing. New behavior:
  - Grows 1..10 content rows from the wrapped (soft-wrap-aware) line
    count, then scrolls internally rather than displacing the block list.
  - Enter submits; Shift+Enter and Alt+Enter both insert a literal
    newline (whichever a given terminal actually distinguishes from bare
    Enter — item O's keyboard-protocol detection is what will one day
    tell the operator which; both are bound regardless so neither
    terminal family goes without a deliberate-newline key).
  - A bracketed paste is always exactly ONE document edit, however many
    embedded newlines it carries — nothing in the paste path can trigger
    a submit, so a multi-line paste can never be mistaken for N presses
    of Enter (each of which is a billed message). CRLF and lone CR both
    normalize to LF.
  - A paste over 4 lines or 4 KB collapses to `⧉ pasted N lines (X KB)`
    (`doxa/paste.py`, shared with item J's excerpt-insertion clipboard
    helper); Ctrl+G expands the placeholder under the cursor back to the
    real text to look at it, and the full text is what actually goes out
    on submit whether or not it was ever expanded.
  - `ctrl+v` is deliberately unbound (mapped to a no-op): `TextArea`'s own
    binding pastes from Textual's in-process `App.clipboard` variable —
    whatever this app last copied — not the live OS clipboard, which is
    silently wrong on a terminal that hasn't echoed an OSC52 write back
    in. The terminal's own paste (bracketed paste) is the real path and
    is unaffected.
  - Image clipboard: a terminal cannot forward binary clipboard content
    through bracketed paste at all (no escape sequence carries it) — an
    empty paste is the only signal available. DOXA checks `wl-paste`/
    `xclip` off the event loop and reports what it found (`image/png`,
    say) as a `SystemBlock`, rather than pretending to attach it — there
    is no image-attachment path into a turn yet to hand the bytes to.
  - Deferred, deliberately: the old `Input` placeholder text
    ("Ask DOXA…") has no `TextArea` equivalent and was dropped rather
    than reimplemented behind an overlay widget; interactive verification
    in a real terminal (bracketed-paste baseline, Shift-drag copy-out)
    was not re-done here and should be spot-checked in one before relying
    on it blind.
  - Every `Input`-era test/script call site (`.value`, `.cursor_position`)
    keeps working through compatibility properties on `PromptInput`.

## 0.8.0 — 2026-08-24

- **Clock** (item M) — a fixed-width chip at the right edge of the tab
  bar (`doxa/clock.py`, `doxa.app.ClockChip`). Configurable: show/hide
  (defaults ON — the one bool setting in this app that does), a date
  prefix, 12/24-hour, seconds, an IANA timezone, or a full custom
  `strftime` that overrides the toggles; a bad timezone or a format
  `strftime` rejects (or reduces to nothing, which glibc does more often
  than it raises) falls back to the built-in format and system-local
  time VISIBLY, as the chip's tooltip, never silently. Laid out on its
  own compositing layer (`layers: base overlay` in `theme.tcss`) rather
  than as a flow sibling of the tab bar, which is what makes it never
  reserve width from — or displace — a single tab: docking a widget
  inline with the tabs would have reserved its column for the app's full
  height, not just the two rows it actually occupies (measured before
  settling on the layer approach). Exactly one timer for its whole life,
  and only while enabled: it rides Textual's own `auto_refresh` slot,
  re-armed to a freshly computed BOUNDARY-ALIGNED delay on every tick
  (minute-aligned with seconds hidden, second-aligned when shown) rather
  than a fixed-Hz repaint of a string that usually hasn't changed. The
  no-idle-timer guard tests (`tests/test_chrome.py`, `tests/test_app.py`)
  now name this one exception explicitly and still fail on any other.
  Measured idle CPU over an 8s window (`scripts/clock_cpu_bench.py`):
  off 0.75%, on with seconds hidden 0.75% (indistinguishable from off —
  the minute-aligned timer essentially never fires in an 8s sample),
  on with seconds shown 1.12%. Gallery scene: `assets/shots/clock.png`.

## 0.7.0 — 2026-08-24

- **`/doctor` and `doxa doctor`** (Tools & config) — read-only health
  checks: pass/fail plus the exact fix command per check. Python and DOXA
  versions, the `claude` CLI's version and auth state, the LORE store's
  location and active belief count, whether `config.toml` parses, live
  daemon count plus stale presence files (report only — added
  `doxa.peers.count_stale`, the read-only twin of `sweep_stale` that
  counts the same dead entries without deleting any of them), the
  detected terminal image protocol, and MCP reachability (nothing
  configured yet, honestly reported as such rather than a check standing
  in for a feature that doesn't exist). Keyboard-enhancement grant is
  reported `?`, not guessed pass/fail — Textual requests the protocol at
  session start but doesn't expose whether the terminal actually granted
  it; real detection is item O's job. `doxa doctor` (`doxa/cli.py`) runs
  headless, no TUI, exit 1 if anything failed.
- `scripts/install.sh`'s doctor step and `/setup`'s final step both wire
  to this for real now — the placeholder wording each shipped with is
  gone.

## 0.6.0 — 2026-08-23

- **`/setup`** (Tools & config) — check state, fix findings one at a time,
  each behind its own confirmation showing exactly what applying it will
  change. Four steps: auth state (surfaced only — `/login` still owns
  signing in), the LORE store (env wins outright; a prior run's choice is
  remembered; an existing store the Claude Code plugin uses is the one
  genuinely ambiguous case, and that's the one that asks instead of
  silently picking a side), `/migrate` (offered when a later DOXA version
  ships one, skipped cleanly here since it doesn't yet), and model/effort
  defaults (hands off to the settings modal, the surface that already
  edits those knobs). Finishes with a summary; the doctor line is a
  placeholder until `/doctor` ships. Auto-runs once, on a genuine first
  launch on this machine (a `~/.doxa/.setup-done` marker, written the
  moment the wizard is OFFERED so declining it can't make it nag again);
  `/setup` runs it again on demand, any time.
- `doxa.config` gained `save_lore_root` — the one write `/setup` makes
  directly, bypassing the settings modal's read-only gate on that row on
  purpose (it's `/setup`'s row to decide, not a field to fat-finger). A
  sticky choice is exported to `LORE_ROOT` before `lore_core` is ever
  imported (`doxa/_lore_bootstrap.py`), since that module reads the
  environment once, at its own import time.

## 0.5.0 — 2026-08-23

- **`scripts/install.sh`** — a `curl | sh` installer, POSIX sh (tested
  against dash). Checks python3 (minimum read live from the target ref's
  own `pyproject.toml`, never a literal baked into the script), offers to
  install `uv` if it's missing (never silently), requires `git`, requires
  the `claude` CLI present *and* authenticated (`claude auth login`
  otherwise, and it stops there). Installs with
  `uv tool install --force git+https://github.com/docwilde/doxa` — never
  PyPI, DOXA isn't published there. Creates `~/.doxa` if absent, never
  touches an existing `config.toml`. Idempotent (a second run updates
  rather than refuses); pipe-safe (the whole script is one function called
  on the last line, so a `curl | sh` pipe truncated at any point runs
  nothing — verified by literally truncating the script at nine byte
  offsets and asserting no side effect). `sh -s -- v0.5.0` installs a
  specific tag instead of `main`'s HEAD. README's install section leads
  with the one-liner now, with an inspect-first alternative and the old
  `git clone` path kept as a fallback.

## 0.4.0 — 2026-08-23

- **Tabs are real sessions, and they say so.** `SessionPane` extraction under a `TabbedContent`: N sessions, one engine handle each, worker groups scoped per pane so a closed tab takes its workers with it. Ctrl+T spawns a fresh daemon in the same repo scope, Ctrl+W detaches it, Ctrl+Q ends it.
- **Tab labels: `Opus@doxa:main`** — short model tier, repo, branch (`branch@worktree` in a linked worktree when the name adds something). Truncation at 34 columns sacrifices the model first, the repo second, and protects the branch. Outside a repo the session names itself from its first turn with one cheap Haiku call, cached in `~/.doxa/names.toml`; the directory name stands in before it and permanently if it fails.
- **Rename a tab in place** — double-click the header (or `/rename`), Enter commits, Esc cancels, empty restores the automatic label. A named tab is pinned: model switches and branch changes stop rewriting it.
- **`/search`** — full-text search over LORE's session index, live in a popup above the prompt from the moment you type `/search `. Debounced, sequence-guarded (a slow query can never repaint over a newer one's results), FTS5 snippets with the matched terms highlighted by the index rather than by us. Empty query lists recent sessions. Replaced the Ctrl+R modal entirely; Ctrl+R now prefills the command, so there is one search path.
- **Terminal images** behind a KGP → sixel → half-block → text ladder, with a guaranteed text fallback; tool results carrying an image render it inside the chip, lazily on first expand.
- **Subagent trace tree** — a Task-spawned subagent's tool calls nest under its chip, foldable at every level, formatted only when opened.
- **Streaming deriver** (`DOXA_DERIVE_SECS`, opt-in) — debounced mid-session LORE review; proposals still wait for the same human gate.
- **Act-time belief consult** — a cite-only note on the prompt, FTS only, floor-gated (`DOXA_CONSULT_FLOOR`).
- **Command surface has ONE order.** Every row of the slash registry declares a functional group; the prompt's autocomplete, the Ctrl+P palette and generated `/help` all iterate the same sequence. The palette adds sections: New tab, the open tabs in tab-bar order with the active one marked, the grouped commands, then attachable sessions.
- **Status line**: context-pressure escalation by colour with the percentage kept in every tier, real subscription headroom read from the CLI's own cached utilization, the git sha marked as a commit (`@a1b2c3d`) beside the branch, and the detached-session handle labelled (`⌁ session a1b2c3d`) — two unlabelled hex strings in one bar read as one id printed twice.
- **`peers N (2⌁)`** — how many live peers, and how many are running detached. Counting now requires a socket that answers, not just a presence file; a launch sweep removes what a crash left behind and says how many.
- **`/sessions`** — every live session with age and attached/detached state, `kill <prefix>` and `kill-detached`.
- **Settings modal** (Ctrl+,) over one precedence rule — environment > `~/.doxa/config.toml` > default — showing the effective value and where it came from.
- **`/model` `/effort` `/usage` `/clear` `/compact` `/help`** — only as far as the SDK actually goes: `/model` switches live (a control request, no reconnect), `/effort` is honest that the SDK sets it at connect time only.
- **`/login` / `/logout`** through the provider's own auth CLI, with the precise plan tier read from the CLI's local config.
- **`/update`** — fast-forward this checkout from origin, never merge, never rewrite; refuses a dirty tree or a non-checkout, runs `uv sync` when the dependencies moved, reports the commits pulled and the version before → after. `--restart` is the explicit opt-in that stops this window's sessions and relaunches.
- **Version is single-sourced** from `pyproject.toml`, exposed as `doxa.__version__`, and shown in the session's identity block.
- **Nothing animates.** The in-flight marker lost its 16 Hz repaint timer, and Textual's own tab underline stopped sliding: measured at ~290–345 ms of extra wall time per tab switch, gone.

## 0.3.0 — 2026-08-23

- **Session daemon** — the engine moved out of the TUI into its own process, reachable over a Unix socket. Detach and reattach freely; a daemon outlives its last client by `--linger` seconds, then finalizes (LORE review + index) itself. `doxa`, `doxa new`, `doxa attach [prefix]`, `doxa stop [prefix]`.
- **Command palette** (Ctrl+P) with a DOXA provider and an attach picker fed by the shared registry.
- **History search** (Ctrl+R) — BM25 over LORE's session index, debounced as you type, inserting a text reference into the prompt rather than auto-sending anything.
- Ctrl+C quits: one press detaches, a second inside the window stops the sessions; the daemon's SIGINT path stays graceful.
- Idle CPU no longer grew with scrollback (hidden thinking indicators kept their animation timers armed).

## 0.2.0 — 2026-08-23

- **Peer layer** — same-repo session discovery through a 0700 runtime registry, presence heartbeats, and scrubbed peer messaging (`/peers`, `/msg`). A message is scrubbed at the receiving choke point, never at the display.
- **Native LORE tools behind a registry** — belief search/show, session search, `lore_remember` (which stages a proposal and never writes memory), each declared as data with its cost and read-only status.
- **PreToolUse containment gate** — two strikes and the tool is disabled for the session, said out loud in the status bar.

## 0.1.0 — 2026-08-23

- **Session engine** wrapping the Claude Agent SDK with LORE wired in-process, event-stream API, host-driven session-end review (there is no SessionEnd hook — see `PHASE0_FINDINGS.md`).
- **Single-pane Textual shell** over it: foldable turns, tool chips that format their arguments and results lazily on first expand, streaming text.
- Dark surface ramp, Claude orange, round borders; logo and wordmark.
- Phase 0 spikes that decided the architecture: minimal agent loop, lifecycle-hook investigation, Textual + `claude-agent-sdk` asyncio coexistence.
