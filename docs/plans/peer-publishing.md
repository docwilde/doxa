# Peer publishing — what a session says about itself

Status: **draft for review**. Nothing implemented. `doxa/peers.py` and
`PeerInfo` are exactly as they are today; this spec proposes fields, not code.

## What exists today

`PeerInfo` (`doxa/peers.py`) carries where a session *is*: `session_id`,
`pid`, `socket_path`, `cwd`, `repo_root`, `title`, `started_at`,
`heartbeat_at`, `daemon_socket`, `clients`. Nothing about what it *is*.
`scope_key` (`repo_root or cwd`) is how two sessions decide they are peers;
`list_peers` filters `read_registry()` to that key.

The transport underneath is deliberately model-agnostic: one JSON file per
session in a `0700` runtime directory, one AF_UNIX socket per session for
line-JSON frames capped at `MAX_FRAME_BYTES` (64 KiB). Nothing in the wire
format assumes Claude, or Anthropic, or even DOXA on the other end — a
session driven by a different `ModelProvider` (`doxa/providers.py`) is
already a valid peer as far as the pipe is concerned. Publishing *what*
that session is, without breaking anything that reads today's format, is
the whole of this spec.

## The idea

Three new optional fields on `PeerInfo`, all defaulting to `None`:

```python
provider: str | None = None
"""Short provider id, the same vocabulary doxa.ui.labels.PROVIDER_GLYPHS
keys on ("claude" today — see that table's one row). Set once at connect
from whichever ModelProvider built this session. None means an older
build, or a writer that predates this field: unknown, never "claude"."""

model: str | None = None
"""The model id or alias currently in force -- the exact string
SessionEngine.model holds and /model accepts (an alias like "sonnet", or
a resolved id like "claude-sonnet-4-5"). MUTABLE: set_model() changes it
mid-session with no reconnect, so the registry entry must be rewritten
at the moment of the switch, not at the next heartbeat -- the same
discipline PeerHost.set_client_count() already applies to the attach
count ("presence has to move when the answer changes, not on the next
heartbeat, or a just-detached session reads as attached for another
beat"). None means unknown, never "default"."""

engine: str | None = None
"""Which SessionEngine implementation hosts this session. Exactly one
value exists today (DOXA's own, wrapping the claude_agent_sdk-driven CLI
subprocess); the field exists so a second engine (DeepSeek, Codex — the
multi-provider engines providers.py's own docstring gestures at)
identifies itself distinctly from `provider`, and so a non-DOXA process
writing this same schema can name itself without a DOXA release. None
means unknown."""
```

`provider` reuses `doxa.ui.labels.PROVIDER_GLYPHS`' vocabulary
(`{"claude": "✳"}` today, doxa/ui/labels.py:91) rather than inventing a
second one — a peers chip that wants to show a provider glyph next to a
peer's title calls `provider_glyph(peer.provider)` directly. `model`
reuses whatever string `/model` and the tab label already carry
(`doxa/ui/labels.py`'s `short_model()` extracts the tier word from exactly
this string today). No new vocabulary, two reused ones.

## Identity ages differently than capability

`provider`, `model` and `engine` are cheap, stable, and known locally at
connect time — they come out of `SessionEngine._build_options()`
(`doxa/engine.py:1712`) and the `ModelProvider` that built the session, not
out of a network call. That is what makes them safe to publish.

A `context_window` field is not in that category, and does not belong in
`PeerInfo`. Three separate, independently measured facts rule it out:

1. **The one live source is unreachable under DOXA's normal auth.**
   `doxa/providers.py`'s own docstring records the empirical finding: the
   Anthropic Models API tier fails at client construction
   (`TypeError: Could not resolve authentication method…`) under DOXA's
   documented OAuth-only posture, and is only even attempted when the
   operator's shell happens to export `ANTHROPIC_API_KEY`. For the
   overwhelming majority of sessions, nothing in this codebase has ever
   measured a context window.
2. **`/context` already encodes the honest answer, and it is "unknown."**
   `ctx_absolute_text` (`doxa/ui/labels.py:474`) prints `?` for an unknown
   limit rather than substituting a plausible number, precisely because
   "DOXA drives several models with very different windows… there is no
   second source to fall back on." A registry field that claims to know
   what the chip built for the same session refuses to claim would
   contradict DOXA's own status bar.
3. **`set_model` has no catalog behind it.** `ModelProvider.list_models()`'s
   fallback tier is four bare aliases (`haiku`, `sonnet`, `opus`, `fable`)
   with no size attached, and `set_model` accepts *any* string. A session
   whose `model` field reads `"fable"` has told a peer nothing a lookup
   table could resolve today.

The conclusion is not "add the field and accept a lot of `None`." It is
that context window is a property of a **model**, not of a **session** —
one row in the catalog `docs/plans/model-registry.md` proposes, keyed by
the same `model` string this spec publishes. Storing a copy of it on every
`PeerInfo` would be a second, driftable copy of catalog data the instant
the catalog disagrees with a session's stale entry. `PeerInfo` publishes
*that* a session is running `"sonnet"`; a reader wanting to know what
`"sonnet"` can do looks it up once, in one place. See "One vocabulary, two
directions" below.

The same reasoning excludes live numeric telemetry — context-used
percentage, cost so far, tokens burned. Those change every turn, are
already reported locally by `/usage` and `/context`, and would turn a
15-second heartbeat write into either stale telemetry or a much heavier
write path for no decision a peer actually needs to make from another
process. `PeerInfo` is presence plus identity; it is not a second `/usage`.

## Schema evolution on a file other builds read

`read_registry` already has the shape this needs, and it is easy to break
by accident. The construction is:

```python
info = PeerInfo(**{k: data[k] for k in _ENTRY_FIELDS})   # peers.py:273
...
ds = data.get("daemon_socket")                            # peers.py:275
info.daemon_socket = str(ds) if ds else None
clients = data.get("clients")                              # peers.py:277-278
info.clients = int(clients) if isinstance(clients, (int, float)) else None
```

Two properties fall out of this that must be preserved, not re-derived:

- **A reader ignores keys it does not know about, by construction, not by
  a check.** `{k: data[k] for k in _ENTRY_FIELDS}` names the keys it wants
  and never unpacks the whole JSON object. An entry written by a *newer*
  build carrying a field this reader has never heard of is already
  harmless today — the dict comprehension never looks at it. `provider`,
  `model` and `engine` must be read the same way the daemon_socket/clients
  pair already is: an individual `.get()`, defensive coercion, default
  `None`. They must never be added to `_ENTRY_FIELDS`, and `PeerInfo` must
  never be constructed via `PeerInfo(**data)` — either change would turn a
  missing key on an *older* entry into the `KeyError`/`TypeError` that
  `read_registry`'s `except` clause (peers.py:279) currently reaps the
  whole entry for, which is exactly the "one upgraded session makes itself
  invisible to every other" failure this spec has to avoid, just aimed the
  other direction (an old entry missing a new key, rather than a new entry
  carrying an unknown one).
- **The dataclass needs a default for every new field.** `PeerInfo(**{...})`
  only ever passes the required tuple; `provider: str | None = None` (etc.)
  is what lets construction succeed when an entry — old, or written by a
  third-party engine that has not adopted this field yet — omits the key
  entirely.

Net: three lines added to the `.get()` block below `clients`, three new
dataclass fields with `None` defaults, nothing added to `_ENTRY_FIELDS`.
That is the entire compatibility mechanism; it already exists for
`daemon_socket` and `clients`, and this spec's job is to not regress it.

## A peer's self-description is untrusted

`title` already crosses `PEER_UNTRUSTED_INTRO` — it appears as `from_title`
inside the block `frame_for_model` renders (peers.py:642-646), which is
wrapped end to end in the untrusted-peer marker. `model` is written by the
same untrusted party (another process, same user, possibly a future
non-DOXA engine) and is, if anything, a **more persuasive** lie than a
title: *"I am running opus"* reads as a capability claim a human or an
orchestrator might act on, where a fabricated title mostly just misleads a
label.

Two things follow, and neither is optional:

- **`provider`, `model` and `engine` may be displayed and may be logged.
  They may never be treated as verified.** No future surface may use a
  peer's self-reported `model` to make a privileged decision — which peer
  gets sent a task, which peer's output is trusted more, whether to relax
  a check — without a human in the loop, for the same reason `/msg` has no
  model-callable equivalent today ("the model has no send tool — every
  peer message crosses because a human typed `/msg`," `docs/manual.md`).
  A future orchestrator reading these fields inherits that rule; it does
  not get to relax it because the string looks like a model id.
- **If any of these fields ever reaches the model** (not true today — see
  below), it must cross the identical `PEER_UNTRUSTED_INTRO` framing
  `frame_for_model` already applies to message bodies. There is no "this
  field is more structured, so it's safer" exception; a structured lie is
  still a lie.

**Verified, and worth fixing when this ships, not deferred as a footgun for
the new fields to inherit:** `title` reaches `/peers`' human-facing output
(`doxa/session/commands.py:1032`, `f"{p.title}  {p.session_id[:8]}
{p.cwd}"`) with **no `scrub_secrets` call on that path** — unlike the
message-frame path, where every string field is scrubbed at the one
receive point before display (`peers.py`'s `_handle_conn`, lines 616-625).
`_system()` (`doxa/session/pane.py:743`) only mounts a TUI widget; it never
reaches the model, so this is a terminal-rendering/spoofing risk (a peer
setting its title to an escape sequence or a fabricated status line), not
a prompt-injection one. `provider`, `model` and `engine` would land on the
exact same unscrubbed path if they render the same way. Recommendation:
registry entries get the same scrub pass message frames already get, at
read time (`read_registry`), not left to whichever display site remembers.

**What these fields are for, stated affirmatively:** advisory display. A
human reading `/peers` or a future roster chip sees "this peer says it is
running `sonnet` via `claude`" — useful context for deciding whether to
`/msg` it, exactly as `title` is useful today for the same reason. Nothing
more is claimed.

## Cross-project is out of scope, and here is the boundary

`scope_key` is `repo_root or cwd` (peers.py:217-219), computed from
`main_repo_root_of` so that every worktree of one repo lands on the same
key (peers.py:153-186). `list_peers` filters `read_registry()` to sessions
sharing that key; `read_registry()` itself does **not** filter by scope —
it returns every live entry in the runtime directory, across every repo
the user has open. The one function that already reads across scopes is
`list_daemons(scope_key=None)`, and only for the attach picker: a human
reattaching to *their own* session, not automated cross-repo reasoning.

Publishing `provider`/`model`/`engine` does not change any of that. A
future orchestrator that wants to reason about a user's whole fleet across
repositories needs a widened discovery surface — a new function, or an
opt-in `list_all_peers()` — and that is a separate design with its own
trust argument: a cross-repo orchestrator has a materially larger blast
radius than same-repo peer discovery (it can see, and potentially act on,
work in every project a user has open, not just the one it was launched
in). This spec names that as the prerequisite for cross-repo orchestration
and stops there; it does not attempt it.

## What this does not add

- **No `context_window`, no price, no capability score on `PeerInfo`.**
  Argued above — that data lives once, in the model catalog
  (`docs/plans/model-registry.md`), keyed by the `model` string this spec
  publishes.
- **No live telemetry** (context-used %, cost-so-far, tokens burned).
  Already available locally via `/usage`/`/context`; publishing it to
  peers turns a 15-second heartbeat into either stale numbers or a much
  heavier write path for a decision no peer needs to make.
- **No model-callable read of any of this.** The roster stays
  human/TUI-facing, same as `/peers` today.

## Testing bar

- an entry written by an older build (missing `provider`/`model`/`engine`
  entirely) is still read as a live peer, with all three fields `None` —
  not reaped, not defaulted to a guess
- an entry carrying an extra, unrecognized top-level key (simulating a
  *newer* build or a third-party engine) is read without error and without
  losing any of its known fields
- calling `set_model()` on a connected session updates its registry entry
  before the next heartbeat tick — asserted by reading the entry
  immediately after the switch, not after sleeping `heartbeat_secs`
- a registry entry's `title` (and, once added, `provider`/`model`/`engine`)
  passes through `scrub_secrets` before `/peers` renders it — regression
  guard for the gap this spec found on the read path
- nothing under `doxa/operators.py` or the SDK tool surface exposes
  `PeerInfo` fields to the model — a peer's self-description stays
  TUI-facing until a future spec explicitly says otherwise, and argues the
  framing for it

## Open questions

1. **Free-string `engine`, or a fixed set?** A free string (matching
   `ModelInfo.id`'s own looseness) is simplest and costs nothing today with
   one real value; nothing here validates it, so a typo in a future
   engine's own code becomes a silent new "engine" nobody asked for. Not
   settled.
2. **Does `model` changing mid-session deserve a `peer_updated` event**,
   symmetric with `peer_joined`/`peer_left` (`doxa/engine.py:1916-1922`),
   so another session's status display refreshes live rather than on its
   own next read? No data yet on how often `set_model` actually fires
   mid-session to justify the extra event type.
3. **Cross-repo discovery** (see above) — real design work, not attempted
   here.
4. **Does `provider`/`engine` get validated against anything at write
   time**, or is a session free to publish a nonsense value about itself?
   Given the untrusted-self-description stance above, probably not worth
   validating — a validated lie is still a lie — but it means the field's
   only defense is the "advisory, never authoritative" rule holding
   everywhere it is read, forever. Whether that holds under a future
   orchestrator is exactly the open question item T3 above gestures at.

## See also

- `docs/plans/model-registry.md` — the catalog `model` looks up into;
  "One vocabulary, two directions" there is the other half of this
  section's `context_window` argument.
- `docs/plans/plugin-api.md`, extension point 4 — the still-unwritten
  session Protocol (spawn/send/interrupt/event-stream) that a genuinely
  new `engine` value would need to implement.
