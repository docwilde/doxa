# DOXA protocol v1

Standalone shared wire contract for the Python 1.x daemon and Rust 2.0
frontend/runtime migration. The codec checks newline-delimited JSON frames,
the 64 KiB size cap, required fields, and protocol version. Unknown optional
fields are retained. It does not implement socket I/O, session lifecycle,
authorization, engine semantics, or persistence.

`doxa/client.py`, `doxa/daemon.py`, and `doxa/peers.py` remain the behavior
reference. The TUI and native runtime will adopt this crate after their
current frontend and daemon PRs merge.
