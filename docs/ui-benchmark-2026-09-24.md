# UI runtime benchmark, 24 September 2026

This is a local, headless comparison of runtime choices for DOXA. The source
revision is `da911aa` on `main`; no real agent, daemon, network request, or
terminal I/O is included in the interaction timings. The host was an AMD
Ryzen 9 7950X running Linux 7.0, Python 3.12.12. Timed runs were sequential
and pinned to CPU 2.

## Workload

Run `python scripts/bench_ui.py --startup-runs 10 --interaction-runs 1` from
the repository root with `PYTHONPATH=.`. It creates seven fake sessions in
two visible pane groups (four and three tabs) at 160 × 48 cells. The sidebar
is visible. The two active transcripts each receive 120 fixed Markdown
lines. The measured operations are 20 sidebar widths (22–41 cells), 100
fixed 40-character streamed chunks, and 100 one-row scroll steps.

Each interaction sample includes the state change, queued Textual messages,
and a forced compositor update through `Pilot.pause(0)`. The append sample
also waits until the Markdown widget contains the chunk and performs one
further paint barrier. The zero-delay form is intentional: plain
`Pilot.pause()` may wait up to one second for process CPU idle, which
distorted early exploratory measurements. The benchmark reports medians
and 95th percentiles in milliseconds. It does not measure terminal output,
keyboard input, daemon startup, provider API latency, or a typical small
conversation.

The harness disables DOXA's first-run wizard and background update check.
The latter otherwise runs a real `git fetch` when the source is a checkout,
while a frozen executable has no checkout and skips it. Both would distort
the comparison.

Cold process figures use 20 separate `--startup-runs 0 --interaction-runs 0`
invocations for benchmark-entry import overhead, and 10 separate
`--startup-runs 1 --interaction-runs 0` invocations for process launch,
first headless frame, and clean exit. The latter is **not** a timestamp of
first visible terminal output; its wall time includes teardown. In-process
startup is the time from `DoxaApp` construction through its first headless
paint, after Python imports have already completed. The benchmark entry
imports `tests.fakes` and its SDK dependencies, so these process timings
must not be used as measurements of the production `doxa` CLI.

## Results

The table below records one full interaction pass per variant. Operation
sample counts per pass are 20 resize, 100 append, and 100 scroll. These
figures are local observations, not portable latency guarantees.

| Runtime | First paint | Resize | Append | Scroll | Full pass |
| --- | ---: | ---: | ---: | ---: | ---: |
| Source, pass 1 | 205 / 211 | 108 / 155 | 1,178 / 1,332 | 32 / 226 | 127.7 s |
| Source, repeat | 186 / 200 | 81 / 110 | 1,150 / 1,344 | 32 / 233 | 125.5 s |
| Textual 8.2.8* | 158 / 217 | 26 / 181 | 1,169 / 1,431 | 47 / 573 | 134.3 s |
| PyInstaller | 191 / 196 | 84 / 137 | 1,074 / 1,259 | 36 / 228 | 119.2 s |
| Nuitka | 215 / 263 | 92 / 188 | 1,146 / 1,375 | 44 / 270 | 129.6 s |

Each operation cell is **p50 / p95 milliseconds**. The repeated source
run shows substantial natural variation in resize time, so a difference
of this size between packages is not evidence that packaging changes
Textual's drawing algorithm. The dense Markdown append remains above
one second at the median in every variant.

*The Textual 8 run used four production API compatibility edits in three
files. The dependency and tests were not upgraded. Its median resize was
faster, but append was within the source range and scrolling had a larger
tail latency. This was one experimental pass, not a compatibility signoff.

### Paired Textual 5 and Textual 8 follow-up

The complete Textual 8 migration in PR #94 passed 2,842 tests (5 skipped).
Four interaction passes alternated 5 → 8 → 5 → 8 on the same CPU 0 with the
same fixture and zero-delay paint barriers. Each operation cell is p50 / p95
milliseconds; each pass has 20 resizes, 100 Markdown appends, and 100 scrolls.

| Runtime and pass | Resize | Append | Scroll |
| --- | ---: | ---: | ---: |
| Textual 5, first | 74.8 / 105.5 | 984.5 / 1,154.0 | 32.1 / 206.0 |
| Textual 8, first | 70.1 / 94.3 | 1,149.8 / 1,400.0 | 47.4 / 560.0 |
| Textual 5, second | 72.7 / 103.8 | 1,144.3 / 1,299.8 | 31.9 / 214.0 |
| Textual 8, second | 69.2 / 134.8 | 1,311.7 / 1,554.7 | 46.3 / 572.0 |

Textual 8's median resize gain was small in these paired runs. Markdown
appends and scrolling were slower, especially the scroll tail. A focused
scroll profile observed 93 full compositor-map rebuilds and 186 root
arrangements on Textual 8, versus no full-map rebuilds and 95 arrangements
on Textual 5. Profiling adds overhead, so it explains the direction rather
than providing comparable absolute latency. The migration remains a separate
choice; these runs do not support it as a performance upgrade.

| Benchmark executable | Import and exit | One first frame and exit | Bundle | Build |
| --- | ---: | ---: | ---: | ---: |
| Source Python | 615 ms | 834 ms | — | — |
| Textual 8.2.8* | 566 ms | 748 ms | — | — |
| PyInstaller onedir | 732 ms | 964 ms | 95 MB | 12.9 s |
| Nuitka standalone | 790 ms | 1,043 ms | 190 MB | 256.2 s |

The import and process-launch entries are medians across 20 and 10
separate processes respectively. Startup measurements inside the UI run
appear in the first table. The Nuitka build recompiled 1,370 C objects
after a small harness change; the observed 256-second build was not an
incremental developer loop win.

The separate Ratatui `TestBackend` prototype ran five fresh processes.
Across those runs, resize p50 was 0.227–0.234 ms, append p50
0.291–0.297 ms, and scroll p50 0.239–0.245 ms. Representative p95 values
were 0.263, 0.324, and 0.274 ms. Its 920,240-byte release binary started,
drew one frame, and exited in 1.775 ms median across 30 processes. Those
numbers measure a much smaller renderer and must not be divided into the
Textual figures to claim a DOXA speedup.

## Scope of each variant

- **Textual 5.3.0:** DOXA's current pinned runtime, using the real widget
  tree and a fake engine.
- **Textual 8.2.8:** Same DOXA fixture and script, with only production API
  compatibility edits needed to run on 8.x. This is an experiment; the
  dependency and existing test assertions have not been migrated.
- **PyInstaller / Nuitka:** Standalone builds of the benchmark entry point,
  not the production `doxa` CLI. They include the fake engine from `tests`,
  so bundle sizes cannot be taken as a release artifact estimate. Nuitka's
  report confirms C compilation of DOXA and Textual modules; PyInstaller
  bundles CPython bytecode.
- **Ratatui:** `scripts/rust_ui_bench` includes the original narrow prototype
  and a [full-screen visual shell model](../scripts/rust_ui_bench/FULL_UI.md).
  The latter parses the accumulated Markdown source on each append and draws
  the visible DOXA layout into Ratatui's in-memory `TestBackend`. It still
  lacks DOXA's widget behavior, input dispatch, async message pump, full
  Markdown presentation, terminal I/O, and engine. Its measured parser and
  drawing costs are not speedup factors for a finished Rust frontend.

## Decision

Packaging is not the next performance fix. It makes cold launch slower in
this fixture, and neither package changes the order of magnitude of the
streaming cost. PyInstaller showed a small append improvement in one pass;
that is too small relative to the remaining latency and run variation to
justify a distribution change for speed. Nuitka costs several minutes to
build and did not improve the measured UI work.

The subsequent complete Textual 8 migration passed the local test suite, but
paired runs found only a small resize gain and slower append and scroll work.
There is no measured performance case for switching from Textual 5 today.

The existing versioned daemon socket provides a seam for a future Rust
frontend. The Ratatui prototype shows renderer capacity, but a real port
would also need DOXA's Markdown, tabs, input, accessibility, images, tool
chips, and error handling. The immediate work is to profile and reduce
Markdown streaming and make startup progress visible while sessions restore.
