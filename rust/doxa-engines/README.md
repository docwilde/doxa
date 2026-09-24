# `doxa-engines` first slice

This standalone crate normalizes `codex exec --json` stdout into DOXA's
`{ "type": "...", "data": { ... } }` engine event shape. It has no TUI or daemon
dependency. Feed arbitrary stdout byte chunks to `push_bytes`, call
`begin_turn` before each turn, and call `finish_turn` after the subprocess
exits. `finish_turn` handles a final line without a newline and emits one
`turn_done` unless a `turn.failed` or `error` frame already did so. A line
over 8 MiB returns `LineTooLong`; the future process adapter must stop and
report that failure instead of continuing with an unsynchronized stream.

Construct the parser with the application's secret scrubber. There is no
default identity scrubber because text and tool output enter UI/transcripts.
The Rust port does not yet have a `lore_core.scrub` equivalent; the fixture
uses a small explicit scrubber only to prove this injection boundary.

The Unix-only `CodexCliDriver` now launches the CLI with separate argv
elements, sends the prompt on stdin, streams stdout into this normalizer,
drains a bounded stderr tail concurrently, and kills the process group on
cancellation, timeout, parser overrun, or explicit terminal error. It uses
the same first-turn / `exec resume THREAD` argv shape as Python, except
that MCP overrides and linked-worktree Git writable-root overrides are not
yet present. It validates a thread ID before passing it to the CLI.

The driver does **not** authenticate `codex`, persist thread IDs or
transcripts, register MCP, implement Git writable-root widening, enforce
budget, or provide a production secret scrubber. `EngineCapabilities::default()`
advertises none of those provider features. The Python `doxa/codex.py`
remains the behavior reference. Running this driver requires an already
installed, authenticated Codex CLI; tests use executable shell fixtures
and no account.

Run `cargo test --manifest-path rust/doxa-engines/Cargo.toml` from the repo
root. Fixture data contains no credentials.
