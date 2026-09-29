# Live Rust terminal gallery

The README gallery records the running Rust app in a real GTK3/VTE terminal on
an isolated Xvfb display. FFmpeg captures the terminal framebuffer directly.
These images use the production frontend and native daemon with an authenticated
Codex session; they do not use `TestBackend`, `--demo`, a fixture engine, or
injected daemon events. The PNGs are unedited.

The gallery combines seven alpha.49 provider frames with two alpha.50 LORE
browser frames. Both menu images show production UI querying an isolated native
LORE store populated through `lore-rs` with shareable example entries.

## Capture provenance

| Item | Recorded value |
| --- | --- |
| Date | 2026-09-29 |
| Provider frames | `2.0.0-alpha.49` |
| Frontend and daemon source | `8b7804e446887368f3b29ca172bf2f60f44dc77b` (alpha.49 version commit over alpha.48 main) |
| Build profile | Cargo release profile, actual `doxa-rs` and `doxa-daemon` binaries |
| LORE | Native `0.62.4`, pinned to `8250d7b037524f3e1e3f17956ba934162e8ba8cd` |
| Provider | Authenticated Codex CLI `0.156.1`, through DOXA's installed protected app server |
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

Six frames use the same live session, model and verified low effort. The
pending-permission image was captured before approval; the hero and other menus
were captured after the greeting command completed. The seventh, `rust-welcome.png`,
uses a second fresh session in the same example repository to show the actual
block Greek ΔΟΞΑ startup banner.
The settings submenu shows startup configuration defaults (Claude and unset
effort) alongside the inherited session model; the live chips show Codex and
verified low effort. Opening settings does not change the active provider.
Displayed context and timing values are the app's reported values, not invented
usage data. In these seven frames memory is disabled and both isolated stores
are empty. The approval
frame contains an ephemeral command request and a private path under the example
state directory; it contains no account credentials.

The curated-memory and beliefs frames were captured on 2026-09-30 from the
alpha.50 version commit `aa62023` in a third fresh Codex session. This session
used `--with-lore`, which sets `DOXA_LORE=1` while keeping `LORE_ROOT` and
`LORE_PROJECTS_DIR` inside its private gallery state. The native `lore-rs`
carrier added 17 short user/project facts and 27 project beliefs (including
one entry of each kind from the initial seeding probe). The menu data is
synthetic, but the storage, carrier queries, terminal interaction and captured
pixels are real. These two frames also measure 3068 × 1734. Both show a selected
row and the right scrollbar; neither exposes a personal memory store.

## Reproduce a live capture

Use a fresh, private directory on real disk, a committed repository containing
only non-private example files, and a real provider login. Set `TMPDIR` to that
disk directory; never put capture state or build caches in `/tmp`. The script
leaves `HOME` intact so the provider CLI can authenticate; DOXA and LORE state
are isolated. It creates its own display and never captures the user's desktop.

Build the frontend and native daemon from the intended release first. Supply
absolute paths for the native frontend, daemon, LORE carrier and DOXA's installed
protected Codex launcher. A stock Codex CLI is insufficient for protected turns:

```sh
/usr/bin/python3 scripts/live_rust_gallery.py run \
  --binary /absolute/path/to/doxa-rs \
  --daemon /absolute/path/to/doxa-daemon-rs \
  --lore /absolute/path/to/lore-rs \
  --repo /absolute/path/to/isolated-example-repo \
  --state /absolute/private/path/gallery-state \
  --control /absolute/private/path/gallery.sock \
  --engine codex --model gpt-6-sol \
  --provider-bin /home/USER/.local/share/doxa/providers/codex-current/codex
```

Keep the control-socket and runtime paths short enough for Unix sockets.
The native launcher checks daemon and carrier ownership and permissions. A
direct Cargo build names the daemon executable `doxa-daemon`; an installer copy
names it `doxa-daemon-rs`.
The script requires system Python with GTK3/VTE, Xvfb, FFmpeg and the font above.
It enables the terminal's normal color palette even if the invoking shell sets
`NO_COLOR`.

For the memory and belief frames, seed only the isolated `LORE_ROOT` and
`LORE_PROJECTS_DIR` through the native `lore-rs memory add` and `lore-rs belief
add` commands, then add `--with-lore` to `live_rust_gallery.py run`. The app
loads those entries through its ordinary LORE carrier. Click the memory or
Beliefs chip, use Down to select a row, and capture with the names
`rust-curated-memory` and `rust-beliefs`.

In another terminal, send normal terminal input through the owned VTE PTY:

```sh
/usr/bin/python3 scripts/live_rust_gallery.py control \
  --control /absolute/private/path/gallery.sock \
  --action '{"kind":"input","text":"Read README.md and greeting.sh using separate file read calls. Give two short bullets describing the greeting. Do not edit files.\r"}'
```

Close the initial setup submenu with a separate Esc input. Select `/effort low`
and wait for the app to report verification before sending a provider prompt.
Ask Codex to read `README.md` and `greeting.sh` separately and summarize both.
Wait for the real turn to finish, then use normal keyboard or mouse input to
expand one of its two tool calls. For a permission frame, ask the provider to
run the harmless greeting command with `sandbox_permissions=require_escalated`
and capture while its genuine approval is pending. Review that request and
explicitly approve or deny it in the app. The script does not answer requests
automatically. Use `/s`, `/help`, and `/settings` for the remaining menus.

Capture only after the intended UI state is visible:

```sh
/usr/bin/python3 scripts/live_rust_gallery.py control \
  --control /absolute/private/path/gallery.sock \
  --action '{"kind":"capture","name":"tool-entries"}'
```

Images are written under `gallery-state/shots`. `status` reports the real terminal
cell size and row/column count; `text` reads the VTE terminal text for state
checks. Capture a second fresh session for the welcome frame. Inspect every
image and verify its dimensions before copying it to `assets/shots`.

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
