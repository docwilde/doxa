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
  --alignment-supervisor claude:claude-sonnet-5-5 \
  --message-review shadow --message-judge jev:jev-1.13.0 --dry-run
```

Reviewer providers `claude`, `codex`, `deepseek`, and `glm` use stateless API
calls and their API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
`DEEPSEEK_API_KEY`, and `ZAI_API_KEY`). These are independent of Claude Code or
Codex subscription sessions. Jev uses `TYPESAFE_API_KEY`. Reviewer calls expose
no tools and retain no worker conversation history. Selecting reviewers opts
the LORE-scrubbed charter, assignment, messages, and host snapshots into those
external services. The model ID is explicit; unavailable models pause review
rather than silently switching providers.

The controller freezes the task, approved path prefixes, run budget, deadline,
human-only actions, and assignments before dispatch. Its manifest records the
charter SHA256 and all review settings. Each daemon binds the assignment to its
host PID. Incoming fleet envelopes must match the kernel-observed Unix sender
PID, current fleet, recipient, charter, and assignment. Unknown kinds/fields,
cross-fleet messages, direct task changes, unknown artifact references,
unsupported completion claims, duplicates, and exhausted limits are refused
before a peer message can start a billed turn. Other peer sessions retain their
ordinary messaging behavior and cannot enter the supervised fleet as free text.

The fast judge reviews each outgoing fleet message once. Its private cached
verdict binds the exact envelope hash; the receiving host rechecks deterministic
policy before admission. Shadow mode records semantic concerns; enforce mode
quarantines risky messages and pauses the fleet. A model cannot waive a denial,
approve authority, change the charter, or spawn sessions. If the judge is down,
ordinary status/evidence may arrive with an explicit unreviewed marker;
questions, proposals, task changes, and completion wait for review. Add
`--strict-unreviewed` to hold every unreviewed message.

The independent supervisor receives bounded host-observed Git path changes,
session state, peer decisions, budget reservations, and immutable assignments.
It runs before initial dispatch and at bounded checkpoints. Changed paths
outside approved prefixes pause deterministically. Worker claims are not
verified tests: snapshots say `tests_verified: false` until host evidence exists.
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
unknown spend requires investigation. A reviewer failure preserves live worker
state and holds new delegation until human recovery. Stop remains available.

Current bounds are 64 workers, a 16 KiB approved task, 6 KiB fleet message bodies,
60 admitted messages/minute, 10,000 message IDs, 8 MiB total admitted body bytes,
512 host artifacts, 200 default reviewer calls, a 60-second default checkpoint,
and a 12-second reviewer request deadline. Thresholds must be evaluated against
labeled real fleet messages before relying on semantic enforcement. Deterministic
fixtures and local HTTP tests validate the transport and policy; they do not
measure real-model false positives or misalignment recall.

References: [TypeSafe API](https://docs.typesafe.ai/api),
[Jev models and pricing](https://docs.typesafe.ai/models),
[OpenAI Responses API](https://developers.openai.com/api/reference/python/resources/responses/methods/create).
