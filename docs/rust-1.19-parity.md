# Rust 2.0 parity with DOXA 1.19.0

Baseline: Python `v1.19.0` production behavior. This tracker records implementation
through **alpha.46**, with canonical **LORE 0.62.4**. Account checks
are bounded by their exact candidate and date.
The Rust runtime implements the audited workflows below. Live account checks
remain bounded by the [latest verification record](live-provider-verification-2026-09-29.md)
and the [remaining verification](#remaining-verification) section; implementation
coverage alone does not establish authenticated compatibility for every provider.

## Implemented workflows

| Area | Implemented Rust behavior |
| --- | --- |
| Sessions | New/attach/stop/list; verified live and saved-tab restoration with order, labels, layout, drafts and focus; safe eager resume without a prompt; read-only fallback with reasons; resume/restore switches; exact and detached session kill |
| Window and prompt | Nested horizontal/vertical splits, draggable dividers and submenu borders, grouped tabs and collections, per-pane prompts, mouse hover/click, keyboard focus across tabs/chips/prompt, tab transfer, prompt-line search and generated action palette |
| Messages | Distinct user styling, clickable HTTP(S) links, processing spinner, folded tool calls and streamed reasoning counts; painted-text selection, OSC52 copy and explicit owned clipboard paste; keyboard-only `!` shell output remains private to the window |
| Questions and permissions | Inline choices, free text/Other, request identity checks, complete permission read-through, one-request approval, restored pending snapshots and blinking input indicators |
| Worktrees and diffs | Repo/worktree detection and navigation, pinned base selection and branch switch, lifecycle locks, guarded finalize/orphan cleanup/missing-checkout recovery, persistent diff and exact tracked-hunk rejection |
| LORE | Scoped curated memory browse/add/edit/remove, per-belief Accept/Reject, exact review, evidence/graph views, pending actions; native Codex/vendor canonical tools with pending/provenance gates, provider-specific context refresh and final indexing |
| Models and usage | Automatically refreshed vendor-dependent model catalogs, supported current-session model/effort changes, permission picker, memory percentages, official context/usage details and measured context grid; handling of reported Claude 5-hour/weekly quota events; plan/quota/balance chips only from reported data |
| Fleets and peers | Native startup barrier, symmetric and interactive supervised runs, budget/approval guards, durable resume, memory-off seeded assignments and quiet dwell; owned stop/detach/attach; private browser mesh; model peer opt-in, bounded broadcast/reply/history and canonical remote bridge |
| Operations | CLI/TUI setup and login/logout/device flow, first-launch setup offer, full categorized preferences and effective sources, sanitized plugin adoption/reload, adopted command completion/help/palette, doctor/update/restart and cached installation/update details |
| Compaction | Canonical Claude review and pinned Codex synchronous PreCompact contract; review outcomes and failure boundaries remain explicit |

## Completion after the original parity audit

| Release | Completed work |
| --- | --- |
| Alpha.32–35 | Source-audit ownership, deadline and durability fixes; canonical Rust LORE integration; typed controller and worker outcomes. |
| Alpha.36–37 | Inline blinking approval requests, individually expandable tool calls, native Claude CLI controls, mesh server, fleets, installer and LORE runtime. |
| Alpha.40 | Private fail-closed Codex 0.156.1, exact synchronous compaction review and guarded legacy thread migration; LORE 0.62.2 sync and native administration fixes. |
| Alpha.41 | Required Code Mode host, receipt-bound helper dispatch and native supervision that reaps escaped tool descendants. |
| Alpha.42 | Verified startup preparation before fault-injection deadlines; early invalid-payload refusal while retaining all execution integrity checks. |
| Alpha.43 | README and tracker refresh with release-specific evidence and remaining live checks; no additional runtime implementation. |
| Alpha.44 | Retain Claude's nested five-hour/weekly quota windows and startup events; preserve valid partial updates and identify native CLI provenance. Record authenticated alpha.43 verification. |
| Alpha.45 | Share native LORE settings across embedded memory, standalone carriers and review workers; resolve the sticky store before loading saved caps, signing keys and transport preferences. |
| Alpha.46 | Canonically scrubbed vendor text streaming and shared known-model capability fallback; approximate Claude thinking progress; immutable verified Codex updates; reported quota colors; hover wheel routing, delayed memory preview, Pending/Clustered titles and offline split restoration. |

Standalone LORE administration, hooks, MCP and network operations are native Rust;
they are not a remaining Python replacement task. See the
[audit follow-ups](source-audit-2026-09-27.md#completion-follow-up-in-alpha40).

## Remaining verification

The [2026-09-29 record](live-provider-verification-2026-09-29.md) reports fixed
Claude/vendor streaming checks and the incomplete default-window Codex run.
Historical alpha.43 manual and lowered-threshold automatic compaction remain
verified separately.

| Check | Current result and remaining gate |
| --- | --- |
| Claude streaming and optional quota variants | Fixed debug daemon counts advanced 50 → 150 → 200 before first text, with 104 reply deltas; thinking plaintext stayed withheld. Optional overage/model-specific quota variants remain unverified live. |
| DeepSeek recall/resume | Later three-turn streaming diagnosis passed with 316 text deltas and 81 reasoning-progress events. Earlier candidate exact-nonce failure remains unresolved. |
| z.ai GLM usage | Three turns and same-session low → high effort passed, with 408 text deltas. Numeric usage was omitted by the collector, not measured as zero. |
| Codex default-window automatic compaction | Stress stopped at the harness cap: 2,758,826 aggregate input tokens but 241,119 context tokens, below the ~244,800 trigger. No automatic compaction, Haiku review or post-compaction restart was verified. The successful 14,022-token controlled test does not establish this coverage. |
| Full historic LORE replica | Persistent loopback hub transport works; owned portable replay applied 302 of 457 ops with zero failures. An unsigned oversized operation, 154 unverified ops and one deferred dependency still gate full replication. |

All paid tests are stopped. Unknown plan, quota, balance and context values remain
unknown. DeepSeek balance can be shown when its endpoint reports it; z.ai has no
supported balance endpoint. Linux is verified. The full protected DOXA runtime
is unsupported on macOS and Windows by its Linux ownership/supervision
architecture. Standalone LORE macOS remains unverified; Windows is unsupported.

## Preserved boundaries

- Terminal images are excluded by user preference. Missing historical provider
  data cannot be reconstructed from a newer runtime.
- Only verified owned session, transcript, worktree and ledger identities authorize
  resume or mutation. Unknown legacy ownership stays protected. Deleted work
  that was never committed or recorded cannot be recovered.
- Canonical LORE trust, contradiction, pending and exact-snapshot gates remain
  authoritative. Older APIs stay read only. A belief graph shows relations, not
  verification of the claims it contains.
- Secret questions need masked private input and remain refused. Changed or
  incomplete pending requests cannot receive an approval for an earlier snapshot.
- Alpha.40 requires DOXA's private **0.156.1** fail-closed app server, an exact
  trusted synchronous review hook and disabled token-budget resets. Review
  infrastructure failures stop compaction before inference/history replacement.
  Stock builds refuse protected turns; legacy exec sessions remain read only
  until explicit migration resumes their original thread. Alpha.41 also installs
  the matching native Code Mode host and verifies its receipt for models that
  require Code Mode rather than direct shell tools. Compiled loopback
  tests verify refusal gates; alpha.43 authenticated checks verify successful
  native review and manual/controlled automatic compaction. Default-window stress
  remains unverified. Native vendor compaction retains durable history and a
  reviewed summary checkpoint.
- Unreported account, plan, quota, balance and context components stay unknown.
  Fixture tests do not establish compatibility with arbitrary live provider builds.
- Native input, output, tab, roster and snapshot bounds remain enforced. Normal
  manual tab admission is bounded; restoration retains one validated reserved
  fresh-session slot rather than silently deleting an archived conversation.
- The native remote peer bridge verifies Unix transport credentials. The retained
  Python browser remote adapter remains unavailable until its transport can
  attest proxy identity; loopback TCP and identity headers alone are insufficient.

## Commands

`/help`, slash completion and Ctrl+P describe the accepted local forms and adopted
plugin passthrough commands. Unsupported DOXA forms stay in the draft; unknown
provider commands follow the selected engine. `!<command>` is reachable only from
keyboard submission, never from slash dispatch, provider output or a peer message.

`/mesh [RUN|stop]` owns a browser graph for this window; `/peers` opens the native
map. `/fleet status`, `stop`, `detach`, `attach INDEX` and `mesh` operate on the
current owned/viewed run. Start/resume requires the complete plan review.

`/update --restart` saves the tabset and finalizes only this window's verified idle
sessions before reopening the upgraded frontend. A busy, changed or unsaved target
postpones restart. Existing daemons outside this window retain their implementation.

## Release verification

Run `./task test` for Rust regressions. Rust CI tests native installation and
provider/LORE seams against disposable stores and local provider fixtures.

| Evidence | Result and scope |
| --- | --- |
| Alpha.42 [main CI](https://github.com/docwilde/doxa/actions/runs/36472835265), commit `3e1f9d3` | 962 Rust tests passed, zero failed, four optional/harness tests ignored; installer checks and native launcher smoke passed. |
| Alpha.43 [main CI](https://github.com/docwilde/doxa/actions/runs/36476565103), commit `37ad100` | Required Rust CI passed before these account checks. |
| Compiled protected app server | Nine automatic-compaction refusal cases preserved history without compaction requests; an explicit allow control produced one checkpoint. These used a local model peer and zero paid requests. |
| Compiled Code Mode package | Three scenarios verified workspace command execution, missing-helper refusal and native DOXA/LORE call-result-detail correlation. Each used two local HTTP requests and zero paid requests. |
| Authenticated Codex, `2026-09-28T18:11:14Z` | Alpha.40 daemon with alpha.41's staged provider package: exactly two turns accepted model/effort controls, read a synthetic file and recalled its token on the same thread after daemon restart. No successful LORE review or compaction was triggered. |
| Helper/tool lifecycle | Ordinary shutdown and an exact owned daemon kill stopped and reaped the active helper and tool without harness cleanup; zero paid requests. |
| Alpha.43 authenticated parity | Two Claude turns verified native reads, text, recall and same-process model/effort controls. Four Codex prompts, one manual and one controlled automatic compaction used three real Haiku reviews and retained exact recall; original authentication remained unchanged and isolated stores/processes were cleaned. |

See the [live verification record](live-provider-verification-2026-09-28.md#alpha41-code-mode-dependency-verification)
for provenance and reproduction of the earlier alpha.40 daemon/alpha.41 package
check; that earlier result is not a fresh authenticated alpha.42 or alpha.43 check.

The [alpha.43 follow-up](live-provider-verification-2026-09-28.md#alpha43-authenticated-follow-up--2026-09-28)
records the newer authenticated checks, including the initial harness-only stale
registry failure and the separate automatic run within the remaining allowance.
Seven focused quota regressions verify alpha.44's nested-window projection,
startup caching, partial updates, reset transitions and legacy flat compatibility.
All 17 ClaudeHost fixtures passed. A
[live candidate check](live-provider-verification-2026-09-28.md#alpha44-quota-candidate-follow-up)
verified actual five-hour and weekly percentages/reset times with native CLI
provenance and no additional tools.

Alpha.31 supplied the [event-loop benchmark](rust-ui-benchmark-2026-09-27.md)
measurements. The [gallery](rust-gallery.md) remains labelled **alpha.37**,
the version actually captured. This documentation refresh changes neither the
measured build nor the screenshots.

See the [source audit](source-audit-2026-09-27.md) for confirmed follow-up fixes,
engineering debt and validation limits.

## Native runtime completion in alpha.37

Claude now uses the CLI control protocol directly. The model catalog comes from
initialize; model and effort changes are confirmed by `get_settings` on the
same process. Context and quota use reported CLI events. Legacy alpha.36 SDK
sessions migrate only when owned DOXA and CLI logs prove the same completed
session and workspace; resumed output waits for the CLI's matching identity.

All providers share native, opt-in `spawn_session` review and lifecycle controls.
The parent fixes engine, repository, depth and executable; exact approval binds
the scrubbed task and model/effort selection. Depth, live-session count, rate
and disk limits are checked again at launch. Foreign or replayed answers fail.

Codex compaction hooks and detached reviewers run native carriers with pinned
executable/source proofs and bounded process ownership. Vendor compaction keeps
original transcript/message files and writes a verified summary checkpoint.
Reported compaction usage participates in the same budget accounting as turns.

Hyper serves compiled mesh assets and attested Unix peer traffic; Reqwest
handles remote requests. The installer, launcher, plugin inventory, fleets,
MCP adapter and integrated LORE have no Python runtime dependency. Retained
Python code is a development compatibility oracle. See the native guide for
provider failure and remote identity boundaries.
