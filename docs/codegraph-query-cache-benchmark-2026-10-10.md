# Alternating explicit code graph queries, 10 October 2026

The native TUI's process-local answer cache now retains up to eight exact
`file`, `calls`, `imports`, or `symbol` answers from one canonical worktree
and one source inventory. The combined serialized-answer limit is 256 KiB;
Rust allocation overhead and bounded query/root metadata are additional.
Least-recently-used entries are evicted when either limit is reached.
Every hit still enumerates Git paths and reads all listed Rust and Python
sources three times before reusing syntax. A source/listing mismatch, failed
check, root change, incomplete scan, or query failure clears the generation.
Module queries remain fresh and clear the cache.

The previous one-entry cache reparsed the repository for every step of an
alternating `file` → `calls` → `imports` → `file` sequence. This slice reuses
the earlier exact answer after complete revalidation. It creates no durable
index, automatic agent-turn query, or semantic binding claim.

## Measurement

Release example built with `cargo build --release --locked --offline -p
doxa-codegraph --example bench_query_cache` from code checkpoint `f0b69705`,
based on main `03cb449c` / 2.0.0-beta.42. Linux workstation, CPU 2 via
`taskset`. The worktree had documentation edits and private scratch files;
HEAD names the code baseline rather than all enumerated bytes. The workload
alternated `file`, `calls`, and `imports` for
`rust/doxa-codegraph/src/main.rs`. There were 863 enumerated Git paths,
243 parsed Rust files and 35 parsed Python files, with complete source
digests in every answer. Other desktop load and filesystem cache state were
uncontrolled.

| Phase | Samples | p50 / p95 ms | Min / max ms |
| --- | ---: | ---: | ---: |
| Fresh library query, five alternating sweeps | 15 | 566.314 / 828.561 | 531.284 / 828.561 |
| Empty answer cache, first alternating sweep | 3 | 786.717 / 856.831 | 708.264 / 856.831 |
| Primed answer cache, five alternating sweeps | 15 | 114.945 / 127.624 | 105.895 / 127.624 |

**The warmed-answer p95 target of 100 ms is not met.** At median the primed
cache saves about 80% compared with fresh scans on this workload. An empty
cache adds admission and source-revalidation work to the first queries, so
its first sweep is slower than uncached fresh queries. The sample count is
small: nearest-rank p95 is the maximum for these phases.

The empty-cache phase means no answers retained in this process. It is
**not a cold filesystem-cache measurement**: fresh scans precede it. Samples
time the synchronous library query, including Git enumeration, source reads,
parsing or revalidation, and answer assembly. They exclude JSON benchmark
verification, process startup, TUI worker scheduling, formatting and drawing.
Cold filesystem tails and representative larger checkouts remain open.

The helper rejects incomplete source inventories and verifies each query's
entire normalized answer against its fresh baseline, removing only read and
observation timestamps. Rust/Python input digests and requested-file hash
must be identical throughout. These are repeated byte observations, not an
atomic snapshot, proof of compiler context, or analyzer/container identity.

The [raw receipt](codegraph-query-cache-benchmark-2026-10-10.json) includes
all samples, exact source digests, answer digests, cache origins and retained
observation times. The release example's SHA-256 was
`33072a49dbd2203fc2077eae20dba8003f98355657ccb26ffeaab0da4b7982ed`.

## Reproduce

Build outside the timed run with a private real-disk temporary directory and
worktree-specific Cargo target. From the worktree:

```sh
TMPDIR=/home/docwilde/.cg-t CARGO_TARGET_DIR=.target CARGO_BUILD_JOBS=3 \
  cargo build --release --locked --offline -p doxa-codegraph --example bench_query_cache
TMPDIR=/home/docwilde/.cg-t timeout 60s taskset -c 2 \
  .target/release/examples/bench_query_cache \
  --root . --file rust/doxa-codegraph/src/main.rs --runs 5
```

The example reads source only; it never writes snapshots, opens an Engine,
launches an analyzer, or calls a model. It allows 1–10 sweeps, rejects changed
normalized answers or unexpected cache origins, and checks a two-minute
budget between queries. The external 60-second process deadline bounds this
invocation as well. Core scan/read/Git limits remain in force.

## Validation and remaining work

The full `doxa-codegraph` suite passed: **98 tests, zero failures, three
existing guest-only broker fixtures ignored**. Eight cache tests cover exact
alternating-answer reuse, count and byte eviction, least-recently-used
promotion, source edits, ignored-to-tracked inventory changes, removal and
failed queries, root changes, incomplete scans, late edits, and fresh module
queries when ignored candidates appear.

Measure colder reads, larger complete inventories and actual query frequency
before considering persistent parsed-source indexing or automatic queries.
Semantic binding remains `unknown`: production analysis still needs the
[reviewed host broker, immutable mounted source snapshot and live Engine
origin/containment evidence](plans/codegraph-semantic-verification.md).
