# Actual attested broker acceptance, 13 October 2026

The disposable Ubuntu 26.04.1 guest used 4 vCPUs, 6 GiB RAM and kernel
7.0.0-34-generic. Root controlled the broker/key/configuration/snapshots;
DOXA was UID 1000 with no privileged groups; rootless Docker 29.1.3 ran as
UID 1001. No owner-workstation service or root configuration was changed.
The shared Engine socket was narrowed to 0600 before the final run without
changing inode 688, daemon 27821 or the reviewed daemon configuration.

The cloud-image provenance supplied by the VM provisioner is retained in
[guest-image-provenance.json](guest-image-provenance.json). Its Ubuntu signed
checksum was verified with gpgv and the installed cloud-image keyring before
boot; image SHA is
`8800651811af9a85465ad1d552add729947bb16488dddb4a9b5305a3d97332b2`.
Rootless Engine service configuration SHA remained
`3a6774e75043931ce3dd1874bac3aabc86908a1bb21fe07c68eb7e46054f26c9`.

## Inputs

The production broker source is commit `52980759`, SHA
`94044157e1f909b9158a2d205a56ef075eeab396b1c896558b0c5f8cd3ef7c69`.
The held-executable reader is `c5219b34`, SHA
`c700862ff7ca7328cb096ccfd1f53c0d5dc66485f8c26096f004b176a387dead`.
The final Rust development client includes `dcfecc8f`, binary SHA
`9b42f26995b438b92e312b533ebff5f90081288bc30fb9c8ea19a39630b1d6ef`.
The acceptance runner includes `1f5ddcfa`; `f31c6d3c` subsequently adds saving
its already verified genuine signed envelope/challenge. Five signed positives
use the same root-controlled installed client/broker bytes throughout the run.

[config.json](config.json) contains the actual nonsecret configuration,
including pinned nsenter/Python/reader bytes. Its exact SHA is
`321cfce8df742a2aa85b83d6b07fc4506ed15463b94553c92ae6088072037fdf`.
[operator-pins.json](operator-pins.json) contains public pins only. The signer
private key is not included. The operator's public key must be supplied through
an independent trusted provisioning channel in a real installation.

The reviewed analyzer is official rust-analyzer 2026-10-12, version 0.3.3081,
SHA `fd89304c85f6c1996b036ea8f2c9ad9d3e25a197dbd713661c7fe51ec9dc8e49`.
The image uses official Rust 1.94.0-slim-bookworm RepoDigest
`rust@sha256:a86cada82e36ebd7a9bffed7548792c55a952fdb20718eea9278a936bcb76e62`
and version-matched rust-src. Final local image ID is
`sha256:03cc3523c7a1da94a4ff606417c1d5f269aa55ebdbffde4b9b6283048dc72803`.
Native toolchain PATH avoids rustup dispatch through the private temporary home.

The Git fixture is dependency-free Cargo source with Cargo.lock and:

```rust
pub fn target() -> u32 { 17 }
pub fn caller() -> u32 { target() }
```

The broker clones the whole original input tree to root-owned immutable files.
Original input digest is
`5393b8b059c4900a9a525de6f01084629cb318c34a1317dd55a2966106e5d741`;
mounted-content digest is
`68fe5ea0dcac89b18628a0b579d0f4613cf3865202c8daea450fc506be940f37`.
Signed settings have autoreload=true solely for this immutable bootstrap,
buildScripts/procMacro/check disabled, client watcher, noDeps and one thread.
The legacy mutable-source producer remains autoreload=false and untrusted.

## Results and scope

Command in the reviewed disposable guest:

```sh
python3 /root/codegraph-proof/prove-attested-guest.py \
  --output /root/codegraph-proof/acceptance.json --repetitions 5
```

[acceptance.json](acceptance.json) records **39/39 passed**. The five actual
unprivileged installed-client positives are 3418.619, 3365.643, 3546.352,
3406.504 and 3397.813 ms: median 3406.504, p95 3546.352 ms. These timings include
source query/prechecks, snapshot, actual container/analyzer, successful shutdown,
cleanup and postchecks. They are single tiny-workspace observations, not a
large-workspace semantic latency guarantee. Every positive verified a fresh
signature/nonce, exact source and target declaration, stable analyzer and
Engine identity, runtime policy and successful server exit. Each exact CID's
absence was independently checked through a fresh pinned Engine connection.

| Real control or fault injection | Observation |
| --- | --- |
| Requested PIDs 128 | Effective Docker policy drift refused; actual CID removed |
| Kernel nofile 32 with Engine metadata 64 | Independent kernel-limit drift refused; CID removed |
| Client and container append to immutable source | Both denied; copied file root-owned 0444 |
| Lost/empty successful-create reply | Exact nonce-name/full-CID recovered, removed and verified absent |
| Pre-inspect URL switched to another actual CID on pinned daemon FD | ID drift refused before upgrade/LSP; both objects removed |
| Reader hash pin changed | Refused before helper execution; actual container removed |
| Actual namespace helper stdout 4097 bytes | Output bound refused; helper SIGKILL/reaped |
| Actual namespace helper sleeps 30 s | Two-second reader deadline refused; helper SIGKILL/reaped |
| Actual helper reports wrong namespace | Refused; successful child reaped, no binding |
| Cancellation during actual helper | Refused; helper SIGKILL/reaped |
| Injected busy serverStatus during real analyzer session | Guard refuses; actual CID removed |
| Injected shutdown error during real analyzer session | Refused; forced cleanup cannot substitute for success |
| Client EOF after real initialized | Cancelled and exact actual CID removed |
| Real analyzer SIGSTOP | Shortened proof deadline expires; stopped process/CID removed |
| Both DELETE attempts fail on an actual container | Unknown and cleanup=false; exact administrative removal then verified |
| Held actual Engine socket alias replaced | Old pinned identity refuses inode drift; shared socket untouched |
| UID 1000 key/code/socket/FD/Engine attacks | Each access denied by actual guest permissions |
| UID 1001 fake Python Engine with correct socket ownership | Actual executable SHA gate refuses |
| UID 1000 fake broker/signature | Genuine operator public key rejects signature |
| Genuine signed runtime receipt replay | Fresh client nonce mismatch rejects |
| Fresh genuine signed receipt relayed with trailing byte | Client EOF/transcript gate rejects |
| Project `.cargo/config.toml` rustc-wrapper override | Authenticated prelaunch refusal; no source command runs |
| Final inventory | No retained semantic CID/name, helper process or snapshot |

The first 27-case run passed its runtime cases but an alias creation attempted
an invalid cross-filesystem hard link. The corrected alias remains in the
Engine runtime tmpfs; the complete final run above passes. This was a proof
harness issue, not weakened production identity checks.

The negative runtime cases deliberately fault-inject the broker's unsigned
control flow while using actual containers, actual analyzer processes and
actual nsenter transitions. They never load the signing key or promote a
binding. Same-UID attacks and client replay/trailing-byte cases exercise the
actual signed production service. Local Python regressions are separately
19/19 passed in 2.308 s; the codegraph library is 108 passed / 3 ignored and focused
TUI checks 8 unit + 1 installed CLI integration passed / 1 ignored benchmark.

[signed-envelope.json](signed-envelope.json) and
[signed-challenge.json](signed-challenge.json) retain an additional genuine
unprivileged broker exchange with the same unchanged fixture and pins. This
supplementary capture was independently verified with the public Ed25519 key;
it is separate from the five installed-client timing samples. Verify without
any private key:

```python
import hashlib, json
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
pins = json.loads(Path("operator-pins.json").read_text())
envelope = json.loads(Path("signed-envelope.json").read_text())
payload = bytes.fromhex(envelope["payload_hex"])
Ed25519PublicKey.from_public_bytes(bytes.fromhex(pins["public_key_hex"])).verify(
    bytes.fromhex(envelope["signature_hex"]), payload)
receipt = json.loads(payload)
challenge = Path("signed-challenge.json").read_bytes().rstrip(b"\n")
assert receipt["challenge_sha256"] == hashlib.sha256(challenge).hexdigest()
assert receipt["nonce"] == json.loads(challenge)["nonce"]
assert receipt["status"] == "attested" and receipt["cleanup_removed"]
```

## Separate coordinated restart

[restart-proof.json](restart-proof.json) records a real held Engine object
across the isolation lane's coordinated shared restart. Its original daemon
PID 4788/socket disappeared; the old object refused instead of accepting the
new daemon 27821. This observer held the daemon identity without an active
semantic container. It is not a restart during a semantic transaction or
a proof of that cleanup-recovery branch. The isolation lane independently
checked its own held quota container across the same Engine restart.

## Remaining claim limits

This proves the optional plain-Rust analyzer-definition path on the described
externally controlled Linux host. It does not attest a caller replaced/ptraced
by its own user or an operator who supplies an incorrect trust anchor. An
owner installation still requires the reviewed external key, distinct Engine
identity, image/toolchain/configuration and honest installed client contract.
No owner-workstation broker was installed.

The fixture has no project dependencies. Arbitrary dependency caches are not
hydrated by this runtime profile; offline/unavailable metadata must stay
unhealthy/unknown. Incomplete Rust inventories, conditional/generated evidence,
traits/types/macro expansion and compiler execution remain outside the claim.
DOXA's own parser-guard tombstones prevent its complete semantic inventory;
its automatic syntax index remains useful while reporting that incomplete
coverage. Semantic defaults, TUI overlays and LORE snapshots are not promoted.
