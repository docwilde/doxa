# Rust 2.0 alpha.31 UI benchmark, 27 September 2026

Code snapshot `7ff2ef2`, release binaries from the production Rust workspace.
AMD Ryzen 9 7950X, Linux 7.0.0-34 x86-64; CPU 2, warm filesystem cache.
The final measurements ran without concurrent compilation or seam tests.
These are local observations under normal desktop background load.

## Production draw and Markdown path

Three fresh `bench_frontend --warmups 2 --runs 5` processes rendered seven
sessions in two pane groups at 160 × 48 through Ratatui TestBackend.
Each append/scroll/resize includes the real reducer, Markdown presenter and draw.
The large transcript case uses 4,000 Markdown rows per visible pane.

| Operation | p50 ms | p95 ms |
| --- | ---: | ---: |
| First in-process draw | 0.523–0.561 | 0.710–0.830 |
| Sidebar resize and draw | 0.354–0.365 | 0.375–0.445 |
| Append and draw | 0.738–0.754 | 0.805–0.905 |
| Scroll and draw | 0.507–0.508 | 0.527–0.602 |
| Split change and draw | 0.855–0.884 | 0.876–1.036 |
| Large transcript redraw | 0.606–0.629 | 0.651–0.734 |
| Large transcript stream update | 1.038–1.051 | 15.256–15.451 |

## Production event loop through PTY

`scripts/bench_rust_pty.py --runs 3` starts the production Crossterm loop,
sends fixture daemon frames over a Unix socket and drives actual keyboard and
resize events. It uses one session with 160 lines at 160 × 48: 120 appends,
120 scroll keys and 60 terminal resizes. Tab twice focuses the transcript.

| Operation | p50 ms | p95 ms |
| --- | ---: | ---: |
| Daemon text update to first output byte | 9.055 | 9.350 |
| Scroll key to last output byte | 0.696 | 0.812 |
| Terminal resize to last output byte | 1.581 | 2.012 |
| Startup initial output burst (3 samples) | 38.882 | 39.015 |

Startup requires visible `Prompt` text before accepting a draw. The initial
burst includes asynchronous startup notices and ends at a 12 ms quiet interval;
its elapsed value excludes that final quiet interval. It does not isolate a
single first frame. Interaction timings end at escape-byte delivery to the PTY.

## Fresh CLI startup

`scripts/bench_rust_cli_startup.py --runs 60` launches `doxa-rs --demo` in
an empty DOXA_HOME and fresh PTY for every observation. The harness removes
keyboard protocol overrides, sends no capability replies, rejects query bytes
and waits for visible `Prompt` text.

| Size | p50 ms | p95 ms |
| --- | ---: | ---: |
| 160x48 | 5.760 | 8.281 |
| 80x24 | 4.918 | 6.759 |

Default startup now selects legacy key mode without blocking capability probes.
Enhanced Kitty keys remain an explicit `DOXA_KEYBOARD_PROTOCOL=kitty` opt-in.
The demo includes CLI parsing, preferences, clock and terminal setup. Live
provider connection, restored sessions, network time and terminal painting are
outside this measurement.

## Comparison and limits

The [25 September fixture](rust-ui-benchmark-2026-09-25.md) measured append
at about 0.634 ms, scroll 0.660 ms and sidebar resize 0.127 ms median. The
richer alpha.31 draw path increases append/resize time while scroll improves.
PTY append remains about 9 ms; scrolling remains below 1 ms median. Large
Markdown stream updates have a roughly 15 ms p95, a remaining performance
target for profiling.

Historical CLI startup used the visible Sessions marker; alpha.31 uses Prompt
because an empty or narrow window may hide the sidebar. Startup output also
includes new preference and worker notices, so marker/burst comparisons need
that distinction. TestBackend excludes terminal I/O; PTY delivery excludes
terminal paint and provider latency. The older Textual harness used a different
compositor and paint barrier, so it does not establish a direct speedup ratio.

Reproduce after `cargo build --locked --release --workspace --bins --examples`.
Use `taskset -c 2`, private TMPDIR/DOXA_HOME and `--binary target/release/...`
for both Python harnesses. The startup harness itself checks the no-probe path.
