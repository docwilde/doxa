# Rust semantic definition verification

The explicit Linux `--semantic-broker` path is implemented in
`rust/doxa-codegraph/src/semantic_attested.rs` and exposed by both installed
`doxa codegraph` and the development binary. It can return `analyzer_resolved`
for one plain Rust call candidate through an externally provisioned,
operator-pinned broker. Ordinary syntax queries, persisted snapshots and the
older `--semantic-probe` retain their existing unknown binding claims.
The optional path does not install a broker or analyzer on the owner's host.

## Trust anchor and immutable source

The operator separately supplies a 32-byte Ed25519 public key and reviewed
broker/configuration/Engine/analyzer/image SHA pins. The root-controlled Python
broker, configuration, helper binaries, socket directory and private key are
outside the unprivileged DOXA user's write/ptrace authority. Startup requires
three distinct identities: root broker, nonroot DOXA client and a different
nonroot rootless Engine user. Namespace-relative socket credentials alone are
not authorization; the signed nonce-bound receipt is required.

The Rust client selects an edge/candidate from a fresh complete Rust `calls`
answer, refuses cfg/cfg_attr, item macros, generated source and build.rs
source/target evidence, then observes the whole worktree input digest.
This bounded digest covers all regular files, including ignored files,
manifests, binary assets and Git control files, plus directory names/modes.
Limits are 8 MiB per file, 128 MiB and 20,000 entries per tree, 20 seconds per
observation and two seconds per source read. Symlinks, unreadable and nonregular
inputs fail closed. A checkout whose Git metadata exceeds these limits cannot
supply this semantic query.

The root broker descriptor-walks and copies the complete source tree, committing
original modes, names, sizes and SHA hashes to the same input digest. Every
copied file is root-owned 0444 and directory 0555 beneath root-controlled
parents. The exact immutable tree is bound read-only at the original canonical
worktree path. The client cannot edit those mounted bytes while the analyzer
runs. `.cargo/config`, `.cargo/config.toml`, rust-toolchain and rust-analyzer
overrides are rejected before launch, including nested Cargo configurations;
project-controlled metadata executable overrides are not accepted.

The client rechecks source/target declarations and hashes, the complete Rust
inventory and whole worktree digest after the exchange. Mutable local byte
observations are sequential, not an atomic snapshot. They can miss an edit
restored between reads or a later edit; the binding describes the attested
immutable mounted bytes, with explicit local observations, not everlasting
worktree freshness. Any durable drift observed during rechecks fails closed.

## Actual Engine and analyzer bytes

The broker uses Engine API v1.51 directly over the pinned Unix peer; no Docker
CLI supplies LSP bytes. It checks the distinct Engine UID, socket dev/inode,
held daemon pidfd and `/proc/PID/exe` SHA across requests. The exact 64-digit
create-response CID is used for start, inspect and attach. Bounded pre-inspect
and upgraded non-TTY multiplexed attach occur on the same Unix FD. Stderr is
never parsed as LSP. A same-UID Python fake Engine cannot satisfy the pinned
actual dockerd executable check.

Engine inspection and independent kernel observations verify the pinned image,
rootless cgroup v2, read-only source/root mounts, separate mnt/net/pid/ipc/uts
namespaces, no-new-privileges, seccomp filtering and zero effective/bounding
capabilities. Effective limits are 1 GiB memory, zero swap, one CPU, 64 PIDs,
64 file descriptors and a 64 MiB noexec/nosuid/nodev temporary filesystem.
Network metadata may be empty or contain only Docker's `none` network; kernel
interfaces must be only `lo` with no routes. Devices, capabilities, privileged
mode, extra mounts, network attachments or inconsistent limits are rejected.
Image/toolchain provisioning may use a network; analyzer runtime has no egress.

Rootless FUSE denies a plain host-root proc-exe open on the measured host.
The broker therefore executes pinned root-controlled nsenter, isolated Python
and `read_analyzer.py`, entering only a held analyzer user-namespace FD while
retaining host mount/PID namespaces. The first exec clears inherited broker
memory before the identity transition. The helper receives minimal environment,
DEVNULL stdin and only the namespace FD; no signer/client/PID descriptors are
passed. It hashes a held proc-exe descriptor, checks its complete metadata
before/after, freshly reopens the current proc-exe mapping and requires the
same dev/inode/size/mtime/ctime/mode. The broker holds the analyzer pidfd and
checks namespace/liveness before/after, then repeats the complete executable
observation after definition. Each helper has a two-second/4 KiB-per-pipe bound,
100 ms cancellation polling and a one-second kill/reap grace; an unreaped
helper disables subsequent use.

## Analyzer settings and lifecycle

The exact settings are signed in the receipt and required by the client:
Cargo build scripts disabled, proc macros disabled, check-on-save disabled,
Cargo noDeps true, client file watcher, one analyzer/cache-priming thread, and
Cargo autoreload true **only for the attested immutable snapshot**.
The pinned rust-analyzer 2026-10-12 main_loop.rs:607 gates initial queued
workspace loading on autoreload. Setting false leaves the initial workspace
unloaded; even manual reload only queues a fetch. Immutability and override
rejection make bootstrap reload acceptable here. The older mutable-source
producer remains autoreload false and untrusted.

Initialize advertises definition linkSupport to select the exact identifier
range. A healthy/quiescent serverStatus must precede definition, and every
later status read during definition/shutdown must remain healthy/quiescent.
The matching definition must be unique and identify the freshly checked
candidate declaration exactly. Wrong IDs, encoded/external URIs, unsupported
UTF-16 positions, ambiguity and errors fail closed. Shutdown must return a
successful null result, followed by an observed exit code zero without OOM.
Forced container removal cannot substitute for successful analyzer shutdown.

LSP headers/bodies are limited to 1 KiB/32 KiB, total stdout/stderr to 256 KiB,
and each wait to 256 messages within a 20-second LSP session. The Rust broker
exchange has a 60-second absolute I/O deadline, including slow-trickle reads
and writes; source pre/post checks lie outside that exchange deadline and
remain independently bounded. Broker accept/header/challenge through reply is
bounded; each Engine HTTP request/attach has a nested two-second absolute
limit. Cancellation checks cover source copying, Engine boundaries and both
client/analyzer read readiness. Cleanup has a separate eight-second budget.

All outcomes remove the exact CID and verify CID and nonce-derived name
absence. Lost or malformed create replies recover the exact nonce name and
validate image/name/label/full CID before deletion. Restart recovery may clean
our exact container through a newly pinned Engine, but never restores the
original binding authority. Failed deletion/verification reports cleanup false
and status unknown. The signed reply binds fresh nonce, exact challenge hash,
immutable source digest, code/config/Engine/analyzer/image pins, attached request
and response, settings, runtime gates, successful exit and observed cleanup.

## Measured acceptance and remaining scope

The [disposable live guest receipt](../../scripts/semantic-broker-proof/evidence/attested-2026-10-13/RUN.md)
records 39 passing cases on Ubuntu 26.04.1/kernel 7.0.0-34, real rootless
Docker 29.1.3 and reviewed rust-analyzer 2026-10-12 with Rust 1.94.0.
Five unprivileged installed-client queries resolved the exact declaration and
verified all required gates, with median 3,406.504 ms and p95 3,546.352 ms.
The fixture is tiny and dependency-free; these are complete semantic query
measurements, not a large-workspace latency guarantee.

The same runtime refused actual PIDs and independent kernel nofile drift,
immutable source writes, wrong actual CID on a pinned daemon FD, helper
hash/namespace/output/timeout/cancellation errors, busy status, shutdown error,
client disconnect and a stopped analyzer. Lost/malformed create responses
recovered and removed the exact actual CID. Both injected DELETE failures
reported cleanup=false; exact administrative removal was separately verified.
UID 1000 could not read the signer, modify code/socket, borrow broker FDs or
connect to the Engine. A UID 1001 fake daemon failed the actual executable pin;
a fake signature, genuine signed receipt replay and trailing byte after a fresh
signed receipt were rejected. Final object/snapshot inventories were empty.
A separate coordinated restart observed the actual old daemon/socket refusal;
it held no active semantic container and does not prove restart cleanup of a
semantic transaction. A supplementary genuine signed envelope and challenge
are retained for independent public-key verification.

An owner installation still needs the reviewed external trust anchor, distinct
Engine identity, pinned image/toolchain/configuration and honest installed
client. The fixture does not attest a caller replaced or ptraced by its own
user, nor an incorrectly selected operator key. No owner-host broker was
installed. Arbitrary dependency caches are not hydrated by this profile;
unavailable offline metadata stays unhealthy/unknown. General Rust imports,
traits/types, macro expansion, cfg reachability and compiler execution are
outside the analyzer-definition claim. Incomplete source inventories stay
unknown; DOXA's own safe parser tombstones still prevent a complete Rust
semantic inventory. Its automatic syntax index explicitly retains that
incomplete coverage instead of manufacturing a digest or verified store.

## Historical observations stay untrusted

`semantic_evidence::inspect_definition_reply` alone returns
`protocol_match_untrusted`. The caller-supplied producer plan and separate
Docker CLI seam cannot establish stream provenance. `semantic_runtime` fake
attach tests and `semantic_broker` UID-zero listener/sender/stream tests never
return an authorized binding. The retained
[offline fixtures](../../scripts/semantic-broker-proof/README.md) demonstrate
stale listener credentials, FD handoff, namespace-relative IDs, changed senders,
changed CIDs and extra packets with synthetic LSP bytes. They remain historical
protocol measurements; none is promoted into the live broker trust chain.
The default TUI syntax viewer and reviewed LORE snapshot read/store gates are
unchanged. The
[rust-analyzer configuration](https://rust-analyzer.github.io/book/configuration)
and [security guide](https://rust-analyzer.github.io/book/security.html)
explain why configuration restrictions alone cannot replace containment.
