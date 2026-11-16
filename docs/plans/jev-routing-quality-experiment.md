# Jev routing quality: experiment plan

Status: **plan for owner review; nothing run.** This plan turns two open
[Jev router](jev-router.md#open-work) items into a measurement: "worker task
success, latency and aggregate execution cost", and "representative routing
mistakes and direct-selection baselines". Results must be **comparable with
published agent-benchmark numbers**, so the held-out evaluation uses a standard
public task set instead of DOXA's own tasks. Everything marked **owner
decision** must be frozen before any held-out result is opened.

## Contents

- [Questions](#questions)
- [Task sets](#task-sets)
- [Why the harness must be held fixed](#why-the-harness-must-be-held-fixed)
- [Arms](#arms)
- [The routed endpoint](#the-routed-endpoint)
- [Design](#design)
- [Metrics](#metrics)
- [Pre-registered decision rules](#pre-registered-decision-rules)
- [Statistical power](#statistical-power)
- [Budget](#budget)
- [Prerequisites](#prerequisites)
- [Phases and stop rules](#phases-and-stop-rules)
- [Threats to validity](#threats-to-validity)
- [References](#references)

## Questions

1. **Success.** Does Jev routing between configured models solve standard agent
   tasks as often as always using the strongest model?
2. **Cost.** At that success rate, how much does routing save, counting Jev's
   own decisions?
3. **Comparability.** Are the numbers on the same scale as published leaderboard
   results, so DOXA's routing can be placed against them?
4. **Latency.** How much time do routing decisions add, per call and per task?

## Task sets

| Role | Task set | Why |
| --- | --- | --- |
| **Held-out, primary** | **Terminal-Bench 2.0**: 89 tasks across 16 categories and three difficulty tiers, each checked by three human reviewers | It is the agentic benchmark the candidates' own vendors report. DeepSeek's V4 paper and Z.ai's GLM-5 papers both report Terminal-Bench 2.0. Each task is a Docker container with a human-written oracle solution and tests on the final container state. It has an official harness (Harbor), a reference agent (Terminus-2) and a public leaderboard. |
| Held-out, secondary (optional) | **SWE-bench Pro**, public set: 731 instances from 11 repositories | Issue resolution in real repositories, which Terminal-Bench does not cover. OpenAI recommends it over SWE-bench Verified, which it stopped reporting in February 2026 because of contamination and flawed tests. Public images and an MIT-licensed harness. |
| Development only | DOXA's [18-task repair benchmark](../representative-routing-benchmark.md) | Cheap, offline and graded locally. Used to build and debug the routed endpoint and to tune candidate descriptions. It is machine-authored with no human labels, so it **never supplies a reported result**. |

Terminal-Bench 2.0 is never used for tuning. Held-out tasks are not opened
before the configuration is frozen, which keeps the comparison honest.
**SWE-bench Verified is not used:** it is widely reported as saturated and
contaminated.

## Why the harness must be held fixed

Published agent scores depend heavily on the harness. On SWE-bench Pro, the
same Opus 4.5 model scored 45.9% under SWE-agent and 49.8–51.8% under other
agents. Running DOXA's own agent on Terminal-Bench would therefore measure
DOXA's harness and its routing at once, and would not be comparable with any
leaderboard row.

The design keeps the **official reference agent, Terminus-2 in Harbor, for every
comparable arm**, and varies only which model answers each call. Any difference
between arms is then the routing, measured on the same scale as published
Terminus-2 rows. DOXA's own harness is an optional, separately labelled arm.

## Arms

All comparable arms run Terminus-2 in Harbor under the official rules: five
trials per task, and no timeout or CPU, memory or storage overrides.

| Arm | Model behind Terminus-2 | Purpose |
| --- | --- | --- |
| **A0, anchor** | One fixed candidate that has a published Terminus-2 Terminal-Bench 2.0 score | **Checks the setup**: our run must reproduce the published number before anything else counts |
| F-cheap | Always the cheapest candidate, for example `deepseek-flash` without thinking | Cost floor |
| F-mid | Always the mid candidate, for example `deepseek-flash` with high thinking or a GLM model | Middle baseline |
| F-strong | Always the strongest candidate, for example `deepseek-v4-pro` with high thinking | Quality ceiling among candidates |
| **J** | Jev 1.13.0 choosing a candidate on **every call**, through the [routed endpoint](#the-routed-endpoint) | Routing under test |
| S (optional) | SubQ Snap 1.2 through the same endpoint | Second router |
| H (optional) | DOXA's own API-worker harness as a Harbor custom agent, with Jev routing | Harness effect. **Not comparable** to leaderboard rows; reported separately |

**Owner decision:** the exact candidate list, model IDs and thinking settings.
The candidates must be configured identically in the fixed arms and in Jev's
candidate config. A0 is preferably the F-strong model, if its vendor published a
Terminus-2 score under the official rules.

## The routed endpoint

This is the main new component: an **OpenAI-compatible endpoint inside DOXA**
that Terminus-2 calls as if it were a single model.

- Each request is one routing turn. The endpoint passes the conversation to the
  Router's existing decision path with the frozen candidate config. It then
  forwards the conversation, tools and sampling controls to the chosen candidate.
  It substitutes only the selected model ID and the candidate's frozen,
  provider-supported effort fields; the corresponding fixed arm uses those
  same fields. It returns the candidate's response unchanged. The journal
  records the exact permitted substitutions and rejects every other rewrite.
- Pinning is off. Jev sees the conversation as the Router normally does, and
  nothing about the benchmark task: no task ID, category or difficulty.
- Every call journals the decision, the candidate chosen, tokens, cost under the
  registry's price bounds, decision latency and call latency. These are the
  same fields the Router already records.
- Budget admission works as it does today: a call with an unknown priced bound
  is refused, and that refusal is recorded as an infrastructure failure, not
  silently retried.
- The endpoint chooses a frozen model/effort pair. It never changes the
  conversation, tools, token allowances or harness resource limits. Candidate
  configuration is not permission to enlarge the official harness allowance.

## Design

- **Splits.** All 89 Terminal-Bench 2.0 tasks are held out. Development uses the
  in-house corpus and, for plumbing only, Harbor's example tasks. **Owner
  decision:** whether a few Terminal-Bench tasks may be used to smoke-test the
  endpoint. If yes, they are listed and excluded from the reported results.
- **Anchor first.** Run A0 before any other held-out arm. If the reproduced
  score misses the published number by more than the tolerance (see
  [decision rules](#pre-registered-decision-rules)), stop and fix the setup.
- **Two stages.** Stage 1 runs every arm with **one trial per task** (89 runs
  per arm), to measure pass rates, token use and cost. Stage 2 runs the
  **decisive comparison**, J against the best fixed arm (and S if run), under
  the official five trials. Other arms may stay at one trial if the budget
  requires it.
- **Paired comparison.** All arms run the same tasks with the same versions and
  resource limits, close together in time, so results can be compared task by
  task.
- **Network.** Terminal-Bench decides per task whether a container has network
  access; DOXA's `docker-offline` policy doesn't apply inside the benchmark. The
  routed endpoint itself runs on the host and is the only route to model
  providers.
- **Official set, then the fork.** Report the official Terminal-Bench 2.0 set
  for comparability. Z.ai's "Terminal-Bench 2.0 Verified" fork fixes
  environment issues; if run, report it separately and never mix it with
  official results.
- **SWE-bench Pro (optional, after Terminal-Bench).** A stratified sample of
  about 100 public instances across the 11 repositories, with mini-swe-agent as
  the fixed harness (the harness behind most rows on Scale's leaderboard). Use
  the same arms and endpoint, and the same anchor rule against a published
  mini-swe-agent score. Task content comes from copyleft repositories, so none
  of it is committed into DOXA.

## Metrics

Per trial: pass or fail by the benchmark's tests; tokens; cost (registry price
bounds, plus billed amounts where a provider exposes them); wall clock; number
of calls; and, for routed arms, the model chosen on each call and the decision
latency.

Per arm:
- **Accuracy**: the share of trials passed, the leaderboard's own metric, with a
  95% interval from bootstrap over tasks. Per-category accuracy across the 16
  categories is reported too.
- **Mean cost per task** and **cost per solved task**, including decision cost.
- **Model mix**: for routed arms, how often each candidate was chosen, overall
  and by task category.
- **Decision overhead**: decision latency p50/p95 per call, and decision time as
  a share of task wall clock.

Comparisons:
- **J minus F-strong** and **J minus F-cheap** in accuracy, paired by task.
- **Cost ratio** J to F-strong.
- **Routing headroom**: the tasks some fixed arm solved that J missed, and the
  tasks J solved that no fixed arm did. There is no per-call oracle, so routing
  mistakes are reported this way, at task level.

## Pre-registered decision rules

**Owner decision:** freeze these before the held-out stage. The values are
proposals, not defaults.

- **Anchor validity.** A0's accuracy over five trials must fall within ±5
  percentage points of its published Terminus-2 score, or its 95% interval must
  contain that score. Otherwise no other arm is reported as comparable.
- **Non-inferiority.** J's accuracy is no more than 5 points below F-strong's,
  judged by the lower bound of the paired 90% interval of J minus F-strong.
- **Savings.** J's mean cost per task is at most 60% of F-strong's.
- **Better than always cheap.** J's accuracy is above F-cheap's, with the
  paired interval excluding zero. Otherwise routing has not shown it adds value
  over always using the cheap model.

All four passing supports a claim of measured routing value **on Terminal-Bench
2.0 with Terminus-2**, which outsiders can check against the leaderboard. A
failure is reported as the result. A new attempt needs a new frozen
configuration, and the results must be labelled as a second look at the same
public tasks.

## Statistical power

Eighty-nine tasks with five trials each gives far more power than the 18
in-house tasks. Trials of one task are correlated, though, so the effective
sample size is closer to the number of tasks than to the number of trials.
**Compute the minimum detectable difference from stage 1**, using how often
two arms disagree on the same task. The 5-point non-inferiority margin is only
meaningful if the experiment can resolve a difference of that size. Stage 1 is an
interim look at the same public tasks: it cannot justify widening the success
margin for the confirmatory comparison. If the frozen design lacks power,
report that limit, or register a separate experiment with fresh tasks. Keep
SWE-bench Pro results separate from the primary endpoint.

## Budget

The separately authorized [pilot](evaluation-pilot.md) has a $25 ceiling and
uses development tasks only. It does not authorize these full benchmark stages.

Terminal-Bench tasks are long, multi-turn agent runs, so the cost per run is
measured, not guessed.

| Stage | Runs |
| --- | --- |
| A0 anchor, 5 trials | 445 |
| Stage 1: F-cheap, F-mid, F-strong, J (and S), 1 trial | 356 (445 with S) |
| Stage 2: J and F-strong (and S), 5 trials | 890 (1,335 with S) |
| Optional SWE-bench Pro, about 100 instances × 4 arms × 1 trial | 400 |

Stage 1 gives token use per run for each candidate. Turn those into a dollar
estimate with `doxa model-facts` price bounds, and set a **hard allowance per
stage** before it starts. Stop when the allowance is reached, not when the
spending looks reasonable. Jev decisions add about 0.2 thousand tokens per
call; their real share is measured in stage 1.

## Prerequisites

1. **Harbor and Terminal-Bench 2.0** installed against the rootless Docker
   engine. Verify that the Harbor version, the task images and the official
   resource limits work rootless before running A0. If they don't, record why,
   and use a dedicated rootful runner rather than changing limits.
2. **The routed endpoint** described above, with journals and budget admission.
   It is the only new production component, and needs its own tests: that it
   preserves conversation and tools, applies only frozen model/effort substitutions,
   counts budget refusals, and ensures that
   decisions are journalled.
3. **Analysis script**: reads Harbor results and endpoint journals, computes
   accuracy, cost and paired intervals, and prints the decision-rule table. It
   fails closed on missing trials.
4. **Published reference values** for the anchor model, with source URL and
   date, recorded before running A0.
5. Optional: a **Harbor custom-agent adapter** for DOXA's API worker (arm H),
   and the SWE-bench Pro harness with mini-swe-agent.

## Phases and stop rules

| Phase | Work | Exit |
| --- | --- | --- |
| 0 | Build the endpoint and analysis; plumbing runs on the in-house corpus and Harbor examples | Endpoint preserves payloads except frozen model/effort fields; journals and costs verified |
| 1 | Freeze candidates, config and rules; run the A0 anchor, 5 trials | Anchor within tolerance, or stop |
| 2 | Stage 1, all arms, 1 trial | Pass rates, token use, cost per arm; compute power; owner confirms margins |
| 3 | Stage 2, J vs F-strong (and S), 5 trials | Decision-rule table |
| 4 | Optional SWE-bench Pro and arm H | Separate result tables |
| 5 | Report under `docs/qa/`; update the Jev plan's open items | Owner review |

**Stop rules:**
- Halt a stage when its allowance is reached.
- Halt if infrastructure failures exceed 10% of a stage's trials: fix, then
  restart that stage.
- Halt if any run needed a timeout or resource override, because the result
  would no longer be official.

## Threats to validity

- **Contamination.** Terminal-Bench 2.0 has been public since 2025, and newer
  models may have seen it. All arms share that exposure, so the *differences*
  between arms remain fair, but absolute scores are reported with this caveat.
- **Provider drift.** A model name can change behaviour over time, and
  published numbers were measured on earlier versions. The anchor checks the
  current state. Record model IDs and dates, and run the arms close together.
- **Per-call routing in a loop.** Switching models mid-task can help or hurt in
  ways a single choice per task wouldn't. That is part of what is being measured
  and is reported through the model-mix metrics. It is not a flaw in the design.
- **Leakage.** Jev must see only what the Router normally sees: no task IDs,
  categories, difficulty or results from other arms.
- **Benchmark quality.** Environment bugs in the official set affect every arm
  equally. The Verified fork shows their size, but only as a separate result.
- **Narrow claim.** A pass supports "Jev routing with these candidates is
  non-inferior and cheaper on Terminal-Bench 2.0 under Terminus-2". It is not a
  general quality claim and not alignment evidence; that is covered by the
  [supervision accuracy plan](fleet-supervision-accuracy-experiment.md).

## References

- Terminal-Bench, ICLR 2026: [paper](https://proceedings.iclr.cc/paper_files/paper/2026/file/444a3737adaee10d86ad2ef5f74468e6-Paper-Conference.pdf); [documentation](https://tbench.ai/docs); [leaderboard](https://snorkel.ai/leaderboard/terminal-bench-2-0/); [Harbor example](https://mintlify.com/harbor-framework/harbor/examples/terminal-bench)
- [Terminal-Bench 2.0 Verified (Z.ai fork)](https://huggingface.co/datasets/zai-org/terminal-bench-2-verified)
- [DeepSeek-V4 paper (Terminal-Bench 2.0 results)](https://arxiv.org/pdf/2606.19348); [GLM-5 paper](https://arxiv.org/pdf/2602.15763)
- SWE-bench Pro: [paper](https://arxiv.org/pdf/2509.16941); [harness](https://github.com/scaleapi/SWE-bench_Pro-os); [leaderboard](https://labs.scale.com/leaderboard/swe_bench_pro)
- [Why OpenAI no longer evaluates SWE-bench Verified](https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/)
- Harness effect: [The Scaffold Effect in Coding Agents](https://arxiv.org/pdf/2607.22585); [harness comparison](https://futureagi.com/blog/coding-agent-harness-benchmark/)

Values quoted from third-party pages above (scores, counts) were collected on
2026-11-14 and must be rechecked against primary sources before use as anchors.
