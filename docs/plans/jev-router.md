# Jev router plan

Status: first native API slice implemented for integration on 2026-10-10;
fixture validation is recorded below. Live routing evaluation remains open.

## Implemented scope

- Explicit `router` engine with absolute `--router-config`, or an explicitly
  named private `DOXA_ROUTER_CONFIG`/Settings path. No invented candidate list.
- Configured DeepSeek/GLM target IDs, fixed per-target effort and cap/rate policy.
  Eligibility is checked against credentials, catalog, history, tools and budget
  before execution. Descriptions are operator assumptions, not quality evidence.
- Turn-boundary Jev Choice routing, bounded requests/deadlines and exact target
  validation. `/model auto` versus a configured target pin remains distinct from
  permission mode and effective per-turn worker identity.
- Canonical durable API conversation and route metadata; hash-bound resume;
  durable routing/worker reservations and accounting uncertainty refusal.
- Bounded target/provider/model/effort status, fallback reason, latency and cost
  estimates. No credentials or full routing input in UI routing rows.
- Recorded-response evaluation with development/holdout group separation,
  synthetic fixture generation, and a separately opted-in synthetic live smoke.
  Evaluator never executes workers or claims worker quality from routing labels.
- Native only. Unsupported Docker, isolation migration, history auto-resume,
  `/clear`/`/cd` inheritance, child/fleet inheritance, CLI handoffs and compaction
  are refused or unavailable.

The [operator guide](../jev-router.md) describes config, controls, failures,
accounting and evaluation commands. An attempted Jev HTTP call with missing
verifiable usage pauses workers; eligible no-call fallback and verified
low-confidence fallback are separate cases.

## Validation record

The private UI worktree passed 21 focused checks: 12 router controls/refusals,
seven settings regressions, one history-resume boundary and one aggregate
billing restore/unknown-cost fixture. The frontend binaries compiled offline.
The integrated
branch passed 730 TUI tests (six existing ignored), 63 daemon tests, four
transcript tests, 14 vendor tests and nine credential tests. The daemon checks
include router CLI target/config validation, same-session resume and refusal
of effort/sandbox/child/Docker combinations. The router host passed 31 local
integration fixtures, the canonical vendor-message integration passed five,
and the bounded vendor turn checks passed two fixtures.
These are local fixture results.
The [evaluation record](../router-evaluation.md) binds an authorized six-call
`jev-1.13.0` synthetic smoke on 2026-10-10 to exact config/input hashes and
recorded responses. Development agreement was 4/4 and separate synthetic
holdout agreement was 2/2, with no fallback or schema/transport failure. All
labels were machine-authored. Routing usage was 3,269 input/204 output tokens;
estimated router cost was $0.000139 and reservations were $0.000574. Observed
latency was 350–473 ms (development p50/p95 372/412 ms; holdout 359/473 ms).
No worker executed. Worker quality, worker/total execution cost and real-task
quality validation remain unmeasured. Source-dated worker price bounds and Jev
usage-based cost are estimates, not live invoices.

## Open work

- [x] Authorized bounded synthetic Jev transport smoke with complete usage evidence;
  six cases passed on 2026-10-10. This is not a real-task quality gate.
- [ ] Consented, scrubbed real cases with independent human development/holdout
  labels, representative routing mistakes and direct-selection baselines.
- [ ] Worker task success, latency and aggregate execution cost measurements;
  current router-label agreement does not establish these.
- [ ] Route-aware reviewed compaction and long-history limits.
- [ ] Reviewed config/allowance propagation for child sessions, fleets, history
  launches and Docker migration.
- [ ] Separate Claude/Codex CLI handoff contract, conversation transfer,
  permission boundary and lifecycle recovery; no automatic CLI fallback.
- [ ] Authenticated platform checks. Local Linux fixtures are not authenticated
  macOS or provider availability evidence.
