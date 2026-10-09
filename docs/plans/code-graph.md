# Code graph: native syntax-query slice

Status: **read-only syntax queries, explicit file-map export, and reviewed LORE
snapshot reads implemented**. DOXA can query current Rust source for definitions, imports,
conservative call-site candidates, and bounded top-level module-file layout,
including literal paths and unverified conditional candidates.
LORE 0.62.20 owns durable source-hashed snapshots; DOXA does not write them.

## Contents

- [Shipped surface](#shipped-surface)
- [Freshness and coverage](#freshness-and-coverage)
- [Explicit snapshot export](#explicit-snapshot-export)
- [Reviewed snapshot read](#reviewed-snapshot-read)
- [Architecture boundary](#architecture-boundary)
- [Still open](#still-open)

## Shipped surface

`doxa codegraph [--root WORKTREE] file PATH | symbol NAME | imports PATH | calls PATH | modules PATH`
returns bounded JSON. With no `--root`, it uses the current Git worktree.
`file` lists parsed definitions and import declarations in one Rust file;
`symbol` finds all exact name or qualified-name matches across that worktree;
`imports` lists the syntactic `use` and `extern crate` declarations in one
file. `modules` lists top-level `mod child;` declarations and resolves a unique
listed, readable, parseable `child.rs` or `child/mod.rs` with source and target
hashes. A literal `#[path = "relative/file.rs"]` can resolve a top-level module
against the containing source file's directory; only relative, traversal-free
Rust paths enter this query. A `#[cfg(...)]` declaration can report a unique
hashed **conditional candidate**, while `target` and `resolution` stay unknown.
The query never evaluates the cfg predicate. This follows the
[Rust module path rules](https://doc.rust-lang.org/reference/items/modules.html)
and [conditional attribute rules](https://doc.rust-lang.org/reference/conditional-compilation.html).
Plain modules use the containing directory for `lib.rs`, `main.rs`, `mod.rs`,
`build.rs`, and common Cargo roots under `src/bin`, `tests`, `examples`, and
`benches`; another `parent.rs` uses `parent/child.rs` or `parent/child/mod.rs`.
Resolved targets say `structural_only`: they do not establish compilation
reachability. `cfg_attr`, nonliteral or unsafe paths, other attributes, duplicate
declarations, both layout candidates, symlinks, ignored/generated files on disk,
missing targets, and unparseable targets yield `unknown` with a reason. Inline
modules are reported as skipped; nested modules are counted and not resolved.
`calls` lists direct function-path and method-call expressions within
file-level functions, trait defaults, and simple `impl` methods. A second
`doxa-codegraph` binary exposes the same queries for development.
Inside the local TUI, `/codegraph file|symbol|imports|calls|modules VALUE` opens
a scrollable, read-only answer for the active session's Git worktree. Its query
runs outside the input loop; the viewer shows source hashes, scan coverage,
omitted counts, and explicit `ambiguous`, `unresolved`, or `unknown` labels.
The answer is a snapshot of bytes read during that query, not a live binding
or a persisted graph.

`/codegraph stored file|imports|calls|modules PATH` reads a previously
reviewed LORE snapshot for the active session's exact worktree. It runs outside
the input/render loop and displays revision, hashes, freshness limit, and the
original graph JSON, including ambiguous candidates. A missing snapshot is
shown explicitly; stale or malformed data fails closed.

The parser is [Syn's Rust source parser](https://docs.rs/syn/latest/syn/fn.parse_file.html).
It records top-level definitions, inline modules, trait methods, and methods
whose `impl` self type is a simple path. Imports are declarations, not resolved
file dependencies. A call edge records its lexical caller and spelled target.
Direct paths carry up to eight function or method definitions whose final name
segment matches; `candidate_only` means one such declaration was observed,
**not** that Rust would bind the call to it. Multiple matches are `ambiguous`;
no match and receiver-method calls are `unresolved`. The answer counts omitted
candidates. Calls through variables, closures, macros, function-local items,
and qualified-self paths are not resolved. Neither macro expansion nor
conditional compilation is evaluated.
A Python, TypeScript, or other recognized non-Rust source file is reported as
unsupported rather than silently treated as empty.

## Freshness and coverage

Every query enumerates the current tracked **and untracked, nonignored** Git
files and reads Rust bytes afresh. There is no index that can lag an edit.
Each row and call edge carries its file, line, SHA-256 of the parsed bytes, and
read time. Candidate declarations carry their own source hashes and read times.
When **every listed Rust file parses**, the answer also contains a deterministic
`scan_input_sha256` over each Rust path and source hash. A skipped or
unparseable Rust file leaves it null. This inventory covers listed Rust inputs,
not ignored files, other languages, macro expansion, or compiler semantics.
File-scoped answers also carry the requested source's hash and read time, even
when that parsed file produces no rows.
The answer names its worktree and reports parsed files, unsupported languages,
syntax errors, skipped files, and unsupported syntax. Same-name definitions
remain separate candidates. A no-hit answer names a live-search fallback.

Work is bounded: at most 20,000 files, 1 MiB per Rust file, 64 MiB total Rust
source, 100 result rows, call edges, or module declarations, 10,000 call sites in the requested file,
100,000 candidate declarations, and a 64 KiB reply. The answer counts omitted
rows, edges, and per-edge candidates.
When enumeration or the total scan budget fails, the command returns an error
instead of a partial result. Oversized, changed-during-read, symlinked, and
unparseable files are named in bounded issue summaries. A file hash is evidence
for the bytes parsed, not a promise that the worktree has stayed unchanged since
that read.

## Explicit snapshot export

`doxa codegraph --lore-map [--root WORKTREE] file|imports|calls|modules PATH`
reads the same project's existing LORE file map and prints a bounded JSON
snapshot to stdout. It is an explicit command, never a TUI background write.
The LORE response is validated for shape, size, and project scope before use.
The original syntax answer, including source hashes, coverage, and ambiguous
module or call candidates, is preserved. `query_sha256` identifies the exact
serialized answer; `storage` says `export_only_not_persisted`.

The overlay matches only an exact file path. It retains all competing curated
purpose entries and labels them `ambiguous`; a single entry is
`curated_unverified`, and no entry is `unknown`. All graph bindings remain
`unknown`. The file map has no source hash or revision, so its purpose's
freshness is explicitly unverified and it cannot prove a binding to the
current source. A missing capability, malformed response, project mismatch,
unparseable source, or oversized export fails closed.

## Reviewed snapshot read

`doxa codegraph --stored [--root WORKTREE] file|imports|calls|modules PATH`
returns a validated LORE `current` snapshot or `{"status":"missing"}`. DOXA
pins `lore-core` 0.62.20 and calls only `codegraph_snapshot_read_v1`; it checks
the exact project, canonical worktree, query, path, revision, source hash,
graph digest, and `unknown` binding claim. LORE checks Git worktree identity
and rehashes the requested file on each read. A source edit or checkout
replacement rejects the read. DOXA also rechecks the hashes of included row,
call-candidate, and module-target files through a bounded, symlink-safe reader.
The result reports `verified`, `stale`, or `unknown` for those included
references, with bounded reasons. When the graph has a complete scan-input
digest, DOXA also re-enumerates and rehashes every listed Rust input at read
time. An edit, addition, or removal makes `scan_inputs` stale; an unreadable
or symlinked source makes it unknown. Older snapshots without the digest stay
unknown. This check is a read-time observation, not an atomic repository
snapshot. Omitted result rows and semantic Rust bindings remain outside its
claim.

To persist an export, an owner must inspect it and invoke LORE's explicit
`codegraph_snapshot_store_v1` command with human-review authority and an
expected revision. DOXA's CLI and TUI expose no store operation. The export's
producer `query_sha256` is not used as a store integrity claim: LORE computes
`graph_sha256` over the stored graph JSON.

The owner command is `lore-rs codegraph store --cwd ABSOLUTE_WORKTREE --input
EXPORT.json --expected-sha256 FILE_SHA256 --expected-revision N`. It requires
an interactive terminal, confirms the reviewed file digest, and asks for
`STORE`. Use `N=0` for a new snapshot; use the current revision for replacement.

## Architecture boundary

The older proposal described Python `ast`, `doxa/operators.py`, and tables in a
LORE SQLite store. DOXA now uses native Rust hosts and a pinned external
`lore-core` crate. Its agent tool catalog is explicitly validated in
`rust/doxa-lore/src/lib.rs`; DOXA cannot silently add a LORE operator or table.
The pinned LORE release offers the read-only `filemap` and reviewed codegraph
snapshot operators. This slice lives in
`rust/doxa-codegraph`, `rust/doxa-lore`, and the installed `doxa` launcher. It
is read-only and creates no second memory authority.

## Still open

- Decide whether reviewed snapshots need an operator index, retention policy,
  and explicit invalidation across worktree lifecycle. The current read is
  exact worktree/query/path; it verifies the requested source and reports
  freshness only for included references.
- Resolve imports and actual Rust call bindings with crate, trait, type, and
  conditional-compilation context. Module edges remain top-level and structural;
  `cfg_attr` and conditional reachability are unresolved. Call candidates stop
  at spelling matches. An optional rust-analyzer evidence overlay needs a
  quota-limited, no-egress container, disabled build scripts and proc macros,
  pinned toolchain/configuration, and source/target hash rechecks before it can
  claim analyzer-resolved definitions. It is not shipped yet.
- Decide whether a reviewed agent tool or persistent TUI tree is useful. The
  current viewer offers explicit fresh and stored queries only.
- Decide whether other languages justify a parser dependency and coverage bar.
  Python support from the old plan has **not** shipped.
- Benchmark scan latency on large repositories before using this query in an
  automatic turn path or adding a persisted incremental index.
