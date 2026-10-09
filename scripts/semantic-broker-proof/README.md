# Disposable broker identity proof

This fixture boots an offline QEMU/KVM initramfs with only static BusyBox,
the codegraph Rust test binary, and a root-owned Unix echo server. The test
runs as guest UID 1000, connects to `/run/doxa-semantic/producer.sock`, checks
the kernel-reported root peer, and confirms the socket cannot be removed by
the client. The server echoes the nonce and exact query digest. No Docker,
rust-analyzer, host mounts, or network device exists in the guest.

Build a static test binary with `cargo test --locked --target
x86_64-unknown-linux-gnu -p doxa-codegraph --lib --no-run` from `rust/` and
`RUSTFLAGS='-C target-feature=+crt-static'`; use a separate `CARGO_TARGET_DIR`
to avoid mixing static and normal artifacts. Provide that binary, a readable
Ubuntu kernel image, and an output directory on disk to `run.sh`. The script
records source/kernel/test hashes and the complete serial log and requires
both the test and root fixture to exit successfully. Do not run the ignored
test on a live host: its root socket is a guest-only fixture.

Passing this fixture authenticates only a host-owned endpoint under the
guest's restricted-user model. It does not prove a rootless Engine, analyzer
bytes, LSP stream origin, resource limits, egress, disk quota, or cleanup.
The CLI remains `binding: unknown`.
