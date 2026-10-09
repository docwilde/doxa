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

## Separate reply-sender check

`run-anchor.sh` uses the same offline guest and a second, disabled verifier.
The root-owned endpoint is `SOCK_SEQPACKET`; one bounded packet contains the
entire nonce/query reply. The client requires `SO_PASSCRED` and checks the
kernel's `SCM_CREDENTIALS` for the process that sent that packet, separately
from the listener's stale `SO_PEERCRED`. Three guest cases run against the
same UID-1000 client:

| Server after root-owned listen | Reply-sender observation | Result |
| --- | --- | --- |
| Remains UID 0 | UID 0 | Accept observation, `binding: unknown` |
| Drops to UID 1000 | UID 1000 | Reject |
| Hands listener FD to UID-1000 child while root parent waits | UID 1000 | Reject |

Build the static test binary as above, then strip debug symbols from a copy
before passing it to `run-anchor.sh`. In the measured 512 MiB guest, the
unstripped 52 MiB binary failed initramfs unpacking; the stripped 8.5 MiB
copy booted. The script records hashes and a serial log and requires all three
cases to pass. No guest disk, network device, Docker, or analyzer exists.

This closes only the stale-listener credential counterexample for the
**current reply packet in the controlled guest**. Linux UIDs and path
ownership are still relative to the client's user/mount namespaces. A
separately controlled installation must establish the honest client's host
namespace and reviewed binary/configuration, and bind every later Engine and
LSP byte to the same controlled broker. A root-owned process could delegate
signing or response authority; this packet test does not attest its code or
policy. No production caller uses the verifier.
