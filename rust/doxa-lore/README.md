# External LORE sidecar bridge

`doxa-lore` talks to `python -m doxa.lore_bridge` over bounded JSONL pipes.
The sidecar uses the same plugin/package selection as Python DOXA and keeps
LORE's scrubber and context builder in their owning Python package. The Rust
client launches it lazily, enforces a deadline on each response, and kills
the process group if it stops responding. Requests carry text on stdin, never
in process arguments. Sidecar stderr is suppressed so exceptions cannot echo
prompts or credentials into terminal logs.

Protocol v1 has `hello` with required `scrub` and `snapshot` capabilities and
optional `pending`, `sync_state`, `refresh_interval`, `consult`, `beliefs`, and
`evidence` readers. `index_transcript_v1` delegates incremental indexing to
LORE after a native Codex turn and again at finalization. The request carries
only cwd and a validated session ID; the sidecar derives the transcript path
from LORE's own project mapping, opens each component without following links,
and verifies the opened file's owner, type, size, and link count. It passes the
file descriptor to LORE's `index_live_fd` so a path replacement cannot redirect
the indexer to another file. Older LORE builds without this API do not advertise
transcript indexing. Indexing returns counts only, is idempotent in LORE, and
does not derive beliefs or approve proposals. When LORE provides its same-descriptor pending snapshot
API, the sidecar advertises `pending_review_v1` to
return one complete raw UTF-8 proposal, its SHA-256 digest, inode, and an
explicit completeness marker. The client checks the raw bytes against the
digest, verifies the requested ID, and rejects partial or malformed replies.
Its raw proposal is intentionally unsummarized and may contain secrets; it is
sent only over the local pipe for a review screen and never logged. A request
may include `expected: {sha256, inode}` to refuse a proposal changed since a
previous review. Missing, oversized, malformed, or changed proposals fail
without a partial response.

The Rust TUI's LORE picker pages scrubbed pending previews for the active
session's project (or the current directory when no session is selected).
Press `P` from an empty belief search, then Enter to request the complete raw
proposal through `pending_review_v1`. The review view exposes the digest and
inode and scrolls the entire raw content, including signed sync operations.
Opening a review does not mark an unverified sync operation as reviewed in
LORE's ledger.
When installed LORE exposes `record_full_review` and `resolve_reviewed`, the
sidecar also advertises `resolve_reviewed_v1`. The Rust picker enables `A`
approve and `R` reject only after the user has scrolled through every visual
row of the complete raw proposal, then requires Enter confirmation for that
single snapshot. The sidecar checks the exact SHA-256, inode, and project
visibility again, records a full-review marker for approval, and delegates the
claim, apply, and archive to LORE. It does not accept a caller-provided item or
bulk IDs. A refusal includes a bounded code. `applied: true` is allowed only
for `archive_failed`: the curated write landed but archival did not complete.
The UI reports it and never retries on its own. A lost sidecar reply has an
unknown outcome; the UI asks the user to inspect LORE pending/archive before
any retry.

Consult returns one active FTS belief labelled `cite_only`;
belief and evidence pages contain at most 50 rows. Text is scrubbed before
reply and truncated fields carry explicit markers. Pending rows
are scoped to the requested project and paged to at most 50 records; text
fields pass through LORE's scrubber. Sync state returns null when sync is off
or unavailable, matching Python DOXA's hidden status chip. Older sidecars
without the optional capabilities remain usable for scrubbing and snapshots.
Requests and replies carry one numeric ID at a time. Frames are at most 1 MiB. Failure
to scrub is an error that callers must handle before persistence or sending
the text to another model. The paged `pending` response is a scrubbed preview,
not approval evidence. `pending_review_v1` itself remains read only. The
write capability is absent when installed LORE lacks its atomic claim API;
the older `apply_item(snapshot=...)` and `archive(expected_snapshot=...)`
functions are never used as a fallback. The human action is local to the TUI,
not a daemon RPC or model tool. Codex transcript indexing is connected to the
native daemon; deriver review of Codex transcripts remains skipped, matching Python
DOXA's declared limitation. Python remains a dependency only for this external
LORE integration during the transition.
