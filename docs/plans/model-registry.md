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

The native budget table already documented exact price rows for selected Codex,
DeepSeek and GLM models, checked against the linked provider pages on
2026-09-30. Those are the only populated registry facts today. They are fresh
input and output USD per million tokens; cached input discounts are not used
for conservative budget accounting. The code links each row to the provider
pricing page.

Context windows and whether thinking is unsupported, optional, or mandatory
remain unknown for every row. Runtime context observations do not establish a
model's maximum window. Effort levels do not establish thinking behavior.
No benchmark score, speed tier, or inferred family capability is recorded.

## Where it appears

The TUI model picker continues to show the live provider catalog. For its
selected row, it displays context and thinking as known or unknown, plus both
price fields with their source host and check date when known. Selection still
sends the exact catalog model ID. The registry does not change the fleet's
owner-approved price ceiling or its fail-closed accounting behavior.

## Remaining work

- Add context and thinking facts only after checking primary provider sources
  against exact model IDs. Record a date for each field independently.
- Ingest trustworthy provider-supplied capability metadata where available,
  preserving its own source and observation time. Do not replace an unknown
  with a guessed family value.
- Consider an operator-facing refresh and stale-fact review flow before using
  the registry for automatic task routing. No quality or benchmark score is
  planned without a maintained, task-specific evaluation method.
