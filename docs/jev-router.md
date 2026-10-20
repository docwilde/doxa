# Jev / SubQ Snap turn router

The opt-in `router` engine selects a configured DeepSeek/GLM API target or an
authenticated Claude/protected Codex CLI target at the start of a turn. It keeps
one durable DOXA conversation and peer/LORE registry identity, recording the
effective target, provider, model and effort for each turn. API targets retain
their narrower tools; CLI targets retain their native command/file permissions
and DOXA reviews. Automatic routing does not approve tools.

Explicit [external MCP tools](vendor-mcp.md) are inherited by API turns only;
Claude/Codex targets retain native MCP ownership. Every external MCP call
requires a one-call review, including with native permission Auto. External
server charges are unknown and excluded from the tracked routing/API allowance;
the cost chip shows `MCP ?`. Managed compaction has no tools and does not
connect or list MCP servers.

Candidate descriptions express the operator's routing policy. They are not
measured quality claims. Jev Choice confidence describes its returned choice
distribution; it is not a probability that the worker will complete the task.

## Decision service

The router accepts pinned `jev-1.13.0` or `subq-snap-preview`. Use the
`router_model` JSON field (legacy `jev_model` remains compatible), or select
**routing decision model** in `/settings` → Session. `DOXA_ROUTER_MODEL` is an
explicit environment override for new sessions. Changing the decision model
changes the frozen config digest; resuming requires the original selection.

Snap uses [SubQ's typed Decisions API](https://docs.subq.ai/endpoints/decisions-api/)
at `https://api.subq.ai/v1/systemone`, with a private saved SubQ key or
`SUBQ_API_KEY`. The API pins `subq-snap-preview`; other SubQ models are refused.
[Published pricing](https://subq.ai/#pricing) is $0.05/M input tokens and free
output. DOXA validates exact model/usage, eligible choices, complete normalized
probabilities and finite confidence. Snap's reported confidence is checked
against the frozen guard; Jev additionally uses its documented distribution
formula. Ties fall back. Neither score certifies worker quality.

Both services share bounded requests, deadlines, durable reservations and the
configured fallback. Unreported usage retains its reservation and prevents
worker execution until accounting is reconciled. There is no automatic paid
retry or service switch. Coverage uses local HTTP fixtures; live comparative
quality and latency evaluation remains open.

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
Model Router in `/settings` → Session or the new-session form requires that
setting. Ctrl+P on the default-engine/config row reviews the private config and
current authenticated catalogs; Ctrl+S saves only after a ready review. The
review checks Jev credential shape/presence without authenticating it over HTTP. The
form offers Auto and configured target IDs; no candidate list is invented. Global `DOXA_MODEL` and `DOXA_EFFORT` do not
select router targets. A stored `[models].router` preference applies to new CLI
launches and must name `auto` or an exact target ID.

- `/model auto` permits turn-boundary routing.
- `/model ds-chat` pins that exact configured target and skips Jev selection.
- Changes require an idle session. Effort is configured per target; `/effort`
  is unavailable on router sessions.
- With configured CLI targets, `/mode default` keeps native manual reviews:
  Claude manual and Codex on-request with workspace-write sandbox. Idle
  `/mode auto` explicitly opts future native workers into their existing Auto
  permission mode. It is separate from `/model auto`; DOXA peer/LORE reviews
  remain active. Router offers neither `full-access` nor `dontAsk`. API-only
  configs have no provider permission control.

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
API price bounds; 1–10,000 routing calls; 1–1,000,000 USD micros of routing spend;
512–24,576 bytes of router input; 100–12,000 ms routing deadline. One dollar is
1,000,000 USD micros, so `10000` is $0.01. The fallback must name a configured
candidate. Unknown caps or API prices cannot be entered as zero. Existing API
configs keep their serialization/hash; omitted `billing` means `api`.

Optional `auto_compact_percent` enables reviewed automatic compaction at a
retained-context pressure threshold of 50–95 percent. For example,
`"auto_compact_percent": 80` opts in at 80 percent of the selected summary
target's configured input cap. It is absent by default and participates in the
frozen config hash. Existing configs retain their previous hash when it is
omitted. The pressure estimate includes the host's reserved memory and tool
overhead; it is a conservative request-byte estimate, not a provider token count.

Before each turn the host checks credentials, account catalog, model/effort,
tools, retained history, context/output caps and aggregate allowance. Configured
targets are policy candidates, not a claim that every account can use them.
The fallback also has to remain eligible. An ineligible pin or an empty eligible
set refuses the turn; it does not switch to another engine. DeepSeek thinking
targets can be ineligible for retained assistant/tool history.

### Authenticated CLI candidates

Add an exact connected-account model and effort as a configured target, with
explicit `billing: "cli_account"`. Omit both USD price fields. The following
schema fragment uses placeholders, which must be replaced with exact advertised
or resolved model IDs from your authenticated catalog; it is not a launchable
recommendation:

```json
{
  "id": "claude-code",
  "provider": "claude",
  "billing": "cli_account",
  "model": "EXACT_AUTHENTICATED_RESOLVED_MODEL_ID",
  "effort": "high",
  "description": "Operator policy for native coding tasks; not measured quality.",
  "context_tokens": 131072,
  "max_output_tokens": 4096,
  "supports_tools": true
}
```

Codex uses `provider: "codex"` with the same explicit account billing policy.
Authentication does not establish a subscription type, available quota or price.
API targets require positive checked price bounds; CLI account targets reject
invented USD rates. Native output usage above the configured cap is refused at
settlement; the cap is not a claim that the provider exposes a per-turn token
limit. The canonical history packet and collected public output are bounded.

DOXA initializes the installed native CLI without a user prompt to check
current authentication and exact model/effort availability before Jev selection
and again before worker launch. Claude needs actual token/API-key source or
known account evidence; its default `apiProvider` metadata alone is insufficient.
Codex uses read-only `account/read` without token refresh and the protected
provider contract. Missing authentication, executable, model or effort makes
that candidate ineligible. An eligible API fallback can still run when an
optional CLI is unavailable. An unavailable exact pin refuses before Jev.
Explicit bad `--claude-bin`/`--codex-bin` paths are errors; those overrides must
correspond to configured provider families. Model aliases that return a different
actual model cannot settle a verified turn: configure the exact resolved ID.

Each CLI turn creates a fresh private worker UUID and uses the existing native
host/tool bridge. A bounded canonical public-history packet supplies context;
old tool calls and old requests are not replayed. Internal provider artifacts
and protected source proof belong to that worker UUID and are excluded from
public history/restore and registry pickers. The router journal binds the worker,
request and canonical source hashes before submission. Native LORE/provider
proof remains separate from router conversation proof; only verified public
user/assistant text enters the canonical router history. Shared AgentTools and
peer/LORE identity remain attached to the outer router across worker teardown.

## Credentials, failure and accounting

Jev uses `TYPESAFE_API_KEY`. API workers use the existing DeepSeek/GLM credential
paths (`DEEPSEEK_API_KEY` and `ZAI_API_KEY`, or the private DOXA `/setup` store).
CLI workers use their existing authenticated account through the private native
hosts; router discovery does not create login state or run a user turn.
Do not put keys in the config, criteria, descriptions, command arguments or
evaluation files. Auto sends a scrubbed, bounded routing view and candidate
policy to Jev; normal worker calls still send the worker conversation to the
selected worker provider.

When no Jev HTTP call is attempted, a missing Jev credential, exhausted routing
allowance or a single eligible target can use the eligible fallback. Pinning
skips Jev. A verified low-confidence result can also fall back with known usage.
An attempted HTTP call without complete verifiable usage leaves accounting
unknown: worker execution and subsequent turns are withheld. Timeouts, invalid
answers and outages must not be assumed to be free or safe to retry.

The session journal records reservations before calls and retains them on
failure or cancellation. The tracked USD session budget combines Jev and direct
API worker holds;
the fixed-price `BudgetHost` wrapper is not used for router sessions. Worker
holds conservatively cover up to 25 bounded requests and the aggregate output
allowance. Displayed aggregate cost combines reported Jev usage and worker
estimates at the checked upper rates; retained reservations are shown separately.
Token-based estimates and conservative reservations are not billed tier or invoice facts.

A reviewed `cli_account` config explicitly excludes unknown CLI provider charges
from that tracked allowance. Mixed-session telemetry labels the estimate
`Router/API · CLI ?` on CLI turns, later API turns and reattach: provider CLI charge
and total charge stay unknown, never zero. Complete observed CLI token usage is
tracked separately. Interrupted, missing or inconsistent usage/model/source
proof retains the pending worker binding and blocks future work/resume replay.
There is no all-in dollar ceiling or affordability claim for CLI account workers.
An interrupted/incomplete journal pauses further work rather than replaying an
uncertain turn.

## Reviewed managed compaction

Use bare `/compact` in an idle router session. The summary target is the exact
pin when pinned; in Auto it is the last ordinary effective worker, or the
configured fallback if no ordinary worker has run. The target must still pass
API eligibility and aggregate budget checks. A CLI pin or last CLI worker is
explicitly refused for `/compact` in this slice; pin an eligible API target for
the reviewed managed summary. Compaction makes no Jev selection call
and changes neither Auto/the pin nor the last ordinary routing status.

The host requires completed LORE review of the canonical source before sending
a bounded summary request. Disabled or unavailable review, stale source proof,
ineligible target and budget refusal stop before a summary HTTP request. A
verified managed summary becomes separate context for subsequent turns; the
canonical messages and transcript originals remain intact.

With `auto_compact_percent` configured, an ordinary turn first checks retained
context pressure against the same pin/last-worker/fallback summary target. If
the threshold is reached, it runs this exact source-review, bounded-summary and
durable-accounting path before routing the original user turn. The pending user
prompt alone does not trigger a paid summary. Automatic progress remains inside
one daemon turn: the UI stays busy through review, summary and the ordinary
worker, and displays the original user turn once. Receipts distinguish automatic
from explicit compaction. Review refusal, cancellation, unavailable API target
or uncertain summary usage withholds the ordinary worker. A CLI pin or last
CLI worker still requires an explicitly selected eligible API summary target;
automatic compaction does not silently switch providers. History and summary
requests remain bounded; late compaction cannot recover an oversized source.

The UI shows the source review and summary phases, then a separate summary
target/provider/model/effort record with the original message count. Reattach
restores that record from billing telemetry. Summary source/content and
credentials are not included in those status rows. Summary usage is charged
against the durable aggregate allowance and included in the session estimate,
even when a known-usage summary is rejected. An attempted summary with missing
usage retains its reservation and blocks further work; retry or resume does
not clear accounting uncertainty. Admission saves the exact summary target,
request hash and canonical source proof before HTTP, so interrupted work retains
that binding in the durable journal. Recovered summary context must still match
its canonical source prefix. A canonical source mismatch is sticky for the live
host and withholds later Jev and worker calls; restoring an old file does not
make that host retry automatically.

The pinned native LORE worker currently accepts the selected API provider as
review authority, rather than `router`. LORE derivation `source_engine` therefore
identifies DeepSeek or GLM. DOXA binds `conversation_engine: "router"` and the
exact summary worker identity in review metadata/receipts, while canonical
source proof, storage and replay retain engine `router`. First-class router
attribution in LORE remains future work.

Compaction verification is local: controlled provider fixtures and a controlled
native LORE carrier test cover review and source integrity. These checks are
separate from the six-call synthetic Jev transport smoke below. No authenticated
paid summary or real-task summary quality result is recorded.

## Resume and remaining limits

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

History launches restore the saved private config path, frozen decision service,
Auto/pin and exact original ceiling, including fleet per-slot configs. They check
the current config hash and canonical project identity before launching. A
different environment or Settings budget cannot create a fresh allowance for
that historical conversation. Older journals without config provenance still
require the original explicit config. Existing live sessions attach normally.

With session spawning enabled, each Router spawn requires a complete human
review of the exact task, target, config digest, native permission mode and child
allowance. A finite parent ceiling is required. Before launch, the parent
durably reserves half its remaining tracked dollar allowance and half its
remaining routing-call and routing-spend allowances. The child validates that
exact allocation, receives its own worktree, conversation and journal, and can
allocate only within its own share. Changes to config, selection, permission or
allowance during review refuse launch. Concurrent stale reviews cannot allocate
the same allowance twice. Failed or interrupted startup retains the allocation;
there is no automatic reclaim or uncertain replay. Ancestor held totals include
all their child ceilings. Session cost estimates cover that session's own
reported work; descendant charges are not presented as a known aggregate invoice.
CLI provider charges remain excluded and unknown.

Router fleet pools accept `--router-config ABSOLUTE_PATH` and require a finite
`--run-budget`. The reviewed routing-call and routing-spend limits are split
deterministically across assigned Router slots. Each slot gets a private frozen
config and its existing disjoint share of the fleet dollar budget. The complete
fleet review binds these configs; startup and controller resume verify each
daemon's exact digest and original ceiling. Controller recovery observes the
existing sessions and does not redeliver admitted prompts.

Router sessions can use the existing native, docker-open and docker-offline
profiles. Config, canonical history, reviews and allowance journals stay under
the trusted host. Configured CLI families prepare their own credential snapshots
and use the existing container transport, while API calls use the host transport
and existing isolated workspace tools. Explicit idle backend migration binds the
canonical transcript and complete Router journal fingerprint, preserves the
logical project identity, original allowance and exact config, and starts fresh
private CLI workers on later turns. Uncertain or pending work refuses migration.
No internal CLI rollout is replayed as a new Router turn. Hardened admission is
governed by the separate [isolation contract](session-isolation.md).

`/clear`, `/cd`, external CLI handoffs and CLI summary targets remain unavailable.
Real-account summary quality and provider/platform verification remain separate
from the credential-free integration fixtures.

See the [2026-10-13 integration verification record](router-integration-2026-10-13.md)
for child/fleet recovery, automatic review failure boundaries and Docker proof
requirements.

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

Credential-free Linux native process fixtures cover routed Claude and protected Codex
authenticated catalog admission, native permission/peer-tool roundtrips, exact
model/usage accounting, fresh worker identities, API switching and canonical
resume. They do not prove real-account quota, billing or task quality.

On 2026-10-10,
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
