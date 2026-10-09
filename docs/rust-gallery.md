# Rust gallery

The current terminal images show the **DOXA 2.0.0-beta.17** production Ratatui `App` fed deterministic example events through its `TestBackend`. A fixed DejaVu Sans Mono font rasterizes the styled cell buffer at **3068 × 1734**. The capture script gives each run isolated DOXA, LORE and home directories. No provider, daemon, Docker Engine, network request, or private user session is opened. The examples include a checked-in image, synthetic turns, memory, fleet status, project triage, and isolation status passed through the same policy validator used for host reports. They demonstrate rendering and review layout; they do not verify a live provider, container, or fleet run.

## Contents

- [Current terminal captures](#current-terminal-captures)
- [Reproduce the captures](#reproduce-the-captures)
- [Earlier live terminal captures](#earlier-live-terminal-captures)
- [Remote browser captures](#remote-browser-captures)

## Current terminal captures

| Scene | Image | What the fixture exercises |
| --- | --- | --- |
| Workspace | [hero](../assets/shots/rust-2.0.0-beta.17-hero.png) | Tabs, project rail, Markdown answer, and version line |
| Project triage | [triage](../assets/shots/rust-2.0.0-beta.17-triage.png) | Verified-root hue and a pane badge pointing to a hidden tab that needs input |
| Pane rail | [pane-triage](../assets/shots/rust-2.0.0-beta.17-pane-triage.png) | One navigable row for the pane with three tabs; hidden-tab urgency remains visible |
| Image preview | [image-preview](../assets/shots/rust-2.0.0-beta.17-image-preview.png) | A checked-in local image rendered inside the transcript using the halfblock fallback |
| Isolation | [isolation](../assets/shots/rust-2.0.0-beta.17-isolation.png) | The Docker policy chip and details from a synthetic rootless worker status |
| Fleet review | [fleet-review](../assets/shots/rust-2.0.0-beta.17-fleet-review.png) | A supervised plan with spending limits, independent reviewer, and message judge; launch is disabled |
| Dependency plan | [fleet-dependency](../assets/shots/rust-2.0.0-beta.17-fleet-dependency.png) | Frozen tasks, predecessor edge, and explicit human-release warning in a disabled plan |
| Dependency release | [fleet-release-review](../assets/shots/rust-2.0.0-beta.17-fleet-release-review.png) | Synthetic host checkpoint and accepted handoff in the real review modal; release is disabled |
| Code graph | [codegraph](../assets/shots/rust-2.0.0-beta.17-codegraph.png) | A synthetic, read-only module query in the production modal, with source hashes and an explicitly unknown conditional edge |
| Fleet status | [fleet-view](../assets/shots/rust-2.0.0-beta.17-fleet-view.png) | A synthetic worker waiting for its predecessor; no controller starts |
| Beliefs | [beliefs](../assets/shots/rust-2.0.0-beta.17-beliefs.png) | Belief selection and review actions; writes are disabled |
| Tool details | [tool-entries](../assets/shots/rust-2.0.0-beta.17-tool-entries.png) | Expanded normalized tool event with synthetic input and output |
| Memory | [memory-management](../assets/shots/rust-2.0.0-beta.17-memory-management.png) | Curated project memory controls; writes are disabled |
| Commands | [commands](../assets/shots/rust-2.0.0-beta.17-commands.png) | Slash-command completion |
| Help | [help](../assets/shots/rust-2.0.0-beta.17-help.png) | Built-in command help |

The `image-preview` scene exercises the portable halfblock path. A terminal with Kitty or Sixel graphics may use another backend; this capture is not evidence of either terminal protocol. Fleet and isolation values are synthetic and labeled as fixtures in the UI. The dependency-release fixture cannot release a worker. Paths under `/demo` are example data.

## Reproduce the captures

From the repository root, install system Python 3.11+ with Pillow and the DejaVu Sans Mono font. Build the gallery example from the same release as the frontend, then rasterize its cell buffers:

```sh
export TMPDIR=/path/on/real/disk
cargo build --locked -j 2 -p doxa-tui --example gallery
python3 scripts/render_rust_gallery.py --binary target/debug/examples/gallery
```

The script requires `TMPDIR` on real disk, checks that the hero frame visibly reports the version from `rust/doxa-tui/Cargo.toml`, writes `rust-2.0.0-beta.17-*.png` under `assets/shots`, and verifies every image is 3068 × 1734. Pass scene names after the options to render a subset. The Rust example uses the production `App` reducer and widgets; the Python step only paints Ratatui cells. The captures are deterministic fixture views, not screenshots of a terminal emulator or authenticated account.

## Earlier live terminal captures

These unprefixed files remain historical **2.0.0-alpha.68** live captures from 2026-10-03:

| File | Earlier live state |
| --- | --- |
| `rust-hero.png` | Completed Codex turn with folded tool calls |
| `rust-welcome.png` | Opening screen |
| `rust-sessions.png` | Grouped live and past sessions |
| `rust-curated-memory.png` | LORE facts |
| `rust-beliefs.png` | LORE belief review |
| `rust-tool-entries.png` | Expanded provider tool call |
| `rust-commands.png` | Slash completion |
| `rust-help.png` | Command help |
| `rust-settings.png` | Key settings |

They were captured in a real GTK3/VTE terminal on private Xvfb with FFmpeg. That run used authenticated Codex CLI 0.156.1 through DOXA's protected app server and isolated LORE 0.62.11 data. The provider read only synthetic Harbour notes files. These files document the older live build; they do not show beta.17 features. The original recorder remains at `scripts/live_rust_gallery.py` for an operator who explicitly chooses an authenticated capture.

## Remote browser captures

The existing `rust-remote-browser-conversation.png` and `rust-remote-browser-review.png` show shipped `rust/doxa-remote/assets` in Chromium with isolated API fixtures. The browser UI did not change in this batch, so these two 3068 × 1734 captures were left intact. They do not verify an authenticated hub or remote transport.

Reproduce them with Google Chrome and Node.js 22+:

```sh
export TMPDIR=/path/on/real/disk
python3 scripts/capture_remote_gallery.py
```

That script serves local assets on `127.0.0.1`, keeps temporary Chrome profiles under `~/.cache/doxa`, and writes the two browser PNGs to `assets/shots`. See the [remote hub plan](plans/remote-hub.md) for transport behavior.
