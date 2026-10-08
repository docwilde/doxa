# DOXA Rust guide

The Rust 2.0 frontend is `doxa-rs`; the installer exposes it as `doxa`. Claude uses its CLI, Codex uses a private app server, and DeepSeek and GLM use Rust API clients. Integrated LORE handles reviewed memory, indexing, and secret scrubbing. The installed runtime needs no Python interpreter.

## Contents

- [Install and build](#install-and-build)
- [Start and recover sessions](#start-and-recover-sessions)
- [Use the workspace](#use-the-workspace)
- [Review and permissions](#review-and-permissions)
- [Memory and context](#memory-and-context)
- [Fleets and peers](#fleets-and-peers)
- [Remote access](#remote-access)
- [Verification and limits](#verification-and-limits)

## Install and build

```sh
curl -fsSL https://raw.githubusercontent.com/docwilde/doxa/main/scripts/install.sh | sh
doxa doctor --engine codex
doxa new --engine codex
```

Pass a release tag after `sh -s --` to pin it. Installation requires Git, Cargo, and Rust; binaries go to `~/.local/bin` unless `DOXA_RUST_BIN_DIR` is set. The installer adds a Linux menu entry or macOS `~/Applications/DOXA.command`; set `DOXA_NO_LAUNCHER=1` to skip it. `doxa update` updates an installed launcher. On macOS, the installer skips protected Codex. `doxa help` lists all CLI commands.

From a checkout:

```sh
cargo build --locked
./task doctor
./task run
./task test
```

`./task` uses incremental builds in `target/rust-task`; ordinary Cargo uses `target`. `./task install` installs committed HEAD, including native LORE, so commit local changes first. The optional protected Codex installer uses Python during its build; the running DOXA app does not. [Codex build details](doxa-engines/README.md#install-the-protected-provider).

Claude needs a signed-in Claude Code CLI. Codex needs a signed-in CLI and the protected provider build. Add DeepSeek or z.ai keys with `/setup` or `DEEPSEEK_API_KEY` / `ZAI_API_KEY`. DOXA stores setup keys in an owner-only credentials file; they override inherited environment keys and are never copied from project `.env` files. [Engine capability matrix](../docs/engine-capabilities.md).

## Start and recover sessions

`doxa` restores this project's saved tabs or starts the configured engine. `doxa new --engine codex|claude|deepseek|glm` always starts another session; `doxa list`, `attach`, and `stop` manage live daemons. Use `--model` and supported `--effort` choices. Titles default to `model@repo:branch` in Git and can be renamed.

`/resume [query]` opens verified saved state; `/attach` lists live daemons. Closing a tab leaves its daemon running. Ctrl+Q closes the window while all running sessions stay detached. Saved split layouts and drafts restore on launch. Historical data that the provider never stored cannot be reconstructed; unsafe or uncertain recovery stays read-only with a reason.

Git sessions normally get managed linked worktrees. `DOXA_WORKTREE=0` or `worktree_per_session=false` disables them. `new --branch NAME` selects a base; `/branch [name]` changes an idle, clean worktree. `/diff` or F2 opens a bounded diff, and F4 keeps it beside the session. `worktrees list` previews orphans; `worktrees cleanup FULL_ID --confirm` deletes only after ownership and Git state are rechecked. [Lifecycle contract](../docs/worktree-lifecycle.md).

Start with `--isolation native|docker-open|docker-offline` or choose a profile in the new-session picker. Linux Docker profiles require a local rootless Engine and a pinned image. The worker checks effective cgroup memory, CPU, PID, and swap limits before admission and CLI provider turns; the chip shows the verified policy. `/isolation PROFILE --confirm` changes an idle session; a backend change verifies and resumes the same conversation. [Setup and limits](../docs/session-isolation.md).

## Use the workspace

| Action | Default control |
| --- | --- |
| Open an engine picker | Ctrl+T |
| Split beside / above | Alt+V / Alt+H |
| Switch tab / pane prompt | Ctrl+Left or Right / Shift+Left or Right |
| Open action palette / peer map | Ctrl+P / Ctrl+M |
| Search turns | Ctrl+R or `/search TEXT` |
| Open tool calls | Alt+T |
| Close tab / stop daemon / exit window | Ctrl+W / Ctrl+X / Ctrl+Q |

Tab reaches the project rail when visible. `/split`, `/vsplit`, `/pane`, and `/movepane` manage up to 16 pane groups and 256 tabs. Divider dragging preserves minimum sizes. `/settings` → **Keys** remaps window shortcuts immediately; `doxa settings set key_new_tab Alt+N` applies on the next launch. Duplicate or malformed chords are refused.

Typing `/` shows completion; unsupported DOXA commands stay in the draft with an error. `/model`, `/effort`, `/mode`, and `/engine` show supported choices. Live model and mode changes require an idle session and empty queue; selecting another engine starts a new session. `/queue` previews or cancels waiting prompts. `/cd PATH` opens a tab in a verified directory without moving an existing daemon.

The session rail groups uncollected sessions by project and keeps named collections in saved order. `/collection new` suggests a name from the active task and known project; add an explicit customer in `[project_customers]` in `~/.doxa/config.toml`, keyed by the session's absolute workspace path. Missing context is omitted, and `/collection new NAME` and `/collection rename` keep your chosen labels. `/collection sort urgency` orders whole groups by needs-input (`!`), reported context at least 50% (`ctx`), then completed-unseen (`new`); `/collection sort manual` restores the saved order. Sorting is off by default and waits until activity settles and the pointer and keyboard leave the rail. Session rows within each group never move. LORE proposal counts are not a per-session current state, so they do not affect this order.

## Review and permissions

Codex's permission chip offers `on-request`, `auto`, and `full-access`. `on-request` reviews protected commands, file changes, and profile requests inline. `auto` keeps the Codex sandbox; `full-access` disables it. The choice applies to the idle session's next turn and restores with that session. DOXA peer and LORE tools retain separate review in every mode. DeepSeek and GLM have no provider permission mode; their peer and LORE calls require individual approval. [Engine capabilities](../docs/engine-capabilities.md#review-and-accounting).

Permission answers bind to an exact pending request. Complete summaries must be read before full approval; changed or stale requests cannot inherit an answer. Secret-input requests wait for a private masked-input interface. Ctrl+Delete asks before stopping a daemon and removing its verified DOXA transcript; provider archives remain separate. Diff hunk rejection checks the current patch and staged state again before applying it.

Links, Markdown, reasoning, and individual tool calls render in the transcript. Standalone local images inside the session workspace have bounded previews. Mermaid fences can render through an explicitly configured local sandboxed renderer; source remains visible otherwise. `doxa doctor` checks its path policy and runs a bounded PNG smoke render, but does not certify Mermaid CLI fidelity. [Image and diagram limits](../docs/terminal-images.md) explain both paths. Ctrl+click opens HTTP(S) links. Review panes and tool output have bounded sizes. Search uses scrubbed indexed excerpts and bounded fallback scans. Unverified external entries cannot become resumable sessions.

## Memory and context

The memory chip browses user and project facts; the belief browser supports evidence review, notes, accept, and reject. `/pending` opens proposals for the current project and global store. Mutations require a current snapshot and full review. DOXA calls canonical LORE and fails explicitly if its native carrier is unavailable; it keeps no separate memory store.

`/context` shows reported provider telemetry and snapshot details. `/usage` uses reported accounting and labels estimates. Missing or stale component counts and quota stay unknown. DeepSeek and GLM costs are estimates based on reported tokens and dated rates. Saved API keys are redacted before transcripts or memory boundaries. [LORE](https://github.com/docwilde/LORE) · [Engine accounting](../docs/engine-capabilities.md#review-and-accounting).

`doxa codegraph file PATH`, `symbol NAME`, `imports PATH`, and `calls PATH` return bounded, fresh Rust syntax queries from the current Git worktree. Call targets are lexical candidates, not resolved bindings; other languages are unsupported and nothing is written to LORE. [Query limits](../docs/plans/code-graph.md).

`/setup` handles provider credentials and defaults. `/plugins` and `/reload-plugins` control sanitized Claude plugin adoption for future sessions. Separately, owner-approved [native text plugins](../docs/native-plugins.md) add read-only local slash commands from private TOML manifests; executable native plugins remain open work. `/settings` edits validated preferences; environment overrides remain read-only.

## Fleets and peers

`fleet preflight` checks capacity, spend ceiling, and socket paths before `fleet start`. Use `--worker-task INDEX:TEXT` to give each worker a frozen deliverable and optional `--worker-path INDEX:RELATIVE_PREFIX` to narrow its file scope. `fleet runs|status|attach|stop|resume` operate on owned manifests and verified slots. `/fleet` shows runs in the TUI; starting or resuming there requires reviewing and arming the complete plan. Budgeted resume needs complete persisted accounting. [Fleet guide](../docs/fleet.md).

Use `--worker-after INDEX:PREDECESSOR` to hold a worker until its predecessor finishes and the operator reviews the host-recorded checkpoint and typed handoff. Dependency plans require an acting coordinator, independent review, and Docker isolation. `fleet dependency-evidence`, `dependency-review`, then `dependency-release` expose the explicit release flow; the review reports `tests_verified: false`. [Dependency gate](../docs/fleet-supervision.md).

Select an independent supervisor with `--alignment-supervisor PROVIDER:MODEL`. Choose a separate fast message judge with `--message-judge llm:PROVIDER:MODEL` or `jev:MODEL`, and choose `--message-review off|shadow|enforce`. `/settings` → **Fleet** stores defaults. The acting `--supervisor` is a worker; the independent reviewer reads evidence and cannot grant its own approvals. Host gates bind peer traffic to the approved charter and assignments. [Supervisor contract](../docs/fleet-supervision.md).

`/peers` or Ctrl+M opens the peer map; `/msg PEER TEXT` sends a scrubbed same-project message. The optional private mesh shows fleet relationships in a browser. Remote peer routes use authenticated private sockets and verified rosters. [Transport contract](../docs/native-peernet.md).

## Remote access

Remote access is off by default. For a private browser view on the session host, set `DOXA_REMOTE_ENABLED=1` and `DOXA_REMOTE_ALLOWED_LOGINS=you@example.com`, run `doxa remote serve`, and point Tailscale Serve at the printed owner-private socket. The view reads recent turns, follows events, sends prompts, and answers pending requests.

For cross-machine control, run `doxa-hub` behind Tailscale Serve on a private server, then `doxa remote connect HUB_URL HOST_ID` on the session host. The hub browser, `doxa remote list/send/answer`, and `doxa remote tui HUB_URL` can control registered sessions. Remote TUI tabs use the normal keys; Ctrl+R opens history and PageUp fetches older records. `/remote-connect HUB_URL HOST_ID` shares sessions while the local window stays open; `/remote-control HUB_URL` adds remote tabs marked `◎` beside local ones. The hub is volatile, so inspect an uncertain command before retrying it. [Remote hub design](../docs/plans/remote-hub.md).

For native end-to-end encryption, generate a shared key with `doxa remote keygen /absolute/private/remote.key`. Keep it owner-only and set `DOXA_REMOTE_E2EE_KEY_FILE` to its path on both host and client. Transcript and control content stays opaque to the hub; session presence and event metadata remain visible. Encrypted sessions work in the native TUI, CLI, and separately installed [Chrome extension](../browser-extension/README.md), but not the hub-served browser. The hub can also deliver generic Web Push completion and input alerts when configured with a private VAPID key. The [Android source client](../android-client/README.md) can control private hub sessions and show generic local alerts while connected; device and two-host tailnet QA remain open, and native background push is not implemented.

## Verification and limits

Run `./task test` for the Rust suite. CI checks portable crates, daemon behavior, and provider fixtures with disposable stores. Linux has bounded live checks for all four engines. macOS builds and transport tests run in CI, but authenticated provider sessions still need live verification. Protected Codex needs Linux process ownership and private compaction hooks; Windows is unsupported. [Platform record](../docs/platform-verification.md) · [Latest provider record](../docs/live-provider-verification-2026-10-04.md).

The protected Codex build verifies a pinned synchronous `PreCompact` hook before starting a thread. Missing or failed LORE review blocks compaction; stock app servers cannot run protected turns. The private Linux launcher owns and reaps provider descendants. [Codex engine contract](doxa-engines/README.md) records exact build and verification details. [Current gallery](../docs/rust-gallery.md) shows real TUI captures and isolated browser samples.

Python 1.x is archived at [v1.19.0](https://github.com/docwilde/doxa/tree/v1.19.0); its [manual](../docs/manual.md) documents that version.
