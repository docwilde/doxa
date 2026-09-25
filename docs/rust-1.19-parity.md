# Rust 2.0 parity with DOXA 1.19.0

Baseline: the `v1.19.0` Python tag, especially `doxa/commands.py`, its session
command handlers, and the 1.19 worktree and fleet contracts. This records the
Rust branch's behavior at `v2.0.0-alpha.22`; it is a release gate, not a
claim that all Python behavior has been ported.

| Area | Rust state | Remaining 1.19 behavior |
| --- | --- | --- |
| Core sessions | Native daemon, Codex and vendor hosts, Claude SDK sidecar; new, attach, stop, list and restore; TUI `/attach` for live sessions and `/resume` for verified saved Claude/Codex/vendor sessions, including recovery of a missing managed checkout with pinned metadata | Full setup/auth flow; recovery of deleted uncommitted changes is impossible |
| Window and prompt | Two split panes, persistent named tabs, guarded `/movepane` tab transfer, persistent named collections with foldable rail headings, per-session drafts, mouse selection and resize, inline questions, Markdown with Ctrl+click HTTP(S) links, live and newly restored Claude/Codex tool detail, expandable reasoning, processing spinner inside transcript; active-session repo/worktree chip | More than two pane groups, moving a final tab out of a pane; historic Codex transcripts cannot recover details that were never stored |
| Commands | Local bare commands, `/msg`, `/mesh`, `/pane 1|2`, sidebar controls, `/dir`, `/cd <path>`, `/attach` and `/rename`; inline autocomplete for supported slash commands | Many argument forms and the full generated command palette; see table below |
| Worktrees and diffs | Managed per-session Git checkout, guarded clean finalization, `new --branch`, idle live base switching, orphan preview and explicit verified Rust-orphan cleanup CLI, guarded missing-checkout recovery, diff pane and tracked-hunk rejection with queued active-turn feedback and a reason; unpinned Python 1.19 sidecars cannot be adopted, switched, or deleted by Rust | Shared lifecycle lock before cleanup of legacy Python sidecars, plus recovery when ownership or pinned Git metadata cannot be verified |
| LORE | Context/scrub via sidecar, scoped curated memory and up to 20 global beliefs in an inline chip menu, belief/evidence picker, exact belief review and confirm/contradict/stale/retract actions, full proposal review and exact-snapshot approve/reject with LORE 0.58.5 | Older LORE builds remain read only; broader 1.19 memory management screens |
| Peers and fleets | Peer map and direct message; optional native inbound peer turns; Python fleet start/inspection/attach; validated slot stop; native read-only preflight including supervisor/approval checks, Claude reported-cost ceiling, and priced DeepSeek/GLM ceiling for known models with complete usage | Native Codex priced budget and durable budgeted resume, fleet supervisor/barrier/approval desk, live fleet tab and remote routing |
| Operational UI | Native doctor and install launcher; read-only CLI `setup`, `auth status`, and Claude Code `plugins` inventory; CLI `update`; per-session usage/context detail panels | Interactive setup/settings/login/logout/plugin adoption and provider context component breakdown |

## Slash command coverage

“Local” means handled by the Rust frontend and never sent as an agent prompt.
“Partial” means a narrower Rust form, action, or chip exists. “Bridge” means
the Rust CLI invokes the current Python fleet harness. Unsupported DOXA
argument forms stay in the draft with a notice. Unknown provider and plugin
commands still pass to the active engine.

| Python 1.19 commands | Rust state | Next required behavior |
| --- | --- | --- |
| `/split`, `/vsplit`, `/pane`, `/sidebar`, `/detach`, `/dir` | Local | More pane groups and complete sidebar sizing/restore semantics |
| `/peers`, `/mesh`, `/msg` | Local or partial | Rich peer details, browser mesh and remote peers |
| `/diff` | Partial | Full 1.19 diff command options and worktree controls |
| `/fleet` | Bridge or partial; native Claude and known-model vendor per-session spend ceilings | Native supervisor, approval desk, barrier, Codex priced budget, budgeted resume and fleet tab |
| `/model`, `/engine`, `/mode`, `/effort` | Local picker for bare form; new-session vendor models refresh from account catalogs, with DeepSeek per-model effort and measured GLM fallback; live effort chip reports daemon state; known DeepSeek/GLM models accept idle live effort changes for the next turn | Supported argument forms for other engines and newer catalog-only vendor models |
| `/beliefs`, `/pending` | Belief reading, exact reviewed confirm/contradict/stale/retract actions, and staged approve/reject after complete raw review | Broader 1.19 memory management screens |
| `/sessions`, `/search`, `/resume`, `/attach` | Bounded archived transcript search, verified saved Claude/Codex/vendor resume with guarded missing-checkout recovery, CLI attach/Claude resume and live TUI attach picker with ID/title search | Full indexed cross-session search |
| `/usage`, `/context`, `/queue` | Scrollable per-session usage/context panels and queued prompt list/cancel picker | Provider context component breakdown and more detailed usage history |
| `/help`, `/about` | Local action menu/version | Registry-wide help and full diagnostics |
| `/compact` | Explicit Claude command waits for completed LORE review; older sidecars and Codex/vendor compaction are blocked | Review gate for automatic provider compaction and other supported engines |
| `/movepane`, `/collection`, `/rename`, `/cd`, `/clear` | `/movepane [1|2]` moves an active tab between two groups while retaining a source tab; named `/collection new|rename|delete|add|remove` orders the rail; `/rename` local; `/cd <path>` opens a verified new-session directory | Final-tab movement and fresh-session replacement with `/clear` |
| `/branch` | `new --branch`, idle live base switch, CLI branch listing | Full branch command argument forms |
| `/login`, `/logout`, `/settings`, `/setup`, `/doctor`, `/update` | CLI doctor, update, read-only setup report and auth-status probes | Interactive operations and settings changes |
| `/plugins`, `/reload-plugins` | Read-only CLI Claude Code plugin inventory | Plugin adoption policy and refresh |
| `/img` | Missing | Terminal image capability/reporting if required for stable parity |

## Stable 2.0 gates

1. Share a lifecycle lock with legacy Python before legacy orphan deletion. Keep the
   LORE 0.58.5 atomic review boundary covered as later versions are adopted.
2. Port or explicitly scope every user-facing 1.19 command and window action;
   ensure DOXA commands never accidentally become model prompts.
3. Add end-to-end startup, daemon, and terminal tests for the supported hosts,
   session restore, fleet runs and cross-version attachment.
4. Re-run the full UI performance benchmarks on the final Rust event loop and
   update the gallery from the actual binary.

The Rust guide in [`rust/README.md`](../rust/README.md) describes commands
already available in the preview.
