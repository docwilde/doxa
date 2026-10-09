# Per-session Docker isolation for DOXA Rust

Status: **Linux implementation shipped in 2.0.0-beta.10**. Native, Docker with
open egress and Docker with no network are implemented; the TUI shows the
host-verified profile. See [the operator guide](../session-isolation.md) and
[reviewed image packaging](../../containers/session/README.md) for current
settings and commands. A bounded allocated-block scan now gates launches,
resumes, migrations and new turns against a saved soft ceiling and host
free-space floor. The chip reports usage as monitoring, not a hard quota.
Hard quotas still require administrator-managed filesystem project quotas and
verified enforcement across bind mounts. The [operator fixture guide](../hard-quota-preflight.md)
includes a read-only project-inheritance preflight and an opt-in bounded
rootless-container `EDQUOT` probe. Neither authorizes production hard-quota
admission. New Docker sessions request a private
cgroup namespace, and the worker checks actual cgroup v2 memory, swap, CPU and
PID ceilings before admission and each CLI provider turn. Beta.16 adds an opt-in
rootless restricted-egress transport smoke; it does not verify production cgroup
or hard-quota enforcement. Hardened egress, macOS Docker Desktop,
remote Engines and a containerized controller remain open.
A [read-only remote Engine fixture preflight](../remote-engine-preflight.md)
checks the evidence shape for rootless identity, private broker transport,
daemon-host mount ownership and effective cgroup limits. Its output never
authorizes remote or Docker Desktop admission.

Target: Linux first. This
spec covers both a DOXA controller running on the host and a DOXA controller
packaged in Docker. The latter does not imply that a privileged Docker daemon
must run inside the controller container.

For multi-agent runs, [fleet communication and independent
supervision](fleet-supervision.md) adds typed message gates, an optional fast
Jev/LLM judge, budget controls and a separate alignment supervisor.

The older [sandbox draft](sandbox.md) documents a Python/Claude SDK design. It
does not describe the current Rust launch path and is not the implementation
plan for this feature.

## Decision and scope

One DOXA session gets one container. The terminal UI, remote connector, session
supervisor, approval authority, LORE store, transcript store and Docker control
socket stay **outside the agent container**. A worker in the container runs the
provider and every process that provider can launch on the agent's behalf.
The worker may call a narrow, session-bound host broker for operations that
cannot safely live in the container. The existing native backend remains
available and is never described as container-isolated.

Container mode initially uses an **independent Git checkout**, not a linked
worktree. A linked worktree's `.git` file leads into the main repository's
shared object and ref store. Giving the container writable access there would
let one session alter sibling branches or the main checkout. The host creates
an isolated clone with its own Git metadata and a DOXA branch; it imports that
branch for review/finalization through a host-side Git operation. This is the
deliberate compatibility difference from native worktree mode.

The Docker boundary protects the host filesystem, sibling sessions, and host
processes from ordinary agent commands. It is **not** a VM boundary, a defense
against kernel or Docker Engine compromise, or a promise that the provider's
own credentials are hidden from shell commands that the provider launches in
the same container. Provider credential containment needs its own work below.

## Current seams to change

- `rust/doxa-tui/src/launch.rs` creates a `doxa-daemon-rs` process directly and
  waits for its entry in the peer registry. All session entry points must use
  the same new launcher, including CLI, TUI, fleet, spawn, restore and remote
  initiated prompts.
- `rust/doxa-daemon/src/main.rs` currently combines provider lifecycle,
  transcript persistence, LORE, peer operations and session tools. Moving the
  binary unchanged into a container would require mounting shared DOXA state
  and sockets, defeating the boundary. Split trusted host services from the
  provider/tool worker before enabling a real engine.
- `rust/doxa-claude/src/cli.rs` launches Claude with a private configuration
  directory. `rust/doxa-engines/src/codex_appserver.rs` passes Codex's native
  approval and sandbox settings. Those protections remain useful but do not
  replace the per-session container boundary.
- `rust/doxa-daemon/src/session_spawn.rs` starts child sessions. A child must
  go through the host supervisor, inherit or strengthen the parent's isolation
  policy, and never receive the Docker API socket.

## Trust and process layout

```text
DOXA TUI / CLI / remote connector
            │ local DOXA protocol
            ▼
host session supervisor ── transcript, LORE, peers, approvals, Git finalization
            │                 │
            │ Docker API      └── session-bound broker socket
            ▼                            │
rootless Docker Engine                   │
            │                            │
            └── session container ◄───────┘
                    provider CLI or API driver
                    agent-callable tools and subprocesses
                    independent checkout + private scratch
```

`doxa-daemon-rs` remains the host-side per-session supervisor and keeps the
existing TUI-to-daemon protocol. A new worker binary and an isolation adapter
move the provider/tool execution out of that process. This keeps session
discovery and remote control attached to the same trusted daemon identity.

The host supervisor owns the canonical session ID and durable state. A worker
gets only its session ID, model selection, a path to its checkout, and an
ephemeral capability for **its own** broker endpoint. The broker accepts an
explicitly enumerated protocol: provider event frames, pending approval
requests, bounded LORE reads/writes, peer messages, spawn requests and
transcript checkpoint requests. Every call is checked against the session ID,
current turn and existing DOXA approval/remote policy. It does not expose
arbitrary file reads, shell execution, permission-mode escalation or generic
daemon RPC. A worker cannot list or connect to other sessions' sockets.

The broker socket lives in an owner-private per-session directory mounted only
into that container. Possession of its random capability authorizes requests
for that session alone; it does not authorize cross-session operations. The
host validates message size, schema, sequence and provenance and fails closed
on a broken channel. The remote hub still talks to the host supervisor, never
directly to a container.

## Session filesystem and Git contract

| Path in worker | Source and access | Reason |
| --- | --- | --- |
| `/workspace` | Independent session checkout, read/write | Agent edits and commits on its DOXA branch. |
| `/run/doxa/session` | Private broker directory, only this session | Narrow control channel. |
| `/home/doxa` | Private per-session home, host-managed and quota/space monitored | Provider state that must persist for resume. |
| `/work-cache` | Private per-session directory on the configured real disk | Builds and package caches; never a shared `/tmp` or RAM-backed 100 GB cache. |
| `/tmp` | Small bounded temporary area | Short-lived files only; large jobs use `/work-cache`. |
| Image root | Read-only | Toolchain and executable files. |

Do not mount the host home, main checkout, sibling checkouts, shared `.git`,
`DOXA_HOME`, Tailscale state, SSH agent socket, 1Password socket, or **any**
Docker socket. Host-side Git creates the independent clone from a verified
base commit, records its base SHA and branch in a private manifest, and checks
the branch and repository identity again before import. It never deletes dirty
or ahead checkouts automatically. Container mode's `/diff` reads the isolated
checkout; `finalize` imports a verified branch or patch into a host review
branch and leaves the original checkout intact until the result is verified.
The first implementation must measure clone cost on large repositories before
making container mode the default. No alternates pointing at a writable
shared object store are allowed.

The clone's `origin` must not give the worker a writable path back to the main
checkout. Do not copy host-global Git config, credential helpers, hooks or
SSH sockets into the worker. A normal `git commit` stays inside the isolated
clone; publication and branch import are host-reviewed operations.

All mount sources must be canonical, owned by the expected user, pre-existing,
free of symlink components at validation time, and inside an allowlisted
session root. For rootless Docker, validate the container UID-to-host UID
mapping with a real write probe against the checkout and cache. Container
UID 0 may map to the unprivileged owner of the rootless daemon and be needed
for writable binds; it is not host root. Otherwise use a mapped non-root UID.
Never solve a mapping failure by world-writable permissions or by switching
to the host's rootful Engine. Docker's bind mounts refer to paths on the **daemon host**, not
necessarily the client host; therefore the first backend accepts only a local
Unix-socket Docker Engine. Inspect the created container and its mounts before
declaring the session ready. A failed validation leaves no runnable container.

## Container policy

The policy is generated by DOXA from trusted user configuration and the
session manifest, never from a repository file or model-supplied arguments.
Use an image pinned by digest and built from reviewed DOXA binaries and
toolchains. Do not build an untrusted repository Dockerfile merely because a
session starts there. Run under a non-root **host** identity with a rootless Docker Engine,
use a read-only root filesystem, drop all capabilities, set no-new-privileges,
retain Docker's seccomp profile, and use private PID/IPC/network namespaces.
Never use `--privileged`, `--network host`, host PID namespace, host devices or
an unrestricted host bind. A repo that needs a device or wider mount requires
an explicit, visible per-session exception and cannot be labeled hardened.

Set finite memory, CPU and PID limits. Do not invent one universal memory
number: derive initial profiles from measured Claude, Codex, Rust-build and
vendor workloads, make limits user-configurable within administrator bounds,
and show the effective values before launch. Put cache and checkout data on
the configured real disk. Docker bind mounts alone do **not** enforce disk
quotas; require filesystem project quotas for a hard disk limit, or report
disk limits as monitored only and stop new sessions below a host free-space
floor. Never present a monitored limit as an enforced quota.

A Docker bridge network is not an outbound allowlist. For the first real
provider pilot, mark egress explicitly as `open` and require opt-in. Hardened
mode routes provider traffic through a host-controlled egress gateway that
permits only the selected provider endpoints and approved task destinations;
it must preserve TLS verification and support the provider's actual streaming
protocol. Test DNS rebinding, redirects, IP literals and CONNECT tunneling.
If the gateway cannot enforce the declared policy, hardened startup fails;
it does not silently fall back to open egress. A fixture worker can run with
`network=none` from the first stage.

A **gateway core** now lives in `doxa-isolation::egress`. The host
binds `egress.sock` beside the private session broker and accepts HTTP/1.1
`CONNECT` for exact owner-listed DNS hostnames on port 443. It resolves each
name on the host, rejects any private/reserved answer (including mixed public
and private answers), and requires a bounded TLS ClientHello with SNI matching
the `CONNECT` hostname before dialing the checked IP or forwarding worker
bytes. The first ClientHello is limited to 64 KiB of handshake bytes and 16
TLS records, so one-byte record fragmentation cannot amplify gateway buffering
or parsing work. Missing, duplicate, mismatched and known encrypted ClientHello names,
early data and extra handshake bytes are refused. TLS remains end-to-end. The
offline gateway fixture now changes an allowed DNS answer to a private address
between tunnels: it verifies that the first dial uses its checked address
without another lookup and that the second tunnel is rejected before dialing.
This tests the gateway's address binding, not provider DNS or redirects. The
worker's `egress-proxy PORT` command can bridge loopback
HTTP proxy traffic to that Unix socket in a `network=none` fixture. A fixture
can explicitly set `HTTP_PROXY` and `HTTPS_PROXY` to its loopback port. The
gateway socket is session-private; losing it produces a proxy error, and
dropping the gateway closes live fixture tunnels. Fake-upstream tests cover
the CONNECT handshake, matching and mismatched SNI, fragmented ClientHello,
binary relay, malformed/unknown targets, DNS answers, socket replacement and
gateway loss. The guarded `start_for_session` entry point requires a saved,
ready `docker-offline` manifest, a verified local rootless Engine and an
inspected network-none container before and after binding the socket.

No production profile starts the gateway or injects proxy variables yet.
The reserved hardened gateway entry point requires a ready offline manifest
and an exact per-session hard-quota proof. Current fixture receipts cannot
authorize it; even a changed receipt flag is refused until a live kernel
limit/restart verifier exists. A Linux XFS/ext4 read-only verifier checks the four
exact session directories, bounded existing descendants in the three data
binds, only the expected owner-private broker sockets, and the effective
project hard limit through open descriptors. A rootless controller can be
denied the XFS/ext4 project-limit query (`EPERM`/`EACCES`); hardened admission
then refuses. An uninstalled read-only helper now binds an administrator-owned
policy to the exact session tree and passed a disposable 21-case QEMU fixture;
it requires distinct trusted caller and tree-owner UIDs and always refuses
hardened admission. Installed-host systemd and runtime integration remain
open. The socket
inventory also permits zero sockets and does not tie a visible socket inode
to the live host listener. Exact live endpoint identity, broker peer/protocol
attestation, and production EDQUOT/restart/remount proof remain open. The
Codex compaction hook broker now checks the connecting Unix peer's rootless
owner UID, accepts one bounded `PreCompact` frame with a session-private
transcript path, and bounds its response. A disposable local Unix-socket test
also demonstrates the remaining gap: a same-UID host client with the bearer
capability passes those checks. Before hardened admission, bind every broker
connection to the exact inspected container and live process namespace,
reject host/sibling/changed-container peers, and rerun the protocol tests
after worker, daemon and Engine restart. A disabled Linux probe now obtains
`SO_PEERPIDFD`, cross-checks the pinned connector against `SO_PEERCRED`, and
observes its PID namespace and cgroup v2 path. It rejects host-scope
candidates, but an inherited-descriptor test proves that the pidfd still
names the original connector when another process writes the frame. No
production broker calls this probe.

A disabled Linux probe now enables `SO_PASSPIDFD` and `SO_PASSCRED` before
accepting a socket. Its 65,536-byte and five-second-bounded `recvmsg` loop
requires one kernel-supplied sender pidfd and credentials for every frame
segment and refuses empty frames, stalled or mixed senders, missing control
messages, and unsupported kernel options. An inherited
socket test shows `SCM_PIDFD` names the child that sent bytes, while the
connect-time credentials name its parent. This is stronger sender evidence,
but the probe is not wired into `HookBroker`: it has no authenticated binding
to the exact inspected Docker container, no proof that sender cgroup membership
cannot change after send, and no real provider hook compatibility result.
Startup must refuse hardened mode without these proofs.
The opt-in disposable quota fixture can compare aggregate writes through all
three data binds and recheck EDQUOT after restarting the same container. A
separate disposable four-bind guest proof retained EDQUOT on ext4 after an
Engine restart and source remount, with a working read-only broker socket;
an XFS control returned ENOSPC instead. The ext4 descriptor-bound syscall
branch passed a privileged read-only query against the disposable ext4
project-quota mount, while a rootless query, substituted root descriptor
(including the same inode through a different bind mount), wrong project/limit,
changed broker project and disabled enforcement all
refused. The [fixture procedure](../hard-quota-preflight.md#disposable-read-only-kernel-query)
is executable; it accepts caller-supplied policy and cannot serve as a
production privileged helper or admission token.
`docker-hardened` is explicitly refused at
profile parsing, with no fallback. A credential-free rootless smoke can test
Python `HTTPS_PROXY` streaming, denied second-host access and failed direct
fallback for one selected upstream; it does not prove real provider behavior.
Before a `docker-hardened` profile can be offered, run actual Claude, Codex
and API-vendor streaming/login/refresh flows through the proxy in a rootless
`network=none` container; verify that each provider honors the proxy for
every outbound path, handles redirects without bypass, and cannot reach the
network by any alternative route. Bind the allowlist to a reviewed per-session
policy/manifest, audit resolver behavior and operational limits, and test
gateway death/restart and resume across container lifecycle transitions.
Because TLS is not intercepted, the gateway cannot check the encrypted HTTP
authority or detect every form of domain fronting. Known ECH extensions are
refused, but provider-specific destination behavior and future TLS extensions
must be reviewed before claiming a hardened boundary. An opt-in, credential-free
rootless fixture now checks the network-none worker's loopback adapter, a
disallowed CONNECT, direct-IP failure and gateway-loss behavior. Permitted
upstream traffic can be probed separately against one operator-selected,
credential-free public HTTPS hostname with certificate verification, a denied
CONNECT and direct-hostname bypass refusal. This ignored rootless test records
transport evidence for that exact host and image only; provider flows, redirects,
all alternate outbound paths and production admission still need live proof.

API vendor keys stay in the host supervisor; it performs provider HTTP calls
or grants a narrowly scoped per-session provider proxy. Claude and Codex may
need session-private auth files for their native CLIs. Those copies are never
the user's full config directory, and their exposure to agent-launched tools
is recorded as a remaining limitation. Full credential secrecy for a CLI
requires a provider-specific auth boundary that still works when the CLI
executes tools; it is a separate release gate before claiming such secrecy.
GitHub, SSH and 1Password access go through explicit user-approved host
operations, not broad credential or socket mounts.

## User-facing contract

Owner configuration in `DOXA_HOME/config.toml` selects
`session_isolation = "native" | "docker-open" | "docker-offline"`; a CLI flag and TUI picker can
override it for a **new** session. Repository files and agent tools cannot
weaken it. A saved session records its backend, image digest, policy hash,
checkout identity, container ID and host runtime paths in an owner-private
manifest. Resume uses the recorded backend and policy or refuses with a
specific migration path; it never silently resumes a Docker session natively.
Switching backend for an existing session requires an explicit export/import
operation while idle, with transcript and Git state verified.

The manifest is versioned and includes session ID, backend, base SHA, clone
path and inode, branch, image digest, policy hash, broker directory, container
ID, creation nonce and last verified state. The state machine is `preparing →
created → ready ↔ detached → stopping → stopped`, with `failed` and
`unavailable` terminal/reconciliation states. A crash after container creation
is resolved by matching both the private manifest and Docker labels, then
inspecting the actual image and mounts. A mismatched or second live container
for one session is quarantined for manual review, never adopted or replaced
automatically.

Show `native`, `docker · open egress`, `docker · hardened`, or `unavailable`
in session details and a compact isolation chip. The hover text names the
actual engine, network, mounts, resource limits and credential exposure. The
Codex permission picker remains independent: `full-access` can disable
Codex's internal sandbox, but it cannot remove the DOXA container boundary.
Approval decisions continue to go through the host supervisor. `Ctrl+Q` or
closing a tab detaches without stopping its container; an explicit stop ends
the session. An abandoned or dead container is shown as recoverable or
failed, not silently launched again with a new policy.

If Docker is missing, rootless prerequisites fail, the digest differs, or a
required hardening control is unavailable, a Docker-requested session does
**not** start natively. The error names the failed preflight. The native
backend remains an explicit choice. A policy exception applies to one
session only and is never inherited by children or persisted as a new default.

## Docker when DOXA itself runs in Docker

There are three distinct deployments:

1. **Host DOXA + rootless Docker** is the first supported production path.
   The trusted host supervisor alone can reach the rootless Engine socket.
2. **Controller in Docker + host rootless Engine** is the next path. The outer
   controller may reach a dedicated rootless Engine socket, but its worker
   containers are siblings of the controller, and no worker receives that
   socket. Because the controller can create containers and bind host paths,
   the outer container is packaging, **not** a separate security boundary.
   Constrain this controller with a dedicated host user/Engine and the same
   host-side mount policy; reject remote TCP Engine endpoints initially.
3. **True Docker-in-Docker** is an opt-in development/CI backend only. Docker's
   documented rootless DinD setup still requires a privileged outer
   container. It must not be presented as host isolation or enabled by the
   hardened profile. Production nested isolation needs an independently
   reviewed VM or equivalent boundary before support.

Do not mount `/var/run/docker.sock` or the rootless Engine socket into a
session worker. Such a mount gives the worker Docker control over the host
Engine. Do not use Docker group membership as a substitute for isolation.

## Implementation stages and gates

### Stage 0 — inventory and protocol split

Introduce a `SessionLauncher`/`SessionRuntime` interface used by every launch
and resume path, with native behavior unchanged. Separate host-owned state and
approvals from provider/tool execution. Define the versioned broker protocol,
manifest schema, exact session identity checks and reconciliation states.
Add an ownership map for every filesystem path and secret each engine opens.

**Gate:** native regression suite passes; a fixture worker can exercise the
broker without access to the host registry or LORE database; all launch paths
are enumerated and covered by one shared launcher test.

### Stage 1 — local rootless Docker fixture

Add a Docker Engine adapter using the local Unix API, a pinned minimal image,
preflight checks, create/start/inspect/stop/remove, private broker mount and
manifest write. Run only the deterministic fixture engine with `network=none`,
no credentials and no repository write access. Reconcile after TUI and
supervisor restart by comparing the manifest, Docker labels, image digest,
mounts and container state. Reuse the existing session ID; never create a
second writer for the same session.

**Gate:** fixture detach/reattach/restart works; a killed supervisor leaves a
recoverable container; startup failure reaps only the container it created;
negative tests cannot read host home, a sibling session, Docker socket or
another broker. Rootless prerequisites and effective cgroup limits are
verified, not assumed.

### Stage 2 — coding-session pilot

Implement independent checkout creation/import, then run Claude, Codex and
the API vendors through the container worker one engine at a time. Keep the
existing turn stream, tool-call expansion, reasoning count, approvals,
interrupts, model/effort changes, compaction, LORE actions, peers, child
spawns, remote control and transcript recovery. A child uses the same Docker
backend unless the host applies a stricter one. Verify Codex's native sandbox
inside the container; if it cannot initialize, report its status accurately
while the DOXA container boundary remains enforced. Test Claude login and
token refresh against a session-private credential copy.

**Gate per engine:** real end-to-end session and resume; Git edit/commit and
host import; cross-session write/read denial; approval and permission-mode
semantics; no access to host credentials except the documented provider copy;
two simultaneous sessions cannot exchange files except through DOXA peers.
The pilot remains opt-in and labels open egress accurately.

### Stage 3 — hardened network, secrets and resources

Add the egress gateway, measured resource profiles, hard disk quotas where
supported, session-private credential lifecycle and explicit host operations
for external credentials. Pin and verify image digests through install/update.
Report each property separately; a green hardened state requires all of
them. Audit symlink, mount-race, Docker API, broker, prompt-injection,
container escape and denial-of-service paths. Benchmark startup, redraw
impact, Git clone/import cost and concurrent memory/disk use against native.

**Gate:** egress bypass tests fail closed; no host/sibling state leak; limits
hold under a deliberate resource spike; remote control cannot widen the
container policy; security review signs off on exactly the claimed boundary.

### Stage 4 — rollout and nested-controller support

Offer Docker as the recommended Linux default only after Stage 3 gates pass.
Ship a reversible migration guide and explicit native choice. Add the
controller-in-Docker deployment with a dedicated rootless host Engine and
re-run all mount, identity, recovery and resource tests there. macOS Docker
Desktop and Windows are separate platform projects: path translation,
filesystem ownership and daemon location differ, so neither inherits a Linux
security claim without its own verification. True DinD stays CI/dev only.

**Gate:** two-host remote sessions, local/remote mixed tabs, detach/restore,
installer/update and release smoke tests pass; one-session rollback to native
preserves transcript and Git state and requires an explicit user action.

## Required adversarial tests

- From inside session A, attempt to read/write the host home, main checkout,
  session B checkout and state, shared Git metadata, Tailscale and Docker
  sockets, and session B broker endpoint. Each attempt must fail.
- Change a bind source into a symlink between validation and container create;
  refuse or detect the changed identity. Inspect mounts after creation.
- Run `git status`, commit, diff, branch, import and finalize on A while B is
  active; A must not move B's or main's refs.
- Kill the worker, supervisor, Docker Engine and TUI in turn; reconcile without
  duplicate provider turns, duplicate writes or loss of committed transcript.
- Exercise Claude and Codex approval, full-access, model changes and tool
  execution; container restrictions must remain even when provider permission
  mode changes.
- Use a malicious repository config, Dockerfile, symlink and shell tool call
  to request extra mounts, network access or Docker control; no policy change
  may result.
- Run a large build and a runaway process tree; check CPU, memory, PID and
  disk behavior, including the distinction between hard quota and monitoring.
- Attempt to bypass egress rules with direct IP, alternate DNS, redirects and
  proxy tunneling; hardened startup fails if these cannot be blocked.

## References

- [Docker rootless mode](https://docs.docker.com/engine/security/rootless/)
- [Docker Engine security and daemon control](https://docs.docker.com/engine/security/)
- [Docker bind mounts and daemon-host paths](https://docs.docker.com/engine/storage/bind-mounts/)
- [Docker resource constraints](https://docs.docker.com/engine/containers/resource_constraints/)
- [Docker network drivers](https://docs.docker.com/engine/network/drivers/)
- [Docker rootless DinD guidance](https://docs.docker.com/engine/security/rootless/tips/)
