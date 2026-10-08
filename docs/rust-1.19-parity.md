# Rust 2.0 parity with DOXA 1.19.0

Baseline: Python `v1.19.0` production behavior. This tracker records implementation
through **alpha.70**, with canonical **LORE 0.62.11**. Account checks
are bounded by their exact candidate and date.
Beta.9 upgrades the canonical native core to **LORE 0.62.17** and verifies
large scrub frames; the dated provider checks below retain their original scope.
The Rust runtime implements the audited workflows below. Live account checks
remain bounded by the [latest verification record](live-provider-verification-2026-09-29.md)
and the [remaining verification](#remaining-verification) section; implementation
coverage alone does not establish authenticated compatibility for every provider.

## Implemented workflows

| Area | Implemented Rust behavior |
| --- | --- |
| Sessions | New/attach/stop/list; verified live and saved-tab restoration with order, labels, layout, drafts and focus; detached tabs stay closed on restore; past sessions appear muted below live projects; safe eager resume without a prompt; read-only fallback with reasons; resume/restore switches; exact and detached session kill |
| Window and prompt | Nested horizontal/vertical splits, draggable dividers and submenu borders, grouped tabs and collections, per-pane prompts, mouse hover/click, configurable window shortcuts, keyboard focus across tabs/chips/prompt, tab transfer, prompt-line search and generated action palette |
| Messages | Distinct user styling, clickable HTTP(S) links, processing spinner, folded tool calls and streamed reasoning counts; painted-text selection, OSC52 copy and explicit owned clipboard paste; keyboard-only `!` shell output remains private to the window |
| Questions and permissions | Inline choices, free text/Other, request identity checks, complete permission read-through, one-request or per-tool session approval, restored pending snapshots and blinking input indicators |
| Worktrees and diffs | Repo/worktree detection and navigation, pinned base selection and branch switch, lifecycle locks, guarded finalize/orphan cleanup/missing-checkout recovery, persistent diff and exact tracked-hunk rejection |
| LORE | Scoped curated memory browse/add/edit/remove with row selection, delayed full-fact previews and scrollbars; per-belief Accept/Reject, exact review, evidence/graph views, pending actions; native Codex/vendor canonical tools with pending/provenance gates, provider-specific context refresh and final indexing |
| Models and usage | Automatically refreshed vendor-dependent model catalogs, supported current-session model/effort changes, permission picker, memory percentages, official context/usage details and measured context grid; reported Claude and Codex subscription windows; vendor API cost estimates from reported token counts and dated published rates |
| Fleets and peers | Native startup barrier, symmetric and interactive supervised runs, budget/approval guards, durable resume, memory-off seeded assignments and quiet dwell; owned stop/detach/attach; private browser mesh; model peer opt-in, clickable peer map, bounded broadcast/reply/history and canonical remote bridge |
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
| Alpha.47 | Accept verified Cargo build outputs with read/execute permissions or extra hard links while retaining strict single-link installed artifacts and receipt checks. |
| Alpha.48 | Fix observed Codex peer-send aliases, add per-tool session approval and clickable peer history, block Greek welcome art, and selected curated facts with delayed scrollable previews and menu scrollbars. |
| Alpha.49 | Refresh the README and user docs; recapture seven unedited frames from the real Rust app, including a new welcome frame. No runtime behavior changes. |
| Alpha.50–51 | Capture the belief/memory browsers from the live app, rerun the Rust UI benchmark, and record bounded DeepSeek/GLM stop/resume recall and usage. |
| Alpha.52 | Pin LORE 0.62.6 session replay repair and add a credential-free Codex default-window compaction fixture. |
| Alpha.53 | Record the authenticated default-window Codex compaction and post-restart recall; check in the bounded verifier. |
| Alpha.54 | Pin LORE 0.62.7. A private fresh direct-peer replay settled 4,568 signed operations with 4,555 applied, 13 quarantined, no failed/deferred/unverified rows and a banked cursor. |
| Alpha.55 | Pin LORE 0.62.8 with stricter native credential scrubbing. Name new sessions from model and Git context or a short path, with stable duplicate suffixes. |
| Alpha.56 | Close only the active tab with Ctrl+X and leave the TUI with Ctrl+Q while sessions run detached. Use Ctrl+Left/Right for tabs and Shift+Left/Right for pane prompts; verified dead detached sessions leave the rail. |
| Alpha.57–58 | Configure window shortcuts at runtime; restore session rail focus and overflow navigation; display queued, running and unread states; route pending LORE commands to the local browser. |
| Alpha.59 | Group uncollected sessions by project; estimate DeepSeek/GLM API spend from reported tokens and published rates; read the Codex subscription quota bucket after turns. |
| Alpha.60 | Pin LORE 0.62.9 to keep project-bound pending proposals out of unrelated review browsers; recapture two real Rust gallery frames with two project groups. |
| Alpha.61–62 | Harden startup config and peer delivery; keep mesh serving after browser-open failure; pin LORE 0.62.10 sync and review fixes. |
| Alpha.63 | Add macOS native transport/build coverage and an application shortcut; pin LORE 0.62.11; attach the optional browser adapter to a kernel-attested Unix proxy. Protected Codex remains Linux-only. |
| Alpha.64 | Register Claude's stdio permission callback and allow live permission-mode changes so inline approval requests and the `auto` picker take effect during an active turn. Label Codex's fixed `on-request` policy explicitly; Codex mode switching remains unavailable in DOXA. |
| Alpha.65 | Forward Codex permission-profile requests into the inline review and grant the exact requested profile for one turn; hide the unsupported DeepSeek/GLM permission chip. Belief-row Accept/Reject and unmodified A/R apply after an exact LORE fetch; `/` or clicking the prompt enables text filtering. |
| Alpha.66 | Show overflow scrollbars throughout scrollable submenus. Let an idle Codex session choose `on-request`, sandboxed `auto`, or `full-access` for its next turn; restore that choice with the saved session. |
| Alpha.67 | Scrollbar thumbs reach the last track cell at the final page. Restore flat detached records without reopening tabs, save generated labels, and show recoverable dead sessions below live projects in muted italic text. |
| Alpha.68 | Recapture the nine README frames from the running Rust frontend and daemon, including grouped sessions and LORE browsers. |
| Alpha.69 | Run bounded alpha.68 Claude, DeepSeek and GLM native release checks; expand macOS CI to portable Rust crates, daemon suites, and a local SSE vendor lifecycle fixture. |
| Alpha.70 | Add the opt-in Rust browser adapter, private hub and outbound connector; keep cross-machine history volatile and prompt admission guarded by current daemon permissions. |

Standalone LORE administration, hooks, MCP and network operations are native Rust;
they are not a remaining Python replacement task. See the
[audit follow-ups](source-audit-2026-09-27.md#completion-follow-up-in-alpha40).

## Remaining verification

The [2026-10-04 provider record](live-provider-verification-2026-10-04.md)
reports fresh alpha.68 Claude and vendor stop/resume recall and numeric usage. The
[authenticated Codex result](live-default-window-compaction-2026-09-30.md)
now verifies one default-window compaction cycle and post-restart recall on
alpha.52. The [2026-09-29 record](live-provider-verification-2026-09-29.md)
retains fixed Claude/vendor streaming checks and its earlier incomplete run.

| Check | Current result and remaining gate |
| --- | --- |
| Claude streaming and optional quota variants | Alpha.68 two-turn exact-token recall across daemon restart passed with reported usage. Each short reply used one text delta. Earlier fixed debug daemon counts advanced 50 → 150 → 200 before first text, with 104 reply deltas; thinking plaintext stayed withheld. Optional overage/model-specific quota variants remain unverified live. |
| DeepSeek recall/resume | Alpha.68 two-turn native file-read and exact-nonce recall passed across daemon stop/resume with complete vendor-reported usage. Earlier candidate failure's cause remains unknown; the three-turn streaming diagnosis passed separately with 316 text deltas and 81 reasoning-progress events. |
| z.ai GLM usage | Alpha.68 two-turn native file-read and exact-nonce recall passed across stop/resume. Complete numeric vendor-reported usage was captured on both turns. Earlier three turns and same-session low → high effort passed with 408 text deltas. |
| Codex default-window automatic compaction | **Passed for alpha.52 and protected Codex 0.156.1 with `gpt-5.5`:** seven data turns reached one native LORE review and one checkpoint; exact first-turn token recall passed after one daemon restart. The seventh turn's aggregate input was 1,172,180 tokens. See the [measured result](live-default-window-compaction-2026-09-30.md) for scope and retained evidence. |
| Full historic LORE replica | A private fresh direct-peer replay of 4,568 signed source ops settled 4,555 applied and 13 quarantined, with zero unverified, failed or deferred and an advanced cursor. Original IDs, MACs and payloads match; normalized belief, edge, evidence and session digests match. The existing hub retains immutable older unsigned copies and was not migrated. |

The one approved paid compaction cycle is complete; no further paid tests are
running. Unknown plan, quota, balance and context values remain
unknown. DeepSeek balance can be shown when its endpoint reports it; z.ai has no
supported balance endpoint. Linux is live verified. macOS receives portable native
workspace suites and local provider lifecycle CI, but authenticated provider sessions
remain unverified there.
Protected Codex is Linux-only by its ownership/supervision contract. Windows
remains unsupported.

## Preserved boundaries

- Standalone local Markdown images now render in the Rust transcript with
  bounded previews and alt-text fallback; see [terminal images](terminal-images.md).
  Provider binary attachments remain open. Mermaid has opt-in local preview
  support, with real CLI and terminal validation still open.
  Missing historical provider data cannot be reconstructed from a newer runtime.
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
  native review and manual/controlled automatic compaction. Alpha.52 verified
  one authenticated default-window cycle and post-restart recall. Native vendor compaction retains durable history and a
  reviewed summary checkpoint.
- Unreported account, plan, quota, balance and context components stay unknown.
  Fixture tests do not establish compatibility with arbitrary live provider builds.
- Native input, output, tab, roster and snapshot bounds remain enforced. Normal
  manual tab admission is bounded; restoration retains one validated reserved
  fresh-session slot rather than silently deleting an archived conversation.
- The native remote peer bridge and Rust browser adapter verify Unix transport
  credentials. The private Rust hub brokers prompts and live events through an
  outbound host connector; its history and commands are volatile. The retained
  Python browser adapter is a compatibility path. Native remote tabs and
  background push remain open.

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

Alpha.50 supplied the [current event-loop benchmark](rust-ui-benchmark-2026-09-30.md)
measurements; the [alpha.31 baseline](rust-ui-benchmark-2026-09-27.md) remains
available. The [gallery](rust-gallery.md) records nine **alpha.68** live terminal
captures, including grouped sessions and the memory browsers.

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
