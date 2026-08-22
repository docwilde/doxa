"""Phase 0 spike 2/3 -- lifecycle hooks vs Claude Code's hook model.

Investigates, with RUNNING code, whether claude-agent-sdk 0.2.144 exposes
Python-callback equivalents of Claude Code's hook lifecycle:
  - SessionStart   -> claude_agent_sdk.types.HookEvent does NOT include
                      "SessionStart" (confirmed by reading types.py:263-273).
                      A SessionStartHookSpecificOutput TypedDict exists (an
                      *output* shape), but there is no SessionStartHookInput
                      and no way to register a callback for it via
                      ClaudeAgentOptions.hooks. This script empirically
                      confirms that registering hooks={"SessionStart": [...]}
                      either has no effect or is rejected -- see below.
  - UserPromptSubmit -> DOES exist as a registerable event (fires once per
                      user turn, before the model sees the prompt). This is
                      the practical "per-turn boundary" / refresh hook.
  - PreCompact      -> DOES exist as a registerable event, with a `trigger`
                      field ("manual" | "auto"). This script registers a
                      real callback and attempts to fire it by sending the
                      literal text "/compact" as a turn, to see whether the
                      SDK's headless/programmatic mode honors CLI slash
                      commands the way the interactive TUI does.
  - SessionEnd      -> No HookEvent, no HookInput, no HookSpecificOutput
                      class exists anywhere in the package for this name
                      (confirmed by grep across the whole package -- zero
                      hits for "SessionEnd" anywhere). The nearest
                      registerable analog is "Stop" (fires when the agent
                      finishes an assistant turn), which is NOT the same
                      thing as end-of-process/session teardown.

Also demonstrates the practical LORE-snapshot-injection path: since
SessionStart is not hookable, injection happens via
ClaudeAgentOptions.system_prompt (preset + "append"), which is applied once
at session construction -- i.e. *before* the first turn, functionally
equivalent to a SessionStart hook's additionalContext, but static
configuration rather than a dynamic callback.

Run: uv run python spike/02_lifecycle.py
"""

import asyncio
import os

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

events_fired: list[str] = []


async def on_user_prompt_submit(input_data, tool_use_id, context):
    """Registerable, real hook. Fires once per user turn before the model
    sees the prompt. This is the closest thing to a per-turn 'refresh'
    boundary the SDK exposes."""
    prompt = input_data.get("prompt", "")
    events_fired.append(f"UserPromptSubmit(prompt={prompt[:40]!r})")
    print(f"\n[HOOK FIRED] UserPromptSubmit: prompt={prompt[:60]!r}")
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "[lore-tui spike] injected via UserPromptSubmit hook",
        }
    }


async def on_pre_compact(input_data, tool_use_id, context):
    """Registerable, real hook. Fires before context compaction, either
    trigger='manual' (user/API-requested) or trigger='auto' (context-window
    pressure)."""
    trigger = input_data.get("trigger")
    events_fired.append(f"PreCompact(trigger={trigger})")
    print(f"\n[HOOK FIRED] PreCompact: trigger={trigger}")
    return {}


async def on_stop(input_data, tool_use_id, context):
    """Registerable event nearest to 'end of turn'. NOT the same as
    SessionEnd -- fires every time the assistant finishes responding, not
    just at process/session teardown."""
    events_fired.append("Stop")
    print("\n[HOOK FIRED] Stop (nearest analog to end-of-turn, not SessionEnd)")
    return {}


async def on_session_start_attempt(input_data, tool_use_id, context):
    """This callback should be UNREACHABLE. HookEvent has no "SessionStart"
    literal, so this registration is either silently dropped by the SDK's
    serialization layer or rejected. We register it anyway to prove which,
    empirically, rather than trusting the type hints."""
    events_fired.append("SessionStart(UNEXPECTED-FIRED)")
    print("\n[HOOK FIRED] SessionStart -- if you see this, the SDK DOES support it!")
    return {}


async def on_session_end_attempt(input_data, tool_use_id, context):
    """Also should be unreachable by the same reasoning -- and unlike
    SessionStart, grepping the ENTIRE package for "SessionEnd" (any case,
    any file) returns zero hits: no HookInput class, no HookSpecificOutput
    class, no Literal member, nothing. This is a strictly weaker case than
    SessionStart, where at least an output TypedDict existed."""
    events_fired.append("SessionEnd(UNEXPECTED-FIRED)")
    print("\n[HOOK FIRED] SessionEnd -- if you see this, the SDK DOES support it!")
    return {}


async def main() -> None:
    assert "ANTHROPIC_API_KEY" not in os.environ

    options = ClaudeAgentOptions(
        model="claude-haiku-4-5",
        # --- SessionStart-equivalent: static system_prompt append ---
        # This is the ONLY session-start injection point that actually
        # exists: a preset + append is baked into the session before the
        # first turn. It cannot react to anything at runtime (no callback,
        # no access to the session_id, no conditional logic) -- it is pure
        # static config, evaluated once, client-side, before connect().
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": (
                "[LORE SNAPSHOT -- injected at session construction, "
                "emulating SessionStart] Project fact: this is a Phase 0 "
                "validation spike for a LORE-native TUI."
            ),
        },
        hooks={
            "UserPromptSubmit": [HookMatcher(hooks=[on_user_prompt_submit])],
            "PreCompact": [HookMatcher(hooks=[on_pre_compact])],
            "Stop": [HookMatcher(hooks=[on_stop])],
            # Deliberately registering an event name that HookEvent's type
            # union does not include, to see what happens at runtime
            # (Python does not enforce Literal types on dict construction).
            "SessionStart": [HookMatcher(hooks=[on_session_start_attempt])],  # type: ignore[dict-item]
            "SessionEnd": [HookMatcher(hooks=[on_session_end_attempt])],  # type: ignore[dict-item]
        },
        max_turns=6,
    )

    session_start_registration_error: str | None = None

    try:
        async with ClaudeSDKClient(options=options) as client:
            # Turn 1: trivial, cheap prompt. Should trigger UserPromptSubmit.
            await client.query("Say 'ok' and nothing else.")
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            print(f"[assistant] {block.text}")
                elif isinstance(message, ResultMessage):
                    print(f"[result] turn 1 done, is_error={message.is_error}")

            # Turn 2: another trivial prompt. Should trigger UserPromptSubmit
            # again -- proves it's a genuine per-turn boundary, not one-shot.
            await client.query("Say 'ok2' and nothing else.")
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            print(f"[assistant] {block.text}")
                elif isinstance(message, ResultMessage):
                    print(f"[result] turn 2 done, is_error={message.is_error}")

            # Turn 3: attempt to force a manual PreCompact by sending the
            # literal CLI slash command as prompt text. This is a genuine
            # empirical test -- if the SDK's headless streaming-input mode
            # doesn't honor slash commands, we expect either an error, a
            # literal-text response ("/compact" treated as a question), or
            # silence on the PreCompact hook.
            await client.query("/compact")
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            print(f"[assistant] {block.text}")
                elif isinstance(message, SystemMessage):
                    print(f"[system] subtype={message.subtype}")
                elif isinstance(message, ResultMessage):
                    print(f"[result] turn 3 (/compact attempt) done, "
                          f"is_error={message.is_error}, "
                          f"subtype={message.subtype}")
    except Exception as e:  # noqa: BLE001 -- we want to see exactly what breaks
        session_start_registration_error = f"{type(e).__name__}: {e}"
        print(f"\n[EXCEPTION during session] {session_start_registration_error}")

    print("\n" + "=" * 70)
    print("[spike-2 summary]")
    print(f"  Hook events observed firing: {events_fired}")
    print(f"  Exception raised: {session_start_registration_error}")
    user_prompt_submit_count = sum(1 for e in events_fired if e.startswith("UserPromptSubmit"))
    precompact_fired = any(e.startswith("PreCompact") for e in events_fired)
    session_start_fired = any(e.startswith("SessionStart") for e in events_fired)
    session_end_fired = any(e.startswith("SessionEnd") for e in events_fired)
    stop_fired = any(e == "Stop" for e in events_fired)
    print(f"  UserPromptSubmit fired {user_prompt_submit_count}x (expect 2 -- '/compact' does not count as a real prompt turn)")
    print(f"  PreCompact fired: {precompact_fired} (trigger='manual' attempted via '/compact' text)")
    print(f"  Stop fired: {stop_fired}")
    print(f"  SessionStart fired despite not being in HookEvent union: {session_start_fired}")
    print(f"  SessionEnd fired despite not existing anywhere in the package: {session_end_fired}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
