# Disposable socket-observation counterexample

This fixture boots an offline QEMU/KVM initramfs with only static BusyBox,
the codegraph Rust test binary, and a Unix echo server. The server creates
and listens on `/run/doxa-semantic/producer.sock` as guest UID 0, then drops
to UID 1000 before accepting and answering. The client also runs as UID
1000. The test records a UID-0 `SO_PEERCRED` observation, confirms it cannot
remove the socket, and still returns `binding: unknown` and
`uid_zero_echo_untrusted`. The server echoes the nonce and exact query
digest. No Docker, rust-analyzer, host mounts, or network device exists in
the guest.

Build a static test binary with `cargo test --locked --target
x86_64-unknown-linux-gnu -p doxa-codegraph --lib --no-run` from `rust/` and
`RUSTFLAGS='-C target-feature=+crt-static'`; use a separate `CARGO_TARGET_DIR`
to avoid mixing static and normal artifacts. Provide that binary, a readable
Ubuntu kernel image, and an output directory on disk to `run.sh`. The script
records source/kernel/test hashes and the complete serial log and requires
both the test and fixture to exit successfully. Do not run the ignored test
on a live host: its UID-0-created socket is a guest-only fixture.

Passing this fixture proves that UID-0 socket credentials do not authenticate
the current server process. Ownership and credentials are also namespace
relative. It does not prove a rootless Engine, analyzer bytes, LSP stream
origin, resource limits, egress, disk quota, or cleanup. The CLI remains
`binding: unknown`.
