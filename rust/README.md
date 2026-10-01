# DOXA Rust 2.0

Rust is the main DOXA frontend. The installer exposes `doxa`; the compiled
frontend is `doxa-rs`. Claude uses its native CLI control protocol, Codex uses
its app server, and API vendors use Rust HTTP clients. Canonical LORE 0.62.11 is an
integrated Rust library for memory, reviews, indexing, and secret scrubbing;
`lore-rs` also provides detached review and standalone plugin commands.
The installed runtime requires no Python interpreter.
New session titles use `model@branch/repo` in Git or `model@short-path`
elsewhere; a second matching session gets `-2`. Explicit renames stay pinned.
The [parity tracker](../docs/rust-1.19-parity.md) records stable release gates.
The [2026-09-29 verification record](../docs/live-provider-verification-2026-09-29.md)
reports the alpha.46 candidate's observed provider behavior and remaining checks.
The [current gallery](../docs/rust-gallery.md) records live terminal captures;
it is visual evidence, not a new provider compatibility test.
Linux is live verified. macOS has native build and transport CI, with Claude,
DeepSeek and GLM as the supported engine path; authenticated macOS provider
sessions still need live verification. Protected Codex remains Linux-only
because its process-owner contract has no macOS equivalent. Windows is unsupported.
On macOS, connected client sockets attest the daemon's effective UID with
`getpeereid`; Linux additionally attests its PID for destructive requests.

## Build and install

Run from the repository root:

```sh
cargo build --locked
./task doctor
./task new
./task test
```

`./task` builds incrementally in `target/rust-task`; ordinary Cargo uses
`target`. `./task build --release` selects a release build. `./task install`
installs committed HEAD, including the native LORE carrier. Working-tree
changes must be committed first. Pass `--claude-bin` or `--codex-bin` to
select a provider CLI executable. `DOXA_LORE_RS` selects a detached native
carrier; the installer places it beside the frontend and daemon.
Python sources and dependencies are development interoperability references.

The POSIX installer builds `main` by default:

```sh
curl -fsSL https://raw.githubusercontent.com/docwilde/doxa/main/scripts/install.sh | sh
```

Append a tag or SHA after `sh -s --` to pin a ref. Installation requires Git,
Cargo and Rust. Binaries go to `~/.local/bin`, overridable with
`DOXA_RUST_BIN_DIR`. The installer adds a Linux application-menu entry or an
executable `~/Applications/DOXA.command` shortcut on macOS. Disable either with
`DOXA_NO_LAUNCHER=1`. On macOS it skips the protected Codex installer; use
Claude or an API engine. `doxa update` updates an installed launcher; source builds
use `./task install`. `doxa help` lists CLI forms and options.
Verified Codex updates publish a fresh immutable provider artifact and select it
for new launches. Running providers retain their original files. Corrupt installed
artifacts or receipts are refused rather than silently replaced.

## Sessions and worktrees

Bare `doxa` restores this project's saved tabs or starts the configured engine
(default Claude). Safe saved conversations resume without a prompt; others remain
read only with a reason. Offline restoration retains saved split layout and
conversations. `restore_tabs` and `resume_restored` control this behavior. `new` always
starts a session; `attach`, `stop`, and `list` manage live sessions. Select
`--engine codex|claude|deepseek|glm`, `--model`, and supported `--effort` values.
Claude needs the Claude Code CLI. Set DeepSeek or z.ai API keys in `/setup`,
or inherit `DEEPSEEK_API_KEY` / `ZAI_API_KEY` from the launching environment.
Doctor checks dependencies without printing credentials.

Git sessions receive managed linked worktrees unless `DOXA_WORKTREE=0` or
`worktree_per_session=false`. `new --branch NAME` selects an existing base.
`/branch [name]` changes an idle, clean verified worktree with no unique commits.
Finalization removes only verified clean worktrees with no commits ahead of
the pinned base. Dirty, switched, unpinned, or uncertain worktrees remain.
`worktrees list` previews orphans; `worktrees cleanup FULL_ID --confirm`
rechecks ownership, Git state, registry, and shared lock before deletion.
Legacy Python sidecars without the pinned ownership contract remain survey only.
Saved-session recovery can recreate a deleted managed checkout from its retained
branch only when all metadata agrees; deleted uncommitted files are unrecoverable.

`/resume [query]` restores verified Claude, Codex, or vendor state. `/attach`
selects live sessions. History restoration is bounded and marks omissions;
historical tool or reasoning data that was never stored cannot be reconstructed.

## Window, prompt, and review

Nested horizontal and vertical splits support up to 16 pane groups and 256
session tabs. `/split`, `/vsplit`, `/pane [number]`, and `/movepane [number]`
operate on numbered groups. Moving the source's final tab is refused. Divider
mouse dragging preserves each subtree's minimum size. Tabsets save layout,
labels, collections, and per-session drafts; insufficient space temporarily
collapses the display without discarding its saved topology.

Ctrl+T opens the new-tab engine picker; Alt+T opens tool calls.
Ctrl+P opens a queryable action palette generated from the command registry,
open tabs, saved fleet views, and session actions. Typing `/` shows completion
above the prompt. Unsupported DOXA forms remain in the draft with an error;
unknown provider/plugin commands follow the normal engine prompt path.
Ctrl+R and `/search TEXT` use the prompt as the query field, with results above
it. Indexed excerpts and bounded fallback scans are scrubbed; external entries
without verified readable session files do not become resumable sessions.

Window shortcuts can be changed in `/settings` under Keys, or with
`doxa settings set key_new_tab Alt+N`. Changes saved in the TUI apply immediately;
CLI changes apply on the next launch. Use `none` to unbind a shortcut. Duplicate
or malformed chords are rejected without replacing the saved config. The prompt,
approval and menu editing keys remain local to those controls.

Ctrl+X closes the active tab and leaves its daemon running; Ctrl+W is an alias.
Ctrl+Q exits the frontend and leaves all running sessions detached. Ctrl+Left/Right
switches tabs in the current pane; Shift+Left/Right switches between pane prompts.
Dead detached sessions leave the session rail after live-registry verification;
their saved transcripts remain available through session history. Inline questions
support selectable answers, free text, and Other drafts. Secret-input requests
are refused until private masked input exists. Permission approval requiring
full review is unavailable until the complete summary has been read. Reconnect
snapshots restore exact pending requests; stale answers cannot apply to changed
requests. There is no blanket Codex permission approval.

Codex uses a fixed `on-request` policy in DOXA. Command, file-change, and
permission-profile requests reach the inline review; an approved profile is
limited to the exact request and current turn. DOXA cannot switch Codex's
policy during a session. “Always approve this tool” applies only to canonical
DOXA peer and LORE tools, never Codex commands, file changes, or permission
profiles. DeepSeek and GLM have no provider permission mode;
their peer and LORE tool calls require individual DOXA approval. The optional
`DOXA_VENDOR_TOOLS=workspace-read` tool is read-only and enabled separately.

In the belief browser, select a row and press A or R, or click its Accept or
Reject button. DOXA fetches the exact current belief before applying the
action. Enter opens detailed review and note editing. Press `/` or click the
prompt field to filter the list; Enter returns from filtering to row actions.

Markdown, HTTP(S) Ctrl+click links, expandable tool results and reasoning,
processing indicators, and per-session chip menus use bounded data. `/queue`
previews and cancels waiting prompts by stable ID. `/model [name]`,
`/effort [name]`, `/mode [name]`, and `/engine [name]` use supported capability
and catalog choices. Live changes require an idle session and empty queue.
Claude model and effort changes are verified with the live CLI
`get_settings` response; unknown state is not presented as applied. Engine selection starts a new session.

`/diff` or F2 opens a bounded worktree diff; F4 keeps it beside the session.
File/hunk navigation and exact tracked-text-hunk rejection are supported.
Rejection rechecks the patch and staged state, queues until idle, and submits
feedback through the session. Changed patches and whole-file metadata hunks
are refused. `/cd PATH` starts a tab in a verified directory; it does not move
the existing daemon. `/clear` requires an idle session and durable tabset swap.

## Memory, context, and operations

The curated-memory chip browses project/user scopes with their own capacity
percentages. Add, edit, and remove use full before/after review and exact snapshot
checks before invoking canonical LORE mutations. Pending, conflict, trust, and
detached-store gates remain authoritative. Beliefs and `/pending` use complete
review and atomic exact-snapshot actions through the integrated canonical core.
A missing native carrier is an explicit availability failure. DOXA does not
implement a separate memory store. Memory browsing delays entry previews;
review view titles distinguish **Pending** from **Clustered** proposals.
Mouse-wheel input follows the hovered pane or control.

`/context` shows available official provider telemetry and reported snapshot
metadata. Claude can report categories, memory files, tools, agents, and local
injection character counts. Codex shows verified model/window and input/cached/
output usage; unavailable component counts remain unknown. Estimates are marked.
`/usage` uses reported accounting; subscription quota is shown only from a
verified source. Missing or stale provider data is not replaced with invented
billing or component totals. Reported quota consumption becomes yellow above
66% and red above 90%; missing quota stays unknown.

`setup` and `/setup` guide provider authentication, LORE store selection, and
model/effort defaults. `auth login claude|codex`, `auth logout claude|codex`, and
corresponding slash commands invoke the selected provider CLI. Codex login
supports `--device-auth`. Public sign-in URLs/codes are allowlisted; credentials
and raw authentication output do not enter transcripts. Closing authentication
cancels and reaps its worker. `auth status` checks CLI exit status.

In `/setup`, select the DeepSeek or z.ai API key edit row, type or paste into
the masked field, and choose Save or Cancel. Setup reports only the source
(saved, environment, or missing); it never reveals the key. Remove deletes
the saved override and falls back to the inherited environment key.
Changes refresh the vendor model list and DeepSeek balance and apply to the
next request in existing sessions.

Saved keys are plaintext in the owner-only `~/.doxa/credentials.json` file
(mode `0600`), under `DOXA_HOME` when configured. They override environment
keys. DOXA does not load credentials from project files, `.env`, or LORE
memory. Key input stays separate from prompt drafts and conversation history;
known keys are redacted before crossing vendor memory or transcript boundaries.

`plugins [refresh|adopt on|off]` and `/plugins` / `/reload-plugins` discover and
control sanitized Claude plugin adoption for future sessions. Native settings
and `/settings` edit the full categorized preference catalog; environment
shadows remain read only. Settings are validated against the categorized catalog before they are saved.

## Fleets and peers

Native `fleet start` accepts pool, prompt/file, worker count, supervisor, budget,
approval policy, and quiescence options. `fleet preflight` validates capacity,
spend ceiling, and socket paths. `fleet runs|status|attach|stop|resume` use owned
manifests and verified slot sockets. All production fleet orchestration is
native; unsupported forms are refused with their original arguments retained.

The native controller owns startup barriers, supervision, approval handling,
budget checks, cancellation, and teardown. `fleet review` / `fleet answer`
bind an answer to the exact reviewed request token. Budgeted resume requires
complete persisted accounting; incomplete or unsupported pricing fails closed.
Vendor cost is an estimate from a dated sheet, not a live balance. Reported
Claude and Codex usage must match the accounting model and basis.

`/fleet` opens runs/status above the prompt and saves verified real run views
in the action palette. `/fleet start OPTIONS` and `/fleet resume RUN` require
complete plan review and explicit arming before spawning an owned controller.
Arguments are passed directly, without shell evaluation. Task text and child
output stay private. Ctrl+C cancels a controller; Ctrl+Q detaches it
before exiting. Controller completion refreshes actual manifest state.

`/peers` and Ctrl+M open the native peer map. `/msg PEER TEXT` sends
same-project scrubbed messages. Native inbound turns use bounded queues;
supervisor peer tools are capability gated. CLI `mesh serve` and `fleet mesh RUN`
serve the private graph through Hyper using compiled page assets and a
private URL token.
TUI `/mesh [RUN|stop]` opens or stops a browser graph owned by this window.
`/fleet status`, `stop`, `detach`, `attach INDEX` and `mesh` use the current run.
Old manifests lacking a verified private ledger are refused for browser serving.
Remote routing uses a private Unix socket and kernel-attested proxy identity.
Configured remote endpoints must have reciprocal verified rosters. See
[the remote transport contract](../docs/native-peernet.md).

## Codex compaction protection

Protected app-server startup requires the verified **Codex 0.156.1** hook
contract and checks DOXA's trusted synchronous `PreCompact` hook hash. An
unsupported build or missing trusted hook refuses startup. The hook binds the
provider thread and owned rollout, prepares a scrubbed private snapshot, and
waits for the native LORE review worker. Before the official manual `/compact`
request, DOXA independently reviews the bound source and rechecks its identity
and digest. Failed, disabled or missing review sends no compaction request.
Startup verifies that Codex's unhooked token-budget reset feature is disabled.
Claude compaction also waits for LORE review. Vendor `/compact` preserves the
full durable conversation and writes a separate, reviewed summary checkpoint;
it validates the original prefix before using that summary on resume.

Alpha.41 requires DOXA's private Codex 0.156.1 app server and its matching
`codex-code-mode-host`. Model-required Code Mode uses that native host; the
launcher verifies both private artifacts before dispatch. The app server flushes the
owned rollout before review and blocks local and remote compaction before
inference or history replacement unless exactly one trusted synchronous hook
explicitly allows continuation. Missing, failed, timed-out, malformed, asynchronous
or duplicate review refuses compaction. Stock app servers refuse protected turns.
Legacy exec sessions stay read only until an explicit same-thread migration with
`DOXA_CODEX_MIGRATE_APPSERVER=1`. The official Codex CLI is retained for login/help.

Protected DOXA sessions use a private Linux provider supervisor. Its control
handshake completes before the provider starts; control closure on cancellation,
shutdown or daemon death stops and reaps provider descendants even across new
process groups or sessions. The daemon does not adopt unrelated jobs.

Ten tests against the compiled provider cover nine refusal cases and a successful
allow control with a loopback model. Real-account successful LORE review and
large-context compaction require the reviewer's Claude authentication. Earlier
manual and lowered-threshold automatic compaction passed native review. An
[alpha.52 authenticated run](../docs/live-default-window-compaction-2026-09-30.md)
also passed one default-window automatic compaction and exact recall after a
daemon restart; the earlier September 29 run remains incomplete historical evidence.
See [engine contracts](doxa-engines/README.md) for transport and review details.

## Verification and gallery

Rust CI verifies the workspace and a native installation using disposable
stores and controlled provider fixtures. Python is used only by development
compatibility tests. Coverage
includes layout, review gates, restoration, current-session controls, cancellation,
interactive fleets, device login, private ledgers and malformed manifests.
Run `./task test` for the current full suite. Alpha tests do not establish live
provider compatibility beyond the explicitly verified contracts.

The [gallery](../docs/rust-gallery.md) captures the real application in a VTE
terminal with an authenticated provider and isolated example repository.
Development fixtures are kept separate. Terminal image probes are disabled.
