# Changelog

Newest first. Versions are annotated git tags on the commit that shipped
them (`v0.1.0` … `v0.10.0`); the ranges below are derived from that history,
not written from memory.

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
