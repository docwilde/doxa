# Code graph: native syntax-query slice

Status: **read-only syntax queries, bounded automatic preparation, explicit
file-map export and reviewed LORE snapshot reads implemented**. An explicit
Linux semantic query can use an externally provisioned, pinned attested broker;
ordinary syntax answers keep their original binding claims. DOXA can query current Rust source for definitions, imports,
conservative call-site candidates, and bounded top-level module-file layout,
including literal paths and unverified conditional candidates. Python `.py`
files support syntax-only definitions, imports, call sites, and module import spellings.
Released LORE owns durable source-hashed snapshots; the combined integration
targets LORE 0.62.22. DOXA does not write them.

## Contents

- [Shipped surface](#shipped-surface)
- [Freshness and coverage](#freshness-and-coverage)
- [Repeated explicit TUI queries](#repeated-explicit-tui-queries)
- [Explicit snapshot export](#explicit-snapshot-export)
- [Reviewed snapshot read](#reviewed-snapshot-read)
- [Architecture boundary](#architecture-boundary)
- [Still open](#still-open)

## Shipped surface

`doxa codegraph [--root WORKTREE] file PATH | symbol NAME | imports PATH | calls PATH | modules PATH`
returns bounded JSON. With no `--root`, it uses the current Git worktree.
`file` lists parsed definitions and import declarations in one Rust or Python file;
`symbol` finds all exact name or qualified-name matches across that worktree;
`imports` lists syntactic Rust `use` and `extern crate`, or Python `import`,
`from ... import`, and `__future__` declarations in one file. Python rows cover
classes, functions, methods, and nested definitions with dotted lexical names.
Aliases and glob imports stay declarations; they do not prove a module target or
binding. Python `calls` lists bare-name, attribute, and indirect call expressions
under their lexical caller. Decorator, default-argument, annotation, and class-base
calls belong to the enclosing scope; definition bodies use the new scope. Every
Python call has `binding: unresolved` and no candidates. Python `modules` lists
static `import` and `from ... import` module spellings. Runtime targets and
resolutions stay unknown. An absolute spelling may show a hashed
`structural_candidate` when one parsed, Git-listed `.py` or package
`__init__.py` matches a complete local package layout. Relative imports,
namespace packages, file/package collisions, ignored files, symlinks, and
unparseable candidates yield no such candidate. This is a worktree layout
observation, not Python import resolution. Dynamic imports are calls, not
module edges.
Rust `modules` lists top-level `mod child;` declarations and resolves a unique
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

Rust uses [Syn's source parser](https://docs.rs/syn/latest/syn/fn.parse_file.html);
Python uses Tree-sitter Python. Both parse source bytes without executing them.
The Rust parser records top-level definitions, inline modules, trait methods, and methods
whose `impl` self type is a simple path. Imports are declarations, not resolved
file dependencies. A call edge records its lexical caller and spelled target.
Direct paths carry up to eight function or method definitions whose final name
segment matches; `candidate_only` means one such declaration was observed,
**not** that Rust would bind the call to it. Multiple matches are `ambiguous`;
no match and receiver-method calls are `unresolved`. The answer counts omitted
candidates. Calls through variables, closures, macros, function-local items,
and qualified-self paths are not resolved. Neither macro expansion nor
conditional compilation is evaluated.
TypeScript and other recognized languages without a parser are reported as
unsupported rather than silently treated as empty. Semantic binding remains
unknown for both Rust and Python rows in ordinary syntax answers.

Linux also exposes `doxa codegraph --semantic-broker --root WORKTREE --socket
SOCKET --pins RECEIPT.json --broker-key ED25519_HEX --file PATH --edge N
--candidate N`. It selects one displayed plain Rust call candidate and requires
an operator-provisioned root broker, a public key obtained separately, and exact
broker/configuration/Engine/analyzer/image pins. A successful immutable-source
and live Engine/LSP transaction says `analyzer_resolved`, with an explicit
analyzer-definition limit. It does not establish compiler execution, cfg
reachability, general Rust type resolution or a persistent graph. Conditional,
generated, ambiguous and uncheckable inputs fail closed. The
[semantic implementation and provisioning contract](codegraph-semantic-verification.md)
records the trust boundary and measured guest acceptance. The older
`--semantic-probe` and UID/socket observation seams remain untrusted.

## Freshness and coverage

Rust syntax admission also bounds recursive parser, visitor and AST cleanup
work before calling `syn`: at most 32 nested delimiter groups and 128 tokens
across active statement/item regions. The nonrecursive check treats comments,
quoted/character literals and raw literals as opaque, and resets sibling
statements/items independently. Large or unusually complex valid syntax may
therefore be reported as unparseable with an explicit `recursive parsing
withheld` reason; it never contributes a complete Rust scan digest. This guard
also applies to declaration checks for semantic evidence. It does not change
the existing 1 MiB source-file, 64 MiB scan or bounded reply limits.

Every CLI query enumerates the current tracked **and untracked, nonignored** Git
files and reads Rust and Python bytes afresh. Git root lookup and file enumeration
use a ten-second subprocess deadline, 4 KiB/4 MiB root/list stdout caps and a
16 KiB stderr cap. On failure, `waitid(WNOWAIT)` reserves the leader's group ID
until process-group cleanup kills descendants, including ones that closed their
pipes. A timeout, failed command, oversized listing, or incomplete
NUL-delimited path list returns no answer. The explicit TUI query cache
re-reads and hashes those bytes before reusing an answer.
Each row and call edge carries its file, line, SHA-256 of the parsed bytes, and
read time. Candidate declarations carry their own source hashes and read times.
When **every listed Rust file parses**, the answer contains deterministic
`scan_input_sha256` over Rust paths and source hashes. The corresponding
`python_scan_input_sha256` covers every parsed, Git-listed Python path and hash.
Each digest is absent if an input in that language was skipped or unparseable.
The fresh query also re-enumerates Git files after parsing and fails without
an answer if the listing changed during the scan. This catches added or removed
inputs before a complete digest is reported. Immediately before returning, it
also rereads every parsed Rust and Python file twice and compares its source
hash; a byte edit after parsing fails without an answer if it remains changed
when that file is rechecked. Each language has the same 64 MiB cap per pass,
and all final rereads share a ten-second elapsed-time limit checked between
file reads. Source opens and reads run on one process-local worker with a
two-second caller deadline, including any wait for that worker. A timeout
rejects that source and permanently disables further source reads in this
process; the blocked worker may remain in the kernel until process exit.
An edit after a file's final read, or an edit and
restoration between reads, can still escape detection; this is not an atomic
repository snapshot.
The inventories exclude ignored files and do not prove compiler or Python
runtime semantics. Separate skipped and unparseable counters let LORE recheck
each language independently. Older stored answers without Python inventory
fields report its freshness as unknown.
File-scoped answers also carry the requested source's hash and read time, even
when that parsed file produces no rows.
The answer names its worktree and reports parsed files, unsupported languages,
syntax errors, skipped files, and unsupported syntax. Same-name definitions
remain separate candidates. A no-hit answer names a live-search fallback.

Work is bounded: at most 20,000 files, 1 MiB per Rust or Python file, 64 MiB
total source per language, ten seconds for the Python scan, and two seconds of
source open/read and two seconds of parsing, 200,000 syntax nodes, and 128
nested definition levels per Python file.
Results hold at most 100 rows,
call edges, or module declarations, 10,000 call sites in the requested
Rust or Python file, 10,000 Python import-module declarations, 4 KiB per
retained Python call target expression and 32 KiB total retained target text,
100,000 candidate declarations, and a 64 KiB reply. The answer counts omitted
rows, edges, and per-edge candidates.
When enumeration or the total scan budget fails, the command returns an error
instead of a partial result. Oversized, changed-during-read, symlinked, and
unparseable files are named in bounded issue summaries. A file hash is evidence
for the bytes parsed, not a promise that the worktree has stayed unchanged since
that read.

## Repeated explicit TUI queries

The active local-session TUI automatically prepares one process-local Rust/Python
source index outside the input/render loop. One preparation worker and nonblocking
cache lock prevent concurrent scans; attempts are throttled to 300 seconds for
the same root and 30 seconds across root changes. This is bounded background
preparation, not a scan on every agent turn or a persistent watcher.

The index retains declarations/imports and hashed parse-failure tombstones,
within a 16 MiB estimated allocation budget and 100,000 declarations. It retains
no ASTs or source bytes. Novel file/symbol/imports/calls queries can reuse these
facts; calls in the requested file are reparsed. Every indexed answer still
re-enumerates the complete tracked/untracked nonignored Git inventory, reads
all readable Rust/Python files and checks their hashes and modes, then repeats
the complete source hash check twice. Listing changes, byte/mode drift, failed
reads, root changes and overflow discard the index and exact-answer entries.
The existing source deadlines, 64 MiB per-language/pass limit and ten-second
recheck limit remain. A source timeout permanently disables this process's
reader; one blocked kernel worker can remain until exit.

The existing eight-entry, 256 KiB serialized exact-answer cache remains for
complete inventories. Answers with parser failures are never retained there.
The index can reuse hashed parser-failure tombstones, preserving the original
skipped/unparseable counts and reasons on every answer. These failures never
create a complete scan digest, semantic promotion or verified stored coverage.
This matters on DOXA itself: the measured inventory contains 84 of 295 Rust
files withheld by the existing parser guard. The viewer labels indexed answers
and revalidated exact answers separately and retains original read times.
Module queries scan afresh and clear both caches because structural candidates
also probe ignored/unlisted paths. CLI syntax and reviewed LORE reads stay fresh.

The [controlled index/cold-source/viewer measurement](../codegraph-index-benchmark-2026-10-13.md)
compares five alternating-order release-library pairs and identical normalized
answers. Prepared novel-query p95 is 85.214 ms on DOXA and 637.613 ms on an
8,000-source fixture. A separate five-repetition source-page-cold CLI scan has
p95 1,013.917 ms after eviction with mincore=0; other caches are uncontrolled.
Production viewer open, worker scheduling, formatting and drawing measured in
a debug TUI build have p95 560.643/885.342 ms respectively. Library latency is
not complete TUI latency, and an empty answer cache is not a cold filesystem.
The previous warmed exact-answer benchmark remains historical evidence.
Repeated mutable observations do not establish an atomic repository snapshot.

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
calls only `codegraph_snapshot_read_v1`; it checks
the exact project, canonical worktree, query, path, revision, source hash,
graph digest, and `unknown` binding claim. LORE checks Git worktree identity
and rehashes the requested file on each read. A source edit or checkout
replacement rejects the read. DOXA also rechecks the hashes of included row,
call-candidate, and module-target files through a bounded, symlink-safe reader.
The result reports `verified`, `stale`, or `unknown` for those included
references, with bounded reasons. When the graph has complete scan-input
digests, DOXA also re-enumerates and rehashes every listed Rust and Python
input at read time. An edit, addition, or removal makes that language's
inventory stale; an unreadable or symlinked source makes it unknown. Older
snapshots without the matching digest stay unknown. Each readback checks the
full source set twice, with a 64 MiB cap and elapsed-time checks before and
after file reads, then checks the Git path list again. Each Git subprocess has
a ten-second deadline. A blocked source read makes the inventory unknown at
the two-second caller deadline; a stuck worker can remain until process exit.
An early file that remains changed during the
second pass makes the inventory unknown instead of falsely verified. Edits
after a file's second read, or an edit restored between reads, can still escape
detection; this is not an atomic repository snapshot. Omitted result rows and
semantic bindings remain outside its claim.

Python snapshot support was introduced in released **LORE v0.62.21**, commit
`e398ea2b0d8b1a8c52614ee69fa483005c5e9c81`. The combined integration targets
released **LORE v0.62.22**, commit `71f9698a`, preserving this reviewed-store gate. The client and service validate bounded `.rs` and `.py` paths;
Python binding remains `unknown`. DOXA does not store snapshots automatically.

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
  exact worktree/query/path; it verifies the requested source, included
  references, and complete Git-listed Rust and Python scan inputs when a digest exists.
- General Rust imports, traits/types, macro expansion and conditional compilation
  remain outside the analyzer-definition slice. Structural module candidates
  keep their existing limits. Semantic activation requires the externally
  provisioned host-controlled signer/Engine/image contract described above;
  no automatic owner-host installation is performed.
- Decide whether a reviewed agent tool, persistent TUI tree or explicit semantic
  viewer overlay is useful. Automatic preparation serves read-only syntax
  queries; attested definitions are explicit CLI requests and are not stored.
- Decide whether other languages justify a parser dependency and coverage bar.
  Python runtime module resolution, dynamic imports, and type-aware
  binding remain outside the shipped syntax slice.
- Prepared library queries meet the DOXA p95 target in the measured workload.
  Larger/cold-source/viewer measurements are now recorded separately. A release
  TUI measurement and representative user workloads may justify further work;
  no timing result is a universal latency guarantee.
