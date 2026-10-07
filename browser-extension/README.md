# DOXA Remote browser extension

This Chrome/Chromium Manifest V3 extension is an installed client for encrypted
DOXA hub sessions. Its HTML and JavaScript are packaged with the extension;
the hub supplies data, not executable code. The extension implements the same
bounded AES-256-GCM and raw DEFLATE envelope as `doxa-remote-wire`.

## Install and connect

1. On the session host, enable the private hub connector and set
   `DOXA_REMOTE_E2EE_KEY_FILE` to an owner-only key created with
   `doxa remote keygen /absolute/private/remote.key`. Copy that file to the
   browser computer through a separate secure channel.
2. In Chrome/Chromium, open `chrome://extensions`, enable **Developer mode**,
   choose **Load unpacked**, and select this `browser-extension` directory.
   A release ZIP must first be extracted to a stable private directory.
3. Copy the extension ID shown on its card. On the hub, set
   `DOXA_REMOTE_EXTENSION_ORIGINS=chrome-extension://EXTENSION_ID` and restart
   `doxa-hub`. Multiple IDs may be separated by commas. The existing Tailscale
   proxy UID and `DOXA_REMOTE_ALLOWED_LOGINS` checks remain required.
4. Click the DOXA extension icon. Enter the private `https://*.ts.net/` hub
   URL, choose the copied key file, and select **Connect**. Chrome asks for
   access to that one hub origin. The extension lists and opens encrypted
   sessions, including transcript pages, live events, prompts and approvals.

The key is imported into a nonextractable Web Crypto key for this tab. DOXA
does not save it in extension storage; reopening or reloading the tab requires
choosing the file again. The hub URL alone is saved locally. If the unpacked
extension's ID changes, update the hub's origin allowlist.

Package a release artifact with
`scripts/package-browser-extension.sh /absolute/output.zip`. The ZIP contains
only the runtime files and icon, never tests, fixtures or a key.

## Security boundaries

- The hub checks the exact configured extension origin for cross-origin writes.
  This is a browser access rule, not an identity credential. The Tailscale
  identity and owner allowlist still authorize every hub request.
- The extension refuses plaintext sessions. The hub-served browser remains
  available for plaintext sessions, but cannot open encrypted ones because
  the hub controls that page's JavaScript.
- The hub still sees session presence, target IDs, operations, event kinds,
  timing and padded ciphertext sizes. It can delay or drop traffic. Native host
  replay checks expire commands after two minutes; a connector restart within
  that window cannot rule out every replay.
- This release uses one manually copied symmetric key. It has no per-device
  pairing or revocation. To revoke a device, rotate the shared key on every
  remaining endpoint. A compromised extension package or browser can read
  plaintext while in use.
- Chrome/Chromium desktop is supported. Android browsers are not covered by
  this extension; a later packaged Android app can reuse the wire protocol.
