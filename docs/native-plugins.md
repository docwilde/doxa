# Native text plugins

Native text plugins add local, read-only slash commands to the Rust TUI. They
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
