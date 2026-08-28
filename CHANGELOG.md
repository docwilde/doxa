# Changelog

Newest first. Versions are annotated git tags on the commit that shipped
them (`v0.1.0` … `v0.15.0`); the ranges below are derived from that history,
not written from memory.

## 0.84.0 — 2026-08-28
- `lore-core` moves 0.42.1 → 0.45.0: 0.43.0 (mermaid/browser graph view),
  0.44.0 (graph-backed context), 0.45.0 (learned-skills tier). DOXA does not
  surface the mermaid view or the skills tier in this release.
- **`lore_belief_show` gains its edges**: the belief's typed relations
  (`lore_core.beliefs.belief_edges`), each carrying verb, direction, the
  other belief's id/claim/status, who asserted it (`source`), and the
  distinct-session `support` count. `"edges": []` for a belief with none —
  an absent block would be indistinguishable from a query that failed.
- New operator **`lore_belief_neighbours`**: one traversal tool, not five.
  `belief_id` + `hops` (≤2) returns the k-hop neighbourhood; adding `to_id`
  switches to the single most-confident path between two beliefs
  (`lore_core.graph.khop`/`best_path`). Capped at `BELIEF_NEIGHBOUR_LIMIT`
  (20, `doxa/events.py`), truncating visibly rather than silently.
- **Structure earns no authority**: every belief either tool returns —
  seed, neighbour, or a node on a path — carries its OWN `citation_status`
  (`steer`/`cite_only`), computed independently the same way
  `lore_core.dialectic.cmd_consult` splits STEER from CITE ONLY (≥3
  outcome-ledger rows). A STEER belief never lends its status to a
  CITE-only neighbour, or the reverse.
- **Path confidence is the product over hops** (`best_path`'s own
  contract — Dijkstra on `-log(weight)`), surfaced next to `hop_count` on
  every result so a long chain reads as weak, never as strong as its best
  single hop.
- `co_derived` relations (projected from `belief_evidence` at read time,
  never a stored row) are labeled `"projected": true` wherever they appear
  — never presented as an asserted relation.
- **Graph-backed act-time context** (new setting `graph_context`,
  `DOXA_GRAPH_CONTEXT`, OFF by default): `SessionEngine._graph_context_block`
  calls LORE's OWN builder (`lore_core.graph.context_candidates`/
  `render_context_block`) rather than a second ranking implementation, as a
  stage SEPARATE from and independent of the plain-FTS consult note
  (`_consult_note`/`consult_floor`) — the two toggle independently. Gated
  the same two ways LORE's own `LORE_GRAPH_CONTEXT` hook is:
  `graph_context_enabled()` (DOXA's opt-in) AND LORE's own
  `stage_disabled("beliefs")`.
- `/context` reports the last-injected block's size as `graph_context_chars`
  (`doxa.engine.context_breakdown`, `doxa/ui/labels.py`) — unlike
  `lore_snapshot_chars`/`worktree_notice_chars`, which are connect-time,
  system-prompt figures the CLI cannot separate out of its own accounting,
  this rides the per-turn `additionalContext` path (already correctly
  counted by the CLI on the next turn) and is reported as the last known
  size rather than a session constant.
- Decided against a bespoke one-hop follow on `_consult_note` itself: LORE
  0.44.0/0.45.0 already ships the ranked, budgeted, calibrated-first
  version of that idea, and a second implementation reading the same store
  would only be able to drift from it.
- Full suite: 1446 → (see this release's own count).

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
