# Backend fixes from the beta47 review

## BE-002: model admission through budget wrappers

`BudgetHost` forwards the provider's `model_change_requires_idle` policy.
The actual runtime regression holds the gap between queued turns, where the
provider is idle but the runtime still owns a queued prompt. Both a direct
Claude-shaped host and the unpriced budget wrapper reject a model change
before calling the provider. Model changes succeed after queue completion.

The synthetic Unix-socket regression passed with no provider calls or owner
configuration changes:

```sh
cargo test --locked --offline -p doxa-daemon --bin doxa-daemon \
  budget_host::queue_tests::unpriced_budget_preserves_queued_model_guard
```

## BE-001: explicit external plugin assets

Adopted Claude plugins accept symlink targets inside their installed root.
External targets need an explicit per-plugin grant:

```toml
[plugin_external_roots]
"example@marketplace" = ["/home/you/approved-plugin-assets"]
```

The config and approved directories must belong to the current user, and may
not be writable by group or others. Grants are absolute directories, capped
at 16 per plugin. Command inventory checks the same roots before reading
Markdown. Plugin copying retains its existing size/depth limits and channel
exclusions. Failed staging never publishes partial data. Personal skills
retain their one-direction external-link copies.

This constrains accidental or malicious external links in a plugin installation.
It does not claim race-proof confinement against a compromised same-user account.
The feature remains opt-in; grants deliberately authorize the selected trees.

Regression fixtures cover internal links, external denial, explicit grants,
component-prefix rejection, atomic replacement, config permissions, and personal
skill compatibility:

```sh
cargo test --locked --offline -p doxa-claude isolation::tests
```

## Peer transcript provenance

Canonical peer admission now carries bounded typed origin metadata through
the runtime and provider wrappers into the exact user transcript record.
The message body cannot confer peer identity. Generic provider records strip
the reserved metadata; only explicit host-created user records can attach it.
The origin is passed directly to the provider prompt, without shared mutable
pending attribution. Invalid origin fields or identity-changing scrubbing
reject the metadata write.

Sent and received cards also have a private, session- and scope-bound JSONL
journal. It has a 4 MiB ceiling and a 32 KiB record ceiling. A journal failure
does not discard an otherwise delivered live message. The daemon emits one
bounded diagnostic, and an unavailable journal does not prevent attachment.
The journal is a presentation record, not a second message-delivery channel.

The peer wire frame remains unchanged for beta47 compatibility. Sender ledger
IDs and receiver-local card IDs are generated locally. They are not a
cross-machine delivery identifier or container-origin attestation.

A generic daemon `prompt` request remains a normal user turn, including the
socket script supplied in the report. Existing history without recorded peer
provenance cannot be reclassified from its wording. Agents should use canonical
`peer_send` with the full session ID; the operator path is `/msg`.

Final core package tests passed **97 tests**; daemon binary unit tests passed
**69 tests**. These include immediate/queued origin propagation, provider
reserved-field stripping, transcript session/type checks and scrub rejection,
and successful attachment when the presentation journal is unavailable.
Three final process fixtures passed for native scoped sends, Claude SDK
full-ID replies, and DeepSeek waking idle/queueing busy Codex and Claude
receivers. The Router host target passed **61 tests** with local catalog,
router and provider transports. No live peer or provider was contacted.
