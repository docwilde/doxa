# Code graph scan latency, 9 October 2026

The production `doxa-codegraph` CLI scans the Git-listed worktree on every
query. This benchmark times a no-hit `symbol` query: it must enumerate and
parse all supported Rust and Python files, but returns a small answer. It
does not exercise semantic binding, stored snapshots, or an automatic turn
path.

## Results

Release binary built with `cargo build --release --locked -p doxa-codegraph`
from `d6a44c87`. Ryzen 9 7950X, Linux 7.0.0-34 x86-64, CPU 2 via
`taskset`, warm filesystem cache, two batches of one warm-up and five timed
fresh processes per checkout (ten samples total). Times include process
startup, Git enumeration, reads, parsing, and JSON output. Other desktop
activity was not controlled.

| Current worktree (HEAD baseline) | Git-listed files | Parsed Rust / Python | Parse issues | p50 / p95 ms | Min / max ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| DOXA `5ee4d8e5`, dirty | 600 | 225 / 28 | 0 | 371.187 / 373.842 | 368.723 / 373.842 |
| ampiric-kg-extractor `0e5dff1b`, dirty | 6,944 | 0 / 787 | 60 Python | 1,641.365 / 1,686.109 | 1,638.716 / 1,686.109 |

Timed samples in batch order, milliseconds: DOXA 373.842, 370.767, 369.339,
370.516, 373.764, 371.606, 373.809, 368.723, 370.664, 372.955;
ampiric-kg-extractor 1,641.692, 1,642.347, 1,664.800, 1,641.037,
1,639.633, 1,674.241, 1,638.758, 1,640.257, 1,686.109, 1,638.716.

The p95 uses nearest rank; with ten samples it is the maximum and is sensitive
to a single slow run. The larger checkout has 847 listed Python files, about
22.85 MB in total.
DOXA had three untracked Markdown files; the larger checkout had one untracked
`NOTES.md`. The harness recorded bounded Git status digests before and after
each batch; each checkout's status was unchanged within both batches. HEAD
therefore identifies the baseline, not the exact bytes scanned. The untracked
files are included in Git enumeration, though these four are not Rust/Python.
Sixty did not parse, so its timing is for the current best-effort syntax
coverage, not a complete graph. DOXA has about 5.19 MB of listed Rust source.
The CLI's existing hard limits still apply: 20,000 listed files, 1 MiB per
source, 64 MiB per language, and ten seconds for the Python scan. Rust has no
aggregate parser deadline, so the harness also enforces a process deadline.
One failing scan should be recorded as a limit, never used in a successful
latency percentile.

## Reproduce

Build once outside the timed runs. Use a trusted production binary and a
real-disk `TMPDIR` and Cargo target. The harness itself creates no temporary
files or repository writes; it disables Git fsmonitor hooks and optional index
locks in its children. Substitute paths to the two Git checkouts:

```sh
TMPDIR=/home/docwilde/t CARGO_TARGET_DIR=/home/docwilde/ssd-cache/doxa-codegraph-bench-target \
  cargo build --release --locked -p doxa-codegraph
for batch in 1 2; do
  TMPDIR=/home/docwilde/t taskset -c 2 python3 scripts/bench_codegraph_scan.py \
    --binary /home/docwilde/ssd-cache/doxa-codegraph-bench-target/release/doxa-codegraph \
    --root /path/to/doxa --root /path/to/ampiric-kg-extractor \
    --runs 5 --warmups 1 --timeout-seconds 15 --budget-seconds 180 \
    > "/home/docwilde/t/codegraph-scan-$batch.json"
done
```

Pool each repository's ten `samples[*].elapsed_ms` values; p50 is the median
and p95 is the nearest-rank value. The script prints per-run coverage, status
evidence, and timing as JSON. It returns nonzero on a failed, hit, overflowed,
or timed-out sample; it caps both output pipes while reading and kills the
process group on failure. The overall budget includes Git metadata. A
0.2-second fixture timeout exited nonzero with no remaining child.

## Index decision

Do not put the current full scan in every automatic turn. Even the warm DOXA
median is 371 ms per query; a sequence of three no-hit queries would spend
roughly 1.1 seconds on repeated scans before agent or provider work. The
larger checkout takes about 1.6–2.0 seconds per query. These are local warm
cache observations, not cold-cache or fleet latency guarantees.

An incremental index is justified only for a specified repeated-query path
whose end-to-end latency budget this scan misses on representative repos.
Before shipping one, measure the query frequency and cold-cache tail,
define worktree-scoped invalidation for tracked and untracked nonignored
files, and verify changed bytes against source hashes at answer time. Keep
explicit queries and fail-closed scan limits until then; an index must not
turn syntax candidates into semantic bindings.
