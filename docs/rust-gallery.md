# Live Rust terminal gallery

The README gallery records the running Rust app in a real GTK3/VTE terminal on
an isolated Xvfb display. FFmpeg captures the terminal framebuffer directly.
These images use the production frontend and native daemon with an authenticated
Codex session; they do not use `TestBackend`, `--demo`, a fixture engine, or
injected daemon events. The PNGs are unedited.

## Capture provenance

| Item | Recorded value |
| --- | --- |
| Date | 2026-09-28 |
| Release | `2.0.0-alpha.37` |
| Frontend and daemon source | `1c563f644a13b5854c01a00211158b0f7b19c680` |
| Build profile | Native installer binaries, Cargo release profile |
| LORE | Native `0.62.0`, pinned to `d8abc3e104aacda221b5df7baace13c0a1a69b53` |
| Provider | Authenticated Codex CLI `0.156.1`, through its app server |
| Model and effort | `gpt-6-sol`; `low` selected and verified before the provider turns |
| Terminal | GTK3/VTE, DejaVu Sans Mono 30, 127 columns × 36 rows |
| Framebuffer | 3068 × 1734, captured with FFmpeg `x11grab` |
| State | Fresh isolated `DOXA_HOME`, `DOXA_RUNTIME_DIR`, `LORE_ROOT` and `LORE_PROJECTS_DIR`; `DOXA_LORE=0` |

The small **Harbour notes** repository was written specifically as safe example
input. It contains a README and a two-line shell greeting script. Codex actually
read the files with separate commands and produced the replies shown. A second
turn requested escalation for `sh greeting.sh Ada`. The real app-server permission
callback was reviewed and approved with plain `a`; its actual output was
`Welcome aboard, Ada!`. No files were edited by the provider.

All six frames use the same live session, model and verified effort selection.
The pending-permission image was captured before approval; the hero and other
menus were captured after the greeting command completed.
The settings submenu shows startup configuration defaults (Claude and unset
effort) alongside the inherited session model; the live chips show Codex and
verified low effort. Opening settings does not change the active provider.
Displayed context and timing values are the app's reported values, not invented
usage data. Memory is disabled and the isolated store is empty.

## Reproduce a live capture

Use a fresh, private directory on disk, a small repository containing only non-private
example files, and a real provider login. The script leaves `HOME` intact so the
provider CLI can authenticate; DOXA and LORE state are isolated. It creates its
own display and never captures the user's desktop.

Build the frontend and native daemon from the intended release first. Supply
absolute paths for the native frontend, daemon, LORE carrier and provider CLI:

```sh
/usr/bin/python3 scripts/live_rust_gallery.py run \
  --binary /absolute/path/to/doxa-rs \
  --daemon /absolute/path/to/doxa-daemon-rs \
  --lore /absolute/path/to/lore-rs \
  --repo /absolute/path/to/isolated-example-repo \
  --state /absolute/private/path/gallery-state \
  --control /absolute/private/path/gallery.sock \
  --engine codex --model gpt-6-sol \
  --provider-bin /absolute/path/to/codex
```

Keep the control-socket and runtime paths short enough for Unix sockets.
The native launcher checks daemon and carrier ownership and permissions.
The script requires system Python with GTK3/VTE, Xvfb, FFmpeg and the font above.
It enables the terminal's normal color palette even if the invoking shell sets
`NO_COLOR`.

In another terminal, send normal terminal input through the owned VTE PTY:

```sh
/usr/bin/python3 scripts/live_rust_gallery.py control \
  --control /absolute/private/path/gallery.sock \
  --action '{"kind":"input","text":"Read README.md and greeting.sh using separate file read calls. Give two short bullets describing the greeting. Do not edit files.\r"}'
```

Close the initial setup submenu with a separate Esc input. Select `/effort low`
and wait for the app to report verification before sending a provider prompt.
Wait for actual provider completion, then use the app's normal keyboard or mouse
controls to expand individual calls. For a permission frame, ask the provider to
run the harmless greeting command with `sandbox_permissions=require_escalated`
and capture while its genuine approval is
pending. Review that request and explicitly approve or deny it in the app.
The script does not answer requests automatically.

Capture only after the intended UI state is visible:

```sh
/usr/bin/python3 scripts/live_rust_gallery.py control \
  --control /absolute/private/path/gallery.sock \
  --action '{"kind":"capture","name":"tool-entries"}'
```

Images are written under `gallery-state/shots`. `status` reports the real terminal
cell size and row/column count. Inspect every image and verify its dimensions
before copying it to `assets/shots`.

When finished, stop the exact capture session shown by the app using its isolated
runtime, then close the capture display:

```sh
DOXA_HOME=/absolute/private/path/gallery-state/home \
DOXA_RUNTIME_DIR=/absolute/private/path/gallery-state/runtime \
  /absolute/path/to/doxa-rs stop FULL_CAPTURE_SESSION_ID

/usr/bin/python3 scripts/live_rust_gallery.py control \
  --control /absolute/private/path/gallery.sock \
  --action '{"kind":"quit"}'
```

The display, window and control socket are cleaned up on exit, including GUI
setup failures. The provider daemon has its own lifecycle, so stopping its exact
session is a separate step. Capture state and transcripts stay in the private
state directory; publish only reviewed PNGs.

## Developer renderer fixtures

`scripts/rust_gallery.py` remains a deterministic renderer utility for development.
Its default output is the ignored `target/gallery-fixtures` directory. Those
images are test fixtures and are not used by the published README gallery.
