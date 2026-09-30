# Codex default-window automatic compaction: bounded verification plan

The 2026-09-29 paid stress run ended at `incomplete_harness_cap`: 2,758,826
aggregate input tokens, but only 241,119 tokens in the current context. The
expected automatic trigger is approximately 244,800 tokens (90% of the cached
272,000-token model window), leaving a 3,681-token observed gap. The provider
reported a 258,400-token usable window. No default-window compaction, Haiku
review, or restart recovery was observed. These are separate from the successful
14,022-token controlled automatic-compaction test. Further paid testing was
stopped in the [live verification record](live-provider-verification-2026-09-29.md).

## Zero-paid source findings

- The protected app-server emits `context_used` from the provider's **last**
  `totalTokens` value (`rust/doxa-engines/src/codex_appserver.rs`). Aggregate
  input counts repeated requests and cannot locate the current trigger. No
  measurement defect was confirmed by this read-only audit.
- The credential-free loopback fixture
  (`scripts/codex-protected/verify_automatic.py`) exercises automatic review
  refusal and an explicit allow control at a deliberately lowered threshold.
  It does not exercise the default model threshold or real Haiku review.
- The pinned PreCompact hook rejects an owned rollout over 32 MiB or a JSONL
  line over 1 MiB, and its review has a 180-second worker timeout within the
  240-second hook deadline (`rust/doxa-engines/codex_compact_hook.py`). A live
  fixture must keep individual prompts below the line limit. The LORE review
  prompt uses a capped session digest; it does not send the entire rollout to
  Haiku verbatim.

## Proposed live run, requiring fresh authorization

1. Preflight the installed receipt-verified protected Codex 0.156.1 launcher
   and its fail-closed hook with a credential-free loopback provider at the
   actual default threshold. Use an isolated private real-disk runtime and
   synthetic memory; preserve the user's normal Codex configuration and LORE
   stores. The preflight must prove the hook fires at the expected threshold,
   refuses failed reviews without compaction inference, and permits exactly one
   synthetic checkpoint after explicit review success.
2. Start a fresh authenticated native DOXA session with the default model
   window and no threshold override. Send deterministic synthetic text in
   chunks no larger than 40,000 estimated tokens and 750 KiB serialized per
   prompt. Request a short reply. After each completed turn, record provider
   `last.totalTokens`, aggregate input, rollout size, hook events, and thread
   identity. Verify the context count increases; stop if it does not. At
   230,000 context tokens, reduce the next chunk to at most 8,000 estimated
   tokens, then to at most 2,000 above 240,000. Let the provider's actual
   usage, rather than the estimate, control each next step.
3. Observe the first automatic PreCompact hook, one successful native LORE
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
review refusal is an incomplete result, not parity. The limits are a proposed
future allowance, not authority to resume paid testing under the previous
stopped run.
