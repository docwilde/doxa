# Protected Codex provider

The reviewed patch targets official `rust-v0.163.0`, commit
`83086188ae0614bd882818ff1aa23f3194962012`. Source, patch, launcher,
app-server and code-mode helper identities are checked independently. The
installer uses a separate `source-<commit>-<patch>` checkout and publishes
immutable receipt-addressed artifacts; it never resets old build sources.

## Compaction boundary

Local and foreground remote V2 compaction must flush the complete rollout and
run exactly one trusted session-flags synchronous command hook before inference
or history replacement. A missing hook, flush failure, infrastructure error,
non-command/asynchronous handler, duplicate required hook or absent explicit
`{"continue":true,"suppressOutput":true}` JSON approval stops compaction.

Upstream 0.163.0 introduces speculative asynchronous remote V2 compaction before
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
Managed policy restrictions remain authoritative.

## Credential-free checks

Build from the exact checked patch and lockfile, then run these focused tests:

```sh
cargo test --locked --profile dev-small -p codex-core --lib doxa_
cargo test --locked --profile dev-small -p codex-core --lib infrastructure_failures_never_authorize_compaction
cargo test --locked --profile dev-small -p codex-hooks --lib doxa_required_hook_rejects_infrastructure_and_invalid_output
python3 scripts/codex-protected/verify_automatic.py --server PRIVATE_SERVER --scratch PRIVATE_SCRATCH
python3 scripts/codex-protected/verify_code_mode.py --server PRIVATE_SERVER --scratch PRIVATE_SCRATCH
```

The async guard test explicitly enables upstream's feature and exercises
scheduled/direct pre-turn, mid-turn and post-turn routes against a loopback
Responses fixture. It also checks refused foreground V2 review. Automatic local
compaction checks include all denial modes and an explicit-allow positive
control. The helper proof must use the same source build and verified receipt;
none of these commands require account credentials or external model calls.
See each verifier's `--help` for its exact options. These checks do not establish
real-account model availability or live sandbox acceptance.
