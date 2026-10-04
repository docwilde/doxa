# Rust platform verification

DOXA's native daemon and TUI target Unix systems. CI verifies the full Rust
workspace on Linux. The macOS job checks the workspace build, desktop launcher,
browser proxy peer credentials, portable Rust crates, daemon library and
focused integration suites, then runs native vendor start, turn, stop and
resume fixtures against a local SSE server. These fixtures use synthetic data
and do not authenticate to a provider account.

The daemon's `tests/process.rs` suite is currently Linux-only CI coverage.
An exploratory macOS run on 2026-10-04 passed 45 of its 72 tests and failed
27. Most failures start at the protected Codex process-owner path, which is
intentionally unavailable on macOS. The suite also contains Linux-specific
Python executable paths, a Unix socket timeout assumption, and one native
memory-index assertion that failed on macOS. Until those fixtures and the
underlying process contracts are ported, a green macOS job does **not** mean
the whole process suite passes there.

Bounded authenticated Claude, DeepSeek and GLM start, turn, stop/resume and
usage checks passed on Linux with alpha.68; see
[the release verification](live-provider-verification-2026-10-04.md).
Authenticated macOS provider sessions remain unverified. Protected Codex is
supported only on Linux. Windows remains unsupported because native session
transport, peer credentials and process supervision rely on Unix facilities.
