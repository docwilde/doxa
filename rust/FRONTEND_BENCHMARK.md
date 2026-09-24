# Rust 2.0 frontend benchmark

This benchmark measures the integrated `doxa-tui` frontend on the Rust 2.0
foundation branch, through `ui::App::apply_daemon_frame`, `App::handle` and
`App::draw` into a Ratatui `TestBackend`. The timing for each interaction
starts immediately before its state change and ends after `Terminal::draw`
returns. The draw includes Markdown parsing and presentation for visible
transcripts. It is not the older Rust visual fixture.

## Reproduce

From the repository root:

```sh
cargo build --release --manifest-path rust/doxa-tui/Cargo.toml --bin bench_frontend
taskset -c 2 rust/doxa-tui/target/release/bench_frontend --warmups 2 --runs 5
```

Repeat the `taskset` command three times. It emits JSON with sample counts,
median, nearest-rank p95, minimum and maximum in milliseconds. A run has two
untimed fixture warmups and five measured repetitions. The measured fixture
is 160×48 with seven sessions in two visible groups (four left, three right),
one 120-line Markdown bullet transcript in each visible pane, and a visible
sidebar. Each repetition performs:

| Series | State change and completed draw per sample | Samples |
| --- | --- | ---: |
| Startup | Construct app and backend, apply resize and one daemon hello frame, draw first frame | 1 |
| Resize | Set sidebar width to each integer from 22 to 41, draw | 20 |
| Split | Alternate Alt+Right/Alt+Left input events, changing the split ratio, draw | 20 |
| Append | Apply a daemon `text_delta` frame with a 40-character chunk, draw | 100 |
| Scroll | Handle one Down key event from transcript focus, moving one row toward the bottom, draw | 100 |

The resize series precedes transcript seeding, matching `scripts/bench_ui.py`.
The append and scroll series run after both visible transcripts are seeded.
The two warmup repetitions are discarded before statistics. All operations
check that their intended state changes occurred.

## Measurements

AMD Ryzen 9 7950X, Linux x86-64, CPU affinity pinned to logical core 2;
release profile (`opt-level=3`), three process executions on 2026-09-24.
Each interaction row has 100 resize, 100 split, 500 append or 500 scroll
samples per execution; startup has five. Values are milliseconds, p50/p95.

| Series | Run 1 | Run 2 | Run 3 |
| --- | ---: | ---: | ---: |
| Startup | 0.2107 / 0.4178 | 0.1738 / 0.2094 | 0.2231 / 0.2372 |
| Resize | 0.1172 / 0.1217 | 0.1158 / 0.1204 | 0.1175 / 0.1217 |
| Split | 0.5413 / 0.5562 | 0.5380 / 0.5449 | 0.5372 / 0.5434 |
| Append | 0.6082 / 0.6710 | 0.6076 / 0.6721 | 0.6066 / 0.6728 |
| Scroll | 0.6366 / 0.6797 | 0.6365 / 0.6727 | 0.6353 / 0.6731 |

The Rust and Python benchmarks are scenario comparisons, not identical
workloads. The Rust test backend omits terminal escape encoding, terminal
I/O and compositor scheduling. It applies already-decoded JSON frames rather
than running a daemon worker, socket reader or background stream task.
Python Textual constructs and maintains a much larger widget tree. The Rust
Markdown presenter supports a smaller set of widgets and features, so full
presenter parity is still outstanding. Rust startup times cover a first
headless frame with one synthetic hello; Python startup includes `run_test`
and app mount. The independent split series has no Python counterpart.
