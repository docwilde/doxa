# Native model facts registry

Status: **first slice implemented** in Rust. The provider catalog still decides
which models and effort levels a connected session may use. This registry adds
sourced facts to an exact `(engine, model)` key; it never grants availability.

## Contents

- [Current design](#current-design)
- [What is known](#what-is-known)
- [Where it appears](#where-it-appears)
- [Remaining work](#remaining-work)

## Current design

[`doxa-engines/src/model_registry.rs`](../../rust/doxa-engines/src/model_registry.rs)
returns one fact for each of context window, fresh input price, output price,
and thinking behavior. Each fact carries its own value and provenance. A field
without evidence is `unknown`, with no numeric or behavioral default. Known
static facts include their source URL and the date checked. Lookup requires an
exact engine and model ID; aliases do not inherit facts.

The connected engine's `list_models` result remains the authority for picker
availability and effort choices. The registry is advisory, and neither model
selection nor the provider's session controls depend on it.

The daemon's existing native price rows now live in this single registry.
`BudgetHost` consumes a price pair only if both fields share a source and date
and contain finite, nonnegative values. Its effective-model, complete-usage,
and durable admission checks remain in place. An unknown price still refuses
native priced-budget admission; it never means zero cost. The session journal's
historical `price_read_on` identity is unchanged for existing resumes.

## What is known

The native budget table documents exact price rows for selected Codex,
DeepSeek and GLM models, checked on 2026-09-30. They are fresh input and output
USD per million tokens; cached-input discounts are omitted for conservative
budget accounting.

Additional fields were checked on 2026-10-08. DeepSeek's [model-list example](https://api-docs.deepseek.com/api/list-models/)
gives a 1,048,576-token context for exact IDs `deepseek-flash` and
`deepseek-v4-pro`; its [model page](https://api-docs.deepseek.com/quick_start/pricing/)
says both permit thinking on or off. Z.AI documents mandatory thinking for
[`glm-5.3`](https://docs.z.ai/guides/llm/glm-5.3) and
[`glm-5.3-flash`/`glm-5.3-flashx`](https://docs.z.ai/guides/vlm/glm-5.3-flash).
Its [thinking guide](https://docs.z.ai/guides/capabilities/thinking-mode)
documents an off switch for `glm-5.2`, `glm-5.1`, `glm-5`, and `glm-4.7`.
Each populated field retains its own source and check date.

Z.AI describes model context as “1M,” “200K,” or “128K” without a precise
integer token count, so those fields remain unknown. Codex context and thinking
facts also remain unknown. Runtime observations and effort labels do not
establish maximum context or thinking behavior. No benchmark score, speed
tier, or inferred family capability is recorded.

## Where it appears

The TUI model picker continues to show the live provider catalog. For its
selected row, it displays context and thinking as known or unknown, plus both
price fields with their source host and check date when known. Selection still
sends the exact catalog model ID. The registry does not change the fleet's
owner-approved price ceiling or its fail-closed accounting behavior.

## Remaining work

- Continue filling unknown context and thinking fields only when primary
  provider sources state exact values for exact model IDs.
- Ingest trustworthy provider-supplied capability metadata where available,
  preserving its own source and observation time. Do not replace an unknown
  with a guessed family value.
- Consider an operator-facing refresh and stale-fact review flow before using
  the registry for automatic task routing. No quality or benchmark score is
  planned without a maintained, task-specific evaluation method.
