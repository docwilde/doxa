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
It also verifies that the provider's unhooked token-budget reset feature is
disabled. An unsupported build, missing trusted hook or unverified reset
configuration refuses startup with its reason.
DOXA adds its own session configuration; it does not overwrite global Codex
settings or trust unrelated user hooks.

The pinned hook binds the actual provider thread and owned rollout, creates
a scrubbed private snapshot, and waits for the configured LORE reviewer.
Failure or a changed source returns a blocking hook decision. Before sending
manual `thread/compact/start`, DOXA independently reads the bound provider
rollout, runs the native reviewer and verifies the same source identity and
digest. Missing review, disabled memory/review, worker failure or changed proof
refuses the request and retains the existing context. Supervisor approval
requires a bounded receipt for the exact job; an exit code alone is insufficient.
`/compact` cannot pass through as an ordinary provider prompt. After submission,
DOXA still waits for matching hook and compaction events. A failed hook
notification stops the protected session.

**Provider limitation:** Codex 0.156.1 can continue compaction if the operating
system cannot spawn a hook, or if the hook times out or returns invalid output.
DOXA's parent can stop the process after observing failure, but this does not
guarantee that automatic compaction was prevented. Manual requests have the
independent review gate above. Raising a token threshold is not an automatic
compaction disable switch: the pinned build can also compact for a full context
window, model/context changes or recovery. Stable parity retains this upstream
limitation until the provider guarantees blocking infrastructure failures.

## Verification

```sh
cargo test --locked -p doxa-engines
```

Tests use local executable fixtures and fake review workers. They cover
stream boundaries, deadlines, cancellation, exact input replies, one-action
approvals, protected build/hook checks and compaction ordering without account
inference. The Python 1.19 `doxa/codex.py` remains the legacy behavior reference.
See the [provider verification record](../../docs/live-provider-verification-2026-09-28.md)
for actual account checks and their remaining authentication requirements.
