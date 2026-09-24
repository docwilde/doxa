<p align="center"><img src="assets/logo.png" width="560" alt="DOXA — belief earning knowledge"></p>

<p align="center">
  <img src="https://img.shields.io/badge/status-beta-f59f00" alt="beta: config keys and the socket protocol can still change">
  <a href="https://github.com/docwilde/doxa/releases"><img src="https://img.shields.io/github/v/release/docwilde/doxa?label=release&color=e8590c" alt="latest release"></a>
  <a href="https://github.com/docwilde/doxa/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/docwilde/doxa/ci.yml?branch=main&label=tests" alt="CI status on main"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/built%20on-Claude%20Agent%20SDK-d97757" alt="built on Claude Agent SDK">
  <img src="https://img.shields.io/badge/auth-provider%20CLI%20or%20API%20key-2f9e44" alt="authentication follows the selected engine">
</p>

> [!WARNING]
> **Beta.** DOXA moves fast. Config keys, the socket protocol and on-disk
> formats can still change between minor versions,
> with no migration path.
> It runs an agent that edits your files and a shell with your privileges.
> The suite gates every release, but the project has one author and most
> defects so far were found by using it, not by the tests. Read
> [Non-goals](#non-goals) before trusting it with anything you would mind
> losing.

**DOXA** is a terminal for coding agents, built on the Claude Agent SDK
and Textual. Four engines: **Claude** on your Claude subscription, **Codex**
through the Codex CLI on whatever it is signed into (a ChatGPT subscription
or an API key), and **DeepSeek** and **GLM** on their own API keys. Pick one
per session with `--engine` or `/engine`; the manual's
[engine capabilities](docs/manual.md#engine-capabilities) table says what
each can do. Each session runs in a **daemon** of its own:
close the terminal, `doxa attach` an hour later, and the transcript picks up
where it stopped. No tmux involved.

Start DOXA inside a repository and the session already knows the project.
Durable facts about that codebase — conventions, past workarounds,
corrections a human made once — reach the model before your first prompt,
per-repo rather than global. Every durable conclusion the agent draws
enters as a **belief**: visible, queryable, citable, never acted on. It
earns influence only when a human approves it or it builds a track record
of being right. The tagline is meant literally.

δόξα (*dóxa*): belief, opinion — as distinct from ἐπιστήμη (*epistēmē*),
justified knowledge. The name is the thesis: belief is the raw material,
never the finished thing.

Memory is [LORE](https://github.com/docwilde/LORE)'s `lore_core`, imported
in-process rather than shelled out to. Its Claude Code and Codex plugins
share the same user and repo memory with DOXA; the source engine is recorded
for context. See
[LORE integration](docs/manual.md#lore-integration).

![DOXA shell: three tabs, one per model tier; a turn answered with a table of belief ids and status above a collapsed tool-calls fold; a status bar led by the permission-mode chip](assets/shots/hero.png)

*Every image here is rendered headlessly from the real app — scripted, no
spend, fake account numbers. See
[screenshots](docs/manual.md#screenshots).*

## What you get

- **[Persistent sessions.](docs/manual.md#sessions-and-the-daemon)** Close the
  terminal and reattach later; `doxa` restores the repository's tabs.
- **[Inspectable turns.](docs/manual.md#the-transcript)** Expand reasoning,
  tool calls and their results from the transcript.
- **[Independent panes and worktrees.](docs/manual.md#worktrees-and-finalize)**
  Split the view; each session has its own branch, and work is never auto-merged.
- **[A live diff.](docs/manual.md#the-live-diff)** Review changes beside the
  session and reject individual hunks.
- **[A prompt queue.](docs/manual.md#typing-while-a-turn-runs)** Type during
  a turn; `/queue` shows and cancels prompts waiting to run.
- **[Auditable memory.](docs/manual.md#lore-integration)** LORE shares memory
  across engines and stages new beliefs for review.
- **[Visible permissions.](docs/manual.md#permission-modes)** Switch modes
  with `shift+tab`; a tool gate checks calls, while `!` runs outside the model.
- **[Measured status.](docs/manual.md#the-status-bar)** Chips show available
  engine metrics; images fall back to formats your terminal supports.
- **[Peer messages.](docs/manual.md#search-resume-and-peers)** Sessions in
  one repository can exchange messages; model sends require opt-in.
- **[Fleets.](docs/fleet.md)** Run mixed-engine pools with budgets, or give
  one supervisor the job of coordinating workers in separate worktrees.
- **[Access from another device.](docs/plans/remote.md)** Opt-in Tailscale
  bridges expose peer sessions and browser control of local sessions.
- **[Isolated Claude config.](docs/manual.md#the-spawned-cli)** Spawned Claude
  processes use a DOXA-owned config; plugins require opt-in.

A `Task` subagent also gets a status row and a live read-only tab, and
`/dir` says [where a session is](docs/manual.md#where-a-session-is)
outside a repo. `alt+d` / `alt+s` / `alt+g` reach the split and diff
actions too, but only on a kitty-protocol terminal; `/help` marks every
binding yours cannot send.

## Gallery

![A session left, its live diff right, headed '2 files changed, +9 -1 against main'; one hunk carries an amber 'reject queued' badge above a disabled reject button](assets/shots/live-diff.png)

*`f2` (or `/diff`) opens the live diff beside the session. **Reject** reverse-applies that hunk and tells the agent why; mid-turn it queues, and says so.*

![One tab split into two panes, each its own session: same identity block, different models, separate transcripts, a status bar apiece](assets/shots/split-panes.png)

*A split spawns a **second session**, not a second view of the first. Each group owns its own tab strip since v0.97.0 — the left group has three tabs and draws one; the right holds a single tab, and a strip of one is chrome, so it draws none.*

![A turn's tool-call count ticking 1 to 3 as chips land, the marker counting 5s, 9s, 14s through the silent wait](assets/shots/tool-calls.gif)

*Calls fold to one row each, opening to exact arguments and result. The marker counts through a silent call, so a slow one never reads as hung.*

![A lore_belief_search chip expanded, listing one STEER belief with an outcome count and one CITE-only belief](assets/shots/memory.png)

*A memory call is an ordinary chip: what decides the agent's beliefs is as inspectable as anything else it does.*

![The beliefs picker grouped by scope, each row carrying inline actions 'y confirmed', 'c contradicted', 's stale', 'r retract', 'g graph'](assets/shots/beliefs-picker.png)

*Every belief, grouped by scope, with what reality has said about it. Four verdicts record an outcome; `g` only looks.*

![/context as a 10 by 20 grid of 200 cells, headlined 'in use 60,910 / 180,000 tokens - 33.8%'](assets/shots/context.png)

*One cell per half-percent. Every number is the CLI's own accounting of its own request — DOXA runs no second tokenizer.*

![An AskUserQuestion dialog above the prompt, asking which environment a migration should target](assets/shots/needs-input.gif)

*Questions and permission requests get a real dialog. A headless run with no callback auto-denies both, silently.*

![The peers chip opening a roster of three sessions with titles and token totals, one detached, one mid-first-turn showing 'tok --'](assets/shots/peers.gif)

*Who else is on this repo, what each says it is running, and tokens spent — self-reported on each peer's 15-second heartbeat, except a model change, which publishes at once because a stale model id is a wrong answer rather than an old number. A peer mid-first-turn reads as unknown, never zero.*

![A peer message block above a system line reading 'a peer message started this turn', and the turn it started, whose fold header carries the sender where a typed prompt would be](assets/shots/peer-turn.png)

*A turn nobody in this window asked for says so three times before it has finished costing anything: the message as it arrived, a line naming who started it, and a turn header carrying that sender instead of a prompt. The model reads the same attribution, as the first paragraph of the prompt itself rather than a flag beside it.*

![The right end of the status bar: 'peers 2 (1⌁)', then an up arrow with a filled lamp and a down arrow with a hollow one](assets/shots/peer-lights.png)

*Two lamps beside the peer count, one per direction. Filled is traffic in the last four seconds, hollow is a channel that exists and is quiet — a filled glyph beside an outlined one, because a colour change alone survives neither peripheral vision nor a screenshot. They appear as a pair or not at all, so neither arrives by shifting the row sideways at the moment traffic does.*

![A fleet tab headed 'fleet 20260919T113402-8c41 — finished' over 'mode symmetric': sixteen worker slots dealt claude, codex, deepseek and glm, three reading 'OFF' under mem, above 'dispatch spread 11 ms across 16 sessions', 'quiesced after 58s' and thirty ledger lines](assets/shots/fleet.png)

*`/fleet start` spawns N sessions on one prompt at one instant and opens this tab: which shape the run is, what it was allowed to be, who was dealt what and in which role, how far apart the prompts landed, when it went quiet, what the agents said to each other, and whether teardown left anything running. This one is a symmetric run and says so; a `--supervisor` run names the slot holding the prompt on the same line. The tab reads the run's manifest and its ledger and never the run itself — so it still says all of that once the run's task has gone, and closing it ends the run unless you typed `/fleet detach`.*

![The peer mesh in a browser under '9 sessions 32 messages 46 pairs 4 broadcasts': nine session nodes rimmed by engine colour, 'release notes' selected in white, and a side panel reading '/home/you/repo/doxa', 'claude · own turn running', '3 sent 10 received 8 peers' above a feed of message bodies, one tagged BCAST to 8 recipients](assets/shots/mesh.png)

*`/mesh` serves the peer ledger as a graph on loopback, gated by a token in the URL and off until you ask for it. An edge is a delivery that happened and its thickness is how many; a broadcast is drawn as one fan rather than as N sessions each deciding to speak, because the ledger records that difference rather than inferring it. Click a session and its own recent traffic opens beside the graph, bodies included. A graph is the one thing a terminal cannot draw honestly, so this view is a browser page — and the only asset here with no SVG twin.*

![The permission-mode chip cycling: grey 'default', teal 'plan', amber 'auto', red 'bypassPermissions'](assets/shots/permission-mode.gif)

*The chip leads the bar at every width — `auto` amber, `bypassPermissions` and `dontAsk` red, the modes where nothing stops to ask.*

![A session in a plain directory: the identity chip reads 'dir design-notes' with no branch half](assets/shots/folder-chip.png)

*Outside a repo the chip is a different shape, not the same one with a hole in it. `/cd` opens the target in a new tab and says the session stayed put.*

![An amber '⇅ sync 2m ↑3 ⚠1' chip in the status bar, between the branch chip and the subscription chip](assets/shots/sync-chip.png)

*With [LORE](https://github.com/docwilde/LORE) sync configured: how long since a pull landed here, how much this machine has not sent yet, and how many ops failed their integrity check and were staged rather than applied. Amber because that last number is waiting on a person. Sync is off by default, and then the chip is absent — never a zero, never an error.*

Eighteen more scenes are catalogued in the
[manual](docs/manual.md#screenshots); between the two documents every
rendered asset is named exactly once. An asset named nowhere is how
`beliefs-browser.png` rotted for eighteen releases before v0.87.0 deleted
it. The gallery is at mixed versions and always has been — a stale
picture of a feature that still looks like that is fine; a caption that
describes something else is not.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/docwilde/doxa/main/scripts/install.sh | sh
```

It checks Python 3.11+, [`uv`](https://docs.astral.sh/uv/) (offering to
install it), and `git`. Missing or signed-out provider CLIs produce a
warning; you can install and sign in to
[`claude`](https://docs.claude.com/en/docs/claude-code) or `codex`
later. In DOXA, `/login claude` and `/login codex` start their CLI's
browser sign-in while the TUI stays usable; `/logout claude` and
`/logout codex` sign out. DOXA authenticates through each CLI's own
OAuth session, never through
`ANTHROPIC_API_KEY`, which it reads for one thing only: listing the model
catalogue in the picker when a key happens to be in the environment —
then runs `uv tool install
git+https://github.com/docwilde/doxa`. DOXA is not on PyPI. Re-running is
safe and never touches an existing `~/.doxa/config.toml`. Add `sh -s --
v1.18.0` to pin a tag instead of tracking `main`. Read it first if you
would rather not pipe a stranger's script into `sh`.

For the default install that tracks `main`, `/update` refreshes the uv tool
copy and reports the installed version and Git revision before and after.
`/update --restart` also closes this window's sessions and relaunches DOXA.
A tag-pinned install stays pinned; reinstall that tag explicitly to change it.
DOXA checks the running copy's uv receipt and installed wheel before updating.

The login command shows the CLI's authorization URL or device code and
completion status in DOXA. It uses the CLI's existing `CLAUDE_CONFIG_DIR`
or `CODEX_HOME` profile. A successful Claude login synchronizes that CLI's
credentials into DOXA's isolated Claude session directory. A successful
Claude logout clears the isolated credential and blocks its automatic
reimport until the next explicit `/login claude`. Existing engine sessions
may retain their connection; open a new session after changing accounts.
Use `/login codex --device-auth` when the Codex device flow is enabled
for your account.

Or run from a checkout:

```sh
git clone https://github.com/docwilde/doxa && cd doxa
uv sync
uv run doxa
```

`uv sync` is all of it: `lore_core` is a pinned git dependency, so the
LORE plugin is not a prerequisite. Install that plugin anyway and its
checkout deliberately wins over the pinned copy — both share one store,
and a terminal quietly disagreeing with the rest of the machine is the
worse surprise. `/about` names which copy loaded.

## Quickstart

```sh
uv run doxa          # spawn a session here, or restore this repo's saved tab set
uv run doxa new      # force a fresh session instead of attaching
uv run doxa new --branch <name>   # fork the session's worktree from <name>
uv run doxa attach   # reattach by session id / title prefix
uv run doxa stop     # finalize now (LORE review + index), daemon exits
uv run doxa doctor   # read-only health checks, no TUI: pass/fail + fix per check
uv run doxa launcher install      # XDG start-menu entry + icons
uv run doxa --engine codex        # drive the session with Codex instead
uv run doxa --engine deepseek     # or DeepSeek, on DEEPSEEK_API_KEY
uv run doxa --engine glm          # or GLM (Z.ai), on ZAI_API_KEY
```

What each engine can do — daemon and detach, LORE tools, permission
modes, cost chips, peer tools, budgets, a fleet slot — is one table in the
manual, read off the registry: [engine capabilities](docs/manual.md#engine-capabilities).

**[Other engines](docs/manual.md#engines).** `--engine` (or the `engine`
setting, or `/engine <id>` for the sessions and tabs you open next) runs a
DOXA session on something other than the `claude` CLI, and
each one declares what it can actually do rather than inheriting Claude's
list. `codex` (v1.4.0) drives the Codex CLI. `deepseek` and `glm`
(v1.10.0) are two third-party chat-completions APIs behind one
implementation and one capability map — eleven of eighteen fields true —
so a run can mix vendors without also mixing what the terminal supports.
Both are billed on their own API key, not on your Claude subscription, and
refuse to start without it.

What an engine does not report, DOXA does not paint. Codex counts tokens
but never reports a window size, so there is no ctx chip; it reports no
cost either and has one fixed permission posture rather than modes to
cycle. Its DOXA tools arrive through a stdio MCP server DOXA registers on
every `codex exec` (`doxa/mcpserver.py`), executed through the same tool
gate the vendors use, and the memory snapshot rides the first prompt
because Codex has no system message and no hook. A `peer_send` from that
server is not performed there: it is forwarded to the session's engine
over a control socket and sent on the session's own rate limiter and
ledger, so a Codex model's message and a human's `/msg` are bounded,
recorded and lit identically. DeepSeek and GLM carry
the tools in-process — the model only ever *names* a call and DOXA
executes it, so the allowed set, the refusals and the two-strikes disable
all apply — but they report no window size either and no per-session
dollar figure, and they have no modes to cycle for a different reason:
DOXA owns their whole tool surface, so a mode would configure nothing.
Since 1.13.0 every engine runs in a daemon, so `ctrl+q` detaches and
`doxa attach` reattaches whatever the engine; `--in-process` is the one
door that still hosts an engine inside the TUI.
`doxa.engines.get("deepseek").supports()` is the whole map for any of
them.

**Model lists and preferences.** A Codex session asks the signed-in Codex
CLI's app-server for its account's picker-visible models (`model/list`);
that CLI may serve a cached list. A Claude subscription session uses the
installed Claude CLI's account-matched catalogue cache, labelled with its
fetch time and whether it is stale. This is a dated snapshot, not a live
subscription Models API. If that cache is unavailable, Claude shows its
four static aliases with a fallback note. `/model` and the model chip list
models for the current session's engine. A successful `/model <id>` switch
also saves that engine's preference: Claude keeps the top-level `model`
setting, while Codex, DeepSeek and GLM use their own entries in `[models]`.
`DOXA_MODEL` and `--model` remain explicit overrides for a new session.
Use `doxa new --engine codex` to start a Codex session; plain `doxa` may
reattach an existing Claude session in the project, whose picker will
correctly continue to show Claude models.

`launcher install` points at **the DOXA you ran it from**, by absolute
path, and prints that path and version — so a shortcut that would start
something unexpected shows up now, not in a month. It names any other
`doxa` on your `PATH` and changes nothing about it.

A daemon finalizes once every client has been detached for `--linger`
seconds (120 by default), or at once on `doxa stop`. `doxa --in-process`
runs the engine inside the TUI: no daemon, no detach, quitting finalizes
on the spot.

**Remote browser (optional).** From a checkout, set these values in
`~/.doxa/config.toml`, replacing the login with your own Tailscale login:

```toml
remote_enabled = true
remote_allowed_logins = "you@example.com"
```

Start a normal daemon-backed DOXA session, then run the bridge and Tailscale
Serve in separate terminals:

```sh
uv run doxa new
uv run --extra remote doxa-remote
tailscale serve --bg 47601
tailscale serve status    # prints the private tailnet URL
```

The bridge binds `127.0.0.1:47601` and refuses to start while remote access
is off or the allow-list is empty. It trusts `Tailscale-User-Login` only on
that loopback connection. Use **Serve**, never public Funnel: [Tailscale's
identity headers](https://tailscale.com/docs/features/tailscale-serve#identity-headers)
are provided for tailnet Serve traffic. The browser controls sessions that
remain on this machine; it does not move a worktree, daemon or model run to
the remote device. The local status bar names an attached browser driver
with an `◎ remote:<login>` chip. `tailscale serve off` stops sharing. See the
[remote plan](docs/plans/remote.md) for the current surface and remaining
work.

Then type a prompt and press enter. `ctrl+p` opens the palette, `ctrl+t` a
tab, `ctrl+r` searches past sessions, `shift+tab` cycles the permission
mode, a `!` line runs as a shell command, and `/help` lists every command
and key — marking any your terminal cannot send.

## Status

Beta, and a working daily driver for its author. Everything in
[What you get](#what-you-get) and in the [manual](docs/manual.md) is on
`main` and behaves as described; [CHANGELOG.md](CHANGELOG.md) has the
history. `main` is what the install script tracks by default; `v1.18.0`
pins this release, including shared LORE provenance, remote browser control,
and engine-specific model choices. Config keys, socket protocol and command
names can still change between minor versions.

**Specified, not built.** Seventeen documents sit in
[`docs/plans/`](docs/plans/) and each states its own status in its opening
lines — but two of those headers now lag their own code, so read them
against this list rather than instead of it. **Five have nothing behind
them:** `plugin-api` (no loader exists — v0.34.0 shipped only the seams one
could bind to), `mermaid`, `code-graph`, `sandbox`, `model-registry`.
**One is part-built:** `remote`. `doxa/remote_policy.py` refuses by default,
`doxa/peernet.py` bridges peer traffic between machines, and the optional
`doxa-remote` process now provides an initial browser renderer for local
daemon sessions behind Tailscale Serve. The richer renderer and other parts
of the [remote plan](docs/plans/remote.md) remain open. **One is an
experiment nobody has run:** `emergent-organization`, whose header calls
its messaging substrate unbuilt — that substrate is precisely what
shipped, while the experiment did not.

**Ten left that list by shipping:** `plugins` (v0.74.0), `split-panes`
(v0.91.0), `live-diff` (v0.92.0), `pane-groups` (v0.97.0, which inverted
the first), `session-sidebar` (v1.0.0), `peer-publishing` (v1.0.2),
`spawn-session` (v1.1.0), `collection-triage` (parts 0, 1 and 1b in v1.2.0;
parts 2 and 3 deliberately not), `engine-providers` (v1.4.0, the second
engine) and `rail-interaction` (v1.5.0). `plugins` is a different system
from `plugin-api` and is easy to confuse with it — it adopts *your own*
Claude Code plugins (commands, skills, agents; never hooks or MCP servers)
into the spawned CLI.

**The default fleet is a measurement harness, not an orchestrator.**
Nothing schedules sessions or assigns work between them, and no document
proposes that it should — the one plan about multi-agent structure asks
whether structure appears when nobody imposes it
([`docs/plans/emergent-organization.md`](docs/plans/emergent-organization.md)).
What `doxa-fleet` does: spawns N daemons into a `DOXA_HOME` and a peer
registry of the run's own, hands every one the byte-identical prompt at
the same moment, deals each slot an engine and model from `--pool
engine:model@weight` and runs it on that engine, runs `--memory-off K` of
them without memory, arms the peer tools for the run, splits
`--run-budget` per session, waits for the run to go quiet, tears it all
down and reports what leaked. The manifest carries the seed, the
assignment and the dispatch spread; the ledger carries every message.

**`--supervisor` is the other shape, and it is not a measurement.** It
adds one session at slot 0 which is the only one the operator's prompt
reaches; the `-n` workers are briefed by the harness — who their
supervisor is, that tasks arrive as peer messages, how to report back —
and are told nothing about the job, because dividing it is the
supervisor's work. Every worker is briefed before the supervisor is
prompted at all, the order is recorded, and the manifest carries the
mode, the roles and each session's own worktree. With no `--prompt` the
run is interactive: the supervisor waits for you to attach to it, and
does not end on quiet. [The manual has
it](docs/manual.md#supervisor-mode).

Since 1.14.0 the same harness has a front end in the TUI, and `doxa-fleet`
is a script entry rather than a name this README used for `python -m
doxa.fleet`. `/fleet start|status|stop|runs|attach|mesh|detach` reads the
flags with the parser the shell reads them with — one grammar, so the two
front ends cannot deal a different run from the same words, `--supervisor`
included — and opens the run in a read-only tab: the capacity and budget
arithmetic it was allowed to start under, which shape the run is and which
slot holds the prompt, the assignment table with each slot's role, the
dispatch spread, quiescence, the ledger tail and what leaked. That tab
reads the run's manifest and ledger and never the run object, so it keeps
working after the run's task has gone; closing it ends the run unless
`/fleet detach` was typed, because a fleet keeps N daemons armed and
spending with nobody typing. `/mesh [run-id]` serves that ledger as a
graph on loopback behind a token, and a `⌗ mesh :<port>` chip sits on the
status bar while it is up.

Measured on 1.13.0: `--pool deepseek@1,glm@1,codex@1 -n 5` dealt two
Codex, two DeepSeek and one GLM slot; all five spawned on their engine,
exchanged 17 messages (DeepSeek and GLM sent `ready` to every peer and
`ack` back across vendors; the Codex slots, which had no `peer_send`
at 1.13.0, received six and sent none), quiesced in 37 s and left no
process behind. A Codex model sends since 1.14.0: the MCP sidecar is
spawned and killed per `codex exec` run, so it forwards `peer_send` over a
per-session control socket to the engine, which sends on the session's own
limiter and ledger — one limiter across turns, one row per send, and the
lamps on the bar of the window that is watching. Two limits remain, both
stated by the harness itself: slots on codex, deepseek or glm report no
dollar figure and are not bounded by `--run-budget` (`--allow-unbudgeted`
is the switch that admits that); and a run's root must be a short path,
because every session's socket lives
under it and `AF_UNIX` allows 108 bytes. [`docs/fleet.md`](docs/fleet.md) has the whole of it, including
what the harness could not do at 128 sessions.

**`/msg` is no longer the only way a message is sent — this README said
otherwise until now.** A human typing `/msg` was the whole mechanism, and
a test asserted the operator registry never mentioned peers at all. That
sentence is retired: `peer_list` enumerates the sessions a model may
address — across repositories, not only this one — `peer_history` shows it
its own traffic so it can notice a loop, and `peer_send` reaches one
session or broadcasts to every one of them, and an arriving message can
start a turn in a session sitting idle. Both are off unless armed
(`agent_peer_send`, `peer_inbound_turns`) — though discovery is not, so a
model can enumerate your open sessions in other repositories with nothing
switched on — and both are read from your
environment or `~/.doxa/config.toml` — never from a file in the repository
a session happens to have open. The test was replaced rather than deleted:
it now pins the peer tool list to exactly three names, so a fourth fails
there before it reaches anyone. Every send, the model's and yours alike,
is rate-limited by deliveries rather than by calls and appended with its
full body to `$DOXA_HOME/peers/messages.jsonl`. `doxa/meshgraph.py` draws
that file as a live browser view of which session messages which — a graph
being the one artifact a terminal is honestly bad at — and `/mesh` is how
it is opened: bare for this machine's ledger, with a run id for that run's.
The server binds loopback, gates every route on a per-process token, and a
`⌗ mesh` chip on the status bar says while it is up; `/mesh stop` ends it.
It opens a browser only if `mesh_open_browser` is on, because DOXA runs in
terminals that have none.

Also absent: history drill-in past `/search`, and custom keybindings.

**Claude sessions older than v0.56.0 cannot be resumed.** That release
stopped DOXA and the CLI minting two session ids and pinned them to one,
and the fix cannot reach backwards: an older conversation is addressed by
an id the CLI's own store never knew, so it returns read-only and says so
first. A Codex or vendor session resumes from the record its own engine
kept beside the transcript, and refuses in the same words when there is
none.

Run the suite with `uv run pytest`. One part of it needs something the
machine may not have: `tests/test_mesh_page.py` loads `assets/mesh/` —
the peer-mesh page, DOXA's only browser surface — in a real headless
Chrome against a real graph server, and asks the page what it rendered
rather than reading its source. It finds Chrome at `/usr/bin/google-chrome`
or on `PATH`; `DOXA_CHROME` overrides that, and pointing it at a path
that does not exist is how to see what a machine without a browser sees:

```bash
uv run pytest tests/test_mesh_page.py    # the browser suite alone, ~38s
DOXA_CHROME=/nowhere uv run pytest       # every browser test skipped
```

Without a browser the eighteen tests that need one skip rather than fail
— the other eight check the server side of the same claims and run
anywhere — and the run says so in a marked line in its summary, because a
suite that never executed the page must not read like one that did.

## Non-goals

Provider-agnostic model routing. `--engine` is a deliberate per-session
choice, not a router picking a backend for you, and nothing load-balances
or falls back between them. Subscription auth remains the default path and
the reason the Claude engine exists; the three others are there because a
study that needs different models cannot be run on one. Replacing the LORE
Claude Code plugin, which keeps shipping the same core. A plugin API of DOXA's own — `docs/plans/plugin-api.md` is a
design with no loader behind it. Full Claude Code plugin compatibility:
`adopt_plugins` carries in commands, skills and agents from the plugins
you already have, and refuses their hooks and MCP servers unconditionally
— that refusal is the design, not a gap waiting to be filled.

## License

[AGPL-3.0-only](LICENSE) for everyone, including over a network; a
[commercial licence](LICENSE-COMMERCIAL.md) is available for uses AGPL's
terms don't suit. The DOXA name and mark are reserved — see
[TRADEMARK.md](TRADEMARK.md).
