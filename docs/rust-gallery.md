# Live Rust terminal gallery

The README gallery records the running Rust app in a real GTK3/VTE terminal on
an isolated Xvfb display. FFmpeg captures the terminal framebuffer directly.
These images use the production frontend and native daemon with an authenticated
Claude session; they do not use `TestBackend`, `--demo`, a fixture engine, or
injected daemon events. The PNGs are unedited.

## Capture provenance

| Item | Recorded value |
| --- | --- |
| Date | 2026-09-28 |
| Release | `2.0.0-alpha.36` |
| Frontend and daemon source | `85b011e` |
| Build profile | Production binaries, Cargo debug profile |
| Provider | Authenticated Claude CLI `2.1.283`, through the Claude SDK sidecar |
| Model | CLI default for the two provider turns; `claude-opus-5-5` explicitly selected afterward |
| Terminal | GTK3/VTE, DejaVu Sans Mono 30, 127 columns × 36 rows |
| Framebuffer | 3068 × 1734, captured with FFmpeg `x11grab` |
| State | Fresh isolated `DOXA_HOME`, `DOXA_RUNTIME_DIR` and `LORE_ROOT`; `DOXA_LORE=0` |

The small **Harbour notes** repository was written specifically as safe example
input. It contains a README, a three-item checklist, and a two-line Python greeting
function. Claude actually read the files and produced the replies shown. The
separate Bash turn ran `python3 -c "from greeting import greet; print(greet('Ada'))"`
after a real permission callback, approved with plain `a`; its actual output was
`Welcome aboard, Ada!`. No files were edited by the provider.

The pending-permission image was captured before explicit model selection.
The hero, individual-tool, help, completion and settings images were captured
after selecting the actual catalog entry. Live effort changes were unavailable
for this session, so the effort chip remains unknown and settings show it unset.
Displayed context and timing values are the app's reported values, not invented
usage data. Memory is disabled and the isolated store is empty.

## Reproduce a live capture

Use a fresh, private directory on disk, a small repository containing only non-private
example files, and a real provider login. The script leaves `HOME` intact so the
provider CLI can authenticate; DOXA and LORE state are isolated. It creates its
own display and never captures the user's desktop.

Build the frontend and native daemon from the intended release first. Supply
absolute paths for your binaries, SDK Python environment and sidecar:

```sh
/usr/bin/python3 scripts/live_rust_gallery.py run \
  --binary /absolute/path/to/doxa-rs \
  --daemon /absolute/path/to/doxa-daemon \
  --python /absolute/path/to/sdk-environment/bin/python \
  --sidecar /absolute/path/to/doxa/rust/doxa-claude/claude_sidecar.py \
  --repo /absolute/path/to/isolated-example-repo \
  --state /absolute/private/path/gallery-state \
  --control /absolute/private/path/gallery.sock \
  --engine claude
```

Keep the control-socket and runtime paths short enough for Unix sockets.
The native Claude launcher requires a non-writable-by-others daemon and sidecar.
The script requires system Python with GTK3/VTE, Xvfb, FFmpeg and the font above.
It enables the terminal's normal color palette even if the invoking shell sets
`NO_COLOR`.

In another terminal, send normal terminal input through the owned VTE PTY:

```sh
/usr/bin/python3 scripts/live_rust_gallery.py control \
  --control /absolute/private/path/gallery.sock \
  --action '{"kind":"input","text":"Read README.md and greeting.py with separate Read calls. Give two short bullets. Do not edit files.\r"}'
```

Wait for actual provider completion, then use the app's normal keyboard or mouse
controls to expand individual calls. For a permission frame, ask the provider to
run the harmless greeting command and capture while its genuine approval is
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
