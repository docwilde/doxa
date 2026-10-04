# DOXA remote hub and Android client

Status: first private implementation. The Rust browser adapter
controls local sessions. A volatile Rust hub and outbound host connector now
register sessions, broker prompts/answers and forward live events across
machines. The browser can request a bounded recent transcript from its host;
the hub also holds a short live event ring.
The native TUI can open remote-only tabs through `doxa remote tui HUB_URL`.
The browser can receive encrypted background Web Push after explicit opt-in;
Android is not yet shipped.

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

The hub assigns each command a random `command_id` result key and retains its
owner, host, session, creation time, operation, and bounded JSON payload. The first kinds
are `prompt`, `answer`, and a read-only recent `transcript` snapshot. A client
may supply `request_id`; reusing it with the same payload returns the same
command during the bounded result-retention window. Replies are `accepted`,
`refused`, or `expired` and
include the daemon's actual reply. A prompt is acknowledged as queued or started;
it is never silently resent after an uncertain disconnect. An answer includes
the exact pending request snapshot and is checked again by the daemon.

Events carry the daemon `seq` and `turn` unchanged. A client supplies its last
received sequence on reconnect. The host replays its bounded event ring; on a
gap the daemon emits `replay_gap`; the browser can then request a fresh recent
transcript snapshot. The hub keeps a short in-memory event buffer for
reconnection, but the host transcript is authoritative. Presence leases expire after missed heartbeats,
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
prompts and resolve simple approvals with `doxa remote` CLI commands, or open
live sessions in a separate native TUI window with `doxa remote tui HUB_URL`.
Remote tabs use the same prompt, pane, transcript and pending-input UI. Local
provider settings, filesystem actions and LORE management remain on the host;
the remote tab layout is not yet persisted or mixed with local tabs.

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
and hidden. The private hub also supports background Web Push through a
service worker, user-approved subscription and server-owned VAPID key. The
operator enables it with `doxa-hub push-keygen` in the private runtime directory,
then sets `DOXA_HUB_VAPID_SUBJECT` to a `mailto:` address and restarts the hub.
The hub sends an encrypted `needs_input` or `turn_done` kind to subscribed
devices and removes endpoints after a push service confirms expiry. The payload
contains no session ID, transcript, tool argument or approval action. Opening
the app fetches current state after Tailscale authentication.

Subscriptions remain volatile and the browser registers them again on its next
visit after a hub restart; the VAPID key file must be retained. Outbound sends
are limited to known HTTPS browser push services. A device needs browser Push
API support and private hub access to enable alerts. Android will use the same
generic event policy with a platform push token and reopen at its last cursor.

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
3. **Remote DOXA client:** remote-only Rust TUI tabs, bounded snapshot replay,
   stable retry IDs, and pending-input review are implemented. Persistent tab
   layouts and mixed local/remote windows remain open.
4. **Background delivery:** private browser Web Push with service worker is
   implemented. Android push and the Android client remain open; the app will
   render transcript and events, send prompts and answers, and use hub sign-in.

Release gates for each stage: opt-in off by default; denied and forged identity
tests; replay, duplicate command and stale approval tests; connection-loss tests;
provider turn and detached daemon checks; Linux and macOS transport verification.
