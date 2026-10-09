# Plan status

Audited against the native Rust tree on 2026-10-09. A Python-era plan's
"shipped" label describes that historical release; it is not evidence that
every proposed extension exists in Rust.

| Work | Current status |
| --- | --- |
| [Session Docker isolation](session-isolation-docker.md) | Linux native/open-egress/no-network profiles, private clones, idle migration and TUI controls shipped in beta.10. Effective cgroup ceilings and rootless fixture checks followed. Beta.17 added HTTPS allowlist/bypass probes and read-only [quota](../hard-quota-preflight.md) and [remote Engine](../remote-engine-preflight.md) preflights; beta.18 audits existing quota-tree descendants. Production hardened egress, hard disk quotas, remote Engines and Docker Desktop remain open. See the [operator guide](../session-isolation.md). |
| [Independent fleet supervision](fleet-supervision.md) | Typed host gates, selectable independent review, frozen scopes, handoffs and explicit dependency release exist. Beta.15 added owner-run offline test receipts; beta.16 adds a real-rootless fixture smoke and private labeled-message scorer. Full live fleet validation and a consented real-message corpus remain open. See the [operator guide](../fleet-supervision.md). |
| [Remote hub and Android](remote-hub.md) | Native/browser control, encrypted Chrome client and browser Web Push exist. Beta.16 can save remote-only native tab layouts for the same owner and session incarnation; mixed layouts are excluded. Beta.17 adds owner- and incarnation-scoped Android FCM background-push source and protocol tests. The debug APK builds and opens in an offline emulator; provisioned device/FCM and two-host tailnet QA remain open. |
| [Model registry](model-registry.md) | Exact-model sourced facts feed the picker and conservative budget bounds. Beta.18 exposes dated static price evidence while keeping billed tier and provider charge unknown. The `gpt-6-astra` supervisor and judge selectors have sourced facts and regression coverage; live account availability and latency remain unknown. Provider catalogs still control availability. |
| [Code graph](code-graph.md) | Rust syntax queries, structural and literal module-file edges, and unverified conditional candidates are available. LORE 0.62.20 owns reviewed, source-hashed snapshots; DOXA offers explicit CLI and TUI reads. Beta.18 labels included reference hashes verified, stale or unknown. Semantic binding, complete cross-file freshness and other languages remain open. |
| [DOXA plugin API](plugin-api.md) | Owner-allowlisted text commands and owner-produced status files are data only. Beta.17 added WASM package preflight, complete WASM 1.0 validation and approval identity recheck; beta.18 bounds memory/table declarations and stages a grantless child protocol. An isolated runner, enforced grants, hooks and provider extensions remain open. Adoption of selected Claude provider plugins is separate. |
| [Mermaid transcript diagrams](mermaid.md) | Opt-in local previews, sandbox doctor and beta.16 Settings preflight exist. A pinned real CLI suite passed fixed diagrams in Linux sandbox. Real terminal graphics QA and an optional installer decision remain open. |
| [Collection triage](collection-triage.md) | Suggested names, settled opt-in group sorting and verified local LORE pending rank exist. Beta.16 added verified project heading hues and hidden-tab urgency; beta.17 added pane-row navigation, manual collection hues and label rename. Beta.18 adds `/collection project-label` for freshly verified canonical roots; unresolved or mixed roots stay read-only. |
| [macOS daemon process coverage](https://github.com/docwilde/doxa/issues/197) | Portable crates and daemon process tests run in macOS CI; beta.15 verifies production peer delivery and fleet PID admission. An opt-in source-only Claude lifecycle harness is available; an authenticated macOS pass and protected Codex success remain unverified. |

Native UI, LORE, provider and remote parity is tracked in
[the Rust parity record](../rust-1.19-parity.md). Older split-pane, rail,
peer-publishing, engine and provider-plugin plans primarily preserve design
history. The [old sandbox draft](sandbox.md) is superseded by the current
Docker specification above.
