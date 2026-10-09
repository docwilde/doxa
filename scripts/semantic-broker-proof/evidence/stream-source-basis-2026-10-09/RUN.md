# Offline stream source-basis guest receipt

The six-case offline QEMU run passed against exact source commit
`d5b2347f63fb92a4f2969e280574716464029df6`. The Rust test binary was
built with `cargo test --locked --target x86_64-unknown-linux-gnu -p
doxa-codegraph --lib --no-run` and `RUSTFLAGS='-C target-feature=+crt-static'`.
`run-stream.sh` used the kernel at
`/home/docwilde/ssd-cache/doxa-semantic-anchor-20261009/kernel/boot/vmlinuz-7.0.0-38-generic`.
The static BusyBox binary was `/usr/bin/busybox`. The guest had no network
device or guest disk.

All six cases printed `test result: ok. 1 passed; 0 failed`, each with
`TEST_STATUS=0 BROKER_STATUS=0`:

| Mode | Client receipt |
| --- | --- |
| `root` | `ordered_root_sender=observed binding=unknown` |
| `handoff` | rejected UID/GID-1000 packet sender |
| `mid_handoff` | rejected UID/GID-1000 packet sender |
| `root_switch` | rejected changed sender PID |
| `cid_swap` | rejected changed container ID |
| `extra` | rejected extra data or missing close |

The final marker was `DOXA_STREAM_ALL_STATUS=0`. The full serial log is
[`guest-serial.log.gz`](guest-serial.log.gz). This proves the updated packet
fixture ran as specified in a disposable guest. Its root sender fabricated
the LSP frame and container ID; it does not prove analyzer or Engine origin.

SHA-256 inputs and outputs:

| Artifact | SHA-256 |
| --- | --- |
| `rust/doxa-codegraph/src/semantic_broker.rs` | `10e40b2e95036ced5c582ab4674d16b175fec0e394463d8dde5bc1c5eddb97ac` |
| `scripts/semantic-broker-proof/stream_fixture.c` | `be4d491ffe194f10ddb8ef8944c1feb2953357b784d6590ef8b2c56246508135` |
| `scripts/semantic-broker-proof/stream-init` | `810db232e5175ccb29c6aaaf47f868df4a7cd1f9133957de611fe80e2470971e` |
| `scripts/semantic-broker-proof/run-stream.sh` | `2cd8442779800ae210f0f624f43d04f585c4b46d4ceda879ec175f949d5c3ea9` |
| `Cargo.lock` | `19ff2796afc7259d3c1c7205cbb34b432bdb431452d0930628144a4df23d1b17` |
| static Rust test binary | `3e20ce19ec612a944aa254610f5a2b8f92cc8e8f895a498082ed561b98a9c782` |
| static BusyBox | `df12634c17fcdca839ae5dc47d7627b7558511f7645de7c99ccf097a0f28ed5b` |
| guest kernel | `9f55ef7253055a9cf68f8a04323c93dead2fca617386d842fbde9ca7b178b27e` |
| compiled C fixture | `99f2a805078dae974e3ad86f4245d3c5fd20434f8efc77662b7ff27e762217e7` |
| initramfs | `ff149c27e3f42d1482e44d0f4afd3ccb101abeff9f2fcfff792a79dd65e894b4` |
| raw serial log | `5b140533b9afe1ef0ec942ce5af98dcfaace96d83d41dca401344fb7424a058a` |
| compressed serial log | `ad0038132c6d55b07224872318ed084d9f208ce8898deb13a0e55b594a79bf44` |
