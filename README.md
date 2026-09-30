<p align="center"><img src="assets/logo.png" width="560" alt="DOXA — belief earning knowledge"></p>

<p align="center">
  <a href="https://github.com/docwilde/doxa/releases/latest"><img src="https://img.shields.io/github/v/release/docwilde/doxa?include_prereleases&amp;sort=semver&amp;label=Rust%20release&amp;color=e8590c" alt="Latest DOXA release"></a>
  <a href="https://github.com/docwilde/doxa/actions/workflows/rust-ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/docwilde/doxa/rust-ci.yml?branch=main&amp;label=Rust%20CI" alt="Rust CI status"></a>
  <img src="https://img.shields.io/badge/TUI-Ratatui-2f9e44" alt="Ratatui terminal interface">
</p>

**DOXA** is a Rust terminal for coding agents. Run Claude, Codex, DeepSeek, and GLM in separate tabs or panes, then reattach to their daemons after closing the terminal. Its integrated [LORE](https://github.com/docwilde/LORE) runtime shares reviewed user and project memory across DOXA, Claude Code, and Codex.

> [!WARNING]
> Rust 2.0 is an alpha. Configuration and stored formats may change. Agents can edit files and run commands with your privileges; review [scope and limits](#scope-and-limits) before using DOXA on important work.

![DOXA running a Codex session with an expandable tool section](assets/shots/rust-hero.png)

*Captured from the running Rust app in an isolated example repository. See the [capture record](docs/rust-gallery.md).*

## What you get

- **Four engines.** Choose Claude, Codex, DeepSeek, or GLM per session; available models and reasoning levels follow the selected engine.
- **A flexible workspace.** Group tabs, split panes, drag dividers, and keep a separate prompt in each pane.
- **Recoverable sessions.** Restore saved layouts and conversations or reattach to running daemons. The rail shows queued prompts, active turns, and unread results.
- **Readable turns.** Watch streamed responses, expand reasoning and individual tool calls, and open links with Ctrl+click.
- **Reviewed memory.** Browse LORE facts and beliefs, inspect evidence, and accept or reject proposed beliefs.
- **Repo tools.** Navigate repositories and worktrees, switch branches, inspect diffs, and review tracked changes.
- **Peer coordination.** Inspect peer messages in the TUI or browser map and run supervised fleets with spend controls.
- **Custom controls.** Manage provider login, API keys, plugins, and settings in the TUI. Remap window shortcuts without rebuilding.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/docwilde/doxa/main/scripts/install.sh | sh
```

The installer builds the Rust frontend, daemon, and LORE carrier and places `doxa` in `~/.local/bin` by default. It also adds a Linux application-menu entry; set `DOXA_NO_LAUNCHER=1` to skip that step. Use `doxa update` for a newer build or pass a tag after `sh -s --` to pin a release.

Claude requires its CLI. Codex uses a private protected app server; the installer builds it when Codex is present. DeepSeek and GLM use API credentials configured through `/setup` or provider environment variables. See the [Rust guide](rust/README.md) for setup, build requirements, and engine capabilities.

From a checkout, use `./task build`, `./task run`, or `./task install`. Run `doxa help` for the CLI command list.

## Quickstart

```sh
doxa doctor --engine codex
doxa new --engine codex
doxa list
```

Ctrl+T opens the new-tab engine picker. Ctrl+X closes the active tab while its daemon keeps running; Ctrl+Q exits DOXA and leaves sessions detached. Reattach with `doxa attach SESSION_ID`. Tab reaches the session rail when visible, and the mouse wheel switches tabs over a tab header. Use `/settings` → **Keys** to change window shortcuts immediately, or `doxa settings set key_new_tab Alt+N` to change one for the next launch.

`/pending` and `/lore:pending --cluster` open LORE proposals for the current project plus user scope with either engine. `lore status` reports the global pending count across all projects.

## Gallery

These frames come from the running Rust TUI with isolated DOXA and LORE state. The [capture record](docs/rust-gallery.md) explains the example data and reproduction steps.

### Welcome and sessions

![The Greek block DOXA banner and a ready session prompt](assets/shots/rust-welcome.png)

![Three sessions in the rail with a tab overflow indicator](assets/shots/rust-sessions.png)

### Curated memory

![A selectable table of LORE facts with a filter and scrollbar](assets/shots/rust-curated-memory.png)

### Beliefs

![A table of recent beliefs with review actions, evidence, and selection](assets/shots/rust-beliefs.png)

### Tool details

![Individual tool calls in an expandable section](assets/shots/rust-tool-entries.png)

### Commands and settings

![Slash-command completion above the prompt](assets/shots/rust-commands.png)

![The running app's command help](assets/shots/rust-help.png)

![The configurable Keys page in DOXA settings](assets/shots/rust-settings.png)

## Scope and limits

Rust 2.0 is the main DOXA line and Linux is the verified platform. The full protected runtime is not supported on macOS or Windows. DOXA does not automatically route between providers, and plugin adoption excludes foreign hooks and MCP servers.

The [Rust guide](rust/README.md) covers workflows and provider limits. The [parity tracker](docs/rust-1.19-parity.md), [verification records](docs/live-provider-verification-2026-09-30.md), [source audit](docs/source-audit-2026-09-27.md), and [UI benchmark](docs/rust-ui-benchmark-2026-09-30.md) hold detailed evidence and remaining gates. Python 1.x documentation remains [historical reference](docs/manual.md); normal installation uses Rust.

## License

[AGPL-3.0-only](LICENSE) applies, including over a network. A [commercial licence](LICENSE-COMMERCIAL.md) is available; the DOXA name and mark are reserved under the [trademark policy](TRADEMARK.md).
