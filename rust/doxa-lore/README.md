# External LORE sidecar bridge

`doxa-lore` talks to `python -m doxa.lore_bridge` over bounded JSONL pipes.
The sidecar uses the same plugin/package selection as Python DOXA and keeps
LORE's scrubber and context builder in their owning Python package. The Rust
client launches it lazily, enforces a deadline on each response, and kills
the process group if it stops responding. Requests carry text on stdin, never
in process arguments. Sidecar stderr is suppressed so exceptions cannot echo
prompts or credentials into terminal logs.

Protocol v1 has `hello` with `scrub` and `snapshot` capabilities, then one
request/reply at a time with a numeric ID. Frames are at most 1 MiB. Failure
to scrub is an error that callers must handle before persistence or sending
the text to another model. The bridge currently does not expose consult,
review, pending proposals, belief operations, indexing, or sync state, and
is not yet connected to the native daemon or engine adapters. Python remains
a dependency only for this external LORE integration during the transition.
