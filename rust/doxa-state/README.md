# doxa-state

Local file compatibility layer for the Rust 2.0 port. It validates session IDs,
reads live daemon registry entries into separate routing and redacted display
fields, reads and atomically writes TOML settings,
and reads and atomically writes flat tabset records. Callers supply paths rather
than relying on process-global environment variables.

Tabset writes preserve unknown top-level keys and layout members, including
Python's split trees, groups, and collections. They refuse changes to tab IDs
while any of those structures are present, until pruning is ported. `ensure_machine_id` now mints DOXA's local 32-character UUID4 hex identity once,
with an atomic no-clobber publish so concurrent starters keep one ID.
`resolve_tabset_path` adopts a valid pre-1.10 scope-only tabset into the
machine-specific name without overwriting an existing record. The Rust crate
does not yet interpret split structures or resolve archived transcripts. It
also does not probe sockets, reap stale registry files, or implement Python's
full typed settings catalog. The caller
must supply its secret scrubber for registry reads. Unknown registry fields are
dropped. Raw routing fields must never be displayed. Registry results are advisory and must not be treated
as proof that a socket is attachable.
