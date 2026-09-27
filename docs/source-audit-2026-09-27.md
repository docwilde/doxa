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

## Remaining work and boundaries

Structural refactoring should be a separate change with preserved behavioral
contracts. Start with typed UI commands and owner transitions, a shared provider
capability policy, and one registry reader. Avoid replacing provider adapters
with a single implementation merely to reduce file count.

A remaining low-priority UI issue is that a background session's model or
permission reply can replace the active session's status notice. The actual
settings update remains scoped to its session. Qualifying notices by owner
should accompany the typed reply refactor.

The transcript writer's ordering relative to Codex clean-session metadata also
needs a durability review: append currently does not synchronize storage before
the clean marker. A power-loss recovery failure was not reproduced in this audit;
transactional checkpoint durability should be validated before claiming that case.

Availability concerns needing further focused validation include the retained
Python peer refusal history, browser mesh connection concurrency and ledger
batching, and unbounded registry-directory enumeration. These are not claimed
fixed by the asset-packaging change.

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
