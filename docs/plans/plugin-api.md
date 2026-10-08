# Native DOXA plugin API

Status: **data-only v1 command slice implemented; executable plugins remain open**.
This plan supersedes the Python/Textual `Plugin` and `PANE_COMMANDS` draft. The
Rust frontend uses its own command registry, palette and help panel. Claude Code
plugin adoption through `/plugins` is a separate provider feature.

## Contents

- [Shipped slice](#shipped-slice)
- [Trust boundary](#trust-boundary)
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
  Editing a manifest requires restarting the TUI to load a new snapshot.
- A repository cannot provide executable native plugin code because the v1
  protocol contains no executable capability. Never add a repository-relative
  discovery path to a later executable API.

## Open extension work

The Python draft proposed status chips, transcript renderers, lifecycle hooks,
LORE access, settings rows and provider backends. None is a Rust-native plugin
contract yet. The next useful extension is a bounded, read-only status value
whose producer and refresh cost are explicit. Dynamic in-process callbacks
need an owner-reviewed package identity, crash isolation and a user-visible
failure ledger before loading can be considered. A WASM or out-of-process
protocol should be evaluated against those requirements; a native shared
library would give full process privileges.

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
- A later callback or status API needs an explicit failure budget, a visible
  disabled state and tests for slow/failing contributors before it is called
  implemented.
