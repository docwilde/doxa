# Live provider verification — 2026-09-28

## Fresh authentication and resume verification

The completion run used isolated worktree `codex/live-providers-20260928` at
`ac0097f` (alpha.39), with all disposable homes and build files beneath
`/home/docwilde/.d40`. The production guarded resolver was run again against
the original effective DOXA home before isolation. DeepSeek and z.ai still
reported `missing`, with no guard rejection. Both `--check` and `--live`
returned zero paid requests; no vendor account request was sent.

Claude Code 2.1.283's fresh `auth status --json` reported `loggedIn: false`,
`authMethod: none`, and `apiProvider: firstParty`. Native LORE 0.62.2's review
worker uses Claude for derivation even when reviewing a Codex source; an
authenticated Codex session alone does not provide that reviewer authentication.
A successful provider-backed LORE review remains unverified with this account
state.

At `2026-09-28T16:50:39Z`, the actual Codex 0.156.1 account catalog returned
seven models and accepted native `set_model` / `set_effort`. Stock protected
startup then refused the first submitted prompt with
`Codex build has no verified DOXA compaction hook contract`. There were no
text/tool events, no successful native turn, and no provider thread to resume
in this fresh run. Earlier successful turns below remain historical evidence;
they do not establish compatibility of this fresh protected startup.

The vendor verifier now uses its existing **two submitted turn** allowance to
verify resume: it completes the first file-read turn, applies the next-turn
effort control, stops and reaps the native daemon, resumes the same session
with the selected model and effort, then submits the retained-token turn. It
checks the resumed hello configuration before that second prompt. This path
has passed against the real native daemon with loopback SSE for **both**
DeepSeek and z.ai. Live account resume remains blocked by their missing keys.
The temporary directory prefix is shortened to leave room for native Unix
socket paths under an explicit disk-backed `TMPDIR`.

## Native Codex and Claude

These checks used the actual installed provider CLIs and the Rust DOXA daemon,
private SSD workspaces and isolated DOXA/LORE stores. Memory review was disabled
for these synthetic probes. Credentials were never printed, recorded in argv
or included in output; the temporary Codex authentication copy was removed.

| Provider | Actual observations | Remaining live verification |
| --- | --- | --- |
| Codex 0.156.1 | Account catalog returned 7 models. Two native turns each read a synthetic local file, emitted streamed text and one tool call, and reported complete usage. Model and effort changes were accepted on the same thread. | Successful native LORE review followed by provider compaction; large-context automatic compaction. |
| Claude Code 2.1.283 | Native initialization returned 8 models. CLI authentication reported logged out; the submitted native turn failed with no text or tool events. | Authenticated streaming, tools, current-session controls and quota. |

The initial Codex turn ran on alpha.38. A second turn used the compaction
integration build; with review disabled, `/compact` was refused and the provider
rollout digest stayed unchanged. The alpha.39 build then resumed the same owned
thread and refused compaction again without submitting an inference turn.
The refusal is an expected protection result, not a successful compaction test.
These short probes are compatibility checks, not latency benchmarks.

Independent fixtures verify that stalled source lookup cancellation returns in
under one second, sends no compaction request, preserves source bytes and clears
the durable restart guard only after successful persistence. A follow-up turn
resumes the original thread. Native approval receipts, carrier/manifest replacement,
held stdout descendants and unreviewed automatic events have separate fixtures.

The historical stock Codex checks above predate alpha.40's private fail-closed
build. The stock provider can continue after hook infrastructure failures and
contains independent triggers outside the configured token threshold; DOXA now
refuses it for protected turns. See the
[engine contract](../rust/doxa-engines/README.md#compaction-review) and pinned
[turn implementation](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/core/src/session/turn.rs),
[context-window cap](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/core/src/session/context_window.rs)
and [model threshold logic](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/protocol/src/openai_models.rs).

To unblock Claude's account check, run `doxa auth login claude` or `claude auth login`.
Configure vendor keys in `/setup` or inherited environment before the opt-in
checks below. Do not put credentials in project files, prompts or memory.

## Private protected Codex completion checks

The private app server builds official source
`b412ff32c417f855c2b2d1581b77058eed87c84b` with reviewed patch SHA-256
`d6c8a41c0370c12dcace10d6babe13de7852f0095fed7b46289b38e7a6cd0f4b`.
Its compiled contract is `doxa-precompact-fail-closed-v1`; the checked executable
SHA-256 is `f9fee41f3ef5eddd362df199d5d5253fec802ffaaa5f2c4baea9c2e04e3723cd`.
It was built with private Rust 1.95 and the unoptimized `dev-small` profile;
these checks establish behavior, not optimized performance.

`scripts/codex-protected/verify_automatic.py` drives that actual executable with a
credential-free loopback Responses model. Nine cases (missing hook/carrier,
timeout, malformed/empty/plain output, stopped, asynchronous and duplicate hooks)
complete the first turn and interrupt the automatic-compaction turn. Each makes
one original-model HTTP request, zero compaction requests and zero history
replacements, preserving the first assistant message. The explicit allow control
makes three requests and one real compacted checkpoint. The missing-carrier
fixture executes a shell that returns 127; the compiled hook parser separately
covers operating-system spawn failure. No case uses a paid account.

At `2026-09-28T17:24:29Z`, an actual authenticated check used the private native
launcher, an alpha.39-labelled integration daemon and native LORE 0.62.2. Catalog
returned seven models; model `gpt-6-sol` and effort `low` were accepted. Two real
turns completed with streamed text and complete reported usage. Stopping and
restarting the native daemon resumed the exact same provider thread. Enabled
manual compaction started native LORE review, refused with the reviewer logged
out, and preserved the exact rollout digest.

The file-read prompt produced a short text-only response, zero tool events and no
matching synthetic file token. A second bounded pair with explicit `cat` had the
same outcome. The replies do not establish tool or content-recall compatibility;
no tool configuration was independently observed. Further paid retries stopped.
No credential, file token, prompt/reply text or private rollout is included in the
verification record, and temporary authentication copies were removed.
Successful live reviewer compaction and large-context automatic compaction still
need Claude reviewer authentication. Token usage does not establish billed cost.

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
usage, committed history, native stop/resume before the second turn for both
providers, timeout cleanup with a SIGTERM-resistant child, and failed-resume
startup cleanup without a second submitted turn.
All six verifier fixture tests passed for the fresh build; the successful cases contain exactly
three loopback HTTP requests per vendor. This is local fixture evidence, not
live account verification.

The exact request assertions exposed DeepSeek effort nested inside `thinking`.
The native builder now sends top-level `reasoning_effort`, as specified by the
[official thinking guide](https://api-docs.deepseek.com/guides/thinking_mode/).
Its assistant tool-continuation message now retains `reasoning_content`; the
loopback DeepSeek fixture rejects a missing field with HTTP 400. Reasoning is
not included in verification reports or the public JSONL transcript.

The fixture now requires **two consecutive low-thinking DeepSeek turns with
tools**, and rejects missing prior-assistant reasoning on the next user turn.
The final completion's reasoning is preserved in the private paired provider
replay file as an optional assistant-only `reasoning_content` string. The file
must be mode 0600 to load reasoning, and each reasoning field is bounded to
1 MiB of UTF-8 bytes before and after scrubbing. Intermediate tool messages
and their reasoning remain turn-local. Public JSONL stays user/final text;
history/verification reports contain roles and counts, not private reasoning.

A real native stop/resume fixture proves replay of the saved field, constructor
redaction of a synthetic credential in that field, and absence of reasoning in
public JSONL. A none→low fixture proves an observed empty reasoning field can
be replayed. Managed compaction's synthetic assistant summary explicitly carries
empty reasoning; that field describes a constructed summary and does not
pretend to recover discarded historical thinking.

Older paired files without reasoning still load, but DeepSeek thinking with
tools refuses before an HTTP request if earlier assistant reasoning is missing.
The old history is preserved and the native error says to start a new session
or restart with `--effort none`. This names a native launch option rather than
assuming an account catalog exposes a thinking-off choice among enabled effort
levels. No legacy trace is inferred from the current effort setting.
This recovery behavior is covered by a transport test with zero accepted
loopback connections. Private-store tests cover role/type/size rejection,
redaction expansion beyond the bound, permission rejection, atomic failed saves,
and comparison of public transcript against private replay's visible projection.
The touched replay reader also caps actual reads at 16 MiB plus one byte and
rejects overflow after reading, so concurrent file growth cannot bypass the
metadata size check.

Run from the repository root. Keep builds and disposable homes on disk:

```bash
verify_dir="$HOME/.cache/doxa-verify"
mkdir -p "$verify_dir/tmp"
chmod 700 "$verify_dir" "$verify_dir/tmp"
export TMPDIR="$verify_dir/tmp"
export CARGO_TARGET_DIR="$verify_dir/target"

cargo build --locked -j 1 -p doxa-daemon --features local-test-server
DOXA_NATIVE_DAEMON="$CARGO_TARGET_DIR/debug/doxa-daemon" \
  python3 -m unittest discover -s tests -p test_native_live_vendor_verifier.py -v
```

### Reproduce safely

Run from a checkout after configuring keys through DOXA's `/setup` or inherited
environment. The launcher uses the same guarded saved-store resolution as the
native provider, including saved-key precedence and unsafe-store rejection.
Keys are resolved before disposable home isolation and passed only in the child
environment. Do not put key values in a shell command, fixture, or log.

Using the disk-backed paths above, build the production daemon and check only
credential availability:

```bash
cargo build --locked -j 1 -p doxa-daemon
cargo run --locked -j 1 -q -p doxa-vendors --example verify_native_live -- --check
```

The following is an explicit opt-in to paid requests for available accounts:

```bash
DOXA_NATIVE_DAEMON="$CARGO_TARGET_DIR/debug/doxa-daemon" \
DOXA_LORE_RS="$HOME/.local/bin/lore-rs" \
  cargo run --locked -j 1 -q -p doxa-vendors --example verify_native_live -- --live
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
the synthetic file and a short token answer. The second tests retained history after native daemon stop/resume
without tool calls, after reapplying the next-turn `low` effort control for
both providers and checking the resumed configuration. Tool definitions remain available so the DeepSeek check exercises
the documented prior-turn reasoning replay requirement. It records catalog/control results, native reasoning/text event
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

## Alpha.41 Code Mode dependency verification

The alpha.40 account check above did not prove file reads: its private Codex
package omitted `codex-code-mode-host`, which `gpt-6-sol` requires for its
`code_mode_only` tool profile. Alpha.41 builds that helper from the same pinned
Codex source and installs it with verified V8 inputs, a receipt-bound payload
and a native dispatcher that rechecks the payload at execution.

The actual compiled package passed three credential-free scenarios:

| Boundary | Observed result |
| --- | --- |
| Protected app server and installed helper dispatchers | Native command completed with exit zero and returned the unpredictable workspace token. |
| Same compiled server with an owned missing-helper fault | No file read or command event; the tool output reported the missing host. |
| Installed DOXA daemon and native LORE carrier | One correlated command call, successful result and exact output detail; final response contained the returned token. |

Each scenario made exactly two local HTTP requests. The first request contained
no fixture token; the loopback model generated its final answer only from the
returned tool output. Fixtures stayed unchanged, no credentials or paid requests
were used, and owned temporary directories were removed. Eight stdlib tests
cover the proof's inventory, leakage, output and event-correlation checks.

Reproduce with `scripts/codex-protected/verify_code_mode.py --help`; use the
installed private launcher for `--server` and `--launcher`, the compiled server
payload for `--negative-server`, and native binaries for `--daemon` and `--lore`.
These checks establish tool execution and DOXA normalization.

### Authenticated file read and same-thread recall

At `2026-09-28T18:11:14Z`, the installed alpha.40 native daemon was checked
with alpha.41's staged, verified private server and both native dispatchers.
`gpt-6-sol` with low effort accepted model and effort controls. Exactly two
turns were submitted, with no retry: the first emitted one correlated successful
command call/result/detail, read the synthetic file and returned its exact token.
After stopping and restarting DOXA, the same provider thread recalled that
token with zero tool calls. Both turns reported complete usage.

Neither prompt contained the token. The fixture and original authentication
were unchanged; no owned authentication copies, descendant processes or
temporary directories remained. Only metadata was retained, with no prompts,
source content or replies. These checks used isolated LORE stores with memory
enabled; they did not trigger compaction or establish successful LORE review.
Claude reviewer authentication is still required for that separate live gate.
