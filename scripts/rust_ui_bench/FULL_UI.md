# Full-screen Ratatui rendering benchmark

`src/bin/full_ui.rs` draws a 160×48 DOXA-shaped screen in Ratatui's
`TestBackend`. It is a **visual shell model**, not a Rust port of DOXA.

It includes a grouped, nested session rail; seven tab labels in two split pane
groups; two visible transcript areas with turn headers and line-level
Markdown-like styling; per-pane status chips; prompt boxes; dividers; and a
modeled scroll offset. The fixture follows `scripts/bench_ui.py`: 20 sidebar
widths (22–41), one 120-line turn in each visible pane, 100 40-character
append chunks in the right pane, and 100 one-row scrolls. It renders every
operation, with 10 warmup draws per interaction run.

```sh
cargo build --release --manifest-path scripts/rust_ui_bench/Cargo.toml --bin full_ui
scripts/rust_ui_bench/target/release/full_ui --snapshot
scripts/rust_ui_bench/target/release/full_ui --runs 3
python scripts/rust_ui_bench/bench_full.py --runs 10
```

The three interaction numbers time the state change and `Terminal::draw`,
including widget construction, layout, and writes to a memory buffer. They
exclude terminal I/O, input dispatch, daemon work, SDK work, and event-loop
barriers. Cold launch must be measured externally around `--startup-only`;
it includes process startup and one initial draw. Each benchmark run resets
the fixture. The per-operation summary combines samples across runs.

The sidebar rows, tabs, transcript turn, status chips, prompt and scrollbar
offset are simulated Rust state. They do not support interaction or DOXA's
actual data model. Markdown is only line-level styling; it does not parse
CommonMark or stream through a Markdown widget. The other five tabs exist as
labels and state, not mounted pane widget trees. The fixture has DOXA's
first-run cover disabled (as in `bench_ui.py`), so the cover is omitted.

This benchmark is useful for measuring the Ratatui drawing cost of a more
representative visible screen. It must not be compared as an end-to-end UI
speedup against `scripts/bench_ui.py`, whose samples include Textual message
processing, asynchronous Markdown updates, and a compositor barrier.
