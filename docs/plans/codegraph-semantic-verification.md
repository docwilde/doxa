# Rust semantic definition verification

DOXA's call graph currently reports syntax candidates, not compiler-resolved
bindings. `doxa-codegraph::semantic_evidence::inspect_definition_reply` is a
library-level, opt-in evidence checker for one `textDocument/definition` exchange.
It accepts a fresh call edge and one displayed candidate, reopens both sources
with the worktree-anchored, no-symlink reader, checks their SHA-256 hashes,
reparses the call and target function, and requires the LSP range to select the
declaration identifier exactly. It rejects ambiguous replies, mismatched IDs,
external or encoded URIs, changed files, unsupported UTF-16 lines, and oversized
messages. A matching exchange returns `protocol_match_untrusted` and
`binding: unknown`.

This checker is deliberately not wired to `/codegraph calls` or persisted
snapshots. Neither an LSP JSON object nor a local rust-analyzer process proves
which workspace configuration, build scripts, proc macros, or network access it
used. No rust-analyzer binary is installed in the development environment.

Before promoting any LSP result to a binding, implement a pinned rust-analyzer
producer in a quota-limited, no-egress container with a read-only worktree and
no host secrets. Disable build scripts and proc macros in both server settings
and server launch policy. Bound memory, CPU, PIDs, file descriptors, time, and
output; require successful workspace indexing and a quiescent diagnostic state.
Attest the exact server binary/configuration, request and response, source and
target hashes, and container policy. Treat missing, timed-out, conditional,
ambiguous, generated, or uncheckable evidence as `unknown`. Verify the final
binding against the same source bytes before surfacing it in the TUI or LORE.
