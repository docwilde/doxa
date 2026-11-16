# Release TUI code-graph query latency, 14 November 2026

This measures what a user waits for after pressing Enter on a `/codegraph`
command in a release build of the production TUI event loop.

**Whether the 100 ms p95 target for prepared DOXA queries is met depended on
the laptop's power profile.** Under the `performance` platform profile at
1-minute load ≤ 4.4, unchanged `c49cfaee` code met it: prepared DOXA p95 was
62.8 ms with a warm page cache (n = 320) and 53.5 ms after source-page
eviction (n = 160). Under the `quiet` profile, with other agents' builds
running at load 2–15, it was not met: p95 was 229.0 and 217.9 ms. The
same-profile release library baseline was also above the target (indexed p95
224.9 ms), so that shortfall comes from host speed and the library's
per-answer freshness work, not from TUI overhead. One small TUI-side defect
was found and fixed: [answers waited through the idle input poll](#fix-answers-no-longer-wait-for-the-idle-input-poll).
No timing result here is a universal latency guarantee.

## Timed span

The new harness, `scripts/bench_codegraph_tui.py`, starts the release
`bench_pty_frames` binary in a 160×48 PTY. That binary runs
`doxa_tui::ui::run_with_frames`, the production Crossterm loop also behind
`doxa-rs --demo`. Fixture `hello` frames on a Unix socket attach one local
session whose cwd is the measured worktree. Real `doxa-rs` sessions use the
typed worker frame source, but both enter the same `run_loop_with_remote`
body. Key handling, `/codegraph` dispatch, the query worker, polling and
drawing are the same code.

- **Start:** the harness writes Enter (`\r`) to the PTY master. The
  complete `/codegraph QUERY` text is already echoed in the prompt.
- **End:** the PTY master read after which a replayed VT screen shows the
  answer's first rows (`QUERY · status` and `Worktree:`) in the production
  modal. At this size the modal shows three rows; the rest of the answer is
  already in memory and needs only scrolling. Every answer frame arrived in a
  single read, so "visible" and "frame last byte" are identical in every
  sample.
- **Included:** PTY delivery, Crossterm input decoding, key handling, command
  dispatch, worker thread spawn and cache lock. In `attach` mode this includes
  waiting for automatic preparation. It also includes Git enumeration, source
  reads, hashes and parses, answer formatting, the loop's input poll, Ratatui
  layout, diffing, escape encoding and the PTY write.
- **Excluded:** terminal emulator parsing, paint, compositor and display;
  human typing; agent daemon or provider startup; real daemon traffic and
  concurrent agent output; LORE.

Each TUI process measures one cold query, then waits for a 300 ms window with
at most one CPU tick, so automatic preparation has finished. It then measures
eight novel prepared queries and one repeat. Cold modes alternate by process:

- **switch:** the session first attaches in a non-Git directory, the command
  is typed, and the same session's cwd then changes to the worktree. The
  in-process index is empty. The production 30-second root-change throttle
  keeps preparation from starting, so this is a fresh complete scan.
- **attach:** the session attaches in the worktree, and automatic preparation
  starts at once. The command is typed and submitted immediately; Enter
  followed attach after a p50 of 14–85 ms, depending on the series. The query
  waits on the cache lock for preparation, then runs a complete indexed query.

Origin labels were read after each span, using an untimed PageDown. Every
cold switch answer was fresh and every cold attach answer was indexed. All
prepared answers were indexed. Repeats were indexed on DOXA and revalidated
exact answers on both fixtures; DOXA's parse failures prevent exact-answer
reuse.

## Machine, build and power state

The host is an Intel Core Ultra 9 285H, with 16 logical CPUs (6 P, 8 E and
2 LP-E cores, one thread each). It runs Linux 7.2.8-9-omarchy with the
`intel_pstate` `powersave` governor. It has 30 GiB RAM and btrfs on dm-crypt
on NVMe. The toolchain is rustc 1.99.1, git 2.56.0 and Python 3.14.8.

All binaries were built with `cargo build --release --locked`, using the
default release profile that `scripts/install.sh` also uses. The base is
commit `c49cfaee` (2.0.0-beta.78), and its `bench_pty_frames` SHA-256 is
`27d617fabf0aaa27bed47f35aa2604a16cab52407d3dfa71146cdbb4da91bd78`.
Processes were unpinned unless stated otherwise.

The ACPI platform profile read `quiet` at 21:12, before the first pass. It
read `performance` (energy preference `performance`) at 21:55. The switch
was not made by this work. It is visible inside a P-core-pinned DOXA run:
prepared medians were about 116 ms in processes 1–7 and about 42 ms in
processes 8–20, at the same load. The first-pass series (21:26–21:53) show
no comparable step, so they are reported as the `quiet` pass. The harness has
since been extended to record the profile and energy preference before and
after every process, and every later process read `performance/performance`.

## Workloads

- **DOXA:** `git archive c49cfaee` extracted to a private real-disk
  directory, then `git init` and `git add -A`. Its inventory has 1,336 listed
  files, including 339 Rust and 95 Python sources (9.65 MB). The answer
  reports 238 parsed Rust files, 95 parsed Python files and 101 Rust
  parse-failure tombstones. The cold query is `file
  rust/doxa-tui/src/ui/codegraph_viewer.rs`. Prepared queries are
  `file|calls|imports rust/doxa-codegraph/src/main.rs`, `symbol query`,
  `file|calls|imports scripts/bench_codegraph_cold.py` and a no-hit symbol.
  The repeat query is the first prepared query again.
- **Small fixture:** 100 Rust and 100 Python untracked sources, using the
  [13 October recipe](codegraph-index-benchmark-2026-10-13.md#reproduce). The
  cold query uses `rust/f0001.rs`, and prepared queries use `rust/f0000.rs`,
  `python/f0000.py` and `symbol function_7`.
- **8,000-source fixture:** the same recipe with 4,000 + 4,000 sources and
  the same queries.

"Evicted" series fsync each source, call `POSIX_FADV_DONTNEED` and confirm
with mincore that zero source pages are resident. The check runs before
each process, and again just before the worktree becomes the session cwd.
Git metadata, directories and the binary remain cached.

## Results

These are answer-visible latencies in milliseconds for the unchanged
`c49cfaee` loop. p95 uses the nearest rank, so for n ≤ 20 it is the 19th of
20 values or the maximum.

### `performance` profile, load 1.2–4.4

| Workload / series | n | p50 | p95 | max |
| --- | ---: | ---: | ---: | ---: |
| **DOXA warm, prepared novel** | 320 | **52.5** | **62.8** | 84.3 |
| DOXA warm, repeat | 40 | 52.2 | 53.3 | 92.2 |
| DOXA warm, cold switch / attach | 20 / 20 | 492.2 / 502.5 | 572.6 / 577.7 | 605.9 / 581.0 |
| **DOXA evicted, prepared novel** | 160 | **52.3** | **53.5** | 63.5 |
| DOXA evicted, cold switch / attach | 10 / 10 | 574.3 / 578.1 | 600.7 / 607.4 | 600.7 / 607.4 |
| Small warm, prepared novel | 160 | 21.5 | 22.0 | 23.0 |
| Small warm, repeat (revalidated) | 20 | 11.6 | 22.0 | 22.0 |
| Small warm, cold switch / attach | 10 / 10 | 22.1 / 21.4 | 22.3 / 21.7 | 22.3 / 21.7 |
| 8,000 warm, prepared novel | 80 | 255.0 | 276.1 | 295.1 |
| 8,000 warm, cold switch / attach | 5 / 5 | 296.1 / 506.4 | 336.3 / 536.7 | 336.3 / 536.7 |

The DOXA, small and 8,000-source warm runs used 40, 20 and 10 processes; the
DOXA evicted run used 20.

### `quiet` profile with concurrent builds, load 2.2–14.5

| Workload / series | n | p50 | p95 | max |
| --- | ---: | ---: | ---: | ---: |
| **DOXA warm, prepared novel** | 320 | **168.7** | **229.0** | 319.6 |
| DOXA warm, repeat | 40 | 163.9 | 210.1 | 228.6 |
| DOXA warm, cold switch / attach | 20 / 20 | 1,420.1 / 1,480.3 | 1,555.1 / 1,650.2 | 1,652.0 / 1,695.6 |
| **DOXA evicted, prepared novel** | 160 | **167.3** | **217.9** | 341.3 |
| DOXA evicted, cold switch / attach | 10 / 10 | 1,705.1 / 1,797.0 | 1,797.5 / 1,849.1 | 1,797.5 / 1,849.1 |
| Small warm, prepared novel | 320 | 44.7 | 55.9 | 77.7 |
| Small warm, cold switch / attach | 20 / 20 | 48.5 / 44.9 | 60.6 / 63.9 | 61.3 / 76.9 |
| Small evicted, prepared novel | 160 | 45.0 | 54.7 | 56.4 |
| 8,000 warm, prepared novel | 160 | 970.2 | 1,133.3 | 1,274.3 |
| 8,000 warm, cold switch / attach | 10 / 10 | 1,408.4 / 2,209.3 | 2,120.2 / 2,470.6 | 2,120.2 / 2,470.6 |
| 8,000 evicted, prepared novel | 80 | 797.9 | 961.5 | 1,066.5 |
| 8,000 evicted, cold switch / attach | 5 / 5 | 1,155.4 / 1,830.8 | 1,294.9 / 2,113.6 | 1,294.9 / 2,113.6 |

The DOXA, small and 8,000-source runs used 40, 40 and 20 warm processes and
20, 20 and 10 evicted processes. The 1-minute load ranges were: DOXA warm
2.22–11.71, small 6.05–8.82, 8,000-source 8.06–14.47, and 3.67–5.45 for the
evicted series. Load still mattered inside a pass. Within the quiet DOXA warm
run, prepared p50 was 157.8 ms for processes started below load 6, and
177.8 ms above it.

The UI acknowledges a command almost at once. The first output after Enter
(the "Scanning current Git worktree…" modal) had p50 1.1–1.5 ms and max
3.6 ms in the performance pass. In the quiet pass it had p50 3.3 ms and max
16.0 ms. Typing and drawing stay responsive while a scan runs.

## Same-machine library baseline

`bench_index` ran five alternating fresh/indexed pairs per workload. Answer
hashes were identical within each pair, and timing excludes preparation:

| Library, release, p50 / p95 ms | Fresh | Indexed | Preparation |
| --- | ---: | ---: | ---: |
| `performance`: DOXA | 469.3 / 529.0 | 44.5 / 49.6 | 451–470 |
| `performance`: small | 11.2 / 13.4 | 11.9 / 12.5 | 11–16 |
| `performance`: 8,000 | 291.5 / 318.8 | 240.5 / 277.4 | 290–325 |
| `quiet`: DOXA | 1,419.7 / 1,497.7 | 171.6 / 224.9 | 1,235–1,466 |
| `quiet`: small | 38.2 / 48.4 | 35.4 / 42.6 | 40–56 |
| `quiet`: 8,000 | 965.4 / 1,047.4 | 753.9 / 833.0 | 902–1,154 |

In the performance pass, the TUI added about 8 ms to DOXA's indexed p50 and
about 10 ms on the small fixture: exactly the loop's input-poll quantum. The
13 October library numbers came from a Ryzen 9 7950X with a smaller DOXA
inventory and are not comparable across hosts.

## Fix: answers no longer wait for the idle input poll

The loop polls terminal input for up to 10 ms per turn, and checks the
code-graph worker only after that poll returns. A finished answer therefore
waited up to 10 ms. Small-fixture latencies clustered at 10.5 ms intervals
(about 34, 45, 55 and 66 ms in the quiet pass). The loop already paints daemon
frames before waiting for input, so they never wait through an idle poll.
`App::input_poll_timeout` now returns 1 ms while an explicit code-graph query
is pending, and 10 ms otherwise. The faster wakeups last only as long as one
query. A unit test covers the switch back to 10 ms once the answer arrives.

Before and after binaries alternated as A B A B, with ten processes per run,
under the `performance` profile at load 0.71–1.61:

| Prepared novel, n = 160 each | p50 | p95 | max |
| --- | ---: | ---: | ---: |
| Small, before → after | 21.6 → 12.9 | 22.0 → 15.6 | 32.0 → 20.6 |
| DOXA, before → after | 42.7 → 42.6 | 52.9 → 46.0 | 62.8 → 50.3 |

Small cold switch p50 fell from 21.8 to 15.0 ms. DOXA cold queries were
unchanged within noise (switch p50 476.8 → 463.3 ms). The DOXA median did not
move, because its worker time sat just before a poll boundary; its tail did
move. The fixed binary's SHA-256 is
`0f459b360a000aee01470f2fac2793c1381d7c16b03273c84d442cbc5d4de108`. The
profile tables above use the unchanged binary.

## Findings not changed here

- **Per-query fixed floor.** Even 200 tiny sources take about 12 ms
  (performance) or 35 ms (quiet) in the library. Each indexed answer runs
  `git rev-parse` twice (once in `cache::query` and again in
  `query_with_index_and_hooks`) and `git ls-files` twice. It also makes three
  single-file round trips per source through the source-reader thread. The
  8,000-source fixture holds about 0.6 MB, so per-file cost dominates there.
  The split between Git, thread hand-offs and hashing was not profiled.
- **Cold start waits for preparation.** A query submitted during automatic
  preparation waits for it, then runs a complete indexed query. On the
  8,000-source fixture, cold attach was about 0.2–0.8 s slower than a fresh
  scan. On DOXA the gap was within about 100 ms.
- **Three-row modal.** The code-graph modal has fixed height 5, so coverage
  and freshness labels need scrolling. This is presentation, not latency.

## Reproduce

Build outside the timed run, then run each series. Use a private real-disk
`TMPDIR`:

```sh
cargo build --release --locked -p doxa-tui --bin bench_pty_frames \
  -p doxa-codegraph --example bench_index
git archive c49cfaee | tar -x -C PRIVATE/doxa
git -C PRIVATE/doxa init -q && git -C PRIVATE/doxa add -A
python3 scripts/bench_codegraph_tui.py --root PRIVATE/doxa \
  --file rust/doxa-codegraph/src/main.rs --python-file scripts/bench_codegraph_cold.py \
  --cold-file rust/doxa-tui/src/ui/codegraph_viewer.rs --symbol query --runs 40
python3 scripts/bench_codegraph_tui.py --root FIXTURE --file rust/f0000.rs \
  --python-file python/f0000.py --cold-file rust/f0001.rs --symbol function_7 --runs 20
target/release/examples/bench_index --root PRIVATE/doxa --file rust/doxa-codegraph/src/main.rs
```

Add `--evict-sources` for evicted series, using owned sources only, and
`--binary` to compare builds. Record `/sys/firmware/acpi/platform_profile`
alongside any comparison. The
[raw receipt](codegraph-tui-release-latency-2026-11-14.json) keeps every
sample with its query, origin label and first-output time. It also keeps
per-process load, power state, residency and the attach-to-Enter time. Run
the harness checks with `python3 -m unittest
scripts.tests.test_bench_codegraph_tui`. The harness starts no model, network
request or LORE write.
