# Shared worktree lifecycle

The installed runtime uses the Rust daemon. Python references below describe
development interoperability and retained legacy checkout metadata; they do
not require Python for native sessions. See the [current Rust guide](../rust/README.md).

Updated Python and Rust daemons use the same advisory `flock` at
`$DOXA_HOME/worktrees/.meta/<checkout-name>.lock`. They acquire it before
creating or reusing a managed checkout and retain it while the daemon is
running, including client detach. Resuming a managed Python checkout must
claim its lock before engine startup. Lock files remain after cleanup;
unlinking one would let another process lock a different inode.

Both sidecar formats now carry `base_oid`, captured when the checkout is
created and updated after a verified base switch. Cleanup verifies the
session, checkout path, registered branch, main repository, pinned base
ancestry, clean tree including ignored files, and absence of unique commits.
A busy lock keeps the checkout. Failed checkout removal retains the branch;
Python branch deletion compares the expected commit so a concurrent update
is retained. Rust orphan cleanup also rechecks live session IDs under the
lock before deletion.

## Old Python sessions

Unmodified Python 1.19 daemons never acquire this lock and their sidecars do
not pin `base_oid`. A free lock cannot prove that an old daemon is idle,
even if its registry entry is absent. These records remain survey-only:
neither implementation adopts, switches, or automatically deletes them.
Do not add `base_oid` by guessing from the current HEAD; that would discard
the only indication that lifecycle ownership is unverified.

Before upgrading a running old session, finish or stop its daemon using the
old runtime. Keep its checkout and branch for normal Git inspection and
manual merge. Start a fresh managed session from the main repository to
obtain verified metadata and the shared lock. A resume of an unverified old
managed checkout is refused with an explicit reason. Missing directories
retain their sidecar for guarded recovery rather than dropping ownership
history.

## Verification

Python tests use disposable repositories and separate processes to exercise
lock contention, retained lock inodes, resume claims, refused fallback,
unpinned metadata, ignored user files, and failed removals. Rust tests prove
that a Python `fcntl.flock` holder prevents Rust creation, that an active Rust
claim prevents orphan deletion, and that a pinned Python-compatible record
can be cleaned after the claim ends. No provider calls are required.
