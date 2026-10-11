# DOXA beta.46 code review — 2026-10-11

## Target and method

The review began **after beta.46 was released** and used the frozen tag
[`v2.0.0-beta.46`](https://github.com/docwilde/doxa/tree/v2.0.0-beta.46),
commit `6519076ccf02392e8cf04dfd0b5f3ba3a27b7016`. Follow-up fixes are in the
beta.47 candidate. Three delegated reviewers covered the interface/code graph/
fleet, backend/engine/isolation, and remote/Android/packaging boundaries.
The requested GPT-6-Terra model was unavailable; no Terra review occurred.

This was a structural inventory and review of sensitive source paths,
supplemented by controlled reproductions against copied beta.46 binaries,
actual extension code, installer code, and bounded local fixtures. It was
**not an exhaustive line-by-line audit or proof that all best practices hold**.
The inventory contained 930 tracked files and 134,632 physical source lines
across Rust, Python, JavaScript, Kotlin, shell, CSS and HTML, including tests;
128,776 were nonblank. This is a reproducible file-line count, not semantic LOC.

No owner credentials, configuration or installation were changed. Provider,
MCP, peer and HTTP fixtures used private state and synthetic credentials.
No paid model request, live search account, Docker provisioning, FCM delivery
or remote device acceptance test was performed.

## Confirmed findings

Severity refers to the released beta.46 defect and its stated prerequisite.
The focused integration results below belong to the beta.47 candidate.

| Area | Released defect | Follow-up and evidence |
| --- | --- | --- |
| High: native credential boundaries | Claude output and structured property names could disclose an arbitrary known inactive API key in display/history. | Central exact-key redaction before/after LORE, provider prompts and structured history; five actual daemon fixtures passed, including fragmented streams and unsafe-store refusal. |
| High: Rust code graph | A 1,012-byte file with 500 nested blocks aborted the process on recursive parsing. | Nonrecursive complexity bounds before both syn entry points; 103 codegraph checks passed, three ignored, plus separate CLI and default-stack cases. Some complex valid Rust is deliberately reported unparseable. |
| High: remote review identity | Extension/native/CLI paths could submit a changed same-ID request as though it had been displayed; gaps did not retire controls. | Exact captured request and incarnation required; contiguous replay and complete snapshot recovery. Bare CLI approvals now require a private review receipt. Node/native fixtures exercise valid and stale decisions. |
| High: Android review | A title hid full arguments; click callbacks adopted the current pending request rather than the rendered request. | Complete selectable request JSON and captured identity; pure Kotlin long-tail and same-ID fixtures passed. App/device acceptance remains a separate gate. |
| Medium: Router lifecycle | Stop during provisional CLI discovery left a catalog child running. | Cancellation and owned process-group retirement through discovery/startup; six actual daemon variants passed, with no prompt or replay. |
| Medium: persistence locks | Config/fleet mutation could wait indefinitely on flock, blocking Settings. | Shared 100 ms lock deadline and explicit busy refusal; state/fleet concurrency and byte-preservation fixtures passed. |
| Medium: transcript append | Write-open of an owned FIFO blocked before regular-file validation. | Nonblocking open; all four public append APIs tested both with and without a reader. |
| Medium: Git discovery | Startup scope lookup had no process, pipe or output bound. | Shared bounded observer preserves main/linked-worktree identity; stalled, descendant-held and oversized output fixtures passed. |
| Medium: terminal shutdown | A full command target could strand CommandRouter shutdown at join. | Typed nonblocking admission/refusal and receiver lifetime ordering; bounded mixed-router tests passed. The original proof used the exact methods with inert transport stubs. |
| Medium: fleet accounting | Failed reserved calls could display a zero complete token estimate. | Sticky unknown accounting and pause; preserve reservations/known subtotal, label total unknown. Journal-only regression and debrief checks passed. |
| Medium: remote HTTP | JSON collection from a configured hub had no byte cap. | Count streamed bytes before parsing, bound total time and preserve eight-command envelope; local oversized/streamed/stalled-body fixtures passed. |
| Medium: Android alerts | The 24-hour registration expired without foreground renewal. | Monotonic lease proof, foreground/timer renewal, stale-callback rejection; pure protocol fixtures passed. No live push delivery claim. |
| Medium: installation | Protected Codex publication survived a later failed frontend install. | Exact-publication rollback receipt joins the outer transaction; two actual-shell failure injections and 17 installer checks passed. Power-loss/concurrent frontend atomicity remains unproved. |
| Low: stored Python graph | Public LORE snapshot lookup rejected Python despite parser/viewer support. | Admit exact rs/py extensions and exercise public native store plus CLI lookup; focused verification pending. |
| Low: Settings draw | Completed Router review reopens private config/key storage during paint. | **Open:** move freshness checks to a bounded controller worker while preserving save/start admission checks. No measured latency or security-bypass claim. |

The native key and FIFO reproductions used controlled fake provider processes.
The Router reproduction measured a 31 ms daemon exit with its synthetic catalog
child still alive. The installer reproduction injected a staging failure and
observed old frontend retention with the new provider pointer still active.
These establish specific defects; they do not establish a live account attack.

## DRY, DIP and performance

The review found concrete duplication failures: native hosts applied different
credential policies, remote adapters applied different review predicates, and
Git discovery had a separate unbounded implementation. The follow-up shares
redaction, review admission and bounded observation at those boundaries.
Cross-language clients still need conformance fixtures for request replacement,
replay gaps, missing snapshots and stale callbacks.

Owned host/tool callbacks and command/frame seams allow tests to replace
provider, UI and transport details. Preserve those dependency boundaries:
permission decisions belong to the authoritative host mode and captured
review, rather than a DOM/Compose callback or provider annotation. Resource
limits must apply before parsing/collection; downstream wire caps cannot bound
an upstream HTTP allocation. Installer publication domains need explicit
transaction ownership and recovery evidence.

Large engine hosts and TUI controllers remain maintenance risks. Extracting
pure policy/lifecycle helpers has a clear payoff; adding a generic abstraction
for every engine difference has not been justified. Budget reservations,
known usage subtotals and unknown billed charges must remain separate.

Performance review found availability and repeated-I/O defects. It did not
run a whole-project throughput benchmark. The earlier codegraph warm timing
measurement remains scoped to its documented fixture; it does not prove cold
or cross-platform latency. Mesh O(N²) layout and slow-filesystem Settings
freshness require measurement before making performance claims.

## Coverage and remaining verification

- Interface slice: 146 Rust files / 75,127 physical lines, including tests;
  persistence, discovery, codegraph bounds/cache, fleet accounting/evidence,
  Settings/credentials, transcript/queue/peer paths, terminal ownership and
  image/plugin/process boundaries were reviewed. Many UI branches/tests were
  sampled; no complete terminal/platform interaction matrix was run.
- Remote/packaging slice: 148 selected files, including scripts, Kotlin,
  browser clients, wire/hub, CI, containers and systemd units. Python/shell/JS/C
  syntax checks and local Node/Kotlin/installer fixtures were run. This is not
  a claim that every selected line received a semantic audit.
- Backend slice: daemon/engines/vendors/Claude/router/runtime/peers/protocol/
  LORE/hub and isolation ownership/admission/accounting paths. Supplementary
  inspection covers egress/quota/runner/migration and broker boundaries.
  No live container/kernel enforcement or provider entitlement was verified.

Unresolved candidates include inherited pipe ownership after child exit and
whether event authentication should bind a restarted session incarnation.
These are recorded as uncertainties, not confirmed exploits. Live Docker
quotas/egress, authenticated search services, physical Android/FCM, tailnet,
semantic codegraph/fleet quality, crash durability and dependency advisory
coverage remain separate work. The pinned dependency inventory was inspected;
no current advisory-database scan or reproducible-build proof was performed.

Final candidate-wide build, workspace and protected CI results will be recorded
with the release. Focused passes do not substitute for those gates.
