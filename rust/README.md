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
```

`--demo` opens the shell without a connection. `Ctrl+Q` detaches the Rust UI
without stopping its daemon. On attach, the frontend restores prompts and
assistant text from the daemon's persisted JSONL file, then follows live
events from the same snapshot boundary. The visible view is limited to the
latest 40 turns, 20,000 assistant characters per turn, an 8 MiB file tail,
and the UI's 512 KiB transcript buffer. It marks omitted earlier content;
the JSONL file retains the full history. Older daemons without snapshot
metadata fall back to their 512-event replay ring. A turn still running at
attach can have text that was streamed but not yet persisted, so its earlier
in-flight deltas may be absent. Session discovery, permission dialogs, rich
tool cards, clickable links, aligned Markdown tables, and drag dividers are
still 2.0 work. The binary version is `2.0.0-alpha.1` for this
separate development line, not a DOXA 2.0 release.

No 2.0 release tag is planned until the frontend reaches feature parity and
passes end-to-end terminal and daemon tests.
