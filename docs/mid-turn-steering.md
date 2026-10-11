# Mid-turn steering feasibility

**Reviewed 2026-10-11.** This is a read-only feasibility investigation of the
pinned Codex app-server source and the locally installed Claude Code CLI. No
provider session was launched. “Steer” here means submitting new user content
to an existing turn; the ordinary DOXA prompt queue is a separate operation.

## Result

| Engine | Provider behavior | What DOXA can promise |
| --- | --- | --- |
| Codex app-server | `turn/steer` accepts user input for an exact active regular turn and returns the turn ID on acceptance. The input enters that turn's ordered pending-input queue. | A future DOXA control can report same-turn submission only after the app-server response succeeds. A stale turn must be rejected. |
| Claude Code CLI | The installed CLI accepts streamed user frames into its prompt queue. Its query loop can fold eligible queued prompts into a running turn at a later model-loop boundary. This is not a turn-scoped steer RPC and does not provide the same turn-ID precondition. | DOXA can report that a user frame was submitted to the CLI input queue. It cannot promise exactly when, or whether at every phase, that frame will be folded into the running turn. |

DOXA currently exposes neither operation as mid-turn steering. The Codex host
has no `steer` call and the Claude host has no such call; current concurrent
prompt admission is not a substitute. The implementations also have different
acknowledgment semantics and should not be presented as equivalent.

## Codex app-server evidence

The pinned official checkout is
`/home/docwilde/ssd-cache/src/codex-0.156.1`, commit
`b412ff32c417f855c2b2d1581b77058eed87c84b`.

- `codex-rs/app-server-protocol/src/protocol/v2/turn.rs:293-315` in the pinned
  checkout defines `TurnSteerParams` with `threadId`, optional
  `clientUserMessageId`, `input`, and required
  `expectedTurnId`. `TurnSteerResponse` returns `turnId` at lines 317–322.
- The method is registered as `turn/steer` in
  `codex-rs/app-server-protocol/src/protocol/common.rs:1052-1056`.
- `codex-rs/app-server/src/request_processors/turn_processor.rs:1020-1158`
  checks direct-input permission, the thread's model provider, a nonempty
  expected ID and the input limit. It submits user input to `steer_turn` and
  returns an error for no active turn, an expected-ID mismatch, an empty
  input, or a non-steerable review or compact turn.
- `codex-rs/core/src/session/turn_input.rs:624-706` holds the active-turn lock,
  checks the expected ID against the current sub-turn ID, accepts only regular
  tasks, reserves user-input order, and adds the input to that active turn's
  pending queue. This is a same-turn submission, not a second turn.
- The native TUI's typed call at
  `codex-rs/tui/src/app_server_session.rs:1422-1443` supplies the current
  thread ID, turn ID, client message ID, and input. Its race handling at
  `codex-rs/tui/src/app.rs:781-804` recognizes missing/stale active turns.

See the [official app-server steer documentation](https://learn.chatgpt.com/docs/app-server#steer-an-active-turn).

## Claude Code CLI evidence

The inspected executable is
`/home/docwilde/.local/share/claude/versions/2.1.296`. Its embedded package
metadata identifies `@anthropic-ai/claude-code` version `2.1.296`, build
`2026-10-09T01:31:52Z`, Git SHA
`fdb6c17a97b6e0e3cf15e3566663fca58e7c43a9`. The executable SHA-256 is
`24972e3bc859fab2b46ed4c1e51f7d6130f06d3bd550811a114640de3370d0de`.
Because this is a self-contained binary with embedded JavaScript, the offsets
below refer to that exact file and are not source line numbers.

- Embedded CLI help at byte offset `225722827` describes
  `--input-format stream-json` as “realtime streaming input”. It also exposes
  `--replay-user-messages`, which re-emits stdin user messages on stdout for
  acknowledgment when both input and output are stream-json.
- The stream reader and JSONL parsing are embedded around offsets
  `225416817-225419777` and `232462505-232476000`. The `StructuredInput`
  reader yields parsed `type:"user"` records; `processLine()` validates the
  frame and returns it to the input loop.
- The session input loop around offsets `233755600-233762300` processes a new
  user frame into a prompt command and passes it through
  `acceptUserFrame`/queue admission and `press` handling. This is not just a
  startup prompt reader.
- The query loop around offset `224278000` examines eligible queued commands
  at model-loop iterations. It converts those commands to
  `queued_command` attachments and consumes them using
  `messageQueue.consume(..., {reason:"absorbed_mid_turn"})`, unless a
  suspension or other eligibility condition prevents absorption.

These paths establish that the installed CLI has an input queue and a
same-turn fold opportunity. They do not establish that every frame is folded
immediately, at every phase, or before a particular tool runs. There is no
Claude protocol equivalent here to Codex's required `expectedTurnId` and
turn-scoped acceptance response.

The recorded test in `docs/manual.md` used `claude-agent-sdk` 0.2.144 and
Claude Code 2.1.273, and observed a second stdin frame being dropped in that
specific run. It was not a test of the installed 2.1.296 executable and does
not prove that current stream-json input lacks a queue. The static inspection
above did not launch the CLI or verify live timing. DOXA currently starts
Claude with stream-json input and output in
`rust/doxa-claude/src/cli.rs:50-75`, and `Cli::prompt()` writes an ordinary
user frame at lines 229-231. It does not enable `--replay-user-messages`.

## DOXA ownership and implementation seam

### Codex

[`CodexHost`](../rust/doxa-daemon/src/codex_host.rs#L47-L80) owns the driver
behind a mutex. During a turn, it holds that driver while running
`AppServerDriver::run_turn_controlled` (`codex_host.rs:697-760`). The driver
validates and stores the provider `turnId` only after `turn/start` responds
(`rust/doxa-engines/src/codex_appserver.rs:562-570`) and owns the app-server
stdin/stdout and request loop (`codex_appserver.rs:133-167, 572-589`). A
separate RPC thread must not write to the provider pipes.

The existing live permission control is a narrow pattern to follow:
`CodexHost` creates a one-slot channel at `codex_host.rs:586-589`, and
`set_permission_mode` uses `try_send` plus a bounded acknowledgment wait at
lines 1004-1022. Add a typed steering control to the same turn-owned event
loop. It should issue `turn/steer` with the private current thread and provider
turn IDs, match the JSON-RPC response, and only report accepted after the
provider response succeeds. Do not try to acquire the driver mutex from the
RPC call or write to stdin concurrently.

The provider ID is available after the host currently emits generic
`turn_started` (`codex_host.rs:607`, before `turn/start` returns). Use a
host-owned active-turn generation for client calls and map that generation to
the provider ID inside the host. Require callers to include the generation;
reject a stale generation instead of redirecting input to a newer turn.

### Claude

The `broker` thread owns the `Cli`, and it drains host commands while polling
provider frames (`rust/doxa-daemon/src/claude_host.rs:1147-1175`). It already
serializes `Cli::prompt()` writes from that thread when a `Command::Prompt`
arrives (`claude_host.rs:1185-1207`). That broker is the only safe place to
write another user frame during a run. `ClaudeHost::call()` currently exposes
settings, interrupt, model/context, and approval calls, but no steering call
(`claude_host.rs:634-681`). A typed bounded broker command can write an
ordinary UUID-tagged user frame through `Cli::prompt()` after host admission.

The default implementation should report “submitted to CLI input queue” and
avoid claiming that the running model has already consumed it. If using
`--replay-user-messages` to confirm receipt, add UUID correlation and explicitly
handle the echoed `isReplay:true` user frame: the current broker stages stdout
`user` and `assistant` messages together (`claude_host.rs:1703-1720`), so an
echo must not be persisted or surfaced a second time as a new user message.

## Shared boundaries and regression plan

Use a bounded one-in-flight control, an opaque DOXA active-turn generation,
and a length-bounded nonempty message. Redact and durably persist the user
message before sending it to either provider. Never retry an ambiguous write
or provider timeout: that could duplicate a message. Never allow an old
generation to submit to a later turn.

Keep steering separate from `needs_input` and `answer_needs_input`: a message
must not approve a tool or answer a question. For the first implementation,
disable steering while an approval or human question is unresolved. Also
disable during compaction/review, after cancellation, while closing, and after
scrub or persistence failure. Codex's provider explicitly rejects review and
compact turns; DOXA compaction has a separate LORE review gate. Do not turn a
rejected steer into a fresh turn without asking the user.

Focused regression coverage should verify:

1. Codex sends to the current provider turn, waits for a matching response,
   rejects stale generation and provider `expectedTurnId` mismatch, and never
   sends during review or compact.
2. Claude writes the second UUID-tagged user frame only through its broker,
   reports queue submission distinctly from fold completion, and safely
   correlates an optional replay acknowledgment without duplicate transcript
   rows.
3. Both adapters reject empty/oversized input, second in-flight submission,
   unresolved approval, completion/cancel races, stale generation, and
   closing or persistence-failed sessions.

Verification here was static source inspection only. It included no provider
session, device, build, or live service test.
