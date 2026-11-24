# Protected Codex provider

The protected patch targets official `rust-v0.172.0`, commit
`3d86e43b075adeba2193ec5c22ac92f55afd82e6`. Source, patch, launcher,
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
reset remains disabled: DOXA overrides and verifies `features.token_budget=false`
and `features.context_management=false`, because experimental context
management would otherwise re-enable the token budget at session start for
eligible accounts. Background compaction may only be enabled after a
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
has twelve core guard tests and one hook guard test. The originating-turn,
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

## Reviewed 0.172.0 port

Upstream now validates legacy writable roots when it converts sandbox policies
for execution. The port retains that validation and carries forward DOXA's
foreground compaction hook, background refusal and permission-only
`doxaAuto` update. The [review record](reviews/0.172.0.json) binds the
source, patch and compiled checks.

## Reviewed 0.171.0 port

The port preserves upstream's request-country setting for ordinary live
updates. A private `doxaAuto` update must leave request country and the other
ordinary settings unset. Foreground compaction still requires the trusted
synchronous review, and the protected background entry refuses before
inference. The [review record](reviews/0.171.0.json) binds the workstation
build and credential-free checks to the pinned source and patch.

## Reviewed 0.170.0 port

The 0.170.0 source includes a root-only option for speculative compaction. The protected
provider disables scheduling for every session role and refuses direct background
dispatch before a request. Foreground compaction keeps upstream audio and tool
history handling behind DOXA's required synchronous review. The [review record](reviews/0.170.0.json)
binds the build and verification to this source and patch.

## Reviewed 0.167.1 port

Codex 0.167.1 changes incremental tool declarations for Bedrock Responses and
keeps hosted web search in the top-level tool list. The manual port preserves
both upstream tool handling and DOXA's live approval policy on captured steps.
Foreground compaction retains its required review and the protected payload
refuses background dispatch. The [review record](reviews/0.167.1.json) binds
the source, patch, toolchain and verification results.

## Reviewed 0.167.0 port

Network approvals enter the shared `request_approval_with_user_prompt` path.
Its live-auto checks refuse escalation before review and after an answer,
including a prompt captured before the switch. The structured result retains
its reviewer and cancellation metadata. Ordinary mandatory sandbox review
remains authoritative; the upstream managed human checkpoint checks run first.

Command decisions claim a registered pending prompt before applying an
execpolicy amendment. The durable write remains serialized with live-auto
publication. The compiled regression registers its positive-control prompt
and verifies both the durable allow rule and the stale-answer refusal.
Upstream turn binding and human-only decision validation remain intact.
Background compaction remains disabled, and direct background dispatch refuses
before inference. The review record is `reviews/0.167.0.json`.

The installer and updater compare a canonical diff with fixed algorithm,
context, prefixes and full object IDs. They retain exact patch-byte checks and
refuse unrelated source changes. Earlier reviewed installation receipts remain
available for migration and rollback.

## Reviewed 0.166.0 port

The port preserves the foreground compaction gate, disabled speculative
compaction and live-auto permission boundary. Upstream moved unified-exec
approval preparation into `handlers/unified_exec/preparation.rs`; both policy
reads use the live `StepContext` policy. Patch approval assessment also reads
that live policy. Permission requests retain their selected environment and
final grants remain serialized with live-auto publication.

The hook summary carries its source path as text, so the required hook checks
the exact synthetic session-flags spelling. The async compaction regression
awaits input capture and passes upstream's shutdown token. The review record
is `reviews/0.166.0.json`.

Compiled checks use the original 0.166.0 manifests and its pinned V8 git
revision, including the source-pinned archive and binding manifests. Nine
core guards, one hook guard, permission/Guardian baselines, app-server and
helper builds, automatic-compaction controls and the code-mode missing-helper
control pass without account credentials or paid model requests. These checks
cover the protected boundary; they do not establish full upstream-suite or
real-account acceptance.

## Reviewed 0.165.0 port

The 0.164.0-to-0.165.0 safety diff (174 guard-sensitive paths) was reviewed
manually; the automatic updater still refuses unreviewed safety-sensitive
paths. The protection hunks carry over unchanged. 0.165 builds PreCompact
hooks together with executor environment requirements hooks. Those are
discovered as `System` hooks at `<environment:ID>/requirements.toml`, so they
can stop compaction but never satisfy the required session-flags identity.
No new caller replaces history or starts compaction inference outside the
gate. The review record is `reviews/0.165.0.json`.

Revision `376dc699` (`reviews/0.165.0-r2.json`) keeps the same 0.165.0 source.
A stale command approval answered after the live-auto switch can no longer
persist an execpolicy amendment: the durable allow rule is written under the
same `active_turn` lock that publishes the switch, and refused once it has.
Docker sessions saved with the earlier `128293eb` revision resume with
beta.76.

The 0.164.0 port preserved structured approval source and cancellation
metadata when a live-auto switch refuses a stale result. It also made remote
execution routing use the live step policy. Current payloads use Rust 1.99.1
and the official source-pinned V8 152.2.0 archive/binding manifests; reviewed
older receipts retain Rust 1.95.0 for migration and rollback.

## Retained Docker sessions after an upgrade

Existing Docker sessions keep the exact image recorded in their isolation
manifest. Installing a new host provider or selecting a new-session image
does not replace that saved image. A saved Docker thread can resume with the
exact reviewed 0.163.0 contract (source `83086188`, patch `3a7b8c09`) or 0.164.0
contract (source `6aaac694`, patch `f8759872`), the reviewed 0.165.0 and
0.166.0, 0.167.0, 0.167.1 and 0.170.0 contracts. The retained contract requires the protected
DOXA/live-auto initialize prefix and all normal hook, feature and launcher
receipt checks. It is unavailable to fresh sessions or native providers.

Before thread startup, the host binds the descriptor's version, source and
patch to the actual reviewed initialize identity. The canonical reviewer
checks that exact tuple; it never relabels an old provider as current. Unknown
versions are refused before thread/resume or turn submission, with the saved
session, transcript and clean checkpoint retained for recovery. Same-backend
Docker image replacement is not implemented. Image or isolation changes still
require explicit review; the application never falls back to a native provider.
