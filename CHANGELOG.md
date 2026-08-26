# Changelog

Newest first. Versions are annotated git tags on the commit that shipped
them (`v0.1.0` … `v0.15.0`); the ranges below are derived from that history,
not written from memory.

## 0.72.0 — 2026-08-26

Two branches, one tree state, one tag.

### The boot mark

- Redraws the boot mark: drops the grey ring, widens the triangle so it reads as solid and confident instead of a narrow spike, and spells ΔΟΞΑ out in full-block letters beside it, same colour as the triangle. `belief earns knowledge` sits below as plain text, replacing the old `doxa · belief earning knowledge` strapline.
- Draws the Greek letters from `█` rather than printing the Unicode characters, for the same reason the mark has never used half-blocks or Geometric Shapes triangles: a monospace font's Greek coverage is not guaranteed, and this sidesteps that tofu risk entirely instead of trading one glyph-coverage gamble for another.
- Keeps the plain Latin `DOXA` wordmark as a fallback for terminals too narrow for the full Greek word, and drops to it alone when even the triangle does not fit — the same three-stage ladder as before, with the thresholds (`DRAWN_MARK_COLUMNS`, `DRAWN_FULL_COLUMNS`) recomputed from the new art's measured width.
- Holds the total row budget at 9, same as the ring-era mark, despite giving Ξ and Α enough rows to read clearly: dropping the ring's moat frees the rows the tagline now spends on its own row below the word. The mid-width fallback (triangle + plain wordmark, no tagline) is shorter than before, at 7 rows rather than 9.
- Ο's sides were drawn two columns wide to make the curve read as a curve; fixed to match Δ and Α's single-cell stroke, with the roundness coming from narrower cap and base rows instead. Word width is unchanged (`GREEK_COLUMNS` 40, `DRAWN_FULL_COLUMNS` 58).

### The curated-memory chip

- Curated-memory chip (`mem u63% p39%`): the project half stayed absent past startup and never moved on a repo switch or a resume. `PaneChipsMixin._lore_slug` resolved the project slug from the pane's own construction-time `cwd`, never the connected engine's — the one reader in the pane still doing that (every other one already prefers `engine.cwd`). Now resolves through the engine's cwd first, falling back to the pane's only when there is no engine yet.
- Approving or rejecting a staged proposal, recording a belief outcome, and retracting a belief never refreshed the status bar afterward — the memory-fill, belief-count and staged-proposals chips stayed exactly as stale as their last unrelated refresh. The three write paths in `doxa/session/chips.py` (the status bar's own quick pending/beliefs pickers) now trigger a refresh once the write actually lands — never on the arming selection, which writes nothing.

## 0.70.0 — 2026-08-26

- A status refresh on a pane whose status bar had not mounted yet raised `NoMatches` from a background task, which the error surface turned into a visible block. Refreshes are event-driven — a peer joining, a turn finishing, a restore reporting its session id — and those arrive before compose finishes. A refresh with nowhere to draw is now a no-op; the next event repaints. Surfaced by this release's mount-ordering change, but the race predates it: it was reported during the v0.60.0 work as reproducible only against a real daemon and left unfixed.

- The drawn boot mark could overflow its column in CI (`test_narrow_terminal_never_overflows_the_glyph_art`): the transcript's scrollbar can appear after the banner's last resize and narrow its box without a Resize message following, leaving a fit computed for a wider box painted into a narrower one. The art now fits itself inside `render()`, against its own live `content_size`, on every paint — not a string cached by `_lay_out` on mount/resize — so a stale fit cannot survive a frame regardless of whether a resize arrives.
- The mark itself: nine rows instead of seven, with a real gap between the ring and the triangle (they read as one blob at the old size), and the ring now renders in a muted grey while the triangle keeps the orange accent — two shapes, not one two-tone blob.
- The raster boot banner is gone. `logo.png` used to draw on `kgp`/`sixel` terminals (`boot_banner=auto`/`image`); the drawn block mark reads better than a downscaled photograph even there, so there is no longer a case where the raster is the right answer. `boot_banner` collapses from a four-way choice to plain on/off — a `config.toml` still holding `auto`, `blocks` or `image` from before this change keeps meaning on. `/img`'s showcase is unaffected and still renders the raster logo in every tier a terminal answers for.
- `test_restore_tabs_open_in_saved_order_with_names_and_active_tab` failed in CI with a wrong active tab id, not the null one v0.38.0 already guards against. Textual's `Tabs` widget defaults itself to its first child tab on its own mount, and that default reaches `TabbedContent.active` as a queued message rather than a synchronous write — a later explicit write can still be overwritten once the stale default message is finally processed. Fixed by computing the correct starting tab before anything mounts and handing it to `TabbedContent` as `initial=`, so the wrong default is never posted in the first place.

## 0.67.0 — 2026-08-26

Gallery uniformity, one row shape for both LORE pickers, and inline
actions on their rows.

- Every asset in `assets/shots/` — 13 PNG/SVG pairs and 11 GIFs — now
  renders at the identical 3068x1734 (250x69 columns/rows, 16:9 within
  0.5%). Previously five different pixel sizes shipped side by side (a
  sixth, bespoke one for `beliefs-browser`), so the README visibly
  changed image size scene to scene. 250 columns is a measured floor:
  the seven scenes carrying a live status bar (`hero`, `trace`,
  `transparent`, `subagent-tracker`, `memory`, `sessions`,
  `image-support`) already needed it, and every other scene now matches
  for uniformity rather than its own smaller content-fit size.
- Scenes that had less content than the new frame got more, not blank
  canvas: `settings`/`clock`/`error-block` and every GIF scene now run
  behind or alongside a real three-tab session already mid-conversation;
  `beliefs-browser` gained a much larger scripted store (14 beliefs, 6
  proposals) in place of three and two. `banner`/`banner-blocks` gained
  two more tabs in the strip but not a running conversation — one would
  scroll the boot banner these two scenes exist to show out of frame —
  and their own blank space below the identity block is real, not
  padded over.
- `scripts/screenshot.py`/`scripts/record_gif.py` leaked real machine
  state into the gallery: `N proposals` and `mem u%p%` read straight off
  this machine's actual `lore_core` store (neither is scripted anywhere
  in either file), and `/settings` showed this machine's real config
  defaults. Measured, not assumed — `N proposals` read 205 one run and
  78 the next on the same unedited scene. Both scripts now isolate
  `LORE_ROOT`/`DOXA_HOME` the same way the test suite already does,
  before any `doxa` import.
- The beliefs and proposals chip pickers used to format their rows two
  different ways (a `belief_stamp` join with no fixed columns, a `` · ``
  string for proposals that drifted with every field's own length). One
  formatter now — `doxa.ui.labels.format_picker_row` — used by both:
  `YY-MM-DD HH:MM  status  age  text`, fixed-width prefix columns, text
  capped to `min(100, measured terminal width)` by the widget at render
  time. The matcher still scores the full untrimmed row, so a word past
  the visible cut stays findable.
- Both pickers gained inline row actions — approve/reject on a proposal,
  confirmed/contradicted/stale/retract on a belief — reachable without
  leaving the list: a click on the row's own action span, or the
  reserved letter while that row is highlighted. Retract and approve
  still arm on the first press and apply on the second, on the same row.
  Additive: selecting a row outright still opens the existing per-row
  action sub-menu unchanged, and its own tests keep passing as written.
- While either picker is open, the prompt input filters its rows instead
  of sending to the agent — typed text syncs live, Enter acts on the
  highlighted row rather than submitting a turn, Escape closes and clears
  it. The five reserved action letters only fire while the filter is
  empty; the moment it holds text they are ordinary characters, so typing
  a word that starts with one of them never fires an action on the way
  through.
- The `user`/`user-model` group headers (picker and full browser) now
  carry LORE's own channel tag — `user · stated` (the user said it
  themselves; a later session may act on it) vs `user-model · inferred`
  (read off behaviour, never spelled out; shapes tone and authorizes
  nothing) — and the full rule is in the belief tooltip. `project` is
  unaffected — it has no channel to distinguish.

## 0.65.0 — 2026-08-26

- `/peers` printed a peer's `title` and `cwd` raw. Another process writes both — a title derives from that session's first prompt, a cwd from a path — and the message receive path has scrubbed since it existed while the registry-read path never did. Scrubbed at the single point an entry becomes a `PeerInfo`, so a consumer added later cannot forget.

Gallery regenerated end to end: every asset in `assets/shots/` predated the
full-block banner, the permission-mode chip, curated-memory fill and the
staged-proposals chip, so the README was showing a mixture of eras.

- All 11 PNG/SVG pairs and 10 GIFs re-captured at this version.
  `banner-blocks` was also fixed in the process: its 80-column frame
  measured 1.51 against a 16:9 target (15% off, outside the ~2% the rest of
  the gallery holds to) — widened to 95 columns, rows unchanged, now 1.78.
- Six status-bar-carrying scenes (`hero`, `trace`, `transparent`,
  `subagent-tracker`, `memory`, `sessions`, `image-support`) widened from
  172 to 250 columns. Measured, not assumed: the live status bar's own
  plain text now runs past 200 characters with the mode chip, memory fill
  and proposals chip added since 172 was chosen, and `hero` was losing
  everything from the memory chip on.
- Three new pairs: `beliefs-browser` (the full `/beliefs` tab —
  scope-grouped beliefs carrying LORE's own outcome verbs, one staged
  proposal armed mid-approve), `error-block` (a caught exception rendered
  inline instead of killing the app), and `permission-mode` (the chip
  cycling default → plan → auto → bypassPermissions, teal/amber/red).
  Captioned in the README's gallery.
- `beliefs-browser`'s header rendered `lore_write_state_result`'s test
  fixture default straight into the image (`lore_core 0.36.0 (package)`)
  instead of asking `doxa.version.lore_core_version()` and
  `doxa._lore_bootstrap.resolved_source()` the way the real engine does —
  a hard-coded fact in a scene fixture, exactly the class of bug this
  release exists to catch, just not limited to the boot banner's version
  line this time. `scripts/screenshot.py`'s `_beliefs_engine()` now reads
  both live. The number on screen is unchanged on this machine (still
  0.36.0) but the source label corrects from `(package)` to `(plugin)` —
  this checkout's LORE plugin checkout wins over the pip-pinned v0.38.0
  per `doxa/_lore_bootstrap.py`'s own documented precedence, which the
  fixture default could not have known to say.
- `beliefs-browser` also stopped sharing the other new `WIDE` (250×69)
  geometry: its tab has no status bar or prompt box beneath it, so none
  of the reasoning that widened those scenes applied, and the content
  (two staged proposals, three belief rows) filled barely a third of the
  frame. Resized to its own content-fit 134×36.

## 0.64.0 — 2026-08-26

- CHANGELOG entries rewritten to state what changed and why: 5139 lines to 947. Narrative, process accounts and quoted reports are gone.
- Reference material that only existed as changelog prose moves to `docs/manual.md` — tabs and keys, permission modes, worktrees and finalize, the daemon, status chips, LORE integration, commands and settings. Each claim checked against the source.
- README's "What you get" is short bullets linking into the manual; "How it works" follows it directly. The session walkthrough, permission-mode table and configuration reference move to the manual. 1076 lines to 310.

## 0.63.0 — 2026-08-26

- Relicensed from the DOXA Noncommercial License 1.0 to **AGPL-3.0-only**, dual with a commercial option. The noncommercial terms were not open source by the OSI definition, which blocked distro packaging and deterred contributors; AGPL keeps a fork's source open, including when it is only offered over a network. `LICENSE-COMMERCIAL.md` is the commercial offer, `TRADEMARK.md` reserves the name and mark.
- `Ctrl+T` raised `NameError: name 'spawn_daemon' is not defined` and the tab hung at "connecting…". v0.61.0 deferred that import and three call sites kept the bare name, two of them the closures `Ctrl+T` and the palette use. The suite passed on the broken code because every test reaching those closures patches `cli_mod.spawn_daemon`, which creates the module global a bare name resolves against — the tests were what made the code work. `test_no_call_site_uses_the_bare_lazy_name` checks the source instead.

Docs restructuring: nothing in `doxa/` changed.

- CHANGELOG.md rewritten throughout to state what changed and why, dropping
  narrative, dead ends and direct user quotes — 5139 to 947 lines.
- Added `docs/manual.md`: the tab model and keys, permission modes,
  worktrees and finalize, the daemon, status-bar chips, LORE integration
  and the review gate, commands, and settings — written against current
  source, not transcribed from release notes.
- README's "What you get" section condensed to short bullets linking into
  the manual; the session walkthrough, permission-mode table, configuration
  table and reasoning section moved there too. "How it works" trimmed to
  the shape of the system and moved up beside "What you get".

## 0.62.0 — 2026-08-25

**lore-core moves from v0.36.0 to v0.38.0.** DOXA runs `lore_core`
in-process, so a LORE release changes what this terminal does, not just
what it depends on. Two releases land at once:

- **0.37.0 cuts the deriver's proposal ceiling from 5 to 3** and tells it
  the number is a ceiling rather than a quota. LORE measured 79% of runs
  emitting exactly 5, and those runs were approved at 0.83% against 2.47%
  for runs emitting 4 or fewer. It also adds the act-not-know test and
  suppresses a proposal an existing entry already covers.
- **0.38.0 separates the two user channels by a stated rule.** The user
  said it → `user`, and a later session may act on it. You concluded it →
  `user-model`, and it authorizes nothing. The check is asymmetric on
  purpose: an inference already carried by a stated fact gets dropped, the
  reverse gets kept and reported.

Expect fewer proposals on the `proposals` chip, and expect them to be
better. Nothing in DOXA changed.

## 0.61.0 — 2026-08-25

`import doxa.app` no longer imports the Claude Agent SDK, which was 404 ms
of its 546 ms cost (`mcp.types`'s pydantic models alone were 330 ms).
Import time drops to 168 ms; `doxa.client` drops from 465 ms to 59 ms.
Every launch paid this before the first frame, including `doxa doctor` and
`doxa launcher install`, which never build a `SessionEngine`.

- `doxa/events.py` now holds the event vocabulary (`EngineEvent`, list
  caps, `PROTOCOL_VERSION`) with no SDK dependency; `doxa.engine` and
  `doxa.daemon` re-export it so existing imports still work.
- `doxa.client`, `doxa.session.runtime` and `doxa.session.chips` import
  from `doxa.events` instead of the SDK.
- `doxa.app` imports `SessionEngine` only inside its session factories,
  and `doxa.cli` imports `spawn_daemon` at point of use, both reachable via
  PEP 562 `__getattr__` so existing `monkeypatch.setattr` test patches
  still work.
- `tests/test_import_cost.py` pins both directions in a subprocess: the
  launch modules must not import the SDK, `doxa.daemon` must.

## 0.60.0 — 2026-08-25

Three fixes in the same tab-attach/tab-persistence area, all reported the
same day.

- Ctrl+Q on the last tab of a window wiped the saved tab set to `"tabs":
  []`. `_persist_tabset` excluded every stopped pane, which was correct
  before v0.56.0 pinned `ClaudeAgentOptions.session_id` to DOXA's own id —
  after that, a stopped session's id is resolvable by `--resume` like any
  other. `_persist_tabset` no longer excludes a stopped pane; an explicit
  `/sessions kill <prefix>` is still the one action that permanently drops
  a session from the set.
- `/attach [prefix]` added as the in-app door to a live detached session
  (mirroring the CLI's `doxa attach`), always opening a new tab and
  reusing the sessions-chip's existing picker.
- The sessions-chip's own attach was broken: `_cmd_attach` swapped the
  active pane's engine in place instead of opening a tab, and never set
  `_restore_transcript_wanted`, so a reattached pane's content came from
  the daemon's in-memory ring (capped at 512 frames) instead of disk — a
  session detached long enough came back blank in the tab you were already
  looking at. Fixed by routing `_cmd_attach` through the same
  `_attach_in_new_tab` door `/resume` already uses.
- Ctrl+Q doing nothing on a read-only tab was already fixed by v0.58.0;
  verified still correct, no change needed here.

## 0.59.0 — 2026-08-25

CI was red on two shipped tags while the local suite (1307 tests) was
green on both — both defects were cases where the machine running the
test decided the answer.

- The startup banner could draw wider than its column with no resize ever
  correcting it: it fit itself to the terminal's raw width rather than
  the widget's own content box, and real chrome (e.g. the scrollbar) isn't
  a constant. It now draws its 4-cell name when width is unmeasured and
  retries up to 3 times once Textual has laid it out.
- `doxa launcher install` could crash on a machine without
  `desktop-file-utils`: the cache-refresh guard checked `shutil.which`,
  but the test stubbed `which` to answer for every name rather than just
  `doxa`, hiding that the real exec could still fail. Now suppresses
  `OSError` and a non-zero exit around that refresh.

## 0.58.0 — 2026-08-25

Three branches landed together.

- **Launcher shortcuts pointed at the wrong DOXA.** `Exec=doxa` resolved
  against the desktop session's PATH at click time, unrelated to the shell
  the install command ran from — a shortcut could silently launch a stale,
  separately-installed copy. `Exec` is now an absolute path to the DOXA
  that wrote the entry (`launcher.exec_target()`, anchored on `sys.prefix`
  rather than `sys.executable`, since `uv run` resolves the latter to a
  base interpreter with no `doxa` installed). The install report and
  `doxa doctor`'s new `launcher` check now surface the pinned path and
  version so a moved or removed checkout is diagnosable; a different
  `doxa` found on PATH is named but never touched (its version is read
  from its `.dist-info`, never executed). A missing scalable icon was
  added so small panel slots get a real rendering instead of a downsampled
  smudge.
- **No terminal window title.** Textual offers no API for one. New
  `doxa/window.py` writes an OSC title-stack push/pop (`CSI 22;0t` /
  `23;0t`) around `DoxaApp.run()`, restoring the terminal's own title on
  quit, Ctrl+C, or a crash (not on `SIGKILL`, which no terminal-title
  convention survives anyway). Title is `DOXA — <project>` (never bare
  `DOXA`, so two windows are distinguishable; never the active session,
  since that would move on every tab switch). Guarded off for
  non-terminals, `TERM=dumb`/unset, and `DOXA_NO_TERMINAL_TITLE`.
- **Ctrl+Q did nothing on a read-only tab** (`SubagentTranscriptTab`,
  `ArchivedSessionTab`, the beliefs browser) because `_end_session`
  required `active_pane` to be a `SessionPane`. Extracted
  `_close_read_only_tab()`, reached by both Ctrl+W and Ctrl+Q, since
  neither key draws a real distinction on a tab with no session to
  end or detach.
- **`bypassPermissions` was on the mode cycle but the CLI refuses it**
  unless the session launched with `--allow-dangerously-skip-permissions`
  — the only one of five modes with a launch-time prerequisite, confirmed
  by driving the real CLI through every mode on both an armed and unarmed
  session. Rather than arming every session by default, new setting
  `allow_bypass` / `DOXA_ALLOW_BYPASS` (default off) controls it; an
  unarmed session now omits `bypassPermissions` from the cycle, the
  chip's picker, and `/mode`'s list entirely, since an option a user can
  see must be one that works. Arming cannot be retrofitted onto a running
  session (it is argv); a restored or resumed session clamps to its own
  arming.
- The startup mark is now a ring around a triangle drawn only in `█` and
  spaces (7×13 cells), superseding v0.55.0's quadrant-triangle glyph:
  half-blocks seam at the font baseline, and the quadrant triangles
  (`◢`/`◣`) live in Unicode's Geometric Shapes block, which a font can
  lack even when it covers Block Elements, producing tofu. Some monospace
  fonts show faint horizontal banding on stacked full blocks — a terminal
  rendering artifact, not a bug.

## 0.57.0 — 2026-08-25

Staged LORE proposals get their own status chip and picker: previously
`/pending` was the only way in, and an operator could accumulate 175
unreviewed proposals with no visible signal.

- A `175 proposals` chip sits beside the belief count and memory fill,
  hidden at zero. Its count and its list both derive from one predicate
  (`engine.pending_visible`), after an earlier version undercounted by
  reading a function that silently drops filemap proposals with no
  text/name.
- Cached on the pending directory's mtime: 4.2 ms cold vs. 0.0062 ms warm
  (~670×).
- Proposals group by kind (memory/user, memory/project, filemap, belief,
  skill), since kind determines what a verdict acts on; selecting a row
  opens that proposal's own approve/reject controls rather than acting
  from the list directly.
- Fixed: both the beliefs and proposals pickers' "browse" doors read
  "open the beliefs browser" even though one led to a different tab half
  than the other; each door now names its own destination and opens the
  tab focused on that half (the tab itself renamed from `beliefs` to
  `lore`).
- Fixed: picker rows dropped the year from a timestamp to save a column,
  making the claim column start at a different offset row to row; rows
  now always show `YY-MM-DD HH:MM`, sized against the terminal's actual
  width instead of a hardcoded 72-column floor.

## 0.56.0 — 2026-08-25

Four features landed together.

**Resume.** Sessions can now be resumed after ending, and a restored tab
continues its prior conversation automatically instead of opening
read-only. DOXA previously minted its own session id separate from the
`claude` CLI's own id, so `--resume` against DOXA's id always failed;
`_build_options` now passes `ClaudeAgentOptions.session_id` so the two
agree. `/resume [session-id]` opens the shared picker or resolves a
prefix; a resume always opens a new tab, never takes over the active one,
and a still-running session is attached rather than forked. New setting
`resume_restored` (separate from `restore_tabs`) controls whether restore
resumes automatically; off is byte-identical to pre-0.56.0 read-only
restore. A resume that cannot happen (still running, cwd gone, or
recorded before this release) degrades to today's read-only tab with a
line naming why, never to an error.

**Transcript density.** Enter on a `/search` session header now opens or
resumes that conversation instead of only toggling its fold (folding
stays on `←`/`→`). A turn's tool-calls fold dropped from 15 rows to 4 for
a three-call turn: the bordered-chip chrome and blank separator rows are
gone, replaced by one line per call plus indentation and a brightness
step to separate chips.

**Spinner.** A spinner now runs during reasoning/generating, driven by
the token-delta stream itself rather than a timer — measured no idle-CPU
regression, and floored at 0.1 s between glyph advances so a 700-delta
turn doesn't cause 700 repaints. It trails the turn's output rather than
sitting above it, since the block list scrolls to the end on every event.

**Lore status line.** The boot `lore` line now also shows pending count
and user/project memory-fill percentages, reusing the same cached values
the status chips already read rather than computing new ones; costs one
extra socket round trip, at boot only.

**Failure containment.** Four separate defects — a crash from an image
library's timeout during paint, a needs-input dialog that stopped
answering keys, a silently dropped server-tool result, a memory chip that
drew half of itself — shared one root cause: nothing caught a failure and
showed it. All are now caught at the one place Textual funnels every
exception (`App._handle_exception`), rendered as a red-ruled transcript
block with a collapsed traceback fold, scrubbed of secrets and frame
locals, and logged to `~/.doxa/errors.log` (256 KiB × 2 generations). A
widget that raises while painting is quarantined (`display = False`)
rather than taking down the whole frame. Repeat failures collapse into
one block with a `×N` tally; past 25 repeats the app exits with a report
instead of spinning. An audit found roughly a dozen more places in
`doxa/` that swallow real failures (a suppressed `finalize()` on
stop/detach, a shell reader that presents lost output as no output,
among others) — reported here rather than silently rerouted, since
routing them without saying which would just be the same silence in a
new coat.

## 0.55.0 — 2026-08-25

Fixed a crash on Linux Mint's default terminal (GNOME Terminal/VTE):
`textual-image`'s cell-size probe (`ESC[16t`) times out because VTE never
answers, and the library logged that timeout with a full traceback to
stderr — which in a full-screen TUI overwrites the screen and looks
exactly like a crash. Fixed by silencing that logger, always seeding the
cell-size cache (even on failure) so no later in-render probe can retry
and burn its own timeout mid-paint, and wrapping the image widget's
width/height/render methods so a failure degrades to a `[image: …]` line
instead of raising.

The startup banner switched from a raster PNG to a hand-drawn
block-character mark as the default: half-block rendering (`▀`) averages
a 238-row image down to 6, producing an illegible smear. The drawn
triangle-in-ring mark plus a plain-text wordmark are now the default
(`boot_banner=auto`); the PNG raster is kept only for kitty/sixel
terminals with real pixel graphics. New setting `boot_banner`:
`auto`/`blocks`/`image`/`off`. (This mark was itself superseded by
v0.58.0's simpler ring-and-triangle glyph.)

Two defects surfaced and fixed while chasing this: the banner could be
silently clipped to a fixed 3-row CSS height regardless of content — now
fits to the widget's actual `content_size`; and the banner's crop/flatten
step could raise and take the whole pane's boot down with it — now
inside the same try/except as the rest of `widget_for`.

## 0.50.0 — 2026-08-25

The permission-mode cycler now reaches `auto` and `bypassPermissions`
(previously stopped at 3 of 6 modes), and the status chip matches Claude
Code's own glyphs and colors, read directly out of the installed CLI
binary rather than invented.

- Cycle order: `default → acceptEdits → plan → auto → bypassPermissions →
  default`. `dontAsk` stays off the cycle, reachable only via `/mode
  dontAsk` with confirmation — it was never explicitly requested.
- The chip moved to first position in the status bar, since it must never
  fall off an overflowing row; it's bold and red for the two modes that
  stop asking (`bypassPermissions`, `dontAsk`).
- Entering a mode that stops asking now writes a transcript line, not
  just a chip change — a corner-of-the-screen indicator is easy to miss.
- `/mode auto` and `/mode bypassPermissions` no longer confirm, since a
  dialog in front of a mode a keystroke already reaches can't prevent
  anything; `/mode dontAsk` still confirms.
- The persisted default setting still accepts only the three safest
  modes — cycling into bypass is per-session and never saved, since a
  stored bypass would silently apply to every future session in every
  repository.
- Four now-distinct constants replace what used to be two: `CYCLE_MODES`
  (reachable by keystroke), `GATED_MODES` (confirms), `PERSISTABLE_MODES`
  (storable), `UNASKED_MODES` (chip must warn about).

## 0.48.0 — 2026-08-25

Five changes to the beliefs chip/picker, requested after living with the
v0.46.0 browser.

- Picker rows now show `HH:MM` alongside the date (year dropped only for
  a belief from the current year, to stay inside the picker's 72-column
  floor); tested beliefs sort to the top of their scope group there too,
  matching the browser's existing order.
- Scope groups in the picker fold, with headers showing counts (`project
  (412 beliefs, 3 tested)`) — collapsed by default above the widget's own
  max-height, expanded below it. Filtering ignores fold state, since the
  matcher always scores the whole row set.
- Added real per-belief actions: `confirmed`/`contradicted`/`stale`/
  `retract`, one keystroke each in the browser, or via a per-belief menu
  in the picker (not a bulk button, since `ChipPicker` rows have no space
  for one). Deliberately not "approve/reject" — those are proposal-only
  verbs from v0.46.0; a belief already in the store needs a different
  vocabulary. Recording an outcome matters because 97.6% of a live
  store's active beliefs had never been tested by anything, and every
  confidence figure in the product is calibrated against that ledger.
- Retract requires arming (two actions, since it's destructive and pulls
  the belief from the model's context); recording an outcome is a single
  action, since a later outcome can supersede it. No bulk actions
  anywhere — one belief per call, enforced at the API and the wire
  protocol.
- Both surfaces degrade to read-only, with a banner naming why, when the
  loaded `lore_core` can't record the write honestly — checked by
  measuring actual capability, not a version string, since a Claude Code
  plugin checkout can shadow the pinned package version.

## 0.47.0 — 2026-08-25

Three workstreams landed together: the permission-mode surface below, a
needs-input/server-tool defect fix (see the 0.43.0 entry, which shipped
in this same tree), and two status-line fixes.

- Added a permission-mode chip and Shift+Tab cycle across the three safe
  modes (`default → acceptEdits → plan`). Ctrl+Tab was requested but is
  unsendable on terminals using the legacy key encoding, so Shift+Tab
  (which every terminal sends) is primary; Ctrl+Tab is bound as a
  secondary and `/help` marks it `✗` with a footnote where it fails.
- The cycle cannot reach `bypassPermissions`, `auto`, or `dontAsk` — each
  removes the human from the approval loop. Those are reachable only
  through `/mode <name>`, which requires typing `y` to confirm (not
  Enter, deliberately, since Enter is a reflex key on this dialog) and
  states what stops happening rather than asking "are you sure?".
- The mode syncs across daemon clients (`set_permission_mode` RPC,
  broadcast on change) and rides the hello frame, so a reattaching client
  sees the current mode before painting anything.
- The persisted default setting accepts only the three cycle-safe modes.
  (v0.50.0 later widened the cycle itself to include `auto` and
  `bypassPermissions`.)
- Also fixed: the project memory-fill chip vanished for every
  worktree-based session, since it resolved the project slug from the
  raw cwd instead of the main repo root; and a release codename was
  rendering as a subscription-plan name.

## 0.46.0 — 2026-08-25

Added the beliefs browser: a full-height tab listing every belief and
staged proposal, since the existing dropdown couldn't make hundreds of
proposals reviewable.

- Belief rows show creation date plus LORE's last recorded verdict on
  that belief (`confirmed 2d`, `contradicted 2d`, `stale 40d`) rather
  than time-since-last-referenced, which the first draft used and was
  corrected before shipping: being cited by the model isn't evidence a
  claim is still true, only a recorded outcome is. "Never tested" renders
  as those literal words, not a large age — measured at ~97.6% of a live
  store's active beliefs never tested by anything, so treating it as
  "stale" would assert something false.
- Tested beliefs sort first within their scope group, most-recently-tested
  first; never-tested beliefs form a stable bucket after them.
- Every proposal row shows its computed verdict up front (`add →
  memory/user`, `retract → belief #42`, etc.), derived from the same
  function that actually applies it, so the verdict can never disagree
  with the write.
- Approve/reject added per row (`/pending` had been read-only since
  v0.31.0, pending a LORE security review that concluded with LORE
  0.36.0's write gate and provenance ledger). Approve arms on first
  press, applies on second; reject is a single action — the irreversible
  one costs two deliberate acts. No bulk actions anywhere.
- The browser degrades to read-only, with a banner naming why, when the
  loaded `lore_core` can't record a write honestly — measured by
  capability probe (does `belief_insert`/`memory_add` accept `via=`),
  never inferred from a version string.
- Full claim text and evidence trails load on hover/expand rather than
  up front: evidence is fetched per belief on expand, capped at 40 rows,
  since a store can hold hundreds of beliefs and a trail is unbounded.

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

*(Scoped and written as 0.43.0, landed after 0.44.0; the version number
reflects when the work was scoped, not ship order.)*

A web search appeared to hang, and the permission dialog answering it had
gone completely unresponsive to every key, including Esc — one defect,
not two. The dialog is answered entirely through the prompt's own
key-handling and requires the prompt to hold focus; three ordinary
gestures (clicking an already-active tab, clicking the transcript, a
stray Tab keypress) could move focus elsewhere while the dialog stayed
open with no way back in.

- Opening a blocking dialog now claims focus for that pane's prompt, but
  only when it's the active tab, so a background request doesn't yank
  focus from someone typing in another tab. A net at the app level also
  returns focus to the prompt if it drifts away while a dialog is open on
  the active pane.
- Separately: server-side tool calls (`ServerToolUseBlock`/
  `ServerToolResultBlock` — tools the API runs on the model's behalf, not
  client-side tools like WebSearch) weren't rendered at all; the call
  drew no chip and the result vanished with no error. Both now render
  onto the existing tool-call chip machinery rather than a new event
  type.

## 0.41.0 — 2026-08-25

The startup banner now draws the actual DOXA logo through the existing
terminal-image ladder (kitty/sixel/half-block/text), exercising the
image renderer on every launch instead of leaving it mostly untested.

- Logo width is derived from the terminal's own cell aspect ratio (~41
  columns at a 6-row budget); height comes from the widget, not a
  hardcoded constant.
- Below 56 columns, or in text-only terminals, a hand-drawn
  block-character wordmark ("DOXA") shows instead of the `[image: …]`
  fallback line, since that fallback wasn't built to be a permanent first
  line of every session.
- New setting `boot_banner` (default on) / `DOXA_BOOT_BANNER=0`.
- Fixed along the way: the logo PNG is RGBA with a transparent
  background, and the image library's RGB conversion discarded alpha
  instead of compositing it, producing a white slab on the dark theme —
  now flattened onto the theme background first, and cropped to its
  non-transparent bounding box (15%/26% of the asset's width/height was
  empty margin).
- `/img` with no argument became the terminal-image diagnostic
  (previously a placeholder), reporting measured vs. inferred vs.
  never-asked capability and rendering the logo in every tier the
  terminal can honestly support.
- Pillow became a declared runtime dependency (already present
  transitively via `textual-image`).

## 0.39.0 — 2026-08-25

Terminals using the legacy key encoding cannot send certain key
combinations at all (e.g. `Ctrl+,`, bound to `/settings`) — DOXA now
detects this and says so instead of leaving a documented key silently
dead forever.

- New `doxa/keyboard.py` sends the kitty keyboard protocol's own support
  query (`\x1b[?u`) plus Primary Device Attributes at startup and
  classifies the reply as kitty / legacy / unknown. Silence is never read
  as "legacy" — a terminal that answers nothing might simply not be
  listening (e.g. headless), and that says nothing about its keyboard.
- `/about` gains a `keyboard` row (shown even when "not measured");
  `/doctor`'s keyboard check now actually measures instead of being a
  placeholder, treating legacy as a pass, not a failure; `/help` marks
  unreachable bindings with `✗` and a footnote naming the working
  fallback (the slash-command equivalent).
- No bindings changed — this release only reports what a terminal can
  send. `DOXA_KEYBOARD_PROTOCOL` overrides detection as an env var only
  (no persistent setting), since a saved claim about a terminal can go
  stale.

## 0.38.0 — 2026-08-25

Two tab races, both caused by relying on Textual's own scheduling rather
than explicit user intent.

- Focusing a pane's prompt on mount also activated that tab as a side
  effect (a `TabbedContent` behavior), racing against whatever else was
  deciding the active tab — measured at ~7/40 failure rate on one flaky
  test, and the reason a three-tab restore always landed on the last tab
  regardless of the saved record. Fixed: mount no longer focuses;
  `DoxaApp._focus_tab` is now the single place any explicit user action
  (new tab, tab cycling, palette switch, restore) puts the keyboard into
  a tab. A mouse click on a tab header is the one path with no explicit
  handler and still triggers focus-on-activate.
- A restore could silently forget which tab was active: `_persist_tabset`
  read `TabbedContent.active_pane`, which resolves asynchronously, so a
  save landing in that window wrote `null` for the active id. Fixed: it
  now falls back to the tab it just restored to while activation is
  still pending.

## 0.37.0 — 2026-08-25

A bare clone of this repo couldn't run its own test suite: `lore_core`
(DOXA's memory model) was never a declared dependency, only resolved by
reaching into a separately-installed LORE Claude Code plugin checkout —
41 of 52 test modules failed at collection on a machine without that
plugin.

- `pyproject.toml` now declares `lore-core` as a pinned git dependency
  (LORE shipped its first `pyproject.toml`, as LORE 0.35.1, to make this
  possible).
- A LORE plugin checkout still wins over the pinned package when present
  — both write to the same store, and the plugin is the busier writer —
  but `DOXA_LORE_SOURCE=package` forces the pinned dependency, for
  reproducing a bug against exactly what CI runs.
- `/about` gains a `lore from` row (`plugin` or `package`, with path),
  measured off `lore_core.__file__` rather than restated from the
  precedence rule.
- CI's workaround (checking LORE out alongside DOXA on every leg) was
  removed; two legs now run the true bare-clone case, one leg
  deliberately checks LORE out to exercise the other precedence branch.

## 0.36.0 — 2026-08-25

Two additions: `/context`, a breakdown of what's occupying the context
window, and `!`, a shell-command escape.

- `/context` shows token counts by category (system prompt, tools,
  messages, free space), loaded `CLAUDE.md` files, and per-MCP-tool cost
  — every figure is the CLI's own accounting (`get_context_usage`), never
  estimated; a category with no reported count is omitted, never
  rendered as a guessed zero. One shared cache/call backs both this and
  the existing ctx% chip, so the two can never disagree.
- `!<command>` runs a real shell command in the session's own directory
  with the user's full privileges — no sandbox, no allowlist, no
  confirmation. Deliberately not a slash command (the registry is
  dispatchable by name from things other than a keystroke, e.g. a future
  plugin row) and not a tool (absent from the model-facing tool list, so
  the model can never invoke it). Output never enters the model's context
  and is never persisted to the transcript. Capped at 64 KB output and
  120 s runtime (whole process group killed after); stdin is `/dev/null`.

## 0.35.0 — 2026-08-25

Two items: absolute context-usage numbers, and `/about`.

- The ctx% chip's tooltip now always shows the absolute token count
  (`24,000 of 200,000 tokens used, 176,000 left`); an inline `24k/200k`
  form is available via the new `ctx_absolute` setting (off by default,
  hidden below 100 columns). An unreported context-window limit renders
  as `?` rather than falling back to a guessed 200000.
- `/about`: a modal with version, Python/Textual/SDK versions, LORE
  plugin version and store path, platform, config path, and repo/license
  — every row is measured, omitted (never guessed) when unmeasurable, and
  copyable via `c`.
- Fixed a tooltip lookup bug: the ctx chip's hover hint was keyed against
  its own markup while the lookup matched against markup-stripped text,
  so the hint silently vanished exactly at the colored, highest-alert
  tiers.

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

Restore previously brought back the tab list but not the actual content.
Four defects, each measured before fixing:

- A restored tab reattaching to its still-live daemon replayed only the
  daemon's in-memory ring (512 frames), so a session longer than that
  came back empty. Fixed: restored scrollback now reads from the
  session's own persisted transcript file on disk, which is complete,
  already scrubbed, and outlives the daemon; the ring is now used only to
  skip to the current head, never replayed on top of it.
- A saved session whose daemon had since ended was dropped from the tab
  set entirely rather than shown; it now comes back as a read-only
  `ArchivedSessionTab` (same strip, same order, marked `⏺`) whenever a
  transcript exists on disk.
- With three or more restored tabs, the saved *active* tab lost to
  whichever pane happened to mount last, because every restored pane's
  prompt-focus-on-mount raced for tab activation. Fixed by focusing only
  the one saved-active pane during restore.
- An out-of-band event arriving before a pane finished composing could
  raise inside its event pump and kill that pump silently for the rest of
  the tab's life; a status-bar repaint failure there can no longer end
  the pump.
- Restore now renders capped content (40 turns, 20,000 characters per
  turn, 30 tool chips per turn) with an explicit "not shown" note, never
  silently truncated as if complete.
- Splits are not restored, because DOXA has no split-pane layout yet; the
  persisted record format now reserves a `layout` slot for one.

## 0.31.0 — 2026-08-24

Staged LORE proposals from the background reviewer had no reliable
notification path.

- Added `notify_staged` (default on): fires only while the DOXA window is
  unfocused, tints the owning tab a steady muted violet — not a blink,
  since nothing is blocked or expiring. Also silences the LORE plugin's
  own duplicate notifier while this is on, to avoid two banners for one
  event.
- The notification block now shows the actual staged proposal texts
  (diffed against the pre-review pending list, so it shows what *this*
  review added), capped at 8 rows / 160 characters / 8 KB.
- Added `/pending` (previously the only hint pointed at
  `/lore:pending`, a Claude Code plugin command that doesn't exist inside
  DOXA): a read-only list/preview. No approve/reject yet — the write path
  was still under security review at this point (it landed in LORE
  0.36.0 / DOXA 0.46.0).

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

Three operator-reported defects in the previous release's status-bar chip
work.

- Confirm dialogs (ctx% compact, Ctrl+Q-with-turn-running) had invisible
  buttons and dead Enter: `height: 1; padding-top: 1` under Textual's
  border-box model rendered the button row at zero content height. Fixed
  to `height: auto`; Enter now takes the action the dialog was opened to
  confirm, and each button labels its own key.
- Clicking the beliefs chip errored ("too much for a message") instead of
  opening its dropdown: a detached session's belief list crossed the
  daemon socket as one oversized frame (500 beliefs measured at 230 KB,
  3.6× the 64 KB cap) and got replaced wholesale with an error. Fixed by
  paging the `beliefs` RPC (100 rows/frame) and having the client loop
  until exhausted — paged at the transport, not lazily by scroll
  position, since the picker's filter matches across the whole row set
  and a partially-loaded list would make the filter lie.
- Picking a branch from the picker appeared to do nothing: the status bar
  showed the checked-out branch, but the picker changes the session's
  *base* branch, which in a worktree-per-session setup is a different
  string — the switch was actually succeeding. Fixed by having the
  status-bar segment show the base (what it's a selector for), moving the
  checked-out branch into its tooltip.

## 0.27.0 — 2026-08-24

Five status-bar chip revisions in one release:

- The ctx% chip now confirms before compacting (previously one click sent
  `/compact` immediately, with no undo).
- The session-handle chip opens a sessions picker (live + detached,
  current marked) instead of only copying to clipboard; copying moved to
  the picker's first row.
- The beliefs chip is now clickable, opening a scope-grouped, filterable
  picker (`user`, `user model`, `project`) — a lightweight viewer only,
  not the full beliefs browser (that landed later, in v0.46.0).
- The repo-name chip becomes a directory-walking picker: selecting a
  plain directory descends, selecting a git repo root opens it in a new
  tab.
- Every chip, including inert ones (cost, sha, headroom), gained a
  tooltip explaining what its number means.

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

Audited DOXA's cost figures against real API usage before building
further on them: `total_cost_usd` (server-computed) was confirmed
authoritative and kept as-is, matching hand-priced arithmetic on the
unisolated engine path but running ~32–34% higher than hand-priced
arithmetic on the isolated path — attributed to the published cache-read
discount being an approximation, not a bug in what the SDK reports.
Confirmed isolation (v0.10.0's CLI sandboxing) costs a negligible ~0.09%
in extra prompt tokens, not a saving. Fixed: the per-turn cost line in
each turn's title showed a bare dollar figure unconditionally, even on
subscription auth where the account pays nothing — now shows `≈$X if
API` on subscription auth, matching the status bar and `/usage`. Added an
`effort:<level>` status chip (connect-time value only, hidden when no
level was set).

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
