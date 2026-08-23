<p align="center"><img src="assets/logo.png" width="560" alt="DOXA — belief earning knowledge"></p>

**DOXA** is a terminal for working with a Claude agent whose memory you can
audit — the native TUI home for [LORE](https://github.com/docwilde/LORE)'s
memory model, built on the Claude Agent SDK and Textual, billed through your
Claude subscription rather than an API key.

δόξα (*dóxa*): belief, opinion — as distinct from ἐπιστήμη (*epistēmē*),
justified knowledge. Everything the agent derives starts as belief:
visible, queryable, cite-only. Only what a human approves, or what earns a
track record, steers the next answer. LORE holds the beliefs; DOXA is where
you watch them form.

<p align="center"><img src="assets/shots/hero.png" width="780" alt="DOXA shell: three tabs (Opus, Sonnet, Haiku, all on doxa:main), a turn asking what the repo believes about deploys, a lore_belief_search tool chip, and a status bar showing the git sha, subscription headroom, context percentage, belief count, session handle and peer count"></p>

*Headless-rendered from the real Textual app (a scripted session, no
spend, fake account numbers). A session is a detachable daemon: close the
terminal, `doxa attach` later, and the transcript resumes where it left
off. Every screenshot below is generated the same way by
[`scripts/screenshot.py`](scripts/screenshot.py).*

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/docwilde/doxa/main/scripts/install.sh | sh
```

Checks for Python 3.11+, [`uv`](https://docs.astral.sh/uv/) (offers to
install it if missing), `git`, and the
[`claude` CLI](https://docs.claude.com/en/docs/claude-code) signed in
(`claude auth login`) — DOXA authenticates through that CLI's own OAuth
session and never reads `ANTHROPIC_API_KEY` — then installs with `uv tool
install git+https://github.com/docwilde/doxa` (never PyPI; DOXA isn't
published there). Re-running it is safe: it never touches an existing
`~/.doxa/config.toml`, and picks up whatever changed on `main` since the
last run. Install a specific tag instead of `main`'s HEAD with
`sh -s -- v0.5.0`.

Piping a stranger's script into `sh` deserves a second look first:

```sh
curl -fsSL https://raw.githubusercontent.com/docwilde/doxa/main/scripts/install.sh -o install.sh && less install.sh   # then: sh install.sh
```

Or clone-and-run from a source checkout instead of installing at all:

```sh
git clone https://github.com/docwilde/doxa && cd doxa
uv sync
uv run doxa
```

## Quickstart

```sh
uv run doxa          # spawn a session here, or attach this repo's most recent one
uv run doxa new      # force a fresh session instead of attaching
uv run doxa attach   # reattach by session id / title prefix
uv run doxa stop     # finalize now (LORE review + index), daemon exits
uv run doxa doctor   # read-only health checks, no TUI: pass/fail + fix per check
```

`uv run doxa` spawns a session **daemon** — a process of its own — and
attaches the TUI to it as a thin client over a Unix socket. Closing the TUI
(`ctrl+q`, or the palette's "Quit: detach") leaves the daemon running with
no tmux involved; running `doxa` again in the same repo reattaches to it.
The daemon finalizes the session (LORE's review + index pass) once every
client has been detached for `--linger` seconds (120 by default), or
immediately on `doxa stop`. `doxa --in-process` runs the engine inside the
TUI instead, with no daemon and no detach — quitting finalizes on the spot.

Once you're in: type a prompt, press enter. `ctrl+p` opens the command
palette, `ctrl+t` opens a new tab, `/help` lists every command and key
binding.

## Features

**LORE, in-process.** The memory system is imported as `lore_core`, not
shelled out to: hard-capped curated memory (user + project), an uncapped
belief store with an FTS index and evidence trails, the same review gate a
human clears before anything is written. It is the same SQLite database
the LORE Claude Code plugin uses — one store, two carriers — so switching
between the plugin and DOXA never forks your memory. A tool call against
that store renders as an ordinary foldable chip; opened up, it shows the
exact arguments and the exact result text — here, one calibrated belief
(`STEER`, with its outcome count) and one still cite-only:

<p align="center"><img src="assets/shots/memory.png" width="780" alt="A lore_belief_search tool chip expanded, showing its JSON arguments and a result listing one STEER belief with an outcome count and one CITE-only belief"></p>

**Tabs.** `SessionPane` widgets mount under a `TabbedContent`, one engine
client per tab. `ctrl+t` opens a fresh session in a new tab (same repo
scope); `ctrl+w` closes a tab and detaches its daemon (the session keeps
running); `ctrl+←`/`ctrl+→` cycle tabs. Outside a git repo, or once a
custom name is cleared, a tab names itself from its first turn with one
cheap Haiku call, cached in `~/.doxa/names.toml` so a session is never
renamed twice. Double-clicking a tab header (or `/rename`) opens an inline
editor in the tab strip itself; Enter commits, Esc cancels, an empty name
restores the automatic label:

<p align="center"><img src="assets/shots/rename.png" width="780" alt="Three tabs in the tab strip; the second tab has become an inline text editor reading 'kg-stats refi', mid-rename"></p>

**Command palette and `/` autocomplete.** `ctrl+p` opens a palette listing
new-tab, the open tabs (in tab-bar order, active one marked), every
registered command grouped (Session · Memory · Panes & tabs · Tools &
config · Maintenance), then live sessions available to attach. Typing `/`
at the start of the prompt opens the same list as a dropdown above the
input. Both read one registry (`doxa/commands.py`); a command cannot exist
on one surface and not the other.

<p align="center"><img src="assets/shots/palette.png" width="780" alt="The Ctrl+P command palette open: a New tab entry, two open tabs with the active one marked, then grouped commands under dim section headers"></p>

**`/search`.** Full-text search over LORE's session index, live in a popup
the moment you type `/search `. Debounced and sequence-guarded, so a slow
query can never repaint over a newer one's results; an empty query lists
recent sessions. This is the one search path — `ctrl+r` opens it too. The
matched terms are FTS5's own `snippet()` output, highlighted rather than
re-matched:

<p align="center"><img src="assets/shots/search.png" width="780" alt="The /search popup open over the prompt after typing '/search deploy checklist', showing three result rows with the matched words highlighted in each snippet"></p>

**Trace tree.** A subagent spawned by the `Task` tool streams its own text
and tool calls, which nest as a foldable tree under the parent chip rather
than interleaving with the main thread. Formatting happens lazily, only
once a chip is opened, and subagent text passes the same secret-scrubber
as everything else before it reaches a block:

<p align="center"><img src="assets/shots/trace.png" width="780" alt="A Task tool chip expanded, showing its own arguments and result plus a SUBAGENT narration line and a nested Grep tool chip inside it"></p>

**Terminal images.** A detection ladder — Kitty graphics protocol → sixel →
half-block cells → a plain `[image: ...]` text line — so a tool result or
`/img <path>` degrades gracefully on any terminal instead of failing on
one that doesn't support graphics.

**Peer sessions and `/sessions`.** Independently launched DOXA sessions on
the same repo discover each other through a same-user runtime registry:
the status bar's `peers N (k⌁)` chip counts live peers and how many are
detached, `/peers` lists them, `/msg <session> <text>` sends a message
over the target's own socket. Received peer text renders in its own
dimmed block and reaches the model, if at all, behind an explicit
untrusted-peer preamble — data to weigh, never an instruction to follow.
`/sessions` lists every live session with its age and whether it's
attached here or running detached, with a kill command for either:

<p align="center"><img src="assets/shots/sessions.png" width="780" alt="/sessions output listing three sessions: one attached here, two detached, each with an age, plus the kill commands and the peers chip in the status bar"></p>

**Settings.** `ctrl+,` opens a modal grouped into category tabs (Session ·
Memory · Appearance · Paths · About). Every row shows its effective value
next to where it came from — session, config file, or default — so a
value the environment is shadowing is never mistaken for one the modal can
edit:

<p align="center"><img src="assets/shots/settings.png" width="620" alt="The settings modal, Session category, showing the model row as 'claude-opus-4-5 (session)' and effort/linger_secs rows marked '(default)'"></p>

**`/setup`.** Checks state and fixes findings one at a time, each behind
its own confirmation showing exactly what applying it will change: auth
state (surfaced only — `/login` is what actually signs in), the LORE
store (env wins outright; a choice a previous run already made is
remembered; an existing store the Claude Code plugin uses is the one case
that asks, rather than silently picking a side), `/migrate` when a later
DOXA version ships one, then model/effort defaults, handing off to the
settings modal to set them. Auto-runs once, on a genuine first launch on
this machine; `/setup` any time after runs it again on demand.

**`/doctor` / `doxa doctor`.** Read-only health checks, pass/fail plus the
exact fix command for anything failing: python and DOXA versions, the
`claude` CLI's version and auth state, the LORE store's location and
active belief count, whether `config.toml` parses, live daemon count and
stale presence files (report only — `/doctor` never deletes what it
counts; a normal launch's sweep does that), the detected terminal image
protocol, and MCP reachability (nothing configured yet, so nothing to
check). Keyboard-enhancement grant is reported `?` rather than guessed —
Textual requests it at session start but doesn't yet expose whether the
terminal actually honored it. `doxa doctor` runs the same checks with no
TUI at all (what `scripts/install.sh` runs at the end of a fresh install)
and exits 1 if anything failed; `/setup` runs it too, as its last step.

**Identity and auth.** The session-start block and status line report the
plan you actually have — DOXA prefers the precise tier the `claude` CLI
keeps locally over the SDK's coarser `subscriptionType` string, and shows
nothing rather than a guess when neither is available. `/login [provider]`
and `/logout [provider]` suspend the TUI and exec the provider's own
interactive auth CLI; DOXA never handles or stores a credential itself.

**No animated chrome.** The in-flight turn marker is a static `⋯ thinking`,
not a spinner, and there are exactly two timers anywhere in the app —
Textual's own 2 Hz caret blink on the focused prompt, and the clock below,
which only exists at all while it's switched on. A test asserts no other
timer is ever armed, with every overlay open.

**Clock.** Fixed-width, right edge of the tab bar, on its own compositing
layer so it paints over that corner without ever displacing a tab or
narrowing a pane. Configurable: 12/24-hour, a date prefix, seconds, an
IANA timezone, or a full custom `strftime` (validated on save; an
unresolvable timezone or a format that stops producing text falls back to
the built-in format and says so, as the chip's tooltip, rather than
either crashing or going silently wrong). Its one timer is boundary-
aligned — it wakes at the next minute edge with seconds hidden, the next
second edge with them shown — never a fixed-Hz repaint of a string that
usually hasn't changed:

<p align="center"><img src="assets/shots/clock.png" width="780" alt="The upper-right clock reading '2026-08-24 14:32:07' at the far right of the tab bar, past two open tabs, never overlapping either label"></p>

**Status bar**, left to right: model · `repo ⎇ branch @sha` · subscription
headroom (`s:9% w:48%`, session/week) or a `$` cost estimate on API-key
auth · context-window percentage (escalates normal → amber ≥70% → red
≥90%, percentage always shown) · belief count · `⌁ session <id>` reattach
handle · peers.

## Configuration

Precedence is the same everywhere: **environment > `~/.doxa/config.toml` >
default.** The file is plain TOML, 0600, and safe to hand-edit; it's also
what the settings modal (`ctrl+,` / `/settings`) writes. Clearing a field
in the modal removes that key, which returns the setting to its default.
Unrecognized keys are preserved on save, so a file written by a newer DOXA
survives being opened by an older one.

| setting | env | default | what it controls |
|---|---|---|---|
| `model` | `DOXA_MODEL` | CLI default | model for new turns (`/model` also switches live) |
| `effort` | `DOXA_EFFORT` | CLI default | reasoning effort, new sessions only (connect-time SDK option) |
| `derive_secs` | `DOXA_DERIVE_SECS` | off | streaming-deriver interval; unset runs review only at session end |
| `linger_secs` | `DOXA_LINGER_SECS` | 120 | seconds a daemon outlives its last detached client |
| `consult_floor` | `DOXA_CONSULT_FLOOR` | 1.0 | act-time belief-consult threshold; 0 disables it |
| `nerd_font` | `DOXA_NERD_FONT` | off | use a Nerd Font glyph for the branch chip |
| `image_mode` | `DOXA_IMAGE_MODE` | probe | force a rung of the image ladder (`kgp`/`sixel`/`halfblock`/`text`) |
| `clock_show` | `DOXA_CLOCK_SHOW` | **on** | show the upper-right clock (the one bool here that defaults on — `0` turns it off) |
| `clock_date` | `DOXA_CLOCK_DATE` | off | prefix the clock with `%Y-%m-%d` |
| `clock_hour` | `DOXA_CLOCK_HOUR` | `24` | `12` or `24`-hour |
| `clock_seconds` | `DOXA_CLOCK_SECONDS` | off | show `:SS`; also re-aligns the clock's timer to the second instead of the minute |
| `clock_tz` | `DOXA_CLOCK_TZ` | system | IANA zone name, e.g. `Europe/Berlin`; unresolvable falls back to system local, visibly |
| `clock_format` | `DOXA_CLOCK_FORMAT` | (none) | custom `strftime`, overrides the toggles above; validated on save |
| *lore store* | `LORE_ROOT` | `~/.claude/lore` | `lore_core`'s own store path; `lore_root` in the file is `/setup`'s sticky choice, not the modal's to edit |

The settings modal shows every row's **effective** value next to where it
came from (`900 (config)`, `120 (default)`, `1 (env DOXA_NERD_FONT)`); a
row the environment is winning is read-only, because an edit that a live
environment variable would immediately shadow is a silent no-op.

`~/.doxa/` holds durable state (this config); the runtime directory
(`$DOXA_RUNTIME_DIR` → `$XDG_RUNTIME_DIR/doxa` → `~/.local/share/doxa`)
holds ephemeral daemon sockets and the peer registry — kept out of the
home directory because a home directory can be network-mounted, where Unix
sockets misbehave. The LORE store is neither: it stays `lore_core`'s own
path, shared with the LORE Claude Code plugin on purpose, so switching
between the two never forks your memory into two divergent halves.

## How it works

Each session runs as its own **daemon process** hosting the Claude Agent
SDK client, the LORE hooks, and the transcript; the TUI is a thin client
attached over a 0600 Unix socket, so detaching and reattaching never
depends on the terminal that started the session staying open. Every tool
call the agent makes passes a containment gate at the PreToolUse
boundary — a call outside the declared tool registry is denied, and a tool
that fails twice in a session is disabled for the rest of it rather than
retried into the step budget.

LORE's memory model is summarized above; the full model — curated memory,
the belief store, the derive/dream/dialectic split, calibration — is
documented in the [LORE repository](https://github.com/docwilde/LORE),
which DOXA embeds as `lore_core` rather than reimplementing.

`lore_core` currently ships inside the LORE Claude Code plugin's
marketplace checkout, not as an installable package, so DOXA locates it
with a `sys.path` shim (`doxa/_lore_bootstrap.py`) documented there as
temporary. Override the location with `DOXA_LORE_CORE_PATH` if your
checkout isn't at the default `~/.claude/plugins/marketplaces/lore`.

## Status

DOXA is a working daily driver for its author, not a finished product.
Shipped so far: the daemon/detach model, tabs, the command palette,
`/search`, the trace tree, the image ladder, peer discovery, the settings
modal described above, the `curl | sh` installer, `/setup`,
`/doctor` / `doxa doctor`, and the clock — see [CHANGELOG.md](CHANGELOG.md) for the
version-by-version history. Not yet built: session-history drill-in past
`/search`'s result list, customizable keybindings, and a graphical
context-window map. Interfaces (config keys, socket protocol, command
names) can still change between minor versions.

Run the test suite with `uv run pytest`.

## Non-goals

Provider-agnostic model routing — the point is subscription auth, not a
router. Replacing the LORE Claude Code plugin, which keeps shipping the
same core. General Claude Code plugin compatibility — DOXA does not load
third-party plugins today.

## License

[DOXA Noncommercial License 1.0](LICENSE) (PolyForm-Noncommercial-derived)
— free for personal use, research, education, and noncommercial
organizations; commercial use requires a separate arrangement with the
author. Same license family as [LORE](https://github.com/docwilde/LORE),
whose `lore_core` DOXA embeds.
