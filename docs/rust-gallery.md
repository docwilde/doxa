# Rust gallery

The beta.49 gallery contains **28 actual terminal screenshots**, captured from the production Ratatui `App` running in a GTK/VTE terminal on a private Xvfb display. `ffmpeg x11grab` captures the display framebuffer at **3068 × 1734**. The gallery example feeds illustrative turns, tool calls, memory, fleets and telemetry into the native UI; it never starts a provider or contacts owner sessions or memory. Every scene has isolated state. DejaVu Sans Mono and a private Symbols Nerd Font provide the terminal glyphs. The capture metadata records the terminal grid and SHA-256 of each original PNG.

The previous beta.48 images were rasterized from styled `TestBackend` cells. Those historical files remain in `assets/shots`; the current README and interactive gallery use the real terminal captures.

## Contents

- [Beta.49 terminal captures](#beta49-terminal-captures)
- [Reproduce the captures](#reproduce-the-captures)
- [Earlier live terminal captures](#earlier-live-terminal-captures)
- [Browser and Android gallery](remote-gallery.md)

## Beta.49 terminal captures

| Scene | Image | What the fixture exercises |
| --- | --- | --- |
| Workspace | [hero](../assets/shots/rust-2.0.0-beta.49-hero.png) | Tabs, project rail, Markdown answer, and version line |
| Project triage | [triage](../assets/shots/rust-2.0.0-beta.49-triage.png) | Verified-root hue and a pane badge pointing to a hidden tab that needs input |
| Pane rail | [pane-triage](../assets/shots/rust-2.0.0-beta.49-pane-triage.png) | One navigable row for the pane with three tabs; hidden-tab urgency remains visible |
| Project label | [project-label](../assets/shots/rust-2.0.0-beta.49-project-label.png) | An explicit label on a verified project heading, kept in fixture memory without writing owner config |
| Image preview | [image-preview](../assets/shots/rust-2.0.0-beta.49-image-preview.png) | An inline checked-in local Markdown image rendered inside a transcript paragraph using the halfblock fallback |
| Isolation | [isolation](../assets/shots/rust-2.0.0-beta.49-isolation.png) | The Docker policy chip and details from a synthetic rootless worker status |
| Fleet review | [fleet-review](../assets/shots/rust-2.0.0-beta.49-fleet-review.png) | A supervised plan with spending limits, independent reviewer, and message judge; launch is disabled |
| Dependency plan | [fleet-dependency](../assets/shots/rust-2.0.0-beta.49-fleet-dependency.png) | Frozen tasks, predecessor edge, and explicit human-release warning in a disabled plan |
| Dependency release | [fleet-release-review](../assets/shots/rust-2.0.0-beta.49-fleet-release-review.png) | Synthetic host checkpoint, receiver read-back and sender agreement in the real review modal; release is disabled |
| Code graph | [codegraph](../assets/shots/rust-2.0.0-beta.49-codegraph.png) | A synthetic, read-only module query in the production modal, with source hashes and an explicitly unknown conditional edge |
| Fleet status | [fleet-view](../assets/shots/rust-2.0.0-beta.49-fleet-view.png) | A synthetic worker waiting for its predecessor; no controller starts |
| Beliefs | [beliefs](../assets/shots/rust-2.0.0-beta.49-beliefs.png) | Belief selection and review actions; writes are disabled |
| Tool details | [tool-entries](../assets/shots/rust-2.0.0-beta.49-tool-entries.png) | Expanded normalized tool event with synthetic input and output |
| Memory | [memory-management](../assets/shots/rust-2.0.0-beta.49-memory-management.png) | Curated project memory controls; writes are disabled |
| Commands | [commands](../assets/shots/rust-2.0.0-beta.49-commands.png) | Slash-command completion |
| Help | [help](../assets/shots/rust-2.0.0-beta.49-help.png) | Built-in command help |
| Settings | [settings](../assets/shots/rust-2.0.0-beta.49-settings.png) | Actual click on Appearance and hover on Memory in the submenu title bar; changes remain unsaved |
| Jev Router settings | [settings-router](../assets/shots/rust-2.0.0-beta.49-settings-router.png) | Actual dropdown and text-editor staging of the Router default and private config path; readiness stays Not checked |
| Routing status | [routing-status](../assets/shots/rust-2.0.0-beta.49-routing-status.png) | Synthetic Auto decision with separate effective API worker, latency and Jev-only routing estimate |
| Context | [context](../assets/shots/rust-2.0.0-beta.49-context.png) | Reported 42,000 of 200,000 tokens, with exact synthetic system, tool and conversation components |
| Subscription quota | [usage-quota](../assets/shots/rust-2.0.0-beta.49-usage-quota.png) | Synthetic Claude session and weekly quotas with reset times and preserved extra provider windows; no billing request |
| Queued prompts | [queued-prompts](../assets/shots/rust-2.0.0-beta.49-queued-prompts.png) | Markdown and user stripe on a queued prompt while a turn is running |
| Peer messages | [peer-messages](../assets/shots/rust-2.0.0-beta.49-peer-messages.png) | Default-collapsed received, sent and queued sections with robot direction icons and full session addresses |
| Peer details | [peer-expanded](../assets/shots/rust-2.0.0-beta.49-peer-expanded.png) | Actual clicks expand the received and sent message bodies with their peer stripe |
| Thinking | [reasoning](../assets/shots/rust-2.0.0-beta.49-reasoning.png) | Collapsed progress section with brain marker and a synthetic token count |
| MCP settings | [settings-mcp](../assets/shots/rust-2.0.0-beta.49-settings-mcp.png) | API worker MCP controls and all four masked search-key fields; no connection or tool call |
| GLM reporting | [glm-account](../assets/shots/rust-2.0.0-beta.49-glm-account.png) | Owner-declared API resource bundle; cash balance and bundle remainder stay unknown |
| Docker setup | [setup-docker](../assets/shots/rust-2.0.0-beta.49-setup-docker.png) | Direct /setup docker opens the production prerequisites and image/socket editor controls without provider probes |

The `image-preview` scene exercises the portable halfblock path. A terminal with Kitty or Sixel graphics may use another backend; this capture is not evidence of either terminal protocol. Fleet and isolation values are synthetic and labeled as fixtures in the UI. The dependency-release fixture cannot release a worker. Project-label state is held in memory; the live command writes owner config only after verifying the root. Paths under `/demo` are example data.

## Reproduce the captures

From the repository root, install system Python 3.11+ with GTK3/VTE bindings, Xvfb, ffmpeg and DejaVu Sans Mono. A Symbols Nerd Font Mono TTF can be registered privately for the process. Build the gallery example from the frontend release:

```sh
export TMPDIR=/path/on/real/disk
cargo build --locked -j 2 -p doxa-tui --example gallery
mkdir -m 700 /path/on/real/disk/capture-state
/usr/bin/python3 scripts/capture_native_rust_gallery.py \
  --binary target/debug/examples/gallery \
  --state /path/on/real/disk/capture-state \
  --icon-font /path/to/SymbolsNerdFontMono-Regular.ttf
```

The script checks the binary version, waits for native terminal content to stabilize, captures all manifest scenes and verifies PNG dimensions. Pass scene IDs after the options for a subset. `assets/shots/rust-2.0.0-beta.49-capture.json` records original hashes and provenance. The JSON cell-buffer mode and `scripts/render_rust_gallery.py` remain developer debugging tools; they are not the current terminal-capture pipeline.

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

They were captured in a real GTK3/VTE terminal on private Xvfb with FFmpeg. That run used authenticated Codex CLI 0.156.1 through DOXA's protected app server and isolated LORE 0.62.11 data. The provider read only synthetic Harbour notes files. These files document the older live build; they do not show current beta.48 features. The original recorder remains at `scripts/live_rust_gallery.py` for an operator who explicitly chooses an authenticated capture.
