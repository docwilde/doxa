# Native Codex integration

`doxa-engines` translates Codex events into DOXA's `{ "type", "data" }`
protocol. New daemon sessions use the app-server transport; saved CLI sessions
keep `codex exec --json` and their original provider thread. The daemon owns
transcript persistence, LORE scrubbing, session recovery and spend guards.

## App server

The native driver initializes the app server, starts or resumes a thread, and
sends each prompt through `turn/start`. Model and reasoning effort changes
apply to the next turn on that same thread. Account model discovery uses a
separate bounded app-server process without starting a provider thread.

The driver handles user questions, command approvals, file approvals and
DOXA peer tool requests. Replies are bound to the exact provider thread,
turn and request. Approvals are for one action. File approvals require the
complete cached proposal; secret input requests are refused until a masked
input interface exists. Cancellation clears pending answers. Peer tools
require human approval and accept only known operations with exact scoped
session IDs. Returned peer text is marked as untrusted data.

Thread IDs and whether DOXA dynamic tools were registered are persisted.
Legacy threads are not assumed to contain tools. Interrupted or failed turns
keep the recovery guard instead of silently creating a replacement thread.

Assistant and reasoning text are bounded and scrubbed as complete messages.
The UI can count incoming reasoning while retaining its content behind a fold.
Context usage follows the provider's telemetry and Codex TUI reserve. Usage
is considered complete only when the reported model and accounting basis
match; incomplete usage cannot authorize additional budgeted turns.

## Compaction review

Protected native app-server sessions currently require **Codex 0.156.1**.
Initialization checks the server's build identity and `hooks/list` verifies
DOXA's synchronous, trusted `PreCompact` command hash before a thread starts.
An unsupported build or missing trusted hook refuses startup with its reason.
DOXA adds its own session configuration; it does not overwrite global Codex
settings or trust unrelated user hooks.

The pinned hook binds the actual provider thread and owned rollout, creates
a scrubbed private snapshot, and waits for the configured LORE reviewer.
Failure or a changed source returns a blocking hook decision. Manual
`/compact` uses `thread/compact/start` and waits for matching review and
compaction events; it is not sent as a model prompt. A failed hook notification
stops the protected session.

**Provider limitation:** Codex 0.156.1 can continue compaction if the operating
system cannot spawn a hook, or if the hook times out or returns invalid output.
DOXA's parent can stop the process after observing failure, but this does not
guarantee that the provider has not already compacted. Normal reviewer failures
return a valid blocking decision before the hook deadline. Stable parity must
retain this distinction until the provider guarantees blocking infrastructure
failures.

## Verification

```sh
cargo test --locked -p doxa-engines
python -m pytest rust/doxa-engines/tests/test_codex_compact_hook.py -q
```

Tests use local executable fixtures and fake review workers. They cover
stream boundaries, deadlines, cancellation, exact input replies, one-action
approvals, protected build/hook checks and compaction ordering without account
inference. The Python 1.19 `doxa/codex.py` remains the legacy behavior reference.
