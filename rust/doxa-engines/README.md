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

This crate does **not** launch or authenticate `codex`, persist thread IDs or
transcripts, configure sandbox/MCP, enforce budget, or own cancellation and
stderr. `EngineCapabilities::default()` advertises none of those provider
features. The Python `doxa/codex.py` remains the behavior reference.

Run `cargo test --manifest-path rust/doxa-engines/Cargo.toml` from the repo
root. Fixture data contains no credentials.
