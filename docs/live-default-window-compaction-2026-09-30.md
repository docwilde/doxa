# Authenticated default-window Codex compaction — 2026-09-30

DOXA `2.0.0-alpha.52` (`9ac089c`), integrated LORE 0.62.6 and the installed
receipt-verified protected Codex 0.156.1 provider completed one automatic
compaction cycle on Linux. The model was `gpt-5.5` at low effort in a read-only,
isolated native daemon session. Neither the model's automatic threshold nor the
context window was overridden. Native LORE review remained enabled.

The bounded verifier sent seven synthetic data prompts, then restarted the
daemon and sent one recall prompt. Each data prompt stayed under the native
64 KiB frame limit. Only numeric telemetry, event counts and equality results
were printed; the temporary Codex home, LORE store, rollout and copied account
authentication were removed after the run.

| Data turn | Last reported context tokens | Aggregate input tokens | Native review | Compacted records |
| ---: | ---: | ---: | --- | ---: |
| 1 | 47,293 | 59,288 | — | 0 |
| 2 | 94,806 | 166,089 | — | 0 |
| 3 | 135,834 | 313,918 | — | 0 |
| 4 | 176,862 | 502,775 | — | 0 |
| 5 | 217,890 | 732,660 | — | 0 |
| 6 | 233,918 | 978,573 | — | 0 |
| 7 | 181,612 | 1,172,180 | started and completed once | 1 |

The seventh turn completed without error and retained the requested short
reply. After one daemon restart, the eighth prompt recalled the first turn's
random 16-character token exactly. One owned rollout file remained, with one
compaction checkpoint and no second review during recall. The run used eight
submitted turns, below the approved 14-turn limit; the last recorded aggregate
input before recall was 1,172,180, below the 1,800,000-token cap. The verifier
did not retain the final aggregate count or the provider thread ID after recall,
so those two details are not claimed from this run.

This verifies the authenticated default-window path for this exact provider,
model and build. It does not establish the behavior of every Codex model or
optional account state. Aggregate input counts repeated context sent to the
provider and are not a billed cost. The earlier [September 29 stress run](live-provider-verification-2026-09-29.md#codex-default-window-automatic-compaction)
remains an incomplete historical check; its 241,119-token stopping point does
not contradict this later successful cycle.

The reproducible [bounded verifier](../scripts/codex-protected/verify_live_default_window.py)
accepts explicit daemon, protected Codex, native LORE, account-home and private
scratch paths. Its checked-in form additionally isolates child temporary files,
ignores ambient Codex API keys, waits for complete rollout records, checks
headroom before recall, and checks provider thread identity and final aggregate
usage after restart. The checked-in script was syntax-checked and its rollout
and path guards were exercised locally; the paid cycle above ran the equivalent
earlier in-memory harness and was not repeated after that refactor.

## Verifier review

A three-model review of the checked-in verifier used Codex, DeepSeek V4.1 Flash
and GLM 5.3. The follow-up closes the shared temporary-file and live-rollout
findings, adds account credential isolation, private ancestor checks, reply
validation, a pre-recall budget guard and shutdown-race handling. The Rust
adapter's usage is session cumulative and its registry write is atomic, so
the two claims assuming otherwise did not apply. These verifier edits were
checked without repeating the paid provider cycle.
