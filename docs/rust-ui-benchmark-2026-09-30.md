# Rust 2.0 alpha.50 UI benchmark, 30 September 2026

Code snapshot `5d05510` on `main`, release binaries built from the production
Rust workspace with `--locked`. AMD Ryzen 9 7950X, Linux 7.0.0-34 x86-64;
CPU 2, warm filesystem cache. Timed runs had no concurrent compilation or
other benchmark process. Temporary directories were on the workstation's real
disk, outside `/tmp`. These are local observations under desktop background
load, not provider or terminal-paint measurements.

## Production draw and Markdown path

Three fresh `bench_frontend --warmups 2 --runs 5` processes rendered seven
sessions in two pane groups at 160 × 48 through Ratatui TestBackend.
Append, scroll and resize include the actual reducer, Markdown presenter and
draw. The large transcript case contains 4,000 Markdown rows per visible
pane; each process contributes 100 large-transcript samples.

| Operation | p50 ms across processes | p95 ms across processes |
| --- | ---: | ---: |
| First in-process draw | 0.413–0.437 | 0.556–0.568 |
| Sidebar resize and draw | 0.253–0.255 | 0.267–0.268 |
| Append and draw | 0.498–0.503 | 0.534–0.538 |
| Scroll and draw | 0.342–0.344 | 0.350–0.351 |
| Split change and draw | 0.586–0.591 | 0.597–0.604 |
| Large transcript redraw | 0.382–0.385 | 0.390–0.411 |
| Large transcript stream update | 0.563–0.574 | 0.601–0.608 |

Relative to the [alpha.31 fixture](rust-ui-benchmark-2026-09-27.md), the
median append, scroll and large-transcript stream measurements are lower.
The earlier large-stream p95 was 15.26–15.45 ms; here it is 0.60–0.61 ms.
The fixtures and host match, but intervening source and environment changes
prevent attributing that difference to one optimization.

## Production event loop through PTY

`scripts/bench_rust_pty.py --runs 3` starts the production Crossterm loop,
sends fixture daemon frames over a Unix socket, and drives actual keyboard
and resize events. It uses one session with 160 lines at 160 × 48: 120
appends, 120 scroll keys and 60 terminal resizes. Tab twice focuses the
transcript.

| Operation | p50 ms | p95 ms |
| --- | ---: | ---: |
| Daemon text update to first output byte | 8.791 | 9.327 |
| Scroll key to last output byte | 0.469 | 0.638 |
| Terminal resize to last output byte | 0.952 | 1.435 |
| Startup initial output burst (3 samples) | 26.268 | 28.014 |

The initial burst ends at a 12 ms quiet interval and excludes that final
quiet interval; it does not isolate a single first frame. Interaction
timings end at escape-byte delivery to the PTY. The terminal emulator's
painting time and real daemon/provider work are outside this measurement.

## Fresh CLI startup

`scripts/bench_rust_cli_startup.py --runs 60` launches `doxa-rs --demo` in
an empty DOXA_HOME and fresh PTY for every observation. The harness removes
keyboard protocol overrides, sends no capability replies, rejects query
bytes, and waits for visible `Prompt` text.

| Size | Visible Prompt p50 ms | Visible Prompt p95 ms |
| --- | ---: | ---: |
| 160 × 48 | 4.210 | 6.117 |
| 80 × 24 | 3.588 | 3.850 |

This includes CLI parsing, preferences, clock and terminal setup. It
excludes provider connection, restored sessions and network time. The
alpha.31 visible-marker medians were 5.760 ms and 4.918 ms, respectively;
the same marker and scripts were used here.

## Reproduction and limits

Build the production binaries with `cargo build --release --locked
--manifest-path rust/doxa-tui/Cargo.toml -p doxa-tui --bins`. Run each
benchmark pinned with `taskset -c 2`; use a private real-disk `TMPDIR` and
`DOXA_HOME` for both Python harnesses. Raw JSON results from this run are
local in `doxa-worktrees/bench-tmp`; they contain no provider credentials.

TestBackend excludes terminal I/O; PTY delivery excludes paint and provider
latency. The older Python/Textual benchmark used a different compositor and
paint barrier, so these data do not establish a direct framework speedup ratio.
