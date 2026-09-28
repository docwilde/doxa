# Native Claude Code transport

`doxa-claude` launches the Claude Code executable directly, without a shell,
Python interpreter, or Agent SDK. [`cli.rs`](src/cli.rs) implements the
bidirectional `stream-json` transport; [`claude_host.rs`](../doxa-daemon/src/claude_host.rs)
provides DOXA session behavior, event mapping, tool gates, LORE integration,
and persistence. See the [current runtime guide](../README.md) for installation
and session commands.

## Transport

`Cli::spawn(CliOptions)` requires a canonical UUID session ID and absolute cwd
and configuration directory. It starts `claude --print --verbose` with
`--input-format stream-json`, `--output-format stream-json`,
`--include-partial-messages`, and `--permission-prompts host`. New sessions use
`--session-id`; resumed sessions use `--resume` with the same identity. Model,
effort, permission mode, and approved plugin directories are explicit options.

`Cli` sends prompts, control requests, and correlated control responses, and
receives provider JSON frames. Input and output frames are limited to 1 MiB.
Writes have deadlines, receive polls have caller-supplied timeouts, and process
group cleanup terminates descendants on termination or drop. Provider stderr
is suppressed. The host owns request validation, approval decisions, reconnect
state, and transcript scrubbing; the transport alone does not grant tool access.

## Isolation and resume

[`isolation.rs`](src/isolation.rs) prepares DOXA's private Claude configuration
and copies authentication in one direction from the user's CLI configuration.
It does not write host authentication. Startup disables provider auto-memory,
sets `LORE_SKIP=1`, empties inherited setting sources, and supplies an empty
strict MCP configuration. Approved plugin adoption copies bounded command,
skill, and agent artifacts while removing executable hook, MCP, and LSP
configuration. The LORE plugin is excluded because DOXA owns that integration.

[`resume.rs`](src/resume.rs) verifies retained legacy SDK-era transcripts against
provider logs inside DOXA's private configuration. It checks ownership, bounded
complete records, session identity, workspace, and completed turns. Missing or
ambiguous proof is refused; it does not manufacture a replacement conversation
or copy state from other CLI configurations.

## Verification

From the repository root:

```sh
cargo test --locked -p doxa-claude
```

Local fixtures exercise transport and isolation behavior. They do not establish
live provider compatibility; the [parity tracker](../../docs/rust-1.19-parity.md)
records the release gates. This crate implements a native CLI adapter, not an
official Rust Agent SDK.
