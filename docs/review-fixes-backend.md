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
