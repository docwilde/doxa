# Provider-only connectivity on 2026-10-20

Two explicitly approved, new subscription CLI turns passed on installed
**2.0.0-beta.64 / d9a4c12d**: native Codex `gpt-6-luna` at low effort and Claude
`claude-haiku-5-5`. Each ran in its own fresh Docker-offline session and returned
exactly `PROXY_OK`. No existing owner conversation was used or modified. These
are controlled connectivity probes, not fleet or supervision quality results.

## Actual container and network observations

Both probes used the reviewed rootless Engine and pinned image
`sha256:9370c2a5166d1226ad836550538d58f1d6f78a9cb31fa37134195d433f171ce4`.
Inspection of each actual container confirmed:

- `network=none`, read-only root filesystem, `cap-drop=ALL`, and
  `no-new-privileges`;
- 4 GiB memory, two CPUs and 256 PIDs;
- checkout, private home and cache mounts inside the disposable fixture, plus
  the read-only session broker mount; no Docker socket or owner project mount.

The checkout mount was writable; Codex separately requested its read-only
sandbox and on-request permission mode. A read-only container root does not make
every mounted directory read-only.

Before each model prompt, a process inside that same container checked:

| Request | Actual result |
| --- | --- |
| Direct TCP connection to `1.1.1.1:443` | Refused by the offline network boundary |
| `CONNECT example.com:443` through the provider Unix gateway | HTTP 403 |
| `CONNECT` to that provider's approved API host | HTTP 200, then verified TLS/SNI |
| Unauthenticated `GET /v1/models` over that TLS connection | HTTP 401 |

The final check used no credential. The subsequent authenticated model reply
exercised the actual provider broker path. Workers did not gain general internet
access through it.

## Model turns and billing limits

The sole prompt in each case was: `Reply exactly PROXY_OK. Use no tools or web.
Do not read or modify files.` Each had a 60-second stop deadline. The harness
refused unexpected tool, approval or compaction events and performed no
generation retry. Peer sending, inbound peer turns, session spawning and LORE
memory were disabled.

| Carrier | Observed result | Provider-reported tokens |
| --- | --- | ---: |
| Protected Codex app-server, `gpt-6-luna`, low | Exact reply, successful turn; saved native checkpoint records low effort, on-request permissions and app-server transport | 10,600 input / 7 output |
| Claude CLI, `claude-haiku-5-5` | Exact reply, successful turn | 2 input / 9 output |

These are the reported fields; no missing cache usage or monetary cost is
invented. No tool, approval, peer or compaction event was observed in either
turn. An initial Codex launch was refused before a container or prompt because
the harness omitted its explicit binary argument; correcting that setup did not
repeat a model turn.

Codex metadata identified ChatGPT Pro authentication, zero purchased credits,
and no reached quota or spend-control limit. The weekly window was 19% used
before and 20% after the first turn. This shared, rounded account telemetry
cannot attribute the percentage difference or a price to this probe. Claude
authentication identified Max 20x; startup supplied no quota. During the approved
turn, CLI telemetry reported 2% in the five-hour window with status `allowed`,
and 0% weekly, with a nonstale source. Extra-usage enablement and any account
charges remain unverified. The actual Claude carrier exposes no supported
128-token output override, so no such hard cap is claimed.

These provider-managed CLI account turns are separate from the
[paid API pilot reservations](fleet-readiness-2026-10-20.md). They do not prove a
dollar ceiling or an invoice amount. Authentication and quota metadata were read
without refresh-token requests or inference during preflight; see the official
[Codex authentication](https://learn.chatgpt.com/docs/auth) and
[app-server](https://learn.chatgpt.com/docs/app-server) documentation.

## Exact GPT-6.1 Sol carrier limitation

A later, separately approved capability probe requested exactly
`gpt-6.1-sol` at low effort through the same protected offline carrier. It stopped
during daemon initialization, before a registry entry, hello or model prompt.
No turn was sent and no alternative model was tried.

Matched metadata-only queries in fresh private Codex homes explain the catalog
discrepancy:

| Executed app-server | Newly fetched catalog client version | Exact `gpt-6.1-sol` advertised |
| --- | --- | --- |
| Standard installed Codex | `0.163.0` | Yes, visible and default, low effort supported |
| DOXA protected app-server | `0.156.1` | No |

Both catalog refreshes succeeded. The protected launcher's `--version` delegates
to the standard CLI, which reports `0.163.0`; `app-server` instead executes the
receipt-checked pinned `0.156.1` payload. Updating the standard CLI alone does
not update this protected payload. The current startup path calls `set_effort`,
which requires the exact selected model in that carrier's catalog. The catalog
absence therefore explains the low-effort initialization refusal from source;
the discarded raw stderr was not used to claim a captured error string.

A separately approved changed configuration omitted the explicit effort flag.
It first initialized an idle session with the exact model and passed the network
denial checks. Its one tiny prompt then failed with HTTP 400
`invalid_request_error`; the own scrubbed durable transcript records that
`gpt-6.1-sol` is not supported when using Codex with a ChatGPT account. The
provider reported the exact model but incomplete usage with null input/output
tokens. No tool, approval, warning, compaction or reroute event was observed.
Effort is unknown, and there was no retry or model substitution. This failed
turn remains unknown account usage, outside the tracked API ledger.

That rejection belongs to this protected `0.156.1` carrier/account request; the
standard `0.163.0` account catalog advertises the model. No global account-access
conclusion or successful served-model claim follows. Replacing the protected
carrier with the standard binary would discard the required compaction boundary
and was not attempted. Both exact-model configurations cleaned up their owned
containers, gateways, copied provider homes and daemons. The requested mixed
fleet still needs verified protected-carrier model support and separate bounded
subscription-turn/API-worker accounting before launch.

The read-only upgrade assessment fetched official `rust-v0.163.0`, peeled commit
`83086188ae0614bd882818ff1aa23f3194962012`. The existing protection patch does
not apply unchanged: step activation, step context/settings and protocol have
conflicts, and the patched `zsh_fork/unix_escalation.rs` path was removed.
Upstream also adds speculative/background remote V2 compaction, whose inference
starts separately from the foreground `run_pre_compact_hooks` path. A mechanical
version change would not establish the existing pre-inference guarantee.
Porting must cover or explicitly disable that new path and rerun compiled
missing/failed/duplicate/asynchronous-hook refusal and successful-review
controls, plus live permission-switch and helper-provenance checks. No source
port, provider build, installation or updated-carrier inference was performed.

## Evidence and remaining scope

The owner-private archive is
`~/.cache/doxa-acceptance-20261020/proxy/`. It contains the explicit opt-in
harnesses, redacted typed receipts and running notes. No credential, owner prompt,
raw account identifier or provider history is committed here. Each own daemon
was stopped and reaped, each container stopped, each gateway removed, and each
copied provider home deleted after its case.

| Private receipt | SHA-256 |
| --- | --- |
| `codex-offline-case.json` | `0c6b1a72d0182a8ee0c52d53759899e8e10043f1b5327b54bdb6e1b0146ba632` |
| `claude-offline-case.json` | `8a3c97d0322e08cfd6760d245f0ce37661e3747a5e4bd9fdf97b7de00e8de479` |
| `codex-protected-catalog-diagnostic.json` | `7a82e4c9c6edf61ed84678693bdaa09acc48f6c3330a5d0ff2bae5baf66437ad` |
| `codex-standard-catalog-diagnostic.json` | `411b2284ea1e53c719e3e6bd8799a4af5a80ad61cbf52b1086def29774e32ca6` |
| `codex-exact-sol-default-effort-case.json` | `d36a90bf4e1b13e375147659abf3dd06397aa47f62d453a418704763bb0a940f` |
| `codex-exact-sol-error-proof.json` | `e9775a091bdb57b1fe8a444662d2e04a74aaa01413809dfb16471e413122606c` |

An actual approved supervisor web fetch and evidence return remain untested by
this record. Jev/SubQ decision quality, independently human-labelled alignment
quality, full fleet completion and platform/image-protocol coverage are also
outside these connectivity results. Available search-provider credentials were
absent during preflight; no search or fetch service was added or called.
