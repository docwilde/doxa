# Rust semantic definition verification

DOXA's call graph currently reports syntax candidates, not compiler-resolved
bindings. `doxa-codegraph::semantic_evidence::inspect_definition_reply` is a
library-level, opt-in evidence checker for one `textDocument/definition` exchange.
It accepts a fresh call edge and one displayed candidate, reopens both sources
with the worktree-anchored, no-symlink reader, checks their SHA-256 hashes,
reparses the call and target function, and requires the LSP range to select the
declaration identifier exactly. It rejects ambiguous replies, mismatched IDs,
external or encoded URIs, changed files, unsupported UTF-16 lines, and oversized
messages. A matching exchange returns `protocol_match_untrusted` and
`binding: unknown`.

`semantic_producer::plan_rust_analyzer` is a separate opt-in, data-only launch
contract. It requires an image digest and a local non-root Docker socket URI,
then emits a `docker run` argument vector with `--pull=never`, no network, a
read-only worktree and root filesystem, dropped capabilities, no-new-privileges,
1 GiB memory and swap ceilings, one CPU, 64 PIDs and file descriptors, and a
64 MiB temporary filesystem. Its LSP initialization disables build scripts,
proc macros, automatic Cargo reload, and check-on-save. LSP frame parsing caps
headers at 1 KiB and bodies at 32 KiB. The Docker command clears the inherited
environment. The plan has no public spawn method. An internal, disabled
launcher seam uses a fresh container ID file to tie `docker run` to Docker
info, image inspect, and container inspect observations. It checks reported
rootless mode, the pinned image reference and local image ID, one read-only
worktree mount, no reported network attachments, the requested container
policy, and cgroup v2 memory, swap, CPU, and PID files for the inspected PID.
It repeats the container and cgroup checks after the definition reply, while
the container is still running.
Its probe subprocesses have a two-second deadline and bounded output. Fake
Docker, inspect, and cgroup fixtures exercise rejection of mismatches. The
CLI still reports `binding: unknown` and does not call this launcher.

The library-only LSP driver exercises a bounded initialize, quiescence,
definition, and shutdown exchange against a fixture server. It caps messages
and output, enforces a 20-second maximum deadline, and kills the process group
on failure. On Linux it also observes a successful server exit without first
reaping the group leader, then kills any descendants before reaping it. A
fixture verifies this path. The fake and observation gates used by these tests
do not constitute live rootless Docker proof.

This checker and plan are deliberately not wired to `/codegraph calls` or
persisted snapshots. No rust-analyzer binary or reviewed, pinned image is
installed in the development environment. The
[rust-analyzer configuration reference](https://rust-analyzer.github.io/book/configuration)
documents the settings. Its
[security guide](https://rust-analyzer.github.io/book/security.html) says
project configuration can execute code, so disabling two features is not a
substitute for enforcing the container boundary.

Before promoting any LSP result to a binding, review the pinned image and
verify its bytes, the effective rootless Engine and cgroup v2 controls, the
mount set and offline network namespace, disk quota, and absence of host
secrets on a live rootless host. Connect the bounded process supervisor to
that proven runtime. It must complete the LSP initialize/initialized exchange,
observe successful workspace indexing and a quiescent diagnostic state, and
attest the exact server binary/configuration, request and response, source and
target hashes, and container policy. Treat missing, timed-out, conditional,
ambiguous, generated, or uncheckable evidence as `unknown`. Recheck the final
binding against the same source bytes before surfacing it in the TUI or LORE.
