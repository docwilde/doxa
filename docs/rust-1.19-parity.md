# Rust 2.0 parity with DOXA 1.19.0

Baseline: the `v1.19.0` Python tag, especially `doxa/commands.py`, its session
command handlers, and the 1.19 worktree and fleet contracts. This records the
Rust branch's behavior after `v2.0.0-alpha.10`; it is a release gate, not a
claim that all Python behavior has been ported.

| Area | Rust state | Remaining 1.19 behavior |
| --- | --- | --- |
| Core sessions | Native daemon, Codex and vendor hosts, Claude SDK sidecar; new, attach, stop, list and restore | Full setup/auth flow and live attachment of other discovered sessions inside an open TUI |
| Window and prompt | Two split panes, tabs, grouped rail, per-session drafts, mouse selection and resize, inline questions, Markdown and tool cards | More than two pane groups, moving a tab across groups, pinned tab names and sidebar collections |
| Commands | Local bare commands, `/msg`, `/mesh`, `/pane 1|2`, sidebar controls and `/dir` | Many argument forms and the full generated command palette; see table below |
| Worktrees and diffs | Managed per-session Git checkout, guarded clean cleanup, diff pane and idle tracked-hunk rejection | Worktree base switching, queued rejection during a turn, rejection reasons, complete orphan cleanup |
| LORE | Context/scrub via sidecar, belief/evidence, full proposal review and exact-snapshot approve/reject with LORE 0.58.5 | Writable belief actions beyond pending resolution; older LORE builds remain read only |
| Peers and fleets | Peer map and direct message; Python fleet inspection/attach; validated slot stop; native read-only preflight | Native spend/turn enforcement, fleet supervisor/barrier/approval desk, live fleet tab and remote routing |
| Operational UI | Native doctor and install launcher | 1.19 setup/settings/update/login/logout/plugin screens and detailed usage/context screens |

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
| `/diff` | Partial | Queued rejections and a user-entered reason |
| `/fleet` | Bridge or partial | Native supervisor, approval desk, barrier, budgets and fleet tab |
| `/model`, `/engine`, `/mode` | Local picker for bare form | Supported argument forms and provider-specific capabilities |
| `/beliefs`, `/pending` | Belief reading and staged approve/reject after complete raw review | Belief confirm/contradict/stale/retract controls |
| `/sessions`, `/search`, `/resume`, `/attach` | History search and CLI attach/Claude resume | Full cross-session search, live TUI attach and resume in a new tab |
| `/usage`, `/context`, `/queue` | Summary chips/queued prompt transport | Detailed screens and queue cancellation |
| `/help`, `/about` | Local action menu/version | Registry-wide help and full diagnostics |
| `/compact` | Provider pass-through | LORE review before provider compaction |
| `/movepane`, `/collection`, `/rename`, `/cd`, `/clear` | Missing | Tab/collection state and safe new-session directory controls |
| `/branch`, `/effort` | Missing | New-session base and effort controls |
| `/login`, `/logout`, `/settings`, `/setup`, `/doctor`, `/update` | CLI doctor only | Interactive operations and 1.19 setup checks |
| `/plugins`, `/reload-plugins` | Missing | Plugin discovery, adoption policy and refresh |
| `/img` | Missing | Terminal image capability/reporting if required for stable parity |

## Stable 2.0 gates

1. Finish worktree lifecycle and branch controls, and verify the LORE 0.58.5
   dependency after both repositories merge.
2. Port or explicitly scope every user-facing 1.19 command and window action;
   ensure DOXA commands never accidentally become model prompts.
3. Add end-to-end startup, daemon, and terminal tests for the supported hosts,
   session restore, fleet runs and cross-version attachment.
4. Re-run the full UI performance benchmarks on the final Rust event loop and
   update the gallery from the actual binary.

The Rust guide in [`rust/README.md`](../rust/README.md) describes commands
already available in the preview.
