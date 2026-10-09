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
the container is still running. The seam also rejects added capabilities,
devices, device requests, and host PID mode. Each attempted run has a random
container name as well as a private CID file. On success, failure, or timeout,
it forces removal and requires a bounded daemon listing to show that both
identities are absent.
Its probe subprocesses have a two-second deadline and bounded output. Fake
Docker, inspect, and cgroup fixtures exercise rejection of mismatches. The
CLI still reports `binding: unknown` and does not call this launcher. In
particular, the current seam trusts the executable and Docker socket supplied
by its caller: a fake wrapper can run an LSP server outside Docker while
returning unrelated inspect and cgroup records. A production caller must
establish that the reviewed Docker client/socket and attached LSP stream refer
to the same attested container before this path can be enabled.

An additional **private, disabled** Linux transport seam opens the configured
Unix socket with a two-second deadline, checks that it is a private socket
owned by the current user, records its inode and `SO_PEERCRED` PID, and sends a
bounded [Engine attach request](https://docs.docker.com/reference/api/engine/version/v1.51/#tag/Container/operation/ContainerAttach)
for the exact 64-digit container ID. It accepts only an HTTP upgrade with a
multiplexed stream and checks the frame selector and size. Fake Unix daemons
exercise the request path, response rejection, and frame bounds. This is a
transport measurement, **not** a binding claim: a same-user fake daemon can
answer it, and the existing Docker CLI child still supplies the LSP pipes.
The probe intentionally rejects a `200` response or a `raw-stream` content
type; API version and response compatibility need review on the chosen live
Engine before this transport can be used.
The `same_uid_fake_engine_and_cli_cannot_promote_a_binding` regression
fixture demonstrates the trust gap without Docker: a same-UID Unix server
passes the private-socket and `SO_PEERCRED` checks, returns a plausible
upgraded LSP frame for the requested CID, while a separate fake CLI supplies
matching inspect/cgroup records and LSP bytes outside a container. Both
paths still yield only `binding: unknown`. Protocol compliance alone is
therefore insufficient evidence of a semantic binding.
The next implementation must launch, inspect, and carry every LSP byte over
one reviewed Engine endpoint, bind the attach request to the inspected CID,
and preserve cleanup and resource checks across that exchange. Until then,
the CLI never opens this seam and keeps `binding: unknown`.

### Direct Engine launch blocker

Moving `create`, `start`, `inspect`, and `attach` to HTTP on the same Unix
socket would remove the caller-supplied Docker CLI from the LSP byte path. It
would **not** establish that the peer is Docker: a process running as the same
user can own a private socket, satisfy `SO_PEERCRED`, return a plausible
container ID and inspect JSON, and send fabricated LSP frames. The current
development host has no reviewed rootless Engine socket, so it cannot provide
a live fixture for this boundary. Fake Engine tests alone
would verify protocol parsing, not daemon identity or containment.

Before implementing an activatable path, supply an owner-reviewed rootless
Engine fixture or a trusted broker with a host-controlled trust anchor that
DOXA can verify beyond socket credentials. The launcher must pin that identity across
every Engine request, use the `create` response's full ID for `start`,
`inspect`, and `attach`, and carry every LSP byte on that exact upgraded
connection. It must check non-TTY multiplexing, effective mounts, namespaces,
cgroup limits, no egress, disk quota, and image bytes, then force-remove the
same ID and prove absence after every outcome. The live fixture must exercise
daemon restart, socket replacement, late output, timeouts, and cleanup
failure. Until those checks pass, direct Engine replies remain untrusted and
the CLI's `binding` claim stays `unknown`.

The recommended next artifact is a **host-managed broker proof fixture** on
a disposable Linux host. The broker must run under a host-controlled identity
distinct from DOXA's user, expose a Unix socket in a directory that DOXA's
user cannot replace, and attest its reviewed binary/configuration and the
rootless Engine it controls. DOXA must verify the broker's host-controlled
identity on each connection and receive one transaction bound to a fresh
nonce, exact container ID, image bytes, effective policy, attached LSP stream,
and cleanup result. The fixture should first show that a same-UID attacker
cannot substitute the broker socket or inject an LSP stream, then exercise
daemon restart, socket replacement, policy drift, timeout, and failed cleanup.
Only after that host-owned boundary and the containment checks above pass
should the CLI gain an activatable semantic binding path. A same-UID helper,
private directory, socket inode check, or process-PID check alone cannot
provide this proof.

An initial private socket-observation protocol is implemented in
`semantic_broker`. It requires an unprivileged DOXA client, a canonical,
unsymlinked Unix socket and nonwritable parents that report UID 0, plus
`SO_PEERCRED` UID 0. Over that connection it sends a fresh 256-bit nonce and
a SHA-256 digest of the exact producer plan, call edge, and candidate in a
4 KiB length-bounded message. An `observation_only` reply must echo the
protocol, nonce, and query digest exactly; extra fields or a claimed
`attested` status are rejected. Connect and exchange share a two-second
wall-clock deadline, and the socket inode is rechecked after the exchange.
The only success result is `uid_zero_echo_untrusted` with `binding: unknown`.
The CLI never calls this seam.

These UID and path ownership values are relative to the caller's user and
mount namespaces. `SO_PEERCRED` can reflect credentials recorded on the
listening socket before its FD is handed to another process or the listener
drops privilege. They do not authenticate a live producer, a host-owned
endpoint, or the source of LSP bytes. The
[offline guest fixture](../../scripts/semantic-broker-proof/README.md)
demonstrates this directly: a process listens as guest UID 0, drops to UID
1000 before accepting and answering, yet the client can observe UID 0 on the
connection. The result remains untrusted. The workstation user's Docker/sudo
groups and possible client replacement or ptrace are additional reasons this
host cannot supply the adversarial production proof. A host-controlled trust
anchor beyond namespace-relative metadata remains necessary before enabling
producer attestation, launch, or semantic binding.

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
