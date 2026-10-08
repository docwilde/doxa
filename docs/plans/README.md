# Plan status

Audited against the native Rust tree on 2026-10-08. A Python-era plan's
"shipped" label describes that historical release; it is not evidence that
every proposed extension exists in Rust.

| Work | Current status |
| --- | --- |
| [Session Docker isolation](session-isolation-docker.md) | Linux native/open-egress/no-network profiles, private clones, idle backend migration and TUI controls shipped in beta.10. Beta.11 adds monitored disk limits. Hardened egress, hard quotas, remote Engines and Docker Desktop remain open. See the [operator guide](../session-isolation.md). |
| [Independent fleet supervision](fleet-supervision.md) | Beta.10 added typed host admission, immutable charters, selectable supervisor/message-review models and budgets. Beta.11 adds frozen worker scopes and artifact handoffs. Beta.12 adds offline threshold calibration. Labeled real-message evaluation, dependency dispatch and automatic trusted test evidence remain open. See the [operator guide](../fleet-supervision.md). |
| [Remote hub and Android](remote-hub.md) | Native/browser control, the packaged encrypted Chrome client and browser Web Push exist. The Android client and Android push remain open. |
| [Model registry](model-registry.md) | Beta.12 adds exact-model sourced facts shared by budget accounting and the picker. Context and thinking remain unknown until independently sourced; provider catalogs still control availability. |
| [Code graph](code-graph.md) | Draft; no native code-graph extraction/query feature is shipped. LORE belief browsing is a separate feature. |
| [DOXA plugin API](plugin-api.md) | Draft; there is no DOXA extension loader. Adoption of selected provider plugins is already supported. |
| [Mermaid transcript diagrams](mermaid.md) | Opt-in local sandboxed previews implemented; pinned CLI/Chromium validation, installer and doctor support remain open. |
| [Collection triage](collection-triage.md) | Beta.12 adds suggested names and settled, opt-in group sorting to the native rail. Current per-session LORE proposal state, Python-era colour and pane-group aggregation remain open for Rust. |
| [macOS daemon process coverage](https://github.com/docwilde/doxa/issues/197) | Open portability work. Portable crates and selected daemon/local-provider suites run in macOS CI. Authenticated macOS provider checks remain unverified. |

Native UI, LORE, provider and remote parity is tracked in
[the Rust parity record](../rust-1.19-parity.md). Older split-pane, rail,
peer-publishing, engine and provider-plugin plans primarily preserve design
history. The [old sandbox draft](sandbox.md) is superseded by the current
Docker specification above.
