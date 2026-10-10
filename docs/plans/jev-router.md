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

Focused CLI/UI fixture results will be recorded after integrated compilation.
Core/host tests are owned by their implementation workers and reconciled during
integration. No authenticated router/worker request or paid evaluation was sent
for this implementation. `TYPESAFE_API_KEY` was absent in the integration
environment. Source-dated worker price bounds are estimates, not live invoices.

## Open work

- [ ] Authorized synthetic Jev transport smoke with complete usage evidence.
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
