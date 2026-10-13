# Automatic source index and latency measurement, 13 October 2026

One bounded, process-local index reuses parsed declarations and hashed parser
failure tombstones. Every answer still verifies the complete Git-listed
Rust/Python source inventory, modes and all source hashes in three read passes.
It stores no source bytes, ASTs, semantic bindings or LORE snapshots. Its
16 MiB estimated retained-allocation and 100,000-declaration ceilings preserve
the existing source, parser, output and subprocess limits.

## Controlled release-library comparison

Five sequential pairs alternate fresh/prepared order. Each pair uses four
novel query shapes: file, calls, imports and a different no-hit symbol. All
40 answers for each worktree have matching normalized answer hashes within
their pair, including coverage and available source digests. Only observed
timestamps are removed from the comparison. Preparation is outside the query
timer and measured separately. Filesystem cache state and other workstation
load are uncontrolled; no whole-system cold claim is made.

| Inventory | Fresh p50 / p95, ms | Prepared p50 / p95, ms | Retained estimate |
| --- | ---: | ---: | ---: |
| DOXA: 295 Rust + 54 Python sources | 360.012 / 391.296 | 74.280 / 85.214 | 2,606,339 B |
| Synthetic: 4,000 Rust + 4,000 Python sources | 624.188 / 682.092 | 599.224 / 637.613 | 8,248,230 B |

Each column has 20 samples. DOXA includes 84 Rust parse-failure tombstones:
211 Rust and 54 Python files parse, and the complete Rust digest remains absent.
The same explicit failure counts/reasons appear in fresh and prepared answers.
The 8,000-source fixture parses completely. Its read/hash cost dominates, so
preparation provides less benefit. This result does not justify removing any
freshness checks. The DOXA prepared-library p95 meets the 100 ms workload
target; it is not an end-to-end TUI target or universal guarantee.

The host is Linux 7.0.0-38-generic on a Ryzen 9 7950X. Builds use two workers
under a 12 GiB memory/swap-zero scope and run outside timed samples. Timings are
unaffinitized and desktop load is uncontrolled. DOXA measurements capture the
development checkout's source bytes at that run, rather than claiming all
later documentation/acceptance edits have identical inventory. The raw receipt
retains exact normalized answer hashes, source coverage and binary hashes.

## Source-page-cold CLI scans

Before each of five complete 8,000-source CLI scans, the harness fsyncs every
source, calls `POSIX_FADV_DONTNEED`, then independently checks `mincore`.
All 8,000 source pages are nonresident before every run. Source-inventory SHA
is `e82f266ff2f0c3c33ab2596b111e2b849676ecc05197322a5dea78e472d5d564`.
Median is **938.032 ms**, p95 **1,013.917 ms**. Each answer has complete
Rust/Python source digests and no-hit rows; source bytes remain identical after
all runs. Git metadata, directories, shared libraries and executable cache
state remain uncontrolled. This is source-page-cold CLI latency, including
process startup, not a full-system cold benchmark.

## Production viewer path

The ignored `measure_codegraph_viewer_latency` test opens the production modal,
polls its real worker to completion, formats the answer and renders a 96×32
ratatui TestBackend. It measures scheduling and render preparation, excluding
full application/session startup and physical terminal paint. Five novel file
queries follow actual preparation; no source edits occur during either run.

| Debug TUI build | Preparation, ms | Open/poll/render p50 / p95, ms |
| --- | ---: | ---: |
| DOXA | 2,098.427 | 547.980 / 560.643 |
| 8,000 sources | 1,289.009 | 854.775 / 885.342 |

These are debug-build user-facing path measurements. They must not be replaced
with the faster release-library numbers. Release TUI latency remains a separate
measurement; neither test measures human interaction or terminal emulator paint.

## Reproduce

Build outside the timed run from `rust/`, retaining binaries separately:

```sh
TMPDIR=/private/canonical/tmp CARGO_TARGET_DIR=/private/target CARGO_BUILD_JOBS=2 \
  cargo build --release --locked -p doxa-codegraph --example bench_index --bin doxa-codegraph
/private/target/release/examples/bench_index --root WORKTREE --file SOURCE
python3 scripts/bench_codegraph_cold.py --binary /private/target/release/doxa-codegraph \
  --root OWNED_FIXTURE --runs 5
```

The synthetic Git worktree has `rust/f0000.rs` through `f3999.rs`, each containing
`pub fn function_N() {}`, `pub fn caller_N() { function_N(); }` and
`use crate::function_N;`. Corresponding `python/fNNNN.py` contains `import os`
and `def function_N(): return os.getcwd()` on separate indented lines.
Initialize the worktree with `git init`; untracked nonignored sources suffice.
Use `rust/f0000.rs` for its file/calls/imports query. The receipt records the
original temporary fixture path, moved out of the checkout after measurement.

For the viewer from `rust/`, set `DOXA_CODEGRAPH_BENCH_ROOT=WORKTREE` and
`DOXA_CODEGRAPH_BENCH_FILE=SOURCE`, then run `cargo test -p doxa-tui
measure_codegraph_viewer_latency -- --ignored --nocapture --test-threads=1`.
Use a canonical private real-disk TMPDIR. The
[raw receipt](codegraph-index-benchmark-2026-10-13.json) contains all pairs,
source-cold samples and viewer rounds. These benchmarks start no model or
analyzer and write no LORE data.
