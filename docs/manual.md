# DOXA manual

Reference for what DOXA does today. Everything here is true of the current
code — verified against source, not transcribed from release notes. For
the pitch and the install instructions, see [README.md](../README.md). For
designs that are **not** built yet, see [docs/plans/](plans/) — this manual
never documents a plan as if it were shipped.

## Contents

- [Sessions and the daemon](#sessions-and-the-daemon)
- [Tabs](#tabs)
- [Worktrees and finalize](#worktrees-and-finalize)
- [Permission modes](#permission-modes)
- [The status bar](#the-status-bar)
- [LORE integration](#lore-integration)
- [Shell escape](#shell-escape)
- [Images](#images)
- [Search, resume, and peers](#search-resume-and-peers)
- [Keyboard protocol](#keyboard-protocol)
- [Commands](#commands)
- [Settings](#settings)

## Sessions and the daemon

Each session runs as its own **daemon process** hosting the Claude Agent
SDK client, the LORE hooks and the transcript. The TUI is a thin client
attached over a `0600` Unix socket (JSON, one object per line); closing the
terminal detaches rather than killing the session. A daemon finalizes a
session (LORE review + index) once every attached client has been gone for
`--linger` seconds (`linger_secs`, default 120), or immediately on `doxa
stop`.

Every published event carries a monotonically increasing `seq` into a
bounded in-memory ring; a client that reattaches sends the cursor it last
saw and the daemon replays from there, then the live tail follows. Nothing
in the ring persists — persisted state is the transcript file plus
whatever passes through LORE's scrub choke point.

CLI entry points (`doxa/cli.py`):

| command | does |
|---|---|
| `doxa` | Restore this repo's whole saved tab set if one exists and `restore_tabs` is on; otherwise spawn-or-attach — reattach the most recent live session in this repo, or spawn a fresh one |
| `doxa new` | Always spawn a fresh session and attach, ignoring any saved tab set |
| `doxa new --branch <name>` | Fork the new session's worktree from `<name>` instead of the launch directory's own checkout |
| `doxa attach [prefix]` | Reattach to a live session anywhere by session id / title prefix; bare form opens a picker when more than one candidate matches |
| `doxa stop [prefix]` | Finalize a session now (LORE review + index) and stop its daemon; no TUI |
| `doxa doctor` | Read-only health checks, no TUI: pass/fail plus the fix command per check; exits 1 if anything failed |
| `doxa launcher install` \| `uninstall` | XDG start-menu entry and icons, pointing at the exact checkout the command was run from |
| `doxa --in-process` | Engine runs inside the TUI process, no daemon; quitting finalizes on the spot |

`--branch <name>` fails with an actionable message if `<name>` does not
resolve. With `worktree_per_session` off, `--branch` refuses by default
(it would move the real checkout, not an isolated worktree); `--checkout`
allows that explicitly, and only on a clean tree.

Quit semantics inside the TUI: `ctrl+c` (and the palette's "Quit: detach")
detaches every tab, leaving each daemon running — pressing it twice stops
the sessions instead. `ctrl+q` ends the current tab's session for real
(finalizes and stops its daemon); on a read-only (archived) tab it just
closes the tab. `ctrl+w` / `/detach` close a tab but leave its session
running.

## Tabs

`ctrl+t` opens a new tab (fresh session, same repo scope). `ctrl+w`
closes the active tab and detaches its daemon. `ctrl+q` ends the active
tab's session for real. `ctrl+left` / `ctrl+right` cycle tabs.

A tab not currently in view reports what is happening on it by color, in
this precedence (lowest to highest): `-done-unseen` (green, a turn finished
while unseen) < `-staged` (muted violet, the background reviewer staged a
LORE proposal — a steady tint, not a blink, since nothing is blocked) <
`-working` (amber, a turn is in flight) < `-attention` (a blinking red,
this tab needs an answer to a question or permission request). All but
`-attention` clear the instant the tab is viewed; `-done-unseen` and
`-staged` never appear on the active tab at all.

A tab names itself from its first turn with one cheap Haiku call
(`doxa/naming.py`), cached in `~/.doxa/names.toml` so a session is never
renamed twice. Double-clicking a tab header, or `/rename`, opens an inline
editor: Enter commits, Esc cancels, an empty name restores the automatic
label.

`ctrl+p` opens the command palette: new-tab, the open tabs in tab-bar
order (active one marked), every registered command grouped (Session ·
Memory · Panes & tabs · Tools & config · Maintenance), then live sessions
available to attach. Typing `/` at the start of the prompt opens the same
list as a dropdown. Both read the one command registry
(`doxa/commands.py`).

### Restoring tabs

`restore_tabs` (default on) makes plain `doxa` restore the whole saved tab
set for a repo — order, pinned names, active tab, and each tab's
conversation read back from its own on-disk transcript — reattaching every
session still alive and reporting what happened:

```
tab restore: restored 2 tabs, resumed 1 ended conversation, skipped 1 session no longer running.
```

A tab whose session has since ended is handled by `resume_restored`
(default on): the tab comes back as a **live session continuing that
conversation** (one `claude` process spawned with `--resume`; no tokens
spent until you type). Off, or when the conversation cannot be continued,
the tab comes back **read-only** over its transcript, marked `⏺`, with the
first block naming why: the session is somehow still running, its
directory is gone, or the `claude` CLI has no history under that id (true
of any conversation recorded before v0.56.0, when DOXA and the CLI still
minted separate session ids).

A tab closed with `ctrl+w` stays in the saved set (only detached). A tab
ended with `ctrl+q` also stays in the set — it resumes or comes back
read-only like any other ended conversation. The only way to remove a
session from the set for good is reaping it by name (`/sessions kill
<prefix>` or `kill-detached`).

`doxa new` always starts exactly one fresh tab and never restores. `doxa
attach <prefix>` stays the single-session path. `DOXA_RESTORE_TABS=0`
returns to attaching only the single most recent session.

There is no split-pane layout — DOXA is a tab strip only (see
[docs/plans/split-panes.md](plans/split-panes.md) for the unbuilt design),
so nothing here restores a layout beyond tab order.

## Worktrees and finalize

With `worktree_per_session` on (default), each session gets its own linked
git worktree (`git worktree add ~/.doxa/worktrees/<repo>-<short> -b
doxa/<short>`), forked from whatever the launch directory has checked out.
`<short>` is the first 8 characters of the session id, fixed at spawn
time. Because git refuses the same branch checked out twice, two sessions
on the same repo — even the same branch — can never stomp each other.

The status bar's git chip shows the worktree's own session branch
(`doxa/<short>`); the **tab** shows the base branch the session forked
from.

`/branch` lists local branches with the current base marked; `/branch
<name>` switches it — free (fast-forward rebase) while the worktree is
clean and carries no commits of its own, refused the moment there is real
work a base switch would silently carry across. The session's own
`doxa/<short>` branch is never offered as a base to fork from.

**Finalize** (`doxa/worktrees.py`, run once at a session's real end, never
at a mere detach):

- Clean tree (`git status --porcelain` empty) and zero commits ahead of
  the branch it forked from → the worktree and its branch are removed with
  no trace.
- Anything else — a dirty tree, or committed-but-unmerged work — is kept.
  Nothing is ever auto-merged; the closing message names the branch to
  merge by hand.

With `worktree_per_session` off, every session runs directly in the launch
directory (the pre-worktree behavior).

## Permission modes

The `mode:` chip leads the status bar (first position, so it is never the
chip a narrow terminal drops) and names the session's permission mode —
what still stops and asks before a tool runs. `shift+tab` cycles it,
`/mode [name]` sets it directly, clicking the chip opens a picker. Glyphs
and colors are read out of the installed `claude` CLI's own permission-mode
table, not invented by DOXA.

| mode | glyph/color | behavior | reachable how |
|---|---|---|---|
| `default` | `⏸` grey | the CLI asks before anything it considers dangerous | Shift+Tab, `/mode` |
| `acceptEdits` | `⏵⏵` purple | file edits run unasked; everything else still asks | Shift+Tab, `/mode` |
| `plan` | `⏸` teal | no tool runs at all — planning only | Shift+Tab, `/mode` |
| `auto` | `⏵⏵` amber | a model classifier approves or denies each call instead of you | Shift+Tab, `/mode` |
| `bypassPermissions` | `⏵⏵` **bold red** | every tool call runs unapproved; nothing asks | Shift+Tab, `/mode`, but only on a session launched with `allow_bypass` armed |
| `dontAsk` | `⏵⏵` **bold red** | anything not pre-approved is denied, with no prompt shown | `/mode` only, with a confirmation dialog — never on the Shift+Tab cycle |

**`bypassPermissions` needs a session launched for it.** The `claude` CLI
arms that capability with `--allow-dangerously-skip-permissions` at launch
and refuses it at runtime otherwise. DOXA spawns that flag only when
`allow_bypass` is on (off by default). A session without it does not have
the mode at all — not in the cycle, not in the chip's picker, not in
`/mode`'s list; typing `/mode bypassPermissions` there explains what is
missing instead of failing opaquely. Arming is decided at launch, so
turning the setting on affects only sessions started afterward.

Four distinct sets govern what a given session can reach (`doxa/engine.py`):

- **Cycle modes** (`default → acceptEdits → plan → auto →
  bypassPermissions`, wrapping home): what Shift+Tab walks. `auto` and
  `bypassPermissions` are on the cycle by explicit user request against
  the original recommendation.
- **Gated modes** (`dontAsk` only): reachable solely through `/mode` plus
  a confirmation dialog.
- **Persistable modes** (`default`, `acceptEdits`, `plan`): the only modes
  a settings file or `DOXA_PERMISSION_MODE` may seed a *new* session with —
  narrower than the cycle on purpose. Cycling into `bypassPermissions` is
  per-session, visible (a red chip, a transcript line) and lasts one
  session; a stored default would be silent and apply to every future
  session in every repo opened afterward.
- **Unasked modes** (`auto`, `bypassPermissions`, `dontAsk`): the modes
  where DOXA stops asking about tool calls at all — what the chip's red
  coloring warns about.

`available_modes(armed)` is the one function every surface (cycle, chip
picker, `/mode`'s listing and validation) derives from: a mode this
session cannot reach is not shown at all, never shown-and-refused.

Entering `auto` or `bypassPermissions` writes a line into the transcript,
not just the chip, naming what stopped ("there is nothing left to
decline").

**Session-scoped, never saved by the hotkey.** `/mode` and Shift+Tab
change only the current session; the persistent default lives in its own
setting, `permission_mode` (see [Settings](#settings)), and only accepts
the three persistable modes.

Both `Shift+Tab` and `Ctrl+Tab` are bound to the same cycle action; under
the legacy terminal key encoding there is no byte for `Ctrl+Tab` at all, so
Shift+Tab is the one guaranteed to work everywhere and `/help` marks
whichever one this terminal cannot send.

## The status bar

Chips are built in paint order by `doxa/session/chips.py`; a chip whose
number is zero, or whose state was never asserted, is omitted rather than
shown empty. Every chip carries a tooltip on hover, including the plain
(non-clickable) ones.

| chip | shows | clickable |
|---|---|---|
| `mode:` | permission mode (see above); hidden only when it would show `default` on a cramped row | yes — mode picker |
| model | the model handling this session's turns | yes — model picker, takes effect next turn |
| `⚑ needs input` | a question or permission request is waiting on this pane | no |
| `effort:` | reasoning effort asserted at connect (hidden when none was) | yes — effort picker, affects future sessions only |
| repo/branch/sha | the git chip: repo name, the worktree's session branch, sha | yes — repo and branch halves each open their own picker |
| `sub:<tier> (≈$…)` or `$…` | subscription tier with a list-price what-if, or the real API spend on API-key auth | no |
| `s:N% w:N%` | subscription session (5h) and weekly utilization, cached by the `claude` CLI itself | no |
| `ctx N%` | context window usage, amber at 70%, red at 90%; `ctx_absolute` adds `24k/200k` inline | yes — confirms, then `/compact` |
| `N beliefs` | active LORE beliefs for this session | yes — grouped belief list |
| `mem u%p%` | curated-memory fill, user and project, as two separate percentages | no |
| `N proposals` | staged LORE proposals awaiting review (hidden at zero) | yes — pending-proposals picker |
| `⧉ N agents` | Task-spawned subagents currently running (hidden at zero) | no (see subagent row below) |
| `⌁ session <id>` | this session's reattach handle (only while attached to a daemon) | yes — sessions picker |
| `peers N (k⌁)` | other DOXA sessions on this repo; `k⌁` is how many are detached | yes — runs `/sessions` |
| `⊘ <tool>` | a tool disabled after two failures this session | no |

A `⧉ N agents` chip is accompanied by a second row under the status bar
with one clickable entry per running subagent; clicking one opens a
read-only transcript tab mirroring that subagent's own narration and tool
calls. Once the parent `Task` call finishes, the same activity becomes a
foldable tree under the parent tool-calls chip.

`/context` breaks the window down by component (system prompt, tools,
messages, free space, loaded `CLAUDE.md` files, per-MCP-tool cost) using
the `claude` CLI's own accounting — the same measurement the `ctx` chip
reads. `/usage` prints the same cost and utilization figures the status
bar chips show, with separators.

## LORE integration

DOXA compiles LORE's `lore_core` in-process (declared dependency, pinned
to a tag) rather than shelling out to the Claude Code LORE plugin — one
memory model, two front ends, one shared SQLite store when both are
installed on a machine (`/about` names which copy loaded).

**If a LORE Claude Code plugin checkout is present on the machine, it wins
over the pinned package** — both write the same `~/.claude/lore` store,
and the plugin's hook fires on every Claude Code session, so it is the
copy whose schema the store actually has. Two env vars override this:
`DOXA_LORE_CORE_PATH` points at a plugin checkout in a non-default
location; `DOXA_LORE_SOURCE` (`auto` default / `plugin` / `package`) forces
which copy loads — `package` is how to reproduce a bug against exactly the
pinned dependency without moving the plugin checkout aside.

**Curated memory** (user- and project-scoped) is hard-capped by character
count (4500 user / 8800 project by default, in `lore_core` itself); the
status bar's `mem u%p%` chip reports fill against those same caps.

**Beliefs** are an uncapped store with an FTS index and evidence trails.
At act time, one FTS pass over the prompt may attach a single belief as a
citation (`consult_floor`, default relevance floor 1.0; 0 disables it) —
labelled CITE-ONLY, never injected as fact. The model's entire memory tool
surface is five operators: four read-only (`lore_belief_search`,
`lore_belief_show`, `lore_memory_list`, `lore_session_search`) and one
write, `lore_remember`, which only **stages a proposal** — it never writes
directly into memory.

**The review gate.** The only write path into curated memory or the
belief store is a human approving a proposal, one row at a time. Through
v0.68.0 that review happened on two surfaces — a ten-row status-bar
picker for a glance, and `/beliefs`'s own full-height browser tab for
everything else. v0.69.0 retired the tab: the picker now carries
everything it did (per-row actions, evidence included), so there is one
surface, not two.

- `/pending` (or the status bar's proposals chip) lists staged proposals
  grouped by kind (`memory/user`, `memory/project`, `filemap`, `belief`,
  `skill`), each row showing what approving it would do. There is no
  bulk approve, on any surface.
- `/beliefs` (or the status bar's beliefs chip) lists every active
  belief, grouped by scope. A row shows its stamp, the newest entry in
  its outcome ledger (`confirmed`, `contradicted`, `stale`, or `never
  tested`), and its claim; scope, confidence and provenance (`via
  derived` / `via approved`, or unknown for anything predating the
  provenance ledger) are one hover away, in the row's own tooltip.
- **Evidence**, expanded in place: `Right` on a highlighted belief row
  fetches and inserts its derivation trail as real rows directly beneath
  it — one row per evidence event (session, project, note) — and `Left`
  folds them away again. Fetched lazily, one belief at a time, and never
  on load, so a store of hundreds of beliefs costs nothing until a row is
  actually expanded. A belief with no evidence still gets one row saying
  so; a trail longer than the picker's own cap says that too, in its own
  trailing row, rather than reading as complete. The evidence rows are
  disabled — the highlight cannot land on one, so an action key always
  acts on the belief above them, never on its own trail.
- A proposal row's controls are **approve** and **reject**. Reject applies
  immediately. Approve **arms** on the first selection and applies on a
  second, differently-worded selection — the write is the irreversible
  half, so it costs two deliberate acts.
- A belief row's own actions are recording an outcome
  (`confirmed`/`contradicted`/`stale`, written straight into LORE's
  outcome ledger as `source: user`) or **retract**, which also arms
  before it applies. These are not "approve" — a belief is already in the
  store and already steering the model; approve/reject applies to a
  *staged proposal*, a different object.
- Every approval and outcome record goes through LORE's own API, so an
  approved entry is labelled `via approved` by LORE, not by DOXA. On a
  `lore_core` older than the provenance ledger, the picker degrades to
  read-only and says why, up front — before a row is ever selected — and
  paints no approve/reject/confirm/retract control at all, on either
  picker, inline or in a row's own action menu.

**Inline row actions.** The `N beliefs` and `N proposals` chips open
dropdowns, not just glances: every row carries confirmed/contradicted/
stale/retract (beliefs) or approve/reject (proposals) reachable without
leaving the list. Click the action span on a row, or press its letter
(`a`/`r` for proposals, `y`/`c`/`s`/`r` for beliefs) while that row is
highlighted; approve and retract still arm on the first press and apply
on the second, on the same row. Selecting a row outright (Enter, or a
click that misses every action) opens a per-row action menu carrying the
same verbs one selection deep — the inline controls are a faster path
alongside it, not a replacement. While either picker is open, the prompt
line filters its rows instead of sending to the agent; typing narrows the
list a beat later (the rebuild debounces, so a fast typist gets one
settled query per word rather than one per letter — a live `/query …`
marker in the picker's own border shows a filter is pending until it
does), `Right`/`Left` expand and fold a belief's evidence, Enter acts on
the highlighted row, Esc closes and clears it. The five action letters
only fire while that filter is empty — once it holds text they are
ordinary characters, so searching for a claim that happens to start with
one of them costs one throwaway keystroke first rather than ever firing
an action by accident. Evidence text is not itself searchable (the filter
only ever scores a row's own claim), so a typed filter hides any expanded
trail without forgetting it — clearing the filter shows it again, with no
second fetch.

Both pickers' rows share one format: `YY-MM-DD HH:MM  status  age  text`,
fixed-width columns so neighbouring rows line up as a table, with a
column-name header of its own naming them at the top of the list (hidden
while a filter is typed — the alignment beneath it never depended on the
header being there). The `user`/`user-model` group headers also carry
LORE's own channel tag —
`user · stated` (the user said it themselves; a later session may act on
it) vs `user-model · inferred` (read off behaviour, never spelled out;
shapes tone and authorizes nothing) — spelled out in full in a belief's
own tooltip.

**Streaming review.** With `derive_secs` set, a background reviewer runs
over the live transcript between turns and stages whatever it judges worth
remembering, behind the same approval gate. Session-end review (LORE's
`PreCompact`/finalize pass) always runs regardless of `derive_secs`.

## Shell escape

A prompt line starting with `!` (`!git status`, `!pytest -q`) runs in the
session's own directory (its linked worktree, if any) under a Textual
worker: stdin is `/dev/null`, output is capped at 64 KB, and the whole
process group is killed after 120 seconds. It is not a slash command and
not a tool — nothing that dispatches by name, and no model tool call, can
reach it; exactly one module imports the executor.

It runs with full user privileges and asks nothing first. Neither the
command nor its output enters the model's context, is written to the
session transcript, or reaches LORE — it does not survive a tab restore.

## Images

Image rendering follows a fallback ladder, probed once per process before
the TUI takes stdin: **kitty graphics protocol → sixel → half-block cells
→ plain text line**. `image_mode` forces a specific rung.
`DOXA_KEYBOARD_PROTOCOL`-style overrides aside, the probe result is
cached and never repeated (re-probing after Textual has taken over stdin
would read a stale reply).

`boot_banner` (default on) draws the DOXA mark above the opening identity
block: a ring around a triangle, hand-authored in block characters, the
same on every terminal regardless of what tier `image_mode` settled on.
`off` removes it. There is no raster form any more — v0.66.0 dropped the
raster `logo.png` this used to draw on `kgp`/`sixel` terminals, so the
knob is a plain on/off switch now rather than a choice of which form to
draw; a config.toml still holding `auto`, `blocks` or `image` from before
that change keeps reading as on.

`/img` with no argument reports which tier this terminal actually
answered for and draws the same asset in each tier it answered for,
labelling anything not measured as not measured rather than guessed.

## Search, resume, and peers

`/search` (or `ctrl+r`, which prefills it) opens a popup over LORE's
full-text session index, debounced and sequence-guarded so a slow query
can never overwrite a newer one's results. A result set spanning more than
one session groups into a collapsed-by-default tree of session headers
over matching snippets. `enter` on a snippet inserts its excerpt into the
prompt; `enter` on a session header offers to resume that conversation.

`/resume [session-id]` reopens a past conversation in a **new tab** with
its history reloaded — bare, it lists recent conversations to pick from.
It refuses, in words, before spawning anything: if the conversation is
still running (attaches instead), if its directory is gone, or if it
predates v0.56.0 (before DOXA and the `claude` CLI shared one session id,
so the CLI has no history to resume from — such a conversation stays
searchable and readable, never resumable).

`/attach [prefix]` reattaches a live detached session in a new tab; bare,
it attaches the one detached session in scope, or opens a picker when
there are several.

`/sessions [kill <prefix> | kill-detached]` lists every live session in
scope with its age and whether it is attached here or detached, with a
kill command for either.

**Peers.** Independently launched sessions on the same repo discover each
other through a same-user runtime registry (`0700`, per-session presence
file, heartbeat, dead entries reaped by any reader). `/peers` lists them;
`/msg <session_prefix> <text>` delivers one line-JSON message over the
target's own `0600` socket. Every received field is scrubbed before
display and reaches the model only behind an untrusted-peer preamble. The
model has no send tool — every peer message crosses because a human typed
`/msg`.

## Keyboard protocol

Textual's Linux driver requests the kitty keyboard protocol at startup but
never reports whether the terminal granted it. DOXA asks the terminal
itself once, before the TUI takes over the keyboard (`\x1b[?u` plus a
Primary Device Attributes sentinel), and reports the answer on `/about`
and in `/doctor`. A binding this terminal cannot physically send (under
the legacy encoding there is no byte for `Ctrl+,` or for distinguishing
`Shift+Enter` from plain Enter) is marked `✗` in `/help`. Silence from the
terminal reads as **not measured**, never as "legacy".

## Commands

Every command below is defined once in `doxa/commands.py` and reaches the
palette, the `/` autocomplete and `/help` from that single registry.

**Session**

| command | does |
|---|---|
| `/model [name]` | Switch the model for the rest of this session (no reconnect) |
| `/branch [name]` | List local branches (current base marked), or switch this session's base |
| `/mode [name]` | Permission mode; bare lists all six with what each does |
| `/effort [level]` | Effort level for new sessions only (connect-time) |
| `/usage` | Session tokens, turns, cost, subscription headroom |
| `/context` | What is occupying the context window right now, by component |
| `/clear` | Fresh session in this tab: finalize, rotate transcript, reset |
| `/sessions [kill <prefix> \| kill-detached]` | Every live session: name, age, attached — and how to kill one |
| `/resume [session-id]` | Reopen a past conversation in a new tab |

**Memory**

| command | does |
|---|---|
| `/beliefs` | Browse active beliefs — confirmed/contradicted/stale/retract inline, evidence on Right |
| `/pending` | Staged proposals — approve or reject inline |
| `/search <terms>` | Search every past session (live results as you type) |

**Panes & tabs**

| command | does |
|---|---|
| `/peers` | Live sessions in this project right now |
| `/msg <session_prefix> <text>` | Send a message to one same-project peer session |
| `/detach` | Close this tab but leave its session running |
| `/attach [prefix]` | Reattach a live detached session in a new tab |
| `/rename [name]` | Name this tab; empty restores the automatic one |

**Tools & config**

| command | does |
|---|---|
| `/img [path]` | What this terminal can draw, in every tier; with a path, render that file |
| `/login [provider]` | Sign in through a provider's own auth CLI (default: `claude`) |
| `/logout [provider]` | Sign out through a provider's own auth CLI |
| `/settings` | Open the settings modal (`ctrl+,`) |
| `/setup` | Check state, fix findings one at a time |
| `/doctor` | Read-only health checks: pass/fail and the fix command for each |

**Maintenance**

| command | does |
|---|---|
| `/compact` | Ask the CLI to compact the transcript (runs LORE's review first); passthrough, not intercepted |
| `/update [--restart]` | Fast-forward this DOXA checkout from origin (never merges) |
| `/help` | Every command and key binding, generated from this registry |
| `/about` | Version, dependencies, platform and config path — what a bug report needs |

## Settings

Precedence everywhere: **environment > `~/.doxa/config.toml` > default.**
The file is plain TOML, `0600`. The settings modal (`ctrl+,` / `/settings`,
grouped into Session · Memory · Appearance · Notifications · Paths ·
About) shows each row's effective value and where it came from; a row the
environment is winning is read-only in the modal.

| setting | env | default | what it controls |
|---|---|---|---|
| `model` | `DOXA_MODEL` | CLI default | model for new turns |
| `effort` | `DOXA_EFFORT` | CLI default | reasoning effort, new sessions only |
| `allow_bypass` | `DOXA_ALLOW_BYPASS` | off | let new sessions reach `bypassPermissions` at all |
| `permission_mode` | `DOXA_PERMISSION_MODE` | `default` | mode new sessions connect in; accepts `default`/`acceptEdits`/`plan` only |
| `linger_secs` | `DOXA_LINGER_SECS` | 120 | seconds a daemon outlives its last detached client |
| `worktree_per_session` | `DOXA_WORKTREE` | on | give each session its own git worktree |
| `restore_tabs` | `DOXA_RESTORE_TABS` | on | plain `doxa` restores the whole saved tab set |
| `resume_restored` | `DOXA_RESUME_RESTORED` | on | a restored tab whose session ended comes back live, continuing the conversation |
| `derive_secs` | `DOXA_DERIVE_SECS` | off | streaming-deriver interval; unset runs review only at session end |
| `consult_floor` | `DOXA_CONSULT_FLOOR` | 1.0 | act-time belief-consult relevance floor; 0 disables it |
| `lore_root` | `LORE_ROOT` | `~/.claude/lore` | where the belief store and session index live; sticky, set by `/setup` |
| `nerd_font` | `DOXA_NERD_FONT` | off | use a Nerd Font glyph for the branch chip |
| `ctx_absolute` | `DOXA_CTX_ABSOLUTE` | off | print `24k/200k` beside the `ctx%` chip (below 100 columns it drops again) |
| `image_mode` | `DOXA_IMAGE_MODE` | probe | force a rung of the image ladder (`kgp`/`sixel`/`halfblock`/`text`) |
| `boot_banner` | `DOXA_BOOT_BANNER` | on | draw the DOXA mark above the opening identity block |
| *keyboard override* | `DOXA_KEYBOARD_PROTOCOL` | probe | `kitty`/`legacy`/`unknown`, for a terminal that lies about it; env-only |
| `show_reasoning` | `DOXA_SHOW_REASONING` | on | stream the model's summarized reasoning into a collapsed fold |
| `background` | `DOXA_BACKGROUND` | `opaque` | `opaque` paints DOXA's own base; `transparent` stops painting it |
| `clock_show` | `DOXA_CLOCK_SHOW` | on | show the upper-right clock |
| `clock_date` | `DOXA_CLOCK_DATE` | off | prefix the clock with `%Y-%m-%d` |
| `clock_hour` | `DOXA_CLOCK_HOUR` | `24` | `12` or `24`-hour |
| `clock_seconds` | `DOXA_CLOCK_SECONDS` | off | show `:SS`; also re-aligns the clock's timer to the second |
| `clock_tz` | `DOXA_CLOCK_TZ` | system | IANA zone name, e.g. `Europe/Berlin`; unresolvable falls back to system local, visibly |
| `clock_format` | `DOXA_CLOCK_FORMAT` | (none) | custom `strftime`, overrides the toggles above; validated on save |
| `notify` | `DOXA_NOTIFY` | `auto` | when desktop notifications fire: `auto` (only while unfocused), `always`, `off` |
| `notify_turn_done` | `DOXA_NOTIFY_TURN_DONE` | on | notify when a turn finishes |
| `notify_staged` | `DOXA_NOTIFY_STAGED` | on | notify when the background reviewer stages proposals |
| `notify_needs_input` | `DOXA_NOTIFY_NEEDS_INPUT` | on | notify when a session is waiting on you; a fully detached session always notifies |
| `notify_update` | `DOXA_NOTIFY_UPDATE` | on | notify when `/update` has something to pull |
| `notify_lore` | `DOXA_NOTIFY_LORE` | on | `lore_core`'s own review banner; held silent while `notify_staged` is on |
| *doxa home* | `DOXA_HOME` | `~/.doxa` | durable state: this config, tab sets, names |
| *runtime dir* | `DOXA_RUNTIME_DIR` | `$XDG_RUNTIME_DIR/doxa` → `~/.local/share/doxa` | ephemeral daemon sockets and the peer registry |

`show_reasoning` off does not force thinking off — some models (Claude
Fable 5, Claude Mythos 5, Claude Mythos Preview) reject an explicit
disable outright; the toggle stops DOXA *asking to see* the summarized
reasoning, nothing more. See `doxa/engine.py`'s `_build_options` for the
exact request shape (`thinking: {"type": "adaptive", "display":
"summarized"}`).

`~/.doxa/` holds durable state; the runtime directory holds ephemeral
daemon sockets and the peer registry, kept out of the home directory
because home directories can be network-mounted (Unix sockets misbehave
there). The LORE store is neither — it stays `lore_core`'s own path,
shared with the Claude Code LORE plugin on purpose.
