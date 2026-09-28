# Live provider verification — 2026-09-28

## Native DeepSeek and z.ai / GLM

**Result: live checks blocked by missing credentials; zero paid requests.**

The production Rust guarded resolver (`doxa_vendors::credentials::status`
and `resolve`) ran against the original effective DOXA home before any home
isolation. Both providers reported `missing`; neither reported guard rejection.
Neither `DEEPSEEK_API_KEY` nor `ZAI_API_KEY` was inherited, and the effective
saved store did not exist. No key was printed, persisted, put in argv, or fetched
from project files or memory. The original store and environment preferences
were not modified.

| Provider | Authentication availability | Live catalog / efforts | Native SSE / tool / history | Paid requests / usage / cost |
| --- | --- | --- | --- | --- |
| DeepSeek | Missing | Unverified | Unverified | 0 / none / $0 incurred by this check |
| z.ai (`glm`) | Missing | Unverified | Unverified | 0 / none / $0 incurred by this check |

Preflight and the opt-in launcher both completed successfully with this metadata:

```json
{"credential_check":[{"credential_available":false,"credential_source":"missing","provider":"deepseek"},{"credential_available":false,"credential_source":"missing","provider":"glm"}],"paid_requests":0}
```

The preflight was reconfirmed at `2026-09-28T15:21:41Z`. The baseline was
`f5f7f217` (`2.0.0-alpha.38`). The native daemon was available
at `/home/docwilde/.cache/doxa-native-target-credentials/debug/doxa-daemon`;
the native carrier was `/home/docwilde/.local/bin/lore-rs`. The launcher was
compiled with `cargo run -j 1`; Python source was checked with `py_compile`.
The credential preflight, the launcher's missing-credential `--live` path,
and the Python child's missing-credential branch were executed.
The paid branch of the verifier has **not** been exercised against either vendor.

### Credential-free native fixture verification

Review caught an obsolete `--lore-python` flag in the initial, unexercised paid
branch. It has been removed. The verifier now launches a separate process group
and cleans up the entire group, including descendants that ignore SIGTERM.

`tests/test_native_live_vendor_verifier.py` executes the actual native daemon
built with `local-test-server`, a loopback SSE vendor, synthetic credentials,
and disposable homes. Only catalog discovery is adapted with a synthetic flat
native reply, because its production URL is fixed. No fixture key is sent to a
real provider; the native test endpoint also suppresses DeepSeek balance fetches.
This fixture exercises startup, flat model/effort/status replies, two native
turns, reasoning/text callbacks, one actual workspace read, aggregate token
usage, committed history, and timeout cleanup with a SIGTERM-resistant child.
Both tests passed for the fresh build; the successful cases contain exactly
three loopback HTTP requests per vendor. This is local fixture evidence, not
live account verification.

The exact request assertions exposed DeepSeek effort nested inside `thinking`.
The native builder now sends top-level `reasoning_effort`, as specified by the
[official thinking guide](https://api-docs.deepseek.com/guides/thinking_mode/).
Its assistant tool-continuation message now retains `reasoning_content`; the
loopback DeepSeek fixture rejects a missing field with HTTP 400. Reasoning is
not included in verification reports or the public JSONL transcript.

```bash
TMPDIR=/home/docwilde/.d39 \
CARGO_TARGET_DIR=/home/docwilde/.cache/doxa-native-target-vendor-live-fixture \
cargo build -j 1 -p doxa-daemon --features local-test-server

TMPDIR=/home/docwilde/.d39 \
DOXA_NATIVE_DAEMON=/home/docwilde/.cache/doxa-native-target-vendor-live-fixture/debug/doxa-daemon \
python3 -m unittest discover -s tests -p test_native_live_vendor_verifier.py -v
```

### Reproduce safely

Run from a checkout after configuring keys through DOXA's `/setup` or inherited
environment. The launcher uses the same guarded saved-store resolution as the
native provider, including saved-key precedence and unsafe-store rejection.
Keys are resolved before disposable home isolation and passed only in the child
environment. Do not put key values in a shell command, fixture, or log.

```bash
TMPDIR=/home/docwilde/.d39 \
CARGO_TARGET_DIR=/home/docwilde/.cache/doxa-native-target-credentials \
cargo run -j 1 -q -p doxa-vendors --example verify_native_live -- --check
```

The following is an explicit opt-in to paid requests for available accounts:

```bash
TMPDIR=/home/docwilde/.d39 \
CARGO_TARGET_DIR=/home/docwilde/.cache/doxa-native-target-credentials \
DOXA_NATIVE_DAEMON=/home/docwilde/.cache/doxa-native-target-credentials/debug/doxa-daemon \
DOXA_LORE_RS=/home/docwilde/.local/bin/lore-rs \
cargo run -j 1 -q -p doxa-vendors --example verify_native_live -- --live
```

The child creates a mode-0700 synthetic workspace with one random-token file.
It isolates `HOME`, `DOXA_HOME`, `CODEX_HOME`, `LORE_ROOT`, and
`LORE_PROJECTS_DIR`; turns memory and peer integration off; and enables only
the read-only `workspace_read` tool. It does not inherit project paths, endpoint
overrides, or other provider settings. No endpoint override is accepted.

The intended protocol is the native daemon Unix socket driving provider Chat
Completions SSE at the fixed production endpoints:

- DeepSeek: `https://api.deepseek.com/chat/completions`, catalog
  `https://api.deepseek.com/models`, optional balance
  `https://api.deepseek.com/user/balance`.
- z.ai: `https://api.z.ai/api/paas/v4/chat/completions`, catalog
  `https://api.z.ai/api/paas/v4/models`.

The verifier requires a verified account catalog before sending paid prompts.
It selects `deepseek-flash` or `glm-5.3-flash` only if that exact ID is advertised
and the native configuration controls accept `low`. If the preferred model or
capability is unavailable, it stops without guessing another paid model.

Each account receives at most two submitted turns, with a 90-second deadline
per turn and short output instructions. The first asks for exactly one read of
the synthetic file and a short token answer. The second tests retained history
without tools, after applying the next-turn effort control (`none` on DeepSeek,
`low` on GLM). It records catalog/control results, native reasoning/text event
counts, native completion and usage metadata, persisted paired-message roles,
and the final native status (including DeepSeek balance when available).
It does not derive a billed dollar cost from aggregate token counts.

**Instrumentation limit:** the native workspace-read gate does not expose a
workspace tool callback or the transport's HTTP request count. Matching the
random file token can prove that a read occurred; this verifier labels the count
`at_least_one_proved_by_random_token` and leaves `chat_request_count` null.
The exact single-read count and exact HTTP request count therefore remain
unverified even when the token/history check passes. The first tool turn may
contain multiple HTTP requests. Output instructions and the deadline bound
this check; they are not a provider-enforced output-token cap.

### Official contract checked

Current documentation was consulted without authenticating or sending prompts:

- [DeepSeek model catalog](https://api-docs.deepseek.com/api/list-models/)
  documents model-specific `effort.supported_levels` and `default_level`.
- [DeepSeek thinking mode](https://api-docs.deepseek.com/guides/thinking_mode/)
  documents thinking enable/disable and `reasoning_effort` low/high/max.
- [z.ai Chat Completion](https://docs.z.ai/api-reference/llm/chat-completion)
  documents SSE, `reasoning_content`, tool calls, and GLM-5.3 / GLM-5.3-FLASH
  low/high/max effort. This documents a request contract, not account access.

Documentation checks do not establish authentication, account availability,
native callback delivery, successful tool execution, billing, or turn/history
success. All those live outcomes remain unverified for this run.
