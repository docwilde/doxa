# Changelog

Newest first. Versions are annotated git tags on the commit that shipped
them (`v0.1.0` … `v0.15.0`); the ranges below are derived from that history,
not written from memory.

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
