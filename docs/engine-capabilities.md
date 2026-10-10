# Engine capabilities

DOXA keeps one engine per session. The model picker shows choices reported by or configured for that engine; an unsupported change is refused rather than silently mapped to another provider.

## Contents

- [At a glance](#at-a-glance)
- [Review and accounting](#review-and-accounting)
- [Platform limits](#platform-limits)
- [Where to go next](#where-to-go-next)

## At a glance

| Capability | Claude | Codex | DeepSeek | GLM |
| --- | --- | --- | --- | --- |
| Connection | Claude Code CLI | Protected private app server | Rust API client | Rust API client |
| Authentication | Claude CLI login | Codex CLI login | `DEEPSEEK_API_KEY` or `/setup` | `ZAI_API_KEY` or `/setup` |
| Session recovery | Verified native conversation | Same protected thread | Durable DOXA conversation | Durable DOXA conversation |
| Model change | Idle session, verified against CLI settings | Next turn on same thread | Idle session, catalog choice | Idle session, catalog choice |
| Reasoning effort | Reported choices | Reported choices | Model-dependent | Model-dependent |
| Tool permissions | Provider controls plus DOXA peer/LORE review | On-request, auto, or full-access; peer/LORE review stays active | DOXA peer/LORE review | DOXA peer/LORE review |
| Compaction | LORE review before provider compaction | Trusted `PreCompact` review in private build | Reviewed summary checkpoint | Reviewed summary checkpoint |

Provider tool features depend on the installed CLI, model, and account. `/model`, `/effort`, and `/mode` expose only supported choices. Changes wait for the current turn and queue to finish. The picker can show separately sourced context, thinking, and price facts for an exact model ID; unknown fields stay unknown and facts do not make a model selectable. [Model fact provenance](plans/model-registry.md).

## Review and accounting

Codex `on-request` reviews protected commands, file changes, and permission profiles inline. `auto` retains its sandbox; `full-access` removes that sandbox. DOXA peer and LORE tools keep their own human review in every mode. DeepSeek and GLM have no provider permission mode; their peer and LORE calls are individually reviewed. Optional vendor workspace reads are off by default.

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

Linux has bounded live verification for all four engines. macOS builds and transport tests run in CI, but authenticated provider sessions need live checks. Protected Codex is Linux-only because its process ownership and compaction contract have no macOS equivalent. Windows is unsupported. See [platform verification](platform-verification.md) and the [provider verification record](live-provider-verification-2026-10-04.md).

## Where to go next

- [Rust guide](../rust/README.md) for installation and everyday controls.
- [Codex engine contract](../rust/doxa-engines/README.md) for protected provider details.
- [Session isolation](session-isolation.md) for native and Docker execution.
- [LORE integration](https://github.com/docwilde/LORE) for shared reviewed memory.
