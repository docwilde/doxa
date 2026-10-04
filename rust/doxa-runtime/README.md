# Native runtime foundation

`doxa-runtime` is a standalone library for the DOXA daemon protocol v1. A
caller supplies a `Host` implementation, session metadata, and a runtime
directory, then calls `Daemon::bind(...).start()`. The returned handle owns
the listener and removes its socket on shutdown or drop. An engine can publish
out-of-band events through `DaemonHandle::publish`.

The socket layer provides:

- Unix socket in an owned, non-symlink runtime directory (0700); socket 0600.
  A pre-existing socket is refused and never unlinked. Cleanup checks the
  original socket inode before removing it.
- Protocol v1 `hello`, `attach`, `event`, and `reply` frames. Events use a
  512-entry in-memory ring and `seq >= cursor` replay. Replay enrollment and
  publication share one lock, so replay precedes the live stream.
- Bounded 64 KiB line-JSON input and output; slow clients have bounded output
  queues and are dropped instead of slowing other clients.
- Prompt dispatch with a bounded eight-item FIFO, a reply before turn events,
  and queue notifications to other clients. The `queue` RPC lists scrubbed
  previews. `cancel_queued` requires an exact queued ID; a supplied 1-based
  position is checked against that ID before removal, so a stale position
  cannot cancel the next prompt after a dequeue. A missing terminal turn
  event is synthesized so clients cannot wait forever on a returned or
  panicked host. Host calls and a built-in minimal `status` call have
  protocol v1 reply envelopes.
- Multiple clients, explicit handle shutdown, and a host-approved `stop` call.

## Production integration

The installed Rust frontend connects to `doxa-daemon`, which uses this crate.
[`doxa-daemon/src`](../doxa-daemon/src) supplies native Claude, Codex, and vendor
hosts plus peer discovery, transcript persistence, canonical native LORE,
permission gates, session recovery, and finalization. Those policies belong to
the host and daemon integration rather than the socket library. See the
[current runtime guide](../README.md) for installation and user commands.

The `Host` trait exposes prompt execution, RPC dispatch, capability and status
snapshots, scrubbing, durable transcript restore, and host-owned peer/session
tools. `DaemonHandle` supports peer prompt admission and detached idle expiry;
active provider work prevents automatic idle expiration. Real hosts must reject
unsafe public prompt text before queueing or sending it to clients.

## Verification

From the repository root:

```sh
cargo test --locked -p doxa-runtime
```

Socket fixtures cover frame shapes, replay/live order, prompt ordering,
multiple clients, malformed and oversized input, permissions, path refusal,
and cleanup. Fixture status does not establish live provider behavior.
