# Swappable engines — a session driven by something other than Claude Code

Status: **draft for review**. Nothing implemented. Written before the work
because the seam already exists informally, and naming it wrongly is more
expensive than not naming it.

## What provoked it

Requested 2026-09-04: *"Now we have hardwired Claude Code SDK in DOXA, but
i would like to have a swappable model provider per session e.g. also add
codex or include opencode sessions."*

## What is already true, measured

`docs/plans/plugin-api.md` assessed this in v0.34.0 and recorded the
finding in `providers.py`'s own docstring: the Protocol is the right shape
for **half** of it.

- **The catalog half is done.** `ModelProvider.list_models()` — the picker
  asks a provider what it offers and never branches on who the provider
  is.
- **The session half has no name.** Spawn, send, interrupt and the event
  stream are what `SessionEngine` and `EngineClient` agree on
  *informally*.

But they do agree, and that is the whole opportunity. Both already
implement the same surface — `start`, `send`, `set_model`,
`context_usage`, `finalize`, `stop`, `send_peer_message` — and both
already yield **`EngineEvent`**, DOXA's own type, not the SDK's. The TUI
has never seen an SDK object.

**The coupling is one module deep.** 79 `claude_agent_sdk` references
across ten files, but **55 are in `engine.py`** and the rest of
`operators.py`'s are `SdkMcpTool` definitions. The other eight files
mention it only in comments and docstrings about import cost. There is no
SDK type in the widget layer, the layout, the rail, the peer registry or
the tabset record.

## The shape

An **`Engine` Protocol** naming what `SessionEngine` and `EngineClient`
already do, and a registry mapping an engine id to a factory — the same
move `ModelProvider` made for the catalog half.

```
EngineProvider          engine_id() -> "claude" | "codex" | "opencode"
                        supports() -> what this engine can actually do
                        new_session(cwd, model, …) -> Engine
Engine                  start / send / set_model / context_usage
                        finalize / stop / send_peer_message
                        every method yields or returns EngineEvent
```

`EngineEvent` is already the boundary type and does not change. **If a new
engine needs a new event kind, that is a finding worth surfacing**, not a
field to add quietly — the renderer map (`EVENT_RENDERERS`) is what the
TUI is written against.

## The part that will actually hurt

Not the Protocol. Four things underneath it:

1. **Capability is not uniform, and pretending otherwise is the trap.**
   Claude Code has MCP tools, hooks, permission modes, a `--plugin-dir`,
   `SystemMessage` init carrying the resolved model, and
   `get_context_usage`. Codex and opencode have some, none, or different
   ones. **`supports()` must be honest and every caller must handle a
   `False`.** DOXA already has the discipline: `/context` says `?` for an
   unreported limit rather than guessing, and the ctx chip hides rather
   than showing zero. A capability map that lies is worse than no second
   engine.
2. **The operator surface is Claude-shaped.** `operators.py` builds
   `SdkMcpTool`s and `to_sdk_tools` projects them through
   `create_sdk_mcp_server`. An engine without MCP cannot receive DOXA's
   LORE tools at all. Decide: does that engine simply lose them (honest,
   and the session says so), or does DOXA grow a second projection? **Do
   not answer this in the Protocol** — answer it per engine, in the
   engine, and let `supports()` report it.
3. **`spawn_session` (v1.3.0) inherits every question.** A spawned child
   currently gets DOXA's own daemon. Does a parent on one engine spawn a
   child on another? The caps (depth 2, 3 live, 1/60s) are engine-agnostic
   and stay so; the argv threading (`--spawn-depth`, `--task`) is DOXA's,
   not the SDK's, so it survives. Say explicitly whether cross-engine
   spawn is in scope. **Recommendation: out. One question at a time.**
4. **`peer.engine` already exists and is already advisory.** v1.0.2 ships
   `provider`/`model`/`engine` on `PeerInfo` as a free string with
   `_self_desc` bounding it, and the rule is that a peer's
   self-description is **never verified**. A real second engine makes that
   field mean something for the first time — and the rule does not
   change: it is still displayed, never believed, and never decides which
   peer gets work.

## Scope

**In:** the `Engine`/`EngineProvider` Protocols named against what already
works; a registry and a per-session choice; an honest `supports()`; ONE
second engine end to end, chosen for whichever has the cleanest
programmatic surface.

**Out:** cross-engine spawn; a second MCP projection; engine-specific
event kinds; changing `EngineEvent`; making `peer.engine` trusted.

## The check this spec owes

Every spec since v0.91.0 has owed itself one, and each found something.
**Can `EngineClient` — the daemon-socket side, which has never imported
the SDK — satisfy the `Engine` Protocol unchanged?** It is the natural
control: it already implements the surface and it is already
engine-agnostic by construction. If the Protocol needs `EngineClient` to
change, the Protocol has been written against `SessionEngine`'s
implementation rather than against the seam, and it is wrong.
