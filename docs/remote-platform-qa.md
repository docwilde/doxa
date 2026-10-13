# Remote platform acceptance

Three empirical gates remain separate: physical Android/FCM delivery,
two-host private-tailnet recovery, and authenticated macOS provider lifecycle.
Protocol fixtures, offline emulator captures and CI synthetic providers do not
close those gates. This procedure uses disposable synthetic sessions, never
an existing owner conversation. Coordinate any authenticated turns with the
release operator; routing/fleet pilots run only after the completion batch merges.

## Observe prerequisites

Run the read-only collector with existing trusted SSH aliases and an explicit
owner-approved Android serial. Omitting the serial only counts attached devices;
the collector never selects or modifies a device. No APK installation, settings
change, notification send or provider request occurs:

```sh
umask 077
python3 scripts/verify_remote_platform_preflight.py \
  --adb /path/to/android-sdk/platform-tools/adb \
  --android-serial DEVICE_SERIAL \
  --ssh-host HOST_A --ssh-host HOST_B --ssh-host MAC_HOST \
  > /absolute/private/remote-platform-preflight.json
# Expected exit 2: observed prerequisites cannot prove release acceptance.
```

SSH uses normal BatchMode authentication and existing trusted host keys. It runs
an in-memory Python observer without installing files. Commands and their combined
output have time and size bounds; exceptions and raw subprocess output are omitted.
The receipt includes platform, hashed machine/device identifiers, tool availability
and minimal Tailscale state. It excludes login names, peer lists, credentials,
app preferences, tokens, notification bodies and transcripts. Repeated aliases
for one machine cannot satisfy the two-host prerequisite. Tagged devices cannot
satisfy the user-owned Tailscale prerequisite. Android `physical_candidate` is a
conservative ADB observation, not trusted hardware attestation or delivery proof.
Firebase build configuration, account authentication, notification permission and
FCM delivery still need the following checks even when prerequisites are present.

Missing-resource receipt is a valid **unknown** result. To progress, the operator
needs a provisioned user-owned phone with Google Play services, configured APK
and Tailscale; a Firebase project and private hub service account from that project;
two distinct reachable user-owned Tailscale hosts and a private HTTPS hub origin;
and a macOS host with native daemon and normal Claude CLI login. Keep secrets on
their owning host. Do not put them into evidence files or command arguments.

## Physical Android and FCM

Build/configure following [Android README](../android-client/README.md), including
the four Firebase app identifiers and private hub service-account file. Verify
hub service-account ownership and mode through normal administration, without
copying or displaying its contents. Install only on the designated test phone
after operator authorization. Record the source commit and APK SHA-256, SDK,
OS version and hashed device ID beside the observations.

Use one disposable plaintext session, then repeat session access and approval
checks with an encrypted session and a key transferred separately. Use the normal
app UI; never inject a Tailscale login header, Firebase message, FCM token or saved
write marker and call that real delivery evidence.

| Check | Required observation |
| --- | --- |
| Token/registration | App enables alerts for the selected live incarnation; hub accepts the authenticated registration. Record acceptance/lease only, never token. |
| Permission | Decline notifications and confirm no alert display; allow them through normal Android permission flow and repeat. |
| Background FCM | Background app, end its process without putting package into force-stopped state, trigger one synthetic `needs_input` and one `turn_done` through normal daemon/connector events; observe generic notifications on the phone and fresh authenticated state on opening. |
| Rotation | Select a new session/incarnation and repeat; delayed old-tag messages produce no notification. Token refresh must re-register through the real Firebase SDK; a fabricated token does not count. |
| Offline opt-out | Disable while hub unavailable; local routing tag clears immediately and old messages remain ignored. Reconnect and verify opt-out remains disabled. |
| Lease/restart | Foreground return renews an expired/unknown lease. Restart disposable hub; app re-registers and later background event delivers. |
| Tailscale loss | Disconnect/reconnect phone tailnet; stale approvals remain disabled until authoritative snapshot recovery. |
| Process-kill write | Interrupt a real bounded submission after marker commit, end/relaunch process, verify no automatic replay and writes remain blocked until fence, original incarnation snapshot and explicit acknowledgment succeed. |
| Duplicate/stale | Same request ID and body produce one host action; changed same-ID approval request or replaced incarnation refuses the older review. |
| Host loss | Stop only disposable connector/daemon; presence expires, writes refuse, reconnection refreshes complete pending state. |

Android force-stop suppresses normal background message delivery until the user
opens the app again, as documented in [Firebase receive guidance](https://firebase.google.com/docs/cloud-messaging/flutter/receive-messages).
Do not use force-stop as the positive background FCM test.
After reopening, verify registration and fresh delivery again. Keep UI/transcript
captures private and synthetic. A successful Google HTTP send alone does not prove
phone receipt. If delivery, permission, rotation or restart cannot be observed,
that check remains unknown and the overall physical delivery gate remains open.

## Two-host private tailnet and recovery

Build matching source on two disposable hosts. Configure the hub's existing
owner-private Unix socket and kernel-attested Tailscale Serve proxy UID, explicit
login allowlist, and extension origin allowlist when needed. Follow
[remote hub plan](plans/remote-hub.md); do not expose TCP or relax identity policy
to make QA pass. Both connector and client must originate from user-owned devices.
Provisioning a live root service or changing host cgroups requires separate
operator authorization. Use an existing private deployment or disposable guests.

1. On host A, create a synthetic session and connect its outbound connector to
   the private hub. On host B, run `doxa remote list HUB_URL`, then
   `doxa remote tui HUB_URL`. Observe host inventory, transcript and sequenced
   live events across the network. Exercise plaintext browser and separately
   packaged encrypted extension with a separately transferred key.
2. Submit one coordinated bounded prompt, capture exact host acknowledgement,
   and verify one provider turn. Review a synthetic approval using
   `doxa remote review HUB_URL HOST~SESSION REQUEST_ID`; answer from that private
   receipt. Change the pending request or incarnation and verify stale receipt
   refusal before any host action.
3. Interrupt only the disposable connector link while a draft and pending
   review are open. Controls disable immediately. Restore link; inventory and
   complete host snapshot refresh before new writes. Draft survives; no prior
   prompt or answer is automatically replayed.
4. Restart the disposable volatile hub, then connector. Verify new lease,
   fresh snapshot/cursor, no recovered authority from old event buffers, and
   Android uncertain-write fence stays blocked when old delivery cannot be proven.
5. Restart the disposable session under a replacement incarnation. Old tabs,
   saved layouts, review receipts and push tags must not authorize new writes.
   Expire the host lease and verify writes fail while disconnected.

Record source/binary digests on each host, distinct machine hashes, observed
Tailscale/Serve identity path, stage outcomes and request-count evidence. Keep
host leases, shared keys, login data and raw conversations out of published
receipts. Local HTTP/SSE or SSH transport fixtures can establish protocol behavior;
they cannot establish user-owned Tailscale identity or two-host recovery.

## Authenticated macOS lifecycle

On the designated authenticated macOS host, use the existing opt-in harness in
[platform verification](platform-verification.md). Set a short private real-disk
`TMPDIR`, build native daemon, and run:

```sh
DOXA_NATIVE_DAEMON="$PWD/target/debug/doxa-daemon" \
  python3 scripts/verify_macos_authenticated_lifecycle.py --live --provider claude
```

This runs at most two synthetic text-only subscription turns, native event
delivery, stop/exit and same-session resume. Retain its allowlisted JSON receipt
and source/binary digest. Protected Codex is Linux-only; an explicit unknown
receipt on macOS is expected and no Codex process should launch. A Linux run,
CI fixture or installed CLI without authenticated lifecycle observations cannot
close the macOS gate.

## Local regression commands

Use a private, short real-disk `TMPDIR` for Unix sockets, isolated SSD target
directory, two build jobs, and one Rust test thread. These commands do not make
provider requests or send real notifications:

```sh
python3 -m unittest discover -s scripts/tests -p test_remote_platform_preflight.py
python3 -m unittest discover -s scripts/tests -p test_android_recovery_ui.py
python3 -m unittest discover -s scripts/tests -p test_macos_authenticated_lifecycle.py
node scripts/tests/test_remote_browser_recovery.mjs
node --test browser-extension/tests/*.test.mjs
CARGO_BUILD_JOBS=2 RUST_TEST_THREADS=1 \
  cargo test --locked -p doxa-hub -p doxa-remote -p doxa-remote-wire
(cd android-client && ./gradlew --no-daemon --max-workers=2 :protocol:test)
```

With an Android 36 default x86_64 image, emulator and debug APK available, run
the existing fresh-AVD capture with optional offline recovery checks:

```sh
DOXA_CAPTURE_RECOVERY_OUTPUT=/absolute/private/recovery.png \
  scripts/capture_android_home.sh \
  android-client/app/build/outputs/apk/debug/app-debug.apk \
  /absolute/private/home.png
```

This creates and deletes its own offline emulator, verifies clean home UI, then
injects a synthetic body-free marker into that fresh app only. Readable-marker
startup, another process relaunch and corrupted-marker startup must all block
acknowledgment. Screenshot and `.receipt.json` record only offline fixture scope;
no real submission, device delivery or notification is tested. An existing
emulator/occupied port is refused before installation.

Keep local results separate from empirical platform receipts. An unknown external
gate remains open after every local test passes.
