# Jev exploratory follow-up on 2026-10-20

The owner supplied a throwaway credential for bounded routing checks. Production
`doxa-router-eval` made seven requests to pinned `jev-1.13.0`: six valid closed
Choices and one unavailable attempt with unknown usage. No worker executed,
no owner transcript was used, and no human label was created. The local key copy
was removed after the checks; retained Jev artifacts were checked for that exact
credential before removal. Provider-side revocation was not performed.

## Observations and retained accounting

The first installed beta.64 run froze six synthetic cases. Its first two calls
selected `fast` with valid pinned model, probabilities, confidence and usage.
The third returned `unavailable` without verified usage. The final three cases
were withheld by sticky unknown accounting. Its conservative reservations total
286 USD micros ($0.000286), including the unknown attempt. That request was not
retried and its journal remains unchanged. The old outcome did not retain an
HTTP category, so a rate limit or other cause cannot be assigned retroactively.

A diagnostic-only source correction adds closed failure categories without raw
provider bodies or retries. Twenty-one router checks passed, including 13 real
loopback failed-attempt cases retaining their reservations across journal reload.
The exact production evaluator built from `2927e28b` then ran four distinct new
synthetic inputs in two two-call batches, separated by six seconds. All four
returned validated Choices matching their machine references: two `fast`, two
`deliberate`. Their latencies were 307, 303, 336 and 321 ms. Cumulative new
reservations were 388 USD micros ($0.000388), with known usage on all four calls.

A preceding one-row batch was rejected by local split validation before
credential lookup, journal creation or HTTP. Its zero-call preflight receipt and
correction are retained; it is not counted as a provider failure or retry.

Across the seven actual calls, cumulative reservations are **674 USD micros
($0.000674)**. Known usage on the six valid responses totals 3,285 input and 202
output tokens; the rounded cost estimate is 141 USD micros ($0.000141). That
lower estimate does not replace the unknown reservation. Including earlier
evaluation work, the cumulative retained amount is $0.052143 of the $0.10
evaluation ceiling. These are host estimates and holds, not provider invoices.

## Evidence and limits

Private plans, exact synthetic inputs, reports, per-call journals and the key
cleanup receipt are under `~/.cache/doxa-acceptance-20261020/jev/`. The production
diagnostic evaluator SHA-256 is
`4e125816e871d4e2a6055f6b5c90ab0e05858f32ae846670bdb6983916157cc4`.
Both the earlier unknown journal and the subsequent new journals are retained;
no accounting was reset to repeat an uncertain request.

The six valid selections agreeing with machine references establish transport
and toy-case behavior only. Fallbacks that happen to match a reference are not
valid model choices. The later files' synthetic holdout split satisfies evaluator
plumbing, not an independent human holdout. No worker success, comparative worker
cost, natural-task routing accuracy or alignment quality was measured;
`real_quality_validated` remains false. The separate
[human supervision gate](../fleet-message-gate.md) remains open.
