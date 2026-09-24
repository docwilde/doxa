# DOXA Rust 2.0 line

`doxa-tui` is the separate, tentative 2.0 frontend. The current Python DOXA
1.x release remains the supported application. The Rust binary is named
`doxa-rs` during development so installing it does not replace `doxa`.

The first milestone has a real terminal event loop, a client for DOXA's
versioned Unix-socket daemon, and a Markdown transcript presenter. It draws
grouped sessions and split panes, accepts prompts, handles resize and scroll,
and restores the terminal on exit. Existing daemon behavior and memory
authority remain on the Python side.

Build the native frontend and daemon from this repository:

```sh
cargo build --manifest-path rust/doxa-tui/Cargo.toml
cargo build --manifest-path rust/doxa-daemon/Cargo.toml
DOXA_DAEMON_BIN="$PWD/rust/doxa-daemon/target/debug/doxa-daemon" \
  rust/doxa-tui/target/debug/doxa-rs new
```

`doxa-rs` now starts a native Codex session when no live sessions exist in the
current project. `new` always starts one; `attach ID` reattaches, `stop ID`
finalizes a running session, `list` shows all live sessions, and `doctor`
checks executable resolution and registry access. `--session ID` and `--socket`
remain available. A unique session ID prefix works for `attach` and `stop`.
The daemon executable is located beside `doxa-rs`, then on `PATH`; an absolute
`DOXA_DAEMON_BIN` overrides that search. The native Codex host needs the
`codex` and `python3` executables on `PATH`, or explicit `--codex-bin` and
`--lore-python` paths. The Python interpreter needs DOXA and LORE installed.
`--model` overrides `DOXA_MODEL` and the Codex entry under `[models]` in
`$DOXA_HOME/config.toml`; `--linger` overrides `DOXA_LINGER_SECS` and the
`linger_secs` config value. `--sandbox` sets the native Codex sandbox. Use
`--engine fixture` only for local integration checks.

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
without stopping its daemon. `Ctrl+M` opens the read-only peer communications
map; Up/Down selects a peer, R refreshes the live roster, and Esc closes it.
The map uses `tui-nodes` 0.9 with the Rust frontend's Ratatui 0.29.
Lines show observed traffic and the detail row names sent and received counts.
Native daemons without peer support show an unavailable state. On attach, the frontend restores prompts and
assistant text from the daemon's persisted JSONL file, then follows live
events from the same snapshot boundary. The visible view is limited to the
latest 40 turns, 20,000 assistant characters per turn, an 8 MiB file tail,
and the UI's 512 KiB transcript buffer. It marks omitted earlier content;
the JSONL file retains the full history. Older daemons without snapshot
metadata fall back to their 512-event replay ring. A turn still running at
attach can have text that was streamed but not yet persisted, so its earlier
in-flight deltas may be absent. Rich tool cards and clickable links are
still 2.0 work. The binary version is `2.0.0-alpha.3` for this
separate development line, not a DOXA 2.0 release.

Alpha tags identify preview snapshots. A stable 2.0 release waits until the
frontend reaches feature parity and passes end-to-end terminal and daemon
tests.

Start the native Claude host from the terminal frontend with
`doxa-rs new --engine claude --claude-python /absolute/path/to/python
--claude-script /absolute/path/to/claude_sidecar.py`. The Python interpreter
must have DOXA, LORE, and the Claude Agent SDK installed. Add `--model NAME`
to choose a model, or `--resume SESSION_ID` to resume that session's Claude
conversation. `doxa-rs doctor --engine claude` checks the selected Python
interpreter, sidecar script, daemon, and runtime path.

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

To host Claude, pass `--engine claude --claude-python /absolute/path/to/python
--claude-script /absolute/path/to/claude_sidecar.py`. This uses DOXA's Python
`SessionEngine` and Claude Agent SDK in a separate process. `--model` is optional.
To resume, also pass `--session-id ID --resume true`. The daemon forwards bounded
events and supports `answer_needs_input`, `interrupt`, and graceful `finalize`
on exit. The Python SDK remains required in this alpha.

The native plain-chat vendor host accepts `--engine deepseek` or `--engine glm`
with `--lore-python /absolute/path/to/python`; the interpreter must have DOXA
and LORE installed. Keys come only from `DEEPSEEK_API_KEY` or `ZAI_API_KEY` in
the environment. `--model` and `--effort low|high|max` are optional; DeepSeek
also accepts `--effort none`. Provider endpoints are fixed in production.
The host advertises no tools, rejects any provider tool call, and holds bounded
conversation history only in memory. The full provider response is withheld
until LORE scrubs it; a scrub failure fails the turn and commits no history.
It reports provider model and token usage but no dollar cost. Interrupt and
stop cancel the in-flight HTTP request. Vendor transcript persistence, resume,
LORE context, tool execution, pricing, and live-provider validation remain open.

Codex turns use `codex exec --json` and resume subsequent turns using the
provider thread ID. User and assistant text is appended to Python 1.19-shaped
JSONL under LORE's project directory, and `<session-id>.codex.json` records the
provider thread for a later daemon started with the same `--session-id`.
The LORE sidecar supplies the exact project identity and scrubs every persisted
string; failure to scrub leaves the new record unwritten. An existing transcript
without a thread ID refuses a fresh Codex thread. `interrupt` cancels the
running CLI process group, and `stop` cancels it and closes the daemon. The
native Codex host does not yet register MCP, integrate LORE context/review/indexing,
or implement peer messaging. The
registry reports the selected engine. `status`, `interrupt`, and `stop` are
supported; Claude also supports `answer_needs_input`. Other calls return an explicit error. The
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
The native Codex host uses this crate. The Rust UI still relies on its existing
transcript reader. The Python vendor `.messages.json` replay file is outside
this crate's current scope.
