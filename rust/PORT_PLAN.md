# DOXA 2.0: full Rust port plan

Status: planning document for `rust/2.0`; PR status snapshot at
2026-09-24 15:52 UTC.
DOXA 1.x remains the behavior reference. **LORE remains an external
integration**: DOXA owns the adapter and its failure behavior, but does not
reimplement LORE's store, derivation, index, scrubber, or sync engine.

## Starting point and PR accounting

The current base tree has one crate, `rust/doxa-tui`, whose `doxa-rs` binary can
discover live Python daemon sockets (`src/discovery.rs`), attach to protocol
v1 (`src/transport.rs`), send prompts (`src/bridge.rs`), and render text and
some structured events (`src/ui.rs`, `src/markdown.rs`). It is a client, not
a Rust implementation of DOXA's runtime. Its `main.rs` exposes `--list`,
`--session`, `--socket`, `--demo`, `--version`; it cannot create or resume a
session. `bridge.rs` has one attached socket per process and no durable
reconnect cursor. `ui.rs` stores two pane groups and a bounded in-memory
transcript; neither is a substitute for DOXA's persisted state.

| PR | State at plan time | Credit toward parity; work still required |
| --- | --- | --- |
| #98 | merged | Registry discovery and session selection in Rust; scope-aware startup, spawn, and resume remain. |
| #99 | merged | Text rows for structured daemon events; interactive cards, full event state, and persisted transcript remain. |
| #100 | open | Persisted transcript restore on attach; integrate only after merge and verify with both engine formats. |
| #101 | merged | Python 1.19.0 source is now on this branch; re-audit parity against it during implementation. |
| #102 | merged | Markdown table alignment and safe link targets; integrated terminal checks still gate parity. |
| #103 | open | Interactive `needs_input` answer UI; verify multi-client resolution and detached replay after merge. |
| #104 | open | Mouse drag for dividers and rail width; save/restore geometry still required. |

Treat an open PR as a dependency, not completed implementation. Once each
lands, remove only its verified gap. None of #98–#104 ports the Python daemon,
engine hosts, peer fabric, session storage, or LORE boundary to Rust.

## Required behavior and source map

| Surface | Python source of truth | Rust deliverable and parity check |
| --- | --- | --- |
| CLI/startup | `doxa/cli.py`, `launcher.py`, `config.py`, `setup.py`, `doctor.py`, `cli_isolation.py` | `doxa` commands `new`, `attach`, `stop`, `doctor`, `launcher`; default scope restore or spawn; `--engine`, `--model`, `--linger`, `--branch`, `--checkout`; config precedence and atomic writes; first-run and diagnostics. Replace development-only `doxa-rs` naming only at release cutover. |
| Daemon/lifecycle | `doxa/daemon.py`, `client.py`, `events.py`, `promptqueue.py`, `notify.py` | Native process per session, attach/detach, bounded replay ring, queued prompts, linger/last-client finalize, signal cleanup, notification ownership, typed RPC errors. Keep v1 wire compatibility during migration. |
| Engine abstraction | `doxa/engines.py`, `engine.py`, `codex.py`, `vendors.py`, `providers.py`, `claude_catalog.py`, `budget.py`, `prices.py` | Capability-driven `Engine` trait and adapters for Claude, Codex CLI, and configured chat APIs; identical event semantics, usage/cost provenance, budget refusals, model and permission changes. Claude's bridge remains an explicit decision gate. |
| Session persistence | `doxa/engine.py` (`_append_record`, `finalize`), `codex.py`, `vendors.py`, `transcript.py`, `history.py`, `naming.py`, `tabsets.py` | Keep existing JSONL session records and Codex thread identity, search/resume/read-only history, stable titles, atomic tabset records and old layout formats. Ring replay is only recent live events. |
| Peer coordination | `doxa/peers.py`, `peerdelivery.py`, `peerledger.py`, `peernet.py`, `meshgraph.py`, `fleet.py`, `fleetsession.py`, `session_ops.py`, `worktrees.py` | Same-user registry/heartbeat, scoped discovery, peer sockets and sidecar delivery, append-only ledger/rate limits, remote peer bridge, mesh view, fleet barrier/approval desk, spawn reservations, and safe worktree cleanup. |
| TUI | `doxa/app.py`, `appwindow/{actions,panetree,restore,sidebar,tabs}.py`, `session/{pane,runtime,commands,chips}.py`, `ui/*.py`, `layout.py`, `tabsets.py` | Multiple attached sessions, arbitrary group/split tree, tab and rail operations, focus/keyboard/mouse, draft/queue, status chips, tool/permission cards, transcript/diff/history views, pickers and commands, restart restore. |
| Auth/security | `doxa/auth.py`, `cli_isolation.py`, `gate.py`, `remote_policy.py`, `remote_web.py`, `mcpserver.py`, `operators.py`, `images.py` | Credential isolation and auth subprocess recovery; tool gate/two-strikes; explicit permission answer path; local/remote identity and policy; bounded untrusted frames, image and terminal sanitization; MCP tool surface. |
| LORE interface | `doxa/_lore_bootstrap.py`, `lore_sync.py`, direct `lore_core` sites in `engine.py`, `codex.py`, `vendors.py`, `operators.py`, `peers.py`, `peerledger.py` | External LORE adapter for scrub, context snapshot/refresh, consult/search, beliefs/evidence/pending, review/index, sync state, and tool calls. DOXA transcripts remain available when LORE is disabled or unavailable. |

The Python UI has behavior outside the daemon RPCs: `/resume`, `/diff`,
`/settings`, `/fleet`, tab movement, and worktree actions are local app or
CLI operations (`doxa/session/commands.py`, `doxa/appwindow/tabs.py`). A
Rust UI cannot reach feature parity by adding daemon `call` buttons alone.

## Workspace boundaries

Turn `rust/` into one workspace. Keep `doxa-tui` as the executable crate
during development; extract code in dependency order, not by copying
Python file boundaries verbatim. The native socket foundation is being
implemented as the `rust/doxa-runtime` **library**. `doxa-daemon` below is a
later production **binary** that supplies real engines and process lifecycle
to that library; do not rename the foundation or count it as the full host.

| Crate | Owns | May depend on |
| --- | --- | --- |
| `doxa-protocol` | Event/RPC types and compatibility fixtures, extracted from `doxa-runtime` when two consumers need stable shared types | `serde` only |
| `doxa-state` | Config, session identity, transcript/history/tabset parsers, atomic files, migration readers | protocol; no engine or terminal |
| `doxa-security` | Secret-scrub adapter contract, peer/remote policy, path and socket checks, prompt/tool trust labels | protocol, state |
| `doxa-lore` | Client for external LORE service/sidecar, capability negotiation, timeout/error mapping | protocol, security |
| `doxa-peers` | Registry, peer host/delivery, ledger, control socket, rate limiter | state, security, protocol |
| `doxa-engines` | `Engine` trait, event normalization, Claude/Codex/chat-provider adapters, tool gate and MCP projection | protocol, state, security, lore, peers |
| `doxa-runtime` | Reusable v1 Unix socket host, frame bounds, replay ring, prompt queue, `Host` trait; initial implementation has no engine, registry, persistence, LORE, signals, or linger | protocol when extracted; no terminal |
| `doxa-daemon` | Production binary: instantiate engine host, full RPC routing, registry, attach lifecycle, linger/finalize/signals, notifications | runtime, engines, peers, state |
| `doxa-tui` | Terminal model/rendering/input, CLI entry point, client transport and startup UX | protocol, state, security; daemon through wire only |

Keep `doxa-tui` independently attachable to a Python v1 daemon until the
native daemon passes compatibility gates. Put remote web, mesh, fleet, and
launcher binaries behind the owning Rust crates, with separate feature or
binary targets rather than dependencies in the terminal hot path. Avoid a
Rust↔Python in-process binding for the core runtime: that would keep SDK
import/startup and Python packaging as a mandatory 2.0 dependency.

## Wire and storage contracts to freeze first

`doxa/daemon.py` defines newline-delimited JSON frames, each at most
`peers.MAX_FRAME_BYTES` = 64 KiB. Server sends `hello` (`proto: 1`,
`session_id`, `cwd`, `engine`, `model`, `next_seq`), `event` (`seq`,
`turn`, `event: {type,data}`), and `reply` (`id`, `ok`, payload/error).
Client sends `attach` with nullable cursor, `prompt` with id/text, and
`call` with id/method/params. `rust/doxa-tui/src/transport.rs` already
validates these envelopes. Maintain `seq >= cursor` replay semantics,
per-client slow-writer eviction, and explicit error responses. A protocol
v2 needs negotiation and a dual-version window, not an in-place shape
change.

RPC methods to account for in typed dispatch: `status`, `peers`, `msg`,
`stop`, `set_model`, `set_permission_mode`, `branch`, `answer_needs_input`,
`beliefs`, `belief_evidence`, `belief_action_state`, `belief_outcome`,
`retract_belief`, `lore_write`, `approve_pending`, `reject_pending`,
`pending`, `context`, `queue`, and `cancel_queued` (`doxa/daemon.py`;
`doxa/client.py`). Event consumers must handle turn events plus
`peer_joined`, `peer_left`, `peer_message`, `tool_disabled`, `needs_input`,
`needs_input_resolved`, `turn_refused`, `remote_driver_changed`, and queue
events (`doxa/events.py`, `doxa/session/runtime.py`). Preserve capability
errors for memory RPCs on engines without `lore_pickers`, rather than
panicking (`doxa/daemon.py:MEMORY_RPC_MEMBERS`).

Storage contracts: `$DOXA_HOME/config.toml` (`doxa/config.py`), scoped
`$DOXA_HOME/tabsets/*.json` with flat `tabs` plus optional `layout.trees`
and `layout.groups` (`doxa/tabsets.py`), session JSONL beside LORE project
transcripts (`doxa/engine.py`, `doxa/transcript.py`), Codex thread mapping
(`doxa/codex.py`), peer registry in the runtime directory and 0600
sockets (`doxa/peers.py`), peer message ledger JSONL
(`doxa/peerledger.py`), and worktree `.meta/*.json` (`doxa/worktrees.py`).
Document golden fixtures from real 1.x records before writing any Rust
writer. Old readers must degrade saved split layouts to flat tabs; malformed
records must not make sessions disappear.

## Migration sequence and acceptance gates

1. **Contract baseline.** Capture sanitized fixtures for every frame/RPC,
   engine event, transcript variant, registry entry, tabset era, and ledger
   row. Write Rust decoder/encoder tests against Python-generated fixtures
   and Python client tests against a small Rust fixture server. Include
   the Python 1.19.0 tree merged in #101. Gate: every v1 fixture round-trips
   within the 64 KiB bound; unknown optional fields are tolerated.
2. **State and security.** Stabilize `doxa-runtime`'s v1 types, then extract
   `doxa-protocol` if sharing them prevents duplication; add `doxa-state`; port
   config precedence, atomic 0600 writes, transcript/history readers,
   tabsets and worktree metadata. Add `doxa-security` with owner/mode/path
   checks and untrusted text handling. Gate: Python→Rust→Python golden-file
   migration, concurrent writer tests, and denial cases for symlinks,
   foreign owners, malformed JSON and oversized inputs.
3. **Native daemon shell.** Complete the `doxa-runtime` socket foundation
   around a fake `Host`, then build `doxa-daemon` with registry presence,
   full RPC dispatch, session lifecycle and a fake engine. The foundation
   already targets v1 hello/attach/event/reply, a 512-event ring, eight-item
   FIFO, 64 KiB frames, per-client bounded queues and private socket paths;
   it does **not** yet supply the production behaviors in this step.
   Gate: Python `EngineClient` and Rust TUI both attach to the Rust
   daemon; reconnect recovers from cursor or persisted transcript; one
   stalled client does not stall turns; detach/linger/stop/SIGTERM finalize
   exactly once. Run corresponding `tests/test_daemon.py`,
   `test_prompt_queue.py`, `test_cli_restore.py` cases against both hosts.
4. **Engine adapters.** Port Claude (`doxa/engine.py`), Codex
   (`doxa/codex.py`), and chat APIs (`doxa/vendors.py`) one at a time behind
   the same `Engine` trait. Preserve Codex per-turn `exec --json`/`resume`
   streaming and stdin prompts; preserve Claude session hooks and
   interactive permission flow; preserve vendor capability omissions.
   Anthropic's [Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview)
   currently documents Python and TypeScript SDKs and recommends the CLI
   subprocess for other languages. Validate a CLI protocol that covers
   DOXA's hooks, tool calls, cancellation, resume, and approvals, or use a
   temporary external SDK sidecar; do not claim Claude parity from a
   direct Messages API client or an unvalidated CLI wrapper.
   Gate: same fixtures for tool calls/results, usage, spend ceiling,
   cancel, model/mode/branch changes, crash/restart, and no duplicate turn
   on uncertain prompt acknowledgment. Run live opt-in smoke tests for
   each provider, with fake processes in CI.
5. **External LORE and peer fabric.** Build a versioned, bounded sidecar
   request/response contract for LORE operations. Use the existing Python
   `lore_core` only inside that external adapter during transition;
   specify `scrub`, `snapshot`, `refresh`, `consult`, `review`, `index`,
   `beliefs`, `pending`, and `sync_state` operations and capability errors.
   Route all persisted/model-bound untrusted text through the scrub
   boundary, including peer frames and tool results. Port peer registry,
   control socket, ledger and rate limiter before enabling model `peer_send`.
   Gate: no unsanitized transcript or peer text reaches disk/model;
   disabled/unavailable LORE still permits local transcript and resume;
   peer scope, stale reaping, delivery attribution and rate-limit tests
   match `tests/test_peers.py`, `test_peerledger.py`, `test_peerdelivery.py`.
6. **Full TUI and orchestration.** Integrate #100, #103, and #104 after merge;
   include #102 in terminal rendering verification.
   Port arbitrary pane group tree, tabset restore, keyboard/mouse focus,
   command palette, pickers, queue/needs-input status, tool cards, diff,
   history, fleet/mesh and remote status surfaces. Wire native CLI spawn,
   attach, stop, doctor and auth/setup. Gate: terminal snapshots at 80×24
   and 120×40, full keyboard/mouse interaction tests, and a restart with
   multiple active daemons and mixed old/new tabsets. Compare against
   `tests/test_tabs.py`, `test_split_persistence.py`, `test_tabsets.py`,
   `test_fleet.py`, `test_remote_web.py`, `test_auth.py`.
7. **Cutover.** Run Python 1.x and Rust 2.0 side by side in separate
   install names. Exercise upgrade and rollback with real copied user
   state, including sessions active during frontend restart. Gate: no
   migration destroys or rewrites the only copy of a user record; a 1.x
   frontend can read 2.0-written compatible files or the writer uses a
   new versioned path with rollback. Only then rename the released binary
   to `doxa`, update launcher/install docs, and tag 2.0.

## Release measurement and blockers

Use `rust/FRONTEND_BENCHMARK.md` and
`docs/ui-benchmark-2026-09-24.md` as the baseline method, then measure
**integrated** cold startup, attach-to-first-frame, steady streaming
latency, 1,000-event replay, 10,000-line scroll, resize, peak RSS and CPU
with equal transcript fixtures. Report median and tail latency on the
same host, terminal, and fixture; do not infer full-app speed from the
Ratatui microbenchmark. Define target thresholds from the measured 1.x
baseline before release candidate, and fail on regressions in attach,
streaming responsiveness, memory bound, or terminal restore.

Release requires: protocol/storage compatibility suite; provider and
external-LORE integration suite; peer/remote threat tests; terminal
interaction suite; opt-in live Claude/Codex/chat smoke runs; clean
`cargo fmt --check`, `cargo clippy -- -D warnings`, `cargo test` for the
workspace and supported-platform build; documented rollback rehearsal.
No release tag while the native runtime still requires the Python DOXA
daemon, while any supported 1.x command has no Rust path, or while LORE
failure can prevent transcript persistence.

The largest unresolved design decision is the **external LORE contract**:
`lore_core` currently exposes Python functions and SQLite-backed objects,
not a stable language-neutral service. Specify its version negotiation,
operation schemas, deadlines, deployment and recovery behavior with the
LORE maintainers before replacing direct imports. Preserve LORE as the
authority for memory contents and scrubbing; DOXA owns session records,
wire compatibility, and the user-visible error path.
