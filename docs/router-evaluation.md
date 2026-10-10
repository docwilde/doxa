# Jev router evaluation

`doxa-router` is an opt-in, bounded API router. Rust filters owner-configured
DeepSeek/GLM targets before Jev receives a closed Choice. Each target has an
explicit ID, provider, model, effort and description. `auto` is reserved for
routing control and cannot be a target ID. The caller supplies only IDs whose
credentials, account catalog, transport and provider/tool/history requirements
it has checked. Jev cannot introduce a provider, model, effort, permission or
tool. A fallback must remain eligible; otherwise preparation fails.

Candidate descriptions are operator assumptions, not measured quality facts.
Context/output fields are narrow operator request caps; prices are declared
bounds. The host must compare them with sourced registry prices and applicable
provider limits/catalog evidence before priced worker admission. Unknown
capabilities, context or price must not become zero, available or unlimited.
See [model registry](plans/model-registry.md) for the remaining source/evidence
workflow. Jev does not execute worker tools. Current API worker tools do not
promise arbitrary shell commands or workspace editing.

## Wire and limits

The client pins `jev-1.13.0` at
`https://api.typesafe.ai/v1/systemone`, authenticated with `TYPESAFE_API_KEY`.
It sends one `questions.target` Choice with explicit eligible criteria and a
bounded, host-scrubbed task summary in state. It accepts only the matching
closed answer, full probability set summing to one, eligible maximum-probability
winner, consistent finite confidence, exact model provenance and integer token
usage. Duplicate JSON keys, null/missing fields, aliases, extra fields and
invented targets are refused. [TypeSafe API reference](https://docs.typesafe.ai/api)

Choice confidence measures distribution concentration. It is not the
probability that a selected worker will complete the task. The implementation
checks TypeSafe's published formula against the probabilities before applying
the owner's threshold. Thresholds need task-specific evaluation.
[Confidence](https://docs.typesafe.ai/confidence)

Rust handles eligibility and integer cost arithmetic. Jev's pinned published
rate is $0.042 per million input tokens; output is free. The evaluator rounds
usage-based costs upward to USD micros per call; these are estimates, not
invoices. Context and service rate limits remain provider-controlled.
[Models and pricing](https://docs.typesafe.ai/models)

Config is an explicit absolute path to an owner-owned private regular JSON
file (at most 32 KiB); symlinks, shared permissions, duplicate targets, unknown
fields and zero/unknown caps or prices are refused. Config permits 2–16 targets,
1–10,000 calls, 1–1,000,000 USD micros spend, 512–24,576 request bytes and a
100–12,000 ms deadline. One USD is 1,000,000 micros. Known credential strings in
config or summary are withheld, and the caller must apply the normal LORE
scrub to other sensitive text before routing.

The host prepares and reserves, persists the reservation, then calls Jev once.
No automatic retries occur. Failed or cancelled attempted calls retain their
reservations. Reservations use request bytes plus a framing allowance as a
conservative token estimate; usage above that estimate makes accounting
unknown and prevents further calls. Unknown response usage also prevents new
calls. A durable restart with an unsettled reservation cannot silently replay
it. `Ledger::held_usd_micros()` combines the cumulative reservation and actual
estimate without charging the same settled cost twice.

Low confidence or service/schema failure proposes the configured eligible
fallback. The host executes it only when the router made no HTTP call or has
verified usage and known accounting, and worker eligibility and aggregate
budget still permit admission. An attempted HTTP call without verified usage
pauses worker execution for reconciliation; an outage cannot silently spend a
worker fallback turn. The proposal grants no wider authority. Cancellation
returns `cancelled` and the host must stop, never execute the fallback. In production, the endpoint is
fixed HTTPS with redirects disabled, bounded response bytes and a deadline.
The `test-transport` feature exposes loopback-only HTTP for deterministic tests.

TypeSafe documents limitations with numeric precision, indirection, irrelevant
state, adversarial text and option ordering. Keep arithmetic in Rust, send only
relevant data and evaluate candidate rubrics/order against the actual workload.
[Jev 1.13 limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13)

## Offline recorded evaluation

The workspace includes `doxa-router-eval`. It starts no worker and supports
private JSONL cases with separate development and holdout groups:

```sh
cargo build --locked --offline -p doxa-router --bin doxa-router-eval
/path/to/doxa-router-eval offline /absolute/private/config.json \
  /absolute/private/recorded-cases.jsonl
```

Each closed case supplies `version: 1`, unique `id`, `group_id`,
`split: development|holdout`, `origin: synthetic|real`, `label_source`,
`consented`, `scrubbed`, `input`, `expected_candidate_id` and `recording`.
Input contains `summary`, `estimated_input_tokens`, `max_output_tokens`,
`requires_tools` and host-eligible `allowed_candidate_ids`. A recording binds
`config_sha256`, `criteria_sha256`, `request_sha256`, a strict API `response`
or typed `transport_error`, `latency_ms`, `attempted` and `rejected_usage`.
The latter retains verified pinned-model usage for a rejected answer without
storing untrusted response prose. Consent/scrubbing/human labels remain
operator attestations; a file cannot prove their truth.

Synthetic cases require `label_source: machine`; real cases require `human`.
Origins cannot mix, and one group cannot cross splits. Model, criteria,
configuration and input identities must match. Both splits must be present.
Raw sensitive transcripts must not be passed to live evaluation. Local offline
real evidence requires the stated ownership/consent/scrubbing attestations and
independently labeled cases. The evaluator does not turn them into a quality
acceptance gate.

The report shows per-split confusion matrices, agreement/error rates, fallback
and transport/schema failure counts, p50/p95/max latency, per-call reservation
and usage, and exact configuration/criteria/input/response hashes. It simulates
configured cumulative router bounds. Worker execution cost and total cost stay
`null` because no worker executes. `real_quality_validated` remains `false`.
Machine-authored synthetic labels are never described as human holdout or
real routing-quality validation.

To test replay plumbing, copy `rust/doxa-router/fixtures/config.example.json`
and `synthetic-cases.jsonl` to an owner directory and chmod both `0600`. Run:

```sh
/path/to/doxa-router-eval fixture /absolute/private/config.json \
  /absolute/private/synthetic-cases.jsonl > /absolute/private/fixtures.jsonl
chmod 600 /absolute/private/fixtures.jsonl
/path/to/doxa-router-eval offline /absolute/private/config.json \
  /absolute/private/fixtures.jsonl
```

`fixture` creates artificial perfect responses with invented latency and usage
for plumbing tests. These are not measurements. The example descriptions,
request caps and prices must be reviewed against host evidence before any
worker execution; this evaluator makes no worker-provider calls.

## Explicit synthetic live smoke

Live evaluation requires all-machine-authored synthetic cases, no prior
recordings, at most 20 cases, and a new private journal path in an owner
directory without shared writes. It applies both config bounds and a hard
$0.01 cumulative reservation ceiling. There is no default live call:

```sh
/path/to/doxa-router-eval live /absolute/private/config.json \
  /absolute/private/synthetic-cases.jsonl --live-synthetic \
  --journal /absolute/private/new-router-journal.jsonl
```

The journal is created exclusively with mode `0600`. Each reservation is
synced before HTTP; each bounded result is synced afterward. SIGINT/SIGTERM
cancel an in-flight request and leave its reservation inspectable. Preserve
this journal and the private JSON report. Inspect outstanding attempts before
resuming; do not repeat a call whose billing is unknown. A missing key records
`missing_credential`, zero calls and zero router cost, and returns a nonzero
status. It does not block offline implementation or fixture testing.

The report retains replayable synthetic recordings. They contain only valid
closed response fields or typed failures; raw invalid server bodies and API
keys are not retained. This smoke can demonstrate typed transport, local
bounds and observed tiny-sample behavior. It cannot establish real-task routing
quality, worker success, comparative worker costs or broad latency guarantees.
No real user transcripts, live fleet or paid worker-provider evaluation are
part of this procedure.
