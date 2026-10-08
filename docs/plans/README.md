# Plan status

Audited against the native Rust tree on 2026-10-09. A Python-era plan's
"shipped" label describes that historical release; it is not evidence that
every proposed extension exists in Rust.

| Work | Current status |
| --- | --- |
| [Session Docker isolation](session-isolation-docker.md) | Linux native/open-egress/no-network profiles, private clones, idle backend migration and TUI controls shipped in beta.10. Beta.11 added monitored disk limits; beta.13 checks effective cgroup ceilings. Beta.14 adds a fixture-only restricted egress gateway; beta.15 gates its TLS SNI and rootless state. A production hardened profile, hard quotas, remote Engines and Docker Desktop remain open. See the [operator guide](../session-isolation.md). |
| [Independent fleet supervision](fleet-supervision.md) | Beta.10 added typed host admission and selectable independent review; beta.11 added frozen scopes and handoffs; beta.12 added offline threshold calibration. Beta.13 adds Docker-isolated dependency waits and operator release; beta.14 adds a TUI release review. Beta.15 adds owner-run offline test receipts; rootless live validation and labeled real-message evaluation remain open. See the [operator guide](../fleet-supervision.md). |
| [Remote hub and Android](remote-hub.md) | Native/browser control, the packaged encrypted Chrome client and browser Web Push exist. Beta.13 adds an Android source client whose debug APK and protocol tests build, plus opt-in local alerts while its SSE connection survives. Device/tailnet QA and native background push remain open. |
| [Model registry](model-registry.md) | Beta.12 added exact-model sourced facts shared by budget accounting and the picker. Beta.13 adds sourced DeepSeek context/thinking and GLM thinking facts; GLM context remains unknown. Provider catalogs still control availability. |
| [Code graph](code-graph.md) | Beta.13 adds read-only Rust file, symbol, import and conservative call-site queries with source hashes. Beta.14 adds structural file-module edges; beta.15 adds a read-only TUI viewer. A follow-on slice resolves safe literal paths and reports cfg-gated files as unverified candidates. Conditional and semantic bindings, LORE storage/operator and other languages remain open. |
| [DOXA plugin API](plugin-api.md) | Beta.13 adds owner-allowlisted, data-only native text commands. Executable plugins, hooks and provider extensions remain open. Adoption of selected Claude provider plugins is separate. |
| [Mermaid transcript diagrams](mermaid.md) | Opt-in local sandboxed previews and a bounded `doxa doctor` PNG smoke check exist. Pinned CLI/Chromium validation and an optional installer remain open. |
| [Collection triage](collection-triage.md) | Beta.12 added suggested names and settled, opt-in group sorting to the native rail; beta.13 preserves source provenance on parsed LORE proposals. Beta.15 ranks complete source-scoped pending summaries in the local rail. Python-era colour and pane-tab aggregation remain open for Rust. |
| [macOS daemon process coverage](https://github.com/docwilde/doxa/issues/197) | Portable crates and daemon process tests run in macOS CI; beta.15 verifies production peer delivery and fleet PID admission. Protected Codex success and authenticated provider checks remain unverified. |

Native UI, LORE, provider and remote parity is tracked in
[the Rust parity record](../rust-1.19-parity.md). Older split-pane, rail,
peer-publishing, engine and provider-plugin plans primarily preserve design
history. The [old sandbox draft](sandbox.md) is superseded by the current
Docker specification above.
