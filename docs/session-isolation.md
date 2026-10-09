# Session isolation

## Contents

- [Choose a profile](#choose-a-profile)
- [Configure the local Engine](#configure-the-local-engine)
- [Resource limits and disk monitoring](#resource-limits-and-disk-monitoring)
- [Change a running session](#change-a-running-session)
- [Boundary and recovery](#boundary-and-recovery)
- [Integration smoke](#integration-smoke)

## Choose a profile

Native sessions retain the existing host provider behavior. Linux sessions can
explicitly run their provider and its commands in one local rootless Docker
container:

    doxa new --engine claude --isolation docker-open
    doxa new --engine codex --isolation docker-open

Select the profile in the new-session form with Left/Right. The owner default is
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
image-owned doxa-isolation-worker, Claude CLI and protected Codex launcher.
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

Defaults are 4 GiB memory, 2 CPUs and 256 processes. Set docker_memory_bytes,
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

Docker's writable-layer `--storage-opt size` does not bound these host bind
mounts. A hard per-session disk quota needs an administrator to enable project
quota accounting and enforcement on the backing filesystem, assign a unique
project ID to each private session tree, set a hard block limit, and verify
that writes through every bind source hit that limit. For XFS, that means a
`prjquota` mount and project setup/limit with `xfs_quota`; owner-only rootless
Docker cannot silently provision this. Hard quotas remain an open stage.
The [hard-quota fixture guide](hard-quota-preflight.md) covers read-only
descriptor-bound XFS checks for project identity, inheritance, bounded
existing data-bind descendants and the exact enforced block limit, plus a
bounded, opt-in rootless container write probe
for an administrator-prepared fixture. A successful fixture `EDQUOT`
receipt is never production admission; per-session quota provisioning,
restart/remount verification and a reviewed runtime admission path remain open.
An opt-in privileged helper can read the exact administrator-pinned project
limit and five directory identities for a separate trusted caller UID. It
always returns `admissible_as_hard_quota=false` and is not installed or called
by the session launcher. The current same-UID controller and worker layout
cannot use that trust split without further work.
`docker-hardened` requests fail with that explicit gate; there is no fallback.
See [Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/),
[Docker's storage option requirements](https://docs.docker.com/reference/cli/docker/container/run/),
and [XFS project quotas](https://man7.org/linux/man-pages/man8/xfs_quota.8.html).

Each worker receives exactly its independent Git checkout, private home,
private cache and session hook endpoint. It has no host home, main checkout,
shared Git metadata, peer registry, LORE store, SSH agent or Docker socket.
The image filesystem is read-only; capabilities are dropped and
no-new-privileges is enabled. Root inside a rootless container maps to its
unprivileged host Engine owner, and a real write probe verifies this mapping.
CLI credentials are copied only for the selected provider; tools in that
container can read those credentials. Canonical DOXA transcripts, LORE,
approvals and peer routing remain in the host daemon.

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

Implementation limits: this is a Linux rootless Docker boundary, not a VM.
The restricted-egress gateway has an exact hostname/443 `CONNECT` allowlist and
checks TLS SNI before dialing the pinned DNS answer. It rejects ECH, TLS early
data and extra handshake bytes in a network-none fixture. An offline rebinding
test checks that a later private DNS answer cannot reach the connector and that
the first tunnel never re-resolves after validating its public answer. The gateway
is not wired into a production profile. Provider streaming, login, refresh and
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
login/streaming evidence. The hardened profile remains unavailable.
