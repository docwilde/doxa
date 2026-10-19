# Session isolation

Fleet-specific defaults, owner confirmations and runtime menus: [Fleet access and isolation](fleet-access.md).

## Contents

- [Choose a profile](#choose-a-profile)
- [Configure the local Engine](#configure-the-local-engine)
- [Resource limits and disk monitoring](#resource-limits-and-disk-monitoring)
- [ROOT-controlled hardened sessions](#root-controlled-hardened-sessions)
- [Change a running session](#change-a-running-session)
- [Boundary and recovery](#boundary-and-recovery)
- [Integration smoke](#integration-smoke)

## Choose a profile

Native sessions retain the existing host provider behavior. Linux sessions can
explicitly run their provider and its commands in one local rootless Docker
container:

    doxa new --engine claude --isolation docker-open
    doxa new --engine codex --isolation docker-open

In New Session, click the Isolation field or press Enter to open its dropdown;
Up/Down chooses a profile and Enter confirms it. Docker choices show local image
policy and owned canonical socket prerequisites before Start. Missing setup
keeps the selected model, profile and first prompt in place.

Click **Docker image Settings** or press **Ctrl+O** to open **Settings → Session →
Docker worker image digest**. Ctrl+S saves staged settings; Esc returns to the
same New Session draft and refreshes the prerequisites. Configure the local
socket under **Settings → Paths → local rootless Docker socket** if it differs
from `unix:///run/user/UID/docker.sock`. The check reads local configuration and
socket metadata only: Start still requires the Engine to prove rootless mode,
effective cgroup limits and the pinned image's local availability.

The owner default is
configured with doxa settings set session_isolation PROFILE, selecting native,
docker-open or docker-offline. Open egress permits worker Internet access.
Docker offline has no worker network: ordinary online Claude and Codex turns
cannot contact their providers in that profile. API-vendor requests are
performed by the host using the selected provider API; their bounded workspace
tool remains confined to the independent checkout. Neither Docker profile
claims an outbound allowlist.

## Configure the local Engine

Configure a reviewed worker image with docker_image = "sha256:CONTENT_ID" or
"NAME@sha256:DIGEST" in the private DOXA config. An image must contain the
image-owned doxa-isolation-worker and the native CLI selected for that session
(Claude CLI or the current protected Codex launcher). Follow the explicit
[rootless Engine and reviewed worker image setup guide](../containers/session/README.md)
before selecting Docker. The builder accepts the current installer/patch receipt
and refuses the legacy protected Codex package; rebuild after upgrading it.
DOXA does not pull images or build a repository's Dockerfile at session start.
Rebuild the reviewed image with the current `doxa-isolation-worker` after upgrading
DOXA; older workers cannot perform the required cgroup probe and are refused.
The docker_host setting accepts only a local owner-owned Unix Engine socket.
The system rootful socket is refused; the Engine must report rootless operation
and cgroup v2 memory, CPU and PID support. Before admission and each CLI provider
turn, the worker checks its own finite kernel cgroup memory, CPU, PID and swap
limits. An unavailable controller or ineffective limit refuses the turn.
The [remote Engine fixture preflight](remote-engine-preflight.md) evaluates
mock rootless, broker, mount and cgroup evidence without connecting to an
endpoint. It cannot authorize remote Docker or Docker Desktop; both remain
unavailable until daemon-host and worker proofs run in the admission path.

## Resource limits and disk monitoring

For `docker-open` and `docker-offline`, defaults are 4 GiB memory, 2 CPUs and 256 processes. Set docker_memory_bytes,
docker_cpus and docker_pids for your workload. The worker verifies their
effective ceilings in its private cgroup namespace before use. For **new Docker sessions**, `docker_disk_soft_limit_bytes` defaults
to 20 GiB and `docker_disk_free_floor_bytes` to 2 GiB. Set them in private
`DOXA_HOME/config.toml`, the settings UI, or with
`DOXA_DOCKER_DISK_SOFT_LIMIT_BYTES` and `DOXA_DOCKER_DISK_FREE_FLOOR_BYTES`.
The soft ceiling accepts 128 MiB–1 TiB; the floor accepts 512 MiB–1 TiB.
Saved sessions keep their original policy. Existing beta.10 manifests have no
soft ceiling and retain the 2 GiB floor.

DOXA samples allocated blocks in the whole private session tree, including
the independent Git checkout, provider home, cache and retained migration
files. Hard links count once; symlink targets outside the tree are not scanned.
It checks the smallest available space across the session root and its bind
sources. The sample appears in the isolation chip details. Launch, resume,
migration and each new provider turn refuse to proceed when a threshold is
crossed or the bounded scan fails. A scan is bounded to one million entries,
128 directory levels and ten seconds. A running worker can write between
samples, and another process can consume host space after a check. **These
are monitored turn gates, not a hard filesystem quota.** Keep session data
on real disk.

Docker's writable-layer storage limit does not bound host bind mounts. The
legacy profiles retain monitored soft limits. A Linux `docker-hardened` session
instead uses the independently installed ROOT quota and broker described below;
its caller mirror is never used to measure or authorize worker disk usage.
Historical [quota fixture receipts](hard-quota-preflight.md) and advisory helper
results remain non-authorizing.

Each worker receives exactly its independent Git checkout, private home,
private cache and session hook endpoint. It has no host home, main checkout,
shared Git metadata, peer registry, LORE store, SSH agent or Docker socket.
The image filesystem is read-only; capabilities are dropped and
no-new-privileges is enabled. Root inside a rootless container maps to its
unprivileged host Engine owner, and a real write probe verifies this mapping.
CLI credentials are copied only for the selected provider; tools in that
container can read those credentials. Canonical DOXA transcripts, LORE,
approvals and peer routing remain in the host daemon.

## ROOT-controlled hardened sessions

`docker-hardened` uses a separate unprivileged Engine identity and an
administrator-installed policy for one reserved session ID. The caller cannot
open the Engine socket or access its data group. The caller's checkout and home
are private staging/review mirrors; the actual four binds live under the
ROOT-controlled quota tree. Staging sends bounded typed bytes through ROOT IPC.
Provider CLI/catalog launches stage the selected prepared home again, with no
full-home copy or host-filesystem fallback.

An administrator must first prepare an enforcing ext4 project-quota filesystem,
allocate the session's project ID, install reviewed quota/native broker binaries
and the isolated Python broker, and pin the image, worker/provider artifacts,
Engine executable, explicit configuration, separate UIDs, exact DNS egress and
finite resource limits. The Engine socket is Engine-owned `0600`; its config and
all ROOT policy/code ancestors exclude non-ROOT writers. See the
[quota provisioning guide](root-controlled-quota.md) and
[ROOT broker installation/protocol guide](../scripts/hardened-isolation/README.md).
Installation receipts and public instance files pin scope/code/process identity;
they do not authorize a container. Every prepare/resume and provider/workspace
operation requires fresh native kernel authority. Persistent provider turns use
a live ROOT guard rather than advisory disk monitoring.

Because a policy reserves one ID, ordinary New Session generation cannot
provision a different ID implicitly. The supported operator entry point is the
host daemon with that exact reserved ID, followed by frontend attach. With a
private `DOXA_HOME` and runtime directory, for example:

```sh
DOXA_HOME=PRIVATE_HOME DOXA_RUNTIME_DIR=PRIVATE_RUNTIME \
  doxa-daemon-rs --session-id RESERVED_ID --runtime-dir PRIVATE_RUNTIME \
  --cwd SOURCE_CHECKOUT --engine claude --isolation docker-hardened
DOXA_HOME=PRIVATE_HOME DOXA_RUNTIME_DIR=PRIVATE_RUNTIME doxa attach RESERVED_ID
```

The daemon uses the caller's configured runtime directory/registry. A later
restart uses the same daemon arguments with `--resume true`; ROOT rechecks and
retires the previous scope before preparing a fresh CID over retained data.
A missing session policy refuses before creating a caller checkout or invoking
an Engine. Do not put this profile into a default that generates unprovisioned
IDs. The root broker service is an explicit administrator operation, never a
launcher side effect. Its unit shares the installation mount namespace so boot
and unique mount pins remain meaningful.

ROOT shutdown reserves a gate for new work and waits for already admitted
provider/workspace transports to settle within the installed operation deadline.
It releases the state lock while waiting so those transports can finish their
cleanup. New turns, execution, staging, prepare/resume and hook registration are
refused while the gate is pending. Existing transports, snapshots and hook
unregistration can finish. Timeout, disconnect or cleanup refusal retains the
gate; an explicit typed ROOT stop retry must finish before prepare/resume can
proceed. A `stop_if_idle` acknowledgement precedes this cleanup; the daemon's
clean exit confirms its completion. See the
[broker lifecycle contract](../scripts/hardened-isolation/README.md).

The native gateway holds Engine/init pidfds, immutable ROOT files and quota
descriptors. It checks exact inspect on its pinned Engine connection, private
namespaces, finite effective cgroups, all worker thread filters and descriptors,
and the physical ROOT listener behind each permitted broker socket. Every frame
segment has kernel sender credentials/pidfd matching the connector. Distinct
UIDs, an immutable private PID namespace, read-only cgroup controls, closed
namespace/control descriptors and the mandatory worker syscall filter exclude
worker-initiated migration; sequential cgroup observations are never described
as an atomic send-time measurement. Host, sibling, delegated-descriptor and
changed-device origins are refusal cases. No caller-supplied `ready` or fixture
flag replaces these checks.

Rootless FUSE image reads use a bounded child in the pinned init **user**
namespace. ROOT retains Engine, cgroup, socket and quota authority. The child
closes control descriptors, clears groups/capabilities, sets no-new-privileges
and non-dumpable state, and uses a default-deny syscall filter. Executable reads
permit scalar-flag read-only opens and deny `openat2`; fixed image artifact reads
use anchored paths plus exact-file Landlock. Metadata reads use preopened
`O_PATH` handles without file-content authority. Landlock ABI 5 or newer is
required for those operations. The child returns bounded facts, reopens actual
executables/artifacts after hashing and rechecks held init/namespace identities.
The executable operation uses a fixed typed `/proc/init/exe` request, but its
scalar-open filter is not a kernel pathname allowlist: compromised native
reader code could read other DAC-readable files under the dropped Engine UID.
It has no file-write, service or ROOT admission capability. Artifact operations
have the narrower exact-file Landlock grant; these are distinct guarantees.
It may inherit allocator locks when forked from the multithreaded gateway; it is
not an exec-clean process. An absolute deadline refuses, kills and boundedly
reaps a stalled child. Parent pidfd checks and parent-death signals bracket UID
transitions; EOF cannot turn into an unbounded wait.

Codex snapshots identify the actual Engine-owned original rollout, including
full bytes/hash and filesystem identity. Review uses a separate caller-owned
`0700` directory and `0600` file. The host callback authenticates the ROOT
process through its independently published instance, live pidfd/start time,
fresh nonce and per-segment sender identity. Core rechecks both the review copy
and the original after approval and before the compact RPC; the native relay
reopens the original again before answering the worker hook. A copied file alone
cannot approve a changed original. Workspace Git uses bounded ROOT-generated
readonly metadata on the existing broker mount, excluding tenant configuration,
filters/hooks/includes/replacements and submodule traversal. Ordinary apply may
change working files; `--cached` and `--index` are refused.
Metadata is built in a ROOT-private directory on the same quota filesystem,
then published/retired atomically. An existing fixed ROOT `0600` lock coordinates
exclusive publication with bounded shared native quota audits. Its presence
grants no authority; missing, changed or contended installation fails closed.

Disposable proof scope is recorded beside each receipt. Project quota
aggregate/EDQUOT, container/Engine restart and source-remount checks, real ELF32
ioctl refusal, actual UID1000-to-ROOT broker authentication and actual UID1001 Git
filter-marker checks have passed. The
[credential-free exact-origin run6](../scripts/isolation-authority-evidence/20261013/RUN.md)
also passed kernel checks, inside/private-PTY frames and six refusal cases, with
exact container/cgroup/socket cleanup. It reports diagnostics only. The
[composed Runtime receipt](composed-runtime-proof-20261013.json), attempt 6,
passed actual prepare/activation, persistent Codex owner/guard handling, original
source snapshot and private review-copy checks, a fixture approval callback,
stale-source refusal, readonly Git metadata and filter rejection, ordinary apply,
and staged Claude settings. Cold resume produced a fresh CID and passed its live
guard and stop; it does not prove provider checkpoint or session conservation.
The body completed in 30.6 seconds. Both exact CIDs returned 404 after teardown;
owned processes, cgroups and physical/kernel sockets were absent and the shared
Engine identity was unchanged. The separate
[daemon/frontend run 6](../scripts/isolation-daemon-evidence/20261013/RUN.md)
also passed. One actual UID1000 frontend prompt crossed the IsolationHost ROOT
guard and produced the exact fixture start, text and done events, rendered
response and idle status. The frontend reached complete PTY EOF, the daemon and
owned broker exited cleanly, and ROOT stop confirmed the exact CID absent.
Owned processes, cgroups, sockets and registry entries were absent after cleanup;
the shared Engine was unchanged. The earlier failed attempts remain recorded.
Historical passes never grant admission for a different image/policy. Canonical
human approval, real Claude/Codex streaming, login/refresh, redirects and provider-specific alternate
outbound behavior are not claimed by the dummy fixtures. TLS remains end-to-end;
the gateway checks CONNECT/SNI/public addresses, not encrypted HTTP authority.

Hardened/native or hardened/legacy-Docker migration is refused **before provider
shutdown** until a typed stopped-workspace and transcript export/import is
implemented. Source data remains available under its original policy. Explicit
logout/deletion, changed adopted-snapshot replacement and arbitrary home writes
also require separate reviewed operations.

## Change a running session

The isolation chip reports the host-verified policy. Click it for the actual
engine, network, limits, mounts and credential exposure. While idle and without
queued turns or pending approvals, explicitly change the isolation profile:

    /isolation docker-offline --confirm
    /isolation docker-open --confirm
    /isolation native --confirm

Docker network changes retain the provider process and checkout. Native/Docker
changes stop and checkpoint the provider, preserve the original native checkout,
copy its complete workspace into an independent clone, import the selected
provider context, and verify same-session resume. Staged and working changes,
deletions, ignored/untracked files and symlinks are preserved. Docker-to-native
continues in the private clone and provider home. Conversation ID, transcript,
LORE project identity, model, effort and permissions remain attached. The chip
changes after backend and resume verification. A failed migration restores the
preceding profile and resumes its original checkpoint; any incomplete recovery
is reported explicitly. The Codex permission picker remains independent of the
container boundary. Legacy Codex exec sessions require their explicit protected
app-server upgrade before migrating. Codex and Claude need a completed clean
turn and an owned provider context before a backend change; a pre-first-turn
request is refused while its original provider stays running. API vendor sessions
can checkpoint and migrate before the first prompt. Workspace copies refuse special files,
more than 100000 entries or more than 8 GiB, retaining original files.

## Boundary and recovery

The owner-private DOXA_HOME/isolation/SESSION_ID/manifest.json records the pinned
image, current and creation policy hashes, base commit, checkout inode,
container ID and creation nonce. Resume uses that manifest. Docker
unavailability, mismatched mounts, duplicate containers or a changed policy
fail closed. Existing dirty clones and their containers are retained for
inspection; DOXA never removes their work or resumes them natively as fallback.

Detach keeps the trusted daemon and its container running until its configured
idle expiration. Explicit stop ends the container. Closing a provider's host
transport causes the image-owned worker to end its provider process, avoiding
an orphan provider writer when the supervisor crashes.

Host Git and Docker controller commands have one 45-second elapsed deadline
covering the direct child and EOF on both output pipes. A descendant retaining
either pipe cannot extend that limit. Timeout, read failure or excessive stdout
signals the command's process group before closing the read ends, with at most
one extra second to confirm direct-child reaping. Unconfirmed cleanup is an
explicit failure alongside the original operation error; a signal refusal is
not treated as verified cleanup. The helper does not retry the command or select native fallback.
This stops local client processes, not an Engine operation already submitted:
retained manifests and containers still need the normal reconciliation checks.

The defect was reproduced with the released helper and a local fake subprocess
whose direct child exited while a descendant retained its pipes. Local regression
fixtures cover inherited stdout/stderr, live children, closed pipes before child
exit, normal completion and output overflow. No real Docker/Engine hang or
rootless container cleanup was exercised by these fixtures.

Implementation limits: this is a Linux rootless Docker boundary, not a VM.
The restricted-egress gateway has an exact hostname/443 `CONNECT` allowlist and
checks TLS SNI before dialing the pinned DNS answer. It rejects ECH, TLS early
data and extra handshake bytes in a network-none fixture. An offline rebinding
test checks that a later private DNS answer cannot reach the connector and that
the first tunnel never re-resolves after validating its public answer. The gateway
also checks the kernel writer of guarded CONNECT, ClientHello and later
worker-to-upstream relay chunks against the original connector. It rechecks
the connector's observed container scope before forwarding each relay chunk
and closes both tunnel directions on a rejected writer. These are sequential
observations; process movement between send, inspection and forwarding and
exact Engine/container identity remain unproven.
The gateway is not wired into a production profile. Provider streaming, login, refresh and
bypass tests in a rootless container remain the gate. There is no hard disk quota
or CLI credential secrecy.
macOS Docker Desktop, remote Engines and nested privileged Docker are refused.

## Integration smoke

For the explicit rootless integration smoke, supply a task-local reviewed image
and Engine socket:

    DOXA_ISOLATION_TEST_IMAGE=sha256:CONTENT_ID \
    DOXA_ISOLATION_TEST_HOST=unix:///run/user/UID/doxa-test-docker.sock \
    cargo test -p doxa-isolation --test docker -- --ignored

The opt-in restricted-egress transport smoke uses a pinned, credential-free
fixture image with the current isolation worker and Python 3. It starts a
`docker-offline` session, checks a disallowed `CONNECT`, verifies that direct
public-IP access fails, and checks that gateway loss returns 502 rather than
falling back to direct access:

    DOXA_ISOLATION_TEST_IMAGE=sha256:CONTENT_ID \
    DOXA_ISOLATION_TEST_HOST=unix:///run/user/UID/doxa-test-docker.sock \
    cargo test -p doxa-isolation --test egress_docker \
      network_none_worker_reaches_only_guarded_gateway_and_fails_closed_on_loss -- --ignored

Use a task-local rootless Engine and a real-disk `TMPDIR` for the fixture. It
does not enable a production restricted-egress profile.

To probe one explicitly selected public HTTPS upstream without credentials,
choose a DNS hostname whose `/` response is 2xx or 3xx and whose certificate
is trusted by the pinned fixture image. This opt-in test sends `GET /` through
the worker's loopback proxy, validates TLS and the response, then verifies a
denied `CONNECT` and direct hostname and public-IP connection failures:

    DOXA_ISOLATION_TEST_IMAGE=sha256:CONTENT_ID \
    DOXA_ISOLATION_TEST_HOST=unix:///run/user/UID/doxa-test-docker.sock \
    DOXA_ISOLATION_TEST_EGRESS_UPSTREAM=PUBLIC_HTTPS_HOST \
    cargo test -p doxa-isolation --test egress_docker \
      network_none_worker_reaches_one_allowlisted_public_https_upstream_only -- --ignored

The test sends no token, cookie, URL query or request body and does not follow
redirects. A pass proves only that this exact hostname's TLS and HTTP transport
worked in the selected image and rootless Engine. Provider streaming, login,
refresh, redirects and alternate outbound paths remain unverified.

An additional ignored smoke uses Python's `HTTPS_PROXY` handling to read a
response in chunks, attempts a second denied destination, and verifies that a
direct fallback still fails in `network=none`. Select a public HTTPS host with
a 2xx `/` response and a nonempty body:

    DOXA_ISOLATION_TEST_IMAGE=sha256:CONTENT_ID \
    DOXA_ISOLATION_TEST_HOST=unix:///run/user/UID/doxa-test-docker.sock \
    DOXA_ISOLATION_TEST_EGRESS_UPSTREAM=PUBLIC_HTTPS_HOST \
    cargo test -p doxa-isolation --test egress_docker \
      network_none_provider_style_proxy_stream_denies_second_host_and_direct_fallback -- --ignored

This is a standard-library client pattern, not actual Claude, Codex or vendor
login/streaming evidence. The ROOT profile has a separate installed authority
and composed verification path described above.
