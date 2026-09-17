# Does hierarchy emerge among peer agents, or do we always put it there?

**Status: plan for review. Nothing implemented.** The messaging substrate this
rests on (`peer_list` / `peer_send` / `peer_history`, the ledger, the browser
graph) is itself unbuilt — see the design notes that precede this. This
document is the experiment those tools exist to make possible, written first
so the instrument is built for the measurement rather than the reverse.

## The question

Give N agents one task and no assigned roles. Does a coordinator appear?

It is worth asking because every multi-agent system in production today
*imposes* hierarchy: one orchestrator, N workers, edges defined by the spawn
tree. That is the shape this very session has — a star, by construction. What
nobody in that arrangement can tell you is whether the structure was necessary
or merely convenient.

## Why the obvious setup answers the wrong question

The natural design — hand one agent the task, tell it there are idle peers,
let it negotiate — produces hierarchy essentially always, for a reason that
has nothing to do with emergence. That agent alone knows what the work is.
The others cannot negotiate until they are told, so the informed agent
coordinates by default. What that measures is the decay of an initial
asymmetry.

**Every participant therefore receives the same task description at the same
moment, and none is privileged in any way.** If a coordinator appears out of
that, it appeared through interaction.

## Design

### Conditions (2 × 2, fully crossed)

| | pairwise only | broadcast available |
|---|---|---|
| **homogeneous** (all one model) | A | B |
| **mixed vendors** (randomised) | C | D |

**Broadcast is the primary manipulation, not a convenience.** Coordinating N
agents pairwise costs O(N²) messages, and a hub is the cheapest way to cut
that — so hierarchy under pairwise-only may be nothing but a routing
response to a constraint we imposed. If structure appears in both columns,
something else drives it. If only in the left, it is an artefact of our own
topology choice, which is a finding worth publishing on its own.

**Mixed vendors is the control for correlated priors.** N copies of one model
are not N independent agents: they share training, phrasing and failure
modes. Homogeneous self-organisation cannot be distinguished from shared
prior. Column C/D breaks that correlation. Model is assigned to agent
**randomly per run**, so role cannot be confounded with capability.

### Scale

**N = 32**, five replications per cell, four cells = **640 agent-sessions**.

Thirty-two is small for degree-distribution claims and adequate for role
emergence, which is the actual question. Five replications is the floor for
saying anything about stability of role across runs.

### Task

One task, fixed across all runs, chosen so that it (a) genuinely decomposes,
(b) has a checkable end state, and (c) cannot be finished by one agent inside
the run budget. A repository-wide mechanical refactor with a test suite as
the oracle fits all three. The task must not name roles, phases, or suggest a
division of labour.

## What is measured

**Primary — role emergence.** Does any agent acquire a coordinating position?
Operationalised before any data is collected, not after:
- out-degree and in-degree per agent over time
- betweenness centrality (are messages routed *through* someone)
- the fraction of work assignments originating from a single agent

**Secondary — stability.** Across the five replications of a cell, is it the
same agent, the same *position*, or neither? Three different answers:
- same agent every time → something about identity or ordering selects it
- a coordinator every time but a different one → the role is real, the
  occupant arbitrary; the strongest result available here
- no coordinator → flat organisation, a real answer

**Tertiary — efficiency.** Task completion, wall clock, total tokens, and
messages per unit of work done. A hierarchy that forms and does not help is
a different finding from one that does.

## Baselines, because "structure" needs something to be structure *against*

Every observed network is compared to two nulls with the same node and edge
count:
1. **Erdős–Rényi random graph** — is the observed topology distinguishable
   from arbitrary at all?
2. **Star** — is it distinguishable from the imposed hierarchy we already
   know how to build?

If the observed graph separates from neither, **there is no emergence to
report**, and that is written up as the result rather than explained away.

## Confounds, named in advance

**Latency decides who speaks first.** In a negotiation among equals, whoever
answers fastest frames what the others respond to. A faster vendor could
acquire the coordinator role for reasons unrelated to reasoning, and it would
look exactly like emergence. Mitigation: record per-message latency and the
responding model; test directly whether role correlates with speed; if it
does, re-run with an artificial response floor equalising round-trip time and
report whether the structure survives.

**Capability, not just model.** If some agents hold tools others lack,
coordination differences are capability artefacts. `EngineCapabilities`
(17 fields, conservative defaults) is the instrument: pin an identical
capability surface across every participant regardless of vendor, and record
it, so the paper can state the agents differed in model and in nothing else.

**Observability changes behaviour.** `peer_history` lets an agent see the
network and adapt to it. Whatever forms is therefore partly self-fulfilling.
This is a property of the system, not contamination — but it is stated, and
a no-history arm is worth running if the primary result is positive.

**The prompt is an intervention.** "Negotiate as equals" already suggests
negotiation. Task text is fixed, published verbatim in the paper, and
identical across cells.

## Resources, measured not estimated

| | |
|---|---|
| live CLI process, resident | 425–545 MB (measured) |
| per session incl. daemon | ~600 MB |
| N=32 | ~19 GB — trivial on the 192 GB workstation, at the edge on the 30 GB laptop |
| cores | not binding; sessions are API-bound, not CPU-bound |

**Quota is the real constraint, not hardware.** Participants run on cheap
tiers: the question is whether structure emerges, not whether the work is
done brilliantly. Sampling weights the inexpensive vendors heavily and
includes costly ones at low probability, so the mix stays honest without the
bill scaling with N.

**Broadcast needs a hard fan-out cap or it is unusable at this N.** One
broadcast is 31 deliveries; a full reply-broadcast round is 992 messages,
~4.5 MB of ledger at the measured mean. Ten rounds is 45 MB per run. The rate
limit must count deliveries, not calls, and **a broadcast must never start a
turn** — only queue — or one message wakes the entire fleet at once.

## What the ledger must record, decided now because it cannot be backfilled

Per message: sender, recipients, timestamp at sub-second resolution,
in-reply-to, the body (full, scrubbed through `scrub_secrets`), the sending
model, the responding latency, and the turn context it arrived in.

Full bodies, not truncated: the content is what distinguishes a coordination
message from a status ping, and that distinction is the measurement.

## Prerequisites

1. `peer_list`, `peer_send`, `peer_history`, rate-limited on the send side.
2. Broadcast, with fan-out counted against the limit and no turn-starting.
3. The ledger, with the fields above.
4. Additional engine providers for the mixed-vendor arms.
5. A harness: spawn N sessions, deliver the identical prompt simultaneously,
   collect the ledger, tear down.

## What a reviewer will attack, and the honest answer

One machine, one task domain, one orchestration tool, N=32. These are real
limits. The framing that survives them is a **systems contribution with an
empirical study attached** — real agents doing real work in real repositories,
instrumented by a production tool rather than a purpose-built simulator —
rather than a claim about language models in general.

The hypothesis is falsifiable and the null is publishable. That is the part
worth protecting.
