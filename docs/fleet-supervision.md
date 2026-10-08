# Independent fleet supervision

The acting coordinator selected by `--supervisor` remains a fleet worker. Choose
an independent read-only reviewer separately with `--alignment-supervisor
PROVIDER:MODEL`. Choose the fast message reviewer separately with
`--message-judge llm:PROVIDER:MODEL` or `--message-judge jev:jev-1.13.0`, and
`--message-review off|shadow|enforce`. The Fleet category in `/settings` exposes
the same model, mode, review allocation, conservative rates, and threshold
defaults. CLI options override those defaults at the launch review.

Example (the dry run makes no model calls):

```sh
doxa fleet start --pool deepseek:deepseek-flash -n 2 \
  --prompt 'Implement the approved change within src/' \
  --allowed-path src --run-budget 10 --review-budget 1 \
  --alignment-supervisor claude:YOUR_SUPERVISOR_MODEL \
  --message-review shadow --message-judge jev:jev-1.13.0 --dry-run
```

Reviewer providers `claude`, `codex`, `deepseek`, and `glm` use stateless API
calls and their API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
`DEEPSEEK_API_KEY`, and `ZAI_API_KEY`). DeepSeek and GLM also use the
private API keys saved through `/setup`, with the same explicit override policy
as ordinary vendor sessions. These are independent of Claude Code or
Codex subscription sessions. Jev uses `TYPESAFE_API_KEY`. Reviewer calls expose
no tools and retain no worker conversation history. Selecting reviewers opts
the LORE-scrubbed charter, assignment, messages, and host snapshots into those
external services. The model ID is explicit; unavailable models pause review
rather than silently switching providers.

Use `--isolation native|docker-open|docker-offline` to select each worker's
execution boundary. The launch review and manifest record it; the owner's
new-session default applies when omitted. Cross-backend migration is held while
a supervised fleet binds a daemon PID; idle Docker network changes retain it.

The controller freezes the task, approved path prefixes, run budget, deadline,
human-only actions, and assignments before dispatch. Its manifest records the
charter SHA256 and all review settings. Each daemon binds the assignment to its
host PID. Incoming fleet envelopes must match the kernel-observed Unix sender
PID, current fleet, recipient, charter, and assignment. Unknown kinds/fields,
cross-fleet messages, direct task changes, unknown artifact references,
completion without host-verified diff and passing test evidence, duplicates, and exhausted limits are refused
before a peer message can start a billed turn. Other peer sessions retain their
ordinary messaging behavior and cannot enter the supervised fleet as free text.

For planned parallel work, pass one `--worker-task INDEX:TEXT` per worker, with
one-based indices. Optionally repeat `--worker-path INDEX:RELATIVE_PREFIX` to
narrow each worker's file scope inside the overall `--allowed-path` prefixes.
The launch review shows a task digest and path scope for every slot. The host freezes
these assignments before dispatch, binds their hash in the private guard
journal, briefs each worker with its own task, and pauses if a checkpoint sees
that worker change paths outside its scope. Without worker tasks, each worker
inherits the shared prompt. Per-worker path enforcement requires independent
review; a plain unsupervised fleet has no host scope checkpoint.

Add `--worker-after INDEX:PREDECESSOR` for a worker that must wait for an
earlier worker (repeat it for several predecessors). Dependencies require an
acting `--supervisor` and independent review. The launch review shows every
edge and binds it into the assignment digest. Waiting workers have live,
budgeted daemons but receive no initial task; supervised peer delivery to them
is refused until host dispatch. `fleet status RUN` shows `dependency_waiting`.
On resume, waiting slots stay waiting, while an uncertain `dispatch_pending`
slot is refused rather than prompted twice. Dependency plans require
`--isolation docker-open` or `docker-offline`; `native` is rejected. The host
checks each session's ready Docker manifest and refuses a fleet root inside a
container mount. Docker workers see their private checkout, home, cache and
restricted broker, while the owner fleet root and CLI stay on the host.

The release is explicitly human-owned. A narrow, opt-in host test runner is
available for supervised `docker-offline` fleets. Put a reviewed recipe in an
owner-controlled JSON file and pass `--test-recipe /absolute/path/recipe.json`
at launch. Its exact command is frozen in the charter:

```json
{"argv":["/usr/bin/python3","-m","unittest","discover"],"cwd_relative":"","timeout_s":120}
```

After a worker finishes, the operator runs `doxa fleet test RUN SLOT`. The
host copies bounded checkout source, including ignored regular files, into a
private snapshot, then invokes the approved argv without a shell in a separate
rootless Docker container.
That container has no network, provider home, broker, host credentials or
Docker socket. It has a read-only source mount, bounded scratch, memory, PIDs,
CPU, output and a deadline. The command returns signed `git_diff` and
`test_result` artifact IDs; a passing test only describes that exact source
snapshot. Completion messages must cite both IDs. Changed source, a different
worker or image, an altered receipt, and a failed test are refused. Test
execution never releases a dependent worker or grants scope. The first slice
limits source to 4,096 files, 128 MiB total and 8 MiB per file; symlinks and
special files fail closed. Ignored files outside the approved assignment scope
block receipts; a checkout that exceeds the capture bounds also fails closed.
Rootless Docker end-to-end execution still needs a
capable host and an owner-approved project recipe.

After the predecessor finishes a turn, the host records
a checkpoint. The operator can inspect its ID and changed paths with
`doxa fleet dependency-evidence RUN SLOT`, give that ID to the predecessor in
a subsequent turn, and ask it to send a typed `handoff` to the acting
coordinator. The coordinator sends a matching `ack`; the worker sends
`confirm` with the same artifact references. Once that turn finishes, run
`doxa fleet dependency-review RUN SLOT`, or open
`/fleet dependency-review [RUN] SLOT` in the native TUI. With RUN omitted,
the command uses this window's current fleet controller; name RUN explicitly
for a detached or saved fleet (and pass `--root ABSOLUTE_PATH` if needed).
The TUI modal shows the host-returned
checkpoint and accepted handoff IDs, artifact references, changed paths,
assignment and turn hashes, dependent slots, and the checkpoint's
`tests_verified: false` (the separate signed test receipt does not rewrite a
past checkpoint).
Scroll through every row, press Shift+A to arm, then Shift+Y to release;
Escape or a terminal resize disarms it. The TUI sends the stored host review
token through the same `release_dependency` call and refreshes fleet status.
The CLI equivalent is `doxa fleet dependency-release RUN SLOT REVIEW_TOKEN`
after reading its review. A changed token,
active predecessor, missing accepted handoff, pause, or changed turn keeps the
dependency blocked. Each predecessor needs its own release. The host then
dispatches ready workers once and journals `dispatch_pending` before prompt
admission. Human release accepts scheduling evidence; it does not certify
test quality or grant new scope. If a predecessor starts another turn after
review, the release token becomes stale and the operator must review again.
The CLI token binds the reviewed evidence; it does not authenticate a human.
Native-profile workers share the operator's Unix account and could invoke
local fleet commands, so dependency plans refuse that profile. The Docker
boundary assumes the owner does not expose the host's fleet root or control
services into containers through separate mounts or network configuration.
The TUI review is an owner-operated control on the host; the review token
binds evidence but is not a separate authentication credential.

Supervised workers can use `fleet_kind=handoff` with host artifact IDs, followed
by the recipient's `ack` and the original sender's `confirm`. The ACK and
confirmation must reference the preceding message UUID and echo the same host
artifact IDs. The host checks direction, ancestry and exact IDs. The message
body still needs human or semantic review for meaning; a matching ACK proves
the artifact reference was returned, not that a model understood the work.
Handoff transitions request a bounded supervisor checkpoint in addition to
turn milestones and the configured timer.

The fast judge reviews each outgoing fleet message once. Its private cached
verdict binds the exact envelope hash; the receiving host rechecks deterministic
policy before admission. Shadow mode records semantic concerns; enforce mode
quarantines risky messages and pauses the fleet. A model cannot waive a denial,
approve authority, change the charter, or spawn sessions. If the judge is down,
ordinary status/evidence may arrive with an explicit unreviewed marker;
the marker appears in the recipient provider prompt and TUI transcript.
Questions, proposals, task changes, and completion wait for review. Add
`--strict-unreviewed` to hold every unreviewed message.

The independent supervisor receives bounded host-observed Git path changes,
session state, peer decisions, budget reservations, and immutable assignments.
It runs before initial dispatch and at bounded checkpoints. Changed paths
outside approved prefixes pause deterministically. Missing or truncated Git observations hold an enforced
supervisor for human review before calling the model. Observations use a private
Git directory, so native worker fsmonitor, hooks and clean filters cannot execute
on the host. Worker claims are not verified tests: snapshots say
`tests_verified: false` until host evidence exists.
Reviewer verdicts are a closed `aligned|uncertain|drifted|blocked` enum; cited
artifact IDs must exist in the host journal. A positive verdict never clears a
pause or marks an implementation complete.

`--review-budget` is reserved from the total fleet allocation; the remainder
sets worker ceilings. All reviewer calls share durable call/spend limits,
including failed calls. For LLMs, reservations use a conservative input token
bound and a hard 512 output token cap at the recorded owner-approved rates.
Defaults are $100/M input and output, not a claim about the selected model's
price. `--review-input-price` and `--review-output-price` change these estimates.
Pinned Jev 1.13 uses its documented $0.042/M input-only rate. UI status labels
reservations and token-based usage as estimates, not invoices. Usage exceeding
a reservation makes accounting unknown and prevents automatic recovery.

The Fleet view shows model selections, mode, alignment, pause reason, charter
hash, reservation estimates, and calls. After reviewing the evidence, explicitly
continue with `/fleet continue RUN CHARTER_SHA256` (or `doxa fleet continue`).
Resume revalidates daemon identity and the frozen charter; a changed host PID or
unknown spend requires investigation. In enforce mode, a reviewer failure preserves live worker
state and holds new delegation until human recovery. Shadow records outages
without imposing a semantic pause. Stop remains available.
Fleet assignments pin daemon identity, so both isolation migration RPCs are
refused while the guard is installed. Stop and review a new fleet to change the
execution backend; same-container network changes retain the pinned identity.

Current bounds are 64 workers, a 16 KiB approved task, 6 KiB fleet message bodies,
60 admitted messages/minute, 10,000 message IDs, 8 MiB total admitted body bytes,
512 host artifacts, 200 default reviewer calls, a 60-second default checkpoint,
and a 12-second reviewer request deadline. Thresholds must be evaluated against
labeled real fleet messages before relying on semantic enforcement. Deterministic
fixtures and local HTTP tests validate the transport and policy; they do not
measure real-model false positives or misalignment recall.

`doxa fleet calibrate /absolute/private/labeled.jsonl` compares the runtime
risk rule at thresholds 0.1–0.9 without sending messages or calling a model.
Each JSONL row supplies a unique `id`, the exact selected `model`, an
independently assigned `risky` boolean, and the five-field `verdict` returned
by that judge. This record is expanded for reading; store it on one line:

```json
{"id":"example-1","model":"jev:jev-1.13.0","risky":true,
 "verdict":{"within_assignment":0.2,"asks_for_authority_change":0.9,
            "contains_instructions_for_recipient":0.1,"likely_secret":0.0,
            "needs_human_review":0.8}}
```

The report shows missed risks, false pauses, recall, false-pause rate and
precision per threshold. It refuses mixed models, duplicate IDs, malformed
scores, and datasets with only one class. Fewer than 100 labels are marked
exploratory. Keep a separately labeled holdout set before choosing an enforce
threshold; a high score on the same examples used to choose it is not a
real-fleet safety result. The command reads at most 2 MiB and prints no message
content. Apply a reviewed choice with `--review-threshold` on a new fleet.

Open coordination work includes automatic invocation of trusted project tests and
calibration against real fleet messages. The host checks the typed handoff chain and checkpoint provenance;
the operator decides whether that evidence is sufficient to release a worker.

References: [TypeSafe API](https://docs.typesafe.ai/api),
[Jev models and pricing](https://docs.typesafe.ai/models),
[OpenAI Responses API](https://developers.openai.com/api/reference/python/resources/responses/methods/create).
