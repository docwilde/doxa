# External LORE sidecar bridge

`doxa-lore` talks to `python -m doxa.lore_bridge` over bounded JSONL pipes.
The sidecar uses the same plugin/package selection as Python DOXA and keeps
LORE's scrubber and context builder in their owning Python package. The Rust
client launches it lazily, enforces a deadline on each response, and kills
the process group if it stops responding. Requests carry text on stdin, never
in process arguments. Sidecar stderr is suppressed so exceptions cannot echo
prompts or credentials into terminal logs.

Protocol v1 has `hello` with required `scrub` and `snapshot` capabilities and
optional `pending`, `sync_state`, and `refresh_interval` readers. Pending rows
are scoped to the requested project and paged to at most 50 records; text
fields pass through LORE's scrubber. Sync state returns null when sync is off
or unavailable, matching Python DOXA's hidden status chip. Older sidecars
without the optional capabilities remain usable for scrubbing and snapshots.
Requests and replies carry one numeric ID at a time. Frames are at most 1 MiB. Failure
to scrub is an error that callers must handle before persistence or sending
the text to another model. The bridge currently does not expose consult,
review, pending approvals, belief operations, or indexing, and
is not yet connected to the native daemon or engine adapters. Python remains
a dependency only for this external LORE integration during the transition.
