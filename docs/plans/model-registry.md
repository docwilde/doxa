# Native model facts registry

Status: **first slice implemented** in Rust. The provider catalog still decides
which models and effort levels a connected session may use. This registry adds
sourced facts to an exact `(engine, model)` key; it never grants availability.
An explicit [account availability check](#account-availability-observations)
records whether the current Codex or DeepSeek account lists an exact ID, as
dated evidence only.

## Contents

- [Current design](#current-design)
- [What is known](#what-is-known)
- [Where it appears](#where-it-appears)
- [Account availability observations](#account-availability-observations)
- [Token-budget bounds](#token-budget-bounds)
- [Remaining work](#remaining-work)

## Current design

[`doxa-engines/src/model_registry.rs`](../../rust/doxa-engines/src/model_registry.rs)
returns facts for context, Standard API fresh-input and output prices, native
budget input/output bounds, and thinking behavior. Each fact carries its own
value and provenance. A field without evidence is `unknown`, with no default. Known
static facts include their source URL and the date checked. Lookup requires an
exact engine and model ID; aliases do not inherit facts.

The connected engine's `list_models` result remains the authority for picker
availability and effort choices. The registry is advisory, and neither model
selection nor the provider's session controls depend on it.

The daemon's native price rows live in this single registry. `BudgetHost`
consumes a separate budget-bound pair only if both fields share a source and date
and contain finite, nonnegative values. Its effective-model, complete-usage,
and durable admission checks remain in place. An unknown price still refuses
native priced-budget admission; it never means zero cost. The session journal's
historical journal identity is unchanged for non-Codex resumes. Changed Codex
bounds invalidate old journals rather than resuming with a different rate.

## What is known

The native budget table documents exact price rows for selected Codex,
DeepSeek and GLM models. OpenAI Standard API prices were checked on 2026-10-09;
the DeepSeek and GLM price rows were rechecked against their primary pricing
pages on 2026-10-10; their rates were unchanged. Values are USD per million
tokens. The picker labels the OpenAI prices as Standard API rates; they are not
the rates used for budget admission.

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

For an OpenAI API reviewer, an authenticated
[`GET /v1/models`](https://developers.openai.com/api/reference/resources/models/methods/list)
with the same API key can check whether `gpt-6-astra` is currently listed without
making a generation call. That read-only result does not establish a successful
Responses request, its billed service tier, latency, or output quality. This
audit made no account call. The model page lists Free as unsupported and
publishes Build, Launch and Grow rate limits; an organization's actual limits
still need its own account view.

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

`doxa model-facts ENGINE MODEL [--review-before YYYY-MM-DD]` prints every
field's value or `unknown`, full primary-source URL, and check date for one
exact registry key. With an operator-chosen cutoff, it marks facts checked
before that date for review. The command is read-only: the date is a queue
filter, not an expiry or live verification. Refreshing a fact requires an
operator to inspect the exact provider source and change the registry in a
reviewed code commit. The command cannot grant a model, effort level, or
priced-budget admission.

`doxa model-facts codex gpt-6-astra --check-catalog` explicitly reads OpenAI's
Models API with `OPENAI_API_KEY`. The fixed `GET /v1/models` request has no
generation payload, follows no redirects or environment proxy, and has a five
second total deadline and a 1 MiB response limit. It prints only `listed`,
`unlisted`, or `unknown`; missing credentials, refused requests, malformed or
oversized responses, and timeouts are `unknown`. An unlisted result means the
exact ID was absent from that successful catalog response. A listed result
does not prove a Responses call will succeed, its billed tier, latency, or
review quality. The flag is accepted only for the `codex` engine and never runs
implicitly during fact review or model selection.

## Account availability observations

`doxa model-facts ENGINE MODEL --check-availability` explicitly asks whether
the engine's current account lists that exact model ID. It prints `listed`,
`unlisted` or `unknown` with a fixed reason, the UTC observation time, the
listing source, and the latency of the listing call itself. The result is
printed and dropped. No cache exists for a later path to mistake for an
availability grant, and model selection, effort choices and budget admission
never read it. Provider error text and credentials are never printed.

| Engine | Listing source | Result without a usable listing |
| --- | --- | --- |
| Codex | Provider `account/read`, then `model/list` with `includeHidden: false`, using the executable and project directory of a new session. DOXA reads up to 10 pages or 1,000 visible rows; one malformed row fails the listing. | `unknown` when unauthenticated, refused, malformed, timed out (20 s including start-up), or absent from a listing cut off at DOXA's bound. |
| DeepSeek | [`GET https://api.deepseek.com/models`](https://api-docs.deepseek.com/api/list-models/) with the key a DeepSeek session resolves. It uses the same fixed-request limits as `--check-catalog`. | `unknown` for a missing key, refusal, redirect, malformed or oversized body, duplicate row, or 5 s timeout. |
| GLM | None. Z.AI's [API reference index](https://docs.z.ai/llms.txt) documents no model-listing endpoint (checked 2026-11-14). | Always `unknown`; no request is made. |
| Claude | None. Claude Code describes [`supportedModels()`](https://code.claude.com/docs/en/agent-sdk/typescript) only as available models with display info, not as an account-scoped list of exact IDs (checked 2026-11-14). | Always `unknown`; no request is made. |

The GLM picker's existing catalog request to `/api/paas/v4/models` is
undocumented, so it is not availability evidence. Anthropic's Models API lists models for an API
key, which need not be the Claude CLI login. An `unlisted` result means only
that a complete, successful listing omitted the exact ID. Hidden Codex rows
do not count as listed. No family, prefix or alias match is made.

Codex can answer `model/list` from its own `models_cache.json`. The account
state can therefore predate the observation time, and the latency can be a
local reply rather than a network round trip. A listing does not prove that a
request will succeed, its quality, or its inference latency.

Neither listing states a billed tier, so billed tier stays `unknown`. Codex
0.167 `model/list` rows can state a `defaultServiceTier`. When every row for the
exact ID agrees, the report names it as a catalog default, not the billed tier
of any request. DeepSeek documents no tier field.

Fixture app-servers and HTTP fixtures cover listed, unlisted, unauthenticated,
refused, malformed, truncated and timed-out listings. They also confirm that no
thread or turn starts. A UI negative control keeps a listed-only model out of
the picker, its effort choices and priced-budget admission. A CLI test
confirms that an observation writes no state beyond the credential store's
existing empty lock file.

On 2026-11-15 at 04:26 UTC, a live check used the stock Codex 0.167.0 CLI and
an existing login, because no protected provider build was installed. It
reported registry models as both listed and unlisted without starting a thread.
The first listing took 694 ms; later ones took 5–9 ms from the provider cache.
The protected 0.165.0 provider build was not exercised, and its listing can
differ. No DeepSeek credential was present, so DeepSeek used fixtures only.

## Token-budget bounds

OpenAI's [pricing table](https://developers.openai.com/api/docs/pricing)
and [Fast](https://developers.openai.com/api/docs/guides/fast-mode) and
[Ultrafast](https://developers.openai.com/api/docs/guides/ultrafast-mode) guides
were checked on 2026-10-09. For tiered models, each budget-bound input field
uses the highest documented long-context cache-write rate; each output field
uses the highest long-context output rate. A 10% regional or FedRAMP uplift is
included where relevant. GPT-5.3-Codex uses its specialized Fast rate plus
FedRAMP uplift, which has no model release-date cutoff.
The bound applies to **all** input/output tokens in each Codex turn because
the turn reports neither individual request sizes nor service tiers. This
overestimates short requests and cache reads but does not miss a long-context
token premium for admitted models.

`status.billing.budget.price_evidence` now reports
`static_upper_bound_only`, the source and checked date, the input/output
bounds, and `unknown` for billing tier and provider charge. The checked date is
the registry review date, not a live price verification or an expiry promise.
The native Codex turn path supplies model-consistent token totals, but no
per-request billed tier, cache-write split, or provider charge. The
[Responses API](https://developers.openai.com/api/reference/resources/responses/methods/create)
can report the tier on direct API responses when `service_tier` is set; DOXA's
Codex app-server turn event does not carry that response field. A stray tier or
cost hint in a turn event cannot reduce the bound. If the exact model has no
complete documented upper bound, priced-budget admission is refused. An
operator must refresh a dated registry fact against its primary source before
changing it; no calendar age alone proves a current price or an exact billed
tier.

| Exact model ID | Standard API input / output | Budget bound input / output | Basis |
| --- | ---: | ---: | --- |
| [`gpt-6-astra`](https://developers.openai.com/api/docs/models/gpt-6-astra) | 10 / 50 | 165 / 495 | Ultrafast long cache write 150 / output 450, ×1.10 |
| [`gpt-5.6-sol`](https://developers.openai.com/api/docs/models/gpt-5.6-sol) | 4 / 20 | Unknown | Preview Ultrafast price not published |
| [`gpt-5.6-terra`](https://developers.openai.com/api/docs/models/gpt-5.6-terra) | 2 / 12 | 11 / 39.6 | Fast long cache write 10 / output 36, ×1.10 |
| [`gpt-5.6-luna`](https://developers.openai.com/api/docs/models/gpt-5.6-luna) | 0.2 / 1.2 | 1.1 / 3.96 | Fast long cache write 1 / output 3.6, ×1.10 |
| [`gpt-5.5`](https://developers.openai.com/api/docs/models/gpt-5.5) | 5 / 30 | Unknown | Fast long-context price not established |
| [`gpt-5.3-codex`](https://developers.openai.com/api/docs/models/gpt-5.3-codex) | 1.75 / 14 | 3.85 / 30.8 | Specialized Fast rate 3.5 / 28, ×1.10 FedRAMP; no long tier published |

Unknown bounds refuse native priced-budget admission at session start.
The [prompt-caching guide](https://developers.openai.com/api/docs/guides/prompt-caching)
confirms cache writes can exceed fresh input rates for GPT-5.6 and later.
These bounds estimate **model token charges** only: separate API tool fees,
contract-specific pricing, and Codex subscription credits are outside this
counter. The host still requires complete, model-consistent usage and marks
spend unknown after an unaccountable turn. A single admitted turn can exceed
the ceiling before its usage arrives; the next turn is withheld. A provider-reported bill and
per-request tier would allow tighter accounting in a later slice.

## Remaining work

- Router candidate review follows the same exact-model evidence rules: inspect
  the primary price page, refresh its registry row in a reviewed change, then
  compare the candidate's declared rates with those bounds. Provider catalog
  availability is checked separately. A configured request cap limits payloads;
  it does not establish an unknown provider context window or model quality.
- Continue filling unknown context and thinking fields only when primary
  provider sources state exact values for exact model IDs.
- Ingest trustworthy provider-supplied capability metadata where available,
  preserving its own source and observation time. Do not replace an unknown
  with a guessed family value.
- An operator-selected stale-fact review report exists, but it does not fetch
  provider pages, update facts, or apply an automatic expiry rule. Router
  candidate admission uses the manual primary-source review workflow above;
  automatic source retrieval and refresh remain open. The budget status still
  exposes its review date and missing live billing
  evidence. No quality or benchmark score is planned without a maintained,
  task-specific evaluation method.
- Capture provider billing mode and per-request tier when available so a budget
  can use an exact billed rate rather than the documented upper token bound.
- Account availability is observed only on explicit request, for Codex and
  DeepSeek. GLM and Claude remain `unknown` until their providers document an
  account-scoped listing. The protected provider's `model/list` has fixture
  coverage only; a live check on that Linux build remains open. Inference
  latency, billed tier and provider charge remain unknown, because the
  observation measures only its own listing call.
