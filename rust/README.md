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
without stopping its daemon. The current alpha attaches to one socket and
renders text turns; permission dialogs, tool cards,
clickable links, aligned Markdown tables, drag dividers, and full transcript
restore are still 2.0 work. The binary version is `2.0.0-alpha.1` for this
separate development line, not a DOXA 2.0 release.

No 2.0 release tag is planned until the frontend reaches feature parity and
passes end-to-end terminal and daemon tests.
