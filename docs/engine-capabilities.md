# Engine capabilities

DOXA keeps one engine per session. The model picker shows choices reported by or configured for that engine; an unsupported change is refused rather than silently mapped to another provider.

## Contents

- [At a glance](#at-a-glance)
- [Review and accounting](#review-and-accounting)
- [Platform limits](#platform-limits)
- [Where to go next](#where-to-go-next)

## At a glance

| Capability | Claude | Codex | DeepSeek | GLM | Router (opt-in) |
| --- | --- | --- | --- | --- | --- |
| Connection | Claude Code CLI | Protected private app server | Rust API client | Rust API client | Jev/SubQ selection + configured API or authenticated native CLI target |
| Authentication | Claude CLI login | Codex CLI login | `DEEPSEEK_API_KEY` or `/setup` | `ZAI_API_KEY` or `/setup` | API credentials / current CLI account; `TYPESAFE_API_KEY` / `SUBQ_API_KEY` for decision service |
| Session recovery | Verified native conversation | Same protected thread | Durable DOXA conversation | Durable DOXA conversation | Explicit same-config hash resume; canonical router history + private native worker proof |
| Model change | Idle session, verified against CLI settings | Next turn on same thread | Idle session, catalog choice | Idle session, catalog choice | Idle `/model auto` or exact configured target ID |
| Reasoning effort | Reported choices | Reported choices | Model-dependent | Model-dependent | Fixed per configured target; effective effort in route status |
| Tool permissions | Provider controls plus DOXA peer/LORE review | On-request, auto, or full-access; full-access admits built-in peer tools | DOXA peer/LORE review | DOXA peer/LORE review | Native manual/default or explicit idle Auto; target-specific DOXA reviews |
| Compaction | LORE review before provider compaction | Trusted `PreCompact` review in private build | Reviewed summary checkpoint | Reviewed summary checkpoint | Reviewed API summary; CLI summary target refused, originals retained |
| Prompt files | Images and UTF-8 text | Images and UTF-8 text | UTF-8 text | UTF-8 text | UTF-8 text; images refused |

Provider tool features depend on the installed CLI, model, and account. `/model`, `/effort`, and `/mode` expose only supported choices. Model and effort changes wait for the current turn and queue to finish; the supported active permission transitions are described below. The picker can show separately sourced context, thinking, and price facts for an exact model ID; unknown fields stay unknown and facts do not make a model selectable. [Model fact provenance](plans/model-registry.md).

The **Attach** chip, **Alt+A** and Linux PNG clipboard paste send bounded image
content directly to Claude/Codex, including Docker sessions. Text files are
quoted input for all engines. Legacy Codex exec refuses images; PDF and remote-hub
attachment submission remain unsupported. See [attachment formats and limits](terminal-images.md#prompt-attachments).

## Review and accounting

Continuing a saved session measures its history together with the new request before sending that request. Native engines use provider context telemetry and configured compaction thresholds where available; API engines conservatively bound the serialized request, including system text and tools. DOXA reserves response headroom and keeps fitting conversations intact. When input exceeds the budget, it keeps the newest turns, marks omitted or clipped content, and retains the complete original transcript. The continuation notice distinguishes provider token counts from byte bounds and identifies a conservative budget when the effective model limit is unavailable. Reviewed compaction checkpoints remain usable.

Codex `on-request` reviews protected commands, file changes, and permission profiles inline. `auto` retains its sandbox; `full-access` removes that sandbox. In beta.47, authoritative Codex `full-access` also admits the exact registered built-in peer list, send and history tools without a second DOXA approval. Exact session IDs, scope, credential scrubbing, rate and fleet checks still apply. `on-request` and `auto` retain peer review; changing to Auto during a turn leaves a pending peer review actionable. LORE, child-session and external MCP review gates remain active. DeepSeek and GLM have no provider permission mode; their peer and LORE calls are individually reviewed. Optional vendor workspace reads are off by default. Explicit external MCP tools are available to API workers, with mandatory one-call review, server-qualified names and scrubbed bounded results; native CLI workers retain their own MCP ownership. See [external API MCP tools](vendor-mcp.md). External MCP charges are unknown and outside the model allowance.

Incoming direct and broadcast peer messages can start a model turn when Settings → Session →
**Wake on peer messages** is enabled. A busy receiver queues the message after
its current turn. The saved setting is checked when the message arrives;
an explicit process/fleet environment override takes precedence. Wake remains
off by default. Every recipient retains its own permission and budget checks.

Local DOXA peer sends report a bounded recipient admission receipt when supported:
`started`, `queued`, `disabled`, `retained`, `rejected` or `dropped`. Started and
queued confirm admission, not provider completion. A missing receipt is
unconfirmed, including legacy or remote recipients; inspect the recipient before
retrying an uncertain send. A disabled recipient needs its owner's setting change.
Long generated titles are abbreviated only in display metadata; exact session
identity and project authorization scope remain unchanged.

Router Auto is a target-selection choice, separate from permission mode. It does
not grant permissions. API workers keep their narrower tool surface; native
Claude/Codex workers use existing protected command/file review controls. Each turn checks configured target
eligibility, and the UI keeps effective worker identity separate from Auto or
the pin. Router owns aggregate durable routing/worker reservations; an attempted
Jev call with unknown usage withholds worker execution. Configured descriptions
are operator policy and token-based price bounds are estimates. See the
[router operator guide](jev-router.md) for fallback and resume limits.

Bare router `/compact` uses the pin, last ordinary worker or configured fallback
without changing Auto/pin. Completed canonical-source LORE review precedes the
bounded API summary, with aggregate budget and unknown-usage limits still active.
A CLI pin/last worker is refused for compaction; explicitly pin an eligible API
summary target.
The UI keeps summary-worker status separate from ordinary routing. The pinned
LORE worker currently records derivation provenance under the selected API
provider; DOXA source proof/storage/replay stay `router` and review receipts bind
the exact summary target. Compaction fixtures and controlled native review
carrier checks are local evidence; paid summary and real-task quality checks
remain open. [Compaction details](jev-router.md#reviewed-managed-compaction).

With beta.43 and the updated private provider, Codex can switch from `on-request` to `auto`
during an active turn. The waiting provider escalation is declined so the model
can retry inside the existing sandbox; questions and DOXA reviews stay open.
Other permission or sandbox transitions require idle. A development
[live check](live-codex-auto-permissions-2026-10-10.md#development-switching-an-active-turn)
passed the pending-command switch, two later automatic commands and an
outside-write denial on the same provider thread. Current releases include the
updated daemon/provider pair. An already-running older daemon retains its
original transition limits until it is restarted.

Claude and Codex context and usage figures come from their reported telemetry. DeepSeek and GLM display estimates from token counts and dated rates; cache discounts or off-peak billing can change the final bill. Missing quota or component counts remain unknown. Budgeted fleets require complete accounting for the selected model and basis. `BudgetHost` exposes its dated static price bound and keeps the actual billed tier and charge unknown. See [fleet supervision](fleet-supervision.md).

## Platform limits

Linux has bounded live verification for the four direct engines: Claude, Codex, DeepSeek and GLM. macOS builds and transport tests run in CI, but authenticated provider sessions need live checks. Protected Codex is Linux-only because its process ownership and compaction contract have no macOS equivalent. Windows is unsupported. See [platform verification](platform-verification.md) and the [provider verification record](live-provider-verification-2026-10-04.md).

Router supports native API and authenticated CLI worker sessions. Protected
Codex workers retain the Linux-only private provider requirement. Explicit
`cli_account` candidates exclude unknown CLI charges from the tracked Jev/API
USD allowance; mixed-session telemetry keeps total charge unknown even after a
later API turn or reattach. Complete observed native token usage is required,
and uncertain attempted work is not replayed. Credential-free native fixtures
are separate from authenticated worker/real-task verification. An authenticated six-call
[synthetic Jev smoke](router-evaluation.md#observed-synthetic-smoke) verified
typed transport and recorded-response replay. Worker success, real-task routing
quality and macOS live behavior remain unmeasured. Reviewed child/fleet config
and allowance inheritance, history recovery and native/Docker migration are
implemented; the [integration record](router-integration-2026-10-13.md) bounds
their local verification. External CLI handoffs and CLI managed-summary targets
remain unavailable. Internal CLI routing transfers bounded canonical history
to a fresh private worker; it does not transfer an arbitrary external CLI session.

## Where to go next

- [Rust guide](../rust/README.md) for installation and everyday controls.
- [Codex engine contract](../rust/doxa-engines/README.md) for protected provider details.
- [Session isolation](session-isolation.md) for native and Docker execution.
- [Jev router](jev-router.md) for explicit config, Auto/target controls and evaluation.
- [LORE integration](https://github.com/docwilde/LORE) for shared reviewed memory.
