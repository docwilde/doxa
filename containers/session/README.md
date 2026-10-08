# Reviewed Docker session image

The host daemon owns transcripts, LORE, approvals, peers and the Docker API.
The image contains the isolation worker and explicitly selected provider
binaries. Each session receives an independent Git checkout and private home
and cache. The Docker socket and host home are never mounted into a worker.

Build the worker with `cargo build --locked -p doxa-isolation`. Use a local
rootless Docker Engine and a reviewed Debian/Ubuntu-based base image pinned
as `NAME@sha256:DIGEST`:

```sh
export TMPDIR="$HOME/ssd-cache/tmp"
python3 scripts/build-session-image.py \
  --docker-host "unix:///run/user/$(id -u)/docker.sock" \
  --base-image YOUR_REVIEWED_BASE_IMAGE_AT_SHA256 \
  --worker target/debug/doxa-isolation-worker \
  --claude-bin "$HOME/.local/bin/claude" \
  --codex-provider "$HOME/.local/share/doxa/providers/codex-current"
```

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
