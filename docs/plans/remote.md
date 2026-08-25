# Remote control and a web client — specification

Status: **draft for review**. Nothing implemented. Written after looking at a
colleague's `telag`, which solves the same user problem from the other end, so
that DOXA's answer is a decision rather than an imitation.

## The problem

One session, reachable from more than one place: start an agent at the desk,
pick it up on a phone, sit down and carry on in the terminal. The session must
be the *same* session — not a copy, not a mirror.

## Prior art: telag, and why its architecture is not ours

`telag` runs Claude Code inside tmux/zellij and streams the multiplexer to a
mobile PWA over a private Tailscale node. The multiplexer is the source of
truth; telag is the bridge. It is a good design for what it wraps: the Claude
Code CLI is a TUI, so the only thing a remote client can be handed is
**terminal bytes**, and any client must therefore emulate a terminal — hence an
xterm-style surface with an on-screen key row for TUI navigation.

DOXA is not in that position, and this is the whole reason the design differs.

**DOXA already solved detach/reattach one layer up.** The daemon speaks
structured, sequenced events over line-JSON on a Unix socket, not bytes:

- every published event gets a monotonically increasing `seq` and lands in a
  bounded ring; a client attaches with `{"type": "attach", "cursor": N|null}`
  and gets `seq >= cursor` replayed, then the live tail on the same connection
- `EngineClient(cursor=…, skip_backlog=…)` picks up mid-stream using the
  `next_seq` the hello frame already carries
- frames are capped at `peers.MAX_FRAME_BYTES` (64 KB) and oversize replies are
  paged (`_fit_page`, shared by the `beliefs` and `pending` RPCs)
- the full transcript is on disk per session (`doxa/transcript.py`), which is
  how v0.32.0 rebuilds a restored tab's content without the daemon

So the terminal is **one renderer of the session**, not the session. A remote
client does not have to emulate anything: it can consume the same event stream
the Textual UI consumes and draw turn blocks, tool chips, reasoning folds and
images natively. tmux can never offer that, because tmux only has bytes.

That is the one real advantage DOXA has here, and the design should spend it.

## Three separable layers

Keeping these apart is what stops this becoming a rewrite:

1. **Transport** — today `AF_UNIX`, `chmod 0600`, same machine only. A remote
   client needs a network transport (WebSocket over TLS is the obvious shape,
   since the framing is already one JSON object per message).
2. **Authorization** — today the socket's file mode *is* the security model:
   same uid, nothing else. It does not survive leaving the machine.
3. **Renderer** — today Textual widgets. A web client is a second renderer over
   the same events.

Only (3) is new product surface. (1) is plumbing. (2) is the part that can
cause real harm, so it is specified first.

## Authorization: the part to get right

**What an exposed daemon actually grants.** DOXA's agent edits files and runs
tools as the invoking user; v0.36.0 added `!`, which runs arbitrary shell
commands; and permission modes including `bypassPermissions` are specified for
v0.42.0, unshipped at the time of writing but assumed here because a security
boundary has to be designed against what is coming, not what has landed. So a
reachable daemon socket is **remote code execution with the user's privileges**,
and no amount of UI care compensates for getting this wrong.

**Adopt telag's model rather than inventing one.** A private network where the
identity question is already answered — a Tailscale node with an in-process
TLS cert, tailnet ACLs, and an explicit per-user allow-list — is stronger than
any bearer token DOXA would grow, and it is proven in the colleague's
deployment. Non-negotiables:

- **Loopback stays the default.** Remote listening is opt-in, per invocation or
  per config, never on by default, and never silently enabled by installing
  something.
- **No new credential store.** If DOXA finds itself writing a password or token
  file, the design took a wrong turn.
- **The allow-list is DOXA's own**, not merely the network's — defence in depth,
  and the same shape as `TELAG_ALLOWED_TS_USERS`.
- **The remote surface is not larger than the local one.** In particular
  `!` shell and `bypassPermissions` deserve an explicit decision: a mode that
  stops asking, driven from a phone that might be unlocked on a table, is a
  different risk than the same mode at a keyboard. Defaulting the remote
  surface to *refuse* those two, with an explicit opt-in, is the conservative
  reading and probably the right one.
- **Say who is connected.** A session driven from elsewhere should show that in
  the status bar, the way the worktree and branch are shown. A silent second
  driver is the thing a user cannot detect and cannot consent to.

## Two candidate renderers

**(a) Stream the Textual app.** `textual-serve` / `textual-web` exist (neither
is currently a dependency — measured) and would put the running TUI in a
browser in days rather than months. It is the cheap path, and it is telag's
model again: a terminal, mirrored. Fine as a stopgap; it inherits every
constraint of a TUI on a phone, and it spends none of the advantage above.

**(b) A web client over the event stream.** The daemon's protocol is already
the API: attach with a cursor, replay, follow the tail. A browser client
renders turns as HTML, and things that are awkward in a terminal — images,
long tables, a beliefs browser with 600 rows, a diff — become easy. This is the
design worth having, and it is roughly the same amount of work as the beliefs
browser was, plus transport.

Recommendation: **(b)**, with (a) explicitly available as a stopgap if a
mobile view is wanted before the web client exists. Do not ship (a) and call
it the answer; the two are not the same product.

## What this is not

- **"Teleport a session to the Anthropic cloud" is not achievable**, and the
  reason is worth writing down so it stops being re-asked. A DOXA session's
  substance is local: the git worktree, the files, the tools that run with your
  privileges, a local `claude` CLI driven by the Agent SDK. Moving the session
  to a cloud sandbox is not a transport change — it is a different product in
  which the *tools* execute somewhere else, with a different threat model and a
  different filesystem. What IS achievable is what telag does: execution stays
  on your machine, only the **view** moves.
- **Not a replacement for telag.** If someone wants a phone view of the Claude
  Code CLI itself, telag already does that and DOXA is not competing.
- **Not multi-user.** One user, several devices. Two humans driving one agent
  concurrently raises interleaving questions this spec does not answer.

## Open questions

1. **Does the daemon serve the network directly, or does a separate process
   bridge to it?** A bridge keeps the daemon's attack surface exactly as it is
   today and makes "remote off" the trivial default; it costs a hop and another
   moving part.
2. **Write access from remote: all of it, or a reduced set?** Reading a
   transcript, seeing status and approving a tool call are three very different
   risks, and they need not be granted together.
3. **How does the web client authenticate to the *daemon* once past the network
   boundary?** The Unix socket's file mode does not translate, and the answer
   must not be a token DOXA invents.
4. **Push notifications.** DOXA already has desktop notifications with a focus
   rule (`notify_if`); Web Push is a different delivery path for the same
   events. Reuse the trigger set rather than growing a second one.

## Testing bar

The house rule applies with more force here, because a security boundary that
tests green and does not hold is worse than none:

- a remote listener is **absent** unless explicitly enabled — assert on the
  default, since a default is what almost everyone runs
- a request from a non-allow-listed identity is refused, and the refusal is
  visible rather than silent
- the reduced remote surface actually refuses what it claims to refuse (write
  the `!`-shell and `bypassPermissions` cases as security assertions, the way
  v0.36.0's "the model cannot reach the shell" test is written — including its
  lesson that such a test passes *vacuously* until the capability exists, so it
  must be verified against a deliberately unsafe build)
- replay-from-cursor over the network reproduces the same transcript the local
  client gets, byte for byte
