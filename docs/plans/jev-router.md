# Jev router plan

Status: native API/CLI routing, reviewed manual/automatic compaction,
child/fleet allowance inheritance, history recovery and Docker migration are
implemented through beta.63. The initial API slice, its synthetic smoke and
subsequent local fixture results retain their dated scopes below. Real-task
routing and summary-quality evaluation remain open; see the
[current operator guide](../jev-router.md) and
[2026-10-13 integration record](../router-integration-2026-10-13.md).

## Implemented scope

- Explicit `router` engine with absolute `--router-config`, or an explicitly
  named private `DOXA_ROUTER_CONFIG`/Settings path. No invented candidate list.
- Configured DeepSeek/GLM API and authenticated Claude/protected Codex CLI target
  IDs, fixed per-target effort and cap/account-billing policy.
  Eligibility is checked against credentials, catalog, history, tools and budget
  before execution. Descriptions are operator assumptions, not quality evidence.
- Turn-boundary Jev Choice routing, bounded requests/deadlines and exact target
  validation. `/model auto` versus a configured target pin remains distinct from
  permission mode and effective per-turn worker identity.
- Canonical durable router conversation and route metadata; hash-bound resume;
  durable routing/worker reservations and accounting uncertainty refusal.
- Bounded target/provider/model/effort status, fallback reason, latency and cost
  estimates. No credentials or full routing input in UI routing rows.
- Bare `/compact` uses the pin, last ordinary worker or configured fallback,
  with no Jev call or selection change. Mandatory canonical-source LORE review
  precedes a bounded summary; originals remain canonical and a verified summary
  is separate context. Aggregate cost/reservation and unknown-usage refusal
  apply to API summary calls; CLI pin/last target is explicitly refused. UI summary identity and reattach state remain separate
  from the ordinary routed worker.
- Internal CLI workers use fresh private UUIDs, exact current authenticated
  catalogs, existing native tool/permission controls and one outer Rust
  peer/LORE registry. Durable worker/request/canonical-source bindings and
  complete observed token usage guard settlement/resume. Private worker proof
  is excluded from public history/restore. Explicit `cli_account` policy keeps
  CLI charges and total charge unknown, outside the tracked Jev/API allowance.
- `/settings` reviews an explicit config/current catalogs before enabling Jev
  Router; routing Auto remains separate from idle native permission Auto.
- Recorded-response evaluation with development/holdout group separation,
  synthetic fixture generation, and a separately opted-in synthetic live smoke.
  Evaluator never executes workers or claims worker quality from routing labels.
- Reviewed automatic compaction is an explicit frozen config opt-in. Child
  launches reserve disjoint durable dollar/call/spend allowances after human
  review; fleet slots split the reviewed routing limits and run budget. History
  restores the original config, decision service and ceiling. Router Docker
  launch/migration uses the existing isolation boundary and complete canonical
  checkpoint. `/clear`/`/cd` inheritance, external CLI handoffs and CLI summary
  targets remain unavailable.

The [operator guide](../jev-router.md) describes config, controls, failures,
accounting and evaluation commands. An attempted Jev HTTP call with missing
verifiable usage pauses workers; eligible no-call fallback and verified
low-confidence fallback are separate cases.

## Validation record

The private UI worktree passed 21 focused checks: 12 router controls/refusals,
seven settings regressions, one history-resume boundary and one aggregate
billing restore/unknown-cost fixture. The frontend binaries compiled offline.
The integrated
branch passed 730 TUI tests (six existing ignored), 63 daemon tests, 20
transcript tests and 14 vendor library tests (including nine credential tests).
The router core and evaluator passed 11 tests. The daemon checks
include router CLI target/config validation, same-session resume and refusal
of effort/sandbox/child/Docker combinations. The router host passed 31 local
integration fixtures, the canonical vendor-message integration passed five,
and the bounded vendor turn checks passed two fixtures.
These are local fixture results.

Compaction validation on 2026-10-11 is local and separate from Jev evaluation.
The private UI worktree passed four focused compaction tests: exact supported
command dispatch, source-review/summary phases with separate Auto/pin and worker
identity, known-failure/unknown-cost handling without false completion, and
bounded billing restoration. The lifecycle fixture retains completed summary
status after a later ordinary turn changes its routed worker. Both frontend
binaries passed `cargo check --locked --offline -p doxa-tui --bins`.

The integrated host passed eight native compaction tests across 22 controlled
fixture variants, 31 routed-host tests, two fixed-vendor compaction tests and
19 vendor transport tests. These cover success and subsequent turns, resume,
review/budget refusal, cancellation and crash reservations, invalid or missing
usage/model identity, source changes during review/catalog/provider work, and
serialized request caps. A full TUI run passed 733 tests with six ignored and
one stale help-text assertion; that assertion was updated and passed separately.
The production daemon, frontend and router compiled without test transports.
No paid calls were made for this compaction verification.

The controlled native LORE carrier verification passed two review-policy tests:
reviewer invocation on canonical router records, unchanged originals and stale
source refusal before the reviewer provider. The pinned LORE worker receives
the selected API provider as derivation `source_engine`; review metadata and
supervisor receipt bind the router conversation plus exact summary target.
Canonical DOXA storage/replay still use `router`. This is not authenticated
paid review or summary quality evidence.

Native CLI routing verification on 2026-10-11 used six credential-free Linux
production-daemon fixture tests. All six passed across focused runs: the five
remaining cases passed together and the corrected advertised-GLM-effort history
case passed separately. They exercise Claude and protected Codex stdio hosts,
command/peer-tool approval roundtrips, exact authenticated-catalog/resolved-model
admission, fresh private identities, one outer registry, API switching/resume,
optional executable refusal, missing/wrong/excess usage, source changes,
interruption/process cleanup and no uncertain replay. Fake account metadata is
controlled fixture data, not live authenticated-account evidence.

Eight focused regressions passed: one core billing-policy/hash check, three
Claude catalog/authentication checks, two daemon argument/config checks, one
large private-worker history exclusion and one native permission-picker check.
The production daemon built with `cargo build --locked --offline -p doxa-daemon`
and frontend binaries passed `cargo check --locked --offline -p doxa-tui --bins`.
No paid worker calls, real login changes or live installation were performed.
CLI account billing/quota and real-task quality remain unmeasured.

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
- [x] Explicit reviewed managed compaction with separate summary context and
  durable aggregate accounting; local verification only.
- [x] Opt-in automatic compaction through the existing canonical source-review
  and durable-accounting path; one ordinary-turn boundary.
- [ ] Broader long-history limits, real-task summary quality and authenticated
  summary-provider verification.
- [ ] First-class router derivation attribution in LORE; current compatibility
  uses the selected API provider while retaining router conversation proof.
- [x] Reviewed config/allowance propagation for child sessions, fleets and
  history launches with disjoint durable allocations and unchanged resume spend.
- [x] Integrated Docker migration checkpoint and disposable-host validation:
  four clean/dirty GLM and Router round trips plus target-start rollback passed
  with unchanged Router journals; no provider turn or hardened-origin claim.
- [x] Internal authenticated Claude/protected Codex workers with bounded
  canonical history transfer, native permission/tool lifecycle and fail-closed
  recovery. Credential-free Linux local fixtures; no real-account quality claim.
- [ ] External CLI handoffs and CLI managed-summary targets. These remain
  separate from the implemented, reviewed child/fleet/Docker inheritance.
- [ ] Authenticated platform checks. Local Linux fixtures are not authenticated
  macOS or provider availability evidence.
