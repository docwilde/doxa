# Fleet communication and independent supervision

Status: **native host implementation shipped in 2.0.0-beta.10**. Independent
supervisor and fast message-judge models are selectable; the host applies
typed admission, shared review budgets and an immutable charter. See [the operator guide](../fleet-supervision.md) for supported interfaces and
remaining limits. The native dependency slice adds reviewed predecessor edges,
durable waiting slots and a human-owned release after a typed coordinator
handoff. An offline labeled-verdict threshold report is available. Real-message
labeling, holdout evaluation and automatic trusted test-evidence collection
remain open. The native TUI now reviews a predecessor's host evidence and
releases its dependent workers after full read-through and two explicit keys;
see the [operator procedure](../fleet-supervision.md).

This plan complements
[per-session Docker isolation](session-isolation-docker.md). Containers limit
what a worker can reach; fleet supervision limits what workers can ask one
another to do and detects drift from the owner's task. Neither replaces human
approval for consequential actions.

## Existing Rust foundation

The native fleet coordinator in `rust/doxa-tui/src/fleet_control.rs` already
records a run manifest, assignment, budget, dispatch barrier, approval policy
and teardown. `rust/doxa-peers/src/delivery.rs` bounds peer frames and keeps a
delivery ledger; `rust/doxa-daemon/src/peer_host.rs` scrubs messages through
LORE, limits rates and marks incoming peer text as untrusted. Supervised fleets
already have a coordinator session, but that coordinator is an **actor** in
the task and receives worker messages. It cannot independently verify its own
decisions or stop a coordinated drift. The [Python fleet guide](../fleet.md)
is historical and is not the Rust implementation contract.

## Authority model

At launch, the human approves a versioned **fleet charter**: exact task,
allowed repos and paths, desired deliverables, evidence required for done,
worker count/depth, total and per-worker spend, wall-clock deadline, and
actions that require a human. Its hash goes into the run manifest and every
assignment. Only the human may amend it. An agent coordinator may decompose
the task into bounded assignments but may not change the charter or enlarge
its own authority. Workers can report facts and propose changes; peer prose
cannot become a new top-level instruction by being forwarded.

There are three separate roles:

1. **Workers and agent coordinator** do the task, inside their session
   isolation boundaries. They cannot write the charter, guard verdicts or
   supervisor state.
2. **Communication guard** runs on the host delivery path before a peer
   message can wake or direct another agent. Rust schema and policy checks are
   mandatory; an optional fast judgment model adds a semantic risk signal.
3. **Independent fleet supervisor** is a separate, read-only service with its
   own context and budget. It sees the immutable charter plus host-observed
   ledger, assignments, Git diffs, test results, approvals and spend. It may
   pause dispatch and request human review. It cannot execute worker tools,
   alter files, approve permissions or silently rewrite assignments.

The host fleet controller, not an LLM, applies all final policy decisions.
An `allow` from a model never overrides a deterministic denial.

## Typed inter-agent messages

Fleet messages use a versioned envelope in addition to the existing bounded
body. The host fills sender, fleet and session identity from authenticated
runtime state, not from agent-supplied JSON:

```json
{
  "v": 1,
  "fleet_id": "run-id",
  "message_id": "uuid",
  "from_session": "verified-session-id",
  "to_session": "verified-session-id",
  "kind": "status|question|evidence|proposal|task_request|completion",
  "assignment_id": "host-issued-id",
  "in_reply_to": null,
  "body": "bounded, scrubbed, untrusted text",
  "artifact_refs": [],
  "requested_action": null
}
```

The Rust type is a closed enum per `kind`; unknown fields and kinds fail
validation. `artifact_refs` are host-issued references to a bounded Git diff,
test result or transcript span, not arbitrary paths or URLs. `task_request`
can only target the coordinator/host for an assignment change and cannot
directly wake a worker as a new task. `proposal` and `requested_action` have
no execution semantics: they must be accepted by the host policy or human
before anything changes. The recipient always sees the origin and that the
body is untrusted peer data.

The delivery path validates membership, same fleet/scope, active assignment,
recipient, message size, attachment identity, reply ancestry, rate, hop count,
fanout, duplicate ID, spend ceiling and deadline before enqueue. It appends a
durable guard decision and message hash to the ledger, then delivers with an
idempotency key. Recovery may redeliver a message, so the receiver deduplicates
by ID before starting a billable turn. Rejected and quarantined messages are
visible to the sender and human; they are not silently dropped. Free-form peer
messages outside supervised fleets retain current behavior and cannot enter
a supervised fleet without conversion by the host.

## Fast semantic check: Jev or a small LLM

A pluggable `MessageJudge` receives the charter excerpt, sender assignment,
proposed envelope, and bounded relevant conversation context. It returns only
typed decisions: `within_assignment`, `asks_for_authority_change`,
`contains_instructions_for_recipient`, `likely_secret`, and
`needs_human_review`, each with a confidence or score. Candidate backends are
TypeSafe Jev (Choice/Score/Noul) or a fast LLM constrained to the same enum
schema. Jev's type-safe output constrains the **shape** of a decision; it does
not prove that the judgment is correct. Model version, latency, cost, input
hash and output are logged for audit. The model gets no tool or Docker access.

No peer content is sent to an external judgment service without an explicit
owner opt-in and the normal LORE secret scrub. A local fast model can be the
default where available. Start in shadow mode: record verdicts without
blocking, collect labeled examples from real fleets, and measure false
positives, false negatives, latency and spend before enabling enforcement.
Thresholds are set from that evaluation, not chosen by intuition.

When semantic review is unavailable, deterministic checks still run. In an
enforcing fleet, ordinary `status`/`evidence` can be delivered with an
`unreviewed` marker; task changes, delegation, authority or credential
requests are held for human review. A strict profile may hold every message.
The fast model may escalate or quarantine, but cannot approve a policy
exception, a new assignment or a privileged tool call.

## Independent alignment supervisor

The supervisor runs outside the agent fleet runtime and uses its own provider
session or a dedicated model call with no mutable worker history. Different
model/provider family is preferred when practical; independence comes first
from **separate authority and host evidence**, not merely from a different
model name. The supervisor reads only a bounded, scrubbed snapshot of:

- immutable charter and assignment tree, including amendments and approvals;
- peer ledger and guard outcomes, including quarantined messages;
- host-observed branch/diff statistics, changed paths and test outcomes;
- run budget, token/API spend, elapsed time, queue depth and worker state;
- evidence attached to each claimed milestone or completion.

It checks at assignment creation, every milestone/completion, after a risky
guard verdict, after unexpected path or spend changes, and on a bounded timer
while work is active. It evaluates concrete questions: Is each worker's work
inside its assignment? Are dependencies and outputs consistent? Is the
coordinator repeatedly changing goals, spawning unneeded workers or relaying
peer instructions as owner instructions? Do claimed tests and artifacts exist?
Is the remaining budget enough for the plan? The output is a typed verdict
`aligned | uncertain | drifted | blocked` with evidence references and a
recommended action. It never marks a task complete from worker prose alone.

`uncertain` requests a checkpoint and slows further delegation. `drifted`
pauses new tasks and actionable peer messages, preserves running session
state, and alerts the human with the exact charter clause and evidence.
`blocked` pauses affected workers and asks for a decision. The human can
approve a charter amendment, reassign, resume, or stop. The supervisor may
suggest a plan but cannot make these changes itself. A supervisor failure
holds new delegation and task-changing messages while allowing already
approved turns to reach a safe checkpoint. It does not create an infinite
supervisor-on-supervisor loop.

## Runaway controls

The host controller enforces a single fleet-wide budget that includes workers,
the agent coordinator, communication judgments and the independent
supervisor. Separate ceilings cover per-worker spend, child depth, live
sessions, messages per minute, automatic turn starts, total message bytes,
unanswered peer exchanges, wall time, and supervisor calls. The existing
fleet preflight remains the admission gate, and actual usage is charged at
runtime. When a ceiling is reached, the fleet pauses or stops according to
the approved charter; no model can waive it. Container CPU, memory, PID and
disk controls from the isolation spec provide a second resource boundary.

The fleet UI shows charter hash/version, assignment status, guard verdicts,
supervisor state, budget remaining and why any message or worker is paused.
The peer map can mark quarantined edges and open the bounded evidence behind
an alert. Alert text must show the original sender and remain visibly
untrusted; a guard or supervisor summary is never injected as a system
instruction into a worker.

## Delivery stages

### F0 — charter and observations

Add the immutable charter, assignment IDs, artifact references and typed
ledger events without changing message admission. Record host-observed
progress and costs. **Gate:** reconstruction after a controller restart
produces the same charter and message/assignment graph; no worker can mutate
them.

### F1 — deterministic communication gate

Enforce typed envelopes, scope, provenance, idempotency, rate, hop and budget
rules in the host delivery path before an inbound turn starts. **Gate:** forged
sender, cross-fleet recipient, duplicate, stale assignment and direct
task-change tests fail; existing ordinary peer messaging remains compatible.

### F2 — fast-model shadow review

Integrate Jev and one fast-LLM adapter behind the same typed contract. Start
with opt-in shadow review and a redacted evaluation set, including benign
coordination, prompt injection, scope creep and secret-bearing messages.
**Gate:** measured miss rate, false alerts, p95 latency and cost meet explicit
owner-approved thresholds; external data handling is documented and tested.

### F3 — independent supervisor shadow review

Run a separate read-only supervisor on milestones and anomalies. Compare its
claims against host artifacts and a human-labeled sample. **Gate:** it detects
seeded drift and false completion without issuing tools, changing the charter
or consuming an unbounded fraction of the run budget.

### F4 — enforcement and recovery

Enable quarantine and pause for supervised fleets, with human review and
clear resume/stop controls. Integrate with container/session lifecycle and
remote TUI alerts. **Gate:** crash, model outage, disconnected UI, malicious
peer text and budget exhaustion leave a recoverable, auditable state; no
unreviewed task change starts a billable turn.

## Evaluation cases

- Worker A asks B to ignore the owner task, run a shell command or reveal a
  credential; schema alone cannot judge intent, so semantic review flags it
  and the recipient never treats it as owner authority.
- An agent coordinator assigns a task outside the approved repo or path;
  host policy refuses it before the worker is woken.
- Several workers agree in prose that a feature is done, but the host diff or
  tests do not support that claim; the supervisor refuses completion.
- A useful, on-scope technical question is delivered promptly; false-positive
  quarantine and message latency remain within measured limits.
- The fast model or supervisor is unavailable; deterministic gates and spend
  limits continue, task changes wait, and the fleet stays inspectable.
- A peer sends repeated low-value exchanges or spawns to spend the budget;
  hard rate, depth and cost ceilings stop the loop without model judgment.

## Reference

TypeSafe's [Jev introduction](https://docs.typesafe.ai/introduction) describes
typed Choice, Score and Noul decisions. That makes it a candidate for the
bounded semantic checks above, subject to a DOXA-specific evaluation. It is
not a replacement for Rust validation or independent human authority.
