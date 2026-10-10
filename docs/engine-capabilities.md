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
| Connection | Claude Code CLI | Protected private app server | Rust API client | Rust API client | Jev selection + configured DeepSeek/GLM API target |
| Authentication | Claude CLI login | Codex CLI login | `DEEPSEEK_API_KEY` or `/setup` | `ZAI_API_KEY` or `/setup` | Worker credentials; `TYPESAFE_API_KEY` for Jev |
| Session recovery | Verified native conversation | Same protected thread | Durable DOXA conversation | Durable DOXA conversation | Explicit same-config hash resume; canonical API messages |
| Model change | Idle session, verified against CLI settings | Next turn on same thread | Idle session, catalog choice | Idle session, catalog choice | Idle `/model auto` or exact configured target ID |
| Reasoning effort | Reported choices | Reported choices | Model-dependent | Model-dependent | Fixed per configured target; effective effort in route status |
| Tool permissions | Provider controls plus DOXA peer/LORE review | On-request, auto, or full-access; peer/LORE review stays active | DOXA peer/LORE review | DOXA peer/LORE review | DOXA peer/LORE review; no provider permission-mode control |
| Compaction | LORE review before provider compaction | Trusted `PreCompact` review in private build | Reviewed summary checkpoint | Reviewed summary checkpoint | Unavailable in first slice |

Provider tool features depend on the installed CLI, model, and account. `/model`, `/effort`, and `/mode` expose only supported choices. Changes wait for the current turn and queue to finish. The picker can show separately sourced context, thinking, and price facts for an exact model ID; unknown fields stay unknown and facts do not make a model selectable. [Model fact provenance](plans/model-registry.md).

## Review and accounting

Codex `on-request` reviews protected commands, file changes, and permission profiles inline. `auto` retains its sandbox; `full-access` removes that sandbox. DOXA peer and LORE tools keep their own human review in every mode. DeepSeek and GLM have no provider permission mode; their peer and LORE calls are individually reviewed. Optional vendor workspace reads are off by default.

Router Auto is a target-selection choice, separate from permission mode. It does
not grant Bash or workspace writes. Each turn checks configured target
eligibility, and the UI keeps effective worker identity separate from Auto or
the pin. Router owns aggregate durable routing/worker reservations; an attempted
Jev call with unknown usage withholds worker execution. Configured descriptions
are operator policy and token-based price bounds are estimates. See the
[router operator guide](jev-router.md) for fallback and resume limits.

With beta.43 and the updated private provider, Codex can switch from `on-request` to `auto`
during an active turn. The waiting provider escalation is declined so the model
can retry inside the existing sandbox; questions and DOXA reviews stay open.
Other permission or sandbox transitions require idle. A development
[live check](live-codex-auto-permissions-2026-10-10.md#development-switching-an-active-turn)
passed the pending-command switch, two later automatic commands and an
outside-write denial on the same provider thread. The older installed beta.42
provider still requires idle; deployment needs the updated daemon and provider.

Claude and Codex context and usage figures come from their reported telemetry. DeepSeek and GLM display estimates from token counts and dated rates; cache discounts or off-peak billing can change the final bill. Missing quota or component counts remain unknown. Budgeted fleets require complete accounting for the selected model and basis. `BudgetHost` exposes its dated static price bound and keeps the actual billed tier and charge unknown. See [fleet supervision](fleet-supervision.md).

## Platform limits

Linux has bounded live verification for the four direct engines: Claude, Codex, DeepSeek and GLM. macOS builds and transport tests run in CI, but authenticated provider sessions need live checks. Protected Codex is Linux-only because its process ownership and compaction contract have no macOS equivalent. Windows is unsupported. See [platform verification](platform-verification.md) and the [provider verification record](live-provider-verification-2026-10-04.md).

Router currently supports native API sessions only. Its local fixtures do not
establish authenticated Jev or worker routing quality, account availability or
macOS live behavior. Docker, CLI handoffs and fleet/child config inheritance are
unavailable in the first slice.

## Where to go next

- [Rust guide](../rust/README.md) for installation and everyday controls.
- [Codex engine contract](../rust/doxa-engines/README.md) for protected provider details.
- [Session isolation](session-isolation.md) for native and Docker execution.
- [Jev router](jev-router.md) for explicit config, Auto/target controls and evaluation.
- [LORE integration](https://github.com/docwilde/LORE) for shared reviewed memory.
