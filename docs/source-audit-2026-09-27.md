# DOXA source audit — 2026-09-27

## Scope and conclusion

The audit began after alpha.31 shipped from main commit
`24be3718f653b4ce4a6306fa125d47cd2935e386`. Three parallel reviewers examined
the frontend, provider/runtime boundaries, and lifecycle/installation paths.
The integration review checked their changes across those boundaries.

The architecture follows dependency inversion at several important boundaries,
but does not consistently follow DRY or single responsibility. The review found
actual ownership and deadline failures as well as structural debt. Alpha.32
addresses the reproduced failures listed below; it is not a claim that every
legacy module has been refactored or every provider build is supported.

Coverage included Rust CLI/TUI rendering, menus, focus, drafts and clipboard;
transport, discovery, saved state and restore; runtime admission and host
adapters; Claude, Codex and vendor integration; LORE and compaction; fleets,
worktrees and peers; mesh and retained remote adapters; installer and task
entry points. Retained Python code was reviewed where the Rust implementation
uses it or an optional entry point can expose it. Historical frontend behavior
was used as the parity baseline.

Validation uses disposable stores, local sockets, fake SDK/CLI implementations
and owned subprocesses. No paid provider/reviewer request or real credential
fixture was used. Live account compatibility, macOS/Windows behavior and an
exhaustive line-by-line proof are outside this review's evidence.

## Findings addressed in alpha.32

| Priority | Finding and consequence | Correction and evidence |
| --- | --- | --- |
| P1 | Retained browser adapter treated loopback TCP as authenticated Tailscale proxy identity; another local user could forge an allowlisted header. | Refuse requests and startup until a transport can attest proxy credentials. Real ASGI routes reject spoofed headers before registry/daemon access. The native credential-checked Unix peer bridge remains available. |
| P1 | Native linger expiry cancelled a detached turn while the provider was still working. | Runtime atomically checks clients, queue, control operations and provider work before expiry; completion starts a full idle interval. Local socket/race and fake-Claude process fixtures exercise the transition. |
| P1 | Detached compaction reviewers could survive an abruptly killed owning sidecar/hook and continue provider work. | A shared supervisor owns the review process group, observes a private control pipe and kills/reaps owned descendants. SIGKILL and successful-review fixtures verify cleanup without relying on recyclable numeric PID checks. |
| P2 | Asynchronous launch/attach moved another session's unsent draft into the newly active prompt. | All activation paths use one pane/session draft transition; submission and clipboard-completion regressions preserve ownership. |
| P2 | Failed attach omitted its session identity, leaving the tab stuck pending. | Failure replies retain the target; a router-to-UI regression verifies pending state clears. |
| P2 | Saved-resume completion used the currently focused pane instead of its initiating pane. | Capture and revalidate the original owner, including admission at completion; switched/removed-pane fixtures cover both outcomes. |
| P2 | Ordinary Unix connect could block before its hello deadline. | Reuse the bounded connection helper and one handshake deadline; an owned full-backlog socket fixture demonstrates the bound. |
| P2 | Opening from the session rail bypassed the manual tab limit. | Reuse manual admission while permitting focus of an existing tab; boundary interaction regression. |
| P2 | Worktree configuration used an independent unbounded blocking reader. | Reuse the canonical bounded private config loader; FIFO and oversized-config fixtures. |
| P2 | Git subprocess deadlines did not bound a reader waiting for inherited stdout after the leader exited. | Own the process group and nonblocking capped output through cleanup; inherited-pipe, overflow and successful-output fixtures. |
| P2 | Claude settings/credential provisioning used predictable temporary paths that could follow symlinks or collide. | One private atomic writer uses unique exclusive nofollow files and an owned directory descriptor; synthetic symlink/concurrent-provision fixtures. |
| P2 | Successfully launched native daemons were dropped without reaping them when they later exited. | Transfer owned child handles to a wait-only reaper; preserve detached daemon lifetime and reap stopped children. |
| P2 | Tool activity stopped being collected for sessions after the first 64 ever observed. | Bounded recent-session eviction admits later sessions instead of permanently rejecting them. |
| P2 | Deliberately detached saved records prevented later layout persistence. | Retain detached records separately from unexplained missing state; close-then-open/save regression. |
| P2 | Claude stream overflow released the runtime queue before the provider acknowledged cancellation. | Separate bounded terminal delivery from streamed deltas, interrupt once and retain ownership until the real terminal; queued-second-turn socket fixture. |
| P2 | The Claude launcher stopped waiting after 10 seconds although SDK initialization allows at least 60 seconds and one credential retry. | Share a bounded startup policy across launcher and host; a real native daemon with an owned slow fake sidecar verifies admission beyond the former deadline. Known SDK failure classes produce fixed safe diagnostics. |
| P3 | LORE status reads relied on garbage collection to close SQLite connections. | Explicitly close on query success/error; private SQLite fixture holds references to prove prompt retirement. |

User checks of the installed release also identified presentation/configuration
defects corrected in this batch: the Markdown-parsed welcome graphic, transient
new-session failure forms, permanent engine emphasis, immediate chip tooltips,
appended hyperlink URLs and stale empty Codex web request input. Link destinations
now travel as explicit renderer metadata through wrapping, tables, roles and folds.
Codex's generated local schema supplied the web action shapes; result bodies absent
from that event are described as unavailable rather than synthesized.

Belief ordering uses recorded LORE timestamps, with stable ID ordering only as a
tie-breaker. Both LORE browsing menus use table rows and prompt-line filters;
curated facts come from the canonical entry API rather than splitting displayed
Markdown. Delayed belief tooltips have a separate bounded read API for full display text;
redaction or omitted content is marked incomplete. Acceptance and rejection
retain complete exact-snapshot review gates.

DOXA already calculated memory percentages from LORE's own character counts and
caps. However, it did not propagate the allowlisted capacity settings carried in
Claude's configuration, so a configured project cap could diverge from the native
sidecar's default. Explicit process environment settings still take precedence.
The reported 116% was not established as an exact live numerator by this audit;
the configuration mismatch was verified independently of memory contents.

The mesh HTTP 404 reported during this work was already corrected in alpha.31:
the wheel now includes `assets/mesh`, with an installed-wheel HTTP regression.
The installed CLI also served the page, JavaScript, CSS and ledger successfully
from outside a checkout. Existing server processes need restarting after update.

## Engineering assessment

- **DRY:** the model picker is one shared frontend with provider-specific catalog
  adapters. Canonical LORE remains the authority. Duplicate configuration and
  subprocess policies had diverged and caused failures; the fixes reuse canonical
  configuration and review ownership. Registry scanning and provider capability
  policy still have multiple implementations that need explicit contract tests.
- **Dependency inversion:** `doxa-runtime::Host` isolates runtime admission from
  provider transports. Budget/peer wrappers must forward ownership and capability
  queries; the linger fix makes that requirement explicit. String method names
  and JSON values still duplicate schema validation across layers. Validated
  command types would improve this boundary without merging distinct transports.
- **Single responsibility:** the large `ui.rs` combines event reduction, worker
  completion, menus and rendering. Extracting reducer/state transitions and
  command dispatch is warranted. File length alone is not the finding: draft and
  resume bugs demonstrate inconsistent ownership transitions in that coordinator.
- **Process and data ownership:** exact owned teardown, immutable review
  snapshots, worktree locks and LORE trust gates are useful existing safeguards.
  Their lifetime/deadline guarantees need to hold through successful completion,
  abrupt parent death, wrapper forwarding and asynchronous UI activation.

## Follow-up fixes in alpha.33

| Finding | Correction and evidence |
| --- | --- |
| Background control replies overwrote the focused notice; disconnected queues could clear unrelated effort verification. | Typed owner-qualified replies and a shared reducer preserve other sessions' drafts, notices and admitted verification. |
| Capability checks and effort policy were duplicated, and effort metadata was shared across sessions. | Reuse engine capabilities and vendor policy. Session-qualified catalogs treat empty metadata as authoritative; failed/loading refreshes revoke choices. Keyboard and mouse application recheck current capabilities. |
| Registry scans duplicated unbounded enumeration and entry checks. | State, discovery, peer registry and presence use canonical bounded readers with explicit overflow, opened-inode checks and private ownership rules; Python peers retain matching limits. |
| Clean-session metadata could get ahead of durable transcript bytes. | Sync the complete final transcript record and directory before atomically syncing checkpoint metadata. Resume validates the byte boundary and final record. Fixtures cover publication failures and legacy recordings; no actual power outage was simulated. |
| Peer and mesh processing lacked finite resource limits. | Cap refusal history, active connections, headers, deadlines, ledger/record/page sizes, routing identities, recipients and normalized JSON. Oversized pages preserve whole-record cursors. Browser node/tie/replay/feed capacities evict coherent projections and display a limited-view notice; an owned Node rotation fixture verifies bounded retention. |
| Claude startup rejected the user's `uv` interpreter despite a private installation tree. | Permit group-writable Python only beneath a safely reached, owned private directory. Public writable paths and world-writable files remain refused. Propagate a fixed launcher diagnostic to the opening view without exposing raw errors. |

The integrated Rust workspace passed 856 test executions with all features
enabled. Focused peer/mesh Python checks passed 111 tests. Browser/page/wheel follow-up checks passed 79 tests. These are separate
runs, not additive unique-test totals. Fixtures use private stores and owned
processes; no paid provider request or real memory mutation was performed.
The startup follow-up passed the complete 423-test frontend library suite and
34 Python launcher/sidecar tests (24 subtests). An actual Claude initialization
probe used a private workspace, disabled memory and sent no prompt: direct
sidecar initialization succeeded while the installed pre-fix native launcher
failed. The release installation is checked again after publication.

## Remaining work and boundaries

Alpha.35 separates the frontend coordinator into controllers and carries typed
worker outcomes through bounded channels. Provider wire payloads retain their
native JSON schemas; legacy consumers use one compatibility adapter. Provider
adapters retain distinct transports.

Canonical native LORE shipped in alpha.34. Standalone LORE administration and
network services retain Python compatibility entrypoints. Live provider/account
compatibility and macOS/Windows behavior remain outside the local fixture evidence.

The optional retained browser remote adapter is deliberately unavailable until
its transport can prove proxy identity. Codex protected compaction remains pinned
to its documented contract; operating-system hook startup failures can fail open
inside Codex before DOXA observes and stops the session. See
[parity boundaries](rust-1.19-parity.md).

## Verification record

Local validation passed 827 Rust workspace test executions with all features
enabled (no failures or ignored tests). The final prompt-focus and delayed-hover
changes then passed the complete 414-test frontend library suite. The LORE Rust
suite passed 18 tests. Focused Python boundaries passed
138 tests with one optional browser fixture skipped; the updated LORE suite
passed 40 tests (including full display reads) and Claude sidecar unit checks
passed 28 tests. Counts describe
separate runs and are not additive totals.

Focused regressions accompany the fixes. The release PR records the integrated
Rust workspace, Python sidecar/authentication gates, CI results and installed
launcher smoke check. The gallery is regenerated from the alpha.32 production renderer. Performance
figures dated 2026-09-27 originate from alpha.31; they are measurements of that
version, not new alpha.32 benchmarks.


## Native LORE integration in alpha.34

Canonical LORE 0.61.0 now owns DOXA memory, belief/evidence/graph reads,
exact reviewed mutations, context refresh, session indexing/history, local sync
records and detached review/reconciliation in Rust. The native frontend and
host call the library in-process; retained SDK/MCP adapters call `lore-rs`.
The installed Python package does not depend on Python LORE. Its development
copy remains only as an interoperability oracle and explicit legacy fixture seam.

The integration audit corrected valid signed proposal refusals, transcript
index reply fields and nullable remote session metadata. Python/native replay
preserves original verified belief, memory and file-map provenance. Native
review freezes engine/session identity and exact transcript proof; changed
sources cannot apply model output, and promotions remain pending.

Resource gates now reject oversized database fields before owned allocation,
limit aggregate loader working data, and bound provider pipes and metadata.
Pinned directory descriptors confine private writes and locks as well as reads.
Landed writes followed by sync or reconciliation failures report partial or
`may_have_applied`, so a retry cannot claim the earlier effects were absent.
Native worker fixtures verify descendant cleanup on success, error and timeout.

Standalone LORE plugin administration and network transport services retain
their Python compatibility entrypoints. The native module covers DOXA's active
memory semantics; those standalone tools are not described as Rust ports.

Checkout task commands now build and select the native carrier explicitly. The
LORE upgrade workflow moves both native and development-oracle pins and locks
to the same immutable commit. Retained adapter pagination uses the canonical
50-row page size and preserves caller windows; a 600-belief socket fixture
checks that it does not stop after the first page.

## Engineering cleanup in alpha.35

The frontend state definitions and defaults stay in `ui.rs`. Controllers own
input, layout, session navigation, archived history, LORE review, diffs, model
selection and explicit operations. Session-event reduction, transcript events,
telemetry, rendering and the terminal loop have their own modules. Existing
frontend entrypoints, state types and interaction contracts remain compatible.

One command registry now generates typed local command identity, help,
completion and palette entries. Unknown commands retain provider passthrough;
malformed reserved forms remain drafts. Private shell execution remains reachable
only from explicit keyboard submission.

Bridge routing and worker channels carry `WorkerFrame` variants with target
session, pane group, request and delivery identities. Real daemon payloads stay
opaque at the wire boundary. Model, permission and effort outcomes go directly
through the same owner-qualified reducer as decoded wire replies. Other legacy
frame consumers use one conversion adapter. The native terminal loop receives
both legacy and typed sources without a relay thread or extra queue, preserving
its 64-frame tick budget and existing 32/128 channel bounds.

Independent source comparisons checked preserved function bodies, public APIs,
command forms, owner binding and null/absent-field compatibility. Local fixtures
exercise malformed provider replies, superseded effort results, target refusal,
exact unsent drafts, backpressure and owned router shutdown. Release CI retains
the installed native-carrier and SDK adapter checks.

`MultiBridge.frames` now yields `WorkerFrame`; direct bridge consumers can
handle those variants or call `into_legacy_value` during migration. Legacy
`run_with_frames` and JSON channel entrypoints remain available without relays.

## Native runtime follow-up in alpha.37

The Claude SDK adapter, mesh subprocess, remote peer bridge, compaction hook,
review supervisor, plugin inventory and session spawner have native replacements.
Claude controls use the CLI's catalog/settings contract; shared session spawning
uses one provider-independent host seam and canonical review owner. Hyper handles
HTTP parsing and SSE; Reqwest handles remote clients. Installed runtime paths
require no Python interpreter.

Compaction pins the native carrier inode and exact transcript bytes. Original
vendor history remains durable beneath its reviewed summary checkpoint. Desktop
asset creation and publication use the same pinned directory descriptor. Native
installation markers reject symlinks/hardlinks and unsafe permissions. Controlled
provider, filesystem and installed-launcher fixtures cover these boundaries;
live-provider and gallery checks retain their explicit provenance.
