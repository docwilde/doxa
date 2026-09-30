# Live Rust terminal gallery

The README images were captured on 2026-09-30 from the running **DOXA
2.0.0-alpha.58** frontend and daemon. A private Xvfb display hosts a real
GTK3/VTE terminal; FFmpeg records its framebuffer. The gallery uses an
authenticated Codex CLI 0.156.1, the protected DOXA app server, and native
LORE 0.62.8. No `TestBackend`, demo engine, fixture events, or composited UI
mockups appear in these images.

| Image | Live state |
| --- | --- |
| `rust-hero.png` | Completed Codex turn with two file reads and folded tool calls |
| `rust-welcome.png` | Fresh session and block Greek ΔΟΞΑ banner |
| `rust-sessions.png` | Three real tabs, session rail, and a tab overflow indicator after a mouse-wheel switch |
| `rust-curated-memory.png` | Native LORE facts in a selectable, scrollable table |
| `rust-beliefs.png` | Native LORE beliefs with a selected review row |
| `rust-tool-entries.png` | One actual provider tool call expanded inside the turn |
| `rust-commands.png` | Slash completion above the prompt |
| `rust-help.png` | Local command help |
| `rust-settings.png` | Editable Keys settings category |

All nine PNGs measure **3068 × 1734** and use DejaVu Sans Mono 30. The
captured repository, **Harbour notes**, contains only a README and a greeting
script made for this gallery. The user and project memory and beliefs were
seeded with synthetic statements through the native `lore-rs` carrier. DOXA
then queried the isolated store normally. The provider read only the two
example files; it did not edit the repository. The published images contain
no account credentials, personal memory, or private workspace paths.

## Reproduce a capture

Build `doxa-rs`, `doxa-daemon`, and `lore-rs` from the same DOXA release.
Prepare a committed example repository and an empty private state directory
on real disk. Keep its runtime and control-socket paths short enough for Unix
sockets. Set `TMPDIR` to a directory on real disk; avoid `/tmp` for build and
capture files. The recorder leaves `HOME` intact for provider authentication
while isolating DOXA and LORE state.

```sh
TMPDIR=/path/on/real/disk /usr/bin/python3 scripts/live_rust_gallery.py run \
  --binary /absolute/path/to/doxa-rs \
  --daemon /absolute/path/to/doxa-daemon \
  --lore /absolute/path/to/lore-rs \
  --repo /absolute/path/to/harbour-notes \
  --state /short/private/gallery-state \
  --control /short/private/gallery.sock \
  --engine codex --model gpt-6-sol --with-lore \
  --provider-bin /absolute/path/to/protected/codex
```

The script requires system Python with GTK3/VTE, Xvfb, FFmpeg, and the stated
font. Seed only that isolated `LORE_ROOT` and `LORE_PROJECTS_DIR` with native
`lore-rs memory add` and `lore-rs belief add`. Enter commands through the VTE
control socket, wait for the desired live state, then capture it:

```sh
/usr/bin/python3 scripts/live_rust_gallery.py control \
  --control /short/private/gallery.sock \
  --action '{"kind":"input","text":"/memory\r"}'
/usr/bin/python3 scripts/live_rust_gallery.py control \
  --control /short/private/gallery.sock \
  --action '{"kind":"capture","name":"rust-curated-memory"}'
```

For the provider frame, select `/effort low` and wait for verification before
sending a prompt that asks Codex to read the two example files separately.
Use Tab to focus the transcript, Enter to expand the tool section, `]` to
select a tool, and Enter to expand it. `Ctrl+T` starts the additional tabs;
wheel input over the tab header switches them. `/help`, `/settings`, `/beliefs`,
and `/s` expose the other captured states. The script's `text` action reads
the actual terminal text for state checks, and `status` reports cell geometry.

Inspect every PNG and its dimensions before copying it from
`gallery-state/shots` to `assets/shots`. Stop the exact isolated capture
sessions, then send `{"kind":"quit"}` through the control socket. The
display and socket close on exit; session daemons have their own lifecycle.

`scripts/rust_gallery.py` remains a deterministic developer renderer. Its
fixtures go to an ignored build directory and are never used in the README.
