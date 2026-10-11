# Remote approval snapshots

Native remote TUI, Android, the plaintext browser and the packaged encrypted
extension bind approvals to the request they displayed and the current session
incarnation. The host refuses a missing or stale `reviewed_request`, incomplete
pending-input state, and a missing or changed incarnation. It does not substitute
a freshly fetched request for an older review. Prompts also require the current
incarnation. Update clients and the host together: older clients that send bare
approvals or prompts without an incarnation are intentionally refused.

A disconnected client must reload a complete pending-input snapshot before
writing. Gap recovery removes approval controls before its network reload starts.
A repeated request ID is not sufficient authority when the request body changed.

## Raw CLI workflow

First capture the pending request using the read-only review command. Save the
receipt with owner-only permissions, inspect its full `reviewed_request`, and
then answer using that file:

```sh
umask 077
doxa remote review https://owner.tail.ts.net/ HOST~SESSION REQUEST_ID > "$HOME/review.json"
cat "$HOME/review.json"
doxa remote answer https://owner.tail.ts.net/ HOST~SESSION REQUEST_ID allow "$HOME/review.json"
```

Use `deny` to decline. The receipt binds the target, request ID and incarnation;
a stale receipt is refused. Bare `remote answer ... allow|deny` is refused with
instructions to review first. The file must be an absolute owner-only regular
file, at most 96,000 bytes, without symlinks, hard links or writable parent
directories. Receipts contain the already scrubbed pending request and no
transport key or authentication values added by the client. Keep them private.
The CLI never fetches a replacement request while claiming the operator reviewed
it. `remote send` reads the current transcript incarnation before sending its
prompt; this read does not approve any pending request.

## Verification

The packaged extension has isolated Node VM regression fixtures for valid exact
approvals, same-ID changes, initial local hello replacement/removal, malformed or
incomplete snapshots, gaps, missing sequences, disconnects, replacement sessions,
failed reloads and retained drafts. The plaintext browser recovery suite exercises
the same snapshot boundary. Rust fixtures cover host review refusal, protected
receipt reads, captured native TUI reviews and native hub recovery. Local fixtures
use no accounts, provider requests or user notification services. Rust execution
is recorded separately by the integration gate.

Hub replies are bounded before JSON parsing. A connector response permits at
most 1,200,000 bytes for the eight-command queue; single CLI results permit
128,000 bytes. Body reads have a total five-second deadline, including chunked
responses. Oversized or stalled replies are refused without automatic replay.
