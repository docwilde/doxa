# Reviewed Docker session image

The host daemon owns transcripts, LORE, approvals, peers and the Docker API.
The image contains the isolation worker and explicitly selected provider
binaries. Each session receives an independent Git checkout and private home
and cache. The Docker socket and host home are never mounted into a worker.

Build the worker with `cargo build --locked -p doxa-isolation`. Use a local
rootless Docker Engine and a reviewed base image pinned as `NAME@sha256:DIGEST`:

| Base family | glibc | Protected Codex provider |
| --- | --- | --- |
| `ubuntu-noble` (Ubuntu 24.04) | 2.39 | supported; **required** when `--codex-provider` is used |
| `debian-bookworm` (Debian 12) | 2.36 | refused; valid only for images without Codex |

The protected `codex-app-server` and its Code Mode payload need `GLIBC_2.39`, so
the helper refuses `--codex-provider` with `--base-family debian-bookworm`
before it contacts Docker. It also reads the verified provider binaries and
refuses any base older than the newest `GLIBC_x.y` version they need. The pinned
package versions are verified for `amd64` and `arm64`; the Dockerfile rejects
other base architectures. Select the matching family; it checks the image's OS
identity and rejects mismatches:

Rebuild the image when DOXA's isolation worker changes. An older pinned image
cannot answer the current worker's cgroup enforcement probe, so session
admission fails closed until its digest is updated in owner configuration.

```sh
export TMPDIR="$HOME/ssd-cache/tmp"
python3 scripts/build-session-image.py \
  --docker-host "unix:///run/user/$(id -u)/docker.sock" \
  --base-image YOUR_REVIEWED_UBUNTU_24_04_IMAGE_AT_SHA256 \
  --base-family ubuntu-noble \
  --worker target/debug/doxa-isolation-worker \
  --claude-bin "$HOME/.local/bin/claude" \
  --codex-provider "$HOME/.local/share/doxa/providers/codex-current"
```

For a Bookworm image, pass `--base-family debian-bookworm` with a Debian 12
base and omit `--codex-provider`.

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
to `last-build.json` in its private build cache, outside the checkout:
`$XDG_CACHE_HOME/doxa/session-image/` (or `~/.cache/doxa/session-image/`).
Pass `--cache DIR` to choose another directory. Set `DOXA_DOCKER_IMAGE` to that
`sha256:…` ID, or to a published `NAME@sha256:DIGEST`. Use
`DOXA_DOCKER_HOST` for the same rootless Engine. A tag alone is insufficient.

`apt/debian-bookworm.sources` and `apt/ubuntu-noble.sources` pin their archive,
updates, and security suites to snapshot `20260901T000000Z`. Their matching
`.packages` files pin direct package versions. A build record captures the
selected family, snapshot, and hashes of the source and package manifests.
When updating packages, review a new snapshot date and package versions in the
same change. The digest-pinned base supplies the initial OS package state; apt
dependencies are resolved only from the fixed snapshot.

Both snapshots are fetched over verified HTTPS; `snapshot.ubuntu.com` redirects
plain HTTP to HTTPS. The base images have no CA store, so apt verifies TLS
against `apt/snapshot-ca.pem` while it installs the pinned `ca-certificates`
package. That file holds only ISRG Root X1 and X2, copied from the Noble
snapshot's `ca-certificates 20260601~24.04.1` package; the helper refuses a
bundle whose SHA-256 differs from its reviewed value. apt still verifies the
snapshots' signed `InRelease` files and package hashes. If a snapshot host
changes certificate authority, review and update the bundle and its hash in
the same change.

The base must supply the toolchain required by the repository being edited.
The Dockerfile adds exact versions of Git, Bash, Python, ripgrep (`rg`) and
CA certificates; it does not select a Rust, Node or other project toolchain.
Rebuild after a provider upgrade: the helper rechecks the provider's glibc
requirement against the selected base.

The original `official_cli` receipt path is not relocated into the image.
The protected launcher supports DOXA's `app-server` and Code Mode paths;
official CLI login/help remains on the host. Provider credentials copied to
a session-private home are visible to tools in that session.

Docker with open egress permits worker network access. Docker without
networking uses a session-bound proxy for the selected Claude/Codex provider's
fixed HTTPS hosts. General web access remains disabled. Rebuild the worker after
upgrading DOXA. See [provider connectivity](../../docs/session-isolation.md#provider-connectivity)
for the exact list and limits. CPU, memory and PID limits are enforced separately;
disk use is monitored and is not a filesystem quota. CLI credentials remain
visible to tools inside that session.

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
