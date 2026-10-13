# Explicit attested semantic broker

`attested_broker.py`, `read_analyzer.py`, `provision-attested.sh` and
`prove-attested-guest.py` implement a separate, optional Linux broker path.
The [implementation contract](../../docs/plans/codegraph-semantic-verification.md)
describes the signer, immutable source, distinct-UID Engine, exact attach FD,
kernel limits, analyzer identity, settings and cleanup gates. The offline
fixtures below remain historical untrusted observations and never authorize
this path.

## Provision on a reviewed disposable host

The operator needs real rootless Docker with cgroup-v2/systemd delegation under
an identity distinct from the DOXA client, reviewed Docker/Python/nsenter bytes,
and root-controlled broker/configuration/key/snapshot directories. The sample
script assumes guest users `doxa` UID 1000 and `engine` UID 1001. It is an explicit
root provisioning script; DOXA never invokes it or installs a host service.
Do not use it to overwrite an existing owner-host installation.

Review the source and stage `attested_broker.py`, `read_analyzer.py` and a reviewed
rust-analyzer binary. The acceptance fixture uses the official
[rust-analyzer 2026-10-12 release](https://github.com/rust-lang/rust-analyzer/releases/tag/2026-10-12),
version 0.3.3081. Linux gzip asset SHA256 is
`040cab4e2abc490fadd95c3f6cbc60b941a78761dbc3acf0ec66fd7a97f31db3`;
uncompressed analyzer SHA is
`fd89304c85f6c1996b036ea8f2c9ad9d3e25a197dbd713661c7fe51ec9dc8e49`.
These recorded hashes must be independently reviewed; a freshly downloaded
binary's self-reported hash does not create a trust anchor.

Build the development codegraph client from the reviewed checkout. Then,
inside the disposable guest, run:

```sh
bash provision-attested.sh ROOT_CONTROLLED_STAGING ENGINE_SOCKET CODEGRAPH_BINARY
```

Provisioning pulls an official Rust 1.94.0 base, records its RepoDigest, builds
rust-src and the reviewed analyzer into an image, then records the final local
image ID. Runtime uses the direct native toolchain path, a read-only root,
private temporary Cargo/Rustup homes, offline Cargo and disabled project
executable overrides. Provisioning network access is distinct from runtime
no-egress policy. Version-matched rust-src is necessary: analyzer 0.3.3081
refused to become healthy with the guest's Rust 1.93.1 package.

The script emits `/var/lib/doxa-semantic/operator-pins.json`, containing only
public pins. Obtain `public_key_hex` through a separate trusted provisioning
channel and pass it explicitly; do not derive authorization from a socket
reply or an unreviewed writable pins file. The private signer stays root-only
and is never copied to the client or evidence bundle. The helper/config paths
and SHA pins are bound by the signed config hash. Review any code/config/image
update and publish new pins before using it; mismatches fail closed.

## Query and prove

Both Linux entry points use the same parser and library gates:

```sh
doxa codegraph --semantic-broker --root WORKTREE --socket /run/doxa-semantic/broker.sock   --pins REVIEWED_RECEIPT.json --broker-key OUT_OF_BAND_PUBLIC_KEY_HEX   --file src/lib.rs --edge 0 --candidate 0
```

The result selects one fresh displayed syntax edge/candidate. Only a complete,
checkable plain Rust source inventory can yield `analyzer_resolved`; the result
states that it is an analyzer-definition observation, not compiler execution
or a persistent graph. Ordinary TUI syntax and stored answers are unchanged.

Run the acceptance script only in its disposable provisioned guest:

```sh
python3 prove-attested-guest.py --output /root/codegraph-proof/acceptance.json --repetitions 5
```

Five positives use the unprivileged installed client and actual signed broker.
Negative fault cases import the exact reviewed broker without loading a signer,
use actual Engine containers/analyzer/namespace helpers, and never sign or
promote their receipts. They exercise policy and kernel-limit drift, immutable
writes, lost/malformed create responses, wrong actual CID on a pinned daemon
FD, status/shutdown errors, cancellation, a stopped analyzer, helper pins,
namespace/output/time/cancellation/reap gates, and truthful cleanup failure
followed by exact administrative removal. Real unprivileged attacks exercise
key/code/socket/FD/Engine isolation, same-UID fake daemons, forged signatures,
genuine signed-receipt replay and trailing bytes. The separate coordinated
restart observer holds an actual daemon pidfd/socket identity across an Engine
restart; the acceptance runner never restarts shared Engine services.

The retained [2026-10-13 receipt](evidence/attested-2026-10-13/RUN.md) distinguishes
actual signed positives, runtime fault injection, unprivileged attacks and the
coordinated restart observation. Python unit regressions remain protocol tests,
not live attestation evidence. The older root-listener/sender fixtures below
remain unknown even when every observation test passes.

# Historical offline socket-observation counterexamples

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
retained there. After requiring the originating complete Rust `calls` answer,
the [corrected source passed the same six cases](evidence/stream-complete-answer-2026-10-09/RUN.md)
at `ef4aa64f`. The synthetic sender still proves no Engine or analyzer origin.
