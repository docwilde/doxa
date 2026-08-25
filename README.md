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

**Since v0.37.0 that is genuinely all of it.** DOXA's memory model is
[LORE](https://github.com/docwilde/LORE)'s `lore_core`, and until v0.37.0
that package was not declared anywhere — DOXA reached into a LORE Claude
Code plugin checkout on the machine and hoped it was there. On a clone
without the plugin, 41 of 52 test modules failed at import. `lore_core` is
now an ordinary pinned dependency (`lore-core @ git+…LORE@<commit>`, a git
URL because neither project is on PyPI), so `uv sync` installs it like
anything else and nothing about the LORE plugin is a prerequisite for
running DOXA.

If you *do* have the LORE plugin installed, that checkout still wins over
the pinned copy: DOXA and the plugin share one SQLite store, the plugin
writes to it from a hook on every Claude Code session, and a terminal that
silently stopped reflecting the memory system the rest of the machine runs
would be a worse surprise than a version that is not the pinned one.
`/about` names which copy loaded, so it never has to be guessed — see
[How it works](#how-it-works).

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
tab set you left — order, pinned names, which tab was active, **and the
conversation that was on each tab** (`restore_tabs`, **on** by default) —
reattaching every session still alive and reporting what it did, rather
than just the single most recent one. The scrollback is read back from the
session's own transcript on disk, so it is the whole conversation and not
just whatever still fit in the daemon's replay buffer; a restore that had
to leave earlier turns out says so where they would have been. A saved
session that finalized in the meantime (its `--linger` expiring while the
window was shut) comes back **read-only** over that same transcript,
marked `⏺` and saying so in its first block — restore never spawns a
replacement for a session that's actually gone, and never lets a
transcript pass for a live tab. Only a saved tab with no session AND no
transcript is skipped, and the report says which is which
(`tab restore: restored 2 tabs, 1 read-only transcript (session ended),
skipped 1 session no longer running.`); a tab you closed with `ctrl+w`
stays in the set (it only detached, it's still running), but one you
explicitly stopped does not. Vertical/horizontal split layouts are not
restored, because DOXA doesn't have any — it's a tab strip. `doxa new` always
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
The session's own `doxa/<id>` is not among the branches offered: it is
this session's identity, not a base to fork from, and a session based on
itself has nothing left to measure unmerged work against.

Once you're in: type a prompt, press enter. `ctrl+p` opens the command
palette, `ctrl+t` opens a new tab, a line starting with `!` runs as a
shell command instead of a prompt, and `/help` lists every command and key
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
the way you left it, showing the conversation it had:

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

**Staged memory proposals, and `/pending`.** With `derive_secs` set, a
background reviewer runs over the live transcript between turns and stages
whatever it judges worth remembering — behind LORE's approval gate, where
it waits for a human. DOXA says so on three surfaces at once: a block in
the transcript that **quotes** what was staged (a count alone cannot tell
you whether a batch is worth opening), a calm steady tint on that
session's tab, and a desktop notification gated by window focus like every
other trigger. The tab tint is deliberately *not* the needs-input blink —
a staged proposal blocks nothing and expires never, and a signal that
shouted would be lying about the stakes.

`/pending` (also on the palette, and one click from the block itself)
lists everything currently staged, a selected row spilling its full text
into the transcript. It is **read-only**: approving and rejecting stay
with LORE's own `/lore:approve` / `/lore:reject`, because the write path
into curated memory is under security review and the approval gate does
not get a second door until that concludes.

**Command palette and `/` autocomplete.** `ctrl+p` opens a palette listing
new-tab, the open tabs (in tab-bar order, active one marked), every
registered command grouped (Session · Memory · Panes & tabs · Tools &
config · Maintenance), then live sessions available to attach. Typing `/`
at the start of the prompt opens the same list as a dropdown above the
input. Both read one registry (`doxa/commands.py`); a command cannot exist
on one surface and not the other.

<p align="center"><img src="assets/shots/palette.gif" width="780" alt="Ctrl+P opens the command palette on New tab; arrowing down moves the highlight through the open tabs and grouped commands; Esc closes it and returns focus to the prompt"></p>

**`!` — a shell command, without leaving the TUI or spending a turn.**
A prompt line beginning with `!` (`!git status`, `!ls -la`, `!pytest -q`)
runs in **this session's own directory** — its linked worktree, so
`!git status` reports on the tree the agent is actually editing — and its
output lands in the transcript as its own kind of block: a green left rule
and a `❯` command line, never the `▎` a turn wears, because shell output
must not be mistakable for the assistant's words. The exit code and
duration are always shown, including for a command that printed nothing.
stdout and stderr interleave in order; stdin is `/dev/null`, so a command
that wants an editor or a password fails immediately instead of hanging on
a terminal it can never get. Output past 64 KB is capped and the block
says how much it dropped; a command still running after 120 seconds has its
whole process group killed, so a stray `!tail -f` cannot outlive the tab.
It runs under a Textual worker, so the prompt stays live and the session
keeps streaming while a slow command runs.

Two things about `!` are deliberate and worth stating plainly.

*It runs with your full privileges and there is no confirmation step.*
`!rm -rf ~` deletes your home directory. That is the point of a shell
escape, and it is safe for exactly one reason: **only a line you type at
the prompt can reach it.** `!` is not a slash command (so nothing that
dispatches a command *by name* — a status-chip click, a future plugin row —
can name it), it is not a tool (so it is absent from the SDK tool surface
and the model has no call that lands there), and text arriving from
outside the window — another session's `/msg`, a tool result, a replayed
transcript — is rendered as a block and never dispatched. Exactly one
module in the package even imports the executor, and a test asserts that,
so wiring a second route in fails loudly rather than shipping quietly.

*Nothing about it enters the model's context.* Neither the command nor its
output is sent as a turn or written to the session transcript, so neither
survives a tab restore and neither reaches LORE's deriver. `!` is your
private side-channel; if you want the model to see the output, paste it
into a prompt yourself.

**`/context`.** The breakdown behind the status bar's context-window
percentage: which components are occupying the window right now, in tokens
— system prompt, tools, messages, free space — plus the `CLAUDE.md` files
that got loaded and what each MCP tool costs, each with its share of the
window. Every figure is the `claude` CLI's own accounting of its own
request (the same measurement the ctx% chip reads, so the two cannot
disagree); DOXA runs no tokenizer of its own and estimates nothing. A
component whose size can only be guessed at is either labelled for what is
actually known about it or left out entirely — the LORE snapshot DOXA
appends to the system prompt is reported as an exact **character** count,
with a note saying its tokens are counted inside the system-prompt row,
rather than a token number nobody measured. A session that cannot be asked
prints one sentence saying so and no numbers at all.

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

**Context headroom, in tokens.** The status bar's `ctx` chip escalates
normal → amber → red as the window fills, and hovering it says how many
tokens are in the window, how many the window holds, and how many are
left — because 12% of a 200k window and 12% of a 1M window are different
situations, and DOXA drives models with both. The percentage alone is
what the chip costs the bar by default; `ctx_absolute` prints `24k/200k`
beside it, and drops that segment again below 100 columns rather than
pushing other chips off the row. A context limit the CLI never reported
reads `?` and stays `?` — DOXA does not substitute a window size it did
not measure. `/usage` prints the same numbers exactly, with separators,
and `/context` breaks them down by component — all three are reads of one
measurement of the session, so they cannot disagree with each other.

**`/about`.** One screen with everything a bug report has to state: the
DOXA version (with its sha, and a `+` when the checkout is dirty), whether
an update is waiting, the Python, Textual and Claude Agent SDK versions,
the LORE version and store path, **which `lore_core` loaded** (the pinned
dependency or a plugin checkout, with its directory), the platform, and
the config file actually in force. `c` copies the whole thing, so it gets pasted rather
than retyped. No row is a constant — each is read off the thing it names,
and one that cannot be filled is left out rather than guessed.

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
≥90%, percentage always shown; `/context` breaks that one number down into
what is actually occupying the window) · belief count · `⌁ session <id>` reattach
handle · peers. Every chip has a one-line hover tooltip explaining what it
means, INERT ones included (cost, sha, headroom) — hovering answers "what
is this number" even where there is nothing to click.

Six chips are **clickable**, Claude-orange to say so. Four open the same
dropdown picker — type to filter, arrows or a click to choose, Enter/click
applies it: the model chip and the branch half of the git chip switch
through the exact `/model` / `/branch` path; the effort chip opens the same
picker with an upfront note that a pick only ever reaches a *future*
session (the SDK sets effort at connect time; nothing can make it live on
this one); the **repo name** now opens a directory-walking picker too —
type to narrow, pick a plain directory to descend into it, pick one marked
as a git repo (`⎇`) to open it in a **new tab** (the same spawn-or-attach
path `ctrl+t` takes, just at a chosen path instead of this session's own
cwd — the running session's own cwd never moves under it).

The remaining two run something that already exists, through a picker of
their own: `peers N` still runs `/sessions` directly, but the **context
chip** now asks first — compacting is lossy and irreversible, so a click
opens a confirm stating the current percentage and that accepting discards
earlier detail, and only an explicit accept sends `/compact`; the
**session handle** opens a dropdown of every session in scope, live and
detached (`⌁`) alike, the current one marked — pick a detached one to
attach to it (the same path `doxa attach` uses), pick one already open in
another tab to switch to that tab, or use the picker's own top row to copy
the handle to the clipboard (the old click-to-copy behavior, kept as a row
rather than dropped). The **belief count** is clickable too now — it opens
a filterable list grouped by scope (`user`, `project`, and `user model`),
picking one shows its full claim and confidence inline; this is a light
viewer, not the full beliefs browser with evidence trails and approve/
reject flows, which is still to come. Cost and sha stay plain — not every
chip is a button, only the ones that are:

<p align="center"><img src="assets/shots/chip-picker.gif" width="780" alt="Clicking the branch chip in the status bar opens a dropdown listing local branches with the current one marked, typing narrows it, and selecting one switches the session's base"></p>

**Background.** DOXA paints its own `#171512` on the body by default —
`opaque`, byte-identical to every release before it. Set `background`
to `transparent` (`ctrl+,` → Appearance, or `DOXA_BACKGROUND=transparent`)
and DOXA stops painting the base: the transcript, tab strip and clock chip
leave their cells at the terminal's own color instead of an explicit RGB.
**This alone does not make your terminal WINDOW see-through** — that is
your terminal emulator's or compositor's job (kitty's `background_opacity`,
WezTerm's `window_background_opacity`, a macOS Terminal profile, etc.).
What DOXA controls is only whether *it* paints; if your terminal is opaque,
`transparent` changes nothing visible. Every other rung of the surface
ramp — the status bar, tool-calls section, tool chips, and every popup and
modal (settings, palette, `/search`, the slash and chip dropdowns) — keeps
its own painted, opaque background regardless, so role tints and floating
surfaces stay legible against whatever the terminal shows through as. That
palette is validated for a **dark** terminal background, same as the rest
of DOXA's chrome; paired with a light terminal background, the base's own
body text (built for a near-black backdrop) will be very low contrast —
transparent mode is meant to sit over a dark desktop or dark terminal
theme, not a light one.

<p align="center"><img src="assets/shots/transparent.png" width="780" alt="The trace scene in transparent mode: a static screenshot can't show real terminal pass-through (Rich still bakes one concrete color for it), so this shows what it CAN prove -- the tool-calls section, tool chips and status bar still read as distinct, painted steps once the base itself stops painting"></p>

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
| `restore_tabs` | `DOXA_RESTORE_TABS` | **on** | plain `doxa` restores this repo's whole saved tab set — order, names, active tab and each tab's conversation — instead of just the most recent session |
| `consult_floor` | `DOXA_CONSULT_FLOOR` | 1.0 | act-time belief-consult threshold; 0 disables it |
| `background` | `DOXA_BACKGROUND` | `opaque` | `opaque` paints DOXA's own base (today's look); `transparent` stops painting it so an already-transparent terminal shows through — the terminal itself still has to be configured that way |
| `nerd_font` | `DOXA_NERD_FONT` | off | use a Nerd Font glyph for the branch chip |
| `ctx_absolute` | `DOXA_CTX_ABSOLUTE` | off | print `24k/200k` beside the `ctx%` chip; dropped again below 100 columns, and the numbers are in the chip's tooltip either way |
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

`lore_core` is a declared dependency — `lore-core @ git+…LORE@<commit>` in
`pyproject.toml`, pinned to a commit rather than a branch — so a bare
clone gets it from `uv sync` and needs nothing else. It is packaged out of
the LORE repo, where the plugin manifest stays the one place the version
is written.

**A pin is only as current as whatever moves it, so something does.**
`.github/workflows/lore-bump.yml` runs weekly (Mondays, 04:17 UTC) and on
demand: it reads the pinned ref, asks GitHub for LORE's newest `vX.Y.Z`
tag, and if that tag is ahead of the pin it rewrites `pyproject.toml`,
re-locks, runs the full suite against the result, and opens a pull
request. It never merges and never pushes to `main` — a green PR means the
upgrade is safe and waiting for a human, a red one means LORE moved in a
way DOXA cannot take yet. There is at most one such PR at a time: the
proposal lives on a single machine-owned branch (`automation/lore-bump`)
that a newer tag supersedes in place, and a proposal a maintainer closes
unmerged is not re-opened. When no LORE tag is newer than the pin the run
is green and silent, which is its ordinary outcome — including today,
because no LORE tag carries a `pyproject.toml` yet, and a ref without one
cannot be installed as `lore-core` at all. `scripts/lore_bump.py` is that
decision on its own and answers in about a second from a terminal.

That is the *staleness* half. CI's third matrix leg, which checks LORE out
at `main`, is the *breakage* half: it goes red when a LORE change breaks
DOXA, and it can never tell you that a LORE release exists which DOXA has
not adopted. Note also what a bump does **not** change, per the next
paragraph — on a machine with the LORE plugin installed the plugin's copy
wins regardless of the pin, so this matters for bare installs and for CI.

**Two copies can exist on one machine, and the plugin's wins.** A user
with the LORE Claude Code plugin installed has a checkout that DOXA
prepends to `sys.path` (`doxa/_lore_bootstrap.py`), ahead of the pinned
one. That is deliberate: both read and write the same `~/.claude/lore`
store, the plugin writes to it from a hook on every Claude Code session
start, end and compaction, and pointing two different `lore_core` versions
at one SQLite file is how a migration gets read by the version that did
not perform it. It also means a checkout you are editing is the one DOXA
sees, which is the behaviour that has been true since the shim was
written.

The price is that `pyproject.toml` no longer tells you what loaded, so
DOXA says it: `/about` carries a **`lore from`** row naming the source
(`plugin` or `package`) and its directory, measured off the imported
module rather than restated from the rule. Two env vars steer it —
`DOXA_LORE_CORE_PATH` points at a plugin checkout somewhere other than the
default `~/.claude/plugins/marketplaces/lore`, and `DOXA_LORE_SOURCE=package`
ignores any checkout and takes the pinned dependency, which is how you
reproduce a bug against exactly what CI runs.

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
