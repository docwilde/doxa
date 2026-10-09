# Plan status

Audited against the native Rust tree on 2026-10-09. A Python-era plan's
"shipped" label describes that historical release; it is not evidence that
every proposed extension exists in Rust.

| Work | Current status |
| --- | --- |
| [Session Docker isolation](session-isolation-docker.md) | Native, open-egress and offline Linux profiles have private clones, idle migration, TUI controls and effective cgroup checks. Fixture probes cover `EDQUOT`, HTTPS allowlists and bypasses; the gateway validates TLS SNI before dialing. Beta.21 adds descriptor-bound XFS project-quota inspection. Production hardened admission remains closed pending live descendant, restart and remount proof; remote Engines and Docker Desktop remain open. See the [operator guide](../session-isolation.md). |
| [Independent fleet supervision](fleet-supervision.md) | Typed host gates, selectable independent review, frozen scopes, handoffs and explicit dependency release exist. Beta.15 added owner-run offline test receipts; beta.16 adds a real-rootless fixture smoke and private labeled-message scorer. Full live fleet validation and a consented real-message corpus remain open. See the [operator guide](../fleet-supervision.md). |
| [Remote hub and Android](remote-hub.md) | Native/browser control, encrypted Chrome client and browser Web Push exist. Beta.16 can save remote-only native tab layouts for the same owner and session incarnation; mixed layouts are excluded. Beta.17 adds owner- and incarnation-scoped Android FCM background-push source and protocol tests. The debug APK builds and opens in an offline emulator; provisioned device/FCM and two-host tailnet QA remain open. |
| [Model registry](model-registry.md) | Exact-model sourced facts feed the picker and conservative budget bounds. Beta.18 exposes dated static price evidence while keeping billed tier and provider charge unknown. The `gpt-6-astra` supervisor and judge selectors have sourced facts and regression coverage; live account availability and latency remain unknown. Provider catalogs still control availability. |
| [Code graph](code-graph.md) | Rust syntax queries and reviewed LORE 0.62.20 snapshots are available. Complete Git-listed Rust scans carry a rechecked input digest. A [restricted rust-analyzer contract](codegraph-semantic-verification.md) and bounded fake-server-tested LSP driver exist, but the opt-in probe reports unknown until effective runtime containment is attested. Verified semantic binding and other languages remain open. |
| [DOXA plugin API](plugin-api.md) | Owner-allowlisted text commands and status files are data only. An explicit, owner-approved, zero-grant Linux CLI prototype routes WASM through the dedicated cgroup-gated worker. Delegated-host aggregate containment proof, TUI activation, grants, hooks and provider extensions remain open. Claude provider-plugin adoption is separate. |
| [Mermaid transcript diagrams](mermaid.md) | Opt-in local previews, sandbox doctor and beta.16 Settings preflight exist. A pinned real CLI suite passed fixed diagrams in Linux sandbox. Real terminal graphics QA and an optional installer decision remain open. |
| [Collection triage](collection-triage.md) | Suggested names, settled opt-in group sorting and verified local LORE pending rank exist. Beta.16 added verified project heading hues and hidden-tab urgency; beta.17 added pane-row navigation, manual collection hues and label rename. Beta.18 adds `/collection project-label` for freshly verified canonical roots; unresolved or mixed roots stay read-only. |
| [macOS daemon process coverage](https://github.com/docwilde/doxa/issues/197) | Portable crates and daemon process tests run in macOS CI; beta.15 verifies production peer delivery and fleet PID admission. An opt-in source-only Claude lifecycle harness is available; an authenticated macOS pass and protected Codex success remain unverified. |

Native UI, LORE, provider and remote parity is tracked in
[the Rust parity record](../rust-1.19-parity.md). Older split-pane, rail,
peer-publishing, engine and provider-plugin plans primarily preserve design
history. The [old sandbox draft](sandbox.md) is superseded by the current
Docker specification above.
