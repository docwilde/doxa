<p align="center"><img src="assets/logo.png" width="560" alt="DOXA — belief earning knowledge"></p>

<p align="center">
  <img src="https://img.shields.io/badge/Rust%202.0-alpha.32-f59f00" alt="Rust 2.0 alpha.32 is the main frontend">
  <a href="https://github.com/docwilde/doxa/releases/tag/v2.0.0-alpha.32"><img src="https://img.shields.io/github/v/release/docwilde/doxa?include_prereleases&amp;sort=semver&amp;label=Rust%20preview&amp;color=e8590c" alt="latest Rust preview release"></a>
  <a href="https://github.com/docwilde/doxa/actions/workflows/rust-ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/docwilde/doxa/rust-ci.yml?branch=main&label=Rust%20CI" alt="Rust CI status"></a>
  <img src="https://img.shields.io/badge/TUI-Ratatui-2f9e44" alt="Rust TUI built with Ratatui">
  <img src="https://img.shields.io/badge/auth-provider%20CLI%20or%20API%20key-2f9e44" alt="authentication follows the selected engine">
</p>

> [!WARNING]
> **Alpha.** Rust 2.0 is the main DOXA frontend. Config and on-disk formats
> can change between alpha releases. Agents
> can edit files and run commands with your privileges. Read
> [Non-goals](#non-goals) before using it on important work.

The official latest GitHub release is [v2.0.0-alpha.32](https://github.com/docwilde/doxa/releases/tag/v2.0.0-alpha.32).
Rust 2.0 leads development; its alpha version still indicates that it is evolving.
Alpha.31 closes the audited Python 1.19 functional gaps: saved-session restoration,
full preferences, native LORE tools, interactive fleets, plugin commands and local
shell controls. The subsequent [source audit](docs/source-audit-2026-09-27.md)
documents the ownership/deadline fixes and remaining engineering debt. See the
[parity tracker](docs/rust-1.19-parity.md) for supported provider contracts and
retained safety boundaries.

**DOXA** is a terminal for coding agents. Development now leads with the
**Rust 2.0 alpha**, built with Ratatui and a native daemon. Run Claude, Codex,
DeepSeek, or GLM in separate sessions; close the terminal and reattach to their
daemons later. Claude currently uses a bundled Python SDK sidecar.
[LORE](https://github.com/docwilde/LORE)
shares user and repo memory across DOXA, Claude Code, and Codex, with
evidence-backed beliefs and an informational source-engine label. See the
[engine setup and capabilities](rust/README.md) guide.

![Rust 2.0 alpha terminal with a session rail and one wide Codex pane showing a fixture transcript and prompt](assets/shots/rust-hero.png)

*The lead image and gallery below are rendered by the real Rust 2.0
Ratatui frontend from deterministic fixture events. No provider calls or
account data are used. The fixtures and renderer live in
[`scripts/rust_gallery.py`](scripts/rust_gallery.py).*

## What you get

- **Four engines:** Run Claude, Codex, DeepSeek or GLM, with supported model, reasoning effort and permission changes during a session.
- **Flexible workspace:** Group tabs, split panes horizontally or vertically, and drag dividers; each pane has its own prompt.
- **Session recovery:** Restore saved tabs, drafts and layouts, safely resume recorded conversations, or reattach to running daemons.
- **Clear conversations:** Follow a processing spinner and live reasoning count, expand thinking or tool details, and open links with Ctrl+click.
- **Shared memory:** Browse and filter LORE's individual user/project memories, inspect belief evidence, and accept or reject exact reviewed claims.
- **Repo and worktree tools:** Browse folders, switch branches, inspect diffs, reject tracked hunks and perform guarded checkout recovery or cleanup.
- **Fleets and peers:** Review multi-agent plans, coordinate supervised runs, enforce reported spend limits and inspect peer activity in the TUI or browser mesh.
- **Usage at a glance:** Inspect reported context, plan, quota and API balance details; unavailable values stay unknown.
- **Keyboard and mouse:** Navigate tabs, chips and prompts with Tab, use slash completion, and select/copy text or paste into the prompt where supported.
- **Setup and customization:** Use interactive login, plugin management, categorized settings, help and updates; run private local shell commands with `!`.

See the [Rust guide](rust/README.md) for engine capabilities, shortcuts and
[compaction boundaries](rust/doxa-engines/README.md#compaction-review).

## Gallery

### Rust 2.0 alpha.32

![Rust belief browser showing Accept and Reject actions for each entry](assets/shots/rust-beliefs.png)

*Accept records a user confirmation in LORE. Reject retracts the belief from
active memory while retaining its history. Each action reviews the exact claim
above the prompt; retraction requires an explicit confirmation. Type in the
prompt to filter, or use Shift+A / Shift+R on the selected row. Hover an entry
for the delayed full-belief tooltip.*

![Rust belief hover preview showing the complete claim after a 500 ms delay](assets/shots/rust-belief-hover.png)

*Hover previews wrap the complete safe claim. Beliefs too large for the terminal
offer Enter to open the full review.*

![Rust directory picker expanded above a single session prompt](assets/shots/rust-repo-picker.png)

*Click the repo chip to browse folders. Enter opens the selected current folder
in a new tab; the upper border can be dragged to show more entries.*

![Rust command registry opened above the active prompt](assets/shots/rust-help.png)

*`/help` lists the supported Rust forms and describes local forms and provider boundaries.*

![Single Rust session pane showing collapsed tool calls below the assistant reply](assets/shots/rust-tool-activity.png)

*Tool calls stay collapsed below the latest reply until expanded.*

![Rust session pane with the tool activity section expanded to show call inputs and results](assets/shots/rust-tool-expanded.png)

*Select a tool section and press Enter, or click it, to inspect the bounded details.*

![Recovered Rust session showing multiline tool result detail inside an expanded section](assets/shots/rust-restored-tool.png)

*Claude tool details remain expandable after session recovery.*

![Rust session pane showing separate prompt and answer turns with a processing spinner](assets/shots/rust-processing.png)

*User messages have a warm highlight. The latest reply appears above its tool section, followed by the processing spinner.*

![Rust session pane with a live Reasoning/Thinking token count above the prompt](assets/shots/rust-reasoning.png)

*Streamed reasoning stays folded while its approximate token count updates.*

![Rust prompt showing slash command suggestions above the input](assets/shots/rust-commands.png)

*Typing a slash command filters local DOXA actions; Tab completes the selected entry.*

![Rust input request choices expanded above the prompt in a single session pane](assets/shots/rust-needs-input.png)

*A daemon input request expands above the active prompt.*

![Rust Claude session form with clickable model and prompt fields and a Start session action](assets/shots/rust-claude-session.png)

*Choosing Claude opens a session form. Click its fields and Start session, or use the keyboard.*

![Rust Claude permission picker above the prompt in a single session pane](assets/shots/rust-permissions.png)

*The permission picker expands from the chip row above the active prompt.*

![Rust reasoning effort picker above the prompt for a DeepSeek session](assets/shots/rust-effort.png)

*The effort chip reports the current session. Supported Codex and vendor choices apply to its next turn when idle.*

![Rust session history picker above the prompt in a single session pane](assets/shots/rust-history.png)

*Session history searches attached and archived transcripts and groups
scrubbed indexed excerpts beneath each matching session.*

![Rust queued prompt picker expanded above the prompt, showing scrubbed previews and a selected row](assets/shots/rust-queue.png)

*The queue picker shows waiting prompts and cancels the selected item by its ID.*

![Rust LORE memory submenu showing a table of individual curated user and project facts](assets/shots/rust-memory.png)

*The memory chip opens a table of individual curated facts with scope and
provenance. The prompt filters the rows while the submenu is open.*

![Rust user and project memory management above the session prompt](assets/shots/rust-memory-management.png)

*Browse scoped entries and review memory changes before applying them.*

![Rust native fleet launch review above the prompt](assets/shots/rust-fleet-review.png)

*Review the planned workers, budget and approval policy before launching a fleet.
The [extended gallery](docs/rust-gallery.md) also shows memory-change review and saved fleet views.*

These images come from `python3 scripts/rust_gallery.py`, which renders the
production `doxa_tui::ui::App` through Ratatui's test backend. The sessions,
events, and usage numbers are fixtures; they are examples of UI behavior,
not a live provider run.

## Install

### Rust 2.0 alpha

```sh
curl -fsSL https://raw.githubusercontent.com/docwilde/doxa/main/scripts/install.sh | sh
```

The installer builds the Rust frontend and daemon from `main` and installs the
Rust `doxa` command in `~/.local/bin` (or `DOXA_RUST_BIN_DIR`). Pass a tag such
as `v2.0.0-alpha.32` after `sh -s --` to pin a release. It uses Git, Cargo,
Python 3.11+, and `uv`; the Python environment it creates is private to the
LORE and Claude sidecars. No Python frontend command is installed. See the
[Rust guide](rust/README.md) for provider setup and current limits.
On Linux, it also installs a per-user application menu entry and icons under
`$XDG_DATA_HOME` (default `~/.local/share`). The entry launches the installed
Rust `doxa` by absolute path. Set `DOXA_NO_LAUNCHER=1` to skip it.
If an older `uv tool` install owns `doxa`, run `uv tool uninstall doxa`
before installing Rust; the installer reports when another `doxa` on `PATH`
would shadow its launcher.

From a checkout, `cargo build --locked` builds the Rust frontend and daemon;
`./task build`, `./task run`, and `./task install` provide the matching local
launcher workflow. Install
builds committed `HEAD` with the same locked sidecars and launcher as the
release installer. If an existing local `main` predates the Rust files, run
`git pull --ff-only` after checking it out; `git checkout main` alone does not
fetch newer commits.
Use `doxa help` for commands and `doxa update` to rebuild an installed Rust
launcher from `main`.

## Quickstart

Check dependencies, start a session, and reattach later:

```sh
doxa doctor --engine codex
doxa new --engine codex
doxa list
```

The [Rust guide](rust/README.md) covers Claude, DeepSeek, and GLM setup.

## Status

Rust 2.0 alpha is the main line. The [Rust guide](rust/README.md) tracks
what is implemented and what still needs porting. Existing Python 1.x releases
and their [manual](docs/manual.md) remain available for historical reference;
the Python SDK and LORE sidecar modules remain internal runtime dependencies.

Rust CI tests the frontend, native daemon, protocol, LORE bridge, installer,
and compatibility paths. See the [Rust UI benchmark](docs/rust-ui-benchmark-2026-09-27.md)
for rendering, event-loop, scrolling, and resize measurements.

## Non-goals

- **Automatic model routing:** you choose an engine per session; DOXA does
  not load-balance or fail over between providers.
- **Replacing LORE:** DOXA uses the same core as the Claude Code and Codex
  plugins.
- **Full Claude plugin compatibility:** the 2.0 alpha uses a Claude SDK
  sidecar. DOXA's [plugin API](docs/plans/plugin-api.md) remains a design.

## License

[AGPL-3.0-only](LICENSE) for everyone, including over a network; a
[commercial licence](LICENSE-COMMERCIAL.md) is available for uses AGPL's
terms don't suit. The DOXA name and mark are reserved — see
[TRADEMARK.md](TRADEMARK.md).
