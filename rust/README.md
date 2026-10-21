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

Startup and New Session show local Claude/Codex login warnings before a prompt. An expired access token with a refresh credential is marked as needing recovery; its recovery is unverified. Credentials present on disk do not prove server validity. The background scan makes no provider requests or refresh attempts. A session's actual authentication rejection stays visible until that session completes a successful turn, even if the host login changes. On restart, retained Codex error records can show a previous authentication failure with recovery unverified; older Claude transcripts lack trusted terminal authentication metadata for that historical warning.

## Start and recover sessions

`doxa` restores this project's saved tabs or starts the configured engine. `doxa new --engine codex|claude|deepseek|glm` always starts another session; `doxa list`, `attach`, and `stop` manage live daemons. Use `--model` and supported `--effort` choices. Titles default to `model@repo:branch` in Git and can be renamed.

`/resume [query]` opens verified saved state; `/attach` lists live daemons. Closing a tab leaves its daemon running. Ctrl+Q closes the window while all running sessions stay detached. Saved split layouts and drafts restore on launch. Typing into a saved tab starts verified continuation and retains the draft on failure. Verified older Codex exec checkpoints use the protected app-server transport; `DOXA_CODEX_MIGRATE_APPSERVER=0` keeps the explicit legacy refusal. Continuation measures history plus the new request against reported context or a conservative bound. Fitting history and reviewed summaries remain intact; overflow keeps marked recent context while preserving full originals, permission policy and budget ledger. Historical data that the provider never stored cannot be reconstructed; unsafe or incomplete recovery reports a specific refusal.

New and explicitly attached sessions save their recovery identity before the first response. Their daemons run independently of the terminal, so quitting or closing its window during a turn leaves the work running. Restart reattaches the same session without resubmitting the pending prompt. An explicit attachment reopens that saved tab; unrelated closed tabs and other windows' layouts remain preserved. If the recovery checkpoint cannot be saved, DOXA reports the running session ID for attachment.

Git sessions normally get managed linked worktrees. `DOXA_WORKTREE=0` or `worktree_per_session=false` disables them. `new --branch NAME` selects a base; `/branch [name]` changes an idle, clean worktree. `/diff`, F2 and F4 toggle a bounded vertical diff split inside the active session window, retaining its prompt and neighboring windows. A narrow window asks to be widened. `worktrees list` previews orphans; `worktrees cleanup FULL_ID --confirm` deletes only after ownership and Git state are rechecked. [Lifecycle contract](../docs/worktree-lifecycle.md).

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
| Attach prompt files | Attach chip, palette **Attach files**, or Alt+A |
| Close tab / stop daemon / exit window | Ctrl+W / Ctrl+X / Ctrl+Q |

Tab reaches the project rail when visible. `/split`, `/vsplit`, `/pane`, and `/movepane` manage up to 16 pane groups and 256 tabs. Divider dragging preserves minimum sizes. `/settings` → **Keys** remaps window shortcuts immediately; `doxa settings set key_new_tab Alt+N` applies on the next launch. Duplicate or malformed chords are refused.

Each window's other tabs stack indented beneath its first tab in the rail.
Selecting a tab activates its existing window. Selection/activity precedes the
type marker: normal tab, fleet group, supervisor or worker. **Settings →
Appearance → nerd font** offers Auto (default), On and Off. Auto uses icons for
recognized direct Kitty/WezTerm clients; stock Mint/VTE, unknown terminals, SSH
and multiplexers use `[T]`, `[F]`, `[S]`, `[W]` and `git`. It checks terminal
identity, not glyph coverage. `DOXA_NERD_FONT=auto|on|off` overrides the choice;
choose Off if your font still shows boxes. No font is installed automatically.

Use **Attach** / **Alt+A** to review image or UTF-8 files on the current draft.
Linux **Ctrl+V** reads a PNG screenshot or text after an explicit paste action;
it needs `xclip` or `wl-paste`. Terminal **Ctrl+Shift+V** carries text. **Ctrl+C**
copies selected transcript text through a desktop helper or OSC52. Files never
submit until Enter; unsent attachments are not restored after TUI exit. Images
work with direct Claude/Codex, including Docker; other engines accept text files.
[Formats, limits and clipboard setup](../docs/terminal-images.md#prompt-attachments).

Typing `/` shows completion; unsupported DOXA commands stay in the draft with an error. `/model`, `/effort`, `/mode`, and `/engine` show supported choices. Live model changes require an idle session and empty queue. Claude permission-mode changes also apply during an active turn; switching to `auto` retries ordinary waiting approvals under Claude's classifier. Selecting another engine starts a new session. `/queue` previews or cancels waiting prompts. `/cd PATH` opens a tab in a verified directory without moving an existing daemon.

The session rail groups uncollected sessions by project and keeps named
collections in saved order. `/collection new` suggests a name from the active
task and known project; `[project_customers]` in `~/.doxa/config.toml` adds an
explicit customer by absolute workspace path. An explicit name remains yours.

`/collection sort urgency` orders whole groups by needs-input (`!`), reported
context at least 50% (`ctx`), verified local LORE pending proposals, then
completed-unseen (`new`). Incomplete or stale LORE summaries add no rank.
`/collection sort manual` restores saved order. Sorting is off by default,
waits for activity to settle, and pauses while the pointer or keyboard is in
the rail. Session rows within each group never move.

Project headings get stable palette hues only after the background probe
verifies a unique project root; `[project_colours]` can override an exact root.
`/collection project-label <label>` sets the selected pane's project heading
for that verified canonical root; `--clear` restores its derived name. All tabs
in the pane must resolve to the same root. DOXA stores explicit edits in
`[project_labels]` in owner config and never renames them automatically.
The active row of a multi-tab pane shows its tab count and strongest urgency,
including the number and short title of a hidden tab that needs attention.
Unknown roots and conflicting labels keep the neutral style.

## Review and permissions

Codex's permission chip offers `on-request`, `auto`, and `full-access`. `on-request` reviews protected commands, file changes, and profile requests inline. `auto` keeps the Codex sandbox; `full-access` disables it. The updated private provider also supports `on-request` → `auto` during an active turn: an outstanding provider escalation is declined so the model can retry inside the existing sandbox. Other permission and sandbox transitions require idle. The choice restores with the session. DOXA LORE reviews remain active. Built-in Codex peer list/send/history tools may run without a second DOXA approval in authoritative full-access mode; scope, exact identity, rates and fleet gates still apply. Other modes retain peer review. DeepSeek and GLM have no provider permission mode; their peer and LORE calls require individual approval. [Engine capabilities](../docs/engine-capabilities.md#review-and-accounting).

Permission answers bind to an exact pending request. Complete summaries must be read before full approval; changed or stale requests cannot inherit an answer. Secret-input requests wait for a private masked-input interface. Ctrl+Delete asks before stopping a daemon and removing its verified DOXA transcript; provider archives remain separate. Diff hunk rejection checks the current patch and staged state again before applying it.

Links, Markdown, reasoning, and individual tool calls render in the transcript. Standalone local images inside the session workspace have bounded previews. Mermaid fences can render through an explicitly configured local sandboxed renderer; source remains visible otherwise. `doxa doctor` and **Settings → Appearance → Mermaid renderer** preflight check its path policy and run a bounded PNG smoke render. A separate pinned real Mermaid CLI suite has passed; installation-specific doctor results and terminal-protocol quality have separate scopes. [Image and diagram limits](../docs/terminal-images.md) explain both paths. Ctrl+click opens HTTP(S) links. Review panes and tool output have bounded sizes. Search uses scrubbed indexed excerpts and bounded fallback scans. Unverified external entries cannot become resumable sessions.

Queued prompts use the same Markdown and user stripe as ordinary prompts, with a pending queue label. Peer messages use a separate color stripe and robot marker with sender attribution; peer-started turns keep that role when queued. Peer text remains untrusted data.

## Memory and context

The memory chip browses user and project facts; the belief browser supports evidence review, notes, accept, and reject. `/pending` opens proposals for the current project and global store. Mutations require a current snapshot and full review. DOXA calls canonical LORE and fails explicitly if its native carrier is unavailable; it keeps no separate memory store.

`/context` shows reported provider telemetry and snapshot details. `/usage` uses reported accounting and labels estimates. Missing or stale component counts and quota stay unknown. DeepSeek and GLM costs are estimates based on reported tokens and dated rates. Model facts in the picker are exact and sourced; unknown context or thinking support stays unknown. Fleet budget admission uses conservative documented price bounds and refuses an unknown priced bound. Saved API keys are redacted before transcripts or memory boundaries. [LORE](https://github.com/docwilde/LORE) · [Engine accounting](../docs/engine-capabilities.md#review-and-accounting).

The Context submenu draws a responsive usage bar from reported used/capacity tokens or percentage. Complete component counts add a colored legend; unavailable or inconsistent reports show unknown usage. **Settings → Appearance → context bar style** selects block glyphs or ASCII. LORE character limits are separate from provider token capacity.

`/codegraph file PATH`, `symbol NAME`, `imports PATH`, `calls PATH`, and `modules PATH` open a scrollable viewer for the active local session's worktree. For example, `/codegraph file rust/doxa-tui/src/main.rs` lists that file's definitions and imports. The same queries are available as `doxa codegraph ... [--root WORKTREE]` in a shell. Rust and Python support syntax-only definitions, imports, calls and module layout; conditional or runtime bindings stay unknown.

Fresh queries enumerate tracked and untracked nonignored Git files. The active local TUI automatically prepares a bounded process-local Rust/Python syntax index outside the input loop and can retain eight exact answers. Every indexed or cached answer rechecks the complete source inventory, hashes and modes; incomplete parsing remains explicit. There is no persistent watcher or dedicated agent codegraph tool; an agent can invoke the CLI through its shell tool. `/codegraph stored modules PATH` reads a reviewed LORE snapshot with freshness checks; `doxa codegraph --lore-map ...` exports a file-scoped answer for explicit owner review and storage through LORE. DOXA does not write snapshots. An explicitly provisioned Linux semantic broker has a separate narrow admission contract. [Operator contract and maintenance limits](../docs/plans/code-graph.md).

`/setup` handles provider credentials and defaults. `/plugins` and `/reload-plugins` control sanitized Claude plugin adoption for future sessions. Separately, owner-approved [native text plugins](../docs/native-plugins.md) add read-only local slash commands and owner-produced status values from private files. DOXA bounds refresh and reports failures; it does not start these status producers. `/native-plugin preflight NAME` reviews an explicit WebAssembly package. On provisioned Linux hosts, `/native-plugin run NAME` can admit an approved zero-grant package with a separately signed installed-host permit; hooks and broader plugin grants remain open. `/settings` edits validated preferences; environment overrides remain read-only.

Settings has numbered category tabs on its title bar: **1 Session · 2 Fleet · 3 Memory · 4 Appearance · 5 Keys · 6 Notifications · 7 Remote · 8 Paths · 9 About**. Click a tab or press its number when no field editor is open. Narrow panes show the selected tab and overflow arrows.

Choice fields in Settings and New Session open dropdowns with a click or Enter. Use Up/Down, the mouse wheel, Home/End or Page Up/Down to browse; click or Enter selects, and Escape cancels. Tab moves between New Session fields. Settings text edits and dropdown selections are staged until Ctrl+S saves; Escape from Settings discards unsaved changes.

New Session loads Claude and Codex model choices from their connected CLI catalogs and vendor models from the vendor catalog. An open model dropdown updates as the live list arrives; press R inside it to refresh. Loading and lookup failures appear in the form and dropdown. Router target choices come from the owner's private candidate configuration and a read-only authentication/catalog review.

To make Model Router the default, open **`/settings` → Session**, choose **Model Router** in the default-engine dropdown, and set the candidate-config path. **Ctrl+P** reviews the private config and current account catalogs without inference; **Ctrl+S** saves after a ready review. Select **routing decision model** (Jev or SubQ Snap) and enter its masked private key in the same tab. The setting applies to new sessions. Choose a fixed engine to disable the Router default. `DOXA_ENGINE` and `DOXA_ROUTER_CONFIG` environment overrides remain read-only.

Router candidates can include authenticated Claude and protected Codex models alongside DeepSeek and GLM API targets. Native CLI tool and permission reviews remain active; routing Auto is separate from permission Auto. The displayed tracked allowance covers the selected decision service and direct API estimates. CLI account charges and the combined total charge remain unknown. [Configuration and accounting](../docs/jev-router.md).

Ctrl+Delete confirms removal of the session and its DOXA transcript. Live daemons stop first; an already-absent transcript still removes the saved window entry after its project identity is checked. Successful removal suppresses late history results and daemon frames so the session cannot reappear in the list. Provider-native history remains separate.

## Fleets and peers

Choose **Start Fleet** in the action palette, or **N** in the `/fleet` browser,
to set the task, models, coordinator, isolation, budget and independent reviewers
without typing a launch command. F10 opens the complete launch review; read it,
Shift+A to arm, then Shift+Y to launch. Workers and coordinator default to
offline Docker. Internet access and native host access need separate owner
confirmations. An approved online coordinator handles workers' research
requests; offline Claude/Codex retain their fixed provider connection only.

`fleet preflight` checks capacity, spend ceiling, and socket paths before `fleet start`. Use `--worker-task INDEX:TEXT` to give each worker a frozen deliverable and optional `--worker-path INDEX:RELATIVE_PREFIX` to narrow its file scope. `fleet runs|status|attach|stop|resume` operate on owned manifests and verified slots. `fleet debrief RUN` reads a finished run's host journal and reports assignment outcomes, handoff cycles, signed test receipts, and retained review events without peer text or model calls. Starting or resuming in the TUI requires the complete plan review. Budgeted resume needs complete persisted accounting. TUI-spawned fleets group live workers beneath their acting supervisor in the session sidebar; click a live worker to inspect its session, or the group's arrow to collapse/expand. Runs without an acting supervisor use a Fleet heading. **M** in a selected run opens retained **Messages & alignment** scores, admission outcomes, reasons and evidence IDs. [Fleet access, isolation changes and review](../docs/fleet-access.md).

Use `--worker-after INDEX:PREDECESSOR` to hold a worker until its predecessor finishes and the operator reviews the host-recorded checkpoint and typed handoff. Dependency plans require an acting coordinator, independent review, and Docker isolation. `fleet dependency-evidence`, `dependency-review`, then `dependency-release` expose host evidence and test-receipt status. An optional frozen offline test recipe collects receipts; releasing the dependency still requires the human review. [Dependency gate](../docs/fleet-supervision.md).

Select an independent supervisor with `--alignment-supervisor PROVIDER:MODEL`. Choose a separate fast message judge with `--message-judge llm:PROVIDER:MODEL` or `jev:MODEL` / `subq:subq-snap-preview`, and choose `--message-review off|shadow|enforce`. `/settings` → **Fleet** stores defaults. The acting `--supervisor` is a worker; the independent reviewer reads evidence and cannot grant its own approvals. Host gates bind peer traffic to the approved charter and assignments. [Supervisor contract](../docs/fleet-supervision.md).

`doxa fleet evaluate-messages` scores a private, consented labeled verdict file
for recall, false pauses and latency by development and holdout split. It does
not call a judge or change enforcement. No real-message corpus ships. An opt-in
rootless Docker fixture checks offline test receipt transport; a full live fleet
run remains to be verified.

`/peers` or Ctrl+M opens the peer map; `/msg PEER TEXT` sends a scrubbed same-project message. The optional private mesh shows fleet relationships in a browser. Remote peer routes use authenticated private sockets and verified rosters. [Transport contract](../docs/native-peernet.md).

## Remote access

Remote access is off by default. For a private browser view on the session host, set `DOXA_REMOTE_ENABLED=1` and `DOXA_REMOTE_ALLOWED_LOGINS=you@example.com`, run `doxa remote serve`, and point Tailscale Serve at the printed owner-private socket. The view reads recent turns, follows events, sends prompts, and answers pending requests.

For cross-machine control, run `doxa-hub` behind Tailscale Serve on a private server, then `doxa remote connect HUB_URL HOST_ID` on the session host. The hub browser, `doxa remote list/send/answer`, and `doxa remote tui HUB_URL` can control registered sessions. Remote TUI tabs use the normal keys; Ctrl+R opens history and PageUp fetches older records. `doxa remote tui HUB_URL --save-layout` persists remote-only tabs for the same owner and session incarnation. `/remote-connect HUB_URL HOST_ID` shares sessions while the local window stays open; `/remote-control HUB_URL` adds remote tabs marked `◎` beside local ones. Add `--save-layout` to that slash command to opt in to mixed-pane restore after fresh local and authenticated remote rosters. The hub is volatile, so inspect an uncertain command before retrying it. [Remote hub design](../docs/plans/remote-hub.md).

For native end-to-end encryption, generate a shared key with `doxa remote keygen /absolute/private/remote.key`. Keep it owner-only and set `DOXA_REMOTE_E2EE_KEY_FILE` to its path on both host and client. Transcript and control content stays opaque to the hub; session presence and event metadata remain visible. Encrypted sessions work in the native TUI, CLI, and separately installed [Chrome extension](../browser-extension/README.md), but not the hub-served browser. The hub can also deliver generic Web Push completion and input alerts when configured with a private VAPID key. The [Android source client](../android-client/README.md) controls private hub sessions and offers generic local alerts while connected. Its opt-in FCM background alert path and debug APK open in an offline emulator; provisioned device, Firebase and two-host tailnet QA remain open.

## Verification and limits

Run `./task test` for the Rust suite. CI checks portable crates, daemon behavior, and provider fixtures with disposable stores. Linux has bounded live checks for all four engines. macOS builds and transport tests run in CI, but authenticated provider sessions still need live verification. Protected Codex needs Linux process ownership and private compaction hooks; Windows is unsupported. [Platform record](../docs/platform-verification.md) · [Latest provider record](../docs/live-provider-verification-2026-10-04.md).

The protected Codex build verifies a pinned synchronous `PreCompact` hook before starting a thread. Missing or failed LORE review blocks compaction; stock app servers cannot run protected turns. The private Linux launcher owns and reaps provider descendants. [Codex engine contract](doxa-engines/README.md) records exact build and verification details. [Current gallery](../docs/rust-gallery.md) shows deterministic Ratatui frames and isolated browser samples.

Python 1.x is archived at [v1.19.0](https://github.com/docwilde/doxa/tree/v1.19.0); its [manual](../docs/manual.md) documents that version.

### Search services and Docker setup

`/settings` → `0 MCP` contains the four masked search API-key editors, explicit
server config and MCP switch. Ctrl+P probes connection/catalog only; the
[client guide](../docs/mcp-client.md) and [API worker guide](../docs/vendor-mcp.md)
explain tool review, private configuration and external cost reporting.
`/setup docker` opens rootless Engine and reviewed-image requirements directly;
`doxa setup docker` offers the same terminal configuration.
