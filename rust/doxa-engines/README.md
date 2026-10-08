# Codex engine contract

`doxa-engines` translates Codex app-server events into DOXA's `{ "type", "data" }` protocol. The daemon owns transcript persistence, LORE scrubbing, session recovery, and spend guards. This page covers the protected Codex path; see the [engine capability matrix](../../docs/engine-capabilities.md) for all four engines.

## Contents

- [Session and review flow](#session-and-review-flow)
- [Compaction protection](#compaction-protection)
- [Install the protected provider](#install-the-protected-provider)
- [Ownership and recovery](#ownership-and-recovery)
- [Verification](#verification)

## Session and review flow

New Codex sessions initialize the private app server, start or resume a thread, and send turns with `turn/start`. Model and reasoning changes apply on the next turn of that same thread. A separate bounded process discovers account models without starting a thread. DOXA persists the thread ID and whether dynamic tools were registered; an interrupted turn keeps its recovery guard instead of silently creating a replacement thread.

User questions, command and file approvals, and peer tool calls bind to the exact thread, turn, and request. Approval covers one action. A file proposal needs its complete cached summary; stale replies cannot apply to a changed request. Secret input is refused until private masked input exists. Peer tools require review, known operations, and scoped session IDs; returned text is untrusted data.

Assistant text and reasoning are bounded and scrubbed as complete messages. The UI folds reasoning without erasing it. Provider telemetry supplies context and usage; budgeted turns require matching model and accounting basis. Unknown components stay unknown.

## Compaction protection

Protected sessions require DOXA's private **Codex 0.156.1** app server with contract `doxa-precompact-fail-closed-v1`. Before thread startup, DOXA checks the build identity, trusted synchronous `PreCompact` hook hash, and disabled unhooked token-budget reset. Stock or unsupported app servers fail before creating or resuming a protected thread.

The hook binds the actual provider thread and owned rollout, prepares a scrubbed snapshot, and waits for the native LORE reviewer. Manual `/compact` independently reviews the same bound source and rechecks its identity and digest before submission. Missing, failed, timed-out, changed, asynchronous, or duplicate review blocks replacement; a worker exit code without an exact receipt is insufficient. DOXA also waits for matching hook and compaction events after submission. It does not overwrite global Codex settings or trust unrelated user hooks.

The private provider makes the decision inside `run_pre_compact_hooks`, before compaction inference or history replacement. Exactly one required synchronous command must return explicit JSON approval. The provider identifies itself as `doxa_codex_rs/0.156.1` and does not impersonate stock Codex. [Authenticated default-window verification](../../docs/live-default-window-compaction-2026-09-30.md).

## Install the protected provider

The standard Linux x86_64 installer builds a native dispatcher, pinned private app server, and matching `codex-code-mode-host`. Some models require that helper even with shell tools enabled. It needs Python 3.11+, Git, and a working user systemd scope. The installer bootstraps checksum-pinned Rust 1.95.0 and uses one build job with a 12 GiB memory cap. Python is build tooling only. The first build downloads a large dependency graph; verified artifacts are reused.

From a checkout, build separately with:

```sh
cargo build --locked -j 1 -p doxa-engines --bin doxa-codex-protected
python3 scripts/install_codex_protected.py \
  --launcher "$PWD/target/debug/doxa-codex-protected"
```

The installer pins source commit `b412ff32c417f855c2b2d1581b77058eed87c84b` and a reviewed patch checksum. The patch normalizes local workspace package versions in the upstream lockfile; external sources and checksums stay fixed. A bounded initialize probe checks the compiled contract. Receipts bind the source, patch, dispatcher, server, and helper bytes. The code-mode helper's V8 inputs come from checksum-verified official artifacts. Changed fingerprints, corrupt installs, or partial receipts are refused; a fresh reviewed rebuild publishes a new immutable directory and atomically selects it for new launches. Existing processes keep their original files. An explicit `--codex-bin` takes precedence.

Set `DOXA_CODEX_PROTECTED_CACHE` to choose the standard build cache, or pass `--cache` to the standalone builder. `DOXA_INSTALL_CODEX_PROTECTED=0` skips this optional provider. The official Codex CLI remains available for login and help; DOXA does not replace it.

## Ownership and recovery

The native launcher is a dedicated Linux subreaper. Its private control socket completes a readiness handshake before the provider starts. Closing that socket on cancellation, shutdown, or daemon death stops and reaps only its descendants, including detached helpers. A launcher that cannot confirm ownership is refused before a protected session starts. The daemon does not adopt unrelated jobs.

Saved legacy `exec` sessions remain readable but cannot run protected turns. To migrate a verified thread, resume it with `DOXA_CODEX_MIGRATE_APPSERVER=1`; the protected server must confirm the same thread identity. `DOXA_CODEX_APPSERVER=0` cannot start new protected turns. No prompt, provider thread, or memory snapshot is created on refusal.

## Verification

```sh
cargo test --locked -p doxa-engines
```

Tests use local fixtures and fake review workers to cover stream boundaries, cancellation, exact replies, one-action approvals, hook checks, and compaction ordering. The credential-free compiled-provider probe is `scripts/codex-protected/verify_automatic.py --server PATH --scratch PRIVATE_DIR`; it runs a loopback model and checks both denial and allow paths. [Provider verification records](../../docs/live-provider-verification-2026-09-28.md) distinguish fixture checks from authenticated behavior.

Protected Codex is Linux-only because its supervision and owner contracts have no macOS equivalent. Windows is unsupported. The [platform record](../../docs/platform-verification.md) tracks other engines and operating systems.
