# Full-screen Ratatui rendering benchmark

`src/bin/full_ui.rs` draws a 160×48 DOXA-shaped screen in Ratatui's
`TestBackend`. It is a **visual shell model**, not a Rust port of DOXA.

It includes a grouped, nested session rail; seven tab labels in two split pane
groups; two visible transcript areas with turn headers and CommonMark-backed
text styling; per-pane status chips; prompt boxes; dividers; and a
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

The three operation families time the state change and `Terminal::draw`,
including widget construction, layout, and writes to a memory buffer. The
append operation reparses the **entire accumulated transcript source** with
`pulldown-cmark` 0.13.4 after every chunk, converts parser events to styled
Ratatui lines, updates the scroll offset, and draws. `parse_only` times just
the parse-and-line-conversion portion within those same append samples;
`append_parse_draw` times the whole operation. These timings
exclude terminal I/O, input dispatch, daemon work, SDK work, and event-loop
barriers. Cold launch must be measured externally around `--startup-only`;
it includes process startup and one initial draw. Each benchmark run resets
the fixture. The per-operation summary combines samples across runs.

The sidebar rows, tabs, transcript turn, status chips, prompt and scrollbar
offset are simulated Rust state. They do not support interaction or DOXA's
actual data model. The Markdown parser is real CommonMark, with tables and
strikethrough enabled; the Ratatui presenter handles lists, paragraphs,
headings, emphasis, strong text, code, and line breaks. It does not implement
Textual's Markdown widget tree, incremental stream, full table layout, or
link interactions. The other five tabs exist as
labels and state, not mounted pane widget trees. The opening DOXA banner is
drawn; the first-run setup wizard is disabled, as in `bench_ui.py`.

This benchmark is useful for measuring the Ratatui parse and drawing cost of a
more representative visible screen. It must not be compared as an end-to-end
UI speedup against `scripts/bench_ui.py`, whose samples include Textual message
processing, asynchronous Markdown updates, and a compositor barrier.

## Local measurement (2026-09-24)

AMD Ryzen 9 7950X; release binary, pinned to CPU 0 with `taskset -c 0`.
Three interaction fixtures and ten fresh-process startup runs. Values are in
milliseconds; the complete sample distributions are summarized by range and
quartiles below. File cache was warm, so "cold launch" means a fresh process,
not a cold filesystem cache.

| Operation | Samples | Min | P25 | Median | P75 | P95 | Max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Sidebar resize + draw | 60 | 0.1350 | 0.1531 | 0.1551 | 0.2865 | 0.3657 | 0.3718 |
| Full-source Markdown parse + line conversion | 300 | 0.0249 | 0.0283 | 0.0310 | 0.0340 | 0.0363 | 0.0435 |
| Append + parse + draw | 300 | 0.3017 | 0.3269 | 0.3472 | 0.3694 | 0.3879 | 0.3983 |
| One-row scroll + draw | 300 | 0.2155 | 0.2351 | 0.2544 | 0.2752 | 0.2910 | 0.2962 |
| Fresh-process launch + first draw | 10 | 1.3798 | — | 1.3937 | — | 1.6349 | 1.6349 |
