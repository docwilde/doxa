# DOXA remote hub and Android client

Status: first private implementation on this branch. The Rust browser adapter
controls local sessions. A volatile Rust hub and outbound host connector now
register sessions, broker prompts/answers and forward live events across
machines. The browser can request a bounded recent transcript from its host;
the hub also holds a short live event ring.
native remote DOXA tabs, background Web Push and Android are not yet shipped.

## User journey

1. A DOXA daemon keeps the conversation, files, worktree, provider connection,
   transcript and approval queue on its host machine.
2. A host connector registers an opaque host ID and the live session IDs with a
   private hub. It makes outbound requests, so the host needs no public
   listener or port forwarding.
3. A browser, another DOXA instance or an Android app signs in, sees the owner's
   hosts and sessions, attaches at a sequence cursor, and issues bounded commands.
4. The host connector checks every command against the local remote policy and
   the current daemon state. It sends the result and event stream back through
   the hub. The daemon remains the single source of truth.

The hub is a broker and presence index. A restarted hub cannot create or resume a
provider session. It cannot directly read the host filesystem or daemon socket.

## Wire contract

Every control command has `command_id` (a random result key), `host_id`,
`session_id`, `kind`, `created_at`, and a bounded JSON payload. The first kinds
are `prompt`, `answer`, and a read-only recent `transcript` snapshot. A client
may supply `request_id`; reusing it with the same payload returns the same
command during the bounded result-retention window. Replies are `accepted`, `refused`, or `expired` and
include the daemon's actual reply. A prompt is acknowledged as queued or started;
it is never silently resent after an uncertain disconnect. An answer includes
the exact pending request snapshot and is checked again by the daemon.

Events carry the daemon `seq` and `turn` unchanged. A client supplies its last
received sequence on reconnect. The host replays its bounded event ring; on a
gap it sends an explicit `replay_gap` and a bounded transcript snapshot. The
hub may keep a short encrypted event buffer for reconnection, but the host
transcript is authoritative. Presence leases expire after missed heartbeats,
and stale sessions disappear from the active list rather than accepting writes.

Initial REST/SSE surface:

| Caller | Operation | Result |
| --- | --- | --- |
| Host | register/refresh | Host lease and sessions |
| Host | pull commands, publish event/reply | Outbound-only relay |
| Client | list hosts/sessions | Owner-scoped presence |
| Client | read transcript, subscribe events | Bounded snapshot, sequenced stream |
| Client | prompt, answer | Exact daemon acknowledgement |

The Rust browser adapter implements the last two rows against one host with
`GET /api/sessions`, `POST` transcript snapshot, SSE events, and `POST` prompt
and answer. The hub brokers snapshots from the host and retains bounded live
events for reconnects. A second DOXA instance can list sessions, send
prompts and resolve simple approvals with `doxa remote` CLI commands;
rendering remote sessions as native TUI tabs remains to be implemented.

## Authentication and authority

The first deployment is private Tailscale Serve over an owner-private Unix
socket, with kernel-attested proxy UID and an explicit login allow-list. Both
host connector and clients authenticate to the hub. The hub scopes all IDs to
the authenticated owner. A host registration gets a random, memory-held lease
credential bound to that owner and host ID; losing it requires a new registration.
No browser response contains that credential.

The connector and browser must originate from user-owned Tailscale devices;
Serve does not populate `Tailscale-User-Login` for tagged source devices.

Every write passes two gates: the hub checks the owner/session lease, and the
host connector applies `remote_policy` and verifies the exact live daemon
session. Shell escapes, permission-mode changes and unrestricted permission
modes remain refused remotely by default. The host connector never forwards
arbitrary daemon RPCs. Approvals are tied to the current pending request
snapshot; replayed or stale answers fail.

The server is trusted with session text in the first implementation. Hosting
it outside the user's private tailnet requires a separate security review,
durable encrypted storage policy, user authentication, rate limits and audit
trail. End-to-end encryption between device and host is the later direction;
the initial private deployment must not imply it has that property.

## Notifications

The Rust browser adapter can show notifications while its page is connected
and hidden. True background delivery needs a service worker, a user-approved
Push subscription, a VAPID key, and encrypted Web Push delivery. The hub owns
subscriptions per device and sends only a generic `needs input` or `turn done`
alert; opening the app fetches the actual content after authentication.
Expired subscriptions are removed. Android uses the same generic event policy
with a platform push token; opening the app resumes from the last event cursor.
Notifications never carry tool arguments, transcript text or approval actions.

## Android client contract

The first Android client can use Kotlin and Compose over the device's Tailscale
connection. It needs no daemon socket or provider credentials. The user supplies
the private hub URL, and Serve supplies the user identity for that device. The
app lists sessions, requests the bounded recent snapshot, follows SSE with its
last sequence, and sends prompts and pending-input decisions through the same
command-result API as the browser. It retains a stable `request_id` while a
submission is uncertain and shows an explicit confirmation before retrying an
expired write. The host lease never leaves the connector.

Keep only session IDs, cursors, and unsent drafts on the device, in Android's
app-private storage. On reconnect, refresh the inventory and pending inputs
before offering an approval. A future push token registers per device and
owner; the notification opens that session and fetches current state through
the authenticated hub. Tailscale Serve login headers are absent for tagged
source devices, so the initial Android path assumes a user-owned device.

## Delivery stages

1. **Local Rust bridge:** ship and live-test the Rust browser adapter on Linux
   and macOS, retire the Python adapter, and add a browser notification setting.
2. **Private hub:** leases, owner-scoped registration, bounded command/reply
   queues, on-demand recent transcript retrieval and cursor replay are
   implemented. Add a browser E2E test across two isolated hosts and deployment QA.
3. **Remote DOXA client:** show remote tabs in the Rust TUI, reconnect without
   duplicate prompts, and expose the same pending approval flow.
4. **Background delivery:** Web Push with service worker and then Android push.
   The Android app renders transcript and events, sends prompts and answers,
   and delegates account sign-in to the private hub.

Release gates for each stage: opt-in off by default; denied and forged identity
tests; replay, duplicate command and stale approval tests; connection-loss tests;
provider turn and detached daemon checks; Linux and macOS transport verification.
