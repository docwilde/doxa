# Native model facts registry

Status: **first slice implemented** in Rust. The provider catalog still decides
which models and effort levels a connected session may use. This registry adds
sourced facts to an exact `(engine, model)` key; it never grants availability.

## Contents

- [Current design](#current-design)
- [What is known](#what-is-known)
- [Where it appears](#where-it-appears)
- [Price review needed](#price-review-needed)
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

On 2026-10-09, official OpenAI API model pages supplied exact model windows for
[`gpt-6-astra`](https://developers.openai.com/api/docs/models/gpt-6-astra),
[`gpt-5.6-sol`](https://developers.openai.com/api/docs/models/gpt-5.6-sol),
[`gpt-5.6-terra`](https://developers.openai.com/api/docs/models/gpt-5.6-terra),
[`gpt-5.6-luna`](https://developers.openai.com/api/docs/models/gpt-5.6-luna),
[`gpt-5.5`](https://developers.openai.com/api/docs/models/gpt-5.5) (1,050,000
tokens each), and [`gpt-5.3-codex`](https://developers.openai.com/api/docs/models/gpt-5.3-codex)
(400,000 tokens). The first four GPT-5.x pages explicitly allow `none`
reasoning effort, so thinking is optional. OpenAI's
[reasoning guide](https://developers.openai.com/api/docs/guides/reasoning)
explicitly refuses `none` for `gpt-6-astra`, so thinking is mandatory. Each
context and thinking field cites its own page and check date.

Z.AI describes model context as “1M,” “200K,” or “128K” without a precise
integer token count, so those fields remain unknown. The `gpt-5.3-codex`
thinking off switch remains unknown. OpenAI API model specifications describe
the model, not the effective context allocation or effort choices in a
particular Codex session; the live provider catalog remains authoritative for
those controls. Runtime observations and effort labels do not establish an
unknown field. No benchmark score, speed tier, or inferred family capability
is recorded.

## Where it appears

The TUI model picker continues to show the live provider catalog. For its
selected row, it displays context and thinking as known or unknown, plus both
price fields with their source host and check date when known. Selection still
sends the exact catalog model ID. The registry does not change the fleet's
owner-approved price ceiling or its fail-closed accounting behavior.

## Price review needed

The 2026-09-30 conservative OpenAI price rows are no longer the current
Standard API rates shown on the model pages checked 2026-10-09. Values below
are fresh input / output USD per million tokens; the `>272K` column applies
to the **whole request** when input exceeds 272,000 tokens.

| Exact model ID | Stored rate | Current Standard API rate | `>272K` rate |
| --- | ---: | ---: | ---: |
| [`gpt-6-astra`](https://developers.openai.com/api/docs/models/gpt-6-astra) | 20 / 100 | 10 / 50 | 20 / 75 |
| [`gpt-5.6-sol`](https://developers.openai.com/api/docs/models/gpt-5.6-sol) | 8 / 40 | 4 / 20 | 8 / 30 |
| [`gpt-5.6-terra`](https://developers.openai.com/api/docs/models/gpt-5.6-terra) | 4 / 24 | 2 / 12 | 4 / 18 |
| [`gpt-5.6-luna`](https://developers.openai.com/api/docs/models/gpt-5.6-luna) | 0.4 / 2.4 | 0.2 / 1.2 | 0.4 / 1.8 |
| [`gpt-5.5`](https://developers.openai.com/api/docs/models/gpt-5.5) | 12.5 / 75 | 5 / 30 | 10 / 45 |
| [`gpt-5.3-codex`](https://developers.openai.com/api/docs/models/gpt-5.3-codex) | 3.5 / 28 | 1.75 / 14 | Not stated on its model page |

`BudgetHost` currently charges all prompt tokens at one stored rate and admits
another turn while recorded spend is below the ceiling. Every stored row
exceeds its displayed Standard rate; the five tiered rows also exceed their
documented long-context rates. This can exhaust the ceiling early. For
`gpt-5.5`, it instead makes accounting unknown and withholds
later turns when aggregate turn input reaches 272,000 tokens. The provider's
threshold is per request, which the aggregate turn does not establish. A
correct refresh needs per-request tier evidence, effective billing mode and
resume-safe price identity; replacing these pairs with short-context rates
alone would risk admitting turns that exceed the ceiling.

## Remaining work

- Continue filling unknown context and thinking fields only when primary
  provider sources state exact values for exact model IDs.
- Ingest trustworthy provider-supplied capability metadata where available,
  preserving its own source and observation time. Do not replace an unknown
  with a guessed family value.
- Consider an operator-facing refresh and stale-fact review flow before using
  the registry for automatic task routing. No quality or benchmark score is
  planned without a maintained, task-specific evaluation method.
- Review the tiered price rows above and their budget admission behavior before
  changing accounting rates.
