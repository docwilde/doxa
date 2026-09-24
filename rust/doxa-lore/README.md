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
from LORE's own project mapping, rejects links and foreign-owned files, and
returns counts only. Indexing is idempotent in LORE and does not derive beliefs
or approve proposals. When LORE provides its same-descriptor pending snapshot
API, the sidecar advertises `pending_review_v1` to
return one complete raw UTF-8 proposal, its SHA-256 digest, inode, and an
explicit completeness marker. The client checks the raw bytes against the
digest, verifies the requested ID, and rejects partial or malformed replies.
Its raw proposal is intentionally unsummarized and may contain secrets; it is
sent only over the local pipe for a review screen and never logged. A request
may include `expected: {sha256, inode}` to refuse a proposal changed since a
previous review. Missing, oversized, malformed, or changed proposals fail
without a partial response.
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
not approval evidence. `pending_review_v1` is read-only. Safe approval still
needs a sidecar operation that atomically claims the pending file and verifies
its current SHA-256 and inode against the exact proposal the UI displayed,
with an explicit human review gate enforced at the UI/daemon boundary. A
caller-supplied `reviewed` flag or pending ID alone cannot authorize a write.
LORE's own approval path also requires special handling for unverified sync
operations and durable archive-on-success. Until that end-to-end protocol
exists, this crate has no approval mutation. Codex transcript indexing is
connected to the native daemon; deriver review of Codex transcripts remains
skipped, matching Python DOXA's declared limitation. Python remains a
dependency only for this external LORE integration during the transition.
