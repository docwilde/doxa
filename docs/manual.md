# DOXA manual

Reference for what DOXA does today. Everything here is true of the current
code — verified against source, not transcribed from release notes. For
the pitch and the install instructions, see [README.md](../README.md). For
designs that are **not** built yet, see [docs/plans/](plans/) — this manual
never documents a plan as if it were shipped.

## Contents

- [Sessions and the daemon](#sessions-and-the-daemon)
- [The spawned CLI](#the-spawned-cli)
- [The transcript](#the-transcript)
- [Tabs](#tabs) — and [restoring them](#restoring-tabs)
- [Pane groups](#pane-groups)
- [The session sidebar](#the-session-sidebar)
- [The live diff](#the-live-diff)
- [Worktrees and finalize](#worktrees-and-finalize)
- [Where a session is](#where-a-session-is)
- [Permission modes](#permission-modes)
- [Containment](#containment)
- [The status bar](#the-status-bar)
- [LORE integration](#lore-integration)
- [Shell escape](#shell-escape)
- [Images](#images)
- [Search, resume, and peers](#search-resume-and-peers)
- [Keyboard protocol](#keyboard-protocol)
- [Commands](#commands)
- [Settings](#settings)
- [Screenshots](#screenshots)

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

## The spawned CLI

The engine behind a session spawns a `claude` CLI process, and that
process gets a **config directory of its own**. `CLAUDE_CONFIG_DIR` is
set on the child's environment only (`ClaudeAgentOptions.env` —
DOXA's own environment is never touched), pointing at a directory DOXA
writes and owns: a `settings.json` with no `hooks`, no `enabledPlugins`
and no `plugins` key at all, plus `LORE_SKIP=1` belt-and-braces. So none
of the Claude Code plugins installed on your machine — none of their
hooks, commands, skills, agents or MCP servers — load into a DOXA session
unasked.

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

## Tabs

Since v0.97.0 tabs belong to a **pane group**, not to the window — one
group unless you split, in which case each region has a strip of its own.
Everything below is about the group holding the keyboard; see
[Pane groups](#pane-groups).

`ctrl+t` opens a new tab in this group (fresh session, same repo scope).
`ctrl+w` closes the active tab and detaches its daemon. `ctrl+q` ends the
active tab's session for real. `ctrl+left` / `ctrl+right` cycle **this
group's** tabs and leave every other group alone.

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

`/split` (or `ctrl+o`) puts a second session **stacked below** this one;
`/vsplit` (or `ctrl+n`) puts one **side by side** with it. That is vim's

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

`/split` (or `alt+s`) puts a second group **stacked below** this one;
`/vsplit` (or `alt+d`) puts one **side by side** with it. That is vim's
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
| `ctrl+o` / `ctrl+n` | split stacked below / side by side (`alt+s` / `alt+d` on a kitty-protocol terminal) |
| `ctrl+shift+←/→/↑/↓` | move the keyboard to the pane in that direction — geometric, never "next pane" |
| `alt+←/→/↑/↓` | move the divider between this pane and its neighbour that way |
| `ctrl+↑` / `ctrl+↓` | move the **in-pane** divider (the status bar): up grows the transcript, down grows the prompt — and this works in a tab with no splits at all |

| `ctrl+←/→` | cycle the tabs of **this group** — every other group stays put |
| `ctrl+1` … `ctrl+9` | jump to a group by position — **numbered left to right, then top to bottom**, so in a 2×2 it is upper-left, upper-right, lower-left, lower-right |
| `alt+s` / `alt+d` | split into a second group, stacked below / side by side |
| `ctrl+shift+←/→/↑/↓` | move the keyboard to the group in that direction — geometric, never "next group" |
| `alt+←/→/↑/↓` | move the divider between this group and its neighbour that way |
| `ctrl+↑` / `ctrl+↓` | move the **in-pane** divider (the status bar): up grows the transcript, down grows the prompt — and this works in a window with no splits at all |

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

Two split refusals, each printed as a block in the group it is about and
changing nothing: a group may be split **twice** (`SPLIT_SLOTS`), which
is what gives the 2×2 the design is written around — each new group is
born with its own fresh allowance, so there is no fixed ceiling on
groups, only on how deep one lineage goes — and a split that would leave
either side under **34 columns or 9 rows** is refused with the number it
actually has. A refusal that performed a sliver would be worse than the
refusal.

**A narrow group hides its own tab strip.** Two strips is more chrome
than one, so below **34 columns** a group draws its labels compactly and
below **17 columns** it draws no strip at all. Both numbers are the same
measurement: a tab header costs its label floor (`4 + " · " + 6` from the
model/repo minimums) plus the provider glyph and Textual's own one-column
padding each side — 17 columns for one header, 34 for the two a strip is
actually *for*. The narrowest group DOXA will create is 34 columns, so it
sits exactly on that boundary.

`ctrl+w` closes the **active tab** of the focused group, detaching its
session as it always has. Closing a tab closes **one** session — through
v0.95.0 closing a tab that held a three-way split ended three. When it
was the group's last tab the group goes with it, the split collapses, the
survivors take the room back, and the nearest remaining group takes the
keyboard. Closing the last tab of the last group closes the app.

## The session sidebar

`ctrl+b` (or `/sidebar`) shows a **collapsible rail down the left of the
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
additionally spends a column on a **glyph**, which a strip has no room
for: `✓` a turn finished unseen, `+` staged proposals, `▸` working, `!`
waiting for you. The needs-input blink blinks here too.

**Collections** group sessions under a name you choose. `group` already
means a region of the screen, so this word is different on purpose: two
sessions in one collection may sit in different pane groups, and one pane
group may show tabs from three collections. A session belongs to **at most
one** collection; the rest appear under an unnamed `— ungrouped —` heading
that is always last and is not itself a collection. Click a heading to
fold it.

| command | does |
|---|---|
| `/sidebar [on\|off]` | Show or hide the rail; with no argument, toggle (`ctrl+b`) |
| `/collection` | List the collections and how many sessions each holds |
| `/collection new <name>` | Make an empty collection |
| `/collection rename <old> <new>` | Rename one |
| `/collection delete <name>` | Drop the grouping — its sessions become ungrouped, **not** closed |
| `/collection add <name>` | Move **this** session into that collection, making it if needed |
| `/collection remove` | Take this session back out |

**`ctrl+b` is tmux's default prefix.** It is the conventional sidebar key
everywhere else and it is what DOXA binds, but on a tmux session it never
reaches the app — `/sidebar` is the door that always works, the same
bargain `ctrl+,`, `ctrl+tab` and `ctrl+1`…`ctrl+9` already ship on. Unlike
those three, `ctrl+b` *is* deliverable under both keyboard encodings; tmux
is the only thing in its way.

**It refuses to open on a window too narrow to hold it.** The rail is 22
columns by default (`sidebar_width`, clamped to 19–38) and a pane needs 34,
so below **53 columns** it cannot open at all — and it also refuses when
opening it would take the narrowest pane group below 34, which is measured
against the rectangles actually on screen rather than against a constant.
Both numbers come out of the same place the tab-strip rungs do: a row's
label floor is `4 + " · " + 6`, plus the rail's own six columns of padding,
indent and mark. It says why, in the transcript, rather than squeezing a
pane — and it opens by itself the moment the terminal is wide enough
again.

**With nothing to say, it stays out of the way.** On a fresh install
`sidebar` is *auto*: the rail appears once there is a collection or a
second session and not before, the same hide-at-zero discipline the
context chip, the side-by-side diff and the group tab strips follow. The
first `ctrl+b` writes the choice, and from then on it is yours.

**Collections are saved with the tab set**, in the same per-repo record,
and come back with the window. A collection whose sessions are all gone
does not; a member whose session is gone is dropped from it, the way a
dead pane is dropped from a saved layout. A member whose **tab** is closed
but whose session is still around keeps its row, marked `· closed` — the
rail is a session index, not a second tab strip, and clicking such a row
tells you `/attach` is how you get it back.

## The live diff

`/diff` (or `f2`) puts a **live diff of this session's worktree** in
the pane beside the session — `git diff` against the branch the worktree
was cut from, recomputed every time an edit lands and never on a timer.
Files are collapsed by default with their changed-line counts; binary and
very large files are named rather than rendered; a diff that hit a cap
says so. Side-by-side turns on above 100 columns and unified is the
default below it, because at 80 columns a half-width pane is 40 and two
20-column sides are unreadable.

The diff pane is a real layout leaf: `ctrl+shift+←/→` moves the keyboard
into it and back, `alt+←/→` widens it, it keeps updating while you type
in the session, and its position is saved and restored with the rest of
the tab's layout. A second `/diff` closes it. Each session has its own.

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

**Two strikes.** A *hard* failure is an unknown tool name, a `TypeError`
from the backend, or any exception the operator raised: the gate returns
it as an ordinary `{"error": ...}` result the model can read and retry.
The **second** hard failure of the same tool disables it for the rest of
the session, and the disabled names collect in the status bar's `⊘` chip.
A refused-but-known tool counts too, because a repeatedly-refused call is
the strongest "stop calling this" signal available. Bad arguments are
*not* hard — a recoverable mistake must stay retryable. Nothing here is
persisted: the next session starts with a clean slate.

**Nothing auto-denies silently.** A headless SDK run with no callback
auto-denies an `AskUserQuestion` and a permission request without telling
anyone. DOXA gives each one a real dialog and blinks the tab that raised
it. A *desktop* notification is opt-in — `notify_needs_input` is **off**
by default (see [Settings](#settings)); turned on, a fully detached
session always notifies. The transcript records the answer either way.

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
| `dir NAME` | the folder chip, shown **instead of** the git chip when this session is not in a git repository at all (see [Where a session is](#where-a-session-is)) | yes — the same repo/directory picker |
| `sub:<tier> (≈$…)` or `$…` | subscription tier with a list-price what-if, or the real API spend on API-key auth | no |
| `s:N% w:N%` | subscription session (5h) and weekly utilization, cached by the `claude` CLI itself | no |
| `ctx N%` | context window usage, amber at 70%, red at 90%; `ctx_absolute` adds `24k/200k` inline | yes — confirms, then `/compact` |
| `N beliefs` | active LORE beliefs for this session | yes — grouped belief list |
| `mem u%p%` | curated-memory fill, user and project, as two separate percentages | no |
| `N proposals` | staged LORE proposals awaiting review (hidden at zero) | yes — pending-proposals picker |
| `⧉ N agents` | Task-spawned subagents currently running (hidden at zero) | no (see subagent row below) |
| `⌁ session <id>` | this session's reattach handle (only while attached to a daemon) | yes — sessions picker |
| `peers N (k⌁)` | other DOXA sessions on this repo; `k⌁` is how many are detached | yes — peers picker: each row is the peer, the beginning of its transcript, and tokens consumed so far (self-reported, up to one heartbeat stale) |
| `⊘ <tool>` | a tool disabled after two failures this session | no |

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
it is scheduled on turn-done, refuses to start while one is already in
flight or while the session is finalizing, so a quiet session pays nothing
and a busy one pays at most four reviews an hour.

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
| `/split` | A second session **stacked below** this pane (`ctrl+o`) |
| `/vsplit` | A second session **side by side** with this pane (`ctrl+n`) |
| `/diff` | This session's live worktree diff in the pane beside it, or close it (`f2`) |

| `/split` | A second pane group **stacked below** this one (`alt+s`) |
| `/vsplit` | A second pane group **side by side** with this one (`alt+d`) |
| `/pane [n]` | Jump to pane group `n`, numbered left to right then top to bottom (`ctrl+1`…`ctrl+9`); with no number, flash them |
| `/movepane <n>` | Move this group's active tab into group `n` — the session keeps running |
| `/sidebar [on\|off]` | Show or hide the session sidebar (`ctrl+b`) |
| `/collection …` | `new` / `rename` / `delete` / `add` / `remove` — group sessions in the sidebar under a name you choose |
| `/diff` | This session's live worktree diff in the group beside it, or close it (`alt+g`) |
| `/dir` | Where this session actually is |
| `/cd <path>` | Open that path in a **new** tab; this session stays where it is |
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
| `adopt_plugins` | `DOXA_ADOPT_PLUGINS` | off | load commands/skills/agents from your OWN installed Claude Code plugins into new sessions — never their hooks or MCP servers, never LORE (see [docs/plans/plugins.md](plans/plugins.md)) |
| `permission_mode` | `DOXA_PERMISSION_MODE` | `default` | mode new sessions connect in; accepts `default`/`acceptEdits`/`plan` only |
| `linger_secs` | `DOXA_LINGER_SECS` | 120 | seconds a daemon outlives its last detached client |
| `worktree_per_session` | `DOXA_WORKTREE` | on | give each session its own git worktree |
| `restore_tabs` | `DOXA_RESTORE_TABS` | on | plain `doxa` restores the whole saved tab set |
| `resume_restored` | `DOXA_RESUME_RESTORED` | on | a restored tab whose session ended comes back live, continuing the conversation |
| `derive_secs` | `DOXA_DERIVE_SECS` | `900` | streaming-deriver interval, seconds; `0`/`off` disables it and leaves review to PreCompact and session end |
| `consult_floor` | `DOXA_CONSULT_FLOOR` | 1.0 | act-time belief-consult relevance floor; 0 disables it |
| `graph_view` | `DOXA_GRAPH_VIEW` | `browser` | how the beliefs picker's `g` shows one belief's neighbourhood: `browser` (mermaid page under `~/.doxa/graphs`) or `ascii` (LORE's edge block, in the TUI) |
| `lore_root` | `LORE_ROOT` | `~/.claude/lore` | where the belief store and session index live; sticky, set by `/setup` |
| `nerd_font` | `DOXA_NERD_FONT` | off | use a Nerd Font glyph for the branch chip |
| `ctx_absolute` | `DOXA_CTX_ABSOLUTE` | off | print `24k/200k` beside the `ctx%` chip (below 100 columns it drops again) |
| `image_mode` | `DOXA_IMAGE_MODE` | probe | force a rung of the image ladder (`kgp`/`sixel`/`halfblock`/`text`) |
| `boot_banner` | `DOXA_BOOT_BANNER` | on | draw the DOXA mark above the opening identity block |
| `sidebar` | `DOXA_SIDEBAR` | *auto* | the session rail: empty = appear once there is a collection or a second session, `1` = always, `0` = never. `ctrl+b` writes `1`/`0`, so the first toggle ends the guessing |
| `sidebar_width` | `DOXA_SIDEBAR_WIDTH` | 22 | columns the rail occupies; clamped to 19–38 rather than rejected |
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
committed beside its PNG. The [README](../README.md#gallery) captions ten
of them. The rest, catalogued here so that **no rendered asset is left
unnamed by any document**: that is the exact condition
`beliefs-browser.png` needed to sit wrong for eighteen releases before
v0.87.0 deleted it.

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
| [`clock.png`](../assets/shots/clock.png) | the clock |
| [`palette.gif`](../assets/shots/palette.gif) | the `ctrl+p` command palette |
| [`rename.gif`](../assets/shots/rename.gif) | renaming a tab by double-clicking its header |
| [`attention-blink.gif`](../assets/shots/attention-blink.gif) | a tab blinking for attention |
| [`image-support.png`](../assets/shots/image-support.png) | `/img` naming the image tier this terminal got |
| [`banner-blocks.png`](../assets/shots/banner-blocks.png) | the boot banner |
| [`transparent.png`](../assets/shots/transparent.png) | the transparent-background setting |
