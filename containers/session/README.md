# Reviewed Docker session image

The host daemon owns transcripts, LORE, approvals, peers and the Docker API.
The image contains the isolation worker and explicitly selected provider
binaries. Each session receives an independent Git checkout and private home
and cache. The Docker socket and host home are never mounted into a worker.

Build the worker with `cargo build --locked -p doxa-isolation`. Use a local
rootless Docker Engine and a reviewed Debian/Ubuntu-based base image pinned
as `NAME@sha256:DIGEST`:

Rebuild the image when DOXA's isolation worker changes. An older pinned image
cannot answer the current worker's cgroup enforcement probe, so session
admission fails closed until its digest is updated in owner configuration.

```sh
export TMPDIR="$HOME/ssd-cache/tmp"
python3 scripts/build-session-image.py \
  --docker-host "unix:///run/user/$(id -u)/docker.sock" \
  --base-image YOUR_REVIEWED_BASE_IMAGE_AT_SHA256 \
  --worker target/debug/doxa-isolation-worker \
  --claude-bin "$HOME/.local/bin/claude" \
  --codex-provider "$HOME/.local/share/doxa/providers/codex-current"
```

The Codex receipt must match the current `scripts/install_codex_protected.py`
source/contract and the SHA-256 of `scripts/codex-protected/precompact.patch`.
Legacy receipts are refused; rerun the protected provider installer and rebuild
the worker image after an upgrade.

Both provider flags are optional. Omit them for a credential-free fixture
image. The build copies only the reviewed worker, selected Claude executable,
and five verified protected-Codex package files. It preserves the existing
Codex receipt and refuses changed, shared, public or symlinked package files.
It never copies provider login files. DOXA prepares the selected provider's
session-private authentication separately when the session starts.

The script prints the exact local image content ID and writes build evidence
to `target/isolation-image/last-build.json`. Set `DOXA_DOCKER_IMAGE` to that
`sha256:…` ID, or to a published `NAME@sha256:DIGEST`. Use
`DOXA_DOCKER_HOST` for the same rootless Engine. A tag alone is insufficient.

The base must supply the toolchain required by the repository being edited.
The Dockerfile adds Git, Bash, Python and CA certificates; it does not select
a Rust, Node or other project toolchain. Protected Codex's Code Mode binary
requires glibc 2.39 or newer in the currently installed Linux package. Check
the binaries against the chosen base and rebuild after a provider upgrade.

The original `official_cli` receipt path is not relocated into the image.
The protected launcher supports DOXA's `app-server` and Code Mode paths;
official CLI login/help remains on the host. Provider credentials copied to
a session-private home are visible to tools in that session.

Docker with open egress permits worker network access. Docker without
networking also prevents Claude/Codex from reaching their provider until
networking is re-enabled. CPU, memory and PID limits are enforced separately;
disk use is monitored and is not a filesystem quota. These profiles do not
claim a provider-only egress gateway or full credential secrecy.

## Configure and return to New Session

**`/setup` → Docker setup** (after model/effort defaults) checks the local
prerequisites, shows rootless Engine and reviewed-image build steps, and edits
the pinned worker image and local socket. The terminal `doxa setup` wizard
offers the same optional Docker configuration. Invalid mutable image tags and
rootful/remote sockets are rejected before saving. These checks do not connect
to the Engine; Start verifies rootless mode, cgroup controls and the actual
local image. Setup reports missing prerequisites even after preferences save.

Use **`/setup docker`** or **`doxa setup docker`** to go directly to this stage
without provider authentication checks or changing model/LORE defaults.

Set the printed immutable image ID in **Settings → Session → Docker worker image
digest**. Set a nondefault local Unix endpoint in **Settings → Paths → local
rootless Docker socket**. Save with Ctrl+S. In New Session, the **Docker image
Settings** action (Ctrl+O) opens the image row and Esc returns to the retained
model, isolation profile and first prompt. The UI checks only the image policy
and local socket; it neither installs an Engine nor builds or pulls an image.
Start performs the existing rootless/cgroup/image admission checks.

A missing socket means the local rootless Engine still needs explicit operator
provisioning or startup. A missing digest means the reviewed image build and
configuration remain incomplete. Follow [Docker’s rootless installation procedure](https://docs.docker.com/engine/security/rootless/)
for your host; never substitute the system rootful
socket or relax digest pinning to make a profile start.
