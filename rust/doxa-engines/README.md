# Native Codex integration

`doxa-engines` translates Codex events into DOXA's `{ "type", "data" }`
protocol. New daemon sessions use the app-server transport; saved CLI sessions
keep their original provider thread in read-only mode until explicit migration. The daemon owns
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

Protected native app-server sessions require **DOXA’s private Codex 0.156.1
app-server build, contract `doxa-precompact-fail-closed-v1`**. Stock Codex
refuses protected startup before creating or resuming a thread.
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

The private provider makes the decision inside `run_pre_compact_hooks`, before
local/remote compaction inference or history replacement. Exactly one required
session hook must complete as a synchronous command with explicit JSON approval.
Missing hooks, spawn/read failures, timeout, invalid/empty/plain output, stopped
review, async handlers and duplicate required hooks cannot authorize replacement.
DOXA still verifies trust, the exact hook hash and disabled token-budget feature.
The provider identifies itself as `doxa_codex_rs/0.156.1` with the explicit private
contract; it never impersonates stock Codex.

### Install the protected provider

The standard installer builds a native Rust dispatcher and the pinned private
app server. This is a separate, initially unoptimized `dev-small` provider build;
no release-performance claim is made. The frontend/daemon keep their release
profiles. Build tooling requires Python 3.11+, Git and a user
systemd scope. The installer bootstraps a private Rust 1.95.0 toolchain on Linux
x86_64 using checksum-pinned Rustup 1.29.1, with a 12 GiB memory cap, zero swap and one build job. The first
build downloads and compiles a large Codex dependency graph; its separate cache
is reused on subsequent installs, including the verified binary. Other hosts
can pass `--cargo` for an existing Rust 1.95.0 toolchain. Python is not used by
the installed dispatcher.

Legacy `exec` sessions and `DOXA_CODEX_APPSERVER=0` cannot run provider turns:
review failure cannot be blocked inside stock exec. Refusal happens before any
prompt/thread persistence or memory snapshot. Their transcripts remain readable.
To explicitly migrate a verified saved legacy thread without creating a new one,
resume it with `DOXA_CODEX_MIGRATE_APPSERVER=1`; the protected server must return
that same `thread/resume` identity. New sessions should remove the old exec flag.

The provider is installed under
`~/.local/share/doxa/providers/codex-0.156.1-precompact-v1/` (or `XDG_DATA_HOME`).
Normal Codex sessions select it automatically; an explicit `--codex-bin` takes
precedence. Its native launcher verifies a private bounded receipt and executable
SHA256, then executes the same open inode. Login, version and other CLI commands
are delegated to the recorded official Codex executable, which is never replaced.

For a separate build/install from a checkout:

```sh
cargo build --locked -j 1 -p doxa-engines --bin doxa-codex-protected
python3 scripts/install_codex_protected.py \
  --launcher "$PWD/target/debug/doxa-codex-protected"
```

The installer pins official source commit
`b412ff32c417f855c2b2d1581b77058eed87c84b` and the reviewed patch checksum. The
release tag leaves 155 local workspace package versions at `0.0.0` in its
lockfile; the patch normalizes only these to `0.156.1`, with no external
version/source/checksum/dependency changes. Every compile uses `--locked`.
A real bounded initialize probe verifies the compiled contract before install.
The receipt records source, patch and binary hashes. An existing differing
provider receipt requires a separate install root for review. An explicit
`DOXA_INSTALL_CODEX_PROTECTED=0` installs the other DOXA engines without this
provider; protected Codex then remains unavailable until it is installed.

## Verification

```sh
cargo test --locked -p doxa-engines
```

Tests use local executable fixtures and fake review workers. They cover
stream boundaries, deadlines, cancellation, exact input replies, one-action
approvals, protected build/hook checks and compaction ordering without account
inference. The Python 1.19 `doxa/codex.py` remains the legacy behavior reference.
The credential-free compiled-provider probe is
`scripts/codex-protected/verify_automatic.py --server PATH --scratch PRIVATE_DIR`.
It uses a loopback Responses server, isolated credential-free homes, two synthetic
turns and a small automatic threshold. Denied cases require exactly one original
model request, no compaction request, no Compacted rollout record and retained
history; the allow control requires a real replacement.
See the [provider verification record](../../docs/live-provider-verification-2026-09-28.md)
for actual account checks and their remaining authentication requirements.
