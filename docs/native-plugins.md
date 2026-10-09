# Native text plugins

Native data plugins add local, read-only slash commands or status values to the Rust TUI. They
do not run code or contact a provider. Claude Code plugin adoption under
`/plugins` is separate.

Put this in your private `$DOXA_HOME/config.toml` (normally
`~/.doxa/config.toml`):

```toml
native_plugins = ["team"]
```

Create `$DOXA_HOME/native-plugins/team.toml`:

```toml
api_version = 1
name = "team"
version = "1.0"

[[commands]]
name = "/team:status"
summary = "Show the team's current handoff"
body = "The handoff is in the shared project tracker."
```

Make the DOXA home and `native-plugins` directory private (`chmod 700`), and
both TOML files private (`chmod 600`). Start a new TUI. Type `/team` to find
the command, or open `/help` or the action palette. `/team:status` displays the
body with its source path and content digest. It accepts no arguments and
never sends a prompt to an agent.

The DOXA home must be outside a Git working repository. DOXA never searches
the current project for a manifest.

The allowlist is explicit. Other manifests in the directory are ignored.
Enabled manifests must match the filename and use `/name:verb` command names.
The loader rejects symlinks, loose permissions, unsupported API versions,
unknown fields and control characters. A rejected plugin is reported in the
TUI notice and `/help`; fix the file and restart the TUI. The limit is 16
plugins, eight commands per plugin and 4 KiB of text per command.

For a status-only plugin, replace the `[[commands]]` section with:

```toml
[status]
producer = "owner-file-v1"
label = "Queue"
refresh_seconds = 30
```

The owner must arrange and supervise an external producer process; DOXA never
starts or manages it. That process should atomically replace the private
`$DOXA_HOME/native-plugins/team.status.toml` file:

```toml
value = "Ready"
```

The producer must rewrite the file at least once every three configured refresh
intervals, even when its value has not changed. Older or future-dated files
fail closed, and a cached value also disappears after three intervals without a
successful refresh. DOXA reads this file as data only: at most 1 KiB per
attempt and 96 bytes of display text, every 5–300 seconds as configured, in a background worker with a
250 ms read budget. Click
the status chip for its source digest and a refresh ledger (attempts, bytes,
time, failures and cache expirations). Three consecutive failed or slow reads disable that status
until restart; the chip then says **disabled**. Missing, linked, loosely
permissioned, malformed and control-character values count as failures. The
status never enters an agent prompt or runs plugin code.
At most eight allowlisted statuses are active. The same manifest may also
declare static commands.

## Executable package identity review

The `native-plugin` commands let the owner inspect a proposed WebAssembly
package and, on Linux, explicitly run a zero-grant prototype. They do not
change the data-only `native_plugins` list or activate anything in the TUI.
The command reads only the name you provide; it never searches a repository or
enumerates packages.

Put private files at `$DOXA_HOME/native-plugin-packages/demo/manifest.toml`
and `module.wasm` (owner-only directories and files). The manifest is:

```toml
package_api_version = 1
name = "demo"
version = "1.0"
artifact_format = "wasm-core-v1"
requested_grants = ["render-local-panel-v1"]
```

Run `doxa native-plugin preflight demo`. It prints the exact manifest and module
SHA-256 digests, requested grants, opened inode/device identities, and review
state. The only recognized proposed grant is `render-local-panel-v1`; it is a
reserved name, not a capability the current DOXA grants or exercises. The
command fully validates a WebAssembly 1.0 core module using the pinned
`wasmparser` validator. Imports and start functions are refused because no host
ABI or safe startup contract exists. Newer WebAssembly proposals are also
refused until a runner explicitly supports them. Modules that contain memory
or tables must declare maxima no greater than 16 MiB and 1,024 entries. Those
static bounds do not enforce runtime use.

In the Rust TUI, `/native-plugin preflight demo` opens the same read-only,
explicit-name review in a local panel. Package validation runs in a background
thread so it cannot stall terminal input. A matching zero-grant review displays
the exact CLI command below; the panel is a snapshot, and the CLI rechecks
approval, bytes and inodes before its cgroup/Bubblewrap admission.

`/native-plugin run demo` now opens a local lifecycle panel. Its background
task validates exact owner approval, refuses every requested grant, and rechecks
the reviewed bytes and inodes. The installed-host admission gate is closed, so
this request reports why execution is unavailable and never starts the worker.
Closing the panel or its tab, changing tabs, or exiting the window cancels the
task and drops late results. Cancellation races with admission through one
atomic fence: a cancelled preflight cannot later dispatch a worker, while an
already admitted request blocks tab/window exit until its supervisor returns
from its cleanup attempt. A stalled read before admission may outlive its panel
but cannot cross that fence. This exit contract has local process-group tests;
an installed-host proof of successful cgroup cleanup, including error paths,
is still required before activation. A future installed-host acceptance policy must
authenticate the current TUI/worker/Bubblewrap identities, delegated cgroup
parent and membership, kernel namespace/egress enforcement, current-host proof
freshness, and revocation independently of owner-editable config or a
disposable-guest receipt. Until then, the explicit Linux CLI is the only
grantless execution route; TUI execution stays disabled.

`doxa native-plugin host-check` is a read-only operator inventory on Linux.
It requires the running CLI to occupy a direct supervisor leaf beneath an
empty, owner-delegated cgroup v2 parent with `memory`, `pids` and `cpu`
controllers enabled. It refuses leftover `doxa-plugin-*` children, unsafe or
linked executables, and replacement during each binary's identity read. The
three binaries are checked sequentially: a pathname can change after its
check, so the report is not an atomic snapshot. It records the parent's and
supervisor's device/inode identities plus SHA-256,
size and device/inode for the opened frontend, companion
`doxa-plugin-worker` and `/usr/bin/bwrap` binaries. It does not create a
cgroup, launch a worker or issue TUI authority.

For an installed-host acceptance review, retain this report together with a
fresh seven-case proof log and receipt from the **same** delegated host.
Compare the report's worker and Bubblewrap digests with the receipt's
`worker_sha256` and `bwrap_sha256`, and its delegated parent identity with
`host.cgroup`. The acceptance must also bind the exact installed frontend
binary and its current supervisor membership, exercise return, timeout,
cancellation and error-path descendant cleanup, and define revocation before
an authority issuer can be implemented. The existing proof command builds a
worker from a clean checkout and records review evidence; neither its receipt
nor a successful `host-check` activates the TUI.

After reviewing both files and the requested grant, the owner can record the
exact identity in private `$DOXA_HOME/config.toml`:

```toml
[[native_plugin_packages]]
name = "demo"
manifest_sha256 = "<64 lowercase hexadecimal digits from preflight>"
module_sha256 = "<64 lowercase hexadecimal digits from preflight>"
grants = ["render-local-panel-v1"]
```

Preflight then reports an exact approval match. Changed bytes or grants fail
closed; an unapproved package remains review-only. Approval alone is not an
execution switch. The digests pin local bytes; they do not authenticate a publisher. A
fresh `recheck_approved` call re-opens both files, checks their digests, opened
inodes and current owner approval against the earlier review, and returns the
validated module bytes. The grantless runner uses those bytes without re-opening
a path and isolates crashes and resource use; nonempty grants remain unavailable.

For an approved package whose `requested_grants` and config `grants` are both
empty (`[]` in both files), run
`doxa native-plugin run demo --grantless-prototype`. This explicit
CLI path rechecks the exact approved bytes, then calls only the dedicated
`doxa-plugin-worker` through the cgroup-gated Bubblewrap sandbox. It has no
home or repository mount, network, host functions or credentials; IPC and
output are bounded, the wall deadline is five seconds, and Ctrl-C, termination
or hangup requests cgroup cleanup. It prints a single integer on success or a
conservative failure class on error. The caller must run in a supervisor leaf
below an empty, user-owned delegated cgroup v2 parent with memory, CPU and PID
controllers enabled for its children. Each worker gets a sibling cgroup under
that parent. Without this layout, admission fails closed before spawning the
worker. A disposable delegated Ubuntu guest passed the seven-case acceptance
fixture, including the approved Wasm worker, aggregate limits, namespace
boundary and cleanup. TUI execution, nonempty grants, native shared
libraries, scripts, provider backends, hooks and
automatic startup remain unsupported.

Cleanup treats `cgroup.events` as bounded evidence: it requires exactly one
valid `populated` value before removing a worker cgroup. Missing, duplicated,
malformed, linked or oversized event data fails closed instead of reporting
cleanup success. This strengthens the CLI cleanup path but is not an
installed-host acceptance receipt or TUI execution authorization.

Before broader activation, a delegated-host acceptance run must verify the
installed memory, swap, CPU and PID limits; worker membership before exec;
aggregate limits under fork and process-group escape attempts; no host file or
network access; and removal of all descendants after return, timeout and
cancellation. The disposable guest passed these checks; broader activation
still requires a reviewed operator path and installed-host acceptance.

### Delegated-host acceptance fixture

On a disposable Linux host, delegate a private cgroup v2 parent to the test
user, leave its `cgroup.procs` empty, enable `memory`, `pids` and `cpu` in its
`cgroup.subtree_control`, and run the test process in a direct child named
`supervisor`. The fixture creates each limited worker in a sibling of
`supervisor`. A process in the delegated parent itself cannot enable the
domain controllers required by cgroup v2. For a systemd-managed disposable
host, a transient service with `User=<test user>`, `Delegate=yes` and
`DelegateSubgroup=supervisor` can provide this shape; the service must still
enable the three controllers in its empty delegated parent before testing.
Do not create cgroups under a systemd-owned, non-delegated slice. The host also
needs a writable cgroup v2 mount, a non-root test user, working unprivileged
user and network namespaces, Bubblewrap with `--ro-bind-fd` and
`--json-status-fd`, Python 3.9 or newer, the fixed `/usr/bin` fixture tools,
Rust, two available CPUs, and a private real-disk scratch directory. Use a
disposable machine with at least 1 GiB of spare memory: the proof deliberately exercises cgroup OOM,
PID denial and CPU throttling. The read-only preflight checks the mount,
caller membership, empty delegated parent, enabled controllers, executable,
tools and scratch prerequisites. It also starts an unprivileged Bubblewrap
smoke probe and requires distinct network, mount, user and PID namespace
identities, only loopback interfaces and only loopback IPv4/IPv6 routes. This checks
admission prerequisites; cgroup limit enforcement and descendant cleanup are
measured only by the opt-in acceptance run. Run from the repository checkout:

```sh
mkdir -p "$HOME/t" "$HOME/ssd-cache"
chmod 700 "$HOME/t"
export TMPDIR="$HOME/t"
export CARGO_TARGET_DIR="$HOME/ssd-cache/doxa-plugin-acceptance-target"
python3 scripts/plugin-delegated-host-proof.py --check
DOXA_PLUGIN_CGROUP_ACCEPTANCE=1 DOXA_PLUGIN_DISPOSABLE_HOST=1 \
  python3 scripts/plugin-delegated-host-proof.py --run --receipt plugin-proof.json
```

`--run` requires both explicit environment switches, an absolute real-disk
`CARGO_TARGET_DIR`, and a new receipt name inside the private `TMPDIR`. It
refuses a dirty source checkout or an existing receipt. It repeats the preflight, builds the actual
`doxa-plugin-worker`, builds and hashes the exact Rust test executable, then
runs that opened executable by descriptor. Build output is limited to 8 MiB
with a 15-minute deadline; the seven-case proof output is limited to 128 KiB
with a 45-second deadline. Deadline or output-limit failure kills the proof
process group and attempts bounded cleanup of worker cgroups. It creates only per-worker child cgroups in
the delegated parent; it does not configure systemd, enable controllers or
change host networking. Missing prerequisites or proof observations fail the
run, rather than skipping it. The fixture uses the same descriptor-mounted
launcher, private cgroup and process supervisor as the grantless CLI. Each
case prints one bounded counter summary, followed by `cleanup=removed` only
after `cgroup.kill` empties and removes the group.

The seven cases include an approved zero-grant Wasm module returning 17 through
the production review, frame, worker and sandbox path; stale approval is
refused before a worker cgroup is created. The other six cases check distinct
network, mount, user and PID namespace identities;
no non-loopback interface or route; host file, environment and TCP isolation;
the installed 256 MiB memory, zero swap, 16 PID and one-CPU quotas; actual
worker cgroup membership; aggregate PID denial and memory OOM under multiple
children; CPU throttling; a `setsid` descendant killed on cancellation; and
timeout cleanup.
The proof validates all seven case lines and their cleanup markers, checks that
no plugin worker cgroup remains, and writes a private JSON receipt only after
the Rust test passes. The receipt binds the clean Git commit and tree, SHA-256
of the staged worker, Bubblewrap binary and exact Rust test executable,
toolchain versions and executable hashes, a digest of build-affecting environment
settings, Cargo config hashes, delegated cgroup parent and supervisor
device/inode, host details, seven observed cases and SHA-256 of the
bounded test log. The terminal prints the receipt SHA-256; retain that line and
the test log separately so reviewers can detect later receipt or log changes.
This is operator evidence, not a signature or an activation token. No TUI code
reads the receipt, and TUI execution remains disabled. A different installed
host, worker build or cgroup fixture needs its own proof and review.
