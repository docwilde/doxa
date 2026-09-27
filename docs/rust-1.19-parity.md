# Rust 2.0 parity with DOXA 1.19.0

Baseline: Python `v1.19.0`, especially its command registry, session actions,
worktree lifecycle, and fleet contracts. This records the integrated alpha.29
state. It is a release gate, not a claim of universal provider compatibility.
Terminal images are explicitly excluded by user preference.

| Area | Implemented in Rust alpha.29 | Remaining boundary |
| --- | --- | --- |
| Sessions | Native Codex/vendor hosts, Claude SDK sidecar; new/attach/stop/list; bounded restore; verified saved-session resume and guarded missing-managed-checkout recovery | Deleted uncommitted files and history never stored cannot be recovered; uncertain ownership/state is refused |
| Window and prompt | Recursive splits up to 16 groups, 256 session tabs, nested horizontal/vertical divider drag, persistent topology/collections/labels/drafts, tab transfer, keyboard/mouse selection, prompt-line search, generated searchable action palette | Final source tab cannot be moved, matching Python; bounded layout and recovery validation remain mandatory |
| Questions and permissions | Codex/Claude question choices, free text and Other, stable question IDs, exact pending-request snapshots, full permission-summary read-through and one-request approvals | Secret questions are refused without masked private input; incomplete snapshots need refresh; stale requests cannot be answered |
| Worktrees and diffs | Managed checkout, pinned base, lifecycle lock, guarded finalize/orphan cleanup/missing-checkout recovery, branch selection/switch, bounded modal/persistent diff and exact tracked-hunk rejection with idle feedback | Legacy unpinned ownership cannot authorize adoption, switching, cleanup, or recovery; staged and whole-file metadata hunks are refused |
| LORE | Scoped memory browse/add/edit/remove with complete before/after review and canonical exact-snapshot mutation; beliefs/evidence and pending proposal actions | Trust, conflict, pending, detached-store, and atomic-API gates remain authoritative; older APIs stay read only |
| Context and usage | Official provider usage/window data, Claude reported categories/files/tools/agents and injection metadata; explicit estimates and unknowns | Historic missing detail and unavailable provider component/plan/quota data remain unknown |
| Fleets and peers | Native controller, startup barrier, supervisor peer tools, approval desk, budget gates, guarded durable resume, manifest views and verified attach; real saved run selection; owned start/resume review controller; local peer messaging/map and CLI private browser mesh | Unsupported native harness options use explicit start-python; incomplete accounting/pricing or legacy private-ledger evidence is refused; TUI browser-mesh argument forms remain unavailable |
| Operations | CLI/TUI setup, selected CLI login/logout including Codex device flow, cancel/reap, public-only progress; sanitized plugin refresh/adoption; native settings | Wider Python settings require explicit implementation; CLI doctor/update are available but their unsupported slash forms remain drafts |
| Compaction | Claude LORE review gate; pinned Codex 0.156.1 trusted PreCompact hook verification, manual official compaction and review outcome monitoring | Codex OS hook failures/timeouts/invalid output can fail open before parent observes failure; vendor compaction unavailable |

## Slash command coverage

Local commands are handled by the frontend. Unsupported DOXA forms remain in
the draft with a notice; unknown provider/plugin slash commands follow the
normal engine path. Help and completion describe actual accepted forms.

| Python 1.19 commands | Alpha.29 behavior and limits |
| --- | --- |
| `/split`, `/vsplit`, `/pane`, `/movepane`, `/sidebar` | Recursive pane groups, numbered focus/move and sidebar size controls; source retains its final tab |
| `/collection`, `/rename`, `/detach`, `/dir`, `/cd`, `/clear` | Local collections/labels/detach; verified new directory tab; clear requires idle state and durable writable tabset |
| `/peers`, `/mesh`, `/msg` | Local peer map and same-project messaging; browser mesh is CLI `mesh serve` / `fleet mesh RUN`, not a TUI argument form |
| `/diff`, `/branch` | Worktree diff and guarded hunk rejection; branch picker or guarded explicit base switch. No unverified extra diff options are advertised |
| `/fleet` | Runs/status/verified slot attach; exact native start options and resume with full plan review, explicit arming, tracked subprocess and teardown; saved real run views in Ctrl+P |
| `/model`, `/engine`, `/mode`, `/effort` | Pickers and supported named forms; authoritative capabilities/catalogs, idle/queue guards, and pending Claude effort verification |
| `/beliefs`, `/pending` | Belief/evidence and exact reviewed actions; proposal complete review and atomic approve/reject. Curated memory actions are available from the memory chip |
| `/sessions`, `/search`, `/resume`, `/attach` | Prompt-line query/results, scrubbed indexed excerpts and bounded fallback, exact owned-file verification, live attach and saved-session recovery |
| `/usage`, `/context`, `/queue` | Reported totals/context details and bounded queue preview/cancel; missing provider detail remains unknown |
| `/help`, `/about` | Scrollable 42-command registry and version; generated Ctrl+P actions from commands, tabs, saved fleet views and session operations |
| `/compact` | Claude review-gated and protected Codex official compaction; unsupported engines/contracts refused |
| `/login`, `/logout`, `/setup`, `/settings` | Selected provider login/logout; Codex-only device flag; asynchronous setup and native linger/worktree settings with environment shadows read only |
| `/plugins`, `/reload-plugins` | Sanitized inventory, adoption controls and refresh through operations menu |
| `/doctor`, `/update` | Native CLI forms available; unsupported slash forms stay in draft |
| `/img` | Explicitly excluded by user preference |

## Stable 2.0 gates

1. Keep canonical LORE trust/pending/conflict and exact reviewed-snapshot gates
   covered when its APIs change. Do not make legacy unverified ownership writable.
2. Resolve or explicitly retain the pinned Codex provider limitation: 0.156.1
   treats hook infrastructure failures as fail open. DOXA can stop after observing
   failure, but cannot promise compaction was prevented in that case. Unknown
   Codex builds must continue to refuse protected startup.
3. Maintain full startup/daemon/terminal regressions for supported transports,
   saved state, native fleet approvals/budgets/teardown, and cross-version attach.
   The UI milestone passed **401 tests across 21 suites**; fixture tests do not
   substitute for supported live-provider contract validation.
4. Re-run final Rust event-loop performance checks and regenerate the production
   gallery from the release build. Gallery fixtures must remain clearly labelled,
   nonmutating, and unable to launch providers or controllers.
5. Preserve honest scope for missing historical telemetry, excluded images,
   wider settings, unsupported native fleet options, and TUI browser-mesh forms.
   Remote routing and invented diff-option rows are not additional 1.19 gates.

Existing daemon processes keep their old implementation after upgrading.
Restart an idle session and resume verified state to use new host behavior.
Settings changes require an idle session with no queued prompts. Catalog
capabilities and effective runtime state must agree before showing a change
as applied; unknown defaults do not justify guessing a model or effort.

See the [Rust guide](../rust/README.md) and
[production-layout gallery](rust-gallery.md) for available commands and images.
