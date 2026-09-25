# Rust 2.0 UI benchmark, 25 September 2026

This measures the DOXA Rust 2.0 alpha frontend at commit `b7873bc` on an
AMD Ryzen 9 7950X running Linux 7.0 x86-64. Release builds ran on CPU 2
with a warm filesystem cache. Figures are p50 / p95 milliseconds. They are
local observations, not portable latency guarantees.

## Full UI draw through Ratatui TestBackend

Run `taskset -c 2 rust/doxa-tui/target/release/bench_frontend --warmups 2
--runs 5`. Three fresh processes each used seven sessions in two pane groups,
160 × 48 cells, and 120 Markdown lines in each visible pane. Every timed
operation includes the real Rust app reducer, Markdown presenter, and
Ratatui draw into an in-memory `TestBackend`.

| Operation | p50 / p95 |
| --- | ---: |
| First in-process draw | 0.23–0.48 / 0.47–0.49 |
| Sidebar resize and draw | 0.127 / 0.131–0.133 |
| Append and draw | 0.633–0.636 / 0.697–0.700 |
| Scroll and draw | 0.659–0.661 / 0.696–0.697 |
| Split change and draw | 0.561–0.562 / 0.567–0.578 |

The historical integrated Rust fixture from 24 September measured append
around 0.607 / 0.672, scroll around 0.636 / 0.673, and resize around
0.117 / 0.121. The richer alpha UI is modestly slower in this synthetic
draw fixture.

## Production event loop through a terminal PTY

Run `taskset -c 2 python3 scripts/bench_rust_pty.py --runs 3`. The harness
starts the release Rust frontend, sends daemon fixture frames over a Unix
socket, drives Crossterm keyboard and resize events, and timestamps terminal
escape output. It seeds one session with 160 lines at 160 × 48 cells. There
are 120 append, 120 scroll, and 60 resize observations across three fresh
processes.

| Operation | p50 / p95 |
| --- | ---: |
| Process start to first complete frame (3 runs) | 1.963 / 1.992 |
| Daemon text update to first terminal byte | 9.041 / 10.103 |
| Scroll key to terminal output | 0.715 / 0.794 |
| Terminal resize to last output byte | 0.879 / 1.218 |

The separate `python3 scripts/bench_rust_cli_startup.py --runs 60` harness
starts the release `doxa-rs --demo` command in a fresh PTY and empty
`DOXA_HOME` for each sample. Time from process launch to the first visible
`Sessions` text was 1.474 / 1.845 ms at 160 × 48 and 1.170 / 1.365 ms at
80 × 24. These are fresh processes with warm OS page caches. This includes
CLI parsing and terminal startup but skips live session discovery, daemon
startup, providers, and restoration; first visible text is not the final
painted frame.

Before the event-loop fix in `b7873bc`, the same phase-varying PTY harness
measured daemon text update to terminal output at 78.581 / 99.336 ms. The
loop had waited for keyboard input before drawing received frames. It now
draws drained daemon frames before that wait and polls input at 10 ms.

The PTY timings end when escape bytes are written; they do not include
terminal paint, a real provider, network latency, or a seven-session PTY
load. The TestBackend figures exclude terminal I/O and event scheduling.
Neither set is a direct speedup ratio against the older Python benchmark.

For historical context, the [Textual benchmark](ui-benchmark-2026-09-24.md)
measured Textual 5 headless append at 984.5–1,144.3 ms median, scrolling at
31.9–32.1 ms, and resizing at 72.7–74.8 ms in its seven-session fixture.
That harness used Textual's widget/message compositor and a forced paint
barrier. No production Python PTY timings were collected, so the two
implementations cannot be ranked by those numbers alone.
