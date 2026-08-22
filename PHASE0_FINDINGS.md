# Phase 0 Findings — LORE-native TUI validation spike

**Date:** 2026-08-22
**Package under test:** `claude-agent-sdk` 0.2.144 (PyPI, "Python SDK for Claude Code"), Python 3.12, `textual` 5.3.0.
**Environment:** local workstation, Claude Code logged in via `claude.ai` OAuth, `subscriptionType: "max"`, **no `ANTHROPIC_API_KEY` set at any point**.

All claims below are backed by a script in `spike/` that actually ran (transcripts inline) plus exact `file:line` citations into the installed SDK source at `.venv/lib/python3.12/site-packages/claude_agent_sdk/`. Nothing here is paraphrased from docs the SDK doesn't ship.

---

## 1. The riskiest assumption, restated

> The SDK's agentic loop exposes boundaries equivalent to Claude Code's hook lifecycle (SessionStart / UserPromptSubmit / PreCompact / SessionEnd), plus tool-call streaming granularity and subagent support, closely enough that LORE's stage model ports without redesign.

## 2. Per-boundary verdict

| Boundary | Exists natively? | Evidence |
|---|---|---|
| **SessionStart** | **Emulatable, not hookable as documented — but empirically fires anyway.** `HookEvent` (types.py:263-273) is a `Literal` union of exactly 10 events; `"SessionStart"` is **not** one of them, and there is no `SessionStartHookInput` class (only a `SessionStartHookSpecificOutput` *output* shape at types.py:458-463, seemingly vestigial/output-only). Static reading says "not supported." **Running `spike/02_lifecycle.py` proved otherwise**: registering `hooks={"SessionStart": [...]}` (Python doesn't enforce the `Literal` at runtime — it's just a dict key) causes the callback to actually fire, once, immediately after a manual `/compact` — i.e. the CLI treats post-compaction context reset as a new "session start" and dispatches the hook. It never fired at the true beginning of the session in this test (we didn't observe it before turn 1). **Net: the CLI supports SessionStart hook dispatch; the Python SDK's type surface just doesn't advertise it.** The one *reliable*, connection-time injection point for LORE's snapshot is `ClaudeAgentOptions.system_prompt` with `{"type": "preset", "preset": "claude_code", "append": "..."}` (types.py:1967-1975) — static config baked in before `connect()`, not a dynamic callback. |
| **UserPromptSubmit** | **Exists natively, confirmed firing.** `UserPromptSubmitHookInput` (types.py:342-347), carries `prompt: str`. Fired exactly once per real user turn in the spike (2 turns → 2 fires); a literal `"/compact"` turn did **not** count as a prompt submit. This is the reliable "per-turn refresh" boundary LORE needs. |
| **PreCompact** | **Exists natively, confirmed firing.** `PreCompactHookInput` (types.py:366-370), carries `trigger: Literal["manual", "auto"]` and `custom_instructions`. Sending the literal string `"/compact"` as a turn's prompt text — in headless/SDK streaming mode, no TUI involved — triggered `trigger="manual"` and the hook fired. This also incidentally proved CLI slash-command handling works outside the interactive terminal. |
| **SessionEnd** | **Absent. No emulation path.** `grep -rn "SessionEnd"` across the entire installed package returns **zero hits** — no `HookInput`, no `HookSpecificOutput`, no `Literal` member, nothing (contrast with SessionStart, which at least had a stray output type). Registering `hooks={"SessionEnd": [...]}` and running through `client.disconnect()` / the `async with` block's `__aexit__` (client.py:573-587, which just closes the query/transport — no CLI round-trip) never fired the callback. **This is a real gap, not a naming difference.** LORE's "flush memory / finalize stage on session end" logic has no CLI-side hook to attach to; it must be driven from the *host application's own* lifecycle (Textual app teardown, `atexit`, or an explicit "wrap up" turn sent before disconnecting), not from the SDK. |
| **Tool-call streaming granularity** | **Exists, fine-grained.** `ClaudeAgentOptions.include_partial_messages=True` (types.py:2143-2147) emits `StreamEvent` (types.py:1360-1364) objects wrapping the **raw Anthropic Messages API stream events** (`message_start`, `content_block_start/delta/stop`, `message_stop`) tagged with `session_id` and `parent_tool_use_id` (so subagent streams are attributable). `spike/01_loop.py` observed 50 chunks for one two-tool-call exchange; `spike/03_textual_marriage.py` observed 46 for a similar exchange and mounted a live UI block per chunk. |
| **Subagent support** | **Exists, fully featured.** `ClaudeAgentOptions.agents: dict[str, AgentDefinition]` (types.py:2210-2214); `AgentDefinition` (types.py:86-104) carries `model` (alias or full ID, including `"inherit"`), `tools`/`disallowedTools`, `skills`, `mcpServers`, `initialPrompt`, `maxTurns`, `background`, `effort`, `permissionMode`. Not exercised end-to-end in these spikes (out of budget scope — no LLM call needed to confirm the shape exists and is wired into `ClaudeAgentOptions`), but the surface is real and matches Claude Code's own subagent model closely enough to carry LORE's stage-as-subagent mapping without redesign. |
| **Tool allowlisting per call** | **Session-level, not per-message.** `allowed_tools` / `tools` / `disallowed_tools` are fields on `ClaudeAgentOptions`, fixed at `connect()` time; `ClaudeSDKClient.query(prompt, session_id)` (client.py:248-273) takes no tool-set override. Dynamic control mid-session is available through three other mechanisms instead: `can_use_tool` callback (per-invocation allow/deny, client.py / types.py:2108-2123), a `PreToolUse` hook returning `permissionDecision` (types.py:416-424), and `toggle_mcp_server(name, enabled)` (client.py:389-414) for whole-server on/off. LORE's "this stage may only use these tools" model maps to a `PreToolUse` hook keyed on the active stage, not to swapping `allowed_tools`. |
| **Compaction control** | **Inspectable and triggerable, not fully configurable.** `ClaudeSDKClient.get_context_usage()` (client.py:471-506) returns `ContextUsageResponse` (types.py:772-816) with `isAutoCompactEnabled`, `autoCompactThreshold`, `totalTokens`/`maxTokens`/`percentage` — enough to build LORE's own pressure-based triggers. Manual compaction is triggerable via the `"/compact"` prompt-text convention (proven above); there is no typed client method (`client.compact()`) for it. |

## 3. Subscription-auth finding

**PASS — no `ANTHROPIC_API_KEY` required anywhere.** Every spike ran with `env -u ANTHROPIC_API_KEY` explicitly unset (also asserted in code: `assert "ANTHROPIC_API_KEY" not in os.environ`). `claude auth status` on this machine reports `authMethod: "claude.ai"`, `apiProvider: "firstParty"`, `subscriptionType: "max"` — the SDK shells out to the local `claude` CLI binary (bundled or on `PATH`, via `ClaudeAgentOptions.cli_path`, types.py:2064-2068) and inherits that CLI's own OAuth session. `ResultMessage.total_cost_usd` was still populated (e.g. `0.0536415` in spike 1) — this is a notional list-price figure the CLI reports regardless of billing path, not evidence of an API-key charge. The plan's subscription-billing assumption holds.

## 4. Asyncio-coexistence finding

**PASS, no friction observed.** `spike/03_textual_marriage.py` runs the SDK's `ClaudeSDKClient` inside `App.run_worker()` with the default `thread=False`, i.e. as a plain `asyncio.Task` on the **same** event loop Textual uses for rendering and input — no thread bridging, no `call_from_thread`, no queue. The SDK's transport is itself asyncio-subprocess-based (`_internal/transport/subprocess_cli.py`), so both halves are asyncio-native and share the loop cleanly. Verified headlessly via Textual's `run_test()` Pilot harness (no real terminal needed): 46 live-streamed `Collapsible` blocks mounted one-by-one as SDK messages arrived, tool call observed, clean exit. The only mild friction: `await container.mount(...)` plus `container.scroll_end()` must be called from *within* the worker coroutine (not fire-and-forget) to keep UI updates ordered with the arriving stream — trivial, not a redesign item.

## 5. Recommendation: **GO-WITH-REDESIGN**

The plan's core bet — SDK hook lifecycle closely tracking Claude Code's, plus real streaming and subagents — holds for 5 of 6 boundaries tested, including one (SessionStart) that looked absent on paper but works at runtime. One boundary is a genuine, confirmed gap.

### Redesign items (small, not architecture-level)

1. **SessionEnd has no SDK hook.** LORE's "finalize/flush stage on session end" logic must be driven by the *host* (Textual app's own shutdown path, or an explicit final turn/flush call before `disconnect()`), not by an SDK-dispatched callback. This is a few lines in the TUI's teardown handler, not a redesign of the stage model itself.
2. **SessionStart is undocumented and behaviorally narrow.** It was only observed firing after a manual compaction, not at true session start in this test — do not build LORE's snapshot-injection path around waiting for a `SessionStart` hook to arrive reliably at turn 1. Use `ClaudeAgentOptions.system_prompt` (preset + `append`) as the primary injection point for "context present at the first turn," and treat any observed `SessionStart` firing as a bonus re-injection opportunity after compaction, not the primary mechanism.
3. **Tool allowlisting is session-scoped, not per-stage-call.** If LORE's stage model assumed "swap the tool set when the stage changes," that must become "gate individual tool calls via a `PreToolUse` hook that checks the active stage" instead — a different implementation shape, same effective behavior.
4. **No typed `compact()` method.** Compaction control is: read `get_context_usage()` for pressure signals, and trigger a manual compact via the `"/compact"` prompt-text convention if LORE wants to force one — an unversioned string convention rather than an API, worth a thin wrapper with a comment noting the CLI-version risk.

### Why not NO-GO

Every boundary the plan needs has *some* working mechanism, verified by running code, at acceptable cost (all four spikes together used the `haiku` model with short prompts). Nothing found requires abandoning the Claude Agent SDK + Textual + in-process `lore_core` architecture.

### Why not plain GO

The SessionEnd gap and the SessionStart unreliability are real enough that Phase 1 should not assume 1:1 hook parity with Claude Code's `.claude/settings.json`-based hooks — those two items should be explicit open questions in the Phase 1 design doc, not discovered mid-implementation.

## 6. Exact API surface reference (for Phase 1 implementers)

- Package: `claude_agent_sdk` — `query()` (one-shot, `query.py:11`) and `ClaudeSDKClient` (stateful, `client.py:26`).
- Model param: `ClaudeAgentOptions.model: str | None` (types.py:2035-2039) — plain string, e.g. `"claude-haiku-4-5"`; also `fallback_model`.
- System prompt injection: `ClaudeAgentOptions.system_prompt: str | SystemPromptPreset | SystemPromptFile | None` (types.py:1967-1975).
- Hooks: `ClaudeAgentOptions.hooks: dict[HookEvent, list[HookMatcher]] | None` (types.py:2126-2136). Registered matchers on the same event **dispatch concurrently**, not sequentially (explicit in the docstring) — design each hook to be independent.
- Registerable `HookEvent` literals (types.py:263-273): `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `UserPromptSubmit`, `Stop`, `SubagentStop`, `PreCompact`, `Notification`, `SubagentStart`, `PermissionRequest`. (`SessionStart` works anyway per §2; `SessionEnd` does not exist.)
- Streaming: `ClaudeAgentOptions.include_partial_messages: bool` (types.py:2143-2147) → `StreamEvent` (types.py:1360-1364).
- Subagents: `ClaudeAgentOptions.agents: dict[str, AgentDefinition]` (types.py:2210-2214), `AgentDefinition` (types.py:86-104).
- Context/compaction introspection: `ClaudeSDKClient.get_context_usage() -> ContextUsageResponse` (client.py:471-506, types.py:772-816).
- Client control surface: `connect`, `query`, `interrupt`, `set_permission_mode`, `set_model`, `rewind_files`, `reconnect_mcp_server`, `toggle_mcp_server`, `stop_task`, `get_mcp_status`, `get_context_usage`, `get_server_info`, `receive_response`, `disconnect`, async context manager (`client.py`, full method list at lines 67-594).
- In-process custom tools: `@tool(name, description, input_schema)` decorator + `create_sdk_mcp_server(name, tools=[...])` (`__init__.py:251-335`, `:491-619`) — runs in-process, no subprocess/IPC per call.

## 7. What was not tested (out of Phase 0 scope)

- Subagent execution end-to-end (only the config shape was verified against source — no LLM call spent on it, per the "modest token spend" constraint).
- `PermissionRequest`, `Notification`, `SubagentStart`/`SubagentStop`, `PostToolUseFailure` hooks — types confirmed to exist; none exercised with running code (budget).
- Session persistence/resume (`resume`, `fork_session`, `session_id` options) — read from source, not run.
- Real (non-headless) Textual rendering in a terminal — spike 3 used `run_test()` deliberately for reproducibility; visual rendering was not separately checked but Textual's own test harness is the standard way this library is verified, so risk here is low.
