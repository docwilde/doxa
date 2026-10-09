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

Executable plugins are **not available yet**. The separate `native-plugin`
preflight command lets the owner inspect a proposed WebAssembly package without
loading or running it. It does not change the data-only `native_plugins` list.
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
refused until a runner explicitly supports them.

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
closed; an unapproved package remains review-only. Approval is not an execution
switch. The digests pin local bytes; they do not authenticate a publisher. A
fresh `recheck_approved` call re-opens both files, checks their digests, opened
inodes and current owner approval against the earlier review, and returns the
validated module bytes. A future runner must use those bytes without re-opening
a path, enforce each grant, isolate crashes and resource use, and define a
bounded host protocol
before executable plugins can run. Native shared libraries, scripts, provider
backends, hooks and automatic startup remain unsupported.
