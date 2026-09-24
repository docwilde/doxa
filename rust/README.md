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

Or compile and install the preview from a ref with the POSIX installer:

```sh
curl -fsSL https://raw.githubusercontent.com/docwilde/doxa/rust/2.0/scripts/install.sh | sh -s -- --rust
```

The default ref is `rust/2.0`; append a branch, tag, or commit SHA to select
another. This needs Git and Cargo. It installs `doxa-rs` in `~/.local/bin`
(override with `DOXA_RUST_BIN_DIR`) and installs `doxa-daemon-rs` there if
the selected ref includes a native daemon. It does not replace the Python
`doxa` command. The native daemon is still a preview; `doxa-rs` can also
connect to an existing Python daemon.

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
in-flight deltas may be absent. Rich tool cards and clickable links are
still 2.0 work. The binary version is `2.0.0-alpha.2` for this
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
