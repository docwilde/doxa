# Plan status

Audited against the native Rust tree on 2026-10-09. A Python-era plan's
"shipped" label describes that historical release; it is not evidence that
every proposed extension exists in Rust.

| Work | Current status |
| --- | --- |
| [Session Docker isolation](session-isolation-docker.md) | Linux native/open-egress/no-network profiles, private clones, idle migration and TUI controls shipped in beta.10. Beta.13 verifies effective cgroup ceilings; beta.15 gates the fixture gateway's TLS SNI and rootless state; beta.16 adds an opt-in real-rootless restricted-egress transport smoke. Read-only [quota](../hard-quota-preflight.md) and [remote Engine](../remote-engine-preflight.md) fixture preflights identify prerequisites without admission. Production hardened egress, hard disk quotas, remote Engines and Docker Desktop remain open. See the [operator guide](../session-isolation.md). |
| [Independent fleet supervision](fleet-supervision.md) | Typed host gates, selectable independent review, frozen scopes, handoffs and explicit dependency release exist. Beta.15 added owner-run offline test receipts; beta.16 adds a real-rootless fixture smoke and private labeled-message scorer. Full live fleet validation and a consented real-message corpus remain open. See the [operator guide](../fleet-supervision.md). |
| [Remote hub and Android](remote-hub.md) | Native/browser control, encrypted Chrome client and browser Web Push exist. Beta.16 can save remote-only native tab layouts for the same owner and session incarnation; mixed layouts are excluded. The Android source client's debug APK and protocol tests build, with local alerts while SSE stays connected. Device/tailnet QA and native background push remain open. |
| [Model registry](model-registry.md) | Exact-model sourced facts feed the picker and conservative budget bounds. Beta.16 adds verified OpenAI context/thinking facts and a FedRAMP-inclusive GPT-5.3-Codex price bound. Unknown facts stay unknown; provider catalogs still control availability. |
| [Code graph](code-graph.md) | Rust syntax queries, structural and literal module-file edges, and unverified conditional candidates are available. LORE 0.62.20 owns reviewed, source-hashed snapshots; DOXA offers explicit CLI and TUI reads. Semantic binding, other-file freshness, and other languages remain open. |
| [DOXA plugin API](plugin-api.md) | Beta.13 added owner-allowlisted data-only native text commands; beta.16 reads owner-produced status files with bounded refresh and a visible failure ledger. Executable plugins, hooks and provider extensions remain open. Adoption of selected Claude provider plugins is separate. |
| [Mermaid transcript diagrams](mermaid.md) | Opt-in local previews, sandbox doctor and beta.16 Settings preflight exist. A pinned real CLI harness awaits an explicitly provisioned package; terminal validation and installer decision remain open. |
| [Collection triage](collection-triage.md) | Suggested names, settled opt-in group sorting and verified local LORE pending rank exist. Beta.16 colours headings only after exact project-root verification and adds hidden-tab urgency to pane badges. Full pane-row grouping, editable project labels and manual hues remain open. |
| [macOS daemon process coverage](https://github.com/docwilde/doxa/issues/197) | Portable crates and daemon process tests run in macOS CI; beta.15 verifies production peer delivery and fleet PID admission. Protected Codex success and authenticated provider checks remain unverified. |

Native UI, LORE, provider and remote parity is tracked in
[the Rust parity record](../rust-1.19-parity.md). Older split-pane, rail,
peer-publishing, engine and provider-plugin plans primarily preserve design
history. The [old sandbox draft](sandbox.md) is superseded by the current
Docker specification above.
