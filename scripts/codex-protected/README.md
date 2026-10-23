# Protected Codex provider

The reviewed patch targets official `rust-v0.164.0`, commit
`6aaac694803004a63c6f393d153c96d22521a6b9`. Source, patch, launcher,
app-server and code-mode helper identities are checked independently. The
installer uses a separate `source-<commit>-<patch>` checkout and publishes
immutable receipt-addressed artifacts; it never resets old build sources.

## Compaction boundary

Local and foreground remote V2 compaction must flush the complete rollout and
run exactly one trusted session-flags synchronous command hook before inference
or history replacement. A missing hook, flush failure, infrastructure error,
non-command/asynchronous handler, duplicate required hook or absent explicit
`{"continue":true,"suppressOutput":true}` JSON approval stops compaction.

Upstream introduced speculative asynchronous remote V2 compaction before
that foreground review. This path is deliberately disabled in the protected
payload: the scheduler refuses it even with its feature enabled, and the direct
background execution entry point returns `TurnAborted` before a request. DOXA
also overrides and verifies `features.async_compaction=false`. Token-budget
reset remains disabled. Background compaction may only be enabled after a
separate reviewed pre-inference authorization design and compiled proof.

The private `doxaAuto` live update only tightens on-request approval to never.
It cannot be mixed with environment/model/reviewer updates, change the sandbox
or alter thread defaults. Already captured steps observe the tightening;
pending escalation approvals and permission grants cannot authorize new access.
Final permission responses are sanitized and grants recorded while holding the
same active-turn lock that publishes the tightening, including stale Guardian
responses. Managed policy restrictions remain authoritative.

DOXA also disables native Codex agents with `agents.enabled=false` and
`features.multi_agent_v2=false`. Protected startup checks the effective settings
before opening a thread. DOXA peer tools still provide coordination through
visible DOXA sessions; native hidden agent creation cannot bypass fleet limits.

## Credential-free checks

Build from the exact checked patch and lockfile, then run these focused tests:

```sh
cargo test --locked --profile dev-small -p codex-core --lib doxa_
cargo test --locked --profile dev-small -p codex-core --lib infrastructure_failures_never_authorize_compaction
cargo test --locked --profile dev-small -p codex-hooks --lib doxa_required_hook_rejects_infrastructure_and_invalid_output
python3 scripts/codex-protected/verify_automatic.py --server PRIVATE_SERVER --scratch PRIVATE_SCRATCH
python3 scripts/codex-protected/verify_code_mode.py --server PRIVATE_SERVER --scratch PRIVATE_SCRATCH
```

List each Rust filter first and require a nonzero inventory: the current source
has eight core guard tests and one hook guard test. The originating-turn,
environment-keyed permission-grant and strict Guardian review baselines must
also remain passing. These focused checks do not replace the complete upstream
test suite.

The async guard test explicitly enables upstream's feature and exercises
scheduled/direct pre-turn, mid-turn and post-turn routes against a loopback
Responses fixture. It also checks refused foreground V2 review. Automatic local
compaction checks include all denial modes and an explicit-allow positive
control. The helper proof must use the same source build and verified receipt;
none of these commands require account credentials or external model calls.
See each verifier's `--help` for its exact options. These checks do not establish
real-account model availability or live sandbox acceptance.

## Reviewed 0.164.0 port

The 0.163.0-to-0.164.0 safety diff remains subject to manual semantic review;
the automatic updater still refuses unreviewed safety-sensitive paths. The
protection port preserves 0.164's structured approval source/cancellation
metadata when a live-auto switch refuses a stale result, and uses the live
step policy for remote execution routing. A regression exercises both an
already-pending approval and refusal before review. Current payloads use
Rust 1.99.1 and the official source-pinned V8 152.2.0 archive/binding manifests;
reviewed older receipts retain Rust 1.95.0 for migration and rollback.

## Retained Docker sessions after an upgrade

Existing Docker sessions keep the exact image recorded in their isolation
manifest. Installing a new host provider or selecting a new-session image
does not replace that saved image. A saved Docker thread can resume with the
exact reviewed 0.163.0 contract (source `83086188`, patch `3a7b8c09`) as well as
the current 0.164.0 provider. The retained contract requires the protected
DOXA/live-auto initialize prefix and all normal hook, feature and launcher
receipt checks. It is unavailable to fresh sessions or native providers.

Before thread startup, the host binds the descriptor's version, source and
patch to the actual reviewed initialize identity. The canonical reviewer
checks that exact tuple; it never relabels an old provider as current. Unknown
versions are refused before thread/resume or turn submission, with the saved
session, transcript and clean checkpoint retained for recovery. Same-backend
Docker image replacement is not implemented. Image or isolation changes still
require explicit review; the application never falls back to a native provider.
