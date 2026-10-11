# Interface review fixes

This note records the implementation boundaries for the beta47 interface review.

## Transcript Mermaid work

Mermaid fence discovery now scans each paragraph once and builds the body only
after it finds a matching closing fence. The source parser still rejects
incomplete, nested, oversized, and non-standalone fences, and keeps the 16 KiB
diagram and eight-preview admission limits. The preview store keeps the newest
eight parsed sources under a SHA-256 transcript revision key; an unchanged
transcript therefore avoids reparsing on later draws. Hashing the bounded
transcript still occurs when Mermaid preview prerequisites are active.

The beta47 review's isolated optimized parser harness measured a 493,425-byte,
31-fence transcript at 10,694 ms before this change. Repeating the same fixture
with the extracted updated production functions measured 8 ms; all eight
parser cases returned their expected diagrams. These measurements exclude a
TUI frame, Mermaid renderer, and process sandbox. The linear-work regression
counts inspected input bytes for valid near-limit diagrams and a deeply
incomplete fence, avoiding a machine-specific timing assertion.

## Router review freshness

Painting Router status reads only the most recent background freshness
observation. At most one local check is pending; completed observations are
refreshed every two seconds and expire with the review's existing 60-second
lifetime. The check revalidates config bytes and Jev key availability
(present/missing); it does not identify a replacement key when both old and new
values are valid. A failed or unavailable observation displays as stale. Save
and Start continue to call `Review::current` synchronously at the point of
action, so cached paint status never authorizes a write or session launch.

## New-session catalog ownership

The TUI owns at most one Codex or Claude catalog worker. A newer selection
cancels the owned provider process and queues only the latest requested engine
until that process has exited and been reaped. Requests carry a form generation;
results from an earlier selection are discarded even if the user returns to
the same engine. Closing or replacing the form also invalidates and cancels its
request. Codex app-server and Claude CLI discovery use their existing
process-group cleanup paths when cancellation is observed.

## Python snapshots and the pinned LORE service

DOXA keeps the Python snapshot gate closed; its existing Rust snapshot support
remains enabled. The pinned LORE revision is
`760e739410d8c5d99e9a74ededd7e719aa47d5b4`, also the current local LORE `main`
at review time. Its `rust/lore-core/src/codegraph_snapshot.rs::query` rejects
every requested path not ending in `.rs`, and the read protocol documentation
describes Rust source paths. DOXA's `LoreClient::codegraph_snapshot` applies the
same restriction before sending the request. A source-only LORE patch is ready
on branch `feature/codegraph-python-snapshots` at
`57e26c7e76b34c924b8045ec6fcfaf89d0b3598c` in a separate local worktree. It
accepts bounded `.py` paths through the same validator and adds Python
round-trip, stale-source, and invalid-path tests. All six `codegraph_snapshot`
tests passed offline in a separate build target; it is not released or pinned.

The upstream prerequisite is for the prepared capability to be reviewed and
released. Only after DOXA updates its dependency pin to that
released revision should it relax its client guard and cover Python snapshot
response parsing. Changing the DOXA side alone would still be rejected by the
pinned LORE service and would leave the stored-snapshot contract unverified.

## Retiring a fleet with unknown review spend

`accounting_unknown` is sticky by design: `resume_review` refuses to clear it,
failed reservations remain in the original journal, and Stop remains
available. DOXA has no operation that attaches external settlement evidence or
reconciles a past reservation. Do not edit the journal or resume that run.

The supported conservative retirement/replacement procedure is:

1. Stop the old run and retain its manifest, frozen charter, state, and journal
   as the record of the unresolved spend.
2. Keep the old run's entire owner-approved review budget reserved in the
   operator's fleet-wide allocation while its spend remains unknown. The
   journal does not identify the unresolved reservation separately from the
   unused slack on settled calls, so `reserved_usd - actual_estimated_usd` is
   not a valid release amount. DOXA does not aggregate this hold across run IDs.
3. Start a replacement only under a new reviewed charter and an independently
   approved allocation that does not consume the retained hold. If there is no
   separate allocation, keep paid review disabled and use human review, or do
   not start the replacement. Do not represent an unverifiable provider charge
   as settled.
4. Preserve the old journal even if provider-side records later clarify the
   amount. The current DOXA interface still cannot clear the old sticky marker;
   any later replacement uses a new run ID and an owner-approved allocation.

This procedure preserves the hold outside DOXA's per-run budget journal. It is
an operator allocation boundary, not a claim that the CLI enforces cumulative
spend across retired and replacement runs.

## Context usage rendering

The context submenu paints known fills as background-color cells, so its normal
style does not depend on a terminal font containing block glyphs. The existing
`ascii` preference still renders explicit markers. Unknown or invalid telemetry
gets a neutral cell strip and a text status; it never appears as a measured
zero. If the provider omits categories, the submenu says that the composition
was not reported. Claude `context_detail` forwards the CLI's validated
`categories` unchanged, and the UI segments only when their token sum agrees
with the reported used total. Codex currently reports totals and window size
but no context composition. The source's Claude CLI fixtures return an empty
category list; the colored-category frame test is a synthetic UI fixture, not
a live-provider measurement. No terminal session was run for this change.

Root-owned focused verification:

```sh
cargo test -p doxa-tui ui::context_usage::tests::reported_components_divide_capacity_with_matching_legend_and_resize -- --exact
cargo test -p doxa-tui ui::context_usage::tests::context_menu_draws_colored_cells_and_keeps_provider_composition -- --exact
cargo test -p doxa-tui ui::context_usage::tests::context_totals_without_provider_categories_do_not_claim_a_composition -- --exact
cargo test -p doxa-tui ui::context_usage::tests::unknown_invalid_and_reported_zero_are_distinct -- --exact
```
