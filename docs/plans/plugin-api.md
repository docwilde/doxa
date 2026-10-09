# Native DOXA plugin API

Status: **data-only v1, package preflight and an unwired runner contract implemented; executable plugins remain open**.
This plan supersedes the Python/Textual `Plugin` and `PANE_COMMANDS` draft. The
Rust frontend uses its own command registry, palette and help panel. Claude Code
plugin adoption through `/plugins` is a separate provider feature.

## Contents

- [Shipped slice](#shipped-slice)
- [Trust boundary](#trust-boundary)
- [Open extension work](#open-extension-work)
- [Executable package preflight](#executable-package-preflight)
- [Runner design and test seam](#runner-design-and-test-seam)
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
versioned manifest and bounded WebAssembly 1.0 core-module file to their SHA-256
digests and opened inodes. The optional owner-private config entry must match
both digests and the requested grants exactly. A mismatch rejects the package;
an absent entry yields a review-required result. No package is loaded at TUI
startup, and neither preflight nor a matching config entry executes code. The
only reserved grant name is `render-local-panel-v1`; no runtime capability is
implemented. See [Native text plugins](../native-plugins.md#executable-package-identity-review)
for the concrete format and owner review flow.

Preflight now uses pinned `wasmparser` validation of the complete module and
refuses imports, start functions and features outside WebAssembly 1.0. A fresh
`recheck_approved` re-opens the owner files, requires exact digest, grant and
inode matches against the earlier approved review, and retains the validated
module bytes so a future runner need not race a second path open. Validation
also rejects any memory or table without a declared maximum, memory above
256 WebAssembly pages (16 MiB), and tables above 1,024 entries. These are
static admission bounds, **not runtime enforcement**. Validation does not
make the module safe to run.

The next execution slice needs an isolated, resource-limited runner with a
narrow host protocol, grant enforcement, cancellation and crash reporting.
Preflight cannot establish safe execution. Native shared libraries and
in-process callbacks remain out of scope.

### Runner design and test seam

Wasmi 2.0.0 is now pinned in the workspace. A private worker core validates
the exact WebAssembly 1.0 bytes again, requires a single exported
`doxa_main: () -> i32`, instantiates without imports, and runs with
5,000,000 fuel, a 16 MiB memory store limit, a 1,024-entry table limit
and one instance. The bounded request frame carries at most 8 MiB, a
protocol version and SHA-256 digest. The response is exactly 13 bytes:
a protocol version, one result code and one integer. The encoder accepts only
a freshly rechecked, owner-approved package with **zero grants**. Focused tests exercise
a return value, infinite loop fuel exhaustion, trap, wrong signature, memory
growth at the cap, changed approval and malformed request/response frames.

This interpreter core is **not wired to a command or TUI path**. Its current
tests invoke it in-process; doing that with an untrusted package in production
would be unsafe. A separate, unwired child supervisor now starts a caller-built
command in its own process group, bounds captured stdout and stderr to 64 KiB
each, polls a wall deadline and cancellation flag, sends SIGKILL to the group
on every outcome and reaps its leader. Linux tests use a Bubblewrap fixture
when user namespaces are available. They cover normal exit, timeout,
cancellation, output flood, signal death and no surviving marked descendant.
The fixture also shows a diagnostic limit: Bubblewrap maps a child killed by
SIGKILL to exit code 137, which is indistinguishable from a deliberate exit 137.

This supervisor accepts a caller-built command; it does not prove that command
is sandboxed. There is still no child launcher with an enforceable OS memory
and CPU budget, a process limit, file/network isolation tests or containment
of a compromised descendant that changes process groups. Child and wrapper
crashes need a reliable distinction. Thus DOXA has **no runner
command, child binary, or plugin execution path**. Do not treat owner approval,
the request frame, fuel or store limits as an execution switch. The handoff is
`recheck_approved(home, review) -> RecheckedPackage`: it returns the exact,
revalidated bytes and fails if either owner file, inode, digest, approval, or
requested grant changed. The encoder consumes those bytes, never a reopened
path.

The first runnable prototype should be an explicit, grantless developer-only
command, never TUI startup or a native slash command. It should require an
approved package with **zero** requested grants and a single exported
`doxa_main: () -> i32`; no WASI, host functions, imports, start function,
ambient credentials, home directory, repository path, or provider connection.
The parent must pipe the `RecheckedPackage` bytes to a dedicated child rather
than passing a path. The child must independently validate the length, digest,
module shape and export signature before instantiation. Only a fixed-size
integer result and bounded failure reason may return. `render-local-panel-v1`
remains a reserved name until a separate reviewed protocol and grant gate
exist.

The remaining step is to run this core only in a separate process whose
sandbox setup fails closed. On Linux, prove a private mount and network
namespace, no inherited secrets or writable host mounts, `no_new_privs`,
a process/cgroup memory and CPU budget, and a file-descriptor/process limit.
The staged supervisor already bounds stdout/stderr, applies an independent
wall deadline, and kills the owned group; a real launcher must also bound stdin,
close inherited descriptors and report timeout, cancel, trap, crash and sandbox
failure distinctly. Neither a parent-side timeout nor the module's declared
maximum is a hard resource guarantee. Other
platforms stay unavailable until equivalent isolation is proven.

The acceptance fixture must run a valid return module, an infinite loop,
memory growth at the cap, a trap and a crashing child; exercise cancellation
and attempt file/network access from a compromised child, checking that the
sandbox prevents it. Verify no orphan remains after timeout/cancellation,
that oversized/truncated frames fail closed, and that stale approval refuses
the child spawn. These are prerequisites for a future prototype, not claims
about current production support.

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
