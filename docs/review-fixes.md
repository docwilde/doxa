# Beta47 review remediation

Baseline: `v2.0.0-beta.47`, commit
`e682d029799c80791b426a61ac5ee74d30a908cf`. The fixes are integrated on
`fix/beta47-review-findings`. This is a source remediation record; it does
not announce a new release or an installed update.

## Findings and disposition

| Finding | Implemented change | Remaining boundary |
| --- | --- | --- |
| BE-001: external plugin assets | Require explicit per-plugin external roots, validate config/root permissions, preserve atomic staging. | Adoption remains opt-in; approved roots authorize their contents. No race-proof same-user confinement claim. |
| BE-002: queued model changes | Forward the provider idle policy through `BudgetHost`. | Actual runtime regression uses a synthetic provider. |
| IF-001: Mermaid paint stalls | Scan paragraphs once; cache parsed sources by transcript revision. | Transcript hashing remains bounded paint work; full-frame performance was not benchmarked. |
| IF-002: Router paint I/O | Observe freshness in one background worker and paint cached status. | Save/Start recheck directly. Key availability is checked, replacement key/account identity is not. |
| IF-003: catalog worker pressure | Own one worker, cancel/reap it before admitting the latest queued selection, discard obsolete generations. | Live authenticated provider discovery was not run. |
| IF-004: stored Python graphs | Pin released LORE v0.62.21 and add scoped Python snapshot client/response fixtures. | Syntax bindings remain unknown; storage still requires explicit LORE human review. |
| IF-005: unknown fleet spend | Document conservative retirement with the entire old review budget retained and independent replacement allocation. | No automatic settlement or cumulative hold enforcement across runs. |
| F-REMOTE-001: root pages | Share route normalization; test authenticated retrieval and unauthenticated denial in both servers. | Live proxy/tailnet acceptance was not run. |
| F-REMOTE-002: candidate CI privileges | Separate read-only candidate execution from a fresh publisher; independently resolve and compare the trusted lock. | Relies on GitHub runner/job isolation and protected review. |
| F-REMOTE-003: event freshness | Bind incarnation in versioned AES-GCM AAD across Rust, browser, and Android; share a test vector. | Coordinated client/publisher upgrade required; legacy ciphertext fails authentication. |
| F-REMOTE-004: Android slow responses | Bound finite exchanges and acknowledgement polling; disconnect on cancellation. | Protocol tests passed; full APK/device testing unavailable locally. |
| F-REMOTE-005: RSA dependency chain | Remove optional JWT/RSA chain; use direct P-256 VAPID and ring RS256 FCM signing. | No live push/FCM delivery was attempted. |
| F-REMOTE-006: source install identity | Support a caller-supplied exact SHA before Cargo; state source-build trust accurately. | Equality is not publisher authentication; release signatures/attestations require a separate trust policy. |
| F-REMOTE-007: mutable action tags | Pin external actions to verified full SHAs; enforce with a linter. | Future updates require deliberate pin review. |
| F-REMOTE-008: apt reproducibility | Pin snapshot and direct package versions for explicit Debian/Ubuntu families and two architectures. | Signed archive metadata checked; Docker image build was not run. |
| KaTeX advisory | Override only KaTeX to 0.18.2, preserving Mermaid CLI 12. | Private CLI rendering tested; DOXA renderer sandbox acceptance was not run. |

The protected Docker hook-origin question remains documented in the original
review: same-user/bearer admission is not exact container-origin attestation.
Hardened modes remain unavailable pending that proof.

## Verification

The workspace build passed. Workspace Rust testing was split after the first
900-second command limit, with no assertion failures before the limit. The
remaining engine suites, remaining workspace packages, and excluded library
doc-test targets then passed. The initial TUI library suite passed **800 tests**. After the UI follow-ups,
the final TUI library suite passed **824 tests**, with six intentionally ignored
tests. The native LORE carrier was built from the
existing released pin and configured for history/picker fixtures; the
unreleased upstream patch did not replace that carrier.

Other completed checks:

- The actual runtime queue guard, plugin staging, root authorization, shared
  encrypted event vector, replay rejection, and FCM/VAPID signature fixtures.
- Browser extension suite: **19 passed**; separate recovery suite: **11 passed**.
- Supply tooling: **28 focused Python tests passed**, including a fake-Cargo
  installer mismatch fixture; shell syntax and action-pin lint passed.
- Android protocol tests passed offline. No local Android SDK was available
  for full application compilation.
- Upstream LORE's six codegraph snapshot tests passed, including Python
  round-trip, stale-source rejection, worktree isolation, and symlink denial.
  Beta48 subsequently pins the released LORE v0.62.21 and matching client.
- Identical optimized parser fixture: **10,694 ms before, 8 ms after**, for
  493,425 bytes and 31 fences. All eight parser cases produced expected
  results. This is one parser-only observation, not a TUI/renderer benchmark.
- Private Mermaid CLI install with lifecycle scripts/browser download disabled,
  local KaTeX diagram rendering, and npm audit: **zero findings**.
- Debian/Ubuntu snapshot signatures and direct package pins verified for
  `amd64` and `arm64`; no apt installation or Docker build.

The offline RustSec scan used database commit
`7eebec69c352c7191b1f13eb95dd510eeca5d1de` and found zero vulnerability
entries. Existing `lru` 0.12.5 soundness warnings and `paste` 1.0.15 maintenance
warning remain tracked. The affected LRU APIs were not found reachable in
Ratatui's layout-cache path; a compatible upstream update is still needed.
Yanked-package status was not refreshed. Zero vulnerability entries does not
mean all dependency warnings are resolved.

No owner configuration, credentials, installed executable, paid provider call,
live peer delivery, physical device, or deployed service was changed by these
checks. macOS CI, real source installation, live Docker isolation, and live
provider acceptance remain release/deployment gates.

## User-reported follow-ups

The integration also includes portable context and quota bars, session-bound
last-context restoration, Codex belief-count forwarding, the Peers chip,
Settings dismissal/focus handling, desktop-helper clipboard copy, and clearer
canonical peer-send guidance and persisted peer-card provenance. Verification
of these follow-ups is recorded in the detailed interface record. The full
824-test TUI suite covers Settings
save/dismiss/type, nested-menu admission, pane drafts, context restoration,
quota validation/scrolling, chips, private clipboard helpers, and typed peer
history restoration/deduplication. Focused Codex
quota, Codex host, and canonical peer-guidance tests also passed.
Final peer provenance checks passed 97 core tests, 69 daemon unit tests,
three isolated delivery process fixtures and the 61-test Router host target.
These counts include overlapping targets and should not be added together.

Mid-turn steering was investigated from the pinned Codex source and installed
Claude CLI. Codex offers acknowledged `turn/steer`; Claude exposes a queued
input path with an opportunity to fold at a model-loop boundary. DOXA does not
yet expose either as a steering control. See the
[feasibility and implementation plan](mid-turn-steering.md).

## Beta48 release candidate verification

The release candidate pins published **LORE v0.62.21**, commit
`e398ea2b0d8b1a8c52614ee69fa483005c5e9c81`. The matching DOXA client passed
14 library, 12 native and 19 sidecar tests, including scoped Python reads and
stale-source rejection. The final TUI library suite passed **827 tests**, with
six intentionally ignored tests. Jev fleet supervision passed 27 unit and
12 policy tests; no paid Jev supervisor call or real-task calibration was run.

All **28** beta48 gallery PNGs were regenerated at **3068 × 1734** with
private fixture state. Browser checks exercised every image and caption,
previous/next wrapping, arrow keys, history, deep links, invalid hashes and
mobile width. The hosted site uses the same manifest as the README's
one-frame pages. Protected Linux and macOS CI remain the merge gates.

## Detailed records

- [Backend changes](review-fixes-backend.md)
- [Interface, codegraph, and fleet boundaries](review-fixes-interface.md)
- [Remote protocol and Android changes](review-fixes-remote.md)
- [Supply and dependency changes](review-fixes-supply.md)

The original review bundle remains at
`/home/docwilde/finch-artifacts/doxa-reviews/beta47-luna-20261011`.
Remediation logs and hashes are collected separately so the reviewed baseline
evidence stays intact.
