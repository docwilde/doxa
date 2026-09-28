# DOXA protocol v1

Shared wire contract used by the native Rust frontend transport. The codec
checks newline-delimited JSON frames, the 64 KiB size cap, required fields,
and protocol version. Unknown optional fields are retained. It does not
implement socket I/O, session lifecycle, authorization, engine semantics,
or persistence.

[`doxa-tui/src/transport.rs`](../doxa-tui/src/transport.rs) uses the codec for
client writes and server-frame validation. [`doxa-runtime`](../doxa-runtime/README.md)
implements the native daemon socket and host boundary. Python 1.x client,
daemon, and peer files remain development interoperability references.

From the repository root:

```sh
cargo test --locked -p doxa-protocol
```

See the [current runtime guide](../README.md) for installation and session use.
