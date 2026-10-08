# DOXA Remote for Android

A small Kotlin/Compose client for the existing private DOXA hub. It lists the
owner's live sessions, loads bounded transcript pages, follows sequenced SSE
events, sends prompts, and answers pending approvals or questions. The hub URL
must be an `https://*.ts.net/` origin reachable from a user-owned Tailscale
device. The app supplies no Tailscale identity header: Serve authenticates the
device and forwards its attested login to the hub.

## Build and connect

Open this directory in Android Studio with Android SDK 37 and JDK 21,
or run `./gradlew :app:assembleDebug` after setting `ANDROID_HOME`. The
wire contract tests also run without the Android SDK via `./gradlew :protocol:test`.
The project uses Gradle 9.3.1, Android Gradle Plugin 9.1.1,
Kotlin/Compose compiler 2.2.10, and the Compose 2026.09 BOM. Install the APK
on a device that is signed into the same private tailnet as the hub.

Enter the private hub origin and select **Choose shared key** if the session
host uses `DOXA_REMOTE_E2EE_KEY_FILE`. Transfer that file to the device through
a separate private channel; select it with Android's document picker. The key
is decoded for this app run and cleared on disconnect or process exit. An
encrypted session cannot be opened without it. Plaintext sessions can be
opened without a key. The app does not store a provider token or host lease.

The app stores the hub URL, selected session ID, last event cursor, and one
unsent draft in app-private preferences. It does not store transcript content,
approval answers, the shared key, or uncertain request bodies. A process
restart reloads an authoritative snapshot before following events again.

## Write and reconnect behavior

Each prompt or answer has a random `request_id`; an uncertain retry sends the
exact same JSON body, including the original encrypted envelope. After the
host's two-minute freshness window, **Retry same request** first reloads the
transcript. The user can review it before choosing **Send new request** and
confirming the new submission.
A new prompt may repeat an action that succeeded before the connection failed;
review the refreshed transcript before confirming. Pending inputs are
refreshed and compared immediately before sending an answer, and the host
checks them again.

The app reconnects SSE from the last processed sequence. A `replay_gap`
reloads the host transcript and pending inputs. Transcript and event text are
bounded in memory and rendered as plain text. The Android client never calls
the daemon or provider directly.

## Local alerts and background push

With explicit permission, the app can show generic Android notifications for
`needs_input` and turn completion while its live SSE connection continues and
the app is hidden. Notifications contain no session ID, transcript, tool
content, or answer action. They are rate-limited per kind. Android can stop the
process and connection at any time, so these are local live alerts, not
reliable background push.

The hub's `/api/push/subscriptions` endpoint accepts browser Web Push
subscriptions with endpoint and `p256dh`/`auth` keys. A native Android FCM
registration token is a different protocol and cannot use that endpoint.
Reliable Android push needs a Firebase project configuration in the app and
an authenticated FCM sender on the hub. Neither is configured here, and the
app does not register a token or add a token-only endpoint that cannot send.

## Scope and verification

There is no Android background push registration, background service, persistent key,
file browser, or host command endpoint in this client. The debug APK builds
with SDK 37.0 and its protocol tests pass. It has not yet been installed on a
device or exercised against a two-host private tailnet. The remaining gate is
a device test covering
Tailscale authentication, encrypted/plaintext sessions, reconnect, duplicate
request, stale approval, and host loss.

The build versions follow the [Android Compose setup guide](https://developer.android.com/develop/ui/compose/setup-compose-dependencies-and-compiler),
[AGP 9.1 compatibility table](https://developer.android.com/build/releases/agp-9-1-0-release-notes),
and [Gradle wrapper guidance](https://docs.gradle.org/current/userguide/gradle_wrapper.html).
The push boundary follows [Firebase's Android registration guidance](https://firebase.google.com/docs/cloud-messaging/android/get-started)
and [FCM server authorization requirements](https://firebase.google.com/docs/cloud-messaging/send/v1-api).
