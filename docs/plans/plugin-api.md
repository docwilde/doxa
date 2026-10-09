# Native DOXA plugin API

Status: **data-only v1 and explicit package identity preflight implemented; executable plugins remain open**.
This plan supersedes the Python/Textual `Plugin` and `PANE_COMMANDS` draft. The
Rust frontend uses its own command registry, palette and help panel. Claude Code
plugin adoption through `/plugins` is a separate provider feature.

## Contents

- [Shipped slice](#shipped-slice)
- [Trust boundary](#trust-boundary)
- [Executable package preflight](#executable-package-preflight)
- [Open extension work](#open-extension-work)
- [Acceptance bar](#acceptance-bar)

## Shipped slice

This is a **declarative owner content extension**. The native TUI reads **only explicitly named** TOML manifests from
`$DOXA_HOME/native-plugins/<name>.toml`. An owner's private
`$DOXA_HOME/config.toml` must contain `native_plugins = ["<name>"]`.
Each manifest declares API version 1, identity, version and one to eight static
`/name:verb` commands. A command has a summary and body. It appears in slash
completion, `/help` and the action palette; executing it opens a local,
read-only panel. Arguments are refused. Its content never reaches the model,
daemon or shell. The panel identifies the loaded file, SHA-256 digest and
opened inode/device so the displayed contribution has inspectable provenance.
Alternatively, an allowlisted manifest can declare one `owner-file-v1` status
with a 5–300 second refresh interval. A background worker reads only that
plugin's fixed, private `<name>.status.toml` file (at most 1 KiB). The TUI
shows the validated value and a ledger of attempts, bytes, elapsed time and
failures. The owner is responsible for supervising a producer that atomically
rewrites the file at least every three refresh intervals; DOXA does not launch
it. Stale or future-dated files and expired cached values show unknown. Each
read has a 250 ms budget; after three consecutive failures the
status visibly disables until TUI restart. No plugin code is invoked.
See [Native text plugins](../native-plugins.md) for a working example.

This is intentionally **not** an in-process code plugin API. No `.py`, shared
library, script, hook or command callback is loaded. Unknown manifest fields,
including `exec`, fail closed. The existing Claude Code command inventory is
provider passthrough and cannot override a native command name.

## Trust boundary

- Only the user-level DOXA home supplies the allowlist. The home, config,
  manifest directory and enabled manifest must be owner-owned and private.
  Files must be regular, singly linked and size bounded. The loader opens
  directory-relative files with `O_NOFOLLOW`, rejects a DOXA home inside a
  working repository and never scans the repository.
- Names have a strict ASCII grammar. The manifest identity must match the
  allowlisted filename; command names must use that plugin's namespace.
  Duplicate names, unsupported API versions, unsafe text and unknown fields
  are rejected. One failed manifest cannot activate another manifest's name.
  A native command cannot replace a built-in DOXA command.
- Loading happens once during TUI startup. Failures are shown in the notice
  and `/help`; a malformed plugin does not prevent the TUI from starting.
  Editing a manifest requires restarting the TUI to load a new snapshot. Only
  an accepted status's owner file refreshes during a session.
- A repository cannot provide executable native plugin code because the v1
  protocol contains no executable capability. Never add a repository-relative
  discovery path to a later executable API.

## Open extension work

### Executable package preflight

`doxa native-plugin preflight NAME` now opens only the explicitly named,
owner-private `$DOXA_HOME/native-plugin-packages/NAME` package. It binds a
versioned manifest and bounded WebAssembly core-module file to their SHA-256
digests and opened inodes. The optional owner-private config entry must match
both digests and the requested grants exactly. A mismatch rejects the package;
an absent entry yields a review-required result. No package is loaded at TUI
startup, and neither preflight nor a matching config entry executes code. The
only reserved grant name is `render-local-panel-v1`; no runtime capability is
implemented. See [Native text plugins](../native-plugins.md#executable-package-identity-review)
for the concrete format and owner review flow.

The next execution slice needs a real WebAssembly validator and an isolated,
resource-limited runner with a narrow host protocol, per-invocation identity
recheck, grant enforcement, cancellation and crash reporting. Preflight checks
only file ownership, size, identity and the WebAssembly core header; it cannot
establish module validity or safe execution. Native shared libraries and
in-process callbacks remain out of scope.

### Other extensions

The Python draft also proposed transcript renderers, lifecycle hooks, LORE
access, settings rows and provider backends. None is a Rust-native plugin
contract yet. Dynamic callbacks still need owner-reviewed package identity
and crash isolation; the data-only status ledger does not grant execution.
A WASM or out-of-process protocol should be evaluated against those
requirements; a native shared library would give full process privileges.

Provider backends are a separate architecture decision. Their lifecycle,
credentials, transport and budget controls cannot be inferred from a text
command manifest. The existing Claude Code `/plugins` adoption flow must also
stay separate: it stages user-installed provider commands, skills and agents
for new Claude sessions, not native TUI extensions.

## Acceptance bar

- A manifest in a repository, or an unlisted file in the owner directory,
  contributes nothing.
- Symlinked, group/world-readable, malformed, mismatched-version or
  executable-looking manifests are rejected without running code.
- An accepted command is visible in completion, help and palette, opens a
  nonempty local panel, and never queues a provider prompt.
- A status-only manifest stays owner-allowlisted; malformed, linked, oversized
  stale or loose-permission owner files never display a value. Slow or failing reads
  consume a visible bounded ledger and disable after three failures.
