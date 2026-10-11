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

## Subscription quota bars

The usage submenu draws separate session and weekly bars only from validated
provider percentages. Claude's five-hour and seven-day quota snapshots retain
their reset timestamps; the provider quota summary remains visible for its
additional Opus, Sonnet, and overage windows. Codex forwards its existing
primary window and seven-day secondary window, including a bounded `resetsAt`
when supplied, without adding a billing request. Older `5h`/`week` quota labels
remain readable. Typed duration metadata must be present and valid; session
and weekly slots reject durations that contradict their window. Missing Claude
duration metadata uses its known five-hour/seven-day key. Unknown windows show
an explicit provider-unavailable line and no bar. Reset time is printed only
when the provider supplied a valid timestamp; stale reports are labeled.
Menu height, keyboard and wheel scrolling, resize clamping, and telemetry
refresh use the wrapped quota rows instead of the static diagnostic lines.

Root-owned focused verification:

```sh
cargo test -p doxa-tui ui::session_telemetry::tests::quota_window_bars_use_only_recognized_provider_windows -- --exact
cargo test -p doxa-tui ui::session_telemetry::tests::explicit_quota_window_durations_must_be_valid_for_their_slot -- --exact
cargo test -p doxa-tui ui::tests::usage_submenu_renders_reported_session_and_weekly_quota_and_keeps_unknowns_clear -- --exact
cargo test -p doxa-tui ui::tests::usage_submenu_keyboard_wheel_and_resize_reach_late_quota_rows -- --exact
cargo test -p doxa-engines --lib billing_uses_only_measured_codex_windows
```


## Restored context reports

A validated last context report is stored with its exact session ID in the
project's existing UI tabset. Restoring that report labels the chip and context
submenu as **Last reported**. A placeholder reconnect status preserves the
historical report; a fresh authoritative report replaces it, and an
authoritative report without valid context counts clears it. New sessions
without a measurement remain explicitly unknown. Corrupt, cross-session,
remote, oversized, and invalid records are rejected. The cache preserves only
normalized token totals and validated categories, never prompt contents.

## Beliefs and peers chips

Codex now exposes the same cached LORE status through its host as the other
engines, so the Beliefs chip can show its reported count. The Peers chip opens
the local peer map for the exact active session. A peer count appears only
after a complete owner-scoped roster; an incomplete roster or membership event
invalidates the count instead of displaying a guessed zero.

## Settings dismissal and prompt focus

An outside left-click closes Settings and its transient text/credential
editors and dropdown, and returns focus to the current prompt. A click on
another session prompt also selects that pane and restores its draft. A
nested New Session form keeps its own input and admission behavior. The
regression uses an isolated test home, changes and saves a setting, dismisses
both an editor and a dropdown, and checks typing and draft retention in two
panes. No owner configuration is used as the test fixture.

## Desktop clipboard copy

Explicit selection copy tries a fixed desktop helper: `wl-copy` on Wayland,
`xclip` on X11, or `pbcopy` on macOS. The writer runs outside the paint loop,
bounds text to 64 KiB at a UTF-8 boundary, and cancels/kills/reaps a failed or
slow process group. If no helper is available or it fails, DOXA sends the
existing OSC52 sequence and reports that terminal-dependent fallback.
GNOME Terminal users need `wl-clipboard` or `xclip` available for this desktop
path. Warp's OSC52 path remains available. Tests write only to a private fake
helper, including Unicode/control characters, bounds, failure and timeout;
no system clipboard or live GNOME/Warp session was accessed.


## Follow-up verification

The full TUI library suite passed **818 tests**, with six intentionally ignored
acceptance/performance cases, using the released native LORE carrier. The
Settings fixture enters the real command, stages and saves a setting in a
private `0700` test home, dismisses an inline editor and dropdown, clicks blank
space and another pane's prompt, and verifies typing and draft ownership.
Clipboard fixtures never read or write the desktop clipboard. The focused
Codex quota projection and Codex host compilation tests passed as well.
Initial runs caught nested-menu routing regressions and fixture mismatches;
the final suite was run after those were corrected.
