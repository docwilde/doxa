<p align="center"><img src="assets/logo.png" width="560" alt="DOXA — belief earning knowledge"></p>

<p align="center">
  <a href="https://github.com/docwilde/doxa/releases"><img src="https://img.shields.io/github/v/release/docwilde/doxa?label=release&color=e8590c" alt="latest release"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/built%20on-Claude%20Agent%20SDK-d97757" alt="built on Claude Agent SDK">
  <img src="https://img.shields.io/badge/TUI-Textual-0b1120" alt="Textual TUI">
  <img src="https://img.shields.io/badge/subscription-no%20API%20key%20needed-2f9e44" alt="billed via Claude subscription">
  <img src="https://img.shields.io/badge/Linux%20%C2%B7%20macOS-terminal-555" alt="Linux and macOS terminals">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Noncommercial%201.0-8a8073" alt="license"></a>
</p>

**DOXA** is a terminal for Claude agents whose sessions outlive your
window and whose memory of your own codebases you can actually watch
form — built on the Claude Agent SDK and Textual, billed through your
Claude subscription rather than an API key.

A session runs in a **daemon** of its own: close the terminal, walk away,
`doxa attach` an hour later, and the transcript picks up exactly where it
left off — nothing lost, no tmux involved.

Start it inside a repository and the session already knows the project.
Durable facts about this codebase — its conventions, its past
workarounds, corrections a human already made once — are injected before
your first prompt, alongside a per-project file map (which files matter
and why) the agent consults instead of grepping blind. That context is
per-repo, not global: a tab open in this project and a tab open in
another carry different project memory, on purpose. Procedures that
worked before — captured as reusable runbooks, approved by you — are
available to the agent here too, not just in the session that learned
them.

Every durable conclusion the agent reaches about you or your code goes
through the same gate before it can shape a later answer: it starts as a
**belief** — visible, queryable, citable but never acted on — and only
gains real influence by being approved by a human or by building an
actual track record of being right. Nothing writes itself into the
model's context unsupervised; the running count sits in the status bar,
and `/search` reaches every past session, not just the current one. This
is what the tagline means literally, not as a slogan: *where belief earns
knowledge* — an idea has to earn its way from opinion to something the
agent will actually rely on.

The memory engine underneath all of this is
[LORE](https://github.com/docwilde/LORE), which also ships as a
standalone Claude Code plugin; DOXA compiles it in-process
(`lore_core`, imported, not shelled out to) rather than requiring the
plugin to be installed — one memory model, two front ends.

δόξα (*dóxa*): belief, opinion — as distinct from ἐπιστήμη (*epistēmē*),
justified knowledge. The name is the thesis: belief is the raw material,
never the finished thing.

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
uv run doxa new --branch <name>   # fork the session's worktree from <name>, not the launch checkout
uv run doxa attach   # reattach by session id / title prefix
uv run doxa stop     # finalize now (LORE review + index), daemon exits
uv run doxa doctor   # read-only health checks, no TUI: pass/fail + fix per check
```

`uv run doxa` spawns a session **daemon** — a process of its own — and
attaches the TUI to it as a thin client over a Unix socket. Closing the TUI
(`ctrl+q`, or the palette's "Quit: detach") leaves the daemon running with
no tmux involved; running `doxa` again in the same repo restores the WHOLE
tab set you left (order, pinned names, which tab was active — `restore_tabs`,
**on** by default), reattaching every session still alive and reporting how
many it skipped, rather than just the single most recent one. A saved
session that finalized in the meantime (`doxa stop`, or its own `--linger`
expiring) is skipped silently — restore never spawns a replacement for a
tab that's actually gone — and shows up in that report instead
(`tab restore: restored 2 tabs, skipped 1 session no longer running.`); a
tab you closed with `ctrl+w` stays in the set (it only detached, it's
still running), but one you explicitly stopped does not. `doxa new` always
starts exactly one fresh tab and never restores; `doxa attach <prefix>`
stays the single-session path either way; `$DOXA_RESTORE_TABS=0` turns the
whole thing off and returns to attaching only the single most recent
session. The daemon finalizes the session (LORE's review + index pass)
once every client has been detached for `--linger` seconds (120 by
default), or immediately on `doxa stop`. `doxa --in-process` runs the
engine inside the TUI instead, with no daemon and no detach — quitting
finalizes on the spot.

Inside a git repo, each session also gets its own **git worktree** by
default (`worktree_per_session`, `ctrl+,` or `$DOXA_WORKTREE=0` to turn it
off) — `git worktree add ~/.doxa/worktrees/<repo>-<id> -b doxa/<id>` off
the branch you were on, so two sessions on the same repo, even the same
branch, never stomp each other's edits (git itself refuses the same branch
checked out twice, which is exactly the constraint two sessions sharing one
checkout would otherwise hit). The status bar's git chip shows the
worktree's own session branch (`doxa/<id>`, plus a short sha); the tab
shows what you're actually *working off* — the base branch it forked from
(`Opus@doxa:main`) rather than that session handle — so a glance at the
tab bar answers "what am I based on", not "which throwaway branch is
this". `/peers` and `/sessions` still group every worktree of a repo as
one project. A session that ends cleanly (no uncommitted changes, nothing
committed beyond its base) leaves no trace; anything you actually wrote is
kept, never auto-merged — the closing message names the branch to merge
by hand.

The base is explicit, not just inherited. `doxa new --branch <name>`
forks the session's worktree from `<name>` instead of whatever the launch
directory has checked out (fails with an actionable message if `<name>`
doesn't resolve); with `worktree_per_session` off, `--branch` refuses by
default rather than silently moving your real checkout — pass `--checkout`
on a clean tree to allow that explicitly. Mid-session, `/branch` lists
local branches with the current base marked, and `/branch <name>` switches
it: free (a fast-forward rebase, no history to replay) when the session's
worktree is clean and carries no commits of its own yet, refused — same
voice as the "kept `doxa/<id>` — merge when ready" message above — the
moment there's real work switching a base would silently carry across.

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

**Markdown responses.** The agent's reply streams as real markdown, not
literal text — tables, bold, fences, inline code — via `Markdown.get_stream`
(Textual 5's append-only path built for LLM deltas), so a table or a bold
span renders as it completes rather than waiting on the whole message.
Chunk boundaries split mid-row and mid-span exactly like a real model
stream does; the table below fills in row by row as the deltas that
complete it arrive, then closes with a bold total and an inline-code tool
name:

<p align="center"><img src="assets/shots/markdown-stream.gif" width="780" alt="An agent reply streaming: prose appears first, then a three-row table fills in one row at a time, ending on a bold 'Total' line with an inline-code tool name"></p>

**Tabs.** `SessionPane` widgets mount under a `TabbedContent`, one engine
client per tab. `ctrl+t` opens a fresh session in a new tab (same repo
scope); `ctrl+w` closes a tab and detaches its daemon (the session keeps
running); `ctrl+←`/`ctrl+→` cycle tabs. A tab not currently in view still
reports what it's doing by color: amber while a turn is running on it,
green the moment that turn finishes unseen, clearing the instant you look
— so a background tab never goes silently forgotten, and never demands a
popup to say so either. The whole set is remembered across restarts too
(`restore_tabs`, see Quickstart above): quit and run `doxa` again in the
same repo and every tab that's still running comes back, in order, named
the way you left it:

<p align="center"><img src="assets/shots/tab-lifecycle.gif" width="780" alt="A second tab starts a turn and turns amber (-working); switching to the first tab leaves it amber in the background; the turn finishes there and the tab turns green (-done-unseen); switching back clears it"></p>

Outside a git repo, or once a custom name is cleared, a tab names itself
from its first turn with one cheap Haiku call, cached in
`~/.doxa/names.toml` so a session is never renamed twice. Double-clicking a
tab header (or `/rename`) opens an inline editor in the tab strip itself;
Enter commits, Esc cancels, an empty name restores the automatic label:

<p align="center"><img src="assets/shots/rename.gif" width="780" alt="Double-clicking the second tab opens an inline editor seeded with its old label; typing 'kg-stats refi' and pressing Enter commits the new name to the tab strip"></p>

**Interactive permission.** A `can_use_tool` callback closes the gap a
headless SDK run otherwise has: without one, anything the `claude` CLI
would normally show its own interactive UI for — an `AskUserQuestion`
call, a permission prompt for a tool call it isn't sure about — gets
silently auto-denied. DOXA's own `PreToolUse` gate stays the containment
layer (its allow/deny decisions are unchanged); this callback handles
only the two genuinely interactive cases, surfacing a dialog above the
prompt — question and options for `AskUserQuestion` (number keys 1-9,
up/down + Enter, Esc to decline), tool name and input summary with
Allow/Deny for a permission request. While one is open the tab blinks
red and the status bar reads `⚑ needs input`, both clearing the moment
you answer (or the instant you look at the tab, for the blink alone —
the dialog itself waits for an actual answer):

<p align="center"><img src="assets/shots/needs-input.gif" width="780" alt="An AskUserQuestion dialog opens above the prompt asking which environment a migration should target; arrowing down highlights 'production'; Enter answers it and the dialog and tab blink both clear"></p>

A session you are not watching still tells you: a desktop notification
(`notify-send`, `notify_needs_input`, gated like every other trigger
below) fires whenever the window is unfocused, and a session with no
attached client at all — closed the TUI, left the daemon running —
notifies unconditionally rather than blinking a tab nobody can see, and
parks the question for whenever `doxa attach` picks it back up:

<p align="center"><img src="assets/shots/attention-blink.gif" width="780" alt="A tab alternates every 0.5s between its normal color and a solid red -attention state while a question is pending"></p>

**Command palette and `/` autocomplete.** `ctrl+p` opens a palette listing
new-tab, the open tabs (in tab-bar order, active one marked), every
registered command grouped (Session · Memory · Panes & tabs · Tools &
config · Maintenance), then live sessions available to attach. Typing `/`
at the start of the prompt opens the same list as a dropdown above the
input. Both read one registry (`doxa/commands.py`); a command cannot exist
on one surface and not the other.

<p align="center"><img src="assets/shots/palette.gif" width="780" alt="Ctrl+P opens the command palette on New tab; arrowing down moves the highlight through the open tabs and grouped commands; Esc closes it and returns focus to the prompt"></p>

**`/search`.** Full-text search over LORE's session index, live in a popup
the moment you type `/search `. Debounced and sequence-guarded, so a slow
query can never repaint over a newer one's results; an empty query lists
recent sessions. This is the one search path — `ctrl+r` opens it too. The
matched terms are FTS5's own `snippet()` output, highlighted rather than
re-matched. A result set spanning more than one session groups into a
tree — a collapsed session header (title, date, hit count) over its
matching snippets; a single-session result set has nothing to fold
against and stays flat. `↑`/`↓` move through the visible rows, `→`/`←`
open and close a session's fold (or, from a snippet, close its parent),
and `enter` toggles a header — the same convention the trace tree's own
folds use — or, on a snippet, inserts its excerpt into the prompt: one
citation line (which session, when) plus the text, collapsed to a
`⧉ pasted …` placeholder past the same size threshold a clipboard paste
uses, `ctrl+g` expandable, sent in full on submit either way:

<p align="center"><img src="assets/shots/search.gif" width="780" alt="Typing '/search deploy' opens the popup on one session, flat; completing the query to '/search deploy checklist' brings up three sessions, collapsed to headers; arrowing to the second and pressing right expands it, revealing a highlighted snippet; enter inserts the excerpt above the prompt as a cited excerpt"></p>

**Trace tree.** A subagent spawned by the `Task` tool streams its own text
and tool calls, which nest as a foldable tree under the parent chip rather
than interleaving with the main thread. Formatting happens lazily, only
once a chip is opened, and subagent text passes the same secret-scrubber
as everything else before it reaches a block:

<p align="center"><img src="assets/shots/trace.png" width="780" alt="A Task tool chip expanded, showing its own arguments and result plus a SUBAGENT narration line and a nested Grep tool chip inside it"></p>

**Subagent tracker.** The trace tree above is where a subagent's activity
lands once its Task call finishes; while it's still RUNNING, a second
status row appears directly under the status bar — `⧉ N agents` in the bar
itself, hidden below one exactly like the peers chip — with one clickable
`⧉ <label>` per subagent still in flight (its own `description`, the name
it gave itself). Clicking one opens a read-only transcript tab: no engine,
no prompt, just that subagent's narration and its own tool calls, seeded
from whatever its Task chip already buffered and kept live from there —
further calls and text keep landing in the open tab exactly as they land
in the chip. The tab marks itself `✓` when the subagent finishes and, the
same convention every other tab carries, picks up a green dot if you
weren't looking. Closing it (`ctrl+w`) is instant — there is no session
behind it to ask about:

<p align="center"><img src="assets/shots/subagent-tracker.png" width="780" alt="A status row reading '⧉ 1 agent' under the status bar, a second '⧉ 1 agent' chip in the bar itself, and a second tab in the strip titled from the running subagent's own description"></p>

**Tool-call compaction.** A turn's own top-level tool chips (the trace tree
above is untouched — a subagent's calls still nest under its own Task
chip) compact behind one "Tool calls (N)" fold, collapsed by default and
created lazily on the first call, so a turn with none grows no section at
all. N updates live as each call lands; opening the fold, then a chip
inside it, shows that chip's exact arguments and result, formatted only
on that first look:

<p align="center"><img src="assets/shots/tool-calls.gif" width="780" alt="A turn's 'Tool calls (N)' count ticks from 1 to 3 as chips land; opening the fold reveals all three collapsed chips; opening the first shows its ARGS and RESULT"></p>

**Reasoning stream.** Each turn asks the model for its own **summarized**
reasoning (`thinking: {type: "adaptive", display: "summarized"}` — see
[Reasoning](#reasoning) below for exactly what that means and doesn't) and
streams it into a "✻ Reasoning (N chars)" fold above the response, collapsed
by default and created lazily on the first chunk — a turn the model answers
without thinking at all grows no section, same hide-at-zero rule the tool-call
fold follows. The count updates live while collapsed; expanding mid-turn
never auto-collapses it back. `show_reasoning` (on by default) turns this
off entirely — see [Configuration](#configuration):

<p align="center"><img src="assets/shots/reasoning.gif" width="780" alt="A turn's collapsed 'Reasoning (N chars)' fold ticking up as the model thinks; opening it reveals the streamed summarized reasoning text, then the response streams in below once thinking finishes"></p>

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
not a spinner — it covers the gap before ANYTHING has arrived, and hides
itself the moment something does, whether that's the model's own reasoning
(see Reasoning stream above), the first word of its reply, or its first tool
call. There are exactly two timers anywhere in the app —
Textual's own 2 Hz caret blink on the focused prompt, and the clock below,
which only exists at all while it's switched on. A test asserts no other
timer is ever armed, with every overlay open.

**Multi-line prompt and paste.** The prompt is a `TextArea`, not a
single-line `Input` — it grows with the conversation, up to 10 rows, then
scrolls internally rather than displacing the block list above it. Enter
submits; Shift+Enter and Alt+Enter both insert a literal newline (whichever
your terminal actually distinguishes from bare Enter — item O's keyboard-
protocol detection will one day tell you which). Bracketed paste is one
edit no matter how many lines land — never one submit per embedded
newline — and CRLF/CR both normalize to LF. A paste past 4 lines or 4 KB
collapses to `⧉ pasted N lines (X KB)`; Ctrl+G expands it back in place to
look, and the FULL text goes out on submit either way, expanded or not.
`ctrl+v` is deliberately unbound (it would paste from this app's own
in-process clipboard variable, not the terminal's — silently stale); the
terminal's own paste delivers the real clipboard content directly. An
image on the clipboard can't reach a terminal app at all (no escape
sequence carries binary data) — DOXA notices the empty paste that results,
checks `wl-paste`/`xclip` for what's actually there, and says so rather
than pretending to attach it.

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

**Status bar**, left to right: model · `⚑ needs input` (only while a
question or permission request is pending on this pane) · effort (only
while one was asserted at connect) · `repo ⎇ branch @sha` · subscription
headroom (`s:9% w:48%`, session/week) or a `$` cost estimate on API-key
auth · context-window percentage (escalates normal → amber ≥70% → red
≥90%, percentage always shown) · belief count · `⌁ session <id>` reattach
handle · peers. Three chips are **clickable**, Claude-orange to say so:
the model chip and the branch half of the git chip open the same dropdown
picker — type to filter, arrows or a click to choose, Enter/click applies
it through the exact `/model` / `/branch` switch path, Esc or a click
elsewhere closes it — and the effort chip opens the same picker with an
upfront note that a pick only ever reaches a *future* session (the SDK
sets effort at connect time; nothing can make it live on this one). Three
more run something that already exists with no popup: click `peers N` for
`/sessions`, the context chip for `/compact`, the session handle to copy
it to the clipboard. Cost, repo name, sha and headroom stay plain — not
every chip is a button, only the ones that are:

<p align="center"><img src="assets/shots/chip-picker.gif" width="780" alt="Clicking the branch chip in the status bar opens a dropdown listing local branches with the current one marked, typing narrows it, and selecting one switches the session's base"></p>

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
| `worktree_per_session` | `DOXA_WORKTREE` | **on** | give each session its own git worktree instead of sharing the launch directory |
| `restore_tabs` | `DOXA_RESTORE_TABS` | **on** | plain `doxa` restores the whole saved tab set for this repo instead of just the most recent session |
| `consult_floor` | `DOXA_CONSULT_FLOOR` | 1.0 | act-time belief-consult threshold; 0 disables it |
| `nerd_font` | `DOXA_NERD_FONT` | off | use a Nerd Font glyph for the branch chip |
| `image_mode` | `DOXA_IMAGE_MODE` | probe | force a rung of the image ladder (`kgp`/`sixel`/`halfblock`/`text`) |
| `show_reasoning` | `DOXA_SHOW_REASONING` | **on** | stream the model's summarized reasoning into a collapsed per-turn fold; `0` stops DOXA asking to see it |
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

## Reasoning

What streams into the "Reasoning" fold is **summarized** reasoning, not
Claude's raw chain of thought: DOXA requests `thinking: {"type": "adaptive",
"display": "summarized"}`, and on every current model that field's other
value — `"omitted"` — is the *default*, meaning the API would otherwise send
back a `thinking` block whose text is an empty string. The raw internal
reasoning is never returned by the API at all, on any model, at any setting;
`"summarized"` is a real (and, per Anthropic's own docs, differently-billed —
you're charged for the full thinking tokens generated, not the shorter
summary you're shown) pass over that reasoning by a separate summarizing
model, not a truncation of it.

`show_reasoning` off does **not** assert `thinking: {"type": "disabled"}` —
it asserts nothing at all. Claude Fable 5, Claude Mythos 5 and Claude Mythos
Preview reject an explicit disable outright (thinking cannot be turned off on
those models under any configuration), and the session's actual model is
usually still unknown at the point these options are built — the CLI only
reports it after connecting — so there is no way to special-case around that
per model. Concretely: off means DOXA stops *asking to see* the reasoning;
on a model where thinking always runs, it still runs, and is still billed,
independent of this toggle. On a model where thinking is optional (most
non-5-generation models), off is a real zero-thinking-cost toggle. This is a
real API constraint, not a DOXA limitation the setting works around.

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
Shipped so far: the daemon/detach model, worktree-per-session, tabs, tab
restore, the command palette, `/search`, the trace tree, the image ladder,
peer discovery, the settings modal described above, the `curl | sh`
installer, `/setup`, `/doctor` / `doxa doctor`, and the clock — see
[CHANGELOG.md](CHANGELOG.md) for the version-by-version history. Not yet built: session-history drill-in past
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
