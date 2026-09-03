# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.session_ops -- the registry of DOXA's native SESSION tools.

A sibling of :mod:`doxa.operators`, deliberately, and not a sixth entry in
it. That module's first line names its own charter -- "the registry of
DOXA's native LORE tools" -- and everything in it reaches ``lore_core`` and
nothing else. The one tool defined here reaches ``doxa.daemon``,
``doxa.worktrees`` and ``doxa.peers``, and touches ``lore_core`` only for
``scrub_secrets`` -- the one utility every text-carrying path in this
codebase runs through, ``doxa.peers`` included -- rather than for a belief
store, a memory file or an index. Filing it next to the belief-search
tools would blur a boundary that module states about itself. So: a second
registry, in the IDENTICAL
:class:`doxa.operators.Operator` shape, gated through the IDENTICAL
:meth:`doxa.gate.ToolGate.execute` path (containment does not care which
module an ``Operator`` was defined in, only that every call flows through
the one executor), and projected onto the SAME in-process SDK MCP server
(``operators.to_sdk_tools``'s ``extra=`` parameter -- see that function's
docstring for why one server and not two).

**What spawn_session actually is.** An agent inside a DOXA session can
already run ``doxa new`` under Bash today -- ``SessionEngine``'s
``allowed_tools`` defaults to None, ``ToolGate`` cannot see inside a shell
string, and ``doxa new`` is on ``$PATH``. That path is real, reachable and
ungoverned. This module does not add the capability; it adds a MANAGED
instance of it: a call that can be refused, a call that is counted, and a
call whose child carries a parent link. The Bash path is not blocked (that
would need shell-content inspection this codebase does not have) and is
only discouraged, in the tool description the model reads. One thing binds
it anyway: every route funnels through ``daemon.spawn_daemon``, which
writes the same registry entry, so the live-count and rate caps below SEE
a Bash-spawned session even though they could not gate it.

**Default off.** :func:`spawn_enabled` is False unless
``~/.doxa/config.toml``'s ``spawn_sessions`` row -- or the
``DOXA_SPAWN_SESSIONS`` environment variable -- says otherwise, resolved
through :func:`doxa.config.raw`, which reads the environment and that one
file and NOTHING inside the repository being opened. That is the whole
security boundary and it is not a style choice: ``docs/plans/
plugin-api.md`` already established it for plugin loading ("a
repo-supplied plugin would be arbitrary code execution on ``doxa new``
against an untrusted clone"), and a repo that could turn session spawning
on for any session that opens it is the identical hole in a different
shape. When the setting is off the tool is not merely refused, it is not
OFFERED: :func:`_spawn_configured` is the operator's ``is_configured``
predicate, so ``to_sdk_tools`` never projects it and the model cannot see
that it exists. The ``fn`` re-checks anyway -- defence in depth at a choke
point, never trust a single layer.

**Three runaway bounds, enforced HERE.** A limit stated in a tool
description is not a limit; nothing enforces an agent reading and honoring
prose. All three run inside :func:`_spawn_session`, before
``spawn_daemon`` is ever called, in the DOXA process the model cannot
reach -- see :data:`MAX_SPAWN_DEPTH`, :data:`MAX_LIVE_SESSIONS` and
:data:`MAX_SPAWNS_PER_WINDOW` for the numbers and, more importantly, for
what each one was derived from. They are resource rails, not part of the
approval gate: they stay enforced in every permission mode, including
``bypassPermissions``, because "how many, ever" is a different question
from "may this one call happen".

**A refusal is a soft error, on purpose.** Every refusal below is shaped
``"spawn_session: <reason>"`` -- the single-colon convention
``_belief_search`` and ``_remember`` already use -- and never
``"spawn_session failed: ..."`` and never a raise. ``doxa.gate.
is_hard_failure`` treats the ``"<name> failed:"`` shape and the phrase
"not configured" as hard failures that feed the two-strikes tracker; a cap
doing its job is neither. A tool that disabled itself by working correctly
twice would be a bug, and the shape of these strings is what prevents it.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from typing import TYPE_CHECKING, Any

from . import _lore_bootstrap  # noqa: F401 -- sys.path shim, see that module

from lore_core.scrub import scrub_secrets

from . import config as config_mod
from . import peers as peers_mod
from . import worktrees as worktrees_mod
from .operators import Operator

if TYPE_CHECKING:  # pragma: no cover -- import-cycle-free typing only
    from .gate import OperatorContext


# -- the enabling setting ---------------------------------------------

SPAWN_ENV = "DOXA_SPAWN_SESSIONS"
"""The ONE knob. Read through :func:`doxa.config.raw`, whose precedence is
environment > ``$DOXA_HOME/config.toml`` > default -- neither of which is
a file inside the repository a session opened. See the module docstring."""


def spawn_enabled() -> bool:
    """Is session spawning armed on this DOXA install?

    Same explicit-truthy-string reading ``engine.graph_context_enabled``
    and ``claude_plugins.adoption_enabled`` already use for the other
    opt-in capability expansions here: a value has to be present AND not
    one of the words that mean no. The direction is the opposite of
    ``worktrees.enabled()``'s default-ON, deliberately -- a
    worktree-per-session is a pure isolation improvement with no downside,
    while this is a new capability surface with real cost and real risk."""
    value = config_mod.raw(SPAWN_ENV).strip()
    return bool(value) and value.lower() not in ("0", "false", "no", "off")


# -- the three runaway bounds, and where their numbers come from -------

MAX_SPAWN_DEPTH = 2
"""How deep a spawn chain may go. A session started by a human is depth 0;
its child is 1; the child's child is 2, and a session AT this depth
refuses to spawn.

Derived, not chosen: with :data:`MAX_LIVE_SESSIONS` at 3, a depth-3
great-grandchild could only ever exist as the fourth live session in its
scope, which the count cap already refuses. Setting this any higher would
be dead code -- the count cap would always get there first -- and setting
it lower would forbid a shape (delegate splits its work once) that the
count cap permits. 2 is the largest value that is not unreachable.

Threaded through argv as ``--spawn-depth N`` and read once into
``SessionEngine.spawn_depth``, NOT derived by walking ``parent_session_id``
chains through the registry: a chain-walk breaks the moment an ancestor's
entry is reaped (a still-live grandchild whose parent already finalized
has nothing left to walk), while a value each process carries from birth
has no such failure mode."""

MAX_LIVE_SESSIONS = 3
"""Live DOXA sessions permitted in one repo scope, counting the caller.

MEASURED, twice, on the machine this was written on:

* ~294 MB RSS per idle DOXA/``claude`` session, and ~1.5% of a core --
  the figure ``tests/conftest.py``'s reaper fixture records from a real
  full-suite run (32 leaked agents, ~9 GB, "two suites at once put 18 GB
  of them on a 30 GB machine").
* Interactive degradation begins past roughly three concurrent agent
  processes: beyond that, turns visibly slow and the timing-marginal half
  of the test suite starts failing on schedule rather than on logic.

Three sessions is therefore about 880 MB resident before anyone has typed
anything, which is the last point where the machine still behaves. This
is the ONE cap that also sees sessions started through the ungoverned
Bash path, because every route writes the same registry entry."""

SPAWN_RATE_WINDOW_SECS = peers_mod.STALE_AFTER_SECS
"""The rolling window the rate cap looks back over -- deliberately
``peers.STALE_AFTER_SECS`` itself rather than an independent number.

That constant is exactly how long a dead session's presence file keeps
claiming to be live, so it is exactly the interval over which
:data:`MAX_LIVE_SESSIONS` can be counting ghosts. Pinning the rate window
to it means the two caps cover each other's blind spot instead of
overlapping arbitrarily: whatever the count cap can be wrong about, the
rate cap saw arrive."""

MAX_SPAWNS_PER_WINDOW = 1
"""New sessions permitted in one scope per :data:`SPAWN_RATE_WINDOW_SECS`.

One, and the reason is the window above rather than a taste for caution.
Inside ``STALE_AFTER_SECS`` the registry genuinely cannot tell a live
session from one that died a moment ago -- that is what the constant
means. So a session younger than the window is UNPROVEN: the count cap
may be counting it, or may be counting its ghost, and a second spawn made
on the strength of that number is precisely the burst behind the measured
disk failure this feature has to avoid reproducing. One unproven child at
a time; the next spawn waits until the last one has outlived the window
that could be lying about it.

This is also what keeps the rate cap from being dead code. Anything
``MAX_LIVE_SESSIONS - 1`` or higher could never fire -- the sessions it
counts are a SUBSET of the ones the count cap counts, so the count cap
would always get there first. At 1 it binds strictly earlier: a scope may
still reach :data:`MAX_LIVE_SESSIONS`, but it takes two windows to get
there instead of two seconds.

Recomputed from ``started_at`` on every attempt, over the same registry
scan the count cap just read; no new storage, nothing that can drift."""


# -- the disk preflight ------------------------------------------------

WORKTREE_CHECKOUT_BYTES = 18 * 1024 * 1024
"""What a linked worktree of this repository actually costs on disk,
measured (``du -sh`` on a real ``git worktree add``): 18 MB. Small,
because ``git worktree add`` shares the object store -- the cost is the
working tree, not a clone."""

SESSION_BUILD_STATE_BYTES = 407 * 1024 * 1024
"""What a session WORKING in that worktree accumulates on top of it,
measured on this same checkout: 433 MB with build state, 26 MB of tracked
files, so 407 MB of virtualenv, caches and test artifacts. This is the
number that matters and the checkout size is not -- the measured failure
this preflight exists for was a filesystem filling to 79% and killing a
test suite on disk quota rather than on its tests, and it was not the
checkouts that filled it."""

MIN_FREE_DISK_BYTES = WORKTREE_CHECKOUT_BYTES + SESSION_BUILD_STATE_BYTES
"""Refuse below this much free space on the worktree root's filesystem:
one checkout plus one session's working state, both measured above.
``worktrees.py``'s own module docstring states the ethos -- every git call
in it "degrades to 'leave it alone' on failure" -- and a preflight is how
that ethos reaches a mutation that has not started yet: refuse cleanly
rather than let ``git worktree add`` or the child's boot fail opaquely
partway through."""


MAX_TASK_CHARS = 2000
"""Longest task text the model may hand a child.

Derived from the human-review requirement, not from argv limits (Linux
allows 128 KB per argument, so the kernel is not the constraint). The
confirmation dialog renders this text VERBATIM, and the containment
argument in ``docs/plans/spawn-session.md`` rests entirely on a human
reading it before approving. Roughly 100 columns by 20 rows is what that
popup can show at once; a task longer than that is one the approver
scrolled past rather than read."""


# -- what the child is told about where its task came from -------------

SPAWN_PROVENANCE_INTRO = (
    "[SPAWNED SESSION] This session was started by another DOXA session, "
    "not by a person typing. The task below was composed by that session's "
    "agent and approved, verbatim, by the human who owns both sessions -- "
    "so it IS your task, and you should carry it out. This marker is "
    "disclosure, not a trust downgrade: it exists so that anyone reading "
    "this transcript later can see where the task came from, and so that "
    "you can weigh its provenance yourself before doing something "
    "genuinely consequential with it -- spending money, running a "
    "destructive command, or spawning further sessions of your own."
)
"""The one line of framing a spawned child gets, and deliberately NOT
:data:`doxa.peers.PEER_UNTRUSTED_INTRO`.

That marker exists because a peer message is data to weigh, never an
instruction to follow, and by every argument this codebase has made about
model-authored text a spawn task is in the same trust class -- another
agent wrote it, with no human at the moment of composition. But the entire
premise of delegation is that the child treats the text AS ITS TASK. A
child wrapped in "weigh it, take no action on it unless this session's own
user asks" would correctly refuse to do the thing it was spawned to do.
The existing framing is structurally the wrong tool for this channel, not
because the trust problem evaporates but because this channel's premise
contradicts that framing's conclusion.

What replaces it is not a second line of defence and this docstring will
not pretend otherwise. The ACTUAL containment is the confirmation dialog:
a human reads the literal task text before the child exists. This marker
is bookkeeping for later. It is prepended by the RECEIVING side
(``SessionDaemon._initial_task_prompt``), the same place
``peers.frame_for_model`` prepends its own marker, so a parent cannot
suppress it by crafting the ``--task`` argument."""


# -- the operator ------------------------------------------------------

def _spawn_configured(ctx: "dict | None") -> bool:
    """``is_configured`` for spawn_session: the setting, and only the
    setting. ``ctx=None`` still means "don't gate on configuredness" for
    schema-introspection callers, exactly as every other operator's
    predicate does -- but this one ignores ``ctx``'s CONTENTS entirely,
    because what arms spawning is a user's config file, never a seam the
    engine happened to wire."""
    if ctx is None:
        return True
    return spawn_enabled()


def _scope_of(cwd: str) -> str:
    """The scope key the peer registry writes for a session in ``cwd``.

    ``peers.main_repo_root_of``, NOT ``gate.repo_root_of``: the caps below
    compare against entries the registry already holds, and every one of
    those was written with the main-checkout rule (see that function's
    docstring for the measured divergence -- from inside a linked
    worktree, ``--show-toplevel`` answers the WORKTREE's path). A cap that
    scanned under a different key than the entries were filed under would
    count zero, forever, in exactly the worktree-per-session setup that is
    DOXA's default."""
    return peers_mod.main_repo_root_of(cwd) or cwd


def _free_bytes(path: str) -> "int | None":
    """Free space on ``path``'s filesystem, or None when it cannot be
    measured (the directory does not exist yet, an OS that refuses the
    call). None means "do not refuse on this" -- an unmeasurable disk is
    not evidence of a full one, and a preflight that cannot see must not
    become a cap that always denies."""
    probe = path
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def _human_bytes(count: int) -> str:
    if count >= 1024 ** 3:
        return f"{count / 1024 ** 3:.1f} GB"
    return f"{count / 1024 ** 2:.0f} MB"


def _spawn_session(
    task: str,
    model: "str | None" = None,
    base_branch: "str | None" = None,
    op_ctx: "OperatorContext | None" = None,
) -> Any:
    """Start one delegated DOXA session, or refuse.

    Returns EITHER a plain dict (every refusal -- all of them decided
    synchronously, before anything has been started) OR an awaitable that
    resolves to a dict (the one path that actually spawns). That split is
    not a quirk: ``ToolGate.execute`` and ``to_sdk_tools``' handler both
    already know how to settle an awaitable result, so the async half can
    park on a human's answer and hand the subprocess poll to a worker
    thread, while every refusal stays an ordinary synchronous dict that
    ``is_hard_failure`` classifies the same way it classifies every other
    operator's soft error.

    ``cwd`` is deliberately absent from the parameters. The child spawns
    from the parent's OWN repo, derived from ``op_ctx.cwd`` -- the trusted
    sidecar the gate injects as its own kwarg and strips from model args
    unconditionally -- never from a model-writable value. This opens no
    cross-repo path; ``docs/plans/peer-publishing.md`` draws that same
    boundary for peer discovery and it applies here with more force, not
    less."""
    if op_ctx is None:
        # Cannot happen through the gate (spawn_session declares the
        # sidecar), and is still not a raise: a missing trusted context is
        # exactly the state where the safe answer is no.
        return {"error": "spawn_session: no session context -- refusing to spawn"}

    # Defence in depth behind the is_configured filter above. When the
    # setting is off the tool was never projected, so the model cannot
    # have called it -- unless a future refactor drops that filter, which
    # is the case this line exists for. NOTE the wording: never the phrase
    # "not configured", which gate.is_hard_failure counts as a strike.
    if not spawn_enabled():
        return {"error": (
            "spawn_session: session spawning is off on this DOXA install "
            f"-- the user turns it on in ~/.doxa/config.toml (spawn_sessions) "
            f"or {SPAWN_ENV}, and nothing in this repository can")}

    # Scrubbed ONCE, HERE, before anything else looks at it -- the same
    # order ``_remember`` uses ("scrub BEFORE truncation ... on approval
    # this text lands verbatim"). This is not decoration: the containment
    # argument is that a human approved the text the child receives, so
    # the string in the confirmation dialog and the string on the child's
    # command line have to be THE SAME STRING. Scrubbing only at the
    # display end would mean approving a redacted rendering of a prompt
    # that then ran unredacted.
    task = scrub_secrets(str(task or "")).strip()
    if not task:
        return {"error": "spawn_session: empty task -- a child needs something to do"}
    if len(task) > MAX_TASK_CHARS:
        return {"error": (
            f"spawn_session: task is {len(task)} characters, over the "
            f"{MAX_TASK_CHARS} a human can actually read in the approval "
            "dialog -- shorten it, or put the detail in a file and point at it")}

    # -- bound 1: depth (argv-threaded, never chain-walked) ------------
    depth = int(getattr(op_ctx, "spawn_depth", 0) or 0)
    if depth >= MAX_SPAWN_DEPTH:
        return {"error": (
            f"spawn_session: depth limit reached ({MAX_SPAWN_DEPTH}) -- this "
            f"session is already {depth} level(s) deep in a spawn chain")}

    # ONE registry scan feeds bounds 2 and 3, and the confirmation dialog
    # shows the SAME numbers rather than recomputing its own (a display
    # value that can drift from what enforcement checked is worse than no
    # display value).
    scope = _scope_of(op_ctx.cwd)
    peers = peers_mod.list_peers(scope, self_id=op_ctx.session_id)

    # -- bound 2: live sessions in this repo scope ---------------------
    live = len(peers) + 1  # + this session, which list_peers excludes
    if live >= MAX_LIVE_SESSIONS:
        return {"error": (
            f"spawn_session: session limit reached ({MAX_LIVE_SESSIONS}) -- "
            f"{live} live sessions in this repo already")}

    # -- bound 3: rate over the same scan ------------------------------
    recent = [
        p for p in peers
        if peers_mod.age_secs(p.started_at) <= SPAWN_RATE_WINDOW_SECS
    ]
    if len(recent) >= MAX_SPAWNS_PER_WINDOW:
        return {"error": (
            f"spawn_session: rate limit reached ({MAX_SPAWNS_PER_WINDOW} per "
            f"{SPAWN_RATE_WINDOW_SECS:.0f}s) -- {len(recent)} session(s) "
            "started in this repo within that window")}

    # -- the disk preflight --------------------------------------------
    root = str(worktrees_mod.worktrees_root())
    free = _free_bytes(root)
    if free is not None and free < MIN_FREE_DISK_BYTES:
        return {"error": (
            f"spawn_session: only {_human_bytes(free)} free under {root} -- "
            f"a session needs about {_human_bytes(MIN_FREE_DISK_BYTES)} of "
            "worktree and working state, and a spawn that runs the disk out "
            "fails opaquely partway through")}

    return _spawn_after_confirm(
        task=task, model=model, base_branch=base_branch, op_ctx=op_ctx,
        depth=depth, live=live, free=free, scope=scope,
    )


async def _spawn_after_confirm(
    task: str,
    model: "str | None",
    base_branch: "str | None",
    op_ctx: "OperatorContext",
    depth: int,
    live: int,
    free: "int | None",
    scope: str,
) -> dict:
    """The half that can block: ask the human, then start the process.

    Two things must not happen on the event loop and neither does.
    ``spawn_daemon`` polls with ``time.sleep(0.1)`` for up to 60 seconds
    waiting for the child's registry entry -- v0.95.0 moved session
    construction off the loop with ``asyncio.to_thread`` for exactly this
    reason, and this call does the same rather than reintroducing the
    stall. The confirmation parks on a real ``asyncio.Future`` (the
    ``_wait_for_answer`` mechanism ``AskUserQuestion`` already uses), so a
    session waiting for its user is idle, not spinning."""
    from .daemon import spawn_daemon  # local: doxa.daemon imports the engine

    confirm = getattr(op_ctx, "spawn_confirm", None)
    if confirm is None:
        # No confirmation channel wired (a headless embedding, a test that
        # forgot). Refuse rather than spawn unasked -- the dialog is the
        # actual containment this design rests on, not decoration.
        return {"error": (
            "spawn_session: no approval channel in this session -- refusing "
            "to start a session nobody could say no to")}

    answer = await confirm({
        "task": task,
        "model": model,
        "base_branch": base_branch,
        "live_sessions": live,
        "max_live_sessions": MAX_LIVE_SESSIONS,
        "depth": depth,
        "child_depth": depth + 1,
        "max_depth": MAX_SPAWN_DEPTH,
        "free_bytes": free,
        "worktrees_root": str(worktrees_mod.worktrees_root()),
        "scope": scope,
    })
    if not isinstance(answer, dict) or answer.get("decision") != "allow":
        # A declined spawn is the gate working. Same soft shape as a cap.
        return {"error": "spawn_session: the user declined this spawn"}

    # The child spawns from the MAIN checkout of the parent's repo, not
    # from the parent's own linked worktree: worktrees.create is per
    # session and keyed off the main root, and a worktree of a worktree is
    # not a shape it promises. Either way this is derived from
    # op_ctx.cwd -- the sidecar -- and never from anything the model wrote.
    child_cwd = scope
    session_id, daemon_socket = await asyncio.to_thread(
        spawn_daemon,
        child_cwd,
        model=model or None,
        base_branch=base_branch or None,
        spawn_depth=depth + 1,
        parent_session_id=op_ctx.session_id,
        task=task,
    )
    return {
        "session_id": session_id,
        "daemon_socket": daemon_socket,
        "cwd": child_cwd,
        "spawn_depth": depth + 1,
        "live_sessions": live + 1,
        "note": (
            "the session EXISTS and has been given the task; it is not "
            "finished. Nothing reports back to you: a child cannot send "
            "peer messages (only a human typing /msg can), and the peer "
            "registry carries presence, not results. You will see a "
            "peer_left event when it goes away -- which says 'gone', not "
            "'succeeded'. What actually comes back is its COMMITS, on its "
            "own doxa/<short> branch in its own worktree, which you can "
            "git log and git diff whenever you want."
        ),
    }


_SPAWN_SESSION = Operator(
    name="spawn_session",
    description=(
        "Start a SECOND DOXA session in this same repository and give it a "
        "task, then return immediately -- delegation, not a blocking call. "
        "The child is a full peer session: its own claude process, its own "
        "git worktree and doxa/<short> branch, its own LORE context, its "
        "own token spend. It does NOT share this session's transcript or "
        "state; all it gets is the task text and the repo. It cannot "
        "report back -- its result is the commits on its branch. Every "
        "call stops and asks the user, showing them your exact task text, "
        "and is refused server-side once the depth, live-session or rate "
        "cap is reached. This is the correct way to start a delegated "
        "session: running `doxa new` through Bash produces the same "
        "process but bypasses this review and this accounting."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "What the new session should do. A human reads this "
                    "verbatim before the session starts, and the session "
                    "itself receives it as its first prompt."
                ),
                "maxLength": MAX_TASK_CHARS,
            },
            "model": {
                "type": "string",
                "description": "Optional model for the child; omit for the default.",
            },
            "base_branch": {
                "type": "string",
                "description": (
                    "Optional ref the child's worktree forks from; omit to "
                    "fork from this repo's current checkout."
                ),
            },
        },
        "required": ["task"],
        "additionalProperties": False,
    },
    fn=_spawn_session,
    cost="high",
    read_only=False,
    is_configured=_spawn_configured,
)


# -- registry (explicit, like doxa.operators' own) ---------------------

SESSION_OPERATORS: dict[str, Operator] = {
    op.name: op for op in (_SPAWN_SESSION,)
}
"""The whole registry. One entry, listed literally, for the same reason
``doxa.operators`` lists its five literally: adding a tool that can start
a process must be a deliberate, reviewed act and never an import side
effect."""

SESSION_OP_CTX_OPERATORS = frozenset({"spawn_session"})
"""Which of these declare the ``OperatorContext`` sidecar. Unioned with
``operators.OP_CTX_OPERATORS`` by the gate -- the injection rule is one
rule, applied over both registries."""
