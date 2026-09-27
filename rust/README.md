# DOXA Rust 2.0

Rust is the main DOXA frontend. The installer exposes `doxa`; the compiled
frontend is `doxa-rs`. This alpha uses native Codex and vendor hosts plus a
Python Claude SDK sidecar. Python also provides the bridge to external LORE,
which remains the authority for memory, reviews, indexing, and secret scrubbing.
The [parity tracker](../docs/rust-1.19-parity.md) records stable release gates.

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
installs committed HEAD, including a locked Python sidecar environment.
Working-tree changes must be committed first. Set `DOXA_LORE_PYTHON` or pass
`--lore-python` / `--claude-python` to choose an interpreter for source builds.

The POSIX installer builds `main` by default:

```sh
curl -fsSL https://raw.githubusercontent.com/docwilde/doxa/main/scripts/install.sh | sh
```

Append a tag or SHA after `sh -s --` to pin a ref. Installation requires Git,
Cargo, Python 3.11+, and `uv`. Binaries go to `~/.local/bin`, overridable with
`DOXA_RUST_BIN_DIR`. Linux application-menu integration can be disabled with
`DOXA_NO_LAUNCHER=1`. `doxa update` updates an installed launcher; source builds
use `./task install`. `doxa help` lists CLI forms and options.

## Sessions and worktrees

Bare `doxa` restores this project's saved tabs or starts the configured engine
(default Claude). Safe saved conversations resume without a prompt; others remain
read only with a reason. `restore_tabs` and `resume_restored` control this behavior. `new` always
starts a session; `attach`, `stop`, and `list` manage live sessions. Select
`--engine codex|claude|deepseek|glm`, `--model`, and supported `--effort` values.
Claude needs the Claude Agent SDK; vendors use `DEEPSEEK_API_KEY` or
`ZAI_API_KEY`. Doctor checks dependencies without printing credentials.

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

Ctrl+P opens a queryable action palette generated from the command registry,
open tabs, saved fleet views, and session actions. Typing `/` shows completion
above the prompt. Unsupported DOXA forms remain in the draft with an error;
unknown provider/plugin commands follow the normal engine prompt path.
Ctrl+R and `/search TEXT` use the prompt as the query field, with results above
it. Indexed excerpts and bounded fallback scans are scrubbed; external entries
without verified readable session files do not become resumable sessions.

Ctrl+W detaches the current tab; Ctrl+Q detaches the frontend. Inline questions
support selectable answers, free text, and Other drafts. Secret-input requests
are refused until private masked input exists. Permission approval requiring
full review is unavailable until the complete summary has been read. Reconnect
snapshots restore exact pending requests; stale answers cannot apply to changed
requests. There is no blanket Codex permission approval.

Markdown, HTTP(S) Ctrl+click links, expandable tool results and reasoning,
processing indicators, and per-session chip menus use bounded data. `/queue`
previews and cancels waiting prompts by stable ID. `/model [name]`,
`/effort [name]`, `/mode [name]`, and `/engine [name]` use supported capability
and catalog choices. Live changes require an idle session and empty queue.
Claude effort stays pending until the SDK verifies it; unknown state is not
presented as applied. Engine selection starts a new session.

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
review and atomic exact-snapshot actions; older LORE without those APIs stays
read only. DOXA does not implement a separate memory store.

`/context` shows available official provider telemetry and reported snapshot
metadata. Claude can report categories, memory files, tools, agents, and local
injection character counts. Codex shows verified model/window and input/cached/
output usage; unavailable component counts remain unknown. Estimates are marked.
`/usage` uses reported accounting; subscription quota is shown only from a
verified source. Missing or stale provider data is not replaced with invented
billing or component totals.

`setup` and `/setup` guide provider authentication, LORE store selection, and
model/effort defaults. `auth login claude|codex`, `auth logout claude|codex`, and
corresponding slash commands invoke the selected provider CLI. Codex login
supports `--device-auth`. Public sign-in URLs/codes are allowlisted; credentials
and raw authentication output do not enter transcripts. Closing authentication
cancels and reaps its worker. `auth status` checks CLI exit status.
`plugins [refresh|adopt on|off]` and `/plugins` / `/reload-plugins` discover and
control sanitized Claude plugin adoption for future sessions. Native settings
and `/settings` edit the full categorized preference catalog; environment
shadows remain read only. Settings are validated against the categorized catalog before they are saved.

## Fleets and peers

Native `fleet start` accepts pool, prompt/file, worker count, supervisor, budget,
approval policy, and quiescence options. `fleet preflight` validates capacity,
spend ceiling, and socket paths. `fleet runs|status|attach|stop|resume` use owned
manifests and verified slot sockets. `fleet start-python` is the explicit legacy
harness path for options outside the native parser.

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
output stay private. Ctrl+C cancels a controller; Ctrl+Q waits for teardown
before exiting. Controller completion refreshes actual manifest state.

`/peers` and Ctrl+M open the native peer map. `/msg PEER TEXT` sends
same-project scrubbed messages. Native inbound turns use bounded queues;
supervisor peer tools are capability gated. CLI `mesh serve` and `fleet mesh RUN`
serve the private graph with an owned, stoppable Python browser-mesh child.
TUI `/mesh [RUN|stop]` opens or stops a browser graph owned by this window.
`/fleet status`, `stop`, `detach`, `attach INDEX` and `mesh` use the current run.
Old manifests lacking a verified private ledger are refused for browser serving.
Remote routing is not claimed as a Python 1.19 parity requirement.

## Codex compaction protection

Protected app-server startup requires the verified **Codex 0.156.1** hook
contract and checks DOXA's trusted synchronous `PreCompact` hook hash. An
unsupported build or missing trusted hook refuses startup. The hook binds the
provider thread and owned rollout, prepares a scrubbed private snapshot, and
waits for LORE review. Manual `/compact` uses the official compaction request;
Claude compaction also waits for LORE review. Vendor compaction is unavailable.

Codex 0.156.1 can continue compaction when the OS cannot spawn a hook, or the
hook times out or returns invalid output. DOXA stops a protected session after
observed failure, but cannot guarantee that the provider has not already
compacted. Normal reviewer failure returns a valid blocking decision before
the deadline. This provider infrastructure limitation remains a stable gate.
See [engine contracts](doxa-engines/README.md) for transport and review details.

## Verification and gallery

The alpha.31 release verifies the full Rust workspace and the installed Python
SDK/LORE seams using disposable stores and local provider fixtures. Coverage
includes layout, review gates, restoration, current-session controls, cancellation,
interactive fleets, device login, private ledgers and malformed manifests.
Run `./task test` for the current full suite. Alpha tests do not establish live
provider compatibility beyond the explicitly verified contracts.

The [gallery](../docs/rust-gallery.md) renders production Rust layouts from
labelled deterministic fixtures. Terminal image support is explicitly excluded
by user preference. Stable release still requires the tracker gates and final
performance/regression verification.
