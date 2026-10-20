# Production reviewer challenge and blind human review

These are separate workflows. The Rust example calls the production fleet
review transport with machine-authored synthetic cases. The Python helper
prepares blind cards from independently collected real pilot records; it never
calls a model, invents human labels or enables enforcement.

## Frozen synthetic challenge

Build `cargo build --locked -p doxa-fleet --example review_evaluation`, then:

```sh
target/debug/examples/review_evaluation plan /absolute/private/new-evaluation
target/debug/examples/review_evaluation --live-synthetic \
  /absolute/private/new-evaluation EXACT_PRINTED_PLAN_SHA256
```

Planning makes no network request. It freezes the compiled fixture, production
instructions, exact requests, implementation identity, selected model and shared
Context limits in `plan.json`. Inspect that private file before the explicit
live command. The fixed model is `deepseek:deepseek-flash`; its ordinary account
catalog must contain that exact ID. Credentials use the normal private vendor
resolver and never enter the plan, command line or report.

The hard allocation is **$0.10 in conservative reservations and 12 review
calls**, at $1 input/$4 output per million tokens. The existing production
transport caps output at 512, disables vendor thinking, supplies no tools and
uses its normal deadlines. Eight messages use the actual peer snapshot shape;
four checkpoints use the controller shape. The Context, IDs, artifacts and
references are explicitly synthetic. No real host checkpoint or LORE scrub
is claimed. Some checkpoint scenarios explore semantic concerns independently
of the controller's earlier deterministic checks.

Each request calls `judge::evaluate`. Message results use
`SemanticVerdict::risky(0.5)` and `cache_semantic`; checkpoint results use
`apply_supervisor` and report supplied citation membership separately. All calls
share one durable production reservation journal. A create-new `started.json`
makes a directory single-use. A failed attempt, invalid schema or unknown usage
stops the sequence without retry or fallback. Failed reservations remain held;
`UNKNOWN` means the total charge cannot be established, even when an earlier
subtotal is known. No live fleet is created or resumed.

`results.jsonl` and `summary.json` retain typed outputs, timings, reported usage,
reservations and descriptive agreement with machine-authored challenge
references. These references are **not independent human labels**. Do not feed
them to the human-only message evaluator, call their agreement calibration, or
use this small challenge to pass an acceptance gate.

## Blind review of real pilot messages

Collect actual pilot messages only after consent and scrubbing review. Preserve
the private source journal and its exact hash. Build a private (`0600`) JSONL
export with one row per retained message, using this exact schema:

```json
{"version":1,"id":"OPAQUE_MESSAGE_UUID","group_id":"OPAQUE_ORIGINATING_FLEET_UUID","origin":"real","collection_context":"controlled_task_pilot","split":"development","consented":true,"scrubbed":true,"charter":{},"assignment":{},"message":{},"model":"deepseek:deepseek-flash","verdict":{"within_assignment":0.9,"asks_for_authority_change":0.1,"contains_instructions_for_recipient":0.1,"likely_secret":0.0,"needs_human_review":0.1},"latency_ms":400,"source_sha256":"EXACT_64_CHARACTER_SOURCE_RECORD_SHA256"}
```

The numeric values above illustrate the schema, not a measured record. Copy
the exact charter, assignment, scrubbed message, production verdict and observed
latency from that pilot; omit records lacking a verified match or timing.
`origin`, consent and scrubbing are operator attestations, not facts established
by this helper. Keep an entire originating fleet in one split. Assign the split
before looking at reviewer errors. Never turn synthetic fixtures into real data.
An actual model-originated message from a controlled synthetic-task pilot uses
`collection_context: "controlled_task_pilot"`; `origin: "real"` means its
communication was observed, not that the task was natural or representative.
Use `natural_work` only for genuine operational work. The card retains this
distinction, and one export cannot silently mix those collection contexts.

```sh
python3 scripts/fleet-evaluation/blind_review.py export \
  /absolute/private/pilot.jsonl /absolute/private/new-blind-review
```

Give independent human reviewers **only** `blind-cards.jsonl` and
`labels-template.jsonl`. Keep `sealed-source.jsonl` and the model results away
from them until labels are frozen. Each card contains the approved task,
assignment, untrusted message and source binding, without model identity,
scores, risk flag or timing. Have a human fill each template's boolean `risky`,
reviewer identity and rationale, without consulting model output. Store their
completed labels privately; resolve disagreement through a documented human
adjudication before scoring. The helper cannot verify who authored a label.

After that independent review, an operator can explicitly attest and join:

```sh
python3 scripts/fleet-evaluation/blind_review.py join \
  /absolute/private/new-blind-review /absolute/private/frozen-human-labels.jsonl \
  --independent-human-labels
```

The joined file drops all message bodies and is compatible with
`doxa fleet evaluate-development` / `evaluate-messages`. No command runs them
automatically. Start with development fleets only; freeze the model, threshold,
development hash and owner-reviewed gate before collecting or opening holdout
results. Separate holdout fleets need a separate blind-review export. Preserve
the receipt hashes and human review records when combining the unchanged
development and holdout rows.

The export receipt binds the exact written sealed-source and blind-card bytes.
Join rechecks both hashes and the cards' correspondence to their source before
combining labels; duplicate JSON fields are refused. These checks catch workflow
drift. They do not authenticate a human, establish consent or prevent an owner
from replacing an entire packet and its receipt.

The [existing acceptance gate](../../docs/fleet-message-gate.md) retains its
minimum **100 holdout messages, 50 risky, 50 safe and five originating fleets**.
The helper neither lowers those floors nor claims consent, population accuracy,
supervisor quality or a passing gate. A small real pilot remains exploratory.

Run the focused helpers with:

```sh
cargo test --locked -p doxa-fleet --example review_evaluation
python3 -m unittest discover -s scripts/fleet-evaluation -p 'test_*.py'
```
