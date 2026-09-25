<p align="center"><img src="assets/logo.png" width="560" alt="DOXA — belief earning knowledge"></p>

<p align="center">
  <img src="https://img.shields.io/badge/Rust%202.0-alpha.9-f59f00" alt="Rust 2.0 alpha.9 is the leading development line">
  <a href="https://github.com/docwilde/doxa/releases"><img src="https://img.shields.io/github/v/release/docwilde/doxa?label=release&color=e8590c" alt="latest release"></a>
  <a href="https://github.com/docwilde/doxa/actions/workflows/rust-ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/docwilde/doxa/rust-ci.yml?branch=rust%2F2.0&label=Rust%20CI" alt="Rust CI status"></a>
  <img src="https://img.shields.io/badge/TUI-Ratatui-2f9e44" alt="Rust TUI built with Ratatui">
  <img src="https://img.shields.io/badge/auth-provider%20CLI%20or%20API%20key-2f9e44" alt="authentication follows the selected engine">
</p>

> [!WARNING]
> **Alpha.** The Rust 2.0 preview is the leading development line; Python 1.x
> remains available for features still being ported. Config and on-disk formats
> can change between releases. Agents
> can edit files and run commands with your privileges. Read
> [Non-goals](#non-goals) before using it on important work.

**DOXA** is a terminal for coding agents. Development now leads with the
**Rust 2.0 alpha**, built with Ratatui and a native daemon. Run Claude, Codex,
DeepSeek, or GLM in separate sessions; close the terminal and reattach to their
daemons later. Claude currently uses a bundled Python SDK sidecar.
[LORE](https://github.com/docwilde/LORE)
shares user and repo memory across DOXA, Claude Code, and Codex, with
evidence-backed beliefs and an informational source-engine label. See the
[engine capabilities](docs/manual.md#engine-capabilities) and
[LORE integration](docs/manual.md#lore-integration) guides.

![DOXA shell: three tabs, one per model tier; a turn answered with a table of belief ids and status above a collapsed tool-calls fold; a status bar led by the permission-mode chip](assets/shots/hero.png)

*Every image here is rendered headlessly from the real app — scripted, no
spend, fake account numbers. See
[screenshots](docs/manual.md#screenshots).*

## What you get

**Rust 2.0 alpha:** Launch and reattach [four engines](rust/README.md), work
across grouped tabs and two-pane splits with separate prompts, inspect bounded
worktree diffs and tool cards, change supported models and permissions, and
browse LORE beliefs and peer activity. The [Rust guide](rust/README.md)
describes its current capabilities and limits.

**Python 1.x:** Keep using the [manual](docs/manual.md) for worktree creation
and cleanup, diff hunk rejection, LORE proposal approval, the full command
palette, [fleets](docs/fleet.md), and [remote access](docs/plans/remote.md)
while those workflows move to Rust.

## Gallery

![A session left, its live diff right, headed '2 files changed, +9 -1 against main'; one hunk carries an amber 'reject queued' badge above a disabled reject button](assets/shots/live-diff.png)

*Review a live diff and reject individual hunks.*

![One tab split into two panes, each its own session: same identity block, different models, separate transcripts, a status bar apiece](assets/shots/split-panes.png)

*Split panes run independent sessions.*

![A turn's tool-call count ticking 1 to 3 as chips land, the marker counting 5s, 9s, 14s through the silent wait](assets/shots/tool-calls.gif)

*Expand a turn to inspect tools and results.*

![A lore_belief_search chip expanded, listing one STEER belief with an outcome count and one CITE-only belief](assets/shots/memory.png)

*LORE calls appear in the transcript.*

![The beliefs picker grouped by scope, each row carrying inline actions 'y confirmed', 'c contradicted', 's stale', 'r retract', 'g graph'](assets/shots/beliefs-picker.png)

*Inspect and rate evidence-backed beliefs.*

![/context as a 10 by 20 grid of 200 cells, headlined 'in use 60,910 / 180,000 tokens - 33.8%'](assets/shots/context.png)

*Context usage comes from the engine’s own accounting.*

![An AskUserQuestion dialog above the prompt, asking which environment a migration should target](assets/shots/needs-input.gif)

*Questions and approvals appear in a dialog.*

![The peers chip opening a roster of three sessions with titles and token totals, one detached, one mid-first-turn showing 'tok --'](assets/shots/peers.gif)

*The peer roster shows session state and usage.*

![A peer message block above a system line reading 'a peer message started this turn', and the turn it started, whose fold header carries the sender where a typed prompt would be](assets/shots/peer-turn.png)

*Peer-triggered turns name their sender.*

![The right end of the status bar: 'peers 2 (1⌁)', then an up arrow with a filled lamp and a down arrow with a hollow one](assets/shots/peer-lights.png)

*Indicators show recent peer traffic.*

![A fleet tab headed 'fleet 20260919T113402-8c41 — finished' over 'mode symmetric': sixteen worker slots dealt claude, codex, deepseek and glm, three reading 'OFF' under mem, above 'dispatch spread 11 ms across 16 sessions', 'quiesced after 58s' and thirty ledger lines](assets/shots/fleet.png)

*Fleet results show assignments, budgets, and outcomes.*

![The peer mesh in a browser under '9 sessions 32 messages 46 pairs 4 broadcasts': nine session nodes rimmed by engine colour, 'release notes' selected in white, and a side panel reading '/home/you/repo/doxa', 'claude · own turn running', '3 sent 10 received 8 peers' above a feed of message bodies, one tagged BCAST to 8 recipients](assets/shots/mesh.png)

*The browser mesh shows peer-message traffic.*

![The permission-mode chip cycling: grey 'default', teal 'plan', amber 'auto', red 'bypassPermissions'](assets/shots/permission-mode.gif)

*The permission chip shows the active mode.*

![A session in a plain directory: the identity chip reads 'dir design-notes' with no branch half](assets/shots/folder-chip.png)

*Directory sessions show their location.*

![An amber '⇅ sync 2m ↑3 ⚠1' chip in the status bar, between the branch chip and the subscription chip](assets/shots/sync-chip.png)

*The optional sync chip shows freshness and pending work.*

More screenshots are catalogued in the [manual](docs/manual.md#screenshots).

## Install

### Rust 2.0 alpha (leading development line)

```sh
curl -fsSL https://raw.githubusercontent.com/docwilde/doxa/rust/2.0/scripts/install.sh | sh -s -- --rust v2.0.0-alpha.9
```

This builds the tagged Rust preview with Git and Cargo and installs `doxa-rs`,
`doxa-daemon-rs`, and the Claude sidecar in `~/.local/bin` (or
`DOXA_RUST_BIN_DIR`). Use `--rust rust/2.0` to track the development branch.
The current alpha still needs a Python environment with DOXA and LORE for its
memory bridge, and the Claude Agent SDK for Claude sessions. Run
`doxa-rs doctor --engine codex` to check your setup; see the
[Rust guide](rust/README.md) for interpreter and provider setup. The Python
`doxa` command is installed separately.

### Python 1.x (full feature fallback)

```sh
curl -fsSL https://raw.githubusercontent.com/docwilde/doxa/main/scripts/install.sh | sh
```

This requires Python 3.11+, [`uv`](https://docs.astral.sh/uv/), and Git. The
installer tracks `main` by default; `sh -s -- v1.19.0` pins that release.
Provider CLIs can be installed and signed in later. DOXA is not on PyPI.

From a checkout:

```sh
git clone https://github.com/docwilde/doxa && cd doxa
uv sync
uv run doxa
```

`uv sync` installs a pinned LORE core. If the Claude Code LORE plugin is
installed, DOXA uses its copy instead; `/about` shows which one loaded.
An install tracking `main` can use `/update` or `/update --restart` in the
TUI. Tag-pinned installs stay pinned.

## Quickstart

For the Rust alpha, check the bridge and start or attach to a session:

```sh
doxa-rs doctor --engine codex
doxa-rs new --engine codex --lore-python /absolute/path/to/python-with-doxa-and-lore
doxa-rs list
```

The [Rust guide](rust/README.md) covers Claude, DeepSeek, and GLM setup.
For Python 1.x, use:

```sh
uv run doxa                         # open or restore this repo's sessions
uv run doxa new --engine codex      # start a fresh Codex session
uv run doxa new --branch <name>     # start in a worktree
uv run doxa attach                  # reattach to a session
uv run doxa stop                    # finalize and stop a session
uv run doxa doctor                  # check the local setup
```

Use `/engine` to choose the default for **new** sessions; the current
session keeps its engine. `/model` lists models for that session's engine.
Codex reads the signed-in CLI's model catalogue; Claude uses an
account-matched CLI cache and labels stale or fallback choices. See
[engine capabilities](docs/manual.md#engine-capabilities) for differences.

`/login claude` and `/login codex` start each provider's sign-in flow;
`/logout claude` and `/logout codex` sign out. Open a new session after
changing accounts. DeepSeek and GLM use their own API keys.

For browser control from another device, opt in to the
[Tailscale Serve bridge](docs/plans/remote.md). It controls sessions still
running on this machine. `/help` lists TUI commands and available keys.

## Status

Rust 2.0 alpha is the leading development line on `rust/2.0`. It is not yet
at Python 1.x feature parity. The Python release remains on `main`; the
[changelog](CHANGELOG.md) and [manual](docs/manual.md) cover that line.

For Python 1.x:

- **Remote control:** the browser bridge is opt-in and still lacks rich diffs, images, and notifications. See the [remote plan](docs/plans/remote.md).
- **Fleets:** mixed-engine runs and a supervisor mode are available; engines without reported costs need explicit budget opt-in. See the [fleet guide](docs/fleet.md).
- **Older sessions:** Claude sessions created before v0.56.0 may be read-only on resume.
- **Plans:** [`docs/plans/`](docs/plans/) separates shipped work from designs still in progress.

Rust CI tests the frontend, native daemon, protocol, LORE bridge, and
compatibility paths. For Python 1.x, run `uv run pytest`; browser tests skip
when Chrome is unavailable. See the [manual](docs/manual.md#screenshots)
for screenshot and browser-test details.

## Non-goals

- **Automatic model routing:** you choose an engine per session; DOXA does
  not load-balance or fail over between providers.
- **Replacing LORE:** DOXA uses the same core as the Claude Code and Codex
  plugins.
- **Full Claude plugin compatibility:** `adopt_plugins` imports commands,
  skills, and agents, but not hooks or MCP servers. DOXA's own
  [plugin API](docs/plans/plugin-api.md) remains a design.

## License

[AGPL-3.0-only](LICENSE) for everyone, including over a network; a
[commercial licence](LICENSE-COMMERCIAL.md) is available for uses AGPL's
terms don't suit. The DOXA name and mark are reserved — see
[TRADEMARK.md](TRADEMARK.md).
