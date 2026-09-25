# Claude sidecar boundary (alpha)

`doxa-claude` starts `rust/doxa-claude/claude_sidecar.py` with an explicit Python
interpreter, no shell. The sidecar reuses `doxa.engine.SessionEngine` and its
Python Claude Agent SDK client. It does not implement a native Rust Claude
Agent SDK. The Python environment must satisfy the repository's `pyproject.toml`.

Frames are newline delimited JSON, maximum 64 KiB including newline. Protocol
hello is `{"type":"hello","protocol":"doxa-claude-sidecar","version":1}`.
Rust sends `{type:"request",id,method,params}` and receives `reply` frames
with matching IDs, plus asynchronous `{type:"event",event,data}` frames.
Available methods: `start` (`cwd`, optional `session_id`, `resume`, `model`),
`prompt` (`text`), `answer` (`id`, `answer`), `interrupt` (empty params), and
`finalize` (empty params). `prompt` acknowledges scheduling; turn completion
arrives as an event. A second prompt while a turn runs is rejected in v1.
`session_id` and `resume` must pass DOXA's session ID check. When both are
provided they must be equal; a lone `resume` also becomes the session ID.

The Rust client caps input and output frames, bounds the receive queue, applies
startup and read timeouts, a 15-second maximum write deadline, and kills the
sidecar's process group on write timeout or drop, including SDK CLI children
that remain in that group. A `Bridge::recv` timeout is an idle poll result and
leaves the process running. The sidecar deliberately
does not print SDK exception text. Neither side logs prompts or secrets.
`Bridge::recv` returns events and replies in arrival order. `doxa-daemon`
owns the bridge through a broker thread with `--engine claude`. It forwards
turn events over protocol v1, correlates replies, and accepts
`answer_needs_input` and `interrupt` during a turn. A bounded event queue
ends the turn if a consumer stalls. Out-of-band peer events are not yet
published while no turn is active.

## Parity still required before replacing the Python engine

- Map every Claude `EngineEvent` to the Rust TUI's full interactive state.
- Support concurrent prompt queueing, out-of-band peer events and reconnect.
- Validate interactive `AskUserQuestion` and permission answers against a live
  Claude SDK session, including denial and cancellation.
- Validate resume identity, LORE hooks, MCP operators, plugin isolation, and
  finalization in a live integration environment.
- Decide how the host handles event frames beyond 64 KiB. This version emits
  `frame_too_large` rather than silently truncating an event.

The installed `claude` CLI 2.1.281 exposes `--input-format stream-json`,
`--output-format stream-json`, `--permission-prompts host`, and `--resume`.
Those flags do not document a stable Rust host control protocol for the SDK
callbacks DOXA uses. Anthropic's [SDK overview](https://platform.claude.com/docs/en/cli-sdks-libraries/overview)
lists official client SDKs for Python, TypeScript, C#, Go, Java, PHP and Ruby.
The [Agent SDK documentation](https://platform.claude.com/docs/en/agent-sdk/overview)
describes the supported Agent SDK surfaces; no native Rust SDK is claimed.
