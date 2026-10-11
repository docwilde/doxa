# Paired code graph revalidation measurement, 11 October 2026

The explicit TUI query cache retains eight exact answers within a 256 KiB
serialized budget. Every hit still enumerates Git paths and reads all listed
Rust and Python source bytes three times. The optimization changes Linux file
opening and the bounded Git child-exit polling interval; it removes no hash
passes, metadata checks, limits or deadlines. Module queries remain fresh.

Linux opens validated relative components with `openat2` beneath an opened
worktree descriptor and refuses symlinks throughout the path. Unsupported or
filtered syscalls and paths exceeding the kernel pathname limit fall back to
the existing descriptor walk. Other platforms keep that walk. The Git wait
polls every 1 ms instead of 10 ms; nonreaping process-group cleanup is unchanged.

## Measurement

Release benchmark binaries were built before timing from baseline `16273327`
and optimized checkpoint `d008e2a6`. Both read the **same frozen detached
checkout**, `f90bea6a`: 896 listed paths, 258 parsed Rust files and 40 parsed
Python files. Five pairs alternate before/after order; each invocation runs
ten sweeps of `file`, `calls` and `imports` for
`rust/doxa-codegraph/src/main.rs`. All ten invocations have identical complete
source inventories and normalized query answers. Later transcript, banner,
long-path compatibility and report commits are outside this source checkpoint.

Linux workstation, CPU 2 via `taskset`, 2 GiB process-scope memory cap,
swap disabled and a 60-second external deadline per invocation. No Cargo
build ran during measurement. Filesystem cache state and other desktop load
were uncontrolled. An empty answer cache is not a cold filesystem cache.

| Phase | Samples per build | Before p50 / p95 (ms) | After p50 / p95 (ms) |
| --- | ---: | ---: | ---: |
| Fresh query | 150 | 574.107 / 636.061 | 528.361 / 569.178 |
| Empty answer cache | 15 | 799.055 / 850.311 | 686.604 / 793.562 |
| Warmed answer cache | 150 | 104.659 / 116.954 | 69.975 / 91.929 |

Aggregate warmed p95 falls **116.954 → 91.929 ms**, meeting the 100 ms target
on this checkout. Per-pair optimized p95 values are 73.695, 77.981, 74.042,
93.014 and **101.120 ms**; the last pair misses the target. Maximum optimized
latency is 104.216 ms. This does not establish a uniform latency guarantee.
The [previous measurement](codegraph-query-cache-benchmark-2026-10-10.md)
used a different source inventory and smaller sample, so its 127.624 ms p95
is historical context rather than the paired baseline.

Samples time synchronous library work, including Git enumeration, reads,
hashing and answer assembly. They exclude process startup, benchmark JSON
verification, TUI worker scheduling, formatting and drawing. Repeated mutable
byte observations do not establish an atomic worktree snapshot or semantic
binding. Cold filesystem tails, larger repositories, actual query frequency
and end-to-end TUI latency remain open. No automatic index is enabled.

## Reproduce and evidence

Build each code checkpoint outside the timed run and retain its example binary:

```sh
TMPDIR=/home/docwilde/.dt CARGO_TARGET_DIR=/path/to/private/target \
  CARGO_BUILD_JOBS=2 cargo build --release --locked --offline \
  -p doxa-codegraph --example bench_query_cache
TMPDIR=/home/docwilde/.dt timeout 60s taskset -c 2 /path/to/retained/binary \
  --root /path/to/frozen/worktree --file rust/doxa-codegraph/src/main.rs --runs 10
```

Run five sequential pairs, reversing order each pair, with no source edits or
builds during timing. The [raw receipt](codegraph-query-cache-benchmark-2026-10-11.json)
contains all 630 samples, per-run and aggregate summaries, source and answer
hashes, binary hashes, code checkpoints and controls. The benchmark reads
source only; it starts no model, analyzer or Engine and writes no LORE data.
