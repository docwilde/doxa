# Native vendor recall and usage follow-up — 2026-09-30

The later [authenticated Codex default-window check](live-default-window-compaction-2026-09-30.md)
records one reviewed automatic compaction and recall after restart. This record
covers the DeepSeek and z.ai checks below.

This check used the alpha.50 native daemon from `5d05510` with the verifier
instrumentation in `e524735`. The guarded launcher found saved credentials for
DeepSeek and z.ai; it passed them only to children with disposable mode-0700
homes, a synthetic workspace, LORE disabled, and the read-only workspace tool.
The launcher sent at most two short turns per provider. No nonce, credential,
prompt, reply, reasoning text, account identity or private path is retained here.

| Provider and model | Synthetic file answer | Stop/resume recall | Reported usage, first turn | Reported usage, resumed turn |
| --- | --- | --- | --- | --- |
| DeepSeek `deepseek-flash`, low effort | Exact 16-character nonce; one occurrence | Exact nonce; paired `user, assistant, user, assistant` history | 884 prompt, 52 completion tokens | 444 prompt, 113 completion tokens |
| z.ai `glm-5.3-flash`, low effort | Exact 16-character nonce; one occurrence | Exact nonce; paired `user, assistant, user, assistant` history | 586 prompt, 28 completion tokens | 314 prompt, 15 completion tokens |

Every turn ended without a native error, identified the selected model, and
reported complete turn-scoped usage from the vendor response. Both resumed
daemons retained the selected model and low effort. Matching the random file
nonce proves at least one workspace read; the verifier does not count exact
workspace reads or underlying HTTP requests. The counters are reported usage,
not independently verified billing or a dollar cost.

This closes the current native stop/resume and numeric GLM usage checks. The
cause of the earlier DeepSeek candidate's exact-nonce failure is still unknown;
the successful current run does not reconstruct that old run. The earlier
three-turn streaming observations remain in the
[September 29 record](live-provider-verification-2026-09-29.md).

Claude's optional overage and model-specific quota event variants have not
been observed live. The quota parser has fixture coverage, but this check did
not manufacture events or claim an account state that the CLI did not report.

The six credential-free native verifier fixture tests passed against the
alpha.50 daemon with `local-test-server`. A short disk TMPDIR was required so
the native Unix socket path stayed within the platform limit. The live checks
used the same short, isolated disk parent. All paid checks have stopped.
