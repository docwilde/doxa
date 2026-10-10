# Jev turn router

The opt-in `router` engine selects a configured DeepSeek or GLM API target at
the start of a turn. It keeps one durable DOXA conversation while recording the
effective target, provider, model and effort for each turn. It does not launch
Claude or Codex CLIs and does not provide Bash or workspace writes.

Candidate descriptions express the operator's routing policy. They are not
measured quality claims. Jev Choice confidence describes its returned choice
distribution; it is not a probability that the worker will complete the task.

## Start and control a session

Use an absolute path to a reviewed, owner-owned JSON file with private file
permissions, such as `0600`. The config must be a regular file, not a symlink or
hard link, and at most 32 KiB. Keep credentials out of it.

```sh
doxa new --engine router --router-config /home/you/.doxa/router-candidates.json --isolation native
doxa new --engine router --router-config /home/you/.doxa/router-candidates.json --model ds-chat --isolation native
doxa doctor --engine router --router-config /home/you/.doxa/router-candidates.json
```

`DOXA_ROUTER_CONFIG` or the `router_config` setting can explicitly supply the
path. DOXA does not invent a default config, candidate list, or target. Selecting
Router in the new-session form requires that setting; the form rotates through
Auto and configured target IDs. Global `DOXA_MODEL` and `DOXA_EFFORT` do not
select router targets. A stored `[models].router` preference applies to new CLI
launches and must name `auto` or an exact target ID.

- `/model auto` permits turn-boundary routing.
- `/model ds-chat` pins that exact configured target and skips Jev selection.
- Changes require an idle session. Effort is configured per target; `/effort`
  is unavailable on router sessions.
- `/mode auto` is a permission control for engines that support it. It does not
  enable routing. Router has no provider permission-mode picker; DOXA peer and
  LORE tool reviews remain active.

The model control continues to show Auto or the pinned target. A separate route
status shows the effective provider/model/effort. Its detail view and transcript
note show the bounded fallback reason, selection latency and routing cost
estimate. They do not show credentials, full router input, criteria text or raw
provider errors. Doctor reads local configuration and credential presence; it
does not verify a live catalog or send a Jev/worker request.

## Candidate configuration

This is an illustrative template, not an evaluated recommendation. Its worker
rate bounds match the exact-model registry rows checked on 2026-10-10: DeepSeek
$0.30 input/$1.20 output and GLM $0.15 input/$0.50 output per million tokens.
They are dated bounds, not account availability or billed-tier evidence.
Review exact model IDs, context/output caps, tool/effort support and sourced
price bounds for your account before using it. [Model facts](plans/model-registry.md)
have per-field dates; estimates are not provider invoices.

The `131072` context cap below is an operator-selected conservative bound for
wire bytes and token reservations, not a verified GLM model context window.
It leaves room for the host's fixed routing/tool/context overhead; a smaller
cap can make every target ineligible. Unknown provider windows remain unknown.

```json
{
  "version": 1,
  "jev_model": "jev-1.13.0",
  "criteria_version": "operator-policy-v1",
  "fallback_id": "glm-chat",
  "confidence_threshold": 0.5,
  "max_calls": 40,
  "max_spend_usd_micros": 10000,
  "max_input_bytes": 4096,
  "deadline_ms": 1000,
  "candidates": [
    {
      "id": "ds-chat",
      "provider": "deepseek",
      "model": "deepseek-flash",
      "effort": "none",
      "description": "Operator policy: use for ordinary API chat when eligible.",
      "context_tokens": 131072,
      "max_output_tokens": 1024,
      "supports_tools": true,
      "input_usd_micros_per_million": 300000,
      "output_usd_micros_per_million": 1200000
    },
    {
      "id": "glm-chat",
      "provider": "glm",
      "model": "glm-5.3-flash",
      "effort": "high",
      "description": "Operator policy: default fallback API chat target.",
      "context_tokens": 131072,
      "max_output_tokens": 1024,
      "supports_tools": true,
      "input_usd_micros_per_million": 150000,
      "output_usd_micros_per_million": 500000
    }
  ]
}
```

Config limits: 2–16 candidates; unique ASCII letters/digits/`-`/`_` IDs up to
48 characters; descriptions up to 1200 bytes; positive context/output caps and
price bounds; 1–10,000 routing calls; 1–1,000,000 USD micros of routing spend;
512–24,576 bytes of router input; 100–12,000 ms routing deadline. One dollar is
1,000,000 USD micros, so `10000` is $0.01. The fallback must name a configured
candidate. Unknown caps or prices cannot be entered as zero.

Before each turn the host checks credentials, account catalog, model/effort,
tools, retained history, context/output caps and aggregate allowance. Configured
targets are policy candidates, not a claim that every account can use them.
The fallback also has to remain eligible. An ineligible pin or an empty eligible
set refuses the turn; it does not switch to another engine. DeepSeek thinking
targets can be ineligible for retained assistant/tool history.

## Credentials, failure and accounting

Jev uses `TYPESAFE_API_KEY`. Workers use the existing DeepSeek/GLM credential
paths (`DEEPSEEK_API_KEY` and `ZAI_API_KEY`, or the private DOXA `/setup` store).
Do not put keys in the config, criteria, descriptions, command arguments or
evaluation files. Auto sends a scrubbed, bounded routing view and candidate
policy to Jev; normal worker calls still send the worker conversation to the
selected API provider.

When no Jev HTTP call is attempted, a missing Jev credential, exhausted routing
allowance or a single eligible target can use the eligible fallback. Pinning
skips Jev. A verified low-confidence result can also fall back with known usage.
An attempted HTTP call without complete verifiable usage leaves accounting
unknown: worker execution and subsequent turns are withheld. Timeouts, invalid
answers and outages must not be assumed to be free or safe to retry.

The session journal records reservations before calls and retains them on
failure or cancellation. A session budget combines routing and worker holds;
the fixed-price `BudgetHost` wrapper is not used for router sessions. Worker
holds conservatively cover up to 25 bounded requests and the aggregate output
allowance. Displayed aggregate cost combines reported Jev usage and worker
estimates at the checked upper rates; retained reservations are shown separately.
Token-based estimates and conservative reservations are not billed tier or invoice facts.
An interrupted/incomplete journal pauses further work rather than replaying an
uncertain turn.

## Resume and first-slice limits

```sh
doxa new --engine router --router-config /home/you/.doxa/router-candidates.json --resume FULL_SESSION_ID --isolation native
```

Resume requires the same reviewed config hash and preserves Auto or the pinned
target, canonical messages and aggregate allowance. A fresh empty router
conversation can also resume; it does not require a prior paid turn. Transcripts retain engine
`router` and per-turn routing metadata; the replay envelope uses the stable
`router-conversation-v1` model identity. The allowance/selection journal lives
at `$DOXA_HOME/router/ID.router.json`.

If a session ceiling was configured, resume must preserve that same ceiling;
changing the allowance or config does not reset recorded spend. Keep the
original `session_budget_usd`/`DOXA_SESSION_BUDGET_USD` setting when resuming.

The history picker explains that explicit config is required for resume.
Existing live router sessions can be attached normally. Automatic launch from
history, `/clear`, `/cd`, child spawning and fleet config inheritance are
unavailable in this slice. Docker isolation, isolation migration, CLI handoffs,
and router compaction are also unavailable. Start a separate explicit session
when those missing inheritance paths would otherwise be needed.

## Evaluation and evidence

`doxa-router-eval` evaluates labels without executing worker providers. During
development use `cargo run -p doxa-router --bin doxa-router-eval -- …`.

```sh
doxa-router-eval fixture /absolute/config.json /absolute/synthetic-cases.jsonl
doxa-router-eval offline /absolute/config.json /absolute/recorded-cases.jsonl
# Separate, explicit opt-in: paid Jev calls on predeclared synthetic rows only.
doxa-router-eval live /absolute/config.json /absolute/synthetic-cases.jsonl --live-synthetic --journal /absolute/new-private-journal.jsonl
```

Inputs must be private owner regular files. Case JSONL is bounded to 2 MiB and
1000 rows. Each row declares `version`, `id`, `group_id`, development or holdout
`split`, `origin`, `label_source`, `consented`, `scrubbed`, `input`,
`expected_candidate_id`, and optional `recording`. Routing input contains
`summary`, `estimated_input_tokens`, `max_output_tokens`, `requires_tools` and
`allowed_candidate_ids`. Development and holdout groups cannot overlap. Real
cases require operator-attested consent/scrubbing and human labels; synthetic
cases use machine labels and are reported separately.

Offline responses bind exact config, criteria and request hashes. `fixture`
generates artificial recordings from synthetic labels for regression tests;
their perfect agreement is not real routing quality. Live smoke accepts at
most 20 unrecorded synthetic rows and at most $0.01 of cumulative routing
reservations, subject to tighter config limits. Missing credentials or incomplete
calls yield an incomplete report and nonzero exit. The report separates
development/holdout confusion, errors, fallbacks, failures, latency and Jev cost.
Worker quality, worker execution cost and total execution cost remain unmeasured.

Live smoke requires a new absolute journal path in an owner directory without
shared writes. The exclusive `0600` journal saves and syncs each reservation
before HTTP and the outcome afterward; an existing journal cannot be reused.
SIGINT/SIGTERM cancels the bounded call while retaining admitted reservations.
See [evaluation format and evidence](router-evaluation.md) for reproducible cases.

Local fixtures cover launch/control and accounting boundaries. On 2026-10-10,
an authorized six-call smoke against pinned `jev-1.13.0` returned six valid
responses: 4/4 development and 2/2 separate synthetic holdout label agreement,
with no fallback, schema or transport failures. All labels were machine-authored.
Reported usage was 3,269 input and 204 output tokens; the routing cost estimate
was 139 USD micros ($0.000139), with 574 USD micros reserved. Observed latency
was 350–473 ms; development p50/p95 was 372/412 ms and the two-case holdout was
359/473 ms. See the [bound evidence and replay record](router-evaluation.md).

No paid worker provider was executed. Worker task quality, worker execution cost
and total execution cost remain unmeasured; `real_quality_validated` is false.
A consented real holdout and a comparison against direct target selection remain
open. Neither artificial fixtures nor this tiny synthetic sample establish
real-task quality or a broad speed guarantee. See the [router plan](plans/jev-router.md).
