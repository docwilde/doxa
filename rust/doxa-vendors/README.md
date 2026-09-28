# DOXA vendor chat adapter (Rust 2.0 preview)

`doxa-vendors` implements bounded chat-completions SSE transport and a tool turn loop for DeepSeek and GLM. The native daemon connects it through `VendorHost` and `NativeVendorGate`. Both receive root-level `reasoning_effort` and a separate `thinking` toggle; GLM refuses `none`. Neither receives `max_tokens`.

## Credentials

Use the masked DeepSeek or z.ai fields in DOXA's `/setup`, or inherit `DEEPSEEK_API_KEY` / `ZAI_API_KEY`. Private saved keys override environment keys. Removing an override restores environment fallback. The owner-only plaintext `DOXA_HOME/credentials.json` store uses checked directory descriptors, atomic writes and a bounded lock; unsafe stores fail closed. Project files and LORE memory are not credential sources.

Each new turn, model catalog and DeepSeek balance request resolves credentials afresh. A running turn keeps its authentication key fixed across tool steps. Keys travel in the Authorization header and are not retained in a client struct. Exact known keys are removed from request context and successful retained history; completed tool metadata is masked before reaching gates. The host applies canonical LORE scrubbing before transcript boundaries, and the workspace gate rejects the credential path before opening it. Provider error bodies are discarded after parsing bounded, sanitized codes.

## Bounds and tool ownership

The SSE decoder caps a line at 1 MiB and a response at 64 MiB. Tool arguments cap at 1 MiB with at most 128 calls. Cancellation drops an in-flight request. `run_turn` limits a turn to 24 tool steps, 512 messages / 8 MiB of history, 1 MiB per tool result and a deadline of at most 3600 seconds; it sums provider-reported usage and commits history only on success. Failed turns may have executed a tool and must not be retried blindly.

A concrete `ToolGate` owns approval and execution. The adapter validates offered names and refuses unoffered calls; gate errors are not sent to the provider. The daemon exposes explicitly enabled workspace reads and approved native LORE, peer and session tools. Callers remain responsible for canonical scrubbing of secrets other than known vendor keys.

## DeepSeek replay

Thinking with tools requires prior assistant `reasoning_content`. The daemon
preserves final-completion reasoning in its private paired replay file, with
owner-only access and a 1 MiB UTF-8 bound per field before and after scrubbing.
Reads are capped at 16 MiB; intermediate tool reasoning remains turn-local.
Public JSONL contains user and final assistant text. Legacy history missing
reasoning still loads, but thinking with tools refuses before HTTP submission;
start a new session or launch with `--effort none` to retain that history.

## Verification

CI enables the loopback-only transport and exercises synthetic keys against local HTTP fixtures, including rotation in existing history, inactive-key metadata, storage attacks, cancellation and tool gates. Production URLs are fixed. Fixtures make no paid requests and do not establish live account availability, catalog contents or pricing.

See the [provider verification record](../../docs/live-provider-verification-2026-09-28.md)
for opt-in commands, actual CLI checks and outstanding account authentication.
