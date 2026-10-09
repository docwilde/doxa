# Complete-answer stream guest receipt

The six-case offline QEMU fixture passed against exact Rust source commit
`ef4aa64f0feb12983c6938e60537dd5e7617d698`. This source requires a
complete Rust `calls` answer before the disabled stream observation can use its
scan-input digest. The Rust test binary was built with `cargo test --locked
--target x86_64-unknown-linux-gnu -p doxa-codegraph --lib --no-run` and
`RUSTFLAGS='-C target-feature=+crt-static'`. The guest used the readable
`vmlinuz-7.0.0-38-generic` kernel from the task-local semantic anchor and
static `/usr/bin/busybox`, with no network device or guest disk.

Each case printed `test result: ok. 1 passed; 0 failed` and
`TEST_STATUS=0 BROKER_STATUS=0`:

| Mode | Client receipt |
| --- | --- |
| `root` | `ordered_root_sender=observed binding=unknown` |
| `handoff` | rejected UID/GID-1000 packet sender |
| `mid_handoff` | rejected UID/GID-1000 packet sender |
| `root_switch` | rejected changed sender PID |
| `cid_swap` | rejected changed container ID |
| `extra` | rejected extra data or missing close |

The final marker was `DOXA_STREAM_ALL_STATUS=0`. The complete serial log is
[`guest-serial.log.gz`](guest-serial.log.gz). The root fixture fabricated the
LSP frame and container ID, so the run does not attest an analyzer or Engine.

| Artifact | SHA-256 |
| --- | --- |
| `rust/doxa-codegraph/src/semantic_broker.rs` | `ad575b1509ccc458313dfec3561760adc91adf6d56f99521875fbad769aae81e` |
| `scripts/semantic-broker-proof/stream_fixture.c` | `be4d491ffe194f10ddb8ef8944c1feb2953357b784d6590ef8b2c56246508135` |
| `scripts/semantic-broker-proof/stream-init` | `810db232e5175ccb29c6aaaf47f868df4a7cd1f9133957de611fe80e2470971e` |
| `scripts/semantic-broker-proof/run-stream.sh` | `2cd8442779800ae210f0f624f43d04f585c4b46d4ceda879ec175f949d5c3ea9` |
| `Cargo.lock` | `19ff2796afc7259d3c1c7205cbb34b432bdb431452d0930628144a4df23d1b17` |
| static Rust test binary | `b9707068d91a218b5fab8ab36641ab0aefdc4a7d89691e9d363a6eedfcaf67ff` |
| static BusyBox | `df12634c17fcdca839ae5dc47d7627b7558511f7645de7c99ccf097a0f28ed5b` |
| guest kernel | `9f55ef7253055a9cf68f8a04323c93dead2fca617386d842fbde9ca7b178b27e` |
| compiled C fixture | `99f2a805078dae974e3ad86f4245d3c5fd20434f8efc77662b7ff27e762217e7` |
| initramfs | `3a0bd83e994605951b858dacc3a9ebc01990fb6dbb4c4bbdb42960f9f0825c80` |
| raw serial log | `42256549fe107981ae43695d1d46e0d877d0ab665784ede0131f43c56c42238a` |
| compressed serial log | `e10efb9037de86357e60d5f020aa201381e00c6700ae17115f75713d50b12563` |
