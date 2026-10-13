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
package and, on Linux, explicitly run a zero-grant package behind installed-host
authority. They do not change the data-only `native_plugins` list.
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

`/native-plugin run demo` opens a local lifecycle panel. Its background task
validates exact owner approval, refuses every requested grant, and rechecks the
reviewed bytes and inodes. Execution also requires the enrolled operator's
current signed permit for this running frontend. A missing, expired, revoked,
wrong-instance or changed-host permit refuses dispatch. Closing the panel or
its tab, changing tabs, or exiting the window cancels the task and drops late
results. One atomic fence prevents a cancelled preflight from dispatching;
once admitted, tab/window exit waits for the supervisor's cgroup cleanup.
A stalled read before admission may outlive its panel but cannot cross the
fence. Permit revocation and expiry also request cancellation while the worker
is running. No code runs in the TUI process.

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

The read-only report and proof receipts are evidence only. A successful report
or an owner package approval cannot authorize TUI execution. The separate
activation workflow below reruns all nine installed-host cases before signing
a short-lived permit for a specific running frontend.

### Installed-host proof and operator activation

The installed-host proof is an explicit operator command. Its `--check` mode
observes the current delegated cgroup, runs a private Bubblewrap namespace
smoke probe, executes the installed frontend's `host-check` through its opened
descriptor, and compares that report with independently opened frontend,
worker and Bubblewrap identities. It makes no cgroup changes:

```sh
export TMPDIR="$HOME/t"
python3 scripts/plugin-installed-host-proof.py --check --frontend /absolute/path/to/doxa-rs
```

Keep the three reported SHA-256 digests for independent review. After the
owner approves an acceptance exercise on a delegated host, `--run` requires
those exact expected digests, an absolute real-disk `CARGO_TARGET_DIR`, a
clean source checkout, and both explicit operator switches. It builds only
the proof test executable and launches the installed worker through the normal
opened-inode sandbox path. The fixture
checks the existing seven containment cases, then an approved Wasm trap and
a launch failure after cgroup allocation. Each case must remove its worker
cgroup. The harness bounds build output and time, proof output and time, and
emergency cleanup to groups named for its proof process. It writes an
exclusive private receipt only after all nine cases pass:

```sh
export TMPDIR="$HOME/t"
export CARGO_TARGET_DIR="$HOME/ssd-cache/doxa-plugin-installed-target"
DOXA_PLUGIN_INSTALLED_HOST_ACCEPTANCE=1 DOXA_PLUGIN_OPERATOR_GO=1 \
  python3 scripts/plugin-installed-host-proof.py --run \
    --frontend /absolute/path/to/doxa-rs \
    --expected-frontend-sha256 <reviewed-frontend-sha256> \
    --expected-worker-sha256 <reviewed-worker-sha256> \
    --expected-bwrap-sha256 <reviewed-bwrap-sha256> \
    --receipt installed-plugin-proof.json
```

The run creates and removes only per-worker children of an already delegated
cgroup parent. It does not configure delegation or systemd. The memory, PID
and CPU cases deliberately exhaust their worker budgets, so run them only on
an owner-approved acceptance host with spare capacity. A receipt records
sampled executable identities, cgroup identity and clean source identity.
The pathname checks before and after the exercise are sequential, not an
atomic snapshot or a promise that a binary cannot be replaced between cases.
A `--run` without `--activate` stays review-only. Its receipt cannot be used
as a permit, and there is no receipt-import activation command.

To activate one running TUI after a fresh proof, first generate an Ed25519
operator key outside the source checkout. Keep its PEM private (`0600`) and
retain its public key for independent owner review:

```sh
umask 077
openssl genpkey -algorithm ED25519 -out "$HOME/plugin-operator.pem"
openssl pkey -in "$HOME/plugin-operator.pem" -pubout -outform DER | tail -c 32 | od -An -tx1
```

Join the 32 public-key bytes into 64 lowercase hexadecimal characters, review
them, and explicitly enroll that operator in private `$DOXA_HOME/config.toml`:

```toml
[native_plugin_host]
operator_public_key = "<64 lowercase hexadecimal public-key characters>"
epoch = 1
```

The signing key is separate from owner package approval and is never passed to
the worker. The operator and target TUI must run as the same non-root user in
the same delegated supervisor leaf. Add these options to the complete `--run`
command above, using a new receipt filename:

```sh
--activate --tui-pid <running-doxa-rs-pid> --home "$DOXA_HOME" \
  --operator-key "$HOME/plugin-operator.pem" --lifetime-seconds 900
```

Activation requires Python 3.11+, OpenSSL Ed25519, all nine freshly passing
cases, exact independently reviewed binary digests and unchanged clean source.
It creates a separate signed file in private
`$DOXA_HOME/native-plugin-authority/<pid>-<kernel-start-ticks>.json`. The signed
payload binds UID, PID, kernel start time, boot ID, home device/inode, enrolled
policy epoch, exact frontend/worker/Bubblewrap SHA-256 and opened device/inode,
delegated parent and supervisor identities, all nine cases and proof-log hash.
Its maximum lifetime is 900 seconds, measured against signed absolute
`CLOCK_BOOTTIME` issuance and expiry on the pinned boot. The TUI verifies the signature and exact
current identities before each launch, and the launcher verifies the opened
worker and Bubblewrap descriptors against the permit again. Package bytes,
owner allowlist and zero grants remain independently checked.

Delete the permit to revoke this instance, increment `epoch` or remove/change
the enrolled key to revoke all existing permits. Any owner config edit
conservatively cancels an admitted run. Expiry or revocation is polled every
20 ms while a worker runs, then the supervisor kills and removes its whole
cgroup before the TUI accepts a result. Every new permit load and every running-worker check enforces its signed
boot-clock expiry, so a wall-clock rollback cannot revive or extend a permit.
An admitted run also has an `Instant` deadline. A new boot,
process, installation, delegated cgroup or owner home requires a fresh proof
and permit. Renewing a permit cancels an in-progress run that held the old file.

This policy trusts the explicitly enrolled operator and the owner account.
It does not defend against another malicious process with the same UID that
can change configuration, signing keys or installed executables. It does not
install delegation, start a root service or grant additional Wasm capabilities.

For a disposable installed-host TUI exercise, the separate PTY driver creates
its own private owner home, signing key, two approved zero-grant modules and
fixture-engine sessions. It reruns the nine-case proof for each permit and
tests refusal without a permit, return 17 through the real slash command,
revocation of a running worker, panel/window cancellation, wrong-instance
replay and expiry. It never contacts a provider or edits the owner's home:

```sh
DOXA_PLUGIN_DISPOSABLE_HOST=1 DOXA_PLUGIN_TUI_ACCEPTANCE=1 \
  python3 scripts/plugin-installed-tui-proof.py \
    --frontend /absolute/private/install/doxa-rs \
    --daemon /absolute/private/install/doxa-daemon \
    --receipt installed-tui-proof.json
```

Use the same private `TMPDIR`, real-disk `CARGO_TARGET_DIR`, clean source and
delegated supervisor layout as the installed-host command. Each receipt name
must be new. The driver writes the host receipts, bounded TUI log and a final
receipt only after every lifecycle case succeeds. Its temporary activation
keys and owner home are removed at exit.

On 2026-10-13 this driver passed all eight cases against a private installed
frontend on a disposable Ubuntu 26.04.1 guest with Bubblewrap 0.11.1 and cgroup
v2. Its three required nine-case runs all passed, each removing every worker
cgroup. The resource cases observed exactly 256 MiB aggregate memory with OOM
kills, a PID peak of 16 with denial, and CPU throttling. The running TUI worker
cgroups had real populated/PID observations before revocation or close and
were gone after cleanup. This evidence covers that exact private installation;
it does not provision the workstation or authorize a different running TUI.

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
boundary and cleanup. TUI execution uses the separately signed operator permit.
Nonempty grants, native shared libraries, scripts, provider backends, hooks and
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
reads the receipt; TUI execution requires the separate signed permit. A different installed
host, worker build or cgroup fixture needs its own proof and review.
