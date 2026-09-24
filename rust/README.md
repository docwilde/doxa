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

## Native daemon

`doxa-daemon` is a native protocol v1 host process for lifecycle and transport
integration. Run `cargo build --manifest-path rust/doxa-daemon/Cargo.toml`, then
`rust/doxa-daemon/target/debug/doxa-daemon --cwd /path/to/project`. The process
prints no prompt or transcript data. Its Unix socket and owner-private registry
entry live under `DOXA_RUNTIME_DIR` when set, otherwise `$XDG_RUNTIME_DIR/doxa`
or `~/.local/share/doxa`. The registry's `daemon_socket` field can be passed to
`doxa-rs --socket` or used by Python `EngineClient`; it has the same hello,
attach, prompt, event, reply, and stop wire shapes as the Python daemon. `--linger`
sets seconds to wait after the last client detaches (default 120). An unclaimed
process gets at least 120 seconds for its first attach. SIGTERM and SIGINT stop
the socket and remove the registry entry.

The default `fixture` host returns one fixed response for every prompt. To
select the first native Codex host explicitly, pass `--engine codex` with
`--codex-bin /absolute/path/to/codex` and
`--lore-python /absolute/path/to/python`. The interpreter must have DOXA and
LORE installed. The Codex CLI must already be authenticated. Both executable
paths are resolved and checked before opening a socket. Optional `--model`
and `--sandbox read-only|workspace-write|danger-full-access` are passed as
separate CLI arguments, never through a shell. A missing LORE sidecar prevents
the session from starting; a scrub failure during a turn withholds further
provider events and fails the turn. The Python sidecar is a temporary
dependency while Rust memory integration is built.

Codex turns use `codex exec --json` and resume subsequent turns using the
provider thread ID. `interrupt` cancels the running CLI process group, and
`stop` cancels it and closes the daemon. The native Codex host does not yet
persist transcripts or thread IDs across daemon restarts, register MCP,
integrate LORE context/review/indexing, or implement peer messaging. The
registry reports the selected engine. Only `status`, `interrupt` (Codex),
and `stop` are supported; other calls return an explicit error. The
`socket_path` is suitable for local TUI or Python `EngineClient` attach, but
peer frames are not implemented. Treat this as an integration alpha.
