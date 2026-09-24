# Native runtime foundation

`doxa-runtime` is a standalone library for the DOXA daemon protocol v1. A
caller supplies a `Host` implementation, session metadata, and a runtime
directory, then calls `Daemon::bind(...).start()`. The returned handle owns
the listener and removes its socket on shutdown or drop. An engine can publish
out-of-band events through `DaemonHandle::publish`.

Implemented in this slice:

- Unix socket in an owned, non-symlink runtime directory (0700); socket 0600.
  A pre-existing socket is refused and never unlinked. Cleanup checks the
  original socket inode before removing it.
- Protocol v1 `hello`, `attach`, `event`, and `reply` frames. Events use a
  512-entry in-memory ring and `seq >= cursor` replay. Replay enrollment and
  publication share one lock, so replay precedes the live stream.
- Bounded 64 KiB line-JSON input and output; slow clients have bounded output
  queues and are dropped instead of slowing other clients.
- Prompt dispatch with a bounded eight-item FIFO, a reply before turn events,
  and queue notifications to other clients. Host calls and a built-in minimal
  `status` call have protocol v1 reply envelopes.
- Multiple clients, explicit handle shutdown, and a host-approved `stop` call.

This is **not yet a replacement for the Python daemon**. The Python engine,
PeerHost registry/discovery, transcript and LORE persistence, permission
gates, full RPC set, linger/finalization lifecycle, signals, session resume,
and process launching are not connected. The test `Host` is an in-process
fixture only. A later slice must define the native engine adapter and audit
each RPC before exposing it to socket clients. Until then, the existing Rust
TUI continues to connect to the Python daemon.

Run `cargo test --manifest-path rust/doxa-runtime/Cargo.toml` from the repo
root. The socket tests cover frame shapes, replay/live order, prompt ordering,
multiple clients, malformed and oversized input, permissions, path refusal,
and cleanup.
