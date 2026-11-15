# Jev routing quality: experiment plan

Status: **plan for owner review; nothing run.** This plan turns the open
[Jev router](jev-router.md#open-work) items "worker task success, latency and
aggregate execution cost" and "representative routing mistakes and
direct-selection baselines" into a measurement. It uses the implemented
[representative routing repair benchmark](../representative-routing-benchmark.md)
and follows its matched evaluation protocol. Everything marked **owner decision**
must be frozen before any holdout result is opened.

## Contents

- [Questions](#questions)
- [Why a counterfactual matrix](#why-a-counterfactual-matrix)
- [Arms](#arms)
- [Design](#design)
- [Metrics](#metrics)
- [Pre-registered decision rules](#pre-registered-decision-rules)
- [Statistical power and its limits](#statistical-power-and-its-limits)
- [Budget](#budget)
- [Prerequisites](#prerequisites)
- [Phases and stop rules](#phases-and-stop-rules)
- [Threats to validity](#threats-to-validity)

## Questions

1. **Success.** Does Jev-routed repair succeed as often as always using the
   strongest candidate?
2. **Cost.** At that success rate, how much execution cost does routing save,
   counting Jev's own decision cost?
3. **Mistakes.** How often does Jev choose a candidate that fails when another
   would have succeeded (*avoidable failure*)? How often does it overspend on a
   task a cheaper candidate solved (*overspend*)?
4. **Latency.** What decision latency does routing add, and how does the
   end-to-end wall clock compare?

A router matching a difficulty label is not evidence it chose a successful
repair. Only graded repairs answer these questions.

## Why a counterfactual matrix

Running every candidate on every task produces an **outcome matrix**: success,
tokens, cost and latency per (task, candidate, repetition). Any routing policy
that picks one candidate per task at task start can then be scored by looking up
its choice's outcome in the same repetition. All policies are evaluated on
identical draws, so their comparisons are paired, and a new policy costs only
its decision calls. The benchmark protocol already requires this full matrix
(three candidates × 18 tasks × three repetitions = 162 trials).

The lookup assumes routing happens once per task. DOXA routes *per turn*, so a
multi-turn repair could switch models midway. A separate, smaller **live-routed
arm** checks whether the counterfactual estimate holds when Jev actually drives
a Router session.

## Arms

| Arm | Policy | Source |
| --- | --- | --- |
| F0 | Always `deepseek-flash`, thinking `none` | Benchmark baseline |
| F1 | Always `deepseek-flash`, thinking `high` | Benchmark baseline |
| P | Always `deepseek-v4-pro`, thinking `high` | Strongest fixed arm |
| M | Provisional difficulty mapping (easy→F0, intermediate→F1, difficult→P) | Declared before results; uses benchmark difficulty labels Jev never sees |
| J | Jev 1.13.0 selection from the frozen candidate descriptions | Router under test |
| S | SubQ Snap Preview selection (optional) | Second router |
| O | Per-task oracle: cheapest candidate that succeeded in that repetition | Upper bound, not a policy |

M is a strong baseline: it knows the difficulty labels. If Jev cannot beat M on
cost-adjusted success, routing is not yet adding value over a static rule.

## Design

- **Corpus and splits.** Use the frozen 18-task corpus with its family-level
  development/holdout split; related variants never cross splits. Jev's
  candidate descriptions, the Router config and any prompt changes are tuned on
  development only. **Owner decision:** the holdout family assignment, recorded
  with the corpus hash before tuning starts.
- **Execution.** Every candidate gets the same editing tools, public tests,
  context, wall-clock budget, repair-attempt budget and output allowance: 32,768
  generated tokens per arm, with at least 16,384 for thinking where thinking is
  enabled. Each trial is a real DOXA API-worker session in `docker-offline`
  isolation, so no network leaks into the repair. Grading uses the benchmark's
  offline Docker grader; evaluator-only cases never reach a prompt.
- **Repetitions.** Three independent repetitions. Infrastructure failures, caps
  and timeouts are recorded separately from functional failures and are not
  silently retried.
- **Jev decisions.** One Jev decision per (task, repetition), from the task text
  and frozen candidate descriptions only, with no difficulty label. The decision
  request, response, usage and latency are journalled with the trial.
- **Live-routed validation.** A real `/model auto` Router session runs each
  holdout task once. Its success and cost are compared with arm J's counterfactual
  value for the same task. A disagreement beyond repetition noise invalidates
  the counterfactual claims for multi-turn work.

## Metrics

Per trial:
- all-tests success and per-requirement pass;
- input, output and thinking tokens as reported;
- cost under the registry's source-dated price bounds and, where a provider
  exposes it, the billed amount;
- decision latency and execution wall clock;
- repair attempts used, cleanup result and infrastructure status.

Per policy, over holdout tasks × repetitions:
- **success rate** and **mean cost per task**, including router decision cost;
- **cost per success** (total cost ÷ successes);
- **avoidable-failure rate**: the policy failed but some candidate succeeded in
  that repetition;
- **overspend**: on tasks the policy solved, the cost above the oracle's
  cheapest success;
- **regret vs oracle** on cost-adjusted success;
- decision-latency p50/p95 and end-to-end wall clock.

Variation is reported per task across repetitions. Confidence intervals come from
a paired bootstrap over tasks, resampling whole tasks with all their repetitions.

## Pre-registered decision rules

**Owner decision:** freeze these thresholds before the holdout. The values below
are proposals, not defaults.

- **Non-inferiority:** J's success rate is no more than 10 percentage points
  below P's, judged by the lower bound of the paired 90% interval of J − P.
- **Savings:** J's mean cost per task, including decisions, is at most 60% of P's.
- **Beats the static rule:** J's cost per success is lower than M's, with the
  interval excluding zero. Otherwise report "no evidence routing beats the
  difficulty mapping".
- **Mistake ceiling:** J's avoidable-failure rate is at most 15%.

All four passing supports a claim of measured routing value *on this corpus*. A
failure is reported as the result, not tuned away; a new attempt needs a fresh
holdout.

## Statistical power and its limits

Eighteen tasks with roughly six in the holdout is small. Repetitions measure
stochastic variation, not task diversity, and do not multiply the effective
sample. With six holdout tasks, only very large differences, such as one arm
solving every task the other fails, can separate from noise. Phase 1 therefore
reports **estimates with intervals, not verdicts**, unless a rule is met with
margin.

To make the decision rules meaningful, extend the corpus to **at least 60 tasks
in about 20 families** (the builder supports new families), keeping the same
difficulty definitions, before the decisive run. The consented real-case arm
from the Jev open work is a separate external-validity check. It is not a
substitute for the controlled corpus.

## Budget

Upper bounds in tokens; convert with `doxa model-facts` price bounds at run time,
because prices change.

| Item | Trials | Generated tokens (upper bound) |
| --- | --- | --- |
| Outcome matrix, 18 tasks | 162 | 162 × 32,768 ≈ 5.3 M |
| Outcome matrix, 60-task extension | 540 | ≈ 17.7 M |
| Jev decisions | 54 (or 180) | about 0.2 k each; negligible |
| Live-routed validation | 6–20 | ≤ 0.7 M |

Input tokens depend on repository context and attempt count. Record them, and
cap them per trial with the existing API budget admission, which refuses an
unknown priced bound. Set a hard total allowance for the run before it starts.

## Prerequisites

1. **Candidate runner.** A harness that prepares a benchmark workspace, starts a
   real DOXA API-worker session with a pinned model and effort, gives it the
   task text and attempt budget, then hands the result to the existing grader.
   It journals usage, cost and latency. This is the only new component.
2. **Router decision capture.** Journal Jev decisions without executing a worker,
   for the counterfactual arms. The Router already records decisions; it needs a
   decision-only mode or an extraction from journals.
3. **Analysis script.** Builds the outcome matrix, scores the policies, runs the
   paired bootstrap and prints the decision-rule table. It fails closed on
   missing trials.
4. A rootless Docker engine and the pinned grader image, verified by the
   benchmark's nonbillable `self_check.py`.

## Phases and stop rules

| Phase | Work | Exit |
| --- | --- | --- |
| 0 | Build runner and analysis; nonbillable self-check; one F0 dry run on one development task | Grader, journals and cost capture verified end to end |
| 1 | Development split, 1 repetition, all arms | Tune candidate descriptions; freeze config, splits and decision rules |
| 2 | Holdout, 3 repetitions, full matrix + J/S decisions | Decision-rule table |
| 3 | Live-routed validation on holdout | Counterfactual agreement check |
| 4 | Report under `docs/qa/`; update the Jev plan's open items | Owner review |

**Stop rules:** halt if the run's allowance is reached, if infrastructure
failures exceed 10% of trials (fix, then restart that phase), or if any trial
shows network access from `docker-offline`.

## Threats to validity

- **Stochastic outputs.** Providers don't guarantee seeds. Three repetitions
  bound, but don't remove, the variation.
- **Provider drift.** A model name can change behaviour over time. Record model
  IDs, dates and response metadata, and run each repetition close in time.
- **Leakage.** Jev must not see difficulty labels, evaluator cases or other
  arms' results. Candidate descriptions are frozen from development only.
- **Narrow domain.** Small Python repair repositories don't represent UI work,
  long features or other languages. Claims stay scoped to this corpus.
- **Cost evidence.** Registry price bounds are estimates. Billed amounts are
  used where providers expose them, and reported separately.
- **Not alignment evidence.** These task oracles are not human labels and cannot
  feed the [supervision accuracy gate](fleet-supervision-accuracy-experiment.md).
