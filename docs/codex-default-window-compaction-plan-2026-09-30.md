# Codex default-window automatic compaction: bounded verification plan

**Completed later on 2026-09-30.** The authenticated alpha.52 check observed
one native LORE review, one provider compaction checkpoint and exact recall
after a daemon restart within eight submitted turns. See the
[measured result](live-default-window-compaction-2026-09-30.md). The text below
records the pre-run reasoning and caps; its earlier status is historical.
The executed native harness used frames below 63 KiB because DOXA's daemon
protocol caps each frame at 64 KiB, tighter than the pre-run prompt estimate.

The 2026-09-29 paid stress run ended at `incomplete_harness_cap`: 2,758,826
aggregate input tokens, but only 241,119 tokens in the current context. The
expected automatic trigger is approximately 244,800 tokens (90% of the cached
272,000-token model window), leaving a 3,681-token observed gap. The provider
reported a 258,400-token usable window. No default-window compaction, Haiku
review, or restart recovery was observed. These are separate from the successful
14,022-token controlled automatic-compaction test. Further paid testing was
stopped in the [live verification record](live-provider-verification-2026-09-29.md).

## Zero-paid source findings and completed preflight

- The protected app-server emits `context_used` from the provider's **last**
  `totalTokens` value (`rust/doxa-engines/src/codex_appserver.rs`). Aggregate
  input counts repeated requests and cannot locate the current trigger. No
  measurement defect was confirmed by this read-only audit.
- The credential-free loopback fixture
  (`scripts/codex-protected/verify_automatic.py --default-window`) now exercises
  automatic review with a 272,000-token model context and no explicit compact
  token override. On the installed protected provider, the allow path completed
  one checkpoint after three loopback requests; the stopped path interrupted
  with no checkpoint and only the first request. Both used zero paid requests.
  A synthetic hook response cannot establish real Haiku review.
- The pinned PreCompact hook rejects an owned rollout over 32 MiB or a JSONL
  line over 1 MiB, and its review has a 180-second worker timeout within the
  240-second hook deadline (historical `rust/doxa-engines/codex_compact_hook.py`
  in the `v2.0.0-alpha.72` tag). A live
  fixture must keep individual prompts below the line limit. The LORE review
  prompt uses a capped session digest; it does not send the entire rollout to
  Haiku verbatim.

## Pre-run authenticated protocol

1. Start a fresh authenticated native DOXA session with the default model
   window and no threshold override. Send deterministic synthetic text in
   frames smaller than 63 KiB, using at most 20,500 two-letter words per data
   prompt. Request a short reply. After each completed turn, record provider
   `last.totalTokens`, aggregate input, rollout size, hook events, and thread
   identity. Verify the context count increases; stop if it does not. At
   215,000 context tokens, reduce the next chunk to 8,000 words, then to
   2,000 above 230,000. Let the provider's actual
   usage, rather than the estimate, control each next step.
2. Observe the first automatic PreCompact hook, one successful native LORE
   review, one provider compaction checkpoint, and the unchanged owned thread.
   Verify a synthetic sentinel is recalled after compaction and after one
   daemon restart on that thread. Retain metadata and digest evidence only;
   remove the private transcript and synthetic memory after the check.

**Hard stop:** at most 14 submitted user turns, 1,800,000 aggregate input
tokens, one automatic compaction cycle, and one post-compaction restart. Stop
before another prompt if the provider reports 252,000 current context tokens
without the hook; if review fails, times out, or has unclear provenance; if a
prompt approaches the rollout line limit; or if any telemetry needed for these
caps is missing. Do not disable the fail-closed review to continue. A cap or
review refusal is an incomplete result, not parity. No authenticated run is
claimed by the zero-cost fixture.
