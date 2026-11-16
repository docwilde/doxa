# Routing and supervision development pilot

Status: **prepared; no paid trials run**. The owner authorized a pilot capped
at **$25** on 2026-11-16. Full benchmark runs require a separate quote.

The pilot checks request forwarding, budget accounting, repair delivery,
offline grading and collection formats. Its small development sample cannot
establish routing quality, supervision accuracy or a leaderboard score.

## Frozen scope

Use five development tasks from the existing
[repair corpus](../representative-routing-benchmark.md): `easy-config`,
`easy-expiry`, `intermediate-graph`, `intermediate-delegation` and
`difficult-budget`. Preserve the corpus family splits. Do not inspect or run
its holdout tasks or Terminal-Bench tasks during this pilot.

Run one trial per task under three arms: fixed `deepseek-flash` with thinking
disabled, fixed `deepseek-v4-pro` with high thinking, and Jev `jev-1.13.0`
choosing between the same frozen candidates on every call. Fifteen trials
supply development observations only. Every arm uses the same editing bridge,
public tests, per-request output allowance and six-call maximum per trial.
The evaluator alone receives the private grading cases.

Each request admits at most 20,000 input tokens under the production
conservative bound and 32,768 generated tokens, including reasoning. The
worker must stop rather than truncate or silently enlarge a frozen allowance.
No model-hosted search, file parsing, images, native CLI workers, automatic
provider fallback or extra paid retry is part of this pilot.

## Dollar ceilings

| Allocation | Ceiling | Admission |
| --- | ---: | --- |
| Repair model calls | $15 | At most 90 calls across all arms; reserve the exact candidate's worst-case price before each dispatch |
| Jev decisions | $2 | At most 30 decisions; reserve the decision service's input bound before each dispatch |
| Fleet development collection | $5 | At most two bounded API fleets, each capped at $2.50, including its coordinator, workers and reviewers |
| Undispatched reserve | $3 | No automatic transfer to another allocation |
| **Total** | **$25** | One shared durable ledger; refuse when any allocation cannot cover the next hold |

DeepSeek's published peak/cache-miss rates were checked on 2026-11-16:
Flash input/output costs $0.30/$1.20 per million tokens; Pro costs
$1.32/$3.96. At the stated caps one Pro call reserves $0.15616128. Charging
all 90 repair calls at that rate gives $14.05451520, below the $15 repair
allocation. This is an admission bound, not a claim about actual billed cost.
[Provider pricing](https://api-docs.deepseek.com/quick_start/pricing/)

Keep failed or ambiguous dispatch reservations charged until settlement is
known. Restart must retain the same ledger and remaining ceilings. A response
without trustworthy usage must not create a refund. Recheck exact model
identity and price bounds before execution; a changed rate or unsupported
account stops admission. Credentials flow through the existing provider
process, and never enter journals, cards or committed fixtures.

## Prerequisites and deliverables

The routed endpoint, analysis script and reviewed repair bridge must pass
credential-free forwarding and refusal controls before paid dispatch. The
endpoint may replace only the selected model ID and frozen effort fields;
fixed arms send the identical candidate settings. Preserve conversation,
tools, token allowance and provider responses. Separate model failures,
budget refusals, incomplete runs and actual grader failures in reports.

Use an existing pinned local rootless Docker image for grading, with the
corpus's network and resource limits. Do not pull an image or weaken limits
silently. The provider sees only the candidate workspace; the host never
executes candidate code.

Prepare blinded checkpoint cards and the label guide before fleet collection.
Keep development collections and independent human labels separate. Do not
invent labels or count synthetic fixtures toward acceptance. The checkpoint
evaluator must refuse synthetic/model-labelled holdouts and group leakage.

Write a private per-call ledger and a body-free public summary of completed
trials, charged and unresolved reservations, measured tokens, timings,
functional outcomes and infrastructure failures. Stop at the ceilings even if
some trials remain incomplete. Use those measurements to quote full routing
stages and fleet collection separately; do not extrapolate confidence from
this pilot's success rate.
