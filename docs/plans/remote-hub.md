# DOXA remote hub and Android client

Status: first private implementation. The Rust browser adapter
controls local sessions. A volatile Rust hub and outbound host connector now
register sessions, broker prompts/answers and forward live events across
machines. The browser can page historical transcripts from its host;
the hub also holds a short live event ring. The packaged Chrome extension can
control encrypted sessions with code installed separately from the hub.
The native TUI can open remote-only tabs through `doxa remote tui HUB_URL` or
mix local and remote tabs in an open window with `/remote-control HUB_URL`.
`doxa remote tui HUB_URL --save-layout` opts into owner-scoped remote-only
pane persistence on this client. It restores tabs only when the authenticated
owner, session IDs and session incarnations match a fresh hub inventory. The
layout file contains pane geometry and tab IDs, never transcript, input or
event cursors; each restored stream obtains a new host snapshot and cursor.
In a local native window, `/remote-control HUB_URL --save-layout` opts into a
separate mixed pane layout for that local tabset scope, hub and owner. Repeating
the command after reopening the local window reconnects the hub and restores
the mixed pane order, geometry and focus once both rosters are complete. Local
tabs still belong to the local tabset; if its open tab set changed, the older
mixed overlay is skipped. Missing or replaced remote sessions are pruned.
`/local` selects an open local tab. Remote tabs use an `◎` marker.
The browser can receive encrypted background Web Push after explicit opt-in.
An Android Kotlin/Compose client project is in `android-client/`. Its FCM
background-push source and protocol tests exist, with owner- and
incarnation-scoped registration and generic tagged notifications. This host
cannot build the APK because its JDK compiler and Android SDK 37 are absent;
provisioned-device, FCM and private-tailnet QA remain open, so Android is not
yet shipped.

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
live sessions in native remote-only tabs with `doxa remote tui HUB_URL`.
From an open local TUI, `/remote-connect HUB_URL HOST_ID` starts a connector
owned by that window and `/remote-control HUB_URL` adds remote tabs to the
same terminal. `/local` selects an open local tab.
Remote tabs use the same prompt, pane, transcript and pending-input UI. Local
provider settings, filesystem actions and LORE management remain on the host;
remote tabs mix with local tabs but are not saved in local tabsets.

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

The native TUI and CLI now offer opt-in end-to-end encryption with an owner-only
shared key file (`DOXA_REMOTE_E2EE_KEY_FILE`) copied separately to host and
client. The host scrubs transcript and event content before encrypting it. Each prompt, approval
answer, transcript page, command result and live event body uses an independent
AES-256-GCM nonce and route-bound associated data. Useful payloads are DEFLATE
compressed before encryption; the compression flag and exact length are inside
the ciphertext, which is padded in 4 KiB buckets. The hub sees owner identity,
host/session IDs, operation and event kinds, timing, and bucketed ciphertext
sizes. It cannot read or change the encrypted content without detection. It
can still drop traffic. The host rejects repeated ciphertext nonces while its
connector runs and refuses commands older than two minutes; a connector
restart within that window cannot rule out every replay. Do not use a hub
outside the private tailnet without separate availability and replay controls.

The hub-served browser is deliberately unavailable for encrypted sessions: a
hub that serves its JavaScript could replace that JavaScript and capture a
browser key. The packaged Chrome extension in `browser-extension/` supplies
its own code, imports the key only for the open tab, and requests access to one
private hub origin. The hub accepts its writes only when that exact extension
ID appears in `DOXA_REMOTE_EXTENSION_ORIGINS` (or `remote_extension_origins`
in config), and still requires the attested Tailscale owner identity. The
local Rust browser adapter also refuses to serve while the shared-key setting
is active. Without that key setting, the hub-served browser continues to use
Tailscale HTTPS transport and the hub can read the content it brokers.

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
API support and private hub access to enable browser alerts. Android local
alerts still work only while SSE survives. Native Android background FCM data
push now has source and protocol tests: a configured build opts in per selected
session, the private hub registers its token only under the attested owner and
exact live incarnation, and the hub signs a short-lived Google OAuth request
before sending a generic kind plus opaque routing tag to FCM HTTP v1. The
Android receiver checks sender, opt-in, and current tag before notification.
Changing sessions rotates the tag, and opening the app refreshes current state.
Unconfigured builds and hubs keep this path disabled. Registrations are
volatile with a 24-hour lease and bounded send concurrency. The APK build,
provisioned Firebase and device delivery QA are still open.

## Android client contract

The first Android client can use Kotlin and Compose over the device's Tailscale
connection. It needs no daemon socket or provider credentials. The user supplies
the private hub URL, and Serve supplies the user identity for that device. The
app lists sessions, requests the bounded recent snapshot, follows SSE with its
last sequence, and sends prompts and pending-input decisions through the same
command-result API as the browser. It retains a stable `request_id` while a
submission is uncertain and shows an explicit confirmation before retrying an
expired write. The host lease never leaves the connector.

Keep session IDs, cursors, unsent drafts, and the opt-in FCM token and random
routing tag in Android's app-private storage. On reconnect, refresh the inventory and pending inputs
before offering an approval. The push token registers for the selected session and owner; the notification
opens the app, which fetches current state through the authenticated hub. Tailscale Serve login headers are absent for tagged
source devices, so the initial Android path assumes a user-owned device.

## Delivery stages

1. **Local Rust bridge:** ship and live-test the Rust browser adapter on Linux
   and macOS, retire the Python adapter, and add a browser notification setting.
2. **Private hub:** leases, owner-scoped registration, bounded command/reply
   queues, on-demand historical transcript pages and cursor replay are
   implemented. Add deployment QA across two isolated hosts.
3. **Remote DOXA client:** mixed local/remote tabs, bounded snapshot replay,
   scrollable history pages, stable retry IDs, pending-input review and optional
   compressed end-to-end encryption are implemented. Opt-in remote-only tab
   and mixed local/remote pane layout persistence are implemented. Mixed
   persistence requires explicit `/remote-control HUB_URL --save-layout` on
   each window opening and fresh owner, session and incarnation inventory.
4. **Background delivery:** private browser Web Push with service worker is
   implemented. The Android client project renders transcript and events,
   sends prompts and answers, and uses the existing Serve sign-in. Android SDK
   push source and protocol tests pass. A debug APK build and provisioned device
   QA for Android background FCM push remain open in this environment. Generic
   Android alerts from a live SSE connection are implemented.

Release gates for each stage: opt-in off by default; denied and forged identity
tests; replay, duplicate command and stale approval tests; connection-loss tests;
provider turn and detached daemon checks; Linux and macOS transport verification.
