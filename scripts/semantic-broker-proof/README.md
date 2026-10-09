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

## Stream sender and transcript continuity

`run-stream.sh` builds a separate offline QEMU initramfs with a synthetic
three-packet LSP stream. The client sends one fresh challenge naming the
query digest, source/target hashes, and Git-listed Rust scan-input digest.
The opening and closing packets must echo that scan digest. It requires a root sender on **every**
packet, one sender PID and container ID, ordered chunks, the exact byte hash,
and EOF. The root-owned socket cannot be replaced by the UID-1000 client.

| Guest server behavior | Client result |
| --- | --- |
| Root sender for the whole stream | Accept transport observation; `binding: unknown` |
| Root listener handed to UID 1000 | Reject reply sender |
| Root opening, then UID-1000 FD handoff | Reject midstream sender |
| Root opening, then another root PID | Reject sender change |
| Container ID changes in byte packet | Reject ID change |
| Extra packet after the close receipt | Reject trailing data |

Build and strip the static Rust test binary as described above, then run
`run-stream.sh STATIC_CODEGRAPH_TEST KERNEL_IMAGE OUTPUT_DIR`. The script
hashes the source, fixture binary, test binary, kernel, and initramfs, saves
the full serial log, and requires all six cases to finish successfully. It
uses KVM, 512 MiB RAM, and no network device or guest disk. Its server
fabricates the LSP bytes and container ID; no Docker or analyzer is present.
The passing root case is therefore an **untrusted stream observation**, not
an attested semantic binding or a rootless-container proof. The CLI does not
call this seam.

The 2026-10-09 run's [full serial log](evidence/stream-2026-10-09/guest-serial.log.gz)
and [input hashes](evidence/stream-2026-10-09/SHA256SUMS) are retained with
the fixture. The uncompressed serial log's SHA-256 is
`44c76b762e875213196741b274fdff8da22c53ea8febd1ef7ff7a0abbbdd2ba8`.
The kernel digest matches the prior disposable anchor guest receipt; the
kernel bytes came from the Ubuntu package recorded there.
That retained run predates the scan-digest packet field. Its sender-continuity
result remains historical evidence for the earlier packet format. The updated
format passed a separate [six-case offline guest run](evidence/stream-source-basis-2026-10-09/RUN.md)
against source commit `d5b2347f`, with input hashes and the full serial log
retained there. The synthetic sender still proves no Engine or analyzer origin.
