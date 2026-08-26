# Session spawn — an agent starting a new DOXA session and delegating to it

Status: **draft for review**. Nothing implemented. `doxa/operators.py`,
`doxa/gate.py`, `doxa/peers.py` and `doxa/daemon.py` are exactly as they are
today; this spec proposes an operator, a gate policy and two new fields, not
code.

## What exists today: an unmanaged path, not a missing capability

An agent running inside a DOXA session already has the Bash tool (DOXA does
not restrict the SDK's built-in toolset by default — `SessionEngine.__init__`'s
`allowed_tools` parameter defaults to `None`, and `ToolGate`'s own contract is
explicit that "no policy set (None) means no filtering: Phase 1 has exactly
one stage and allows everything," `doxa/gate.py:17-20`). `doxa new` is on
`$PATH` in that same shell. So the sequence "an agent decides to delegate,
runs `doxa new`, gets a second daemon-backed session" already works, today,
with no code changed.

What that path lacks is everything this spec is about:

- **No gate.** `ToolGate.pre_tool_use` governs tool *names*
  (`doxa/gate.py:138-158`); it has no mechanism to inspect a Bash call's
  argv, so `doxa new` inside a Bash string is invisible to it. Confirmed by
  reading the module — there is no command-content filter anywhere in
  `gate.py` or `engine.py`. Blocking it outright would need a second kind of
  containment (shell-command inspection) this codebase does not have, is a
  materially larger and harder design than one operator, and is out of
  scope here.
- **No accounting.** Nothing counts how many sessions one Bash-invoked chain
  has produced, or how deep it has gone.
- **No parent link.** The child's `PeerInfo` entry looks exactly like a
  session a human started directly — `doxa/peers.py`'s registry carries no
  field that could say otherwise (see "Attribution" below).
- **Invisible except through the registry.** The spawning session finds out
  its child exists the same way any other session would — `/peers`, or a
  `peer_joined` event once the child's `PeerHost` writes its entry
  (`doxa/peers.py:513-523`). There is no dialog, no confirmation, no record
  that this particular peer was requested rather than independently started.

So this spec is not proposing new capability. `doxa new` under Bash is real,
reachable today, and produces a real daemon, worktree and registry entry —
`doxa/daemon.py:990-1060` (`spawn_daemon`) and `doxa/worktrees.py:249-312`
(`create`) run exactly the same either way. **The work here is replacing an
unmanaged instance of an existing path with a managed one: a call that can be
refused, a call that is counted, and a call that is seen as what it is** (a
delegation, not an independent session start) rather than merely present in
the registry.

**What happens to the Bash path once a sanctioned tool exists.** Left alone,
not blocked, and only partly discouraged, for a reason stated plainly rather
than assumed: `ToolGate` cannot see inside a Bash string, so "blocked" is not
available without inventing shell-content inspection, and that is a
different, larger design than this one. Discouragement is what's left: the
sanctioned operator's description should say, in the text the model reads,
that it is the correct way to start a delegated session and that invoking
`doxa new` via Bash bypasses review and accounting — the same kind of steer
`to_sdk_tools` already gives every operator through its `[cost: ...]` /
`[write: staged for review]` suffix (`doxa/operators.py:494-497`). This is
advisory, not enforced, and the spec should not claim otherwise. One thing
*is* enforced regardless of path: because `spawn_daemon` is the single
function every route funnels through, an unmanaged Bash spawn still lands in
the peer registry `read_registry()` scans (`doxa/peers.py:256-305`) — so the
count-based caps in "Runaway bounds" below still see it and can refuse the
*next* sanctioned spawn on the strength of it, even though the Bash spawn
itself was never asked and carries no parent link. Counted, imperfectly; not
gated; not attributed. That asymmetry is real and is left as a named
limitation, not resolved further here.

## Sessions, not subagents, and why

DOXA cannot spawn subagents. The `claude` CLI's own `Task` tool does, entirely
inside the spawned subprocess (`docs/plans/model-registry.md`'s "spawn seam"
section, corroborated directly: DOXA only *observes* `Task` calls via the
subagent tracker, `tests/test_subagent_tracker.py`). Two facts, both measured
and already recorded in `model-registry.md`, make a `Task` subagent unusable
as a delegation target regardless of what this spec wants:

1. **No LORE snapshot reaches it.** DOXA's one context-injection channel is
   `system_prompt={"append": "[LORE SNAPSHOT]\n" + snapshot}` on the *parent*
   session's own `ClaudeAgentOptions` (`doxa/engine.py:1814-1818`) — "a
   parent's `--append-system-prompt` does not propagate to a `Task`-spawned
   subagent (measured)."
2. **No addressable session id.** DOXA mints a uuid per session and resumes
   against it (`doxa/engine.py:1750-1789`) — that id is what `set_model`, the
   peer registry, and `/resume` all key on. A `Task` subagent is a tool call
   inside the CLI's own loop, not a second `ClaudeAgentOptions` DOXA builds;
   there is nothing to register, resume or send a message to.

A subagent also has no worktree (`doxa/worktrees.py`'s whole mechanism is
per-*session*, keyed by session id — `_short_id`, `worktrees.py:134-139`), no
mailbox (`PeerHost` is one per `SessionEngine`, `doxa/peers.py:451-464`), and
dies with the turn that spawned it. A DOXA session, by contrast, is a
`claude` process with its own daemon, its own worktree, its own registry
entry, its own inbox, and a lifetime independent of any one turn.

**What this means for delegation:** everything DOXA can hand to a spawned
session is everything a session *is* — a task prompt, a cwd/worktree, a
model choice, a starting LORE snapshot. Nothing about DOXA's finer-grained
state (the parent's own transcript, its in-flight tool results, its
`ToolGate` counters) crosses, because none of that lives at the session
boundary — it lives inside one `SessionEngine` instance. A spawned session is
a peer, addressable the same way any independently-started session is
(`/peers`, `/msg`, `/attach`), not a lighter-weight thing living inside the
parent's own turn. This is why the feature is *session* spawn and not
*subagent* spawn — DOXA has exactly one primitive with an id, a worktree and
a mailbox, and it is the session.

## What the operator looks like

A new registered `Operator` (`doxa/operators.py:93-114`'s shape, reused —
`name`, `description`, `parameters`, `fn`, `cost`, `read_only=False`,
`is_configured`), but **not added to `doxa/operators.py` itself.** That
module's own docstring names its charter in its first line: "the registry of
DOXA's native LORE tools." A process-spawning capability that touches
`doxa.daemon`, `doxa.worktrees` and `doxa.peers` and never touches
`lore_core` at all would blur a boundary the module states about itself —
the same "second taxonomy meaning almost the same thing" drift
`model-registry.md` argues against for its own tier field. It needs a
sibling module (e.g. `doxa/session_ops.py`) defining its own small registry
in the identical `Operator` shape, gated through the identical
`ToolGate.execute` path — containment does not care which module an
`Operator` was defined in, only that every call flows through the one
executor (`doxa/gate.py`'s module docstring, contract 2). Exactly how that
second registry composes with `to_sdk_tools`' current single-module
assumption (`doxa/operators.py:467-505`, and its one call site,
`doxa/engine.py:1722`) — a second `create_sdk_mcp_server`, or a parameter
that lets `to_sdk_tools` take more than one registry — is an implementation
decision this spec does not force.

Parameters, at minimum: `task` (the text the child session is told — see
"What the spawned session is told" below), and optionally `model`,
`base_branch`. `cwd` is *not* a model-suppliable parameter — the child
always spawns from the parent's own repo (`OperatorContext.cwd`,
`doxa/gate.py:56-71`, the trusted sidecar every `OP_CTX_OPERATORS` entry
already receives, never a model-writable value) — the "Cross-project is out
of scope" boundary `peer-publishing.md` already draws for peer discovery
applies with the same force to spawning: this spec does not open a
cross-repo path.

`is_configured`: default **off**. The precedent is `worktrees.enabled()`'s
`_bool("DOXA_WORKTREE", True)` (`doxa/worktrees.py:65-81`) — but spawn's
default inverts that precedent's *direction* on purpose. Worktree-per-session
is a pure isolation improvement with no real downside, hence default-on;
spawn is a new capability surface with real cost and real risk (see below),
hence default-**off**, opt-in per DOXA install. And, following
`plugin-api.md`'s own rule for exactly this class of problem: the enabling
setting lives only in `~/.doxa/config.toml`, **never** in a repo-local file —
"a repo-supplied plugin would be arbitrary code execution on `doxa new`
against an untrusted clone" (`docs/plans/plugin-api.md`, "Trust and
loading"); a repo that could turn spawn on for any session that opens it is
the identical hole in a different shape.

## Runaway bounds

A session that can spawn sessions can spawn sessions that spawn sessions. The
requirement is not "say this shouldn't happen" — a limit stated in a tool's
description is not a limit, because nothing enforces an agent reading and
honoring prose. Three checks, each with a concrete, code-level place they run,
**inside the operator's `fn`, before `spawn_daemon` is ever called** —
server-side, in the DOXA process the model cannot reach:

- **Depth.** Threaded through argv, the same way `base_branch` and `resume`
  already are: `spawn_daemon`'s docstring states plainly that a subprocess
  daemon has no channel back to its parent's in-memory state except what
  rides on its own command line (`doxa/daemon.py:1017-1020`, "the daemon is a
  separate process, so this is the only way anything reaches
  `SessionDaemon.__init__`'s own `base_branch` parameter"). A new
  `--spawn-depth N` flag follows the identical pattern, read once at daemon
  start into `SessionEngine.spawn_depth: int` (0 for a root, human-started
  session). The operator's `fn` refuses (`{"error": ...}`, gate.py contract
  2's graceful-degradation shape — never a raise) once
  `self.spawn_depth >= MAX_SPAWN_DEPTH`, and passes `spawn_depth + 1` to the
  child's own `--spawn-depth`. Chosen over deriving depth by walking
  `parent_session_id` chains through the registry (see "Attribution") because
  a chain-walk breaks the moment an ancestor's entry is reaped (a still-live
  grandchild whose parent already finalized has nothing left to walk) — an
  argv-threaded value each process carries from birth has no such failure
  mode.
- **Count.** A live-session cap per repo scope, checked by scanning
  `peers.read_registry()` filtered to `scope_key`
  (`doxa/peers.py:369-381`'s own `list_peers`, reused directly) at the
  moment of the call. Refuses once the count meets the cap. This is the one
  check that also sees Bash-spawned sessions (see "What exists today" above)
  — every `spawn_daemon` call writes the same registry entry regardless of
  caller.
- **Rate.** A rolling window over the same scan: how many entries share
  `scope_key` and have `started_at` (an existing field on every entry,
  `_ENTRY_FIELDS`, `doxa/peers.py:90-93`) within the last N seconds. No new
  storage — recomputed fresh on each attempt, the same "derive, don't store
  what drifts" discipline `model-registry.md` argues for capability fields.

None of these three caps' exact numbers are settled by this spec — see Open
Questions. What is settled is that all three are computed from data the
*registry already carries*, checked by code the model never executes, and a
refusal is phrased so it does **not** trip `ToolGate`'s two-strikes tracker:
`is_hard_failure` treats `"not configured"` and the `"<name> failed: ..."`
shape as hard, everything else — including a single-colon validation message
— as a working tool correctly saying no (`doxa/gate.py:90-107`). A budget
refusal ("spawn_session: depth limit reached (2)") is deliberately shaped
like the existing `"<name>: <reason>"` soft-error convention `_belief_search`
and `_remember` already use (`doxa/operators.py:141`, `:350`) — a cap doing
its job is not a broken tool, and must not get disabled by its own
enforcement.

## Cost, made visible

Each spawned session is a `claude` subprocess, a linked git worktree, and its
own token spend — three separate costs, and DOXA has no aggregate view of any
of them across a fleet today. Measured tonight, in this session's own setup:
several concurrent sessions filled `/tmp` to 79%, and a test suite failed on
disk quota rather than on the tests it was meant to run. That is exactly the
failure mode a spawn feature makes easier to trigger by accident, not a
hypothetical.

**Before a spawn:** the confirmation this spec requires (see "The gate")
states, in the second person, the same way `PermissionModeConfirm` already
states what a permission mode changes (`doxa/ui/dialogs.py:1571-1589`
"the body states WHAT STOPS HAPPENING... rather than asking 'are you sure?'"):
a new `claude` process is about to start, a new worktree is about to be
created under `worktrees_root()` (disk cost is mostly the working tree, not a
full clone — `git worktree add` shares the object store), and this child's
token spend is separate from and additive to this session's own. The dialog
also shows the *same number the enforcement code just computed* — "there are
already N live sessions in this repo, at depth D" — rather than a second,
independently-computed display value that could drift from what the caps
above actually checked.

**Disk preflight.** Given tonight's measured failure, the operator's `fn`
should check available space on the worktree root's filesystem before
calling `spawn_daemon`, refusing gracefully under a threshold rather than
letting `git worktree add` or the child's own boot fail opaquely partway
through — the same "leave it alone / never half-do a mutation" ethos
`worktrees.py`'s own module docstring states for every git call in it
("Every git call here degrades to 'leave it alone' on failure").

**After a spawn — fleet cost aggregation is explicitly deferred, not
solved.** `/peers` and `/usage` both exist today; neither aggregates.
`peer-publishing.md` already drew the line this spec will not cross: "the
same reasoning excludes live numeric telemetry — context-used percentage,
cost so far, tokens burned... `PeerInfo` is presence plus identity; it is not
a second `/usage`." An aggregate fleet-cost view would have to be a *pull* —
a live query to each descendant, not a field on the 15-second heartbeat this
spec's own registry entries already use — and this spec does not design that
pull mechanism. Left as an open question, honestly, rather than quietly
re-opening a door `peer-publishing.md` closed for a stated reason.

## The gate

Spawn **must** be wired as an ordinary `claude_agent_sdk` tool call — an
`Operator` behind the same `PreToolUse`/`can_use_tool` choke points every
other tool goes through — never a bespoke engine-level RPC that bypasses
them. That is not a style preference; it is the only way it inherits
`permission_mode` enforcement for free instead of needing to reimplement it,
and reimplementing containment outside the one choke point
`docs/gate.py`'s own module docstring insists on ("tool allowlisting is
session-scoped... so 'this session may only use these tools' has to be
enforced HERE") is exactly the mistake that docstring exists to prevent.

Given that constraint, DOXA's six permission modes
(`doxa/engine.py:149-252`) are not a uniform yes/no gate — they diverge on
purpose, and spawn has to be checked against each rather than assumed to
follow the group:

- **`plan`** — "no tool executes at all" (`engine.py:193`). Spawn is a tool
  call; it is blocked **for free**, by the same mechanism that blocks every
  other tool in this mode, with no special-casing needed. That "no
  special-casing needed" claim is exactly the kind of thing a future refactor
  could quietly break by moving spawn outside the normal tool-call path — see
  the testing bar below, which pins it down as an explicit assertion rather
  than an assumption.
- **`default` / `acceptEdits`** — `acceptEdits` only stops asking about *file
  edits*; "the rest still is" (`engine.py:192`). Spawn is not a file edit, so
  it is not covered by that carve-out in either mode; it still asks, through
  the confirmation this spec adds (below).
- **`auto`** — "a model classifier decides instead of the user"
  (`engine.py:194`). This is the mode where the exact risk this spec's setup
  named — a fleet spawning further fleet with nobody watching — would happen
  silently if spawn rode the classifier like every other tool. **Decision:
  spawn is explicitly excluded from `auto`'s delegation** and still surfaces
  the confirmation dialog even under `auto`. Precedent for carving an
  exception out of the six-mode uniformity already exists in this same
  module: `dontAsk` sits alone in `GATED_MODES`, behind its own confirmation,
  "because it was not asked for" — a deliberate, named asymmetry rather than
  an oversight (`engine.py:215-226`). This spec adds a second one, for a
  different but comparably strong reason: a spawn is not reversible the way
  a wrongly-auto-approved edit is — `git` can revert an edit; a spawned
  process has already spent tokens and disk before anyone notices the
  classifier let it through.
- **`bypassPermissions`** — runs unapproved, like everything else in this
  mode, and gets no special carve-out here. The user who cycled all the way
  out to bypass, or typed `/mode bypassPermissions` past its own confirmation
  dialog (`PermissionModeConfirm`), already accepted exactly this risk in
  general terms; treating spawn as one more exception would be inconsistent
  with a mode this codebase already respects as "here by explicit user
  decision, twice, against the recommendation that was put to them in
  writing" (`engine.py:206-207`). What does **not** relax under bypass: the
  depth/count/rate caps above are DOXA's own resource safety rails, not part
  of the approval gate, and stay enforced regardless of permission mode —
  they answer "how many," which is a different question from "may this one
  call happen."
- **`dontAsk`** — silently denies anything not pre-approved
  (`engine.py:218-225`). Nothing is pre-approved today; spawn is silently
  denied under this mode, with no special-casing needed.

**The confirmation itself is not left to the CLI's own dangerousness
classifier.** `_on_can_use_tool` only escalates to an interactive prompt when
the CLI populates `context.title`/`display_name`/`decision_reason` — fields
it sets "for a call it would genuinely have shown its own interactive
permission prompt for" (`doxa/engine.py:1319-1324`). Whether the installed
CLI's classifier extends that treatment to a brand-new, DOXA-defined MCP tool
name it has never seen is **not something this repo's code can answer** —
that behavior lives inside the `claude` CLI binary, not in anything read for
this spec. Rather than depend on an unverified assumption, spawn should reuse
the mechanism DOXA already built for exactly this situation:
`_ask_user_question`'s `_wait_for_answer` pattern (`doxa/engine.py:1331-1382`)
— park the call on the out-of-band queue, emit a `needs_input` event, block
for a real answer, shaped like a new `SpawnConfirm` dialog in
`doxa.ui.dialogs` (the `PermissionModeConfirm` shape,
`doxa/ui/dialogs.py:1571`, reused: state what starts happening, in the
second person, Esc cancels). This makes the ask an explicit DOXA-owned
behavior that does not depend on an unverified CLI heuristic ever firing.

**Refusable per call, and budgeted — both, not a choice between them.** The
confirmation dialog is the "may this one call happen" gate; the depth/count/
rate caps are the "how many, ever" gate. A human saying yes to one call
cannot raise the caps, and the caps cannot substitute for asking — they
answer different questions and both apply on every call.

## Attribution

`PeerInfo` (`doxa/peers.py:189-215`) has no parent field today, so a spawned
fleet reads, in the registry, exactly like N independently-started sessions.
One new field:

```python
parent_session_id: str | None = None
"""The session_id of the DOXA session whose spawn_session call created this
one. None means either a human started this session directly, or it was
started through the unmanaged Bash path (see "What exists today"), which has
no channel to populate this field -- the two are NOT distinguishable from
this field alone, and this spec does not add a third state to make them so."""
```

Added by the **identical mechanism** `peer-publishing.md` already specified
and this spec must not reinvent: a dataclass default of `None`, read
defensively with `.get()` in `read_registry` (alongside `daemon_socket` and
`clients`, `doxa/peers.py:286-289`), and **never** added to `_ENTRY_FIELDS`
(`peers.py:90-93`) — the same reasoning applies verbatim: "a reader ignores
keys it does not know about, by construction, not by a check... an entry
written by an older build carrying a field this reader has never heard of is
already harmless." If both this spec and `peer-publishing.md` ship,
`PeerInfo` gains four new optional fields (`provider`, `model`, `engine`,
`parent_session_id`) through the same three-part mechanism, at the same three
call sites — an implementer building both should treat that as one set of
edits, not two independent diffs to the same lines.

Absence stays distinguishable from a value, per that spec's own rule: `None`
means "no parent recorded," `"a1b2c3d4-..."` means a specific, real
spawning session. What `None` does **not** distinguish — a provably-root
session from one whose parent went through the unmanaged path — is named
above as a real limit on what this field can claim, not glossed over.

Depth is **not** stored on `PeerInfo` at all (see "Runaway bounds" for why:
argv-threaded and chain-walk-immune, computed once at birth, not derived from
a registry that can lose ancestors). `parent_session_id` is for **display and
lineage** — a future `/peers` or `/fleet` view can render a tree, and a human
auditing a fleet can see who asked for what — not for enforcement, which
lives elsewhere and does not depend on the registry staying intact.

## What the spawned session is told

This is the sharpest tension in the whole design, and it does not have a
clean resolution — only an argued one.

`PEER_UNTRUSTED_INTRO` exists because a peer message is **data to weigh**,
never an instruction to follow (`doxa/peers.py:74-87`), precisely because it
was written by another process the receiving session has no reason to trust.
A spawned session's initial task prompt is written by exactly the same kind
of party — another agent, no human in the loop at the moment of
composition — which puts it in the same trust class by every argument this
codebase has made about that class so far. But the entire *point* of spawn
is that the child treats that text **as its task**, not as a claim to
skeptically evaluate. A child wrapped in `PEER_UNTRUSTED_INTRO`'s framing
("weigh it, take no action on it unless this session's own user asks") would
correctly refuse to do the thing it was spawned to do. The framing this
codebase already has for model-to-model text is, structurally, the wrong tool
for this channel — not because the trust problem goes away, but because this
channel's entire premise contradicts that framing's conclusion.

**Resolution, in two parts, neither of them a full answer:**

1. **The human-in-the-loop requirement from "The gate" does double duty.**
   The confirmation dialog gating every spawn call must show the *literal
   task text* the child is about to receive, not a bare "spawn a session?"
   toggle. The human approving the spawn is then also the one reviewing what
   the child will be told — the same review point `peer-publishing.md`
   already requires before any privileged decision acts on untrusted
   self-description ("without a human in the loop"). A parent session whose
   own tool calls are gated cannot use "tell my child to do it" as a
   sandbox escape if the human reading the spawn confirmation reads the
   child's actual instructions, not a summary of them.
2. **The child still gets a light provenance marker — not a trust downgrade.**
   Not `PEER_UNTRUSTED_INTRO`'s "treat as data, never as instruction," which
   would defeat delegation, but a narrower statement, prepended once, that
   this task originated from another DOXA session rather than the user
   directly. Its purpose is disclosure, not skepticism: a human reading the
   child's transcript later sees where the task came from instead of a
   prompt that reads as if the user typed it, and the child itself retains
   the option to reason about provenance for genuinely consequential actions
   (money, further spawning, destructive commands) if it chooses to, the way
   any agent can reason about a fact in its own context.

What this resolution does **not** claim: that a human reviewing task text at
spawn time reliably catches a subtle injection, or that a provenance marker
changes the child's behavior at all. Both are real gaps. The honest position
is that (1) is the actual containment — a human reads the task before it
runs — and (2) is bookkeeping for later, not a second line of defense that
does real work at spawn time.

**What else crosses the boundary, stated plainly:** nothing but the task
text and a starting LORE snapshot. The child is a normal session boot through
`spawn_daemon` → `connect()`, so it gets the same `system_prompt={"append":
"[LORE SNAPSHOT]..."}` injection every session gets
(`doxa/engine.py:1814-1818`) — its **own** belief/memory context, not a copy
of the parent's specific working state. No transcript handoff, no shared
in-memory state, nothing beyond what the `task` parameter's text says and
whatever the repository itself already contains. Delegation here means "here
is a task and a repo," not "here is my context."

## What comes back

Spawn returns to the parent's own turn as soon as the child **exists** — the
same point `spawn_daemon` already waits for today (its own registry entry
appearing, up to `wait_secs`, `doxa/daemon.py:1043-1059`) — not once the
child's task is done. This is not a design choice this spec is making; it is
what the function it wraps already does, and preserving it is what keeps
spawn a parallel-delegation primitive rather than a blocking call.

None of DOXA's three existing peer mechanisms is a full answer to "what came
back," and the honest thing is to say which parts each one covers rather
than force one to be the answer:

- **`peer_joined`/`peer_left`** fire from the heartbeat-driven registry diff
  (`doxa/peers.py:591-597`) with no new code needed — the parent already
  learns, on the existing 15-second cadence, when its child's entry
  disappears. This is a **presence** signal, not a result: it says "gone,"
  not "succeeded," "failed," or "was killed."
- **`/msg`** is model-*unreachable* on purpose — "the model has no send
  tool — every peer message crosses because a human typed `/msg`"
  (`docs/manual.md:444-445`). A child cannot proactively report back even if
  it wanted to; giving it that would be exactly the model-callable-send
  capability DOXA has withheld everywhere else, and this spec does not
  propose reopening it.
- **The registry itself carries no result payload.** `PeerInfo` is presence
  plus identity, and `peer-publishing.md` already rejected putting live
  content on it. There is nothing to "read" from the registry beyond whether
  the child is still there.

**What actually comes back is the child's own commits, on its own
`doxa/<short>` branch, in its own worktree** — the same thing that "comes
back" from any two DOXA sessions on one repo today. `finalize`'s clean/dirty
rule (`doxa/worktrees.py:512-547`) applies identically to a spawned session:
clean and zero commits ahead vanishes with no trace; anything else is kept,
`"kept doxa/<short> — merge when ready"`. A parent notified by `peer_left`
that its child is gone can `git log`/`git diff` the child's branch the moment
it wants to, with zero new machinery — delegation's result is a branch
sitting in the same repo, not a message.

**Open, and not resolved here:** `peer_left` fires identically whether the
child finished its task, crashed, or was killed mid-task by the user via
`/sessions kill <prefix>` (`docs/manual.md:434-436`) — `finalize`'s own
dirty/clean check does not distinguish those causes either, it only asks
"is there uncommitted or unmerged work right now." A parent (or a fleet
built on top of this) cannot currently tell "my delegate succeeded" from "my
delegate was killed" from the presence signal alone. Widening `PeerInfo`
with an outcome tag would cross the same "no live telemetry" line
`peer-publishing.md` drew for cost and context-window and would need its own
argument the way that spec built one — not attempted here.

## Testing bar

- a spawn attempted under `plan` mode is refused by the existing "no tool
  executes" enforcement, with **no spawn-specific code path involved** —
  regression guard for the "inherited for free" claim above, since a future
  change that moves spawn outside the normal tool-call route would silently
  reopen this
- a spawn attempted under `auto` mode still surfaces the confirmation
  dialog and is not silently classifier-approved — the one deliberate
  exception to `auto`'s delegation, asserted directly rather than assumed
- a spawn refused by the depth, count, or rate cap produces a soft,
  single-colon-shaped error and does **not** trip `ToolGate`'s two-strikes
  disable after two such refusals — regression guard distinguishing "a
  working cap saying no" from "a broken tool"
- a spawn's depth/count/rate check runs and can refuse **before**
  `spawn_daemon` is ever invoked — no subprocess started, no worktree
  created, for a call the caps were always going to refuse
- an older registry entry (missing `parent_session_id`) is still read as a
  live peer with the field `None` — not reaped, not defaulted to a guess,
  the same contract `peer-publishing.md`'s testing bar already states for
  its own three fields
- a spawned session's initial system prompt carries its own LORE snapshot
  injection, verified independently of the parent's — regression guard for
  "sessions, not subagents": a spawned session must never silently share or
  omit context the way a `Task` subagent measurably does
- the enabling setting (`spawn_enabled` or equivalent) is read only from
  `~/.doxa/config.toml` / its env override, never from anything inside the
  repository being opened — security assertion, same bar `plugin-api.md`
  states for its own allowlist
- a killed spawned session's worktree obeys the existing `finalize`
  clean/dirty rule unchanged — no new special case for "killed while spawned
  by another session" that could diverge from "killed" generally

## Open questions

1. **What are the actual depth/count/rate numbers?** This spec settles the
   enforcement mechanism (registry-scan and argv-threaded, checked
   server-side) and not the defaults. A number picked without real usage
   data is a guess either way; getting it wrong in the permissive direction
   reproduces tonight's disk-quota failure, and getting it wrong in the
   restrictive direction makes the feature useless. Not settled here.
2. **Does the CLI's own dangerousness classifier extend to a brand-new MCP
   tool name at all?** Stated above as unverifiable from this repo's code —
   real uncertainty this spec designed around rather than resolved, by not
   depending on the answer either way.
3. **Fleet cost aggregation.** Named as deferred, not designed. Whether it
   should be a pull query over the peer socket, a new `/fleet` command, or
   left to `git`/`/usage` per-session forever is a real open design question
   `peer-publishing.md`'s own boundary does not answer for this spec either.
4. **Outcome on `peer_left`.** Whether "succeeded / crashed / killed" is
   worth a new field, and where it would live without repeating the mistake
   `peer-publishing.md` already named (stale or heavy telemetry on a
   heartbeat write) — not resolved.
5. **Where does the sibling operator module actually live, and how does it
   compose with `to_sdk_tools`'s current single-registry shape** (a second
   `create_sdk_mcp_server`, or a widened `to_sdk_tools` signature)? Named as
   an implementation decision this spec deliberately does not force.
6. **Does a child ever need to spawn with a *different* repo than its
   parent's?** This spec forecloses it by treating `cwd` as non-model-
   suppliable, following `peer-publishing.md`'s cross-repo boundary — right
   for a first version, but a real limitation if delegation ever needs to
   span repositories, which is its own design with its own trust argument,
   same as that spec already names for cross-repo peer discovery.
7. **Is the two-part resolution in "What the spawned session is told" load-
   bearing enough, or theater?** Argued above as containment (the human
   reads the task) plus bookkeeping (the marker), not two independent
   defenses. Whether a human actually reads spawn confirmations carefully
   enough for (1) to do real work, at fleet scale, is not something a spec
   can settle — only usage can.

## See also

- `docs/plans/model-registry.md` — the "spawn seam" section this spec is the
  other half of: the model-registry side of "pick a model for a task,"
  including the finding (measured, cited above) that a `Task` subagent gets
  no LORE snapshot and no addressable id.
- `docs/plans/peer-publishing.md` — `PeerInfo`'s existing compatibility
  mechanism, reused verbatim for `parent_session_id`; its own boundary
  against live telemetry on the heartbeat, which this spec's deferred cost-
  aggregation question respects rather than crosses.
- `docs/plans/plugin-api.md` — the "never load from the working repository"
  rule, applied here to the spawn-enabling setting itself; extension point 4
  (the still-unwritten multi-provider session Protocol) as the eventual
  home for `spawn`/`send`/`interrupt` if a second engine ever needs them.
