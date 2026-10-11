# Remote review fixes

Review target: beta47 (`e682d029799c80791b426a61ac5ee74d30a908cf`). This note records the implementation for F-REMOTE-001, F-REMOTE-003, F-REMOTE-004, and F-REMOTE-005. Rust builds and tests are delegated to the root verification lane; this worktree has not run Cargo or rustc.

## F-REMOTE-001: normalize and authorize the root page

`doxa-peers::remote_policy::route_segments` maps `/` to an empty route, and both HTTP daemons now use that shared helper. Their existing `GET` empty-route branch can therefore serve the packaged UI. The route still goes through the same attested-proxy identity and allow-list authorization as other status reads before static bytes are returned. In `doxa-remote`, that check and static page dispatch are factored into `authorize_route` and `static_route` so the focused test exercises the same path decisions as the handler. The hub test runs the actual in-memory HTTP service for anonymous, claimed-but-unattested, and attested allow-listed identities.

Regressions: `cargo test -p doxa-peers route_segments_normalize_root_without_aliasing_interior_separators`; `cargo test -p doxa-remote root_page_requires_attested_allowlisted_identity`; `cargo test -p doxa-hub root_page_is_denied_without_identity_and_retrievable_by_attested_owner`.

## F-REMOTE-003: bind event ciphertext to one session incarnation

Encrypted event AAD now uses `target|event-v2|incarnation|seq|event_type` in the Rust publisher, browser extension, and Android client. The hub requires encrypted event frames to carry the current inventory incarnation and its stream path emits a replay gap if a stored encrypted frame belongs to an older incarnation. Both clients require the expected incarnation and verify the same context before delivering decrypted event content. The browser and Android continue to obtain the expected incarnation from their selected session snapshot; this change does not remove the existing captured-review checks before an answer is submitted.

`test-vectors/remote-event-v2.json` is a synthetic shared AES-GCM fixture opened by the Rust wire crate, extension, and Android protocol tests. Browser and Android regressions capture ciphertext under incarnation A, restart to B, edit the broker-visible frame incarnation to B, then verify the client rejects the old ciphertext before delivering it. The hub state regression covers missing, stale, and matching incarnation metadata.

Regressions: `cargo test -p doxa-remote-wire event_context_is_versioned_and_incarnation_bound`; `cargo test -p doxa-remote-wire shared_event_vector_opens_with_rust_wire`; `cargo test -p doxa-hub encrypted_sessions_reject_plaintext_commands_and_events`; `node --test browser-extension/tests/crypto.test.mjs`; `./gradlew :android-client:protocol:test --offline`.

Compatibility: `event-v2` is a coordinated wire-protocol change. Update the Rust publisher, browser extension, and Android client together. Legacy v1-AAD ciphertext is deliberately unsupported: clients authenticate only the v2 context, so old frames fail AEAD verification and are not delivered.

## F-REMOTE-004: cap finite Android requests and close sockets on cancellation

Each finite Android HTTP exchange now has a 30-second wall-clock deadline covering response headers and body reads; expiry disconnects the active `HttpURLConnection` and reports a timeout. A pending command's POST plus acknowledgement polling is capped at 120 seconds. Coroutine cancellation registers an immediate disconnect hook for finite requests and SSE. SSE remains long-lived while active and does not receive the finite-request timer; cancelling its owning coroutine closes the connection so a blocking line read can finish.

The protocol tests inject local fake `HttpURLConnection` instances. One blocks a finite response until the absolute deadline and checks disconnect; another blocks an SSE read, cancels the coroutine, and checks prompt disconnect. They make no network request. The protocol Gradle test task passed offline; device lifecycle and platform TLS acceptance were not run in this review lane.

Regression: `./gradlew :android-client:protocol:test --offline`.

## F-REMOTE-005: remove the RSA advisory dependency path

`web-push-native` is now built with default features disabled, so its optional `vapid` feature no longer enables `jwt-simple`. The hub signs VAPID ES256 tokens directly with the existing 32-byte P-256 key format, preserving the 300-second TTL, audience, `mailto:` subject, and uncompressed public key. These parameters follow [RFC 8292](https://datatracker.ietf.org/doc/html/rfc8292), which requires P-256/ES256 and binds the audience to the push resource origin. A focused test verifies the emitted JWT signature with the matching P-256 public key and checks the audience and subject.

FCM service-account assertions use `ring::signature::RsaKeyPair` over PKCS#8 PEM. The parser accepts only the normal `BEGIN/END PRIVATE KEY` PKCS#8 form used by the service-account configuration; the signer emits the existing RS256 OAuth claims and request. This follows [Google's service-account OAuth flow](https://developers.google.com/identity/protocols/oauth2/service-account) and [FCM HTTP v1 authorization requirements](https://firebase.google.com/docs/cloud-messaging/send/v1-api). A test-only synthetic key checks parsing, JWT claims, signature length, and rejection of tampered inputs. In ring 0.17.14, `RsaKeyPair::sign` calls the `elem_exp_consttime` private exponentiation path and checks the CRT result with public exponentiation before returning ([ring 0.17.14 source](https://docs.rs/crate/ring/latest/source/src/rsa/keypair.rs)). Root resolved the shared lock without `rsa`, `jwt-simple`, or `superboring` and reports zero RustSec vulnerability entries. This does not claim that an attacker had a reachable remote RSA oracle; FCM remains optional and requires owner-supplied private configuration.

Regressions: `cargo test -p doxa-hub push::tests::generated_push_request_encrypts_generic_event_and_signs_vapid`; `cargo test -p doxa-hub fcm::tests::ring_signs_bounded_fcm_oauth_assertion_from_pkcs8`; `cargo tree -p doxa-hub -i rsa`; workspace advisory audit. The root verification lane must resolve the shared lockfile and confirm the dependency tree; this worktree did not run Cargo. No live FCM or push-service delivery was attempted.

## Verification limits

The browser crypto tests passed (`node --test browser-extension/tests/crypto.test.mjs`, 7/7); Android protocol tests passed (`cd android-client && ./gradlew --offline --no-daemon :protocol:test`); `git diff --check` and JSON fixture parsing passed. Rust compilation, Rust tests, workspace lock resolution, dependency audit, device acceptance, and live FCM acceptance remain for the root verification lane. The RSA dependency finding is a supply-chain issue from the advisory scan; these fixes do not imply a demonstrated network timing oracle in DOXA.
