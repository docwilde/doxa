# Changelog

Newest first. Versions are annotated git tags on the commit that shipped
them (`v0.1.0` … `v0.15.0`); the ranges below are derived from that history,
not written from memory.

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
