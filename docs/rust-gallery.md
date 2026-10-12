# Rust gallery

The beta.48 capture set targets the production Ratatui `App` fed deterministic example events through its `TestBackend`. After the final beta.48 UI and fleet changes land, the capture script rasterizes the styled cell buffer at **3068 × 1734**, with DejaVu Sans Mono, Noto Color Emoji for peer, tool and thinking markers, and Symbols Nerd Font Mono v3.5.1 for private-use icons. Nerd Fonts are enabled by default; saved or environment-controlled off settings remain supported. Each run gets isolated DOXA, LORE and home directories, inherited settings and credentials are scrubbed, and the clock display is frozen. The examples include a checked-in image, synthetic turns, memory, fleet status, project triage, context telemetry, subscription quota snapshots, and isolation status passed through the same policy validator used for host reports. They demonstrate rendering and review layout; they do not verify a live provider, container, or fleet run. This documentation-only change does not regenerate images; regenerate the beta.48 PNGs only from the final built example after the remaining UI and fleet changes land.

## Contents

- [Beta.48 terminal captures](#beta48-terminal-captures)
- [Reproduce the captures](#reproduce-the-captures)
- [Earlier live terminal captures](#earlier-live-terminal-captures)
- [Browser and Android gallery](remote-gallery.md)

## Beta.48 terminal captures

| Scene | Image | What the fixture exercises |
| --- | --- | --- |
| Workspace | [hero](../assets/shots/rust-2.0.0-beta.48-hero.png) | Tabs, project rail, Markdown answer, and version line |
| Project triage | [triage](../assets/shots/rust-2.0.0-beta.48-triage.png) | Verified-root hue and a pane badge pointing to a hidden tab that needs input |
| Pane rail | [pane-triage](../assets/shots/rust-2.0.0-beta.48-pane-triage.png) | One navigable row for the pane with three tabs; hidden-tab urgency remains visible |
| Project label | [project-label](../assets/shots/rust-2.0.0-beta.48-project-label.png) | An explicit label on a verified project heading, kept in fixture memory without writing owner config |
| Image preview | [image-preview](../assets/shots/rust-2.0.0-beta.48-image-preview.png) | An inline checked-in local Markdown image rendered inside a transcript paragraph using the halfblock fallback |
| Isolation | [isolation](../assets/shots/rust-2.0.0-beta.48-isolation.png) | The Docker policy chip and details from a synthetic rootless worker status |
| Fleet review | [fleet-review](../assets/shots/rust-2.0.0-beta.48-fleet-review.png) | A supervised plan with spending limits, independent reviewer, and message judge; launch is disabled |
| Dependency plan | [fleet-dependency](../assets/shots/rust-2.0.0-beta.48-fleet-dependency.png) | Frozen tasks, predecessor edge, and explicit human-release warning in a disabled plan |
| Dependency release | [fleet-release-review](../assets/shots/rust-2.0.0-beta.48-fleet-release-review.png) | Synthetic host checkpoint, receiver read-back and sender agreement in the real review modal; release is disabled |
| Code graph | [codegraph](../assets/shots/rust-2.0.0-beta.48-codegraph.png) | A synthetic, read-only module query in the production modal, with source hashes and an explicitly unknown conditional edge |
| Fleet status | [fleet-view](../assets/shots/rust-2.0.0-beta.48-fleet-view.png) | A synthetic worker waiting for its predecessor; no controller starts |
| Beliefs | [beliefs](../assets/shots/rust-2.0.0-beta.48-beliefs.png) | Belief selection and review actions; writes are disabled |
| Tool details | [tool-entries](../assets/shots/rust-2.0.0-beta.48-tool-entries.png) | Expanded normalized tool event with synthetic input and output |
| Memory | [memory-management](../assets/shots/rust-2.0.0-beta.48-memory-management.png) | Curated project memory controls; writes are disabled |
| Commands | [commands](../assets/shots/rust-2.0.0-beta.48-commands.png) | Slash-command completion |
| Help | [help](../assets/shots/rust-2.0.0-beta.48-help.png) | Built-in command help |
| Settings | [settings](../assets/shots/rust-2.0.0-beta.48-settings.png) | Actual click on Appearance and hover on Memory in the submenu title bar; changes remain unsaved |
| Jev Router settings | [settings-router](../assets/shots/rust-2.0.0-beta.48-settings-router.png) | Actual dropdown and text-editor staging of the Router default and private config path; readiness stays Not checked |
| Routing status | [routing-status](../assets/shots/rust-2.0.0-beta.48-routing-status.png) | Synthetic Auto decision with separate effective API worker, latency and Jev-only routing estimate |
| Context | [context](../assets/shots/rust-2.0.0-beta.48-context.png) | Reported 42,000 of 200,000 tokens, with exact synthetic system, tool and conversation components |
| Subscription quota | [usage-quota](../assets/shots/rust-2.0.0-beta.48-usage-quota.png) | Synthetic Claude session and weekly quotas with reset times and preserved extra provider windows; no billing request |
| Queued prompts | [queued-prompts](../assets/shots/rust-2.0.0-beta.48-queued-prompts.png) | Markdown and user stripe on a queued prompt while a turn is running |
| Peer messages | [peer-messages](../assets/shots/rust-2.0.0-beta.48-peer-messages.png) | Default-collapsed received, sent and queued sections with robot direction icons and full session addresses |
| Peer details | [peer-expanded](../assets/shots/rust-2.0.0-beta.48-peer-expanded.png) | Actual clicks expand the received and sent message bodies with their peer stripe |
| Thinking | [reasoning](../assets/shots/rust-2.0.0-beta.48-reasoning.png) | Collapsed progress section with brain marker and a synthetic token count |
| MCP settings | [settings-mcp](../assets/shots/rust-2.0.0-beta.48-settings-mcp.png) | API worker MCP controls and all four masked search-key fields; no connection or tool call |
| GLM reporting | [glm-account](../assets/shots/rust-2.0.0-beta.48-glm-account.png) | Owner-declared API resource bundle; cash balance and bundle remainder stay unknown |
| Docker setup | [setup-docker](../assets/shots/rust-2.0.0-beta.48-setup-docker.png) | Direct /setup docker opens the production prerequisites and image/socket editor controls without provider probes |

The `image-preview` scene exercises the portable halfblock path. A terminal with Kitty or Sixel graphics may use another backend; this capture is not evidence of either terminal protocol. Fleet and isolation values are synthetic and labeled as fixtures in the UI. The dependency-release fixture cannot release a worker. Project-label state is held in memory; the live command writes owner config only after verifying the root. Paths under `/demo` are example data.

## Reproduce the captures

From the repository root, install system Python 3.11+ with Pillow, DejaVu Sans Mono regular/bold and Noto Color Emoji. Obtain the official Symbols Nerd Font Mono regular TTF from the pinned v3.5.1 release for the private-use icons. Build the gallery example from the same release as the frontend, then rasterize its cell buffers:

```sh
export TMPDIR=/path/on/real/disk
cargo build --locked -j 2 -p doxa-tui --example gallery
python3 scripts/render_rust_gallery.py --binary target/debug/examples/gallery \
  --icon-font /path/to/SymbolsNerdFontMono-Regular.ttf
```

The script requires `TMPDIR` on real disk, checks each scene's binary version against `rust/doxa-tui/Cargo.toml` and the visible hero version, writes all **28** default `rust-2.0.0-beta.48-*.png` images under `assets/shots`, and verifies every image is 3068 × 1734. It also checks visible feature content in the Settings, context, quota, routing, Docker, GLM and message scenes, including default-hidden peer bodies. Missing Nerd Font glyph support is an error instead of a replacement-glyph capture. Pass scene names after the options to render a subset. The Rust example uses the production `App` reducer and widgets; the Python step only paints Ratatui cells. The captures are deterministic fixture views, not screenshots of a terminal emulator or authenticated account.

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
