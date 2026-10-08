# Code graph: native syntax-query slice

Status: **read-only syntax queries implemented**. DOXA can query current Rust
source for definitions, imports, conservative call-site candidates, and bounded
top-level module-file layout. It does
not build a resolved dependency graph or write anything to LORE.

## Contents

- [Shipped surface](#shipped-surface)
- [Freshness and coverage](#freshness-and-coverage)
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

## Architecture boundary

The older proposal described Python `ast`, `doxa/operators.py`, and tables in a
LORE SQLite store. DOXA now uses native Rust hosts and a pinned external
`lore-core` crate. Its agent tool catalog is explicitly validated in
`rust/doxa-lore/src/lib.rs`; DOXA cannot silently add a LORE operator or table.
This slice lives in `rust/doxa-codegraph` and is callable through the installed
`doxa` launcher. It is read-only and creates no second memory authority.

## Still open

- Coordinate a LORE-owned persisted graph and curated `purpose` file-map field
  with LORE's write gate, including worktree lifecycle and freshness checks.
- Resolve imports and actual Rust call bindings with crate, trait, type, and
  conditional-compilation context. Module edges remain top-level and structural;
  `cfg_attr` and conditional reachability are unresolved. Call candidates stop
  at spelling matches.
- Add a reviewed agent tool and optional persistent TUI tree/chip after the
  shared LORE operator exists. The current TUI viewer is a direct syntax query.
- Decide whether other languages justify a parser dependency and coverage bar.
  Python support from the old plan has **not** shipped.
- Benchmark scan latency on large repositories before using this query in an
  automatic turn path or adding a persisted incremental index.
