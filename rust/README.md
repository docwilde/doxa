# DOXA Rust 2.0 line

`doxa-tui` is the separate, tentative 2.0 frontend. The current Python DOXA
1.x release remains the supported application. The Rust binary is named
`doxa-rs` during development so installing it does not replace `doxa`.

The first milestone has a real terminal event loop, a client for DOXA's
versioned Unix-socket daemon, and a Markdown transcript presenter. It draws
grouped sessions and split panes, accepts prompts, handles resize and scroll,
and restores the terminal on exit. Existing daemon behavior and memory
authority remain on the Python side.

Build from this repository:

```sh
cargo build --manifest-path rust/doxa-tui/Cargo.toml
rust/doxa-tui/target/debug/doxa-rs --socket /path/to/existing/daemon.sock
rust/doxa-tui/target/debug/doxa-rs --list
rust/doxa-tui/target/debug/doxa-rs --session SESSION_ID
```

With one live daemon session, `doxa-rs` attaches to it directly. With multiple
sessions, use `--list` and select one by full ID or unique ID prefix.
`--socket` remains available for an explicit path. `--demo` opens the shell
without a connection. `Ctrl+Q` detaches the Rust UI
without stopping its daemon. On attach, the frontend restores prompts and
assistant text from the daemon's persisted JSONL file, then follows live
events from the same snapshot boundary. The visible view is limited to the
latest 40 turns, 20,000 assistant characters per turn, an 8 MiB file tail,
and the UI's 512 KiB transcript buffer. It marks omitted earlier content;
the JSONL file retains the full history. Older daemons without snapshot
metadata fall back to their 512-event replay ring. A turn still running at
attach can have text that was streamed but not yet persisted, so its earlier
in-flight deltas may be absent. Permission dialogs, rich
tool cards, clickable links, aligned Markdown tables, and drag dividers are
still 2.0 work. The binary version is `2.0.0-alpha.1` for this
separate development line, not a DOXA 2.0 release.

No 2.0 release tag is planned until the frontend reaches feature parity and
passes end-to-end terminal and daemon tests.

## Transcript persistence crate

`doxa-transcript` is a standalone Rust crate for Python 1.19 session JSONL and
Codex `<session-id>.codex.json` records. It reads a bounded 8 MiB/20,000-line
tail, appends original JSON objects with the Python `engine` override, and
retains unknown keys. Writes require a caller-supplied secret scrubber that
visits every string value. Codex metadata updates merge existing keys and use
an atomic replacement. Files and the project directory must belong to the
current user; symlink and hard-link file targets are refused.

Run `cargo test --locked --manifest-path rust/doxa-transcript/Cargo.toml`.
The crate is not yet wired into the daemon or UI. The caller still supplies
Python's `project_slug(cwd)` result and the engine's actual scrubber, and must
decide whether a persistence error affects an active session. The Python
vendor `.messages.json` replay file is outside this crate's current scope.
