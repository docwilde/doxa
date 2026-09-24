# Ratatui rendering prototype

This is a narrow benchmark prototype, not a DOXA client or a port of its
Textual widget tree. It uses Ratatui's in-memory `TestBackend`: there is no
terminal I/O, SDK call, daemon, input handling, or live process.

Run with:

```sh
cargo build --release --manifest-path scripts/rust_ui_bench/Cargo.toml
scripts/rust_ui_bench/target/release/doxa-ratatui-bench
scripts/rust_ui_bench/target/release/doxa-ratatui-bench --events scripts/rust_ui_bench/sample-events.jsonl
scripts/rust_ui_bench/target/release/doxa-ratatui-bench --startup-only
```

The optional JSONL file demonstrates consumption of the versioned `hello`
and `text_delta` event envelope used by `doxa.daemon`; ingest is outside the
timed region. The benchmark prints operation count and p50/p95 wall time in
milliseconds. A draw means Ratatui builds widgets and writes to the memory
buffer. `resize_draw` times one sidebar width change and one draw at each
of 20 widths, 22 through 41 columns, while the terminal stays 160x48.
`append_draw` times appending one 40-character chunk (`delta NNN: ` plus
28 `x` characters and a newline) to the visible transcript, scrolling to
the end, and drawing, repeated 100 times. `scroll_draw` times a one-row
scroll update and a draw, repeated 100 times. After resize, each of the
two visible transcripts receives the same 120 lines as the Python harness;
the other five tabs have empty transcripts. Seven tabs are arranged in
groups of four and three.

`--startup-only` draws one initial frame and exits, suitable for an external
process startup timer. The full command's warmup and fixture creation are
outside the interaction timing.

These numbers measure this prototype's operations only. They cannot be
treated as an end-to-end speedup for DOXA.
