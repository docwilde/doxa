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
passed 45 of 72 process tests. Of its 27 failures, 24 entered the unsupported
protected Codex path; the authoritative refusal case now asserts the macOS
failure rather than expecting a Linux build-version check. The three remaining
macOS cases remain visibly ignored under
[issue #197](https://github.com/docwilde/doxa/issues/197): two fake peer socket
receivers saw `EINVAL` during full-stream reads, and native vendor finalization
indexed zero messages where four were expected. Beta.15 keeps production peer
senders connected until the receiver reads the frame and samples its PID;
the fleet process fixture now uses that sender and passes macOS CI. The SQLite
probe reports query errors instead of treating all errors as zero. A green
macOS job covers this portable process path, but not the three ignored
interactions, an authenticated live fleet or authenticated provider sessions.

Bounded authenticated Claude, DeepSeek and GLM start, turn, stop/resume and
usage checks passed on Linux with alpha.68; see
[the release verification](live-provider-verification-2026-10-04.md).
Authenticated macOS provider sessions remain unverified. Protected Codex is
supported only on Linux. Windows remains unsupported because native session
transport, peer credentials and process supervision rely on Unix facilities.
