# Session isolation

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

Configure a reviewed worker image with docker_image = "sha256:CONTENT_ID" or
"NAME@sha256:DIGEST" in the private DOXA config. An image must contain the
image-owned doxa-isolation-worker, Claude CLI and protected Codex launcher.
DOXA does not pull images or build a repository's Dockerfile at session start.
The docker_host setting accepts only a local owner-owned Unix Engine socket.
The system rootful socket is refused; the Engine must report rootless operation
and effective cgroup v2 memory, CPU and PID controls.

Defaults are 4 GiB memory, 2 CPUs and 256 processes. Set docker_memory_bytes,
docker_cpus and docker_pids for your workload. These are enforced resource
ceilings. Disk usage has no hard quota: startup checks a 2 GiB free-space floor.
Session data belongs on real disk.

Each worker receives exactly its independent Git checkout, private home,
private cache and session hook endpoint. It has no host home, main checkout,
shared Git metadata, peer registry, LORE store, SSH agent or Docker socket.
The image filesystem is read-only; capabilities are dropped and
no-new-privileges is enabled. Root inside a rootless container maps to its
unprivileged host Engine owner, and a real write probe verifies this mapping.
CLI credentials are copied only for the selected provider; tools in that
container can read those credentials. Canonical DOXA transcripts, LORE,
approvals and peer routing remain in the host daemon.

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
app-server upgrade before migrating. Workspace copies refuse special files,
more than 100000 entries or more than 8 GiB, retaining original files.

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
There is no hardened egress gateway, hard disk quota or CLI credential secrecy.
macOS Docker Desktop, remote Engines and nested privileged Docker are refused.

For the explicit rootless integration smoke, supply a task-local reviewed image
and Engine socket:

    DOXA_ISOLATION_TEST_IMAGE=sha256:CONTENT_ID \
    DOXA_ISOLATION_TEST_HOST=unix:///run/user/UID/doxa-test-docker.sock \
    cargo test -p doxa-isolation --test docker -- --ignored
