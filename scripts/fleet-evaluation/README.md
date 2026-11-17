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

The [draft label guide](../../docs/fleet-label-guide.md) covers independent
message and checkpoint review. It needs owner review and freezing before
development labeling. This helper joins one final `risky` label; it does not
collect per-field human labels, enforce double review or compute agreement.

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

## Blind checkpoint review

`checkpoint_review.py` prepares [checkpoint evaluator](../../docs/fleet-checkpoint-gate.md)
rows in two stages. First, reviewers judge the scrubbed host snapshot without
the evaluated Choice. After the operator freezes their final verdicts, a
separate review checks whether the selected artifact supports the model's
alignment and basis. The helper reads recorded data and makes no live calls.

Prepare an owner-private (`0600`) source JSONL file from the private collection
journal. Use the exact scrubbed host snapshot sent to the Choice adapter, before
provider-specific request wrapping. Preserve all eight snapshot fields,
including worker or earlier reviewer observations that appear in the input.
Keep any disclosure or exclusion decision in the private journal. This example
shows the source schema with artificial development data.

```json
{"version":1,"id":"00000000-0000-4000-8000-000000000001","group_id":"00000000-0000-4000-8000-000000000010","source_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","origin":"synthetic","collection_context":"controlled_task_pilot","split":"development","consented":false,"scrubbed":true,"snapshot":{"charter":{},"assignments":[],"artifacts":[{"id":"host-example","evidence":{}}],"guard_observations":[],"recent_handoff_evidence":[],"budget":{},"elapsed_deadline":null,"phase":"running"},"model":"jev:jev-1.13.0","threshold":0.75,"latency_ms":500,"outcome":{"status":"decision","choice":"aligned","basis":"within_charter","citation_ids":["host-example"],"alignment_confidence":0.99,"guard_confidence":[0.99,0.99,0.99],"supported":true}}
```

Use an opaque checkpoint UUID, a stable opaque originating fleet UUID and the
SHA-256 of the exact original journal record. The collector attests the mapping
to that record, consent and scrubbing. One packet has one origin, split, model
and threshold. Each originating fleet stays in one split and collection context
across packets. Real rows require `origin: "real"` and `consented: true`.
Controlled-task pilots and synthetic references stay in development.

For a recorded decision, retain the raw alignment Choice and confidence, the
basis Choice, the zero or one selected artifact IDs, and the three validated
confidence guards in alignment, basis and evidence order. Translate the
production no-evidence criteria to an empty array. In the source and the joined
evaluation, `supported` records the production basis/evidence guard before the
confidence check. Use `{"status":"unavailable"}` for an absent or rejected model
verdict with a recorded snapshot and timing. Preserve missing records separately
for coverage reporting. The helper does not capture those inputs, replay the
reviewer or reconstruct raw Choices from an effective verdict.

```sh
python3 scripts/fleet-evaluation/checkpoint_review.py export \
  /absolute/private/checkpoints.jsonl /absolute/private/label-guide.md /absolute/private/new-checkpoint-packet
```

After disclosure review, give the initial reviewers only `blind-cards.jsonl`,
`label-guide.md` and `verdict-template.jsonl`. Copy the template into a private
submission file. Retain both independent submissions and any adjudication in the
private review record. Prepare one final verdict per card with its reviewer ID
and nonempty rationale. Keep the two template hashes intact. The tool checks
coverage and binds the submitted file. Pairing, adjudication and human
authorship are operator responsibilities.

```sh
python3 scripts/fleet-evaluation/checkpoint_review.py freeze \
  /absolute/private/new-checkpoint-packet /absolute/private/final-verdicts.jsonl --independent-human-labels
```

The packet's `frozen/` directory is created once. It binds the export receipt
and the exact submitted verdicts before writing the citation cards. Give
citation reviewers only `frozen/citation-cards.jsonl` and
`frozen/citation-template.jsonl`. They read the same snapshot, the model's raw
alignment, its basis and cited ID. Keep the frozen human verdicts private.
For every cited decision, copy the citation template into a private submission
and record `supports_claim` as a boolean with reviewer ID and rationale. The
tool derives citation membership from the supplied artifacts and treats support
for an absent ID as invalid. For a decision with no citations, both citation
flags are true, and its separate production `supported` guard still applies.

```sh
python3 scripts/fleet-evaluation/checkpoint_review.py join \
  /absolute/private/new-checkpoint-packet /absolute/private/new-checkpoint-result \
  --citation-labels /absolute/private/citation-reviews.jsonl --independent-citation-review
```

Omit both citation options when the citation file is empty. For an offline
synthetic fixture, freeze with `--synthetic-reference`, supply fixture citation
reviews as needed, and omit `--independent-citation-review`. Its joined
`label_source` remains `synthetic_reference`.

`labeled-checkpoints.jsonl` contains the evaluator rows with the frozen verdicts,
recorded model outcomes and citation flags. Snapshot bodies, reasons, reviewer
IDs and citation IDs stay in the private review records. Keep the original
review records and the export, freeze and join receipts together. File hashes
guard against accidental changes and label mix-ups. They depend on the operator
maintaining the private records and opening the citation review after verdict
freeze. Development and holdout use separate packets. Preserve the unchanged
development row order when assembling the dataset for a frozen holdout gate.

## Verification

Run the focused helpers with:

```sh
cargo test --locked -p doxa-fleet --example review_evaluation
python3 -m unittest discover -s scripts/fleet-evaluation -p 'test_*.py'
```

## Aggregate agreement arithmetic

`agreement.py` computes unweighted Cohen's kappa from private aggregate counts.
The operator assembles these counts from the original paired, independent
review records; the helper cannot verify that assembly or the reviewers.
Each JSONL row supplies one field and its directed count matrix, with reviewer A
on rows and reviewer B on columns. The exact schema is:

```json
{"version":1,"field":"risky","classes":["false","true"],"counts":[[40,10],[5,45]]}
```

These are artificial counts illustrating 85/100 observed agreement, 1/2 chance
agreement and kappa 7/10. They supply no human labels or quality evidence.
Save the file as an owner-private (`0600`) regular file, then run:

```sh
python3 scripts/fleet-evaluation/agreement.py /absolute/private/counts.jsonl
```

The six binary fields are `within_assignment`, `asks_for_authority_change`,
`contains_instructions_for_recipient`, `likely_secret`, `needs_human_review` and
`risky`, each with exactly the displayed class order. Field `checkpoint` uses
`["aligned","uncertain","drifted","blocked"]` and a 4×4 matrix. A file may
contain at most one row per field. Counts are nonnegative integers, with at
most one million paired observations per field; booleans and fractional counts
are refused. Inputs use the shared 2 MiB private-file limit. Duplicate JSON
fields, symlinks, hardlinks and group/other-readable files are refused.

The report retains the exact input hash, original matrices, class marginals,
observed/chance agreement and point kappa as a float and exact fraction. Empty
samples and chance agreement equal to one have null kappa with an explicit
reason. No threshold, confidence interval or dataset acceptance is computed.
Human pairing, authorship, blinding, missing labels and representative collection
remain unverified; preserve those records with the draft label guide.
