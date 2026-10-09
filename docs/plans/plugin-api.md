# Native DOXA plugin API

Status: **data-only TUI v1 plus package review and a closed run-request lifecycle; explicit Linux grantless CLI prototype; disposable delegated-host proof passed; installed-host activation open**.
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

`doxa native-plugin preflight NAME` and the Rust TUI's
`/native-plugin preflight NAME` now open only the explicitly named,
owner-private `$DOXA_HOME/native-plugin-packages/NAME` package. It binds a
versioned manifest and bounded WebAssembly 1.0 core-module file to their SHA-256
digests and opened inodes. The optional owner-private config entry must match
both digests and the requested grants exactly. A mismatch rejects the package;
an absent entry yields a review-required result. No package is loaded at TUI
startup, and neither preflight nor a matching config entry executes code. The
TUI validates in a background worker and displays a read-only snapshot with
an explicit CLI run command only for an exactly approved zero-grant package.
`/native-plugin run NAME` starts a cancellable local admission request that
rechecks exact approval and identity, then stops at a closed installed-host
gate. No native slash command can execute a package or reach a provider.
The CLI repeats the approval, byte and inode check at execution. The
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

The explicit `doxa native-plugin run NAME --grantless-prototype` command now
routes an approved zero-grant package through the cgroup-gated child boundary.
It refuses execution when the delegated cgroup controls are unavailable.
Aggregate containment passed on a disposable delegated guest. TUI activation
and broader plugin capabilities still need a reviewed operator path. Native shared libraries and
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

This interpreter core is **not wired into the TUI process**. Unit tests
invoke it in-process; production must not. The separate `doxa-plugin-worker`
binary independently decodes and validates the bounded frame, executes the
grantless module, and emits only the fixed 13-byte result. Child-process tests
cover success, malformed, truncated, oversized and changed-digest requests,
fuel exhaustion and traps. A separate child supervisor starts a caller-built
command in its own process group, bounds captured stdout and stderr to 64 KiB
each, and can send one input frame of at most 8 MiB plus its header through a
nonblocking pipe. It polls a wall deadline and cancellation flag, sends SIGKILL
to the group on every outcome and reaps its leader. Linux Bubblewrap fixtures
cover normal exit, timeout, cancellation, output flood, stalled input, signal
death and no surviving marked descendant.
The fixture also shows a diagnostic limit: Bubblewrap maps a child killed by
SIGKILL to exit code 137, which is indistinguishable from a deliberate exit 137.

A Linux sandbox seam requires an empty, owner-delegated cgroup v2 parent with
`memory`, `pids` and `cpu` enabled for its children, while DOXA runs in a
direct supervisor leaf. Plugin workers receive sibling cgroups; admission
refuses a populated or non-delegated parent before it can build a command. It
installs 256 MiB memory, zero swap, 16 PIDs and one CPU of aggregate bandwidth;
the child also gets address-space, CPU-time, file-descriptor and core-dump
limits. Setup moves the child into the cgroup before Bubblewrap executes,
sets `no_new_privs`, marks ambient descriptors close-on-exec, clears the
environment and uses private mount, PID, user and network namespaces with no
host home or repository mount. The parent kills the entire cgroup on every
outcome, including a descendant that changes process groups. Bubblewrap
fixtures show host files, mounts, a host TCP listener and inherited test
environment are unavailable; resource fixtures show memory and CPU exhaustion
stopped by kernel limits. Without a user-delegated cgroup subtree, admission
refuses to spawn. A disposable delegated guest recorded aggregate limits and
cgroup cleanup; installed-host acceptance remains open.
An [ignored delegated-host acceptance fixture](../native-plugins.md#delegated-host-acceptance-fixture)
now measures the actual aggregate limits, namespace boundary and cgroup
cleanup in one bounded run. The opt-in `scripts/plugin-delegated-host-proof.py`
checks the disposable host's delegated cgroup shape, writable cgroup v2 mount,
fixed tools, Bubblewrap features and namespace support, private real-disk
scratch, and available CPUs before building the real worker and running the
fixture. Its read-only preflight requires distinct network, mount, user and
PID namespace identities and no non-loopback interface or IPv4/IPv6 route;
actual cgroup limit and cleanup proof remains in the opt-in fixture. One case
sends an approved zero-grant Wasm module through the
production review, frame and sandbox path, and rejects stale approval before
spawn. Its boundary case
compares worker network, mount, user and PID namespace identities with the
host and rejects any non-loopback network interface or route. All seven cases,
including cgroup writes, passed on a disposable delegated Ubuntu guest. This
does not authorize TUI execution or nonempty grants.

The sandbox opens the trusted worker executable with `O_NOFOLLOW` and
binds that descriptor into the private mount; replacing its pathname after
open cannot replace the executed inode. It also executes Bubblewrap through
its checked descriptor, with an inode check immediately before `exec`, so a
pathname replacement after validation cannot switch the wrapper. These
checks assume trusted host executables cannot be rewritten in place by
another process with the same user's privileges. Bubblewrap writes a bounded
status receipt to a separate anonymous descriptor. The child emits a fixed entry
marker before reading the module, allowing the parent to distinguish a worker
that fails after entry from sandbox or pre-entry failure. Timeouts,
cancellations, output overflow, malformed responses and module traps have
separate result classes. Bubblewrap cannot distinguish a signalled child from
a deliberate nonzero exit, so those remain one abnormal-worker class. A
setup failure and a worker failure before its entry marker remain one
conservative class. The explicit CLI command now calls this seam with a
five-second deadline and signal-driven cancellation. The TUI run request has
background ownership, bounded result classes and cancellation on panel, tab,
owner and window exit. The TUI worker entry requires a private installed-host
authority value; its production issuer remains closed until delegation and
descendant cleanup are verified for the installed host. There is still no
authenticated installed-host admission, so TUI execution stays disabled. Do
not treat owner approval,
the request frame, fuel or store limits as an execution switch. The handoff is
`recheck_approved(home, review) -> RecheckedPackage`: it returns the exact,
revalidated bytes and fails if either owner file, inode, digest, approval, or
requested grant changed. The encoder consumes those bytes, never a reopened
path.

The explicit `doxa native-plugin host-check` operator command now makes one
read-only observation of the running CLI's delegated cgroup parent and
supervisor membership and the exact opened frontend, worker and Bubblewrap
binary hashes/inodes. It refuses stale worker cgroups and ambiguous or
changing identities. This is a prerequisite report, not a cleanup exercise or
authority token; the production `InstalledHostAuthority` issuer remains closed.

The runnable prototype is an explicit, grantless developer-only CLI command,
never TUI startup or a native slash command. It requires an
approved package with **zero** requested grants and a single exported
`doxa_main: () -> i32`; no WASI, host functions, imports, start function,
ambient credentials, home directory, repository path, or provider connection.
The staged parent seam pipes the `RecheckedPackage` bytes to the dedicated
child rather than passing a module path. The child independently validates the
length, digest, module shape and export signature before instantiation. Only
a fixed-size integer result and bounded failure code return. `render-local-panel-v1`
remains a reserved name until a separate reviewed protocol and grant gate
exist.

The seven-case fixture passed on a disposable host with delegated
controllers. It covered aggregate memory/CPU/PID enforcement, cancellation,
network and host-file denial, stale approval, and cgroup cleanup. A separate
review-only receipt workflow now binds an exact test executable and clean
source tree. Installed-host acceptance and an operator activation policy
remain open. The current TUI request can abandon a stalled private-file read
after setting cancellation, but an atomic fence prevents that read from later
dispatching a worker. Once admitted, panel or window close waits for the
supervisor's cleanup attempt to return. Local tests cover that ordering and a
real process-group cancellation. Successful cgroup cleanup on window exit,
including error paths, still needs installed-host acceptance. Parent deadlines
and module maxima are not hard resource guarantees; other platforms need
equivalent isolation proof.

An opt-in installed-host proof command now pairs the exact installed
frontend, worker and Bubblewrap identities with a clean-source Rust test
executable. Its read-only check precedes any cgroup write. The run requires
operator-supplied expected digests and exercises the seven namespace and
resource cases plus trapped-module and post-allocation launch-error cleanup.
Its private receipt stays review-only; no code consumes it as TUI authority.
The nine-case installed-host run has not been performed.
An operator policy must still authenticate the result, bind the running TUI
instance and define expiry and revocation before the issuer can open.

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
