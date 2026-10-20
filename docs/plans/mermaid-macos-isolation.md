# Mermaid renderer isolation on macOS

Status: **scoped design; no macOS renderer implementation or host acceptance**.
The production renderer still requires Linux bubblewrap. macOS keeps Mermaid
source visible. Linux terminal protocol results do not establish iTerm2 app,
Retina, macOS sandbox, or Apple silicon browser compatibility.

## Proposed first implementation

Use an optional, dedicated Linux renderer VM managed by a small signed macOS
helper. This is a design choice, not a verified implementation. It reuses the
existing reviewed Mermaid CLI and Linux bubblewrap command without granting
the Node/Chromium process host filesystem access. The helper must remain
separate from general fleet/session execution.

Apple's [Virtualization framework](https://developer.apple.com/documentation/virtualization)
supports Linux guests and configurable virtual devices. Its
[networkDevices property](https://developer.apple.com/documentation/virtualization/vzvirtualmachineconfiguration/networkdevices)
defaults to an empty list, and [virtio sockets](https://developer.apple.com/documentation/virtualization/sockets)
provide host/guest communication independently of a guest network interface.
These capabilities support the proposed boundary; they do not prove its
implementation secure.

The helper configuration must have no network devices, shared directories,
clipboard, USB, audio, host sockets, or guest-accessible host paths. Use one
read-only, digest-pinned guest image and a fresh disposable writable layer.
Only the helper opens the image. The guest sees its reviewed package/runtime
and private job directories, never DOXA state, repositories, peer registries,
provider credentials, or the user's home.

Start with a measured target of two virtual CPUs and 2 GiB memory. Keep at most
two admitted jobs, the existing five-second renderer timeout, and a separate
outer watchdog that destroys an unresponsive VM. These are proposed limits;
Chrome startup and two-job memory behavior need measurement before selecting
release defaults. A warm VM may start only after explicit optional setup and
successful doctor preflight. Cold boot/provisioning must not block a TUI draw
or silently extend a renderer job's deadline.

## Broker and wire contract

The trusted host helper owns the VM and opens a single private virtio-socket
connection. The guest cannot request a host file read, network connection,
command, environment variable, or additional device through that connection.
The renderer remains an unprivileged guest process inside the current
networkless bubblewrap boundary.

Use a versioned, bounded byte protocol: a random job nonce, UTF-8 Mermaid source
of at most 16 KiB, source hash and target-column count. Return the same nonce,
a closed success/failure code and one PNG of at most 8 MiB. Reject unknown
fields, lengths, versions, nonces and extra responses before decoding. Accept
no paths, shell commands, arbitrary arguments, renderer selection, or guest
diagnostic text. The host applies the current 8-million-pixel/64-MiB decoder
limits and owns transcript placement and the private PNG cache. Logs contain
fixed categories and hashes, not diagram source or renderer output.

Job cancellation kills the guest renderer's process group and discards its
job directory; timeout, invalid framing or lost transport invalidates the
connection. An outer watchdog must terminate the VM if guest cleanup cannot
be verified. Failure always retains readable source, with no native fallback.

## Packaging and native alternative

Apple requires the [virtualization entitlement](https://developer.apple.com/documentation/virtualization/adding-the-virtualization-entitlement-to-your-project)
for the process using those APIs. The helper must validate framework support,
entitlements, signature identity and the image digest before accepting a job.
Optional setup must display the image download, storage and memory cost before
provisioning; no Node/browser/VM installation belongs in the default installer.

Apple silicon and Intel need independently reviewed guest/browser artifacts.
The current Linux x86-64 Chrome fixture cannot establish arm64 compatibility.
Pin architecture, OS image, Mermaid lockfile, browser and helper identities in
the receipt; no architecture translation or replacement browser is implicit.

A future native implementation could use a separately signed, sandboxed XPC
service with no network or broad-file entitlements. Apple's
[App Sandbox documentation](https://developer.apple.com/documentation/security/app-sandbox)
describes entitlement-controlled access; its
[helper-tool guidance](https://developer.apple.com/documentation/xcode/embedding-a-helper-tool-in-a-sandboxed-app)
requires embedded command-line tools to inherit the sandbox. Merely spawning
an arbitrary installed Node/Chrome executable from the TUI does not supply
this boundary. Chromium's complete subprocess tree, signatures, inherited
rights and writable containers would need a separate proof. Do not ship a
private sandbox-profile wrapper as a substitute for that review.

## Release gates

No macOS rendering setting becomes available until all of these pass on each
supported architecture and minimum OS release:

1. Verify signed helper/image identities and actual device configuration.
   A wrong image, missing entitlement or unsupported host refuses admission.
2. Run hostile renderer fixtures that attempt home/repository/DOXA-state reads,
   host socket and credential access, IPv4/IPv6/DNS/TCP/UDP connections, and
   writes outside the job. Verify denial and absence of host-side sentinel
   reads or traffic. A no-NIC declaration alone is insufficient evidence.
3. Exercise cancellation, forked descendants, timeout, crash, malformed or
   oversized output, stale nonce, duplicate reply and concurrent-job limits.
   Verify VM/process cleanup, bounded resources and source fallback.
4. Render real flowchart, sequence, class and Gantt fixtures; exercise cache
   invalidation, resize and TUI restart. Verify no source/output enters logs
   and no session/provider/peer state is created.
5. Inspect actual iTerm2 app display at normal and Retina scaling, plus one
   source-only terminal. Capture protocol bytes and real screenshots separately
   from the isolation evidence. Test scrolling/clipping and font changes.

Linux protocol tests can prepare fixtures and verify framing, but cannot close
these Mac hardware, signing, containment or display gates. No Mac host was
available for the 2026-10-20 acceptance run. This design is ready for
implementation review only.
