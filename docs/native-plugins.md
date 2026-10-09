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
conservative failure class on error. Without a delegated cgroup v2 subtree
with memory, CPU and PID controllers, it fails closed before spawning the
worker. The current host lacks that delegation, so end-to-end aggregate cgroup
containment still needs proof on a delegated host. TUI execution, nonempty
grants, native shared libraries, scripts, provider backends, hooks and
automatic startup remain unsupported.

Before broader activation, a delegated-host acceptance run must verify the
installed memory, swap, CPU and PID limits; worker membership before exec;
aggregate limits under fork and process-group escape attempts; no host file or
network access; and removal of all descendants after return, timeout and
cancellation. The current host cannot exercise those cgroup checks.

### Delegated-host acceptance fixture

On a disposable Linux host, delegate a private cgroup v2 subtree with the
`memory`, `pids` and `cpu` controllers to the test user. The host also needs
Bubblewrap with `--ro-bind-fd` and `--json-status-fd`, Python 3, `setsid`,
two available CPUs, and a private real-disk scratch directory. Then run:

```sh
mkdir -m 700 -p "$HOME/t"
TMPDIR="$HOME/t" DOXA_PLUGIN_CGROUP_ACCEPTANCE=1 \
  CARGO_TARGET_DIR="$HOME/ssd-cache/doxa-plugin-acceptance-target" \
  cargo test --locked -p doxa-tui --lib \
  delegated_cgroup_containment_acceptance -- --ignored --nocapture
```

The fixture is ignored by ordinary tests and requires the explicit environment
switch.
It fails on missing delegation, controllers, tools or observations; it does
not silently skip a failed host. It uses the same descriptor-mounted launcher,
private cgroup and process supervisor as the grantless CLI. Each case prints
one bounded counter summary, followed by `cleanup=removed` only after
`cgroup.kill` empties and removes the group.

The six cases check host file, environment and TCP isolation; the installed
256 MiB memory, zero swap, 16 PID and one-CPU quotas; actual child cgroup
membership; aggregate PID denial and memory OOM under multiple children; CPU
throttling; a `setsid` descendant killed on cancellation; and timeout cleanup.
The evidence is limited to cgroup counters, outcomes, byte counts and elapsed
time. Record the test output with the host's kernel, cgroup and Bubblewrap
versions for review. Passing this fixture on one host does not grant plugin
permissions or enable TUI execution.
