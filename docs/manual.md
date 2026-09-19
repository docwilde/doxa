# DOXA manual

Reference for what DOXA does today. Everything here is true of the current
code — verified against source, not transcribed from release notes; this
document was last read against **1.9.3** end to end. For the pitch and the
install instructions, see [README.md](../README.md). For designs that are
**not** built yet, see [docs/plans/](plans/) — this manual never documents
a plan as if it were shipped.

## Contents

- [Sessions and the daemon](#sessions-and-the-daemon)
- [Engines](#engines) — [engine capabilities](#engine-capabilities) and [a Codex session](#a-codex-session)
- [The spawned CLI](#the-spawned-cli)
- [The transcript](#the-transcript)
- [Tabs](#tabs) — and [restoring them](#restoring-tabs)
- [Pane groups](#pane-groups)
- [The session sidebar](#the-session-sidebar)
- [The live diff](#the-live-diff)
- [Worktrees and finalize](#worktrees-and-finalize)
- [Where a session is](#where-a-session-is)
- [Permission modes](#permission-modes)
- [Containment](#containment) — [session spawn](#session-spawn--off-unless-you-turn-it-on) and [remote drivers](#remote-drivers--a-policy-and-no-transport)
- [The status bar](#the-status-bar)
- [LORE integration](#lore-integration)
- [Shell escape](#shell-escape)
- [Images](#images)
- [Search, resume, and peers](#search-resume-and-peers) — [fleets from the TUI](#fleets-from-the-tui) and [spend ceilings](#spend-ceilings)
- [Keyboard protocol](#keyboard-protocol)
- [Commands](#commands)
- [Settings](#settings)
- [Screenshots](#screenshots)

## Sessions and the daemon

Each session runs as its own **daemon process** hosting the engine — the
Claude Agent SDK client, or whichever `--engine` named — plus the LORE
hooks and the transcript. The TUI is a thin client
attached over a `0600` Unix socket (JSON, one object per line); closing the
terminal detaches rather than killing the session. A daemon finalizes a
session (LORE review + index) once every attached client has been gone for
`--linger` seconds (`linger_secs`, default 120), or immediately on `doxa
stop`.

Every published event carries a monotonically increasing `seq` into a
bounded in-memory ring (`RING_CAPACITY`, 512 events); a client that
reattaches sends the cursor it last saw and the daemon replays from there,
then the live tail follows. Nothing
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

Quit semantics inside the TUI are **tab-scoped on the keys and
window-scoped on the palette**. `ctrl+q` ends the current tab's session for
real (finalizes and stops its daemon); on a read-only (archived) tab it
just closes the tab. `ctrl+w` / `/detach` close a tab but leave its session
running. Ending the whole window is the palette's job: **Quit: detach**
detaches every tab and leaves each daemon running, **Quit: stop session**
finalizes every session now — except a tab you detached on purpose, which
stays up, because detaching is the explicit "keep this running" gesture and
a later quit must not quietly undo it.

**`ctrl+c` is bound to nothing**, and since v0.85.0 Textual's own default
binding for it is popped out of the resolved set at startup rather than
rebound to a no-op (`doxa/app.py`). Through v0.84.0 DOXA did claim it — one
press quit-detached every tab, two quit-stopped them — and a report from
live use asked for it back: a terminal emulator only treats `ctrl+c` as a
copy gesture over a selection if no foreground app has claimed it. Quitting
never needed the key, so the key went.

## Engines

An **engine** is whatever actually runs a turn. `doxa/engines.py` is the
seam between it and the rest of DOXA: one `Engine` Protocol — the 24
public names the in-process `SessionEngine` and the daemon-fronting
`EngineClient` already shared before the Protocol was written — plus a
registry mapping an engine id to a provider. Four ids ship — `claude`
(the default), `codex`, `deepseek` and `glm` — and the registry is an
explicit dict with explicit registration calls: nothing is discovered from
the path.

Pick one per session with `--engine <id>`, `DOXA_ENGINE`, the `engine`
setting, or `/engine <id>`, in that precedence. An unknown id fails as one
line of usage before anything is built, rather than as a traceback out of
a half-started app — and the list it fails with, the settings row's
choices and `/engine`'s listing are all `doxa.engines.available()`, so an
engine that is registered is selectable everywhere and one that is not is
offered nowhere.

`/engine` with no argument prints every registered engine with its
capability count and the fields it does *not* have, read off
`EngineCapabilities` itself. Selection is a **connect-time** choice: it
reaches new sessions and tabs and never the running one, and the command
says so rather than letting you find out by watching it do nothing.

**Capability is not uniform, and pretending otherwise is the trap.** Each
provider declares an `EngineCapabilities` — eighteen flat booleans naming
the surfaces it actually has, every one defaulting to `False` so a
provider that forgets something under-promises instead of over-promising.
`doxa.engines.get("codex").supports()` is the whole map for that engine.
The rule every caller follows is that a `False` **hides** the surface
rather than painting it inert: an engine that never reports a context
window gets no `ctx` chip at all, because `ctx —` reads as "not yet" when
the truth is "never", and `/context` says it cannot be asked rather than
inventing a breakdown.

### Engine capabilities

What each engine can do, one row per surface. The rows are
`doxa.engines.get(<id>).supports()` read off the registry, not a promise;
`/engine` prints the same counts live (claude 18 of 18, codex 8, deepseek
and glm 11). A fleet slot dealt an engine has exactly that engine's row.

| | claude | codex | deepseek | glm |
|---|---|---|---|---|
| billed on | Claude subscription | Codex CLI sign-in | `DEEPSEEK_API_KEY` | `ZAI_API_KEY` |
| daemon, detach, `doxa attach` | yes | yes | yes | yes |
| LORE tools and the tool gate | yes | yes, via MCP | yes | yes |
| permission modes, hooks, plugins | yes | no | no | no |
| cost and context-window chips | yes | no | no | no |
| `/msg` to a peer | yes | yes | yes | yes |
| `peer_list`, `peer_history` tools for the model | yes | yes | yes | yes |
| `peer_send` tool for the model | yes | yes | yes | yes |
| budgets, `Task` spawns | yes | no | no | no |
| fleet slot | yes | yes | yes | yes |
| `--no-lore` honoured | yes | yes | yes | yes |


Two rows deserve a sentence. *LORE tools and the tool gate* reach Codex
through the stdio MCP server `CodexEngine` registers on every turn, and
the gate lives in that server's process, so the two-strikes disable lasts
one turn. *Budgets* need a dollar figure, which only the Claude engine
reports; a slot on any other engine is unbounded and `doxa-fleet` says so
before it starts.

### A Codex session

`--engine codex` runs a DOXA session on the Codex CLI, end to end: its own
tab, transcript, turns, status bar, peer rail and `/msg`. It is not the
`codex:rescue` subagent plugin — that is a tool a Claude session calls;
this is a session.

The structural difference is the process model. `ClaudeSDKClient` is one
long-lived process for the whole session; a Codex session is **one `codex
exec --json` process per turn**, the first starting a thread and every
later one running `codex exec resume <id>`. The prompt goes in on stdin
rather than argv, because a pasted prompt can be megabytes and `ARG_MAX`
is not. Since v1.13.0 the daemon hosts it like any other engine
(`doxa.daemon --engine codex`), so a Codex session detaches, reattaches
with `doxa attach` and survives the terminal closing. `doxa --in-process
--engine codex` still runs it inside the TUI, and there `ctrl+q` ends the
session rather than detaching it.

What Codex does not report, DOXA does not paint. Measured against
codex-cli 0.144.4:

| absent | what DOXA does |
|---|---|
| any window size (`turn.completed.usage` counts tokens and nothing else) | no `ctx` chip; `/context` says it cannot be asked; `/usage` still prints the real token counts |
| any cost field | no cost chip — `$0.0000` would read as "free", which is a different claim from "nobody said" |
| content deltas (`agent_message` arrives whole) | still one `text_delta`, just one per message |
| reasoning content | no reasoning fold; the usage block counts reasoning tokens but the stream carries none |
| permission modes | no `mode:` chip, nothing on Shift+Tab |
| a hook surface | the LORE snapshot cannot be injected mid-session, so `CodexEngine` prepends it to the first prompt under a header that says what it is |
| the model it actually resolved | `self.model` is what was *asked for*; an unasked-for default publishes as absent, never guessed |

Two consequences worth stating plainly. **A Codex session reaches DOXA's
LORE and peer tools through a stdio MCP server**, `python -m
doxa.mcpserver`, which `CodexEngine` registers on every `codex exec` with
`-c mcp_servers.doxa.*` overrides and which executes every call through
the same `ToolGate` a vendor session builds. Two limits follow from Codex
spawning that server per turn: the two-strikes disable lasts one turn,
and Codex keeps the server's stderr, so a disable cannot be read back
into a `tool_disabled` event; the gate's refusal result is the guarantee.
`peer_send` is offered, and the sidecar performs none of it: it forwards
the request over a per-session control socket to `CodexEngine`, which
sends through the same `PeerDelivery` object `/msg` uses, so the
session's rate limiter, its ledger and its status bar see a model's send
and a human's identically. And **the LORE review is not wired for this
engine**: the transcript is still
indexed at session end, and the `session_done` event says `review:
skipped` rather than implying one ran.

The belief *count* is real on both engines — it is one `SELECT` against a
store neither engine owns. The belief and proposal *pickers* are not: they
live on `SessionEngine` because that is where they were written, which is
a fact about the code's shape rather than about the engine. So the
`N beliefs` chip is plain rather than clickable here, and `/beliefs` stays
in the command list and says the engine has no memory surface rather than
opening an empty picker. The peer layer has no
model in it, so the rail, `/msg` and the registry work identically.

## The spawned CLI

The Claude engine behind a session spawns a `claude` CLI process, and that
process gets a **config directory of its own**. (Everything in this
section is that engine's; a Codex session spawns `codex exec` instead and
has no plugin surface at all — see [Engines](#engines).) `CLAUDE_CONFIG_DIR` is
set on the child's environment only (`ClaudeAgentOptions.env` —
DOXA's own environment is never touched), pointing at a directory DOXA
writes and owns: a `settings.json` with no `hooks`, no `enabledPlugins`
and no `plugins` key at all, plus `LORE_SKIP=1` belt-and-braces. So none
of the Claude Code plugins installed on your machine — none of their
hooks, commands, skills, agents or MCP servers — load into a DOXA session
unasked.

Your own **learned skills** (`~/.claude/skills`) do carry through — they
are approved artifacts, and losing them inside DOXA would be a surprise.
They are **copied** into that config directory at each session start, not
linked: the copy is a one-directional snapshot, so a session editing a
`SKILL.md` changes only its own copy and your skill set is never written
to from inside a session. The cost of that is timing — a skill approved
while a session is running reaches it at its next start, not mid-session.

`/plugins` shows what DOXA found on the machine and what it did with it:
discovered, adopted, or refused with the reason. `/reload-plugins`
re-scans; adoption is read at spawn, so a re-scan reaches new
sessions and tabs, not the one you are in.

`adopt_plugins` (**off** by default) is the opt-in. Turned on it carries
in **commands, skills and agents only**. Hooks are refused
unconditionally, `.mcp.json` / `mcp.json` are not read, and the `hooks`
and `mcpServers` keys are stripped out of any manifest that carries them.
The LORE plugin is blocklisted outright even then, because `lore_core`
already runs in-process here and a second copy would fork one memory
store into two halves. See
[docs/plans/plugins.md](plans/plugins.md).

## The transcript

A turn renders as one block, and the block is built in this order, top to
bottom: the prompt you typed, the reasoning fold, the reply body, the
tool-call fold.

**Reasoning** streams into a fold headed `✻ Reasoning (N chars)`, whose
count ticks up live while it is still collapsed — which it is by default.
It is requested at connect (`thinking={type: adaptive, display:
summarized}`) and the whole thing is behind `show_reasoning`, on by
default; off, DOXA asks for nothing extra.

**The reply** streams as real markdown, not as text painted after the
fact: tables fill in row by row, bold spans and inline code close as
their deltas arrive, and a delta that splits a table row or a bold span
mid-token survives it.

**Tool calls** compact behind one fold per turn, headed `⚒ Tool calls
(N)`, collapsed by default and counting up as calls land. Opening it
shows one chip per call; opening a chip shows `ARGS:` (the exact JSON the
model sent) and `RESULT:` (what came back), built lazily so a
twenty-call turn costs nothing until you look. A memory-store call is an
ordinary chip like any other — there is no special case for it anywhere
in the renderer, which is the point: the mechanism deciding what the
agent believes is inspectable on the same terms as a `Grep`.

**The in-flight marker** sits under the block for the whole turn and
names the phase it is in — `thinking` before anything has arrived,
`reasoning` while summarized reasoning is streaming, `generating` while
the reply is, `working` between a tool call and its result — with a
spinner and a live second count beside it. The seconds keep climbing
through the silent stretch between a tool call and its result, which is
exactly where "is this still working?" gets asked.

**A failure** is a block too. A caught exception renders as a
collapsible red-ruled block inside the transcript, one line collapsed,
its traceback and origin one keystroke away — rather than taking the app
down.

### Typing while a turn runs

A prompt submitted while a turn is still running is **queued, never refused**,
and the running turn is not touched. The transcript acknowledges it with its
position; it starts on its own the moment the current turn ends, and every
attached tab of the same session sees the same queue. At most eight prompts
wait; the ninth is refused with a reason. `/queue` lists what is waiting,
`/queue 2` (or an id such as `q3`) cancels one before it starts. A queued
prompt survives detaching and is discarded, visibly, only when the session
finalizes.

**Why it queues, and what that is not.** Mid-turn delivery is not
impossible in general. Claude Code does it: its CLI holds a message queue
of its own and folds what is waiting into the **running** turn as a
`queued_command` attachment delivered alongside the next tool result. The
CLI's internals name the mechanism outright —
`messageQueue.consume(..., {reason: "absorbed_mid_turn"})`,
`isMidTurnFoldSuspended()`. Absorption, not a second turn.

That machinery is **not reachable through the stdin protocol the Agent SDK
speaks**, and this is measured rather than inferred. Spawn the CLI in
stream-json mode, start a turn that makes three sequential `Bash` calls,
and write a second user frame to its stdin six seconds in, while the first
turn is inside a tool call. The frame never appears in the output stream at
all. The turn completes all three steps unaffected, emits its result, and
no second turn ever starts — the frame is neither absorbed into the running
turn nor deferred into a following one. It is **dropped, silently**. Two
runs, identical: the first under `-p`, the second without it, because the
SDK does not pass `--print` — it spawns with `--output-format stream-json
--verbose --input-format stream-json`. The same outcome on the SDK's own
spawn shape is what makes this a fact about DOXA's conditions rather than a
print-mode artefact. Measured on `claude-agent-sdk` 0.2.144 and Claude Code
2.1.273.

So a bounded FIFO on DOXA's side is not a second-best reading of the SDK —
it is the only thing that does not lose the prompt. `interrupt()` is the
one mid-turn primitive the SDK exposes and it aborts rather than steers,
and a second `query()` is no way round it either: `receive_response()` ends
at the first `ResultMessage` on one shared stream, with no per-query
correlation id on a `result` frame, so two turns in flight risk one
iterator eating the other's result.

> **Note:** `SessionEngine.send`'s docstring argues the same conclusion
> from the SDK's read side alone. The conclusion holds; the reason recorded
> there is not the operative one, which is that the write never arrives.
> The docstring has not been corrected.

## Tabs

Since v0.97.0 tabs belong to a **pane group**, not to the window — one
group unless you split, in which case each region has a strip of its own.
Everything below is about the group holding the keyboard; see
[Pane groups](#pane-groups).

`ctrl+t` opens a new tab in this group (fresh session, same repo scope).
`ctrl+w` closes the active tab and detaches its daemon. `ctrl+q` ends the
active tab's session for real. `ctrl+left` / `ctrl+right` cycle **this
group's** tabs and leave every other group alone.

`ctrl+q` with a turn still in flight is the one close that asks first,
because killing work you are waiting for is not something a keystroke
should decide alone. The confirm has three doors and states each one's
key: **enter** (or `t`) terminates — the door `ctrl+q` already asked for —
`d` detaches instead and leaves the turn running, and **esc** keeps the
tab open. An idle session ends with no prompt at all.

A tab not currently in view reports what is happening on it by color, in
this precedence (lowest to highest): `-done-unseen` (green, a turn finished
while unseen) < `-staged` (muted violet, the background reviewer staged a
LORE proposal — a steady tint, not a blink, since nothing is blocked) <
`-working` (amber, a turn is in flight) < `-attention` (a blinking red,
this tab needs an answer to a question or permission request). All but
`-attention` clear the instant the keyboard actually arrives on that tab;
`-done-unseen` and `-staged` never appear on a tab you are looking at.
An inactive tab in a group you can SEE is still not one you are looking
at, so its marks survive.

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
conversation** (one daemon spawned with `--resume`, on the engine the
session ran on; no tokens spent until you type). Off, or when the
conversation cannot be continued, the tab comes back **read-only** over
its transcript, marked `⏺`, with the first block naming why: the session
is somehow still running, its directory is gone, the `claude` CLI has no
history under that id (true of any Claude conversation recorded before
v0.56.0, when DOXA and the CLI still minted separate session ids), or a
Codex session has no recorded thread id.

A tab closed with `ctrl+w` stays in the saved set (only detached). A tab
ended with `ctrl+q` also stays in the set — it resumes or comes back
read-only like any other ended conversation. The only way to remove a
session from the set for good is reaping it by name (`/sessions kill
<prefix>` or `kill-detached`).

`doxa new` always starts exactly one fresh tab and never restores. `doxa
attach <prefix>` stays the single-session path. `DOXA_RESTORE_TABS=0`
returns to attaching only the single most recent session.

Since v0.97.0 the window holds a **tree of pane groups** and each group
holds its own tabs, and the record restores all of it: the geometry, each
group's tab list, and which tab each group was showing. The design is
[docs/plans/pane-groups.md](plans/pane-groups.md).

Three record shapes exist and all three still read, with no version field
and no migration step — **the absence of a key is the migration**:

| written by | carries | restores as |
|---|---|---|
| v0.23.0 – v0.90.0 | a flat `tabs` list | one group holding all of them, showing the saved active tab |
| v0.91.0 – v0.95.0 | `trees`, one per tab | the active tab's tree, one single-tab **group per leaf**; the other saved tabs become tabs of the group holding the active session |
| v0.97.0 | `groups`, the window's one tree | itself |

It goes the other way too. The flat `tabs` list stays authoritative and
complete, and a v0.97.0 record still writes the older `trees` shape
alongside — one tree per group, each region's leaf being that group's
active tab. So an older DOXA reading a grouped record gets the geometry it
can express and picks the rest up as ordinary tabs, rather than getting
nothing.

## Pane groups

Since v0.97.0 the window is a **tree of groups**, and each group owns its
own tab strip. A window that never splits is one group holding every tab
and behaves exactly as it always did.

This inverts what shipped in v0.91.0, where the window owned the tabs and
each tab owned a tree of panes. The reason was a report: *"the new
sessions have no tab menu of their own… if i switch tabs, the split out
sessions go with the tab. Shouldn't the split out sessions be
independent?"* They are now. `ctrl+←/→` cycles the tabs of the group
holding the keyboard and leaves every other group alone — three sessions
cycling on the left while a fourth stays pinned on the right is the thing
the old model could not express.

`/split` (or `ctrl+o`) puts a second group **stacked below** this one;
`/vsplit` (or `ctrl+n`) puts one **side by side** with it. That is vim's
sense of the two words and the opposite of tmux's `split-window -h`, so
every description spells the direction out rather than trusting the
letter — the letters are not mnemonic and are not trying to be.

They were `alt+s` / `alt+d` through v0.94.0 and were reported dead from
live use. Both earlier attempts were rejected against the wrong test.
`ctrl+shift+<letter>` sends the same byte as plain `ctrl+<letter>` under
the legacy encoding and is undeliverable — correct. Alt then looked safe
because every terminal has sent it as an ESC prefix for decades, which is
true of the terminal and irrelevant, because **Textual has no
ESC-prefix-to-Alt path**: it decodes `\x1b s` as Escape followed by a bare
`s`. `alt+<letter>` therefore only ever arrived on a terminal that granted
the kitty protocol. `alt+<arrow>` is unaffected — a modified arrow is
`CSI 1;3<final>`, which does decode — so the divider keys below keep it.
`alt+s` / `alt+d` / `alt+g` are still bound as kitty-only aliases and
`/help` marks them `✗` where they cannot arrive.

A split **spawns a new, independent session** — the same factory `ctrl+t`
uses — not a second view of the one you were in. Focus moves to the new
group, because someone who just asked for a second region is asking to
work in it. The group it was split off keeps rendering, keeps streaming,
and keeps any "you missed something" mark it had: visible, focused and
seen are three different states. An **inactive tab inside a visible
group** is the stronger case of the same rule — it is neither visible nor
focused, so its `done` dot, its needs-input blink and its staged tint all
survive until the keyboard actually arrives there.

| key | does |
|---|---|
| `ctrl+o` / `ctrl+n` | split into a second group, stacked below / side by side (`alt+s` / `alt+d` on a kitty-protocol terminal) |
| `ctrl+←/→` | cycle the tabs of **this group** — every other group stays put |
| `ctrl+1` … `ctrl+9` | jump to a group by position — **numbered left to right, then top to bottom**, so in a 2×2 it is upper-left, upper-right, lower-left, lower-right |
| `ctrl+shift+←/→/↑/↓` | move the keyboard to the group in that direction — geometric, never "next group" |
| `alt+←/→/↑/↓` | move the divider between this group and its neighbour that way |
| `ctrl+↑` / `ctrl+↓` | move the **in-pane** divider (the status bar): up grows the transcript, down grows the prompt — and this works in a window with no splits at all |
| `alt+shift+←/→` | move the divider between the **session sidebar** and the panes (it also drags with the mouse) |

Any `ctrl+<digit>` also **flashes each group's number** over its own
region, briefly. The jump happens immediately — it is feedback, not a
mode, and DOXA does not wait for a second keystroke the way tmux's
`display-panes` does. It fires even when the digit names no group, which
is when it earns the most: `ctrl+7` in a two-group window shows `1` and
`2` and moves nothing. A window with one group shows nothing at all,
because there is no choice to make. Any following key takes it away.

**`ctrl+<digit>` cannot be sent by every terminal.** Under the legacy key
encoding `ctrl` has a code only for the 26 letters and ``@ [ \ ] ^ _ ?``
and space; a digit produces no byte at all, so these keys work on
terminals speaking the kitty protocol and do nothing elsewhere. `/help`
and `/doctor` say so. **`/pane <n>` is the door that always works**, and
`/pane` with no number just flashes them.

`/movepane <n>` moves this group's active tab into another group. The
session does not restart, stop or fork — Textual cannot re-parent a
mounted widget, so the tab is re-created at the destination and the live
engine handle is re-seated onto it, which is possible only because the
session lives in the daemon and never in the widget. It is refused, with
nothing changed, when the tab is the last one in its group: that would be
a close and a move at once, and the two have different undo stories.

Two split refusals, each changing nothing: a group may be split **twice**
(`SPLIT_SLOTS`), which is what gives the 2×2 the design is written around
— each new group is born with its own fresh allowance, so there is no
fixed ceiling on groups, only on how deep one lineage goes — and a split
that would leave either side too small is refused with the number it
actually has. A refusal that performed a sliver would be worse than the
refusal. The size test is **per axis**, not both at once
(`layout.split_refusal`): a side-by-side split checks only that half the
width clears `MIN_LEAF_WIDTH` (**34 columns**), a stacked one only that
half the height clears `MIN_LEAF_HEIGHT` (**9 rows**). A stacked split
never consults the width, and a side-by-side one never consults the
height.

Where the refusal is *printed* depends on which door you used. `/split`
and `/vsplit` put it in the transcript as a block in the group it is
about; `ctrl+o` and `ctrl+n` raise it as an eight-second toast, because a
key press has no transcript line to attach to.

**A narrow group hides its own tab strip.** Two strips is more chrome
than one, so below **34 columns** (`GROUP_STRIP_COMPACT_COLS`) a group
draws its labels compactly and below **17 columns**
(`GROUP_STRIP_MIN_COLS`) it draws no strip at all. Both numbers are the
same measurement: a tab header costs its label floor (`4 + " · " + 6`
from the model/repo minimums) plus the provider glyph and Textual's own
one-column padding each side — 17 columns for one header, 34 for the two
a strip is actually *for*. The narrowest group DOXA will create is 34
columns, so it sits exactly on that boundary. Width is not the only rule:
**a group holding one tab draws no strip at any width**, because a strip
of one is a label for something already unambiguous.

`ctrl+w` closes the **active tab** of the focused group, detaching its
session as it always has. Closing a tab closes **one** session — through
v0.95.0 closing a tab that held a three-way split ended three. When it
was the group's last tab the group goes with it, the split collapses, the
survivors take the room back, and the nearest remaining group takes the
keyboard. Closing the last tab of the last group closes the app.

## The session sidebar

`f3` (or `/sidebar`) shows a **collapsible rail down the left of the
window**: every session this window knows about, in one list, with the
state marks the tab strips already carry.

That last clause is why it exists. A session in a **background tab of an
unfocused group** is invisible today — its `done` dot, its needs-input
blink and its staged tint are painted on a tab header you are not looking
at. The rail is the one surface that can show all of them at once, and it
is the answer to the v0.99.0 lost-turn report in its general form: not
"the scroll was lost" but "you had no way to know anything had happened
over there".

The rail is **not a pane**. It is a sibling of the whole layout tree, so
splits, `alt+←/→/↑/↓` growth, `ctrl+shift+arrow` focus and `ctrl+1…9` never
see it, and it is unaffected by every split and by which group has focus.
Opening it changes the tree's width and nothing else. It is deliberately
**not focusable** either — clicking a row moves the keyboard to that
session, never into the rail.

**Marks are read from one place.** A row carries the same four classes a
tab header does (`-done-unseen`, `-staged`, `-working`, `-attention`),
through the same derivation, resolved by the same stylesheet cascade in
the same order — so the rail and the strip cannot disagree. The rail
additionally spends **two columns on glyphs**, which a strip has no room
for: `✓` a turn finished unseen, `+` staged proposals, `▸` working, `⏳`
**waiting for you**, and in a second column `⧉` for **context at or past
50%**. The needs-input blink blinks here too.

Those two — waiting for you, and half the window gone — are the two states
worth interrupting for, and they are glyphs so that the rail still says
them on a monochrome terminal, in a screenshot, and to a reader who cannot
separate the colours. Two, not a scale of five: a scale is a gauge, and
the `ctx` chip already is one.

**A session whose context limit was never reported gets no ctx glyph at
all** — not the absence of a warning, which would read as "plenty of
room". That is `/context`'s `?` rule one level down: DOXA does not guess a
window size, here either.

**An entry is a pane group, and its rows are that group's tabs.** A pane
group owns its own tabs, so one visible pane can hold three sessions of
which two are invisible — and the invisible one waiting for you is exactly
what the rail exists to surface.

So a group gets a **heading row** carrying the *most urgent* state over
all of its tabs, the invisible ones included, and a count: `·3` means three
tabs and what you see is what is on screen; **`·2/3` means three tabs and
the state came from the second one, the one you cannot see.** Underneath
it sit its tabs, one row each, carrying **their own** marks and not the
heading's roll-up. A **one-tab group gets no child row** — one tab is the
heading's own subject, and a row repeating it is noise.

Three gestures, and they are deliberately not one:

| click | does |
|---|---|
| a **tab row** | switches that group to that tab and focuses it — the one gesture that changes what is drawn |
| a group **heading** | focuses the group and leaves its active tab exactly where it is |
| a heading's **caret** (`▸`/`▾`) | folds the group's tab rows away. Remembered per group, across restarts |

None of them is rail-only. `ctrl+←/→` cycles the focused group's tabs,
`ctrl+1…9` and `/pane <n>` focus a group, and folding hides rows rather
than taking anything away — so closing the rail with `f3` costs you no
capability.

**Hovering a row highlights it** — every row, not only the headings.
Presentation only: the rail is not focusable and hovering never rebuilds
it.

**Colour says which PROJECT, never which state.** Each repo gets one of
six named colours — teal, sky, rose, clay, moss, mauve — assigned by a
stable hash of the repo root, so the same repo is the same colour on every
machine with nothing stored anywhere. Sessions auto-group under their
project's heading (a manual collection overrides that for the sessions it
names). Override a colour by NAME in `~/.doxa/config.toml`, never by hex:

    [projects]
    "/home/me/src/doxa" = "teal"

Six names and a hash means two projects eventually share a colour. That
costs redundancy, not meaning: the project's **name** is the primary
channel, grouping is keyed on the repo and never on the colour, and two
same-coloured projects stay two separate named headings.

**Grey means exactly one thing: no project colour.** A session outside a
repo has no project, so it has no colour. **Age is a separate channel and
it dims** — an ended session's row loses contrast but keeps its project's
colour, faded. "Old" means *ended*, and deliberately not *detached*: a
detached session is live and may be doing work right now, so it renders
`· closed` (its pane is gone) without being dimmed.

**Collections** group sessions under a name you choose. `group` already
means a region of the screen, so this word is different on purpose: two
sessions in one collection may sit in different pane groups, and one pane
group may show tabs from three collections. A session belongs to **at most
one** collection; the rest appear under an unnamed `— ungrouped —` heading
that is always last and is not itself a collection. Click a collection
heading anywhere along it to fold it.

**The rail's right edge moves.** Drag it with the mouse, or press
`alt+shift+←` / `alt+shift+→`, or run `/sidebar width <n>`. The width is
remembered in `sidebar_width`. A drag **refuses at the same floor opening
the rail refuses at** — it stops rather than squeezing a pane below its
own minimum, so the mouse cannot build an arrangement DOXA will not
create for you.

The edge **lights up when the pointer is on it**, and stays lit for the
whole drag. That is deliberately the only affordance it has: a GUI would
say "draggable" by changing the mouse pointer to a resize arrow, and DOXA
cannot. The sequence that would do it — `OSC 22` — is unimplemented in a
large share of terminals including Warp, is write-only everywhere except
kitty (so there is nothing to ask before writing it), and Textual 5.3
offers no API for it. DOXA does not emit escape sequences it cannot
verify a terminal accepted, so the highlight carries the whole message
instead. If your pointer does not change shape over the divider, that is
DOXA declining to guess, not a bug.

| command | does |
|---|---|
| `/sidebar [on\|off]` | Show or hide the rail; with no argument, toggle (`f3`) |
| `/sidebar width <n>` | Set the rail's width in columns; `wider` / `narrower` step it (`alt+shift+←/→`) |
| `/collection` | List the collections and how many sessions each holds |
| `/collection new <name>` | Make an empty collection |
| `/collection rename <old> <new>` | Rename one |
| `/collection delete <name>` | Drop the grouping — its sessions become ungrouped, **not** closed |
| `/collection add <name>` | Move **this** session into that collection, making it if needed |
| `/collection remove` | Take this session back out |

**Why `f3` and not `ctrl+b`.** `ctrl+b` is the conventional sidebar key
everywhere else, and it is what this feature's spec asked for — but it is
also tmux's default *prefix*, so a tmux user cannot press it at all, and
`doxa/app.py`'s own split-key subtraction had already listed it among
"the terminal's own" for that reason. `f3` follows `f2`'s precedent
(`/diff`): function keys go out as sequences every terminal since xterm
sends, so they are deliverable under both keyboard encodings, Textual's
own defaults claim none of them, and tmux passes them through. `/sidebar`
is still the door that always works, the same bargain `ctrl+,`,
`ctrl+tab` and `ctrl+1`…`ctrl+9` already ship on.

**It refuses to open on a window too narrow to hold it.** The rail is 25
columns by default (`sidebar_width`, clamped to 22–41) and a pane needs 34,
so below **56 columns** it cannot open at all — and it also refuses when
opening it, or widening it, would take the narrowest pane group below 34,
which is measured against the rectangles actually on screen rather than
against a constant. Both numbers come out of the same place the tab-strip
rungs do: a row's label floor is `4 + " · " + 6`, plus the rail's own nine
columns of padding, indent, caret and marks — measured against the
DEEPEST row it draws, a tab under a group heading under a project. It says
why, in the transcript, rather than squeezing a pane — and it opens by
itself the moment the terminal is wide enough again.

**With nothing to say, it stays out of the way.** On a fresh install
`sidebar` is *auto*: the rail appears once there is a collection or a
second session and not before, the same hide-at-zero discipline the
context chip, the side-by-side diff and the group tab strips follow. The
first `f3` writes the choice, and from then on it is yours.

**Collections are saved with the tab set**, in the same per-repo record,
and come back with the window. A collection whose sessions are all gone
does not; a member whose session is gone is dropped from it, the way a
dead pane is dropped from a saved layout. A member whose **tab** is closed
but whose session is still around keeps its row, marked `· closed` — the
rail is a session index, not a second tab strip. Clicking such a row tells
you `/attach` is how you get it back; **double-clicking it types the
command for you**, into the active pane's prompt, unsent. It waits there
to be read and edited — nothing runs until you press enter. Rows that are
open reveal on a double click exactly as they do on a single one, and a
session you reaped with `/sessions kill` has no row at all: reaping means
forget it, and it means it here too.

## The live diff

`/diff` (or `f2`) puts a **live diff of this session's worktree** in
the pane beside the session — `git diff` against the branch the worktree
was cut from, recomputed every time an edit lands and never on a timer.
Files are collapsed by default with their changed-line counts; binary and
very large files are named rather than rendered; a diff that hit a cap
says so, and the caps are 2000 hunk lines per file, 200 files, 20,000
lines in total and 50 untracked paths. Side-by-side turns on at **100
columns** (`SIDE_BY_SIDE_MIN_COLS`) and unified is the default below it,
because at 80 columns a half-width pane is 40 and two 20-column sides are
unreadable.

Changed lines are drawn with a **background**, not just a coloured
foreground — removed rows red, added rows green — and each row carries
its **line numbers down the left**: both the old and the new number in
unified (only the relevant one filled per row), one number per side in
side-by-side. The numbers themselves are green for an added line and red
for a removed one, and they sit outside the wash so they stay readable.
A file's fold carries its `+42 −7` in the same two colours.

The diff pane is a real layout leaf: `ctrl+shift+←/→` moves the keyboard
into it and back, `alt+←/→` widens it, it keeps updating while you type
in the session, and its position is saved and restored with the rest of
the tab's layout. A second `/diff` closes it. Each session has its own.

**You do not have to open it to know there is something in it.** When the
worktree has changes the status bar carries a `diff 3 files +42 −7` chip —
clickable, and the click is the same toggle `f2` is — and it is hidden
when there is nothing, like every other chip on that row. Two states are
not "nothing": a worktree whose recorded base is its own branch cannot be
diffed at all and reads `diff ⚠ no base`, and a git that refuses reads
`diff ⚠ unreadable`. A session with no worktree base recorded is diffed
against `HEAD`, and the chip says `vs HEAD` because that is a smaller
claim. The counts are `git diff --numstat`, recomputed on the same edit
that ticks the pane and at no other time.

`auto diff` (settings, **off** by default) opens the pane by itself the
first time a session edits the worktree — **once** per session, so
closing it is final. It never takes the keyboard away from the prompt,
and on a window too narrow to split it says so instead of making an
unusable sliver.

**Reject** on a hunk does two things, in this order: it reverse-applies
exactly that hunk (a second hunk in the same file is untouched), and it
tells the session's agent what was rejected, in your own words if you
typed a reason. That message goes down the same path a prompt you typed
does — it is you speaking, not another session, so it is not wrapped in
the untrusted-peer framing a `/msg` from a peer gets.

If a turn is running, the rejection is **queued and visibly marked**, and
applies when the turn ends: reverting a file under an agent that is
mid-edit produces a conflict neither side understands, and the daemon
refuses a second concurrent prompt anyway. If the reverse patch no longer
applies — the file moved underneath it — nothing changes and the pane
says why. Closing a diff that still has queued rejections is refused
rather than losing them. Queued rejections do not survive a restart; the
diff comes back showing the hunk still there, which is the truth.

Two cases the pane distinguishes on purpose. **"No changes"** means git
was asked and answered nothing. **"Cannot determine a base"** means the
worktree's recorded base is its own branch, so nothing it committed could
appear in a diff against it — the same defect that in v0.33.0 made
`commits_ahead` read zero and force-deleted real commits. An empty diff
and an unanswerable one must not look alike. The design is
[docs/plans/live-diff.md](plans/live-diff.md).

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

## Where a session is

`/dir` reports the directory this session is actually rooted in — the
literal cwd its engine was booted with, which is what every one of its
tool calls resolves a relative path against. Since v0.17.0 that is
usually a DOXA-managed worktree rather than the directory you launched
in, so a worktree session's answer also names the repo it was forked from
and the base it is on.

`/cd <path>` **opens that path in a new tab** and says, every time, that
this session was left where it was. That is the only honest reading of
"change directory" here: the `claude` CLI subprocess behind a running
session was spawned with an operating-system cwd, no SDK control request
exists to hand a running process a new one, and repainting only DOXA's
own bookkeeping would make the status bar claim a location none of the
session's tool calls are touching. It is the same mechanism `/resume` and
the repo chip's directory picker already use. Bare `/cd` explains this
rather than doing nothing, and names where the session stays.

Outside a git repository the status bar's leftmost identity chip is
`dir NAME` — the directory's own basename, with no `⎇` and no branch
half. It is deliberately a **different shape** from the git chip's
`repo ⎇ branch @sha` rather than the same shape with the branch missing:
"a repo, on branch X" and "a plain directory" have to read as different
facts. Before v0.93.0 there was no chip at all there, so a session
started outside a repo had nothing on screen saying where it was.
Clicking it opens the same repo/directory picker the git chip's repo
half does.

## Permission modes

The `mode:` chip leads the status bar (first position, so it is never
crowded off the end of a narrow row) and names the session's permission
mode — what still stops and asks before a tool runs. The one case it
stands down is a chip that would read `default` on a row under
`MODE_CHIP_MIN_COLS` (110): every mode that is *not* `default` is painted
at every width, because those are the ones worth the columns.
`shift+tab` cycles it,
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

## Containment

Permission modes decide what stops to ask you. Two mechanisms sit under
them and do not move when the mode does.

**The tool gate.** Every tool call passes `doxa/gate.py`'s `ToolGate` at
the SDK's `PreToolUse` boundary, built-ins included. When the session
declares an allowed set, a call outside it is denied there and the model
is told why. DOXA-native operators are re-checked a second time in
`execute()` — defence in depth at a choke point, never one layer.

**Two strikes.** Every failure comes back as an ordinary
`{"error": ...}` result the model can read and retry; what differs is
whether the gate *counts* it. `is_hard_failure` counts exactly two
shapes: a result whose error says the tool is `not configured`, and one
that opens `<name> failed:` — which is what any exception an operator
raised turns into, sync or async. A known tool refused by the session's
allowed set is counted directly, at the hook and again in the executor,
because a repeatedly-refused call is the strongest "stop calling this"
signal available. The **second** hard failure of the same tool disables
it for the rest of the session, and the disabled names collect in the
status bar's `⊘` chip.

Two things that look like failures and are deliberately **not** counted:
bad arguments (a `TypeError` from the backend comes back as
`bad arguments for <name>: …`) and an unknown tool name, which returns
the list of names that do exist. Both are recoverable mistakes and must
stay retryable. Every `spawn_session` cap refusal is soft for the same
reason. Nothing here is persisted: `ToolGate`'s whole state is two
in-memory fields built fresh per session.

**Nothing auto-denies silently.** A headless SDK run with no callback
auto-denies an `AskUserQuestion` and a permission request without telling
anyone. DOXA gives each one a real dialog and blinks the tab that raised
it. A *desktop* notification is opt-in — `notify_needs_input` is **off**
by default (see [Settings](#settings)); turned on, a fully detached
session always notifies. The transcript records the answer either way.

### Session spawn — off unless you turn it on

With `spawn_sessions` on, the model gets one extra tool,
`spawn_session`: it starts a **second daemon-backed session in the same
repository**, gives it a task, and returns as soon as that session exists
— delegation, not a blocking call. The child is a full peer with its own
`claude` process, its own worktree and `doxa/<short>` branch, and its own
LORE context. It does not receive this session's transcript or state, and
it cannot send anything back; what "comes back" is its commits on its
branch, which you read with `git log` and `git diff` like any other
session's.

**It is off by default, and the setting lives only in
`~/.doxa/config.toml` (or `DOXA_SPAWN_SESSIONS` in your own shell).**
Nothing inside a repository you open can turn it on — a repo that could
would be arbitrary code execution on `doxa new` against a clone you have
not read. While it is off, the tool is not offered at all: the model
never learns the name exists.

Turning it on costs real machine and real money per spawn — another
`claude` process (~294 MB RSS, measured once by the suite's own
leaked-process reaper and recorded as prose, not computed anywhere),
another worktree (18 MB, which *is* a constant:
`WORKTREE_CHECKOUT_BYTES`), and a second token bill additive to this
session's. DOXA does not aggregate cost across a fleet.

Every call **stops and asks you**, showing the exact task text the child
will be given, in every permission mode except `bypassPermissions` —
including `auto`, which is the one deliberate exception to that mode
handing tool decisions to a classifier. Under `plan` no tool runs at all,
which is the `claude` CLI's own behaviour rather than a block DOXA
enforces: nothing in `doxa/session_ops.py` tests for that mode.
Independently of the mode, three caps are enforced inside DOXA before any
process starts: spawn **depth 2** (`MAX_SPAWN_DEPTH`), **3 live
sessions** per repo (`MAX_LIVE_SESSIONS`), and **1 spawn per 60 seconds**
(`MAX_SPAWNS_PER_WINDOW`), plus a preflight refusing to start below
**425 MB** free — 18 MB for the worktree and 407 MB for the session it
will build. Disk it cannot measure is not a refusal. A cap saying no is a
soft refusal the model can read; it never counts toward the two-strikes
disable above.

Two honest limits. An agent with the `Bash` tool can already run `doxa
new` itself — the gate cannot see inside a shell string, so that path is
counted by the live-session cap but is not gated and records no parent.
And `peer_left` tells you a delegate is *gone*, never whether it
succeeded. The design, including what is deliberately not built, is
[docs/plans/spawn-session.md](plans/spawn-session.md).

### Remote drivers — a policy, and no transport

Nothing listens on a network. `doxa/remote_policy.py` (v1.8.0) is the
**authorization decision** a future bridge process will ask, shipped
ahead of the bridge on purpose: a reachable daemon socket is remote code
execution with your privileges, so the answer is decided once, in one
module a security review can read start to finish, rather than
re-derived at each call site a bridge would grow. There is no socket, no
TLS and no header parsing in it, and there is no second renderer
anywhere.

Three independent questions, and every answer is a `Decision` carrying a
reason — never a bare bool, because a refusal that cannot say why is a
refusal nobody can act on:

- **May a listener exist at all?** `remote_enabled` is off by default.
- **Is this identity one DOXA trusts?** `identity_decision` refuses
  outright unless the request arrived on the loopback listener
  `tailscale serve` forwards to, whatever login the caller claims. DOXA
  then keeps its **own** allow-list on top of the tailnet's, and an
  **empty allow-list refuses everyone** — it does not fall back to
  permitting everyone, which is the direction allow-list bugs usually
  fail in.
- **Is this kind of request on the remote surface at all?**
  `request_kind_decision`. The remote surface is deliberately **smaller**
  than the local one: reading the transcript and status, sending prompts,
  and approving or denying a pending tool call are granted; running a `!`
  shell line and raising the permission mode to `bypassPermissions` are
  refused unless `remote_allow_shell` / `remote_allow_bypass` say
  otherwise, both off by default. `remote_allow_bypass` is a **separate**
  gate from the local `allow_bypass`, not the same one: that one arms this
  session's CLI to reach the mode at all, this one decides whether a
  request that arrived over the network may ask for it. Both have to be
  open.

The one surface a user sees today is the status bar's `◎ remote:<id>`
chip, hidden until something is driving the session. That is the spec's
"say who is connected" rule: a silent second driver is the thing a user
cannot detect and cannot consent to. Nothing can set it yet, because no
bridge exists to pass an identity in.

## The status bar

**Twenty-two chips** are built in paint order by
`doxa/session/chips.py`, and a row never shows all of them: a chip whose
number is zero, or whose state was never asserted, is omitted rather than
shown empty, and a chip whose engine cannot report the thing it names is
not painted at all rather than painted blank (see
[Engines](#engines)). Every chip carries a tooltip on hover, including
the plain, non-clickable ones and the git chip's inert `@sha` span.

| chip | shows | clickable |
|---|---|---|
| `mode:` | permission mode (see above); hidden when the engine has no permission modes, and when it would show `default` on a row under 110 columns — every other mode is painted at every width | yes — mode picker |
| `◎ remote:<id>` | a remote driver is attached to this session. Hidden at zero, and there is no companion "local" chip: the absence says it (see [Remote drivers](#remote-drivers--a-policy-and-no-transport)) | no |
| model | the model handling this session's turns | yes — model picker, takes effect next turn |
| `⚑ needs input` | a question or permission request is waiting on this pane | no |
| `effort:` | reasoning effort asserted at connect (hidden when none was) | yes — effort picker, affects future sessions only |
| repo/branch/sha | the git chip: repo name, the worktree's session branch, sha | yes — repo and branch halves each open their own picker; `@sha` is inert but tooltipped |
| `dir NAME` | the folder chip, shown **instead of** the git chip when this session is not in a git repository at all (see [Where a session is](#where-a-session-is)) | yes — the same repo/directory picker |
| `diff N files +A −B` | uncommitted work in this session's worktree, recomputed on the edit that ticks the pane; `vs HEAD` when no base was recorded, `⚠ no base` / `⚠ unreadable` for the two states that are not "nothing", and a short `diff Nf +A −B` under 110 columns (see [The live diff](#the-live-diff)) | yes — the same toggle `f2` is |
| `⇅ sync <age>` | LORE sync: how long since a pull landed here, `↑N` local ops not yet acknowledged, `⚠N` conflicts plus ops that failed their integrity check and were staged. Amber when that last number is non-zero, because it is the one waiting on a person. Absent — not a zero, not an error — when sync is off, when `lore_core` has no op log, or when the store predates the `sync_*` tables. LORE owns the transport and the merge rules; DOXA owns this chip and the two record types it keys by machine (see [LORE integration](#lore-integration)) | no |
| `sub:<tier> (≈$…)` or `$…` | subscription tier with a list-price what-if, or the real API spend on API-key auth. Both hidden on an engine that reports no cost | no |
| `s:N% w:N%` | subscription session (5h) and weekly utilization, cached by the `claude` CLI itself; a third scoped segment appears when one is published, and a trailing `~` means the reading is stale | no |
| `ctx N%` | context window usage, amber at 70%, red at 90%; `ctx —` while an engine that *can* report one has not yet, and hidden outright on an engine that never will. `ctx_absolute` adds `24k/200k` inline, and that segment needs 100 columns of its own | yes — confirms, then `/compact` |
| `N beliefs` | active LORE beliefs for this session; painted at zero too, because zero beliefs is a fact | yes on an engine carrying the belief pickers, plain on one that is not |
| `mem u%p%` | curated-memory fill, user and project, as two separate percentages | no |
| `N proposals` | staged LORE proposals awaiting review (hidden at zero) | yes — pending-proposals picker |
| `⧉ N agents` | Task-spawned subagents currently running (hidden at zero) | no (see subagent row below) |
| `⌁ session <id>` | this session's reattach handle (only while attached to a daemon) | yes — sessions picker |
| `peers N (k⌁)` | other DOXA sessions on this repo; `k⌁` is how many are detached | yes — peers picker: each row is the peer, the beginning of its transcript, and tokens consumed so far (self-reported, up to one heartbeat stale) |
| `↑●` and `↓◌` | **two chips**, the modem lights: one for peer messages sent, one for received. Filled for four seconds after the traffic that lit it, hollow after. They appear as a pair once anything has crossed in either direction and never one at a time, so arriving traffic cannot shift the row sideways as you read it; a session that has never touched a peer carries neither. Counts and the time since are on hover | no |
| `⌗ mesh :<port>` | the message-graph server is running on loopback for this window (`/mesh`). Hidden at zero, like the peers chip beside it: a loopback server serving full message bodies is a second surface you started and can forget, so it says so while it is up (see [Fleets from the TUI](#fleets-from-the-tui)) | no |
| `⊘ <tool>` | every tool disabled after two failures this session, space-joined into one chip | no |

A `⧉ N agents` chip is accompanied by a second row under the status bar
with one clickable entry per running subagent; clicking one opens a
read-only transcript tab mirroring that subagent's own narration and tool
calls. Once the parent `Task` call finishes, the same activity becomes a
foldable tree under the parent tool-calls chip.

`/context` leads with a 10x20 grid of the window (Claude Code's own look:
draughts glyphs by default, `[#]`/`[ ]` ascii behind the `context_grid`
setting for a terminal font that tofu's them — `context grid` in
`/settings`), model and headline beside the top rows, a category legend
beside the lower rows, per-source summaries (MCP tools, agents,
adopted-plugin skills) below the grid, and the exact breakdown below all
of that — system prompt, tools, messages, free space, loaded `CLAUDE.md`
files, per-MCP-tool cost — using the `claude` CLI's own accounting, the
same measurement the `ctx` chip reads. No reported window size means no
grid, the same way it means no percentage; unlike a stretching bar the
grid never draws smaller, so a pane too narrow for its own fixed width
drops it and keeps the numbers alone. `/usage` prints the same cost and
utilization figures the status bar chips show, with separators.

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
count — **9000 user, 8800 project** on `lore_core` 0.55.0, overridable
with `LORE_USER_CAP` / `LORE_MEMORY_CAP`. The caps live in `lore_core`,
not in DOXA, so a LORE pin bump can move them; the status bar's
`mem u%p%` chip reads `memory_cap(scope)` rather than a number of its own,
which is why it cannot disagree with `lore status`.

**Beliefs** are an uncapped store with an FTS index and evidence trails.
At act time, one FTS pass over the prompt may attach a single belief as a
citation (`consult_floor`, default relevance floor 1.0; 0 disables it) —
labelled CITE-ONLY, never injected as fact. The model's entire memory tool
surface is six operators (`doxa/operators.py`): five read-only —
`lore_belief_search`, `lore_belief_show`, `lore_belief_neighbours`,
`lore_memory_list`, `lore_session_search` — and one write,
`lore_remember`, which only **stages a proposal** into
`$LORE_ROOT/pending/` — it never writes directly into memory. They reach
the model as `mcp__doxa__<name>`.

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
- **The graph**, per belief: `g` on a highlighted belief row shows that
  belief's own graph neighbourhood — the relations LORE has recorded
  about it, and theirs. `graph_view` picks the rendering: `ascii` folds
  LORE's own edge block (arrow for direction, the other belief's id, who
  asserted it, and the distinct-session support count) in under the row,
  the same way `Right` folds evidence; `browser` (the default) writes
  LORE's pan/zoom mermaid page under `~/.doxa/graphs` and opens it,
  printing the path into the transcript either way so a headless or SSH
  session still gets the file. That page needs network the first time it
  is opened (mermaid loads from a CDN), and because a `file://` page is a
  null origin some browsers refuse that fetch from, DOXA serves it over a
  loopback-only HTTP server instead — token-gated, so a co-tenant on a
  shared machine cannot read your beliefs off the port. Nine beliefs in
  ten have no recorded relation at all (745 of 799 on the store this was
  measured against), and those say `no relations recorded` rather than
  opening an empty page. Deliberately **per belief and never
  whole-graph**: the whole graph, filtered to asserted relations,
  fragments into dozens of disconnected clusters that mermaid stacks into
  a strip fitting on screen at 5%; a k-hop neighbourhood is connected by
  construction. `g` is the one belief control that writes nothing, so a
  session whose `lore_core` is too old to record an outcome keeps it, and
  a `lore_core` too old to draw at all says which function is missing
  instead of failing.
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
stale/retract/graph (beliefs) or approve/reject (proposals) reachable
without leaving the list. Click the action span on a row, or press its
letter (`a`/`r` for proposals, `y`/`c`/`s`/`r`/`g` for beliefs) while that
row is highlighted; approve and retract still arm on the first press and apply
on the second, on the same row. Selecting a row outright (Enter, or a
click that misses every action) opens a per-row action menu carrying the
same verbs one selection deep — the inline controls are a faster path
alongside it, not a replacement. While either picker is open, the prompt
line filters its rows instead of sending to the agent; typing narrows the
list a beat later (the rebuild debounces, so a fast typist gets one
settled query per word rather than one per letter — a live `/query …`
marker in the picker's own border shows a filter is pending until it
does), `Right`/`Left` expand and fold a belief's evidence, Enter acts on
the highlighted row, Esc closes and clears it. The six action letters
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

**Streaming review.** A background reviewer runs over the live transcript
between turns — at most once every `derive_secs`, **900 seconds by
default** since v0.98.0 — and stages whatever it judges worth remembering,
behind the same approval gate as everything else. It never blocks a turn:
its one trigger is turn-done, it refuses to start while **another review**
is still running or while the session is finalizing, and `finalize()`
waits for an in-flight review rather than racing it. So a quiet session
pays nothing and a busy one pays at most four reviews an hour. It does
not check whether a turn is running, because it is only ever scheduled
when one has just ended.

Each review shells out to a headless `claude -p`, so it is a real cost.
`derive_secs = 0` (or `off`) turns it off and leaves review where it was
through v0.97.0: at `PreCompact` and at session end. Those two always run
regardless, and honour `LORE_DISABLE_REVIEW` the way LORE's own hook does.

Why it defaults on: a session that runs for hours and ends without a clean
finalize used to derive **nothing at all**, because review fired only at
compaction and at the end.

**Typed edges between beliefs.** Since LORE 0.41.0 the store carries
relations as well as beliefs, derived the same way the beliefs are, in
five asserted verbs: `depends_on`, `specializes`, `explains`,
`contradicts` and `applies_when`. Support is counted in **distinct
sessions**, so one session repeating itself does not manufacture
agreement, and a path's confidence is the **product of its hops**, so a
long chain of individually plausible steps is weak by construction.

Structure earns no authority. A belief reached by following an edge is
still CITE-only unless it earned STEER on its own — the graph changes what
the agent can *find*, never what it may *act on*. DOXA has this today
because it imports `lore_core` in-process; the only interface onto it is
the beliefs picker's `g` action, which opens one belief's neighbourhood
(`graph_view`, see [Settings](#settings)). Nothing else surfaces it yet.

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
its history reloaded, on the engine it originally ran on — bare, it lists
recent conversations to pick from. It refuses, in words, before spawning
anything, and the question it asks is the one that session's OWN engine
would ask: if the conversation is still running (attaches instead); if its
directory is gone; if it is a `claude` conversation predating v0.56.0
(before DOXA and the `claude` CLI shared one session id, so the CLI has no
history to resume from); or if it is a Codex conversation with no recorded
thread id. Such a conversation stays searchable and readable, never
resumable. One case gets past that gate and refuses at the engine instead,
after the daemon process has started: a Codex conversation whose thread
record the engine cannot find where it looks for it (beside the transcript
of the directory the tab is being reopened in).

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
display and reaches the model only behind an untrusted-peer preamble.

**The model can message peers too, and it is off by default.** Until this
release the manual said the model had no send tool and that every peer
message crossed because a human typed `/msg`. That sentence is now a
setting rather than a fact, and the setting starts off. Turning on **let
the model message other sessions** (`agent_peer_send` /
`DOXA_AGENT_PEER_SEND`) offers three tools:

- `peer_list` — who is running, across every repository you have open,
  with what each one says it is and how long it has been up;
- `peer_send` — one message to one session, or to all of them at once;
- `peer_history` — this session's own sent and received traffic, so an
  agent can see that a peer has asked it the same thing four times and
  stop answering on its own judgement.

`peer_list` and `peer_history` are read-only and available either way;
only sending is gated. What the switch actually grants is worth stating
plainly: a model that can send can reach another live session's context
on its own initiative, in a repository you did not open this session in.
Every guard below exists because of that, not as ceremony.

- **Nothing is silent.** Every send — the model's and yours — is appended
  with its full body to `$DOXA_HOME/peers/messages.jsonl`, the file the
  mesh graph draws from, and flashes a light on the status bar. There are
  two lights, one for sent and one for received; they decay to dark after
  a few seconds and carry the counts on hover.
- **Sending is rate limited by DELIVERIES, not by calls.** A broadcast to
  31 peers costs 31. A refusal tells the model why and when the budget
  frees up, because an agent told why can send to fewer peers or wait,
  and one silently throttled just retries.
- **Addressing does not guess.** Name a session by its full id or by a
  prefix matching exactly one; an ambiguous prefix is refused, listing
  every candidate. The sender's repo travels with the message and is
  shown on arrival.

**Being woken by a message is a second switch.** *Let an arriving message
start a turn* (`peer_inbound_turns` / `DOXA_PEER_INBOUND_TURNS`), also off
by default, decides whether an incoming message starts a turn when this
session is idle — it queues behind a running one either way, in the same
bounded FIFO a prompt you type mid-turn goes through. Accepting messages
and being woken by them are different grants, which is why they are
different switches. A **broadcast never starts a turn** at any setting: at
thirty-two sessions one broadcast would otherwise wake the whole fleet in
a single step. A turn a peer started says so in its own first line, mounts
a block above itself naming the sender, and carries a `peer-` turn id into
the ledger — so spend that began with an inbound message has a traceable
cause.

### Fleets from the TUI

`/fleet start` runs the [fleet harness](fleet.md) from a session, with the
**same flags** `doxa-fleet` takes — one parser, `doxa.fleet.build_parser`,
so a line that works in a shell works here and the two cannot drift. The
only difference is `--cwd`, which defaults to this session's own repo
rather than to the process's directory.

```
/fleet start --pool claude:sonnet@1 -n 4 --run-budget 5 --prompt "…"
```

The run goes on the TUI's event loop and opens a **read-only tab** named
`fleet <run-id>`: the capacity arithmetic and the budget note it started
under, the assignment table (slot, engine, model, memory on/off, phase,
session id, error), the dispatch spread, the quiescence state with elapsed
time, the last thirty ledger lines as `t+s  from → to  body`, and at the
end the leaked-pid report and the manifest path. The tab reads the run's
own **manifest and ledger** and nothing else — the run rewrites its
manifest on a heartbeat while it is live — so a refresh never touches state
the orchestration is mutating. A refusal that fires before any manifest
exists (the capacity arithmetic, a missing run budget, a run root too deep
for a Unix socket) is the tab's first line rather than a traceback.

| verb | does |
|---|---|
| `/fleet` | The verbs, and whether a run is live in this session |
| `/fleet start …` | Spawn a run and open its tab; `--dry-run` prints the arithmetic and spawns nothing |
| `/fleet status` | The same report, in the transcript |
| `/fleet stop` | Tear it down now, through the same teardown the quiescence deadline takes; the tab keeps the final report |
| `/fleet runs [root]` | Past runs under the root — id, started, n, state, ledger count — read from their manifests |
| `/fleet attach <slot>` | Open one slot's session in a live tab |
| `/fleet mesh` | Graph this run's ledger (see below) |
| `/fleet detach` | Leave the run going when its tab closes |

`/fleet attach` goes through the run's **manifest**, not the peer registry:
a run gets its own `DOXA_RUNTIME_DIR` precisely so the registry it
discovers is the fleet and not your own editor session, which means
`/peers` cannot see one of its sessions at all. The manifest records each
slot's socket, and that is the handle the attach uses. What you type in
that tab is a message into the run, and the run's ledger records it like
any other.

**Closing the fleet tab tears the run down.** A fleet is not a background
service: it keeps N daemons alive, arms every one of them to be woken by
another's message, and so keeps spending with nobody typing. `/fleet
detach` is the explicit "keep this running" gesture — the same distinction
`/detach` draws for one session — and the tab's header says which of the
two states it is in.

**`/mesh` draws the graph.** `doxa/meshgraph.py` serves the ledger as a
live browser view of which session messages which, a graph being the one
artifact a terminal is honestly bad at. Bare, `/mesh` graphs **this
machine's** peer ledger; with a run id (or an unambiguous prefix of one) it
graphs **that run's**, which is a different file because a run gets its own
`DOXA_HOME`. It binds loopback only, gates every route on a per-process
token that is never written to disk, and prints its URL — a `⌗ mesh` chip
sits on the status bar while it is up, and `/mesh stop` ends it and
releases the port. It opens a browser only when `mesh_open_browser` is on
(off by default): DOXA runs in terminals that have none — over SSH, in a
container, on a headless box — and the URL is printed either way.

### Spend ceilings

Both switches above hand something other than you the ability to spend
this session's money, so there is now a bound on it.
`session_budget_usd` / `DOXA_SESSION_BUDGET_USD` is **off by default** —
unset, nothing about any session changes.

Set it and the session stops **starting** turns once it has spent that
much. It says so in the transcript, naming what it spent, what the
ceiling is and how to lift it; every command still answers; and raising
the number (Ctrl+, → Session, the config file, or the environment) lets
the very next prompt through with no restart — the ceiling is read per
turn, not captured at connect. A turn an **arriving peer message** would
have started is refused the same way, which is the path it exists for,
and the message is not lost: it rides the next turn that runs.

Two things it does not do, both on purpose:

- **It bounds starting a turn, not a turn in flight.** The only dollar
  figure that exists arrives with the message that *ends* a turn, so a
  session can exceed its ceiling by the price of the one turn that
  crosses it. DOXA will not multiply tokens by a price sheet it would
  have to maintain in order to pretend otherwise.
- **It cannot be enforced on an engine that reports no cost.** `codex`
  and both API vendors report token counts and no dollars, so their spend
  reads as `$0.00` and a ceiling compared against it would never fire.
  The settings row says so when you set it, and a session that starts
  under such a ceiling says so too, rather than looking like a control.

For a fleet, set the run-wide total instead — `doxa-fleet --run-budget`,
divided into a per-session ceiling, because thirty-two individually
reasonable limits multiply into one unreasonable one. See
[docs/fleet.md](fleet.md).

Each `/peers` row also carries what that session *says* it is:
`self-reported: sonnet via claude on doxa` — its model, its provider, and
the engine hosting it. Read that line as a claim, because it is one:
another process wrote it, and DOXA neither verifies it nor lets anything
act on it. It is there for the same reason a peer's title is — to help you
decide which session to `/msg` — and for nothing else. A session that does
not publish one of the three prints `?` in its place, and one that
publishes none prints `self-reported: unknown`; you will never see a
plausible-looking default standing in for something nobody measured.

## Keyboard protocol

Textual's Linux driver requests the kitty keyboard protocol at startup but
never reports whether the terminal granted it. DOXA asks the terminal
itself once, before the TUI takes over the keyboard (`\x1b[?u` plus a
Primary Device Attributes sentinel), and reports the answer on `/about`
and in `/doctor`. A binding this terminal cannot physically send (under
the legacy encoding there is no byte for `Ctrl+,` or for distinguishing
`Shift+Enter` from plain Enter) is marked `✗` in `/help`. Silence from the
terminal reads as **not measured**, never as "legacy".

`alt+<letter>` joined that list in v0.95.0, and reachability there is a
fact about **Textual**, not about the terminal: the terminal does send
Alt, as an ESC prefix, and `textual/_xterm_parser.py` has no path that
turns an ESC prefix back into Alt. `alt+<arrow>` and `alt+<F-key>` use
the `CSI 1;3<final>` encoding instead and stay reachable.

On a terminal measured legacy, the opening block also carries a one-line
notice naming the affected bindings and the slash command that reaches
each one instead (e.g. `Ctrl+,` → `/settings`) — past a handful it names
the count and points at `/doctor` rather than the whole list. It says
nothing on a kitty-protocol terminal or one never measured, and
`key_notice` (default on) turns it off entirely.

## Commands

Every command below is defined once in `doxa/commands.py` and reaches the
palette, the `/` autocomplete and `/help` from that single registry, in the
six groups that registry declares. Five of them are below; the sixth,
**Plugins**, is built at runtime from whatever adopted Claude Code plugin
commands this session carries, and is omitted entirely when there are none
(see [The spawned CLI](#the-spawned-cli)).

**Session**

| command | does |
|---|---|
| `/model [name]` | Switch the model for the rest of this session (no reconnect); bare lists this engine's own catalogue |
| `/engine [id]` | Engine for NEW sessions and tabs, with what each one can do — never the running session |
| `/branch [name]` | List local branches (current base marked), or switch this session's base |
| `/mode [name]` | Permission mode; bare lists all six with what each does |
| `/effort [low\|medium\|high\|xhigh\|max]` | Effort level for new sessions only (connect-time); prompt-only, with no palette entry |
| `/usage` | Session tokens, turns, cost, subscription headroom |
| `/context` | What is occupying the context window right now, by component |
| `/clear` | Fresh session in this tab: finalize, rotate transcript, reset |
| `/sessions [kill <prefix> \| kill-detached]` | Every live session: name, age, attached — and how to kill one |
| `/resume [session-id]` | Reopen a past conversation in a new tab |
| `/dir` | This session's own working directory — where its tool calls actually run |
| `/queue [position-or-id]` | Prompts waiting behind the running turn, by position and id; an argument cancels one |

**Memory**

| command | does |
|---|---|
| `/beliefs` | Browse active beliefs — confirmed/contradicted/stale/retract inline, evidence on Right |
| `/pending` | Staged proposals — approve or reject inline |
| `/search <terms>` | Search every past session (live results as you type) |

**Panes & tabs**

| command | does |
|---|---|
| `/split` | A second session **stacked below** this pane (`ctrl+o`) |
| `/vsplit` | A second session **side by side** with this pane (`ctrl+n`) |
| `/diff` | This session's live worktree diff in the group beside it, or close it (`f2`) |
| `/pane [n]` | Jump to pane group `n`, numbered left to right then top to bottom (`ctrl+1`…`ctrl+9`); with no number, flash them |
| `/movepane <n>` | Move this group's active tab into group `n` — the session keeps running |
| `/sidebar [on\|off\|width <n>\|wider\|narrower]` | Show or hide the session sidebar (`f3`), or move its right edge (`alt+shift+←/→`) |
| `/collection …` | `new` / `rename` / `delete` / `add` / `remove` — group sessions in the sidebar under a name you choose |
| `/cd <path>` | Open that path in a **new** tab; this session stays where it is |
| `/peers` | Live sessions in this project right now |
| `/msg <session_prefix> <text>` | Send a message to one same-project peer session |
| `/fleet start\|status\|stop\|runs\|attach\|mesh` | Start and watch a fleet run — N sessions, one prompt, one instant, in a tab; `detach` leaves it running past its tab ([fleets from the TUI](#fleets-from-the-tui)) |
| `/mesh [run-id \| stop]` | Graph the message ledger in a browser — this machine's, or one fleet run's; loopback only, token-gated |
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
| `/plugins` | Your Claude Code plugins/skills: discovered, adopted or refused, and why (see [docs/plans/plugins.md](plans/plugins.md)) |
| `/reload-plugins` | Re-scan Claude Code plugins/skills now (new sessions/tabs only) |

**Maintenance**

| command | does |
|---|---|
| `/compact` | Ask the CLI to compact the transcript (runs LORE's review first); passthrough, not intercepted |
| `/update [--restart]` | Fast-forward this DOXA checkout from origin (never merges) |
| `/help` | Every command and key binding, generated from this registry |
| `/about` | Version, dependencies, platform and config path — what a bug report needs |

## Settings

Precedence everywhere: **environment > `~/.doxa/config.toml` > default**,
resolved in one place (`config.raw()`). The file is plain TOML, written
`0600` inside a directory clamped to `0700`. The settings modal (`ctrl+,`
/ `/settings`, seven category tabs — Session · Memory · Appearance ·
Notifications · Remote · Paths · About, walked with `shift+←/→`) shows
each row's effective value and where it came from. A row the environment
is winning is not merely marked: it gets **no input field at all**, and
says `(set by env — editing here would be shadowed; unset DOXA_X to use
the config file)`. A save against a config file that exists but will not
parse is refused rather than clobbering it.

| setting | env | default | what it controls |
|---|---|---|---|
| `engine` | `DOXA_ENGINE` | `claude` | which engine drives NEW sessions; the row's choices are the registry itself, so every registered engine is offered (see [Engines](#engines)) |
| `model` | `DOXA_MODEL` | CLI default | model for new sessions; `/model` switches the live one and writes this row |
| `effort` | `DOXA_EFFORT` | CLI default | reasoning effort, new sessions only |
| `allow_bypass` | `DOXA_ALLOW_BYPASS` | off | let new sessions reach `bypassPermissions` at all |
| `adopt_plugins` | `DOXA_ADOPT_PLUGINS` | off | load commands/skills/agents from your OWN installed Claude Code plugins into new sessions — never their hooks or MCP servers, never LORE (see [docs/plans/plugins.md](plans/plugins.md)) |
| `auto_diff` | `DOXA_AUTO_DIFF` | off | open the live diff by itself the first time a session edits its worktree — once per session (see [The live diff](#the-live-diff)) |
| `spawn_sessions` | `DOXA_SPAWN_SESSIONS` | off | offer the model `spawn_session`, which starts a second session in this repo and gives it a task (see [Session spawn](#session-spawn--off-unless-you-turn-it-on)) — read from this file and the environment only, never from a repository |
| `session_budget_usd` | `DOXA_SESSION_BUDGET_USD` | off | dollars this session may spend before it stops STARTING turns (see [Spend ceilings](#spend-ceilings)) — a peer-started turn is refused exactly like a typed one |
| `permission_mode` | `DOXA_PERMISSION_MODE` | `default` | mode new sessions connect in; accepts `default`/`acceptEdits`/`plan` only |
| `mesh_open_browser` | `DOXA_MESH_OPEN_BROWSER` | off | `/mesh` opens the graph in this machine's browser as well as printing its URL. Off, because DOXA runs in terminals that have none (see [Fleets from the TUI](#fleets-from-the-tui)) |
| `linger_secs` | `DOXA_LINGER_SECS` | 120 | seconds a daemon outlives its last detached client |
| `worktree_per_session` | `DOXA_WORKTREE` | on | give each session its own git worktree |
| `restore_tabs` | `DOXA_RESTORE_TABS` | on | plain `doxa` restores the whole saved tab set |
| `resume_restored` | `DOXA_RESUME_RESTORED` | on | a restored tab whose session ended comes back live, continuing the conversation |
| `derive_secs` | `DOXA_DERIVE_SECS` | `900` | streaming-deriver interval, seconds; `0`/`off` disables it and leaves review to PreCompact and session end |
| `consult_floor` | `DOXA_CONSULT_FLOOR` | 1.0 | act-time belief-consult relevance floor; 0 disables it |
| `graph_context` | `DOXA_GRAPH_CONTEXT` | off | attach a graph-context block to every turn. Recurring per-turn token cost once on, which is why it is off |
| `graph_view` | `DOXA_GRAPH_VIEW` | `browser` | how the beliefs picker's `g` shows one belief's neighbourhood: `browser` (mermaid page under `~/.doxa/graphs`) or `ascii` (LORE's edge block, in the TUI) |
| `lore_root` | `LORE_ROOT` | `~/.claude/lore` | where the belief store and session index live; sticky, set by `/setup` |
| `nerd_font` | `DOXA_NERD_FONT` | off | use a Nerd Font glyph for the branch chip |
| `ctx_absolute` | `DOXA_CTX_ABSOLUTE` | off | print `24k/200k` beside the `ctx%` chip (below 100 columns it drops again) |
| `image_mode` | `DOXA_IMAGE_MODE` | probe | force a rung of the image ladder (`kgp`/`sixel`/`halfblock`/`text`) |
| `boot_banner` | `DOXA_BOOT_BANNER` | on | draw the DOXA mark above the opening identity block |
| `sidebar` | `DOXA_SIDEBAR` | *auto* | the session rail: empty = appear once there is a collection or a second session, `1` = always, `0` = never. `f3` writes `1`/`0`, so the first toggle ends the guessing |
| `sidebar_width` | `DOXA_SIDEBAR_WIDTH` | 25 | columns the rail occupies; clamped to 22–41 rather than rejected. Written by a drag of the rail's edge and by `alt+shift+←/→` |
| `key_notice` | `DOXA_KEY_NOTICE` | on | one-line startup notice naming any bound keys this terminal can't deliver and the slash command that reaches them instead; silent on a kitty-protocol terminal or one whose protocol was never measured |
| `context_grid` | `DOXA_CONTEXT_GRID` | `glyphs` | cell style for `/context`'s grid: `glyphs` (⛀⛁⛶) or `ascii` (`[#]`/`[ ]`) for a font that tofu's them |
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
| `notify_staged` | `DOXA_NOTIFY_STAGED` | on | notify when the background reviewer stages proposals |
| `notify_needs_input` | `DOXA_NOTIFY_NEEDS_INPUT` | **off** | notify when a session is waiting on you (a turn merely finishing never notifies); a fully detached session always notifies once this is on |
| `notify_update` | `DOXA_NOTIFY_UPDATE` | on | notify when `/update` has something to pull |
| `notify_lore` | `DOXA_NOTIFY_LORE` | on | `lore_core`'s own review banner; held silent while `notify_staged` is on |
| `remote_enabled` | `DOXA_REMOTE_ENABLED` | off | allow a remote bridge to attach to this daemon at all. On by itself grants nothing — the allow-list below still has to name someone (see [Remote drivers](#remote-drivers--a-policy-and-no-transport)) |
| `remote_allowed_logins` | `DOXA_REMOTE_ALLOWED_LOGINS` | empty | DOXA's own allow-list of logins, on top of the tailnet's. **Empty refuses everyone**, never everyone-allowed |
| `remote_allow_shell` | `DOXA_REMOTE_ALLOW_SHELL` | off | let a remote driver run `!` shell lines — refused on the reduced remote surface without it |
| `remote_allow_bypass` | `DOXA_REMOTE_ALLOW_BYPASS` | off | let a remote driver raise the permission mode to `bypassPermissions`; independent of `allow_bypass`, and both must be on |
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

## Screenshots

Every still and GIF under [`assets/shots/`](../assets/shots/) is generated
headlessly from the real app by
[`scripts/screenshot.py`](../scripts/screenshot.py) and
[`scripts/record_gif.py`](../scripts/record_gif.py) — a scripted session,
no spend, fake account numbers — and each still keeps its source SVG
committed beside its PNG. **Thirty-two images, each named exactly once**
— the [README](../README.md#gallery) captions fourteen of them, counting
the hero, and the other eighteen are catalogued below so that **no
rendered asset is left unnamed by any document**. That is the exact
condition `beliefs-browser.png` needed to sit wrong for eighteen releases
before v0.87.0 deleted it. All thirty-two are 3068x1734, but they are not
all from one pass: an image that still matches the feature it shows is
left alone rather than re-rendered, so the gallery sits at mixed versions
by design.

Every scene renders the app inside **the checkout the script runs from**,
so the identity block, the tab labels and the `repo ⎇ branch` chip carry
that checkout's own branch and path. Capture from `main`, on a clean tree
— a throwaway clone is the reliable way to have both — or a working branch
name ends up baked into eighteen of the nineteen stills, and an
uncommitted edit paints a `diff` chip that belongs to the capture, not to
the feature. `folder-chip` is the one still that carries neither, because
its whole subject is a session that is not in a repository.

| asset | shows |
|---|---|
| [`split-panes.gif`](../assets/shots/split-panes.gif) | one pane becoming two — the keystroke, and the pane arriving |
| [`markdown-stream.gif`](../assets/shots/markdown-stream.gif) | a reply streaming as real markdown, a table row at a time |
| [`subagent-tracker.png`](../assets/shots/subagent-tracker.png) | a running subagent's status row and its own tab |
| [`trace.png`](../assets/shots/trace.png) | a subagent's activity as a tree under its parent `Task` chip |
| [`error-block.png`](../assets/shots/error-block.png) | a caught exception as a collapsible red-ruled transcript block |
| [`chip-picker.gif`](../assets/shots/chip-picker.gif) | the shared selector picker, opened from the branch chip |
| [`tab-lifecycle.gif`](../assets/shots/tab-lifecycle.gif) | a background tab amber while running, green once finished unseen |
| [`search.gif`](../assets/shots/search.gif) | `/search` over every past session, live as you type |
| [`settings.png`](../assets/shots/settings.png) | the settings modal, each row's effective value and its source |
| [`reasoning.gif`](../assets/shots/reasoning.gif) | the reasoning fold ticking, then the phase flipping to `generating` |
| [`sessions.png`](../assets/shots/sessions.png) | `/sessions`, attached and detached |
| [`clock.png`](../assets/shots/clock.png) | the upper-right clock |
| [`palette.gif`](../assets/shots/palette.gif) | the `ctrl+p` command palette |
| [`rename.gif`](../assets/shots/rename.gif) | renaming a tab by double-clicking its header |
| [`attention-blink.gif`](../assets/shots/attention-blink.gif) | a tab blinking for attention |
| [`image-support.png`](../assets/shots/image-support.png) | `/img`'s tier table — the rung in use, and every rung it could not measure labelled as such rather than guessed |
| [`banner-blocks.png`](../assets/shots/banner-blocks.png) | the boot banner, drawn in block characters on every terminal alike |
| [`transparent.png`](../assets/shots/transparent.png) | the transparent-background setting |
