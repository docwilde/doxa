# Representative routing repair benchmark

This is an **implemented, offline 18-task repair suite**, not a measured model
ranking. It prepares actual small Git repositories and grades their repaired
Python programs. No provider call, fleet launch, routing change or supervision
acceptance is performed by this tooling. The earlier 12 short-answer questions
remain exploratory evidence; they do not supply labels for this suite.

## Difficulty and provisional model mapping

Difficulty is declared before seeing any model results. It describes the code
scope and interacting failure modes, not a guaranteed best model or the number
of changed lines. A one-line repair can still require tracing a crash boundary.

| Difficulty | Six tasks | Initial model/effort hypothesis |
| --- | --- | --- |
| easy | Config types/defaults; POSIX scope boundaries; strict JSON; integer billing; expiry boundaries; structured redaction | `deepseek-flash`, `none` |
| intermediate | Event replay; dependency scheduling; unknown budget holds; source-bound release evidence; internet delegation; request idempotency | `deepseek-flash`, `high` |
| difficult | Durable outbox; crash-atomic checkpoint; concurrent reservations; session-incarnation replay; uncertain dispatch crash; authority-preserving migration | `deepseek-v4-pro`, `high` |

Easy tasks have one editable module and a localized primary defect. Intermediate
tasks have two modules and interacting state/validation invariants. Difficult
tasks have two or three modules with actual child-process restart/death or real
threaded concurrency, together with durable state, authority or replay rules.
They remain mini-repositories; they do not represent full production feature
development, UI work, sustained fleet supervision or every programming language.

[DeepSeek's official model table](https://api-docs.deepseek.com/quick_start/pricing/)
currently identifies `deepseek-flash` as V4.1 Flash and `deepseek-v4-pro` as V4
Pro. Its [thinking controls](https://api-docs.deepseek.com/guides/thinking_mode/)
support disabled thinking and explicit high effort. The model mapping file
records the source URLs, observation date and conservative peak token prices.
An exact executing account catalog must still be refreshed before paid admission.

All three candidates remain eligible for every task. The provisional mapping is
a baseline policy to compare, **not** an `expected_candidate_id` or task oracle.
Only actual success, cost and latency can establish whether escalation helps.
Optional native GPT-6 Sol/GPT-6.1 Sol arms require separate verified carrier,
account availability and matched tool capabilities; this suite does not claim
either native engine has been measured.

## Prepare a candidate workspace

Use an owner-private directory on real disk, outside `/tmp`. There is no model
execution in either command:

```sh
mkdir -m 700 "$HOME/ssd-cache/doxa-repair-benchmark"
python3 scripts/routing-benchmark/benchmark.py list
python3 scripts/routing-benchmark/benchmark.py prepare difficult-checkpoint \
  "$HOME/ssd-cache/doxa-repair-benchmark/checkpoint-run1"
```

Give the candidate only the printed `agent_project` directory. It contains a
fresh Git repository, buggy source, the contract and public smoke examples.
The operator's plan, trusted repairs and evaluator cases are outside it. Models
may repair only the listed files; extra files, symlinks, hardlinks and changes to
the contract/public cases are refused. The suite does not supply API workers
with a new filesystem permission: a provider-specific reviewed patch bridge or
existing authorized editing engine must deliver the actual repair. Prose-only
answers are not code changes.

`--trusted-reference` is solely for operator fixture validation. Never use that
workspace as a model input. The repository contains the transparent corpus for
reproducibility, so it is not a secret external benchmark. Evaluator-only means
the cases/answers are withheld from the candidate workspace and score container;
future contamination and deliberate test gaming remain limitations.

## Grade in offline Docker

```sh
python3 scripts/routing-benchmark/benchmark.py grade \
  "$HOME/ssd-cache/doxa-repair-benchmark/checkpoint-run1" \
  --docker-host unix:///ABSOLUTE/ROOTLESS/ENGINE.sock \
  --image sha256:EXACT_64_HEX_IMAGE_ID
```

The image must already exist locally and provide `/usr/bin/python3` with the
standard library, including SQLite. No image pull is allowed. The Engine must
report rootless mode. Each case starts a new credential-free container with
network `none`, read-only source/root filesystem, no capabilities or privilege
escalation, 256 MiB memory/no swap, one CPU, 64 PIDs, a 32 MiB private state
tmpfs and a 30-second deadline. It receives no host home, Docker socket, provider
gateway, credential store or oracle answers. Root inside the rootless container
is used only to read the owner-private source mount; host-root privileges are
not granted.

The host never imports or runs candidate/reference modules. A trusted container
driver invokes their documented APIs and returns bounded JSON. The host compares
actual outputs to the private oracle using exact types; exit zero or fabricated
test-success text cannot substitute for the required outputs. Difficult cases
use real child-process termination/restart or synchronized threads, not a model
answer claiming that those operations ran. No real network/isolation migration
or paid side effect is executed by the simulated approval/dispatch tasks.

Every grade copies a new source snapshot, hashes the complete allowed inventory,
and records its task/driver/corpus/image identity and actual case outcomes in a
new private result directory. Output overflow, timeout, invalid JSON, process
failure or unconfirmed cleanup is a failed/incomplete measurement. Model service
or billing failures belong in the outer provider ledger and must remain separate
from functional repair failures. Neither successful references nor failing buggy
starters constitute model-quality results.

## Matched evaluation protocol

1. Freeze this corpus, task-family split, candidate descriptions, models/efforts,
   output allowances, attempt budget and routing policies before opening results.
   Related variants share a family; event/replay and idempotency variants cannot
   cross splits. This public corpus uses fixed family-level splits, not random
   per-message or per-test splitting.
2. Run every model on every task under the same editing tools, public tests,
   context input, wall-clock budget and number of repair attempts. Suggested
   initial design: three independent repetitions, 18 tasks and three candidates
   (162 model trials before any permitted repair iteration). Provider outputs
   remain stochastic; no unsupported deterministic seed guarantee is implied.
3. Use at least 16,384 thinking-output tokens. The supplied candidate hypotheses
   allow **32,768 total generated tokens for every arm**, reasoning plus answer,
   so a tiny final-output cap does not defeat code-artifact delivery. Freeze any
   task-specific cap change before the held-out run. Provider/request limits and
   observed usage remain independently checked and cost-bounded.
4. Keep evaluator-only cases out of candidate prompts and feedback. Development
   may use separately disclosed failure diagnostics; freeze the final repair
   before held-out grading and never tune thresholds on its failures. Record
   capped/timeout/infrastructure trials separately from functional correctness.
5. Report all-tests task success and individual requirements, repeated-trial
   variation, actual input/output tokens, verified charge estimate, latency,
   correction attempts and cleanup. Compare always-Flash-direct,
   always-Flash-thinking, always-Pro, the provisional difficulty mapping and
   Jev selection against measured outputs, including the cheapest successful
   candidate and avoidable errors. A router matching a difficulty label is not
   evidence it chose a successful repair.

The shared human-labelled message holdout is a different evaluation. These
machine-authored task oracles must not become human alignment labels or pass
the 100-message/five-fleet supervision gate.

## Nonbillable validation

```sh
TMPDIR="$HOME/ssd-cache/tmp" python3 -m unittest discover \
  -s scripts/routing-benchmark -p 'test_*.py'
python3 scripts/routing-benchmark/self_check.py \
  "$HOME/ssd-cache/doxa-repair-benchmark/corpus-validation" \
  --docker-host unix:///ABSOLUTE/ROOTLESS/ENGINE.sock \
  --image sha256:EXACT_64_HEX_IMAGE_ID
```

The explicit Docker check requires every trusted repair to pass and every buggy
starter to fail at least one case. It calls no model and starts no DOXA session.
