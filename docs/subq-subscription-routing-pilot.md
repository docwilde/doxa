# SubQ Snap subscription routing pilot

On November 24, 2026, all **25 fixed-model trials** and all **four routed
executions** passed the offline grader. SubQ Snap withheld one of its five
routing decisions. The grader ran **128 checks** across the 29 repairs.

## Setup

The pilot used the same five development cases as the earlier
[routing pilot](plans/evaluation-pilot.md) and the same
[offline repair grader](representative-routing-benchmark.md). Each fixed arm
received the same candidate-visible inputs in a fresh workspace and had one
attempt to return a repair proposal. The grader evaluated the reviewed
proposals in network-disabled Docker. All five worker profiles requested high
effort through existing Claude Code, Codex, and Antigravity accounts.

The five-way selection ran in a benchmark harness using
`subq-snap-1.2` and its typed Choice endpoint. Each task exposed all five
candidate descriptions to Snap. The policy required at least 0.50 confidence
to dispatch and withheld execution below that cutoff. The Antigravity CLI ran
as standalone `agy` because DOXA beta.87’s native Router supports Claude and
Codex account execution. [Native Router scope](jev-router.md) ·
[Antigravity integration](antigravity.md)

## Results

| Arm | Worker model ID | Tasks passed | Median trial time |
| --- | --- | ---: | ---: |
| Claude Opus 5.5 | `claude-opus-5-5` | 5/5 | 8.63 s |
| Claude Sonnet 5.5 | `claude-sonnet-5-5` | 5/5 | 5.78 s |
| GPT‑6.2 Sol | `gpt-6.2-sol` | 5/5 | 15.79 s |
| GPT‑6.2 Astra | `gpt-6.2-astra` | 5/5 | 14.94 s |
| Gemini 4 Neon | `gemini-4-neon-high` | 5/5 | 65.64 s |
| SubQ Snap 1.2 routing | Selected per task | 4/5 | 13.03 s |

Trial time is wall-clock time through the CLI. The routed median includes
selection latency and covers the four trials that executed a worker.

| Task | Snap selection | Confidence | Outcome |
| --- | --- | ---: | --- |
| `easy-config` | Sol | 0.989 | Pass |
| `easy-expiry` | Sol | 0.968 | Pass |
| `intermediate-graph` | Sonnet | 0.554 | Pass |
| `intermediate-delegation` | Withheld | 0.451 | No worker dispatched |
| `difficult-budget` | Astra | 0.762 | Pass |

The five Snap decisions took a median **328 ms** and used 2,411 input tokens.
That is about **$0.000096** at the published rate. Execution time and token
usage come from the provider CLIs. Subscription charges were unverified.
Codex’s JSON output reports completion and usage. Its model evidence here is
the exact requested ID and the authenticated account catalog.
[SubQ pricing](https://docs.subq.ai/using-the-api/models/)

Sonnet was the fastest fixed arm on this sample. All five workers passed the
delegation task when dispatched directly. Pass rates were saturated across the
fixed arms, so these five cases measure the effect of the confidence policy
and the observed latency. Supervision accuracy, native fleet operation, and
holdout performance sit outside this run. The earlier Jev pilot used a
different worker roster.
