# Code graph: native syntax-query slice

Status: **first read-only slice implemented**. DOXA can query current Rust
source for file-level definitions and imports. It does not yet build a resolved
dependency graph or write anything to LORE.

## Contents

- [Shipped surface](#shipped-surface)
- [Freshness and coverage](#freshness-and-coverage)
- [Architecture boundary](#architecture-boundary)
- [Still open](#still-open)

## Shipped surface

`doxa codegraph [--root WORKTREE] file PATH | symbol NAME | imports PATH`
returns bounded JSON. With no `--root`, it uses the current Git worktree.
`file` lists parsed definitions and import declarations in one Rust file;
`symbol` finds all exact name or qualified-name matches across that worktree;
`imports` lists the syntactic `use` and `extern crate` declarations in one
file. A second `doxa-codegraph` binary exposes the same queries for development.

The parser is [Syn's Rust source parser](https://docs.rs/syn/latest/syn/fn.parse_file.html).
It records top-level definitions, inline modules, trait methods, and methods
whose `impl` self type is a simple path. Imports are declarations, not resolved
file dependencies. The query deliberately does not infer calls, references,
macro expansion, conditional compilation, or function-local declarations.
A Python, TypeScript, or other recognized non-Rust source file is reported as
unsupported rather than silently treated as empty.

## Freshness and coverage

Every query enumerates the current tracked **and untracked, nonignored** Git
files and reads Rust bytes afresh. There is no index that can lag an edit.
Each row carries its file, line, SHA-256 of the parsed bytes, and read time.
The answer names its worktree and reports parsed files, unsupported languages,
syntax errors, skipped files, and unsupported syntax. Same-name definitions
remain separate candidates. A no-hit answer names a live-search fallback.

Work is bounded: at most 20,000 files, 1 MiB per Rust file, 64 MiB total Rust
source, 100 result rows, and a 64 KiB reply. The answer counts omitted rows.
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
- Resolve one-hop import dependencies and add references/calls only where the
  language semantics make the target decidable; report ambiguity explicitly.
- Add a reviewed agent tool and optional TUI tree/chip after the shared LORE
  operator exists. The CLI is the current operator surface.
- Decide whether other languages justify a parser dependency and coverage bar.
  Python support from the old plan has **not** shipped.
- Benchmark scan latency on large repositories before using this query in an
  automatic turn path or adding a persisted incremental index.
