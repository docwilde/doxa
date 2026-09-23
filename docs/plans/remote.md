# Remote control and a web client — specification

Status: **partly implemented**. The policy gate, cross-machine peer bridge,
and an initial browser renderer for local daemon sessions are in the code.
The richer renderer and the remaining design work below have not shipped.
This plan was written after looking at a colleague's `telag`, which solves
the same user problem from the other end.

## The problem

One session, reachable from more than one place: start an agent at the desk,
pick it up on a phone, sit down and carry on in the terminal. The session must
be the *same* session — not a copy, not a mirror.

## Implemented slice: the browser bridge

`doxa-remote` is a separate, optional process. It binds only
`127.0.0.1:47601`, reads the local daemon registry and Unix sockets, and
serves HTML and a WebSocket through Tailscale Serve. The daemon protocol and
the session's worktree stay local. The first browser client lists running
daemon sessions, loads each transcript, follows live turn events, sends
prompts, and answers pending input. It is an initial renderer: it does not
yet reproduce the Textual interface's richer turn, diff, image and belief
views or push notifications. An `◎ remote:<login>` chip names an attached
browser driver in the local status bar and disappears when it disconnects.

From a checkout, opt in explicitly in `~/.doxa/config.toml`:

```toml
remote_enabled = true
remote_allowed_logins = "you@example.com"
```

Replace that value with the Tailscale login allowed to drive the session;
multiple logins are comma-separated. Then, with a daemon-backed DOXA session
running, start the optional web dependencies, bridge, and private Serve proxy:

```sh
uv run --extra remote doxa-remote
tailscale serve --bg 47601
tailscale serve status
```

`doxa-remote` refuses to start while remote access is off or the allow-list
is empty. It checks the `Tailscale-User-Login` header only on its loopback
listener, applies the allow-list to each action, and rejects cross-origin
browser WebSocket connections. Use Tailscale **Serve**, not public Funnel:
[Serve supplies identity headers for tailnet traffic; Funnel does not](https://tailscale.com/docs/features/tailscale-serve#identity-headers).
The browser currently exposes transcript/status reads, prompts, and pending
input answers. Shell escapes and permission-mode changes are not browser
operations. `tailscale serve off` disables sharing; stopping the bridge
process closes the browser endpoint. A detached daemon still has its normal
`--linger` timeout when no client is attached.

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

1. **Transport** — the daemon remains on `AF_UNIX`, `chmod 0600`. The separate
   bridge serves a WebSocket on loopback and Tailscale Serve provides the
   private HTTPS endpoint.
2. **Authorization** — the local socket still uses file permissions. The
   bridge additionally requires Serve's login header on loopback and DOXA's
   own `remote_allowed_logins` list.
3. **Renderer** — Textual remains the full local renderer. The initial
   browser client renders transcript text and live events over the same
   daemon session; the richer web view proposed below remains work to do.

Only (3) is new product surface. (1) is plumbing. (2) is the part that can
cause real harm, so it is specified first.

## Authorization: the part to get right

**What an exposed daemon actually grants.** DOXA's agent edits files and runs
tools as the invoking user; the local TUI has `!` for shell commands and
permission modes including `bypassPermissions`. So a reachable daemon socket
is **remote code execution with the user's privileges**,
and no amount of UI care compensates for getting this wrong.

**Adopt telag's identity model rather than inventing one.** The implemented
bridge uses Tailscale Serve's private HTTPS endpoint, tailnet identity, and
an explicit per-user DOXA allow-list. It does not create a bearer token.
Non-negotiables:

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
long tables, a belief store with 600 rows, a diff — become easy. This is the
design worth having. An initial version now renders transcript text and live
events. The richer views in this paragraph and cursor replay through the
bridge remain proposed work.

Recommendation: **(b)**. The initial browser bridge follows this path;
continuing toward a full renderer remains the work in this plan. Streaming
Textual as in (a) would be a separate stopgap with different limits.

## What this is not

- **"Teleport a session to a cloud agent" is not this feature.** A DOXA
  session's substance is local: the daemon, git worktree, files, and tools
  that run with your privileges, whether the selected engine is Claude,
  Codex, DeepSeek or GLM. Moving tool execution to a cloud sandbox would be
  a different product with a different filesystem and security model. Here,
  execution stays on your machine; the **view and controls** reach another
  device.
- **Not a replacement for telag.** If someone wants a phone view of the Claude
  Code CLI itself, telag already does that and DOXA is not competing.
- **Not multi-user.** One user, several devices. Two humans driving one agent
  concurrently raises interleaving questions this spec does not answer.

## Open questions

1. **Transport choice: answered for the first client.** A separate bridge
   keeps the daemon's Unix socket local and leaves remote access off until
   explicitly enabled.
2. **Remote writes: initially a reduced set.** The browser can send prompts
   and answer pending input; shell escapes and permission-mode changes have
   no browser operation. Further write controls remain to be designed.
3. **Authentication: answered for the first client.** Tailscale Serve inserts
   a login header for tailnet traffic, DOXA matches it against its own
   allow-list on a loopback-only bridge, and the bridge reaches the daemon
   under the local user's Unix-socket permissions.
4. **Push notifications: open.** DOXA already has desktop notifications with a focus
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
- when cursor replay is added to the browser bridge, replay-from-cursor
  reproduces the same transcript the local client gets, byte for byte; the
  initial browser client instead loads the on-disk transcript and then
  subscribes to live events
