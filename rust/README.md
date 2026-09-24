# DOXA Rust 2.0 line

`doxa-tui` is the separate, tentative 2.0 frontend. The current Python DOXA
1.x release remains the supported application. The Rust binary is named
`doxa-rs` during development so installing it does not replace `doxa`.

The first milestone is a real terminal event loop, a client for DOXA's
versioned Unix-socket daemon, and a Markdown transcript presenter. It must
render grouped sessions and split panes, accept input, resize, scroll, and
recover terminal state on exit. Existing daemon behavior and memory authority
remain on the Python side until corresponding Rust components are implemented
and tested.

Build from this repository:

```sh
cargo build --manifest-path rust/doxa-tui/Cargo.toml
```

No 2.0 release tag is planned until the frontend reaches feature parity and
passes end-to-end terminal and daemon tests.
