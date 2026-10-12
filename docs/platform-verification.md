# Rust platform verification

DOXA's native daemon and TUI target Unix systems. CI verifies the full Rust
workspace on Linux. The macOS job checks the workspace build, desktop launcher,
browser proxy peer credentials, portable Rust crates, daemon library and
focused integration suites, then runs native vendor start, turn, stop and
resume fixtures against a local SSE server. These fixtures use synthetic data
and do not authenticate to a provider account.

The macOS job now runs the portable portion of `tests/process.rs`, including
daemon registry, worktree, Claude, vendor, peer refusal and protected Codex
**refusal** cases. Protected Codex turn-success and process-owner cases are
Linux-only by design. Fixture Python is resolved through `PATH`; macOS CI
already installs `python3`.

The exploratory [2026-10-04 macOS run](https://github.com/docwilde/doxa/actions/runs/37165658097)
passed 45 of 72 process tests. Most failures entered the unsupported protected
Codex path. Those turn-success cases are now Linux-only; macOS still checks
the authoritative protected-startup refusal before any provider execution.

The four remaining cases tracked by
[issue #197](https://github.com/docwilde/doxa/issues/197) are now enabled on
macOS: direct peer delivery, broadcast and reply-history filtering, Claude MCP
peer sends, and native vendor memory refresh and finalization indexing. Peer
fixtures use a bounded poll and the complete one-line protocol, including empty
discovery probes. This avoids setting `SO_RCVTIMEO` on an accepted connection
whose peer already closed it: [Darwin rejects socket options after full shutdown](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/uipc_socket.c).
It also handles accepted sockets inheriting a nonblocking listener's mode.
Production peer senders retain the connection until the receiver reads the
frame and samples its PID.

The old memory-index probe hardcoded `/usr/bin/python3` and converted every
SQLite query error to zero, so its result did not establish that indexing had
failed. The current probe resolves Python through `PATH`, opens the private
fixture database read-only, and reports query errors. With native LORE
0.62.21, the unchanged finalization test verifies zero indexed messages before
shutdown, four after shutdown when memory is enabled, and zero when disabled.

The [2026-10-12 macOS job](https://github.com/docwilde/doxa/actions/runs/38257543111/job/114839395634)
passed all **58 portable process tests with zero ignored**, including all four
restored cases and the protected Codex refusal. The same source passed all
**93 Linux process tests with zero ignored**. The macOS job also passed the
portable workspace, focused daemon suites and local SSE lifecycle checks.
These credential-free results do not verify an authenticated live fleet or
provider account.

Bounded authenticated Claude, DeepSeek and GLM start, turn, stop/resume and
usage checks passed on Linux with alpha.68; see
[the release verification](live-provider-verification-2026-10-04.md).
Authenticated macOS provider sessions remain unverified. Protected Codex is
supported only on Linux. Windows remains unsupported because native session
transport, peer credentials and process supervision rely on Unix facilities.

## Opt-in authenticated macOS check

An operator on a macOS host can run the source-only lifecycle verifier after
building the native daemon and signing in to the Claude Code CLI through its
normal flow. It does not accept a credential argument or print account data:

```sh
# Use a private, short path on real disk for Unix sockets and scratch state.
mkdir -p "$HOME/.t"
chmod 700 "$HOME/.t"
export TMPDIR="$HOME/.t"
cargo build --locked -p doxa-daemon
DOXA_NATIVE_DAEMON="$PWD/target/debug/doxa-daemon" \
  python3 scripts/verify_macos_authenticated_lifecycle.py --live --provider claude
```

The command requires macOS and explicit `--live` opt-in. It launches two short
synthetic subscription turns through DOXA's normal isolated Claude CLI path,
checks native event delivery, acknowledges and waits for each daemon stop,
then resumes the same session. LORE and peer tools are disabled; the verifier
uses an owner-private disposable workspace and suppresses provider output.
Its JSON receipt contains only `passed` or `unknown`, four stage results,
the submitted-turn count, and a bounded reason code. An unknown result is not
evidence of provider success. Run `--provider codex` to record the explicit
`protected_codex_linux_only` unknown state; no Codex process is launched.

This harness has only source and mock-test evidence on Linux. No authenticated
macOS pass has been recorded. CI's credential-free daemon and vendor fixtures
remain the automatic checks.
