# doxa-peers

Standalone Rust port of local peer presence in `doxa/peers.py`. It reads and writes the same JSON registry schema, including optional daemon marker, client count, usage, self description, and parent session ID. `origin` is not serialized because Python sets it from the remote endpoint, never from the local registry.

A caller must supply a `Scrubber` implementation backed by LORE's secret scrubber before reading records for display. This crate intentionally has no pass-through scrubber. Provider, model, and engine are unverified self descriptions; consumers must never treat them as capabilities or authority. The static fixture in `tests/fixtures/python_peer.json` was emitted from Python `PeerInfo` with `dataclasses.asdict` (omitting local-only `origin`), and the Rust test checks old and future schema compatibility.

`Registry::open` creates owner-private runtime and registry directories. Records are written through 0600 temporary files and atomic rename. Reads are bounded, reject symlinks and foreign-owned files, and remove stale entries. Stale heartbeat with a live PID never removes its socket. A dead PID permits socket removal only when the path resolves within the runtime directory, is a socket owned by this user, and refuses a live connection. Scoped discovery follows Python's main Git checkout key and cwd fallback.

Still missing from the Rust port: a peer inbox, sending and receiving frames, message trust framing, remote peer bridge, peer ledger, and automatic heartbeat scheduling. The host should call `heartbeat` every `HEARTBEAT_SECS` after binding its inbox socket; this crate does not create that socket.
