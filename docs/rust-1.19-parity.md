# Rust 2.0 parity with DOXA 1.19.0

Baseline: Python `v1.19.0` production consumers, rather than planned APIs or
unused transport helpers. Alpha.31 closes the audited functional gaps below.
Alpha.32/33 add the subsequent source-audit ownership, deadline and durability
corrections. Alpha.34 integrates canonical LORE 0.61.0 in Rust, including the
retained SDK/MCP adapter paths, detached review and reconciliation.
Alpha.35 separates controllers and uses typed, owner-qualified worker outcomes
without changing the audited interaction contracts.
Alpha.36 moves permission requests above the prompt: select Approve/Deny, press
Enter, or press A to approve. Requests blink until answered; complete-input review
and original request ownership still apply.
Each tool call expands independently within its turn's tool section. Click a
call or focus the transcript, select with `[`/`]`, then press Enter or Space.
Terminal images remain excluded by user preference. Provider capabilities,
trust gates and unavailable historical data retain their documented limits.

| Area | Rust alpha.31 |
| --- | --- |
| Sessions | New/attach/stop/list; verified live and saved-tab restoration with order, labels, layout, drafts and focus; safe eager resume without a prompt; read-only fallback with reasons; resume/restore switches; exact and detached session kill |
| Window and prompt | Nested horizontal/vertical splits, draggable dividers and submenu borders, grouped tabs and collections, per-pane prompts, mouse hover/click, keyboard focus across tabs/chips/prompt, tab transfer, prompt-line search and generated action palette |
| Messages | Distinct user styling, clickable HTTP(S) links, processing spinner, folded tool calls and streamed reasoning counts; painted-text selection, OSC52 copy and explicit owned clipboard paste; keyboard-only `!` shell output remains private to the window |
| Questions and permissions | Inline choices, free text/Other, request identity checks, complete permission read-through, one-request approval, restored pending snapshots and blinking input indicators |
| Worktrees and diffs | Repo/worktree detection and navigation, pinned base selection and branch switch, lifecycle locks, guarded finalize/orphan cleanup/missing-checkout recovery, persistent diff and exact tracked-hunk rejection |
| LORE | Scoped curated memory browse/add/edit/remove, per-belief Accept/Reject, exact review, evidence/graph views, pending actions; native Codex/vendor canonical tools with pending/provenance gates, provider-specific context refresh and final indexing |
| Models and usage | Automatically refreshed vendor-dependent model catalogs, supported current-session model/effort changes, permission picker, memory percentages, official context/usage details and measured context grid; live Claude 5-hour/weekly quota events; plan/quota/balance chips only from reported data |
| Fleets and peers | Native startup barrier, symmetric and interactive supervised runs, budget/approval guards, durable resume, memory-off seeded assignments and quiet dwell; owned stop/detach/attach; private browser mesh; model peer opt-in, bounded broadcast/reply/history and canonical remote bridge |
| Operations | CLI/TUI setup and login/logout/device flow, first-launch setup offer, full categorized preferences and effective sources, sanitized plugin adoption/reload, adopted command completion/help/palette, doctor/update/restart and cached installation/update details |
| Compaction | Canonical Claude review and pinned Codex synchronous PreCompact contract; review outcomes and failure boundaries remain explicit |

## Preserved boundaries

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
  until explicit migration resumes their original thread. Compiled loopback
  tests verify the gate; successful real-account LORE review and large-context
  compaction remain unverified without Claude reviewer authentication. Native
  vendor compaction retains durable history and a reviewed summary checkpoint.
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
Alpha.31 supplied the event-loop benchmark measurements. Production gallery captures
are labelled by their rendered package version;
see the [benchmark](rust-ui-benchmark-2026-09-27.md) and
[gallery](rust-gallery.md). Live-provider compatibility remains bounded by the
explicit supported contracts.
The [live verification record](live-provider-verification-2026-09-28.md) separates
actual account checks from fixture coverage and missing authentication.

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
