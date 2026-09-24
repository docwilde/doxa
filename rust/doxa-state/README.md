# doxa-state

Local file compatibility layer for the Rust 2.0 port. It validates session IDs,
reads live daemon registry entries, reads and atomically writes TOML settings,
and reads and atomically writes flat tabset records. Callers supply paths rather
than relying on process-global environment variables.

Tabset writes preserve unknown top-level keys and layout members, including
Python's split trees, groups, and collections. The Rust crate does not yet
interpret those structures or resolve archived transcripts. It also does not
mint a machine id, migrate legacy tabset names, probe sockets, reap stale
registry files, scrub secrets in registry strings, or implement Python's full
typed settings catalog. Registry results are advisory and must not be treated
as proof that a socket is attachable.
