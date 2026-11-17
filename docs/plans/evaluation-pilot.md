# Routing and supervision development pilot

Status: **prepared; no paid trials run**. The owner authorized a pilot capped
at **$25** on 2026-11-16. Full benchmark runs require a separate quote. The frozen development settings
are in [`pilot.json`](../../scripts/routing-benchmark/pilot.json). Run
`python3 scripts/routing-benchmark/pilot_quote.py` from the repository root to
check the allocations and obtain configuration/corpus hashes without dispatch.

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

The offline admission ledger uses SQLite transactions and rounds each hold
upward to integer USD micros.
A full-size Pro hold is $0.156162 after rounding. Jev holds use its pinned
$0.042 per million input tokens and the production 24,576-byte limit plus
1,024 framing tokens. The total for 30 maximum-size decisions is $0.032280.
[Admission ledger](../../scripts/routing-benchmark/pilot_ledger.py)

Initialize one owner-private database with `PilotLedger.create(path)` for the
whole pilot. Use `PilotLedger.open(path)` for every worker or restart. The
ledger binds the plan and corpus hashes and refuses a second reservation for
the same slot. Make exactly one dispatch after a successful reservation.
Bind verified response usage to its request digest with `settle_usage`. A
missing response or unverified usage leaves the whole hold charged. An
out-of-bound verified observation also freezes new reservations. Use `summary`
for aggregate spend and usage without request identifiers or contents.

`reserve_fleet` reserves the entire $2.50 for a fleet slot. Before use, the
fleet launcher must enforce that cap across every API role and independently
verify the deployment configuration. Fleet holds stay charged in full. The
ledger has credential-free tests. Paid dispatch still requires integration of
these admissions into the routed endpoint, repair bridge and fleet launcher.

The fixture-only fleet check uses `fleet start --dry-run` with a pinned
DeepSeek API target in the coordinator and both workers. It leaves the Docker
roles offline and checks both outer ledger slots. Its $2.49 host plan assigns
$0.75 to each of the three API roles and $0.24 to the shared review allowance.
The native fleet launcher subtracts review before dividing the API allowance,
and verifies the per-role ceiling and Router configuration before it
submits a prompt. The review guard durably retains each review request's
reservation. The test runs no daemon, contacts no provider and uses a
disposable ledger. [Fleet fixture check](../../scripts/routing-benchmark/test_pilot_fleet.py)

The October 20 `live_pilot.py` case uses its own $0.90 ceiling, manually
accounts for prior holds and can migrate the coordinator to an online profile.
It has no linkage to the evaluation ledger. Before this pilot's fleet
collection, bind each actual launch to a successful outer hold and the same
run identity on resume. Verify the exact account and rate bounds for all
selected API roles and keep LORE provider calls, external MCP and other
spending outside those allowances disabled for the pilot. Confirm the
coordinator, workers and independent review use the intended durable
journals before dispatching development tasks.

## Prerequisites and deliverables

The routed endpoint, analysis script and reviewed repair bridge must pass
credential-free forwarding and refusal controls before paid dispatch. The
endpoint may replace only the selected model ID and frozen effort fields;
fixed arms send the identical candidate settings. Preserve conversation,
tools, token allowance and provider responses. Separate model failures,
budget refusals, incomplete runs and actual grader failures in reports.

The offline forwarding core in `doxa-router` checks both fixed profiles and
the Jev selection path. It preserves the JSON values of all other accepted
request fields, including raw message and tool payloads, and binds the exact
forwarded bytes and limits to a body-free receipt. Run
`cargo test -p doxa-router --test pilot_forward` for the fixture controls.
The live endpoint still needs credential handling, ledger admission and
provider transport connected to this core.

The repair bridge can use text-only requests for all three arms. Tool-bearing
requests must meet both candidates' requirements before Jev runs. DeepSeek's
thinking mode requires prior assistant reasoning content when tools are
supplied, so histories missing it are refused for Pro and the routed arm.
[DeepSeek thinking mode](https://api-docs.deepseek.com/guides/thinking_mode/)

The [offline repair bridge](../representative-routing-benchmark.md#deliver-a-reviewed-pilot-repair)
exports only candidate-visible files, previews a bounded source proposal and
delivers an approved revision to the grader. Run its credential-free controls
with the routing benchmark unit tests before connecting it to provider dispatch.

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
