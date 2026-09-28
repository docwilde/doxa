# Canonical native LORE integration

`doxa-lore` integrates canonical native LORE 0.62.1. Production
`LoreClient::open` constructs an in-process `lore_core::Core`; Python is not
an installed runtime dependency. LORE owns memory, authority, locks, indexing,
review, and secret scrubbing. DOXA validates bounded results at the adapter
boundary and must reject unsanitized text before persistence or model input.
See [`lib.rs`](src/lib.rs) and the [current runtime guide](../README.md).

`open` creates a human-review carrier; `open_agent` creates a separate model
carrier whose host identity is bound by the catalog operation. Model JSON cannot
select human authority. Construction is lazy and does not open a store, so a
memory-off session can still use pure scrubbing. [`native_config.rs`](src/native_config.rs)
resolves explicit `LORE_ROOT`, the sticky DOXA store setting, and bounded shared
capacity settings without changing the process environment.

An explicitly selected carrier can also serve the compatibility protocol over
bounded JSONL pipes. The installed `lore-rs` supports the retained `-m` module
dispatch; Python bridge scripts remain development interoperability fixtures.
The adapter enforces response deadlines, suppresses carrier stderr, and kills
the process group if it stops responding. Text travels on stdin rather than
in process arguments. The following protocol and review contracts apply to
the native adapter as well as a compatible carrier.

## Bounded operations and review

The native human-review adapter provides scrubbing, context snapshots, curated
memory usage and entries, belief/evidence pages, pending proposals, sync state,
and transcript indexing. A pipe carrier advertises the operations it supports
in a protocol-v1 `hello`; missing optional capabilities remain unavailable.
Requests and replies retain the same numeric-ID and 1 MiB encoded frame boundary
on both backends. Model carrier operations have a separate 64 KiB boundary.
Text fields are scrubbed before display; truncated previews carry markers.
Belief, evidence, and pending pages contain at most 50 rows.

`index_transcript_v1` accepts cwd and a validated session ID, not an arbitrary
transcript path or transcript contents. Canonical LORE derives and opens the
DOXA transcript through its project mapping with owner and descriptor checks.
Indexing returns counts and does not derive beliefs or approve proposals. Native
daemon hosts own turn/finalization indexing and configured review workers;
review support and compaction guarantees remain provider-specific. See the
[Codex adapter guide](../doxa-engines/README.md) and
[current runtime guide](../README.md) for their documented limits.

The Rust TUI's pending picker requests `pending_review_v1` for one complete raw
UTF-8 proposal. The adapter verifies its ID, SHA-256 digest, inode, JSON shape,
and explicit completeness marker. Raw review content may contain secrets and
is never logged. It is available only to the local human review screen rather
than a model tool. A previous digest/inode can be supplied to refuse a changed
proposal. Missing, malformed, oversized, or changed content returns no partial
approval evidence; scrubbed previews cannot authorize a write.

`resolve_reviewed_v1` requires the complete reviewed snapshot and one explicit
human decision. The UI requires all visual rows to be read and a confirmation.
LORE rechecks project visibility and exact snapshot under its own claim/write
rules, then owns application and archive. DOXA accepts no caller-provided item
or bulk IDs. Refused results report whether a write applied; indeterminate
results require inspection before any retry. The UI never retries an uncertain
mutation automatically. Human review authority is separate from the bound
model carrier and is never a daemon RPC or model tool.

Belief and curated-memory review/actions also use exact snapshot proofs and
canonical LORE mutation gates. The adapter validates returned identity, hashes,
completeness, scope, and bounded result shapes; capability availability alone
does not grant approval authority. Sync state returns null when unavailable or
disabled, matching the hidden status chip.

## Verification

From the repository root:

```sh
cargo test --locked -p doxa-lore
```

Use isolated disposable stores for adapter tests. Fixture success does not
establish live provider review success.
