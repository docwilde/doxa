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

| Checkout at HEAD | Git-listed files | Parsed Rust / Python | Parse issues | p50 / p95 ms | Min / max ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| DOXA `5ee4d8e5` | 600 | 225 / 28 | 0 | 372.132 / 393.192 | 370.189 / 393.192 |
| ampiric-kg-extractor `0e5dff1b` | 6,944 | 0 / 787 | 60 Python | 1,641.409 / 2,042.788 | 1,628.911 / 2,042.788 |

The p95 uses nearest rank; with ten samples it is the maximum and is sensitive
to a single slow run. The larger checkout has 847 listed Python files, about
22.85 MB in total.
Sixty did not parse, so its timing is for the current best-effort syntax
coverage, not a complete graph. DOXA has about 5.19 MB of listed Rust source.
The CLI's existing hard limits still apply: 20,000 listed files, 1 MiB per
source, 64 MiB per language, and ten seconds for the Python scan. Rust has no
aggregate parser deadline, so the harness also enforces a process deadline.
One failing scan should be recorded as a limit, never used in a successful
latency percentile.

## Reproduce

Build once outside the timed runs. Use a real-disk `TMPDIR` and Cargo target;
the harness creates no temporary files or repository writes. Substitute paths
to the two Git checkouts:

```sh
TMPDIR=/home/docwilde/t CARGO_TARGET_DIR=/home/docwilde/ssd-cache/doxa-codegraph-bench-target \
  cargo build --release --locked -p doxa-codegraph
TMPDIR=/home/docwilde/t taskset -c 2 python3 scripts/bench_codegraph_scan.py \
  --binary /home/docwilde/ssd-cache/doxa-codegraph-bench-target/release/doxa-codegraph \
  --root /path/to/doxa --root /path/to/ampiric-kg-extractor \
  --runs 5 --warmups 1 --timeout-seconds 15 --budget-seconds 180
```

The script prints per-run coverage and timing as JSON, returns nonzero on a
failed or timed-out sample, kills the CLI and its Git child on timeout, and
caps the overall run. A 0.2-second fixture timeout was observed at 200.802 ms
with a nonzero exit and no remaining child.

## Index decision

Do not put the current full scan in every automatic turn. Even the warm DOXA
median is 372 ms per query; a sequence of three no-hit queries would spend
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
