# doxa-state

Shared native state helpers for session IDs, bounded registry reads, TOML
configuration, machine identity, and tabset files. [`lib.rs`](src/lib.rs)
provides owner-checked file access and atomic writes; callers supply the paths
for configuration and tabset operations.

## Tabset storage and frontend ownership

`load_tabset` validates the flat session list and retains the original JSON,
including unknown top-level keys, split trees, pane groups, and collections.
`save_tabset` preserves those fields and refuses membership/order changes when
structured references have not been rebuilt to match. This guard protects
callers that only understand the flat list; it is not a limit on native pane
or collection support.

The TUI's [`ui_state.rs`](../doxa-tui/src/ui_state.rs) restores and saves nested
layouts, active tabs, labels, drafts, collections, and fleet views. It rebuilds
layout and collection references before calling `save_tabset`, and refuses a
save when a partial or unsupported view would lose retained sessions. Pane
geometry and bounds belong to [`ui/panes.rs`](../doxa-tui/src/ui/panes.rs) and
[`ui/layout.rs`](../doxa-tui/src/ui/layout.rs), rather than this file layer.

`ensure_machine_id` mints the local 32-character UUID4 hex identity once using
atomic no-clobber publication. `resolve_tabset_path` safely adopts a valid
pre-1.10 scope-only tabset into the machine-specific name without replacing an
existing record. Retained Python-shaped records are compatibility data; the
installed runtime is native Rust.

## Settings and sessions

This crate loads and atomically writes TOML tables, preserving unrelated keys;
`update_config` serializes a read-modify-write operation with an advisory lock.
The typed settings catalog, validation, categories, and environment override
presentation belong to [`doxa-tui/src/settings.rs`](../doxa-tui/src/settings.rs).
Archived transcript discovery and verified resume plans belong to
[`doxa-tui/src/history.rs`](../doxa-tui/src/history.rs).

Registry reads separate raw routing fields from scrubbed display fields.
Callers must supply the secret scrubber; unknown registry fields are dropped.
Raw routing fields must never be displayed. Results are advisory: this crate
filters registry records but does not probe sockets or remove stale entries.
The TUI's [`discovery.rs`](../doxa-tui/src/discovery.rs) verifies attach targets,
while the [`peer registry`](../doxa-peers/README.md) owns stale peer cleanup.
See the [current runtime guide](../README.md) for user commands and behavior.
