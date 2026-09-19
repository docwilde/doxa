# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.fleet -- spawn N sessions, hand them ONE identical prompt at ONE
instant, wait for quiescence, collect the ledger, tear down.

This is prerequisite 5 of docs/plans/emergent-organization.md, and it is an
INSTRUMENT before it is a feature. That plan's methodological core is one
sentence -- "**Every participant therefore receives the same task
description at the same moment, and none is privileged in any way.** If a
coordinator appears out of that, it appeared through interaction" -- and
every design choice below exists to make that sentence literally true of a
run rather than approximately true of an intention. A human opening N
terminals cannot produce it: the first terminal is informed seconds before
the last, and an initial asymmetry is exactly the confound the plan spends
its second section rejecting.

WHAT SYMMETRY MEANS HERE, mechanically, because "at the same moment" is
not implementable and the honest approximation has to be stated:

1. **Nobody is prompted until everybody is armed.** Every session is
   spawned AND has a client attached to it before the first prompt frame
   is written. A session cannot start working while another does not yet
   exist. This is the property that actually matters, it is absolute
   rather than approximate, and :class:`FleetRun` enforces it with a
   barrier (:meth:`FleetRun.dispatch`).
2. **The text is byte-identical.** One string, handed to every session,
   never formatted per-session -- no "you are agent 7 of 32", because a
   number is a position and a position is a privilege.
3. **Dispatch order is randomised per run** from the run seed, so slot 0
   is not systematically first across the five replications of a cell.
   Over a cell, ordering is noise rather than a variable correlated with
   slot index.
4. **The residual spread is measured, not assumed.** Every slot records
   the monotonic instant its prompt frame was written;
   :attr:`RunReport.dispatch_spread_s` is the observed width of the
   dispatch window and goes in the manifest, so a paper can state it
   rather than claim simultaneity.

WHAT ELSE A RUN OWNS:

* **Model assignment is randomised per run and recorded** (:func:`assign`).
  The experiment's column C/D exists to separate "coordinated because of
  position" from "coordinated because it was the strong model", which only
  works if the mapping slot -> model is redrawn every run and written
  down. The draw is seeded, so a run reproduces from its manifest.
* **Each run gets its own DOXA_HOME**, so the ledger at
  ``$DOXA_HOME/peers/messages.jsonl`` (doxa.peerledger's own "one file per
  DOXA_HOME, which is also how a harness collects a run") IS the run, with
  nothing to filter and nothing to separate afterwards. Each run also gets
  its own runtime dir, so the registry a fleet discovers is the fleet --
  not whatever else the operator happens to have open.
* **Memory is a per-agent variable, not a per-fleet switch**
  (:class:`MemoryPolicy`). A shared LORE store is a communication channel
  that does not appear in the ledger: thirty-two agents reading and writing
  one belief store can coordinate through memory instead of through
  messages, and any hierarchy that formed could have been negotiated
  somewhere the graph cannot see. Per-agent control turns that confound
  into a manipulable variable -- a run may mix agents with and without
  memory and ask whether shared memory substitutes for messaging.
* **Nothing hangs.** Every phase has a deadline and every deadline has an
  escalation. A session that never answers is marked, left behind, and
  killed; a run that never ends is a run that cannot be replicated, which
  at 640 agent-sessions is not a tolerable failure mode.

RESOURCE ARITHMETIC, measured rather than estimated, and the reason
:data:`DEFAULT_N` is not 32:

    live CLI process, resident   425-545 MB   (measured, emergent-organization.md)
    per session incl. daemon     ~600 MB
    N = 32                       ~19 GB
    N = 8                        ~4.8 GB

The 192 GB workstation swallows N=32 without noticing. A 30 GB laptop with
7 GB already resident and swap in use does not: 19 GB of new anonymous
memory on top lands in swap, and a swapping fleet does not measure
coordination, it measures paging. So N is a PARAMETER with a small default
and :func:`capacity_note` states the arithmetic for whatever N was asked
for. :func:`check_capacity` refuses an N whose estimate exceeds available
memory unless the caller explicitly overrides -- a refusal that names the
numbers, never a silent success that ends in the OOM killer taking half
the fleet and leaving a ledger that looks like attrition.

SUPERVISOR MODE, and why a module built to refuse privilege grew a way
to grant one. Everything above describes an INSTRUMENT: a symmetric run
exists to answer a question about emergence, and every asymmetry in it is
a confound. Ordinary software work is the opposite problem. An operator
who wants four sessions to split a refactor does not want four agents
each deciding independently to edit the same file; they want one session
to take the task and hand out the pieces. ``--supervisor <engine:model>``
is that second shape, and it is a MODE rather than a replacement:

* ``mode="symmetric"`` -- today's run, unchanged in every particular. One
  prompt, one instant, no roles, no supervisor. Nothing in this paragraph
  reaches it.
* ``mode="supervisor"`` -- ``n`` WORKERS drawn from ``--pool`` exactly as
  before, plus ONE supervisor session at slot 0 (workers shift to
  1..n). The operator's prompt goes to the supervisor and to nobody
  else; the workers get a short briefing composed by this module, and
  their work arrives from the supervisor as peer messages.

The two properties the symmetric run keeps, because they are correct in
both shapes: the BARRIER (nothing is prompted until every session is
armed) and the RECORD (who was dealt what, in which order they were
prompted, in the manifest). What supervisor mode drops is the shuffle --
dispatch order there is a deliberate sequence, workers first and the
supervisor last, so that no worker can be handed a task before it has
been told what it is. It is recorded like any other order.

The briefings are :func:`worker_briefing` and :func:`supervisor_briefing`
and they are dispatched AS TURNS, which is the one structural decision
worth defending here. See :func:`worker_briefing`'s own comment.

TESTING THIS. Every backend seam is injected (:class:`FleetBackend`), so
the orchestration -- the barrier, the seeded shuffle, the quiescence
deadline, the teardown escalation -- is testable without spawning a single
process or spending a token. :class:`DaemonBackend` is the real one and is
the only part that knows doxa.daemon exists.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import random
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from . import budget as budget_mod
from . import peerledger as peerledger_mod
from . import peers as peers_mod

__all__ = [
    "DEFAULT_N",
    "MODE_SUPERVISOR",
    "MODE_SYMMETRIC",
    "ROLE_SUPERVISOR",
    "ROLE_WORKER",
    "Assignment",
    "BudgetRefused",
    "DaemonBackend",
    "FleetArgsError",
    "FleetBackend",
    "FleetRun",
    "FleetSpec",
    "MemoryPolicy",
    "ModelSlot",
    "RunReport",
    "Slot",
    "assign",
    "assign_for",
    "budget_note",
    "build_parser",
    "capacity_note",
    "default_root",
    "check_capacity",
    "check_run_budget",
    "run_fleet",
    "spec_from_args",
    "spec_from_argv",
    "supervisor_briefing",
    "worker_briefing",
]


# -- the numbers ------------------------------------------------------

DEFAULT_N = 4
"""Sessions per run unless told otherwise.

Deliberately NOT 32. Thirty-two is what the experiment needs and what the
192 GB workstation can hold; it is roughly 19 GB of resident memory, which
on a 30 GB machine with anything else open is a swap storm rather than a
fleet. A default is what someone runs by accident, so the default is a
size that cannot hurt, and the experiment's N is passed explicitly by the
person who has read :func:`capacity_note`."""

SESSION_RESIDENT_MB = 600
"""Resident megabytes to budget per session -- the daemon, its engine and
the CLI process the engine drives.

Measured, from docs/plans/emergent-organization.md's own resource table:
the live CLI process alone is 425-545 MB, and ~600 MB is the whole session
including the daemon around it. Budgeting the top of a measured range
rather than its middle is the direction that fails safe: an estimate that
is 15% high refuses a run that would have fitted, and an estimate that is
15% low lets the OOM killer choose which agents leave the experiment."""

MEMORY_HEADROOM_MB = 2048
"""Resident megabytes :func:`check_capacity` leaves for everything that is
not the fleet -- the operator's editor, the analysis, the OS page cache
that git needs. A capacity check that plans to use every byte is a
capacity check that plans for the OOM killer to arbitrate."""

SOCKET_PATH_MAX = 108
"""The AF_UNIX ``sun_path`` budget, in bytes, on Linux.

doxa.peers already truncates a session id to eight characters for exactly
this reason. A harness adds a SECOND long path component -- a per-run
runtime directory under a per-run home -- and the sum is what overflows.
Overflow is not a clean error either: the bind fails deep inside asyncio
with a message about the path, minutes into a spawn loop. So
:func:`check_socket_budget` measures it up front and refuses with the
arithmetic. See its own docstring for the components."""

SOCKET_NAME_BUDGET = 40
"""Bytes the longest socket filename this layer creates needs, beneath a
runtime directory: ``daemon-<8 hex>-<pid>.sock`` is 8 + 8 + 7 + 5 = ~28,
and the margin covers a seven-digit pid and the ``.tmp`` suffixes the
registry writes beside it."""


# -- the two shapes a run can have ------------------------------------

MODE_SYMMETRIC = "symmetric"
"""N sessions, one identical prompt, one instant, no roles. The
experiment's shape and this module's original one."""

MODE_SUPERVISOR = "supervisor"
"""One supervisor session gets the operator's prompt and distributes the
work to N briefed workers over peer messages. Ordinary software work's
shape, and the one that is NOT a measurement instrument."""

ROLE_WORKER = "worker"
"""What a slot drawn from ``--pool`` is. Also what every slot of a
SYMMETRIC run is: a symmetric run has no supervisor, so the honest
reading of its rows is "workers, nobody directing them" rather than a
third role invented to describe the absence of the second."""

ROLE_SUPERVISOR = "supervisor"
"""The one slot in a supervisor run that receives the operator's prompt.
Exactly one per run, always slot 0 -- see :func:`assign_for`."""


# -- what a run is ----------------------------------------------------


@dataclass(frozen=True)
class ModelSlot:
    """One draw in the model pool: an engine id, a model id, and how
    heavily to weight it.

    ``weight`` carries the plan's own economics: "Sampling weights the
    inexpensive vendors heavily and includes costly ones at low
    probability, so the mix stays honest without the bill scaling with N."
    A weight is a relative frequency in the draw, never a guarantee -- a
    run with N=4 and a 1-in-50 vendor will usually contain none of it, and
    that is the intended behaviour rather than a bug to smooth over."""

    engine: str
    model: "str | None" = None
    weight: float = 1.0

    def __post_init__(self) -> None:
        if not str(self.engine).strip():
            raise ValueError("a model pool entry needs an engine id")
        if self.weight <= 0:
            raise ValueError(
                f"weight must be positive, got {self.weight!r} for "
                f"{self.engine}/{self.model}"
            )

    @property
    def label(self) -> str:
        return f"{self.engine}:{self.model}" if self.model else self.engine


@dataclass(frozen=True)
class MemoryPolicy:
    """How many agents in a run run with LORE entirely off, and which.

    PER AGENT, NEVER PER FLEET, and the reason is measurement rather than
    hygiene. The experiment reads structure off the message ledger. A LORE
    store shared by every session is a second channel -- one that carries
    no records, draws no edges, and is invisible to every measure the plan
    names. Two agents that converge on a division of labour after both
    read the same belief have coordinated somewhere the graph cannot see,
    and the run reports a hierarchy that formed for reasons it cannot
    show. Making it per-agent turns the confound into a manipulable
    variable: a cell can hold memory-off agents beside memory-on ones and
    ask directly whether shared memory substitutes for messaging.

    ``off_count`` is a count and not a fraction because a fraction of 32
    rounds, and a rounding rule is a thing to get wrong silently in a
    replication. ``None`` means "every agent keeps memory" -- today's
    behaviour, unchanged, which is what a caller that has not thought
    about this gets."""

    off_count: "int | None" = None

    def resolved(self, n: int) -> int:
        if self.off_count is None:
            return 0
        return max(0, min(int(self.off_count), int(n)))


@dataclass(frozen=True)
class Assignment:
    """What one slot in the fleet was dealt: which model, and whether it
    keeps its memory. Written verbatim into the run manifest, because an
    assignment that is not recorded makes the run's primary result
    uninterpretable -- "the coordinator was slot 7" says nothing until the
    manifest says what slot 7 was running."""

    index: int
    engine: str
    model: "str | None"
    lore: bool = True
    #: :data:`ROLE_WORKER` or :data:`ROLE_SUPERVISOR`. Recorded for the
    #: same reason the model is: "the supervisor integrated nothing" and
    #: "slot 0 integrated nothing" are the same observation only if the
    #: manifest says which slot was the supervisor.
    role: str = ROLE_WORKER

    @property
    def label(self) -> str:
        return f"{self.engine}:{self.model}" if self.model else self.engine

    def to_obj(self) -> "dict[str, Any]":
        return {
            "index": self.index,
            "engine": self.engine,
            "model": self.model,
            "lore": self.lore,
            "role": self.role,
        }


def assign(
    n: int,
    pool: "list[ModelSlot] | tuple[ModelSlot, ...]",
    *,
    seed: int,
    memory: "MemoryPolicy | None" = None,
) -> "list[Assignment]":
    """Deal ``n`` slots from ``pool``, reproducibly from ``seed``.

    Randomised per run, which is the plan's own requirement and not a
    convenience: "Model is assigned to agent **randomly per run**, so role
    cannot be confounded with capability." A fixed mapping would make
    "slot 3 coordinates" and "opus coordinates" the same observation
    forever.

    Reproducible from the seed, which is the other half. A run whose
    assignment cannot be recreated cannot be re-analysed, re-run with a
    latency floor (the plan's own mitigation for its first named
    confound), or defended to a reviewer. A dedicated
    :class:`random.Random` rather than the module-global one, so a caller
    that seeded ``random`` for its own reasons neither perturbs this draw
    nor is perturbed by it.

    Memory-off agents are drawn from the SAME stream, after the models, so
    which agents lose memory is also randomised per run and also
    reproducible -- and so that a run with ``off_count=0`` produces exactly
    the model assignment it produced before memory was a variable at all."""
    if n <= 0:
        raise ValueError(f"a fleet needs at least one session, got n={n}")
    entries = list(pool)
    if not entries:
        raise ValueError("a fleet needs at least one model pool entry")
    rng = random.Random(seed)
    weights = [entry.weight for entry in entries]
    drawn = rng.choices(entries, weights=weights, k=n)
    assignments = [
        Assignment(index=i, engine=entry.engine, model=entry.model)
        for i, entry in enumerate(drawn)
    ]
    off = (memory or MemoryPolicy()).resolved(n)
    if off:
        for index in rng.sample(range(n), off):
            assignments[index] = replace(assignments[index], lore=False)
    return assignments


def assign_for(spec: "FleetSpec") -> "list[Assignment]":
    """Every slot this run has, in slot order -- the supervisor included.

    A SYMMETRIC run is :func:`assign` and nothing else, byte for byte:
    same seed, same draw, same memory sample, same indices. That equality
    is the point rather than an implementation detail, because a run
    recorded before supervisor mode existed has to replay from its
    manifest into the same assignment it had.

    A SUPERVISOR run is the same draw for the ``n`` workers, SHIFTED to
    slots 1..n, with the supervisor inserted at slot 0. Slot 0 is chosen
    rather than slot n for one reason a reader will meet: ``/fleet attach
    0`` is the line an operator types to reach the session that holds the
    task, and a number that moves with N is a number they have to look up
    first.

    The supervisor is NOT drawn from the pool and is NOT subject to
    ``--memory-off``. It is the session that has to hold the shape of the
    whole job across every worker's reply, which is precisely the
    continuity a memory-off agent does not have; and a run that silently
    dealt the coordinating session no memory would be a run whose failure
    mode is "the supervisor forgot what it had already handed out". The
    policy still applies in full to the workers, where it means what it
    always meant."""
    workers = assign(spec.n, list(spec.pool), seed=spec.seed, memory=spec.memory)
    if spec.supervisor is None:
        return workers
    head = Assignment(
        index=0,
        engine=spec.supervisor.engine,
        model=spec.supervisor.model,
        lore=True,
        role=ROLE_SUPERVISOR,
    )
    return [head] + [
        replace(w, index=w.index + 1, role=ROLE_WORKER) for w in workers
    ]


@dataclass
class FleetSpec:
    """Everything a run is, in one object that goes into the manifest.

    Deliberately a plain dataclass with no I/O: a spec can be built,
    recorded, diffed against another run's, and replayed, and none of that
    should require a machine with sessions on it."""

    prompt: str
    cwd: str
    n: int = DEFAULT_N
    pool: "tuple[ModelSlot, ...]" = ()
    seed: int = 0
    memory: MemoryPolicy = field(default_factory=MemoryPolicy)
    root: "Path | None" = None
    run_id: str = ""
    broadcast: bool = False

    #: The supervisor's engine and model, or None for a symmetric run.
    #: A :class:`ModelSlot` rather than a plain string because it is
    #: parsed by the same grammar one ``--pool`` entry is, so
    #: ``claude:opus`` means here what it means there -- and because its
    #: ``weight`` is then structurally meaningless, which is the honest
    #: shape: there is exactly one supervisor and nothing to weight.
    supervisor: "ModelSlot | None" = None

    # -- what this run may spend ---------------------------------------
    #
    # A RUN-WIDE total, not a per-session one, and that is the whole point
    # of putting it here rather than leaving each session to read its own
    # knob. Thirty-two individually reasonable ceilings multiply into one
    # unreasonable one, and the number an operator can actually reason
    # about overnight is "this run may cost fifty dollars" -- never "each
    # of thirty-two sessions may cost some amount I will now multiply in
    # my head".
    #
    # It is enforced by DIVISION: :attr:`session_budget_usd` is the share
    # each session is given through DOXA_SESSION_BUDGET_USD, and N
    # separately-bounded sessions can together spend at most the total.
    # See :func:`doxa.budget.per_session_share` for the two limitations
    # that arithmetic does not hide -- unused share is not reallocated,
    # and the one-turn overshoot is per session and therefore N-fold.

    #: Dollars for the WHOLE run, or None for no ceiling. None is refused
    #: at :meth:`FleetRun.prepare` when this run arms inbound
    #: turn-starting, unless :attr:`allow_unbudgeted` says otherwise.
    run_budget_usd: "float | None" = None

    #: The operator said, in words, that they mean to run this unbounded.
    #: Recorded in the manifest, exactly the way ``force`` is for the
    #: memory arithmetic -- and deliberately NOT the same flag: "this
    #: machine's memory numbers are wrong" and "I accept a swarm with
    #: nothing bounding its spend" are two different claims, and one
    #: ``--force`` that granted both would grant the second by accident.
    allow_unbudgeted: bool = False

    #: May an arriving peer message START a turn in this run's sessions
    #: (doxa.peers.PEER_INBOUND_TURNS_ENV)? True, because a run with it off
    #: measures nothing -- an agent cannot answer another when nobody is
    #: typing. It is a FIELD rather than a constant so that the guard in
    #: :func:`check_run_budget` has a real condition to read: a run that
    #: genuinely cannot wake itself is a run the budget guard has no claim
    #: over, and a guard whose condition is always true teaches nobody what
    #: it is actually guarding against.
    inbound_turns: bool = True

    # -- deadlines. Every one of them exists because the phase it bounds
    # has a way of never finishing, and a run that never ends is a run
    # that cannot be replicated.
    spawn_timeout_s: float = 90.0
    arm_timeout_s: float = 20.0
    dispatch_timeout_s: float = 30.0
    #: None means NO deadline, which only an INTERACTIVE supervisor run
    #: (:attr:`interactive`) may ask for: a session waiting for a human to
    #: attach and type has no phase that can be timed, and a deadline
    #: there would tear the run down while the operator was still reading
    #: the tab. Every other run keeps a number, and the default is the
    #: one it has always been.
    quiescence_timeout_s: "float | None" = 1800.0
    quiet_dwell_s: float = 20.0
    poll_interval_s: float = 2.0
    #: The BUDGET for a session's own graceful shutdown, ack through
    #: finalize (issue #58): ``DaemonBackend.stop`` -> ``EngineClient.
    #: stop`` now waits for the daemon to close its socket, which it
    #: does only once ``engine.finalize()`` -- the LORE review/index for
    #: a memory-enabled session -- and the worktree decision have both
    #: run (see ``EngineClient.stop``'s docstring). Measured against a
    #: real spawned Claude/sonnet daemon, a trivial single-turn finalize
    #: is well under a second (0.018s observed); it scales with
    #: transcript size and with whether a deriver LLM is configured, and
    #: the issue's own report (a longer, tool-using transcript) is the
    #: case that exceeded the OLD 5s ``kill_grace_s``. 60s is generous
    #: headroom above either, rather than a number sized to the ack,
    #: which is what `kill_grace_s` used to be and is why a LORE
    #: session's clean-but-slow shutdown was mistaken for a wedge. A
    #: session that has not answered `stop` inside this window is
    #: escalated to SIGTERM/SIGKILL.
    stop_timeout_s: float = 60.0
    #: The window AFTER a successful ``stop`` for the OS to actually
    #: report the pid gone -- reap latency, not finalize time, now that
    #: `stop_timeout_s` above is what waits out the finalize itself. A
    #: pid still alive at this deadline is signalled and the slot's
    #: phase moves from `stopped` to `killed`, so this must stay short:
    #: widening it re-opens the mistake `stop_timeout_s` was just fixed
    #: to avoid, just moved to a second clock.
    kill_grace_s: float = 5.0

    # Spawns run concurrently, but not all at once: thirty-two processes
    # each doing a git worktree creation and an SDK connect in the same
    # instant is a thundering herd on one disk. A window keeps the spawn
    # phase parallel without making it a load test. It does NOT affect
    # symmetry: nothing is prompted until every spawn has finished.
    spawn_concurrency: int = 8

    def __post_init__(self) -> None:
        # A SYMMETRIC run still refuses a missing prompt exactly as it
        # always has: the prompt IS the run there, and a fleet spawned
        # without one would be N sessions sitting idle at a cost. A
        # SUPERVISOR run may legitimately have none -- that is the
        # interactive shape, where the operator attaches to the
        # supervisor and types the task into it.
        if self.supervisor is None and not str(self.prompt).strip():
            raise ValueError("a fleet run needs a prompt")
        if int(self.n) <= 0:
            raise ValueError(f"a fleet needs at least one session, got n={self.n}")
        if not self.pool:
            raise ValueError(
                "a fleet run needs a model pool -- an explicit pool is what "
                "makes the assignment recordable, so there is no default"
            )
        self.run_id = str(self.run_id or "").strip() or _mint_run_id()
        self.pool = tuple(self.pool)
        # Normalised through the same parser the per-session knob uses, so
        # "5", 5, "$5" and 5.0 are one value and zero/negative is OFF
        # rather than "refuse everything" -- a mistyped ceiling must not be
        # able to produce a run in which no session may start a turn.
        self.run_budget_usd = budget_mod.usd(self.run_budget_usd)

    # -- which of the two shapes this run is ---------------------------

    @property
    def mode(self) -> str:
        """:data:`MODE_SYMMETRIC` or :data:`MODE_SUPERVISOR`.

        Derived rather than stored, so it cannot disagree with the field
        that decides it -- a spec carrying ``mode="supervisor"`` and no
        supervisor would be a manifest that lies about the run."""
        return MODE_SUPERVISOR if self.supervisor is not None else MODE_SYMMETRIC

    @property
    def session_count(self) -> int:
        """Sessions this run actually starts: ``n``, plus the supervisor.

        ``n`` stays the number of WORKERS -- ``-n 4 --supervisor
        claude:opus`` is four workers and a supervisor, not three and a
        supervisor -- because the operator is choosing how many hands the
        job gets, and silently spending one of them on the coordinator is
        the sort of arithmetic that surprises somebody at the bill.
        Everything that counts sessions (capacity, the budget division,
        teardown's evidence) counts THIS."""
        return int(self.n) + (1 if self.supervisor is not None else 0)

    @property
    def interactive(self) -> bool:
        """A supervisor run with no prompt: the operator will attach to
        the supervisor and type the task there.

        The run then has no measurable end of its own -- the supervisor
        sits idle until a human types, which is indistinguishable from a
        finished fleet to anything outside the session -- so
        :meth:`FleetRun.await_quiescence` refuses to end it on quiet. See
        that method."""
        return self.supervisor is not None and not str(self.prompt or "").strip()

    # -- where a run's state lives ------------------------------------

    @property
    def run_root(self) -> Path:
        """Everything this run writes, under one directory."""
        base = self.root if self.root is not None else default_root()
        return Path(base) / self.run_id

    @property
    def home(self) -> Path:
        """This run's ``DOXA_HOME``.

        Per RUN, which is what makes collection trivial: doxa.peerledger
        puts the ledger at ``$DOXA_HOME/peers/messages.jsonl`` and says so
        deliberately -- "One file per DOXA_HOME, which is also how a
        harness collects a run: point DOXA_HOME at a per-run directory and
        the run's ledger is the whole file, with no filtering and nothing
        to separate afterwards." Filtering a shared ledger by timestamp
        would work until two runs overlapped, and then it would work
        wrongly and silently."""
        return self.run_root / "home"

    @property
    def runtime(self) -> Path:
        """This run's ``DOXA_RUNTIME_DIR`` -- the peer registry and the
        sockets.

        Per run for a reason the ledger's reason does not cover: peer
        DISCOVERY reads the registry, so a fleet sharing the machine's
        registry would discover the operator's own editor session and
        count it as a participant. N is a measured quantity here. It has
        to be the N that was dealt."""
        return self.run_root / "rt"

    @property
    def ledger_path(self) -> Path:
        return self.home / peerledger_mod.DIR_NAME / peerledger_mod.LEDGER_NAME

    @property
    def manifest_path(self) -> Path:
        return self.run_root / "manifest.json"

    @property
    def session_budget_usd(self) -> "float | None":
        """The run's total, divided into the share ONE session is given --
        or None when the run has no total.

        Division is what turns a run-wide number into something the
        existing machinery can actually enforce: DOXA has no cross-process
        cost aggregator, and this deliberately does not invent one (that
        would be a shared file N sessions race on, which is a worse answer
        than arithmetic). N sessions each bounded at ``total / N`` can
        together spend at most ``total``, because the bounds add.
        :func:`doxa.budget.per_session_share` states the two things that
        buys and the two it does not.

        Divided by :attr:`session_count`, never by ``n``: in a supervisor
        run the supervisor spends too, and a ceiling that had not counted
        it would be a ceiling the run can exceed by one whole session's
        share."""
        if self.run_budget_usd is None:
            return None
        return budget_mod.per_session_share(
            self.run_budget_usd, self.session_count
        )

    def env_for(self, assignment: Assignment) -> "dict[str, str]":
        """The environment ONE session is spawned with.

        Built from this process's own environment rather than from
        scratch: a session needs PATH, HOME and whatever credentials the
        engine reads, and a harness that curated that list would break the
        first time a vendor added a variable. What this overrides is
        exactly the state that must be per-run (home, runtime dir) and
        per-agent (memory)."""
        env = dict(os.environ)
        env["DOXA_HOME"] = str(self.home)
        env["DOXA_RUNTIME_DIR"] = str(self.runtime)
        # The fleet talks to itself: without this the peer tools are not
        # offered and the ledger is empty, which is a run that measured
        # nothing. Set per run rather than assumed from the operator's own
        # config, so a run does not silently depend on a machine's state.
        env[peers_mod.PEER_SEND_ENV] = "1"
        # An arriving message may start a turn -- how an agent answers
        # another at all when nobody is typing. A broadcast still never
        # starts one (doxa.peers.send_message's own `kind` field): at N=32
        # one broadcast would otherwise wake the entire fleet in a single
        # step.
        if self.inbound_turns:
            env[peers_mod.PEER_INBOUND_TURNS_ENV] = "1"
        else:
            env.pop(peers_mod.PEER_INBOUND_TURNS_ENV, None)
        # This session's share of the run's ceiling. Set only when the run
        # HAS one: an unbudgeted run leaves whatever the operator's own
        # environment carries, because popping it would strip a ceiling
        # they set deliberately and the only direction this function may
        # err in is the safe one. A budgeted run OVERRIDES an inherited
        # value -- the run's own total is authoritative for the sessions
        # the run itself spawns, or the arithmetic in the manifest would
        # be describing a bound that is not the one in force.
        share = self.session_budget_usd
        if share is not None:
            env[budget_mod.SESSION_BUDGET_ENV] = repr(share)
        if not assignment.lore:
            env["DOXA_LORE"] = "0"
        else:
            env.pop("DOXA_LORE", None)
        return env


def default_root() -> Path:
    """Where runs land when a caller names no root: ``$DOXA_HOME/fleet``.

    Resolved per call, never at import, for the same reason every other
    path helper in this codebase is -- a test (and a harness) moves
    DOXA_HOME, and a constant captured at import time would not follow."""
    from . import config as config_mod

    return config_mod.doxa_home() / "fleet"


def _mint_run_id() -> str:
    """``20260918T104355-3f2a`` -- sortable, and unique even when two runs
    start inside the same second."""
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    return f"{stamp}-{uuid.uuid4().hex[:4]}"


# -- capacity, stated rather than discovered --------------------------


def capacity_note(n: int, *, available_mb: "int | None" = None) -> str:
    """One sentence of arithmetic for this N on this machine.

    Printed before a run starts and recorded in the manifest. The point is
    that an operator reads the numbers BEFORE the OOM killer does: an
    estimate in a docstring is a thing nobody reads, and an estimate on
    stderr at spawn time is a thing they read exactly when it matters."""
    need = int(n) * SESSION_RESIDENT_MB
    have = available_mb if available_mb is not None else available_memory_mb()
    if have is None:
        return (
            f"N={n} x ~{SESSION_RESIDENT_MB} MB/session = ~{need / 1024:.1f} GB "
            "resident; available memory could not be measured on this machine"
        )
    return (
        f"N={n} x ~{SESSION_RESIDENT_MB} MB/session = ~{need / 1024:.1f} GB "
        f"resident, against ~{have / 1024:.1f} GB available "
        f"(reserving {MEMORY_HEADROOM_MB / 1024:.1f} GB headroom)"
    )


def available_memory_mb() -> "int | None":
    """MemAvailable, in MB, or None where it cannot be read.

    ``MemAvailable`` rather than ``MemFree`` deliberately: free memory on a
    working machine is near zero because the page cache holds the rest, and
    budgeting against it would refuse every run. None on a platform without
    ``/proc/meminfo`` -- unknown, never a guess, and :func:`check_capacity`
    treats unknown as "do not refuse" rather than inventing a number to
    refuse against."""
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


class CapacityRefused(RuntimeError):
    """This N does not fit in this machine's memory, and the run was not
    started. Carries the arithmetic -- a refusal a reader cannot check is
    a refusal they will override blindly."""


def check_capacity(n: int, *, force: bool = False, available_mb: "int | None" = None) -> str:
    """Refuse an N that does not fit; return the note when it does.

    ``force`` is the escape hatch, and it is explicit for the reason the
    plan's own resource table implies: the 192 GB workstation genuinely
    fits N=32 and a 30 GB laptop genuinely does not, and the harness cannot
    tell which one it is on beyond what ``/proc/meminfo`` says. What it
    must not do is decide silently. A forced run records that it was
    forced."""
    note = capacity_note(n, available_mb=available_mb)
    have = available_mb if available_mb is not None else available_memory_mb()
    if have is None or force:
        return note
    if int(n) * SESSION_RESIDENT_MB + MEMORY_HEADROOM_MB > have:
        raise CapacityRefused(
            f"{note} -- refusing to start. A fleet that swaps does not "
            "measure coordination, it measures paging. Lower N, or pass "
            "force=True (--force) if this machine's numbers are wrong."
        )
    return note


class BudgetRefused(RuntimeError):
    """This run can wake its own sessions and nothing bounds what that
    costs, and the run was not started.

    Sibling of :class:`CapacityRefused` and deliberately a separate type:
    one of them says the machine cannot hold the run, the other says
    nobody has said what the run may spend. An operator who overrides one
    has not said anything about the other."""


def budget_note(spec: "FleetSpec") -> str:
    """One sentence of arithmetic for this run's ceiling, for the operator
    to read before spawning and for the manifest to keep afterwards.

    Same job :func:`capacity_note` does for memory, and the same reason:
    an estimate nobody reads is not a control. This one also names the
    engines in the pool that cannot be held to it at all."""
    if spec.run_budget_usd is None:
        return (
            "no run budget: nothing bounds what this run may spend"
            + ("" if spec.inbound_turns else " (inbound turn-starting is off)")
        )
    share = spec.session_budget_usd or 0.0
    # N here is every session the run STARTS, which in supervisor mode is
    # the workers plus the supervisor -- the number the division actually
    # used, so an operator checking the arithmetic against the bill is
    # reading the same one the sessions were given.
    note = (
        f"run budget {budget_mod.format_usd(spec.run_budget_usd)} across "
        f"N={spec.session_count} = {budget_mod.format_usd(share)} per session "
        f"({budget_mod.SESSION_BUDGET_ENV}); each session stops STARTING "
        "turns at its share, so the run spends at most the total plus one "
        "turn of overshoot per session"
    )
    blind = sorted({
        slot.engine for slot in spec.pool
        if not budget_mod.enforceable_for(slot.engine)
    })
    if blind:
        note += (
            " -- EXCEPT on " + ", ".join(blind) + ", which report no dollar "
            "figure at all, so slots dealt those engines are UNBOUNDED and "
            "their share is not enforced (doxa.vendors: DOXA will not "
            "multiply tokens by a price sheet it would have to maintain)"
        )
    return note


def check_run_budget(spec: "FleetSpec") -> str:
    """Refuse a run that arms inbound turn-starting with nothing bounding
    its spend; return the note when it is allowed.

    The condition is the honest one rather than a blanket rule. A fleet
    whose sessions can be woken by each other's messages
    (:attr:`FleetSpec.inbound_turns`, which :meth:`FleetSpec.env_for`
    turns into ``DOXA_PEER_INBOUND_TURNS`` for all N) is a swarm that can
    keep spending with nobody watching and nobody typing -- the whole
    reason the emergence experiment is worth running is also the whole
    reason it must not be launchable unbounded BY OMISSION. Forgetting a
    flag is the most likely way that happens, so forgetting it is what
    this refuses.

    ``spec.allow_unbudgeted`` is the escape hatch, and it is explicit for
    the same reason :func:`check_capacity`'s ``force`` is: an operator who
    means it should be able to say so, once, in words, and have that fact
    survive into the manifest where anyone reading the run later can see
    what was accepted."""
    note = budget_note(spec)
    if spec.run_budget_usd is not None or not spec.inbound_turns:
        return note
    if spec.allow_unbudgeted:
        return note + " -- ACCEPTED by allow_unbudgeted (--allow-unbudgeted)"
    raise BudgetRefused(
        f"this run arms inbound turn-starting for all "
        f"{spec.session_count} sessions "
        "(an arriving peer message starts a turn in an idle session, so "
        "the fleet can keep spending with nobody typing) and no run "
        "budget is set -- refusing to start. Set one: "
        "FleetSpec(run_budget_usd=<dollars>) / --run-budget <dollars>, "
        "which is divided into a per-session "
        f"{budget_mod.SESSION_BUDGET_ENV} share. Run it unbounded only by "
        "saying so: allow_unbudgeted=True / --allow-unbudgeted, which is "
        "recorded in the manifest."
    )


def check_socket_budget(runtime: "Path | str") -> None:
    """Refuse a runtime directory too deep for an AF_UNIX socket.

    The components: ``<runtime>/daemon-<8 hex>-<pid>.sock``. doxa.peers
    already truncates the session id to eight characters against this same
    108-byte ceiling; what a harness adds is a per-run directory under a
    per-run root, and the SUM is what overflows. Measured up front because
    the natural failure is not a clean one -- the bind fails inside asyncio,
    per session, minutes into a spawn loop, with a message about a path
    nobody chose by hand."""
    runtime = Path(runtime)
    used = len(str(runtime).encode("utf-8")) + 1 + SOCKET_NAME_BUDGET
    if used > SOCKET_PATH_MAX:
        raise ValueError(
            f"fleet run directory is too deep for a Unix socket: "
            f"{runtime} needs {used} bytes of the {SOCKET_PATH_MAX}-byte "
            f"AF_UNIX path budget (the directory alone is "
            f"{len(str(runtime))} characters). Use a shorter root -- "
            "root=/tmp/dx, say -- rather than a path under a long home."
        )


# -- one session's slot in the fleet ----------------------------------

PHASE_PENDING = "pending"
PHASE_SPAWNED = "spawned"
PHASE_ARMED = "armed"
PHASE_DISPATCHED = "dispatched"
PHASE_QUIET = "quiet"
PHASE_HUNG = "hung"
PHASE_FAILED = "failed"
PHASE_STOPPED = "stopped"
PHASE_KILLED = "killed"
PHASE_LEAKED = "leaked"

#: Phases from which a slot never becomes quiet, so the quiescence wait
#: must not keep waiting on it. Naming the set once is what keeps the
#: wait loop's exit condition from drifting away from the teardown's.
TERMINAL_PHASES = frozenset({PHASE_HUNG, PHASE_FAILED, PHASE_STOPPED, PHASE_KILLED})


@dataclass
class Slot:
    """One participant: what it was dealt, what happened to it, when.

    Mutable on purpose -- it is the run's own record of a session as the
    run proceeds, and it is serialised into the manifest at the end."""

    assignment: Assignment
    phase: str = PHASE_PENDING
    session_id: "str | None" = None
    socket_path: "str | None" = None
    pid: "int | None" = None
    #: Where this session is ACTUALLY working, read from the registry
    #: entry the daemon wrote -- which is its own git worktree whenever
    #: ``doxa.worktrees.enabled()`` and ``cwd`` is a repository, and both
    #: are true by default. It is the run's only record of where the work
    #: ended up: a fleet's worktrees live under the RUN's ``DOXA_HOME``,
    #: so nothing outside the run can find them by looking.
    cwd: "str | None" = None
    dispatched_at: "float | None" = None
    error: "str | None" = None
    handle: Any = None

    @property
    def index(self) -> int:
        return self.assignment.index

    @property
    def role(self) -> str:
        return self.assignment.role

    @property
    def is_supervisor(self) -> bool:
        return self.assignment.role == ROLE_SUPERVISOR

    def fail(self, phase: str, exc: BaseException) -> None:
        """Record a failure WITHOUT raising. One session failing to spawn
        is data about the run, not a reason to abandon the other thirty-one
        -- and a fleet that aborts on the first failure produces no ledger
        at all, which is strictly worse than a ledger with a hole in it
        that the manifest names."""
        self.phase = phase
        self.error = f"{type(exc).__name__}: {exc}"

    def to_obj(self) -> "dict[str, Any]":
        return {
            "index": self.index,
            "role": self.role,
            "assignment": self.assignment.to_obj(),
            "phase": self.phase,
            "session_id": self.session_id,
            # The worktree, when there is one. Beside the socket for the
            # same reason the socket is here: a reader outside this
            # process cannot derive it, because it is named after a
            # session id under a DOXA_HOME only the run knows.
            "cwd": self.cwd,
            # RECORDED because a reader outside this process has no other
            # way to reach one session of a run: the run's peer registry
            # lives under its OWN DOXA_RUNTIME_DIR, so the machine's
            # doxa.peers.read_registry() cannot see it. `/fleet attach
            # <slot>` opens an EngineClient on exactly this path.
            "socket_path": self.socket_path,
            "pid": self.pid,
            "dispatched_at": self.dispatched_at,
            "error": self.error,
        }


# -- what the two roles are told --------------------------------------
#
# COMPOSED BY THE HARNESS, DISPATCHED AS A TURN, and both halves of that
# are deliberate.
#
# Composed here rather than by a model: the briefing is the run's own
# protocol -- who reports to whom, over which channel, with which tool --
# and a protocol a participant wrote is a protocol the next participant
# did not agree to. It is also the only text in a supervisor run that the
# operator did not type, so it belongs somewhere they can read it, once,
# in the source, rather than reconstructing it from four transcripts.
#
# Dispatched as a TURN rather than injected as a system preamble, which is
# the part worth defending. DOXA hosts four engines behind one daemon and
# only one of them is Claude; a preamble would have to be plumbed through
# each vendor's own notion of a system prompt, would differ between them
# in ways nobody could test from here, and would be INVISIBLE -- not in
# the transcript, not in the ledger, not in anything an operator reading
# the run afterwards can see. A turn goes through
# :meth:`FleetBackend.dispatch`, which is the same path the operator's own
# prompt takes on every engine, and it lands in the transcript where the
# worker's reply lands. The cost is one short turn per worker, and what it
# buys is a run whose whole instruction set is readable after the fact.
#
# The house voice these follow is doxa.peers.PEER_UNTRUSTED_INTRO's and
# doxa.session_ops.SPAWN_PROVENANCE_INTRO's: name the channel in brackets,
# say what the session is and is not, and never pretend a person typed it.

FLEET_WORKER_INTRO = (
    "[DOXA FLEET -- WORKER] This session is one of several a DOXA fleet run "
    "started together, and it was not started by a person typing. One "
    "session in this run is the SUPERVISOR: it is the only one the operator "
    "prompted, and it decides how the work is divided. You are not it. This "
    "briefing was composed by the harness that spawned you, not by another "
    "agent and not by the operator -- it describes how the run is wired, and "
    "nothing in it is the task."
)

FLEET_SUPERVISOR_INTRO = (
    "[DOXA FLEET -- SUPERVISOR] This session is the supervisor of a DOXA "
    "fleet run. It is the only session in the run the operator prompted. The "
    "others are workers: already spawned, already briefed, and idle -- each "
    "one waiting for a task from you and for nothing else. Dividing the "
    "work, handing it out, collecting the answers and reporting back is your "
    "job, and nobody else in this run will do it. This briefing was composed "
    "by the harness that spawned you; the operator's own words, when there "
    "are any, are under the task marker at the end."
)

#: Separates the harness's briefing from the operator's own prompt --
#: the same marker ``doxa.daemon.SessionDaemon._initial_task_prompt``
#: puts between the spawn provenance intro and a spawned session's task,
#: because it is the same distinction and a reader should not have to
#: learn two.
TASK_MARKER = "--- task ---"


def worker_briefing(
    *,
    run_id: str,
    slot: int,
    session_id: str,
    supervisor_id: str,
    supervisor_label: str,
    cwd: str,
) -> str:
    """The one short turn a worker is dispatched before the run begins.

    It says four things and refuses to say a fifth: who this session is,
    who its supervisor is, how a task will arrive and how to answer it,
    and that there is nothing to do until one does. It does NOT describe
    the job -- the worker has not been told the operator's prompt and
    must not guess at it, because a worker that starts work it was not
    given is exactly the failure supervisor mode exists to remove.

    The closing ``ready`` is not ceremony: it is the only evidence,
    visible in the run's own transcript and phases, that the worker
    received the briefing and is listening. A worker that never answers
    it is a worker the supervisor should not be handed."""
    return "\n".join([
        FLEET_WORKER_INTRO,
        "",
        f"Run {run_id}. You are worker {slot}, session {session_id}.",
        f"Your supervisor is session {supervisor_id} ({supervisor_label}).",
        f"The run works on {cwd}; this session has its own checkout of it.",
        "",
        "How this run is wired:",
        f"- Your tasks arrive as peer messages from {supervisor_id}. An "
        "arriving message starts a turn here, so you are woken by the work "
        "rather than waiting for it.",
        "- Carry each task out IN THIS CHECKOUT. Commit what you change.",
        "- Then report back with peer_send to session "
        f"{supervisor_id}: what you did, where it is (branch, files), and "
        "anything you could not do. That message is the only way your work "
        "reaches the run -- nobody reads your transcript.",
        "- Do nothing else until a task arrives. Do not start work you were "
        "not given, and do not message the other workers.",
        "",
        "Reply now with a single line: ready",
    ])


def supervisor_briefing(
    *,
    run_id: str,
    session_id: str,
    workers: "list[tuple[int, str, str]]",
    cwd: str,
    prompt: str,
) -> str:
    """The supervisor's first turn: the roster, the protocol, the task.

    ``workers`` is ``(slot, session_id, label)`` per worker, in slot
    order, and it is spelled out rather than left to ``peer_list``
    because the supervisor has to be able to address a worker in its
    FIRST turn -- a roster it would have to go and fetch is a roster it
    may decide not to fetch.

    The task comes last, under :data:`TASK_MARKER`, so that the operator's
    own words are the final thing in the prompt and are unambiguously
    separable from the harness's. With no prompt at all the marker still
    appears and says so: a supervisor told nothing would otherwise invent
    a task, which at N workers is an expensive way to be wrong."""
    roster = [
        f"  slot {slot:>3}  session {sid}  {label}"
        for slot, sid, label in workers
    ] or ["  (none -- every worker failed to start; you are on your own)"]
    task = str(prompt or "").strip() or (
        "No task yet. The operator will attach to THIS session and type it. "
        "Wait for it: brief nobody, dispatch nothing, and do not invent work "
        "for the workers. When the task arrives, divide it and hand it out."
    )
    return "\n".join([
        FLEET_SUPERVISOR_INTRO,
        "",
        f"Run {run_id}. You are the supervisor, session {session_id}.",
        "Your workers:",
        *roster,
        "",
        f"The run works on {cwd}. Every worker has its OWN checkout of it -- "
        "a git worktree on its own branch -- so two workers never edit one "
        "file in one tree. They do share the repository's history, so say in "
        "each task which files and which branch that worker owns.",
        "",
        "How this run is wired:",
        "- Give a worker work with peer_send naming its session id. One task "
        "per message, and self-contained: a worker knows nothing about the "
        "others, about this briefing, or about the operator's words.",
        "- A worker's reply arrives here as a peer message and starts a turn, "
        "so you are woken when an answer lands instead of polling for it.",
        "- peer_list shows this run's sessions; peer_history shows the "
        "traffic you have already had.",
        "- Divide the task, dispatch it, wait for the replies, integrate "
        "them, and report the result.",
        "- Do the work yourself only when no worker fits it. A supervisor "
        "that does the job alone has spent the fleet for nothing.",
        "",
        TASK_MARKER,
        task,
    ])


# -- the injectable backend -------------------------------------------


class FleetBackend(Protocol):
    """Everything the orchestration needs a real session for, and nothing
    else.

    The whole point of this seam: the properties this module exists to
    guarantee -- nobody prompted before everybody is armed, dispatch order
    randomised, a hung session not hanging the run, teardown leaving
    nothing running -- are properties of the ORCHESTRATION, and a test that
    had to spawn thirty-two Claude sessions to check them would be a test
    nobody runs and nobody trusts. :class:`DaemonBackend` is the one
    implementation that knows doxa.daemon exists."""

    async def spawn(self, slot: Slot, spec: FleetSpec) -> None:
        """Start the session. Sets ``slot.session_id`` / ``slot.pid`` /
        ``slot.socket_path``. May raise; the caller records and continues."""
        ...

    async def arm(self, slot: Slot, spec: FleetSpec) -> None:
        """Attach to the session so a prompt can be written the instant the
        barrier lifts. Separate from :meth:`spawn` because THIS is what
        makes symmetry mechanical: connecting is the slow, variable part,
        and it all happens before anything is prompted."""
        ...

    async def dispatch(self, slot: Slot, prompt: str) -> None:
        """Write the prompt frame. Must return as soon as the daemon has
        acknowledged -- never after the turn finishes, or the last session
        would be prompted a turn late."""
        ...

    async def is_quiet(self, slot: Slot) -> bool:
        """Is this session idle right now?"""
        ...

    async def stop(self, slot: Slot) -> None:
        """Ask the session to finalize and exit."""
        ...

    async def kill(self, slot: Slot) -> bool:
        """Last resort. True when the process is gone afterwards."""
        ...


class DaemonBackend:
    """The real backend: one ``doxa.daemon`` process per slot, driven over
    its own socket by a :class:`doxa.client.EngineClient`.

    Imports are deferred into the methods rather than taken at module
    import: ``doxa.client`` pulls the daemon protocol in, and a test that
    only exercises the orchestration should not pay for it -- the same
    reason doxa.events exists at all."""

    def __init__(self) -> None:
        self._clients: "dict[int, Any]" = {}
        self._drains: "dict[int, asyncio.Task]" = {}

    async def spawn(self, slot: Slot, spec: FleetSpec) -> None:
        from .daemon import spawn_daemon

        env = spec.env_for(slot.assignment)
        session_id, socket_path = await asyncio.to_thread(
            spawn_daemon,
            cwd=spec.cwd,
            model=slot.assignment.model,
            wait_secs=spec.spawn_timeout_s,
            env=env,
            # The engine this slot was DEALT (issue #39). Until the daemon
            # took an --engine, this argument did not exist and every slot
            # ran Claude whatever the pool said -- so the manifest's engine
            # column and budget_note's "unbounded on deepseek/glm"
            # described assignments that never ran. It is the whole reason
            # a mixed pool means anything.
            #
            # A slot whose engine cannot start -- a vendor with no API key
            # raises doxa.vendors.MissingCredential inside the daemon,
            # which then exits during startup -- surfaces here as
            # spawn_daemon's RuntimeError carrying the daemon log's tail.
            # FleetRun.spawn_all records that on the slot (PHASE_FAILED,
            # with the reason in slot.error and therefore in the manifest)
            # and the run continues with the rest.
            engine=slot.assignment.engine,
            # The per-agent memory draw, delivered on the command line --
            # the channel that actually decides this session, where the
            # env var beside it is only the config-layer default. See
            # MemoryPolicy for why this is per agent and not per fleet.
            lore=slot.assignment.lore,
        )
        slot.session_id = session_id
        slot.socket_path = socket_path
        entry = _registry_entry(Path(env["DOXA_RUNTIME_DIR"]), session_id)
        slot.pid = _entry_pid(entry)
        slot.cwd = _entry_cwd(entry)

    async def arm(self, slot: Slot, spec: FleetSpec) -> None:
        from .client import EngineClient

        if not slot.socket_path:
            raise RuntimeError("no daemon socket -- the spawn did not finish")
        client = EngineClient(slot.socket_path)
        await client.start()
        self._clients[slot.index] = client
        # One drain per CLIENT, started at arm time and running for the
        # whole session -- not one per dispatched turn. See _drain for the
        # measured reason a per-turn drain was wrong.
        self._drains[slot.index] = asyncio.create_task(_drain(client))

    async def dispatch(self, slot: Slot, prompt: str) -> None:
        client = self._clients.get(slot.index)
        if client is None:
            raise RuntimeError("not armed -- nothing to dispatch to")
        # dispatch() hands the frame over and returns on the ACK, without
        # consuming the turn. Draining the turn's events is a separate,
        # background job (see arm) precisely so that the dispatch instant
        # is the write, not the first event to come back -- which would
        # make a slow model's session look like a late dispatch.
        #
        # A queued ack is a SUCCESS, not a failure: a peer message may
        # already have started a turn in this session (the fleet arms
        # peer_inbound_turns), in which case the daemon enqueues this
        # prompt behind it. It still runs, and the quiescence wait below
        # counts the queue.
        await client.dispatch(prompt)

    async def is_quiet(self, slot: Slot) -> bool:
        """Idle means the DAEMON says so -- nothing running and nothing
        queued.

        MEASURED, at N=32 with the fleet actually messaging each other:
        the first version of this asked whether the client's own turn-drain
        task had finished, and the run never quiesced. The reason is worth
        keeping: when a peer message has already started a turn, the
        daemon ENQUEUES an arriving prompt rather than running it, and the
        eventual turn's events then ride the out-of-band stream instead of
        the dispatching client's own -- so that drain task never completes,
        and a harness reading it as "still busy" waits out its entire
        deadline on a session that went idle in four seconds.

        Only the daemon can answer this at all: a turn started by an
        arriving peer message begins with no client involved. Hence
        ``running``/``queued`` in its status reply."""
        client = self._clients.get(slot.index)
        if client is None:
            return True
        status = await client.refresh_status()
        return not bool(status.get("running")) and not int(status.get("queued") or 0)

    async def stop(self, slot: Slot) -> None:
        client = self._clients.pop(slot.index, None)
        task = self._drains.pop(slot.index, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if client is not None:
            await client.stop()

    async def kill(self, slot: Slot) -> bool:
        return await asyncio.to_thread(_kill_pid, slot.pid)


async def _drain(client: Any) -> None:
    """Consume this client's events for the whole session, and throw them
    away.

    Somebody has to: ``EngineClient`` buffers events in TWO unbounded
    queues -- the turn stream and the out-of-band stream -- so a harness
    that dispatches and never reads grows both for the length of the run,
    times N. The harness's measurement is the ledger on disk, not the
    transcript, so the events themselves are genuinely not wanted; they
    still have to be taken off the queues.

    BOTH queues, and for the whole session rather than for one turn. The
    out-of-band stream is where a peer-started turn's events arrive, where
    a queued prompt's eventual turn arrives, and where every peer join and
    leave arrives -- at N=32 with the fleet messaging, that is the busier
    of the two by a wide margin."""

    async def _turns() -> None:
        while True:
            event = await client.next_turn_event()
            if event is None:
                return

    async def _oob() -> None:
        async for _event in client.peer_events():
            pass

    with contextlib.suppress(Exception):
        await asyncio.gather(_turns(), _oob())


def _registry_entry(runtime: Path, session_id: str) -> "dict[str, Any]":
    """The registry entry this session just wrote, or ``{}``.

    ONE read for the two facts the harness needs from it (the pid and the
    working directory), rather than one read each: the file is written
    once at startup and both callers want the same instant's answer, and
    two reads would be two chances to catch a rewrite.

    Read at all -- rather than taken from the spawn's return value --
    because ``spawn_daemon`` returns the session id and socket and
    nothing else. The pid it holds is the pid of the process it forked,
    which with ``start_new_session=True`` is the daemon itself but only
    incidentally, and the cwd it was given is the one the daemon then
    REPLACED with a worktree of its own (:meth:`doxa.daemon.SessionDaemon
    ._apply_worktree`, which runs before the presence entry is written).
    The entry is where the daemon states both for itself."""
    path = runtime / "registry" / f"{session_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _entry_pid(entry: "dict[str, Any]") -> "int | None":
    """The daemon's own pid -- what the teardown's last resort aims at."""
    try:
        return int(entry["pid"])
    except (KeyError, ValueError, TypeError):
        return None


def _entry_cwd(entry: "dict[str, Any]") -> "str | None":
    """Where the session is really working: its worktree, when it made
    one, and the run's ``cwd`` when it did not."""
    value = str(entry.get("cwd") or "").strip()
    return value or None


def _gone(pid: "int | None") -> bool:
    """Is this process REALLY gone -- zombies included?

    MEASURED, and it cost a whole scale run to find. ``os.kill(pid, 0)``
    reports a zombie as alive, and every session a fleet spawns is this
    process's own child: ``subprocess.Popen(..., start_new_session=True)``
    starts a new SESSION, not a new parent, and neither ``spawn_daemon``
    nor this module keeps the ``Popen`` object to wait on. So a daemon that
    accepted ``stop``, finalized and exited perfectly cleanly stays "alive"
    to the liveness check until somebody reaps it. At N=4, all four clean
    shutdowns were reported as leaks -- a harness crying wolf about the
    exact property it exists to verify, which is worse than not checking.

    Three answers, in the order that costs least: reap it if it is our
    child and has exited; ask the OS if the pid exists at all; and failing
    both, read ``/proc`` for the zombie state directly -- needed because
    another thread's ``waitpid`` may have got there first, or the process
    may be a child of something else entirely (a daemon that outlived the
    run that spawned it, and was found again from the registry)."""
    if not pid:
        return True
    pid = int(pid)
    with contextlib.suppress(ChildProcessError, OSError, ValueError):
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return True
    if not peers_mod._pid_alive(pid):
        return True
    return _is_zombie(pid)


def _is_zombie(pid: int) -> bool:
    """``/proc/<pid>/stat`` field 3 is the process state; ``Z`` is a
    process that has exited and is waiting to be reaped. Linux-only and a
    miss is silent -- a platform without /proc gets the ``os.kill`` answer,
    which is the answer this module had before.

    An OSError is NOT a zombie. This function is only ever consulted
    AFTER :func:`doxa.peers._pid_alive` has said the pid exists, so an
    unreadable ``/proc/<pid>/stat`` -- no /proc mounted, a hardened
    kernel, a permission error -- means "cannot tell", and "cannot tell"
    reported as "gone" is a live daemon the teardown walks away from.
    False leaves ``_pid_alive``'s answer standing, which is the answer
    this module had before /proc was consulted at all. A pid that has
    actually vanished raises FileNotFoundError and still reads as gone."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return True  # it vanished between the two checks: gone
    except OSError:
        return False  # cannot tell -- _pid_alive already said it exists
    return stat.rpartition(")")[2].strip().startswith("Z")


#: What ``/proc/<pid>/cmdline`` must contain for :func:`_kill_pid` to
#: signal a pid. The daemon is spawned as ``python -m doxa.daemon``
#: (:func:`doxa.daemon.spawn_daemon`), so the marker is in its argv and
#: needs no cooperation from the process.
_DAEMON_CMDLINE_MARK = "doxa.daemon"


def _is_doxa_daemon(pid: int) -> "bool | None":
    """Is this pid still a DOXA daemon? ``None`` when there is no way to
    tell on this platform.

    Linux only, by reading ``/proc/<pid>/cmdline``. A pid that has gone
    away reads as False (there is nothing to signal); a ``/proc`` that
    cannot be read at all reads as None, which :func:`_kill_pid` treats as
    "no better answer than the pid itself"."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except FileNotFoundError:
        return False
    except OSError:
        return None
    return _DAEMON_CMDLINE_MARK in raw.decode("utf-8", "replace").replace(
        "\x00", " "
    )


def _kill_pid(pid: "int | None", grace_s: float = 5.0) -> bool:
    """SIGTERM, wait, SIGKILL. True when nothing is left.

    The teardown's floor. A daemon that ignored ``stop`` is a daemon whose
    engine is wedged inside an SDK call, and no amount of further asking
    over the socket it is not reading will change that.

    **What is checked before a signal is sent.** The pid comes from a
    registry entry or a slot recorded earlier in the run, which is a claim
    about the past: a daemon can exit and the kernel reuse its number
    while this run is still holding it. On Linux the pid's
    ``/proc/<pid>/cmdline`` must still name ``doxa.daemon``
    (:func:`_is_doxa_daemon`) or nothing is signalled and this reports
    True -- the daemon the pid stood for is gone, which is what the caller
    asked about. On every other platform there is no cheap way to ask, so
    the pid IS the whole check; that is a real limit of this function and
    it is written down here rather than assumed."""
    if _gone(pid):
        return True
    if _is_doxa_daemon(int(pid)) is False:
        # Either it exited between the two checks, or this number now
        # belongs to something else. Both mean "the daemon is not here",
        # and neither is a reason to signal whatever is.
        return True
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(int(pid), signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if _gone(pid):
            return True
        time.sleep(0.05)
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(int(pid), signal.SIGKILL)
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if _gone(pid):
            return True
        time.sleep(0.05)
    return _gone(pid)


# -- the report -------------------------------------------------------


@dataclass
class RunReport:
    """What a run was and what happened to it -- the manifest's content.

    Written whether the run succeeded or not. A failed run that leaves no
    record is a failed run nobody can learn from, and at 640 sessions the
    failures are where the learning is."""

    run_id: str
    spec: FleetSpec
    slots: "list[Slot]"
    capacity: str = ""
    forced: bool = False
    #: The spend arithmetic this run started under (:func:`budget_note`).
    budget: str = ""
    #: The operator explicitly accepted a run with no ceiling. Its own
    #: field rather than a clause of ``forced``: the manifest is the only
    #: record of what a run was allowed to do, and "the memory numbers
    #: were overridden" and "the spend ceiling was waived" must be
    #: separately readable by anyone auditing a bill against a run.
    unbudgeted: bool = False
    dispatch_order: "tuple[int, ...]" = ()
    dispatch_started_at: "float | None" = None
    dispatch_spread_s: "float | None" = None
    quiesced: bool = False
    quiescence_s: "float | None" = None
    ledger_messages: int = 0
    leaked_pids: "tuple[int, ...]" = ()
    started_at: str = ""
    finished_at: str = ""
    #: This run is still going. True from :meth:`FleetRun.prepare` until
    #: :meth:`FleetRun.run` has torn down, and it is what makes the
    #: manifest readable WHILE the run happens rather than only after it:
    #: a watcher (the TUI's fleet tab) re-reads the same file the CLI
    #: prints at the end, and ``finished_at`` is left empty until there
    #: genuinely is one. A manifest found live by a LATER process is a
    #: run that died without tearing down -- the honest reading, and the
    #: one ``/fleet runs`` prints.
    live: bool = False
    #: The operator asked for teardown before quiescence (``/fleet stop``,
    #: :meth:`FleetRun.request_stop`). Its own field rather than a clause
    #: of ``quiesced``: "the fleet went quiet" and "somebody ended it" are
    #: different facts about a run and only one of them is a measurement.
    stopped: bool = False

    @property
    def ledger_path(self) -> Path:
        return self.spec.ledger_path

    def slots_in(self, phase: str) -> "list[Slot]":
        return [s for s in self.slots if s.phase == phase]

    @property
    def hung(self) -> "list[Slot]":
        return self.slots_in(PHASE_HUNG)

    @property
    def dispatched(self) -> "list[Slot]":
        return [s for s in self.slots if s.dispatched_at is not None]

    @property
    def supervisor_slot(self) -> "Slot | None":
        """The run's supervisor, or None in a symmetric run."""
        for slot in self.slots:
            if slot.is_supervisor:
                return slot
        return None

    def to_obj(self) -> "dict[str, Any]":
        supervisor = self.supervisor_slot
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            # WHICH SHAPE THIS RUN WAS, first-class and at the top level
            # rather than inferred from the presence of a supervisor key.
            # Every other field in this document means something slightly
            # different in the two modes -- dispatch_order is a shuffle in
            # one and a protocol in the other, `quiesced` is a measurement
            # in one and an impossibility in the other -- so a reader has
            # to know which before reading any of them.
            "mode": self.spec.mode,
            "interactive": self.spec.interactive,
            "supervisor": None if supervisor is None else {
                "slot": supervisor.index,
                "session_id": supervisor.session_id,
                "engine": supervisor.assignment.engine,
                "model": supervisor.assignment.model,
                "cwd": supervisor.cwd,
            },
            "spec": {
                "n": self.spec.n,
                "sessions": self.spec.session_count,
                "cwd": self.spec.cwd,
                "seed": self.spec.seed,
                "prompt": self.spec.prompt,
                "prompt_sha256": _sha256(self.spec.prompt),
                "supervisor": None if self.spec.supervisor is None else {
                    "engine": self.spec.supervisor.engine,
                    "model": self.spec.supervisor.model,
                },
                "broadcast": self.spec.broadcast,
                "inbound_turns": self.spec.inbound_turns,
                "run_budget_usd": self.spec.run_budget_usd,
                "session_budget_usd": self.spec.session_budget_usd,
                "allow_unbudgeted": self.spec.allow_unbudgeted,
                "memory_off": self.spec.memory.resolved(self.spec.n),
                "pool": [
                    {"engine": m.engine, "model": m.model, "weight": m.weight}
                    for m in self.spec.pool
                ],
                "quiescence_timeout_s": self.spec.quiescence_timeout_s,
                "home": str(self.spec.home),
                "runtime": str(self.spec.runtime),
            },
            "capacity": self.capacity,
            "forced": self.forced,
            "budget": self.budget,
            "unbudgeted": self.unbudgeted,
            "assignments": [s.assignment.to_obj() for s in self.slots],
            "dispatch_order": list(self.dispatch_order),
            "dispatch_spread_s": self.dispatch_spread_s,
            "quiesced": self.quiesced,
            "quiescence_s": self.quiescence_s,
            "live": self.live,
            "stopped": self.stopped,
            "ledger": {
                "path": str(self.ledger_path),
                "messages": self.ledger_messages,
            },
            "slots": [s.to_obj() for s in self.slots],
            "leaked_pids": list(self.leaked_pids),
        }

    def summary(self) -> str:
        phases: "dict[str, int]" = {}
        for slot in self.slots:
            phases[slot.phase] = phases.get(slot.phase, 0) + 1
        parts = ", ".join(f"{k} {v}" for k, v in sorted(phases.items()))
        spread = (
            f"{self.dispatch_spread_s * 1000:.0f} ms"
            if self.dispatch_spread_s is not None
            else "n/a"
        )
        leak = (
            f", LEAKED {len(self.leaked_pids)}" if self.leaked_pids else ""
        )
        stopped = ", STOPPED on request" if self.stopped else ""
        # The mode leads, because every number after it reads differently
        # under the other one -- and the session count is spelled with the
        # supervisor in it, since that is how many sessions ran.
        shape = (
            f"{self.spec.session_count} sessions "
            f"({self.spec.n} workers + supervisor)"
            if self.spec.supervisor is not None else f"n={self.spec.n}"
        )
        return (
            f"run {self.run_id} [{self.spec.mode}]: {shape} ({parts}); "
            f"dispatch spread {spread}; ledger "
            f"{self.ledger_messages} messages{stopped}{leak}"
        )


def _sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -- the run ----------------------------------------------------------


class FleetRun:
    """One run, phase by phase.

    Split into public phase methods rather than one ``run()`` body so that
    a test can drive a single phase and assert the property that phase
    exists to guarantee -- and so that an operator debugging a run can do
    the same from a REPL."""

    def __init__(
        self,
        spec: FleetSpec,
        backend: "FleetBackend | None" = None,
        *,
        force: bool = False,
        stop: "asyncio.Event | None" = None,
    ) -> None:
        self.spec = spec
        self.backend: FleetBackend = backend or DaemonBackend()
        self.force = bool(force)
        # AN END THE OPERATOR CAN ASK FOR, and the reason it is an Event
        # rather than a task cancellation: teardown is the phase that must
        # not be interrupted (it escalates stop -> SIGTERM -> SIGKILL and
        # then asks the OS whether the process really went), so a stop has
        # to be a value the phases READ, never an exception thrown into
        # whichever await happens to be current. `python -m doxa.fleet`
        # passes none and behaves exactly as it did; the TUI's /fleet stop
        # sets it (:meth:`request_stop`) and the run takes the same
        # teardown path the quiescence deadline takes.
        self.stop_signal = stop if stop is not None else asyncio.Event()
        self.slots = [Slot(assignment=a) for a in assign_for(spec)]
        self.report = RunReport(run_id=spec.run_id, spec=spec, slots=self.slots)

    # -- the end an operator can ask for -------------------------------

    def request_stop(self) -> None:
        """End this run at the next phase boundary, then tear down.

        Never kills anything here: what this does is make
        :meth:`stopping` true, which every phase gate in :meth:`run` and
        the quiescence wait's own loop read. Teardown then runs in the
        ``finally`` it always runs in, so a stopped run is written down
        exactly like a quiesced one -- with ``stopped`` in the manifest
        saying which it was."""
        self.report.stopped = True
        self.stop_signal.set()

    def stopping(self) -> bool:
        return self.stop_signal.is_set()

    async def _sleep_or_stop(self, seconds: float) -> bool:
        """Sleep, unless a stop arrives first. True when one did.

        The poll interval is seconds long and a stop the operator has to
        wait out is a stop that reads as a hang, so the wait is on the
        event rather than on the clock."""
        try:
            async with asyncio.timeout(seconds):
                await self.stop_signal.wait()
        except (asyncio.TimeoutError, TimeoutError):
            return False
        return True

    # -- phase 0: the ground ------------------------------------------

    def prepare(self) -> str:
        """Make the run's directories and state the arithmetic. Raises
        before anything is spawned when N does not fit or the paths are too
        deep -- both are failures that are cheap here and expensive later."""
        # Every session the run STARTS, supervisor included: the memory
        # arithmetic is about resident megabytes, and a coordinating
        # session costs the same ~600 MB as a working one.
        self.report.capacity = check_capacity(
            self.spec.session_count, force=self.force
        )
        self.report.forced = self.force
        # Before any directory is made and long before any process is: a
        # run that may not spend must not leave a half-built run root
        # behind either. Raises BudgetRefused, which is NOT what --force
        # overrides -- see check_run_budget.
        self.report.budget = check_run_budget(self.spec)
        self.report.unbudgeted = (
            self.spec.run_budget_usd is None and self.spec.allow_unbudgeted
        )
        check_socket_budget(self.spec.runtime)
        for directory in (self.spec.run_root, self.spec.home, self.spec.runtime):
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        self.report.started_at = _iso_now()
        # From here the manifest describes something that is HAPPENING.
        # write_manifest() may now be called repeatedly (the TUI's fleet
        # tab watches the file) and leaves finished_at empty until run()
        # clears this.
        self.report.live = True
        return self.report.capacity

    # -- phase 1: spawn ------------------------------------------------

    async def spawn_all(self) -> None:
        """Start every session. A slot that fails to spawn is recorded and
        left behind; the run continues with the rest."""
        gate = asyncio.Semaphore(max(1, int(self.spec.spawn_concurrency)))

        async def one(slot: Slot) -> None:
            async with gate:
                try:
                    async with asyncio.timeout(self.spec.spawn_timeout_s + 15.0):
                        await self.backend.spawn(slot, self.spec)
                    slot.phase = PHASE_SPAWNED
                except (Exception, asyncio.TimeoutError) as exc:
                    slot.fail(PHASE_FAILED, exc)

        await asyncio.gather(*(one(s) for s in self.slots))

    # -- phase 2: arm --------------------------------------------------

    async def arm_all(self) -> None:
        """Attach to every session that spawned.

        THE phase that makes the start symmetric. Connecting is slow and
        its cost varies per session; doing it here, before any prompt
        exists, is what leaves nothing between the barrier and the write
        but the write."""

        async def one(slot: Slot) -> None:
            if slot.phase != PHASE_SPAWNED:
                return
            try:
                async with asyncio.timeout(self.spec.arm_timeout_s):
                    await self.backend.arm(slot, self.spec)
                slot.phase = PHASE_ARMED
            except (Exception, asyncio.TimeoutError) as exc:
                slot.fail(PHASE_FAILED, exc)

        await asyncio.gather(*(one(s) for s in self.slots))

    # -- phase 3: the identical prompt, at one instant -----------------

    async def dispatch(self) -> None:
        """Prompt the run -- symmetrically, or through its supervisor.

        The BARRIER is common to both and is the reason this is a phase
        rather than something spawn does: this method runs only after
        :meth:`arm_all` has returned, so no session is prompted while any
        other is still being armed. What differs is what is written and
        in which order, and each shape has its own method below."""
        if self.spec.supervisor is None:
            await self._dispatch_symmetric()
        else:
            await self._dispatch_supervisor()
        stamps = [s.dispatched_at for s in self.slots if s.dispatched_at is not None]
        if stamps:
            self.report.dispatch_spread_s = max(stamps) - min(stamps)

    async def _dispatch_symmetric(self) -> None:
        """Hand every armed session the SAME prompt, released together.

        Three mechanisms, each answering a different way the start could
        stop being symmetric:

        * **The barrier.** Every dispatch task is created and parked on one
          :class:`asyncio.Event` before any of them can write. Nothing is
          prompted while anything is still being armed -- and THAT, rather
          than sub-millisecond simultaneity, is the property the experiment
          actually rests on: no participant can begin work in a world where
          another does not yet exist.
        * **The shuffle.** Task creation order is drawn from the run seed,
          so the slot that happens to go first is a different slot each
          run. Over a cell's five replications, "who was first" is noise
          instead of a constant correlated with slot index -- and slot
          index is what the model assignment is recorded against.
        * **One string.** ``self.spec.prompt``, handed to every backend
          call unmodified. No per-session formatting exists in this method,
          because a session that can be told its own index has been told
          its own position, and position is the privilege the design is
          trying not to grant.

        The spread that remains is recorded, not claimed away."""
        armed = [s for s in self.slots if s.phase == PHASE_ARMED]
        if not armed:
            return
        order = list(armed)
        random.Random(self.spec.seed ^ 0x5F1E).shuffle(order)
        self.report.dispatch_order = tuple(s.index for s in order)
        released = asyncio.Event()

        async def one(slot: Slot) -> None:
            await released.wait()
            try:
                async with asyncio.timeout(self.spec.dispatch_timeout_s):
                    await self.backend.dispatch(slot, self.spec.prompt)
                slot.dispatched_at = time.monotonic()
                slot.phase = PHASE_DISPATCHED
            except (Exception, asyncio.TimeoutError) as exc:
                slot.fail(PHASE_FAILED, exc)

        tasks = [asyncio.create_task(one(s)) for s in order]
        self.report.dispatch_started_at = time.monotonic()
        released.set()
        await asyncio.gather(*tasks)

    async def _dispatch_supervisor(self) -> None:
        """Brief every worker, then hand the supervisor the task.

        THE ORDER IS THE PROTOCOL, and it is the exact inverse of the
        symmetric shape's shuffle. Every worker is briefed and has
        acknowledged the write before the supervisor is prompted at all,
        so there is no instant at which the supervisor could dispatch a
        task to a session that has not yet been told what a task is. It
        is deterministic (workers in slot order, supervisor last) because
        a protocol whose order varies per run is a protocol whose
        failures do not reproduce -- the opposite of the shuffle's
        reason, for the opposite kind of run.

        A worker is briefed by :func:`worker_briefing` with its OWN
        session id and the supervisor's, so the two can address each
        other by name from the first turn. Nothing here formats the
        operator's prompt into a worker's text: a worker is told how the
        run is wired and nothing about the job, because the job is the
        supervisor's to divide.

        With no supervisor session to report to -- it failed to spawn or
        to arm -- NOTHING is dispatched. Briefing four workers to send
        their results to a session that does not exist would spend four
        turns to produce a run that cannot go anywhere, and the manifest
        already carries the reason on the supervisor's own slot."""
        boss = self.report.supervisor_slot
        if boss is None or boss.phase != PHASE_ARMED or not boss.session_id:
            return
        workers = [
            s for s in self.slots
            if not s.is_supervisor and s.phase == PHASE_ARMED
        ]
        self.report.dispatch_order = tuple(
            [s.index for s in workers] + [boss.index]
        )
        self.report.dispatch_started_at = time.monotonic()

        for slot in workers:
            await self._dispatch_one(slot, worker_briefing(
                run_id=self.spec.run_id,
                slot=slot.index,
                session_id=str(slot.session_id or "?"),
                supervisor_id=str(boss.session_id),
                supervisor_label=boss.assignment.label,
                cwd=self.spec.cwd,
            ))
        await self._dispatch_one(boss, supervisor_briefing(
            run_id=self.spec.run_id,
            session_id=str(boss.session_id),
            workers=[
                (s.index, str(s.session_id or "?"), s.assignment.label)
                for s in workers if s.phase == PHASE_DISPATCHED
            ],
            cwd=self.spec.cwd,
            prompt=self.spec.prompt,
        ))

    async def _dispatch_one(self, slot: Slot, prompt: str) -> None:
        """One write, with the phase and the stamp the report reads.

        Shared by the sequential supervisor path rather than by the
        symmetric one, whose task body has to stay inside its own barrier
        -- but the bookkeeping either way is identical, and two copies of
        it is how a slot ends up dispatched with no ``dispatched_at``."""
        try:
            async with asyncio.timeout(self.spec.dispatch_timeout_s):
                await self.backend.dispatch(slot, prompt)
            slot.dispatched_at = time.monotonic()
            slot.phase = PHASE_DISPATCHED
        except (Exception, asyncio.TimeoutError) as exc:
            slot.fail(PHASE_FAILED, exc)

    # -- phase 4: quiescence -------------------------------------------

    async def await_quiescence(self) -> bool:
        """Wait until every dispatched session has been idle for
        ``quiet_dwell_s``, or the deadline passes.

        THE DWELL IS NOT PADDING. A session is idle between its own turn
        and the turn an arriving peer message starts, so the first moment
        everything is idle is routinely the middle of an exchange rather
        than the end of one. Requiring the quiet to HOLD is what
        distinguishes the two, and it is the only distinction available
        from outside a model's head.

        A session that stops answering its status call is marked
        :data:`PHASE_HUNG` and stops being waited on -- never waited on
        forever, which is the failure that turns one wedged SDK call into a
        run nobody gets back. It is still torn down, still killed if it has
        to be, and still named in the manifest.

        AN INTERACTIVE RUN IS NOT ENDED BY QUIET AT ALL
        (:attr:`FleetSpec.interactive`). Its supervisor is waiting for a
        human to attach and type, which from out here is
        indistinguishable from a fleet that has finished -- and every
        session in the run goes quiet within seconds of being briefed. A
        dwell would therefore tear the run down before the operator had
        finished reading the tab, which is not a subtle failure: it is
        five spawned sessions and a worktree each, gone. So the dwell is
        skipped, the deadline is whatever the operator asked for (and
        ``None``, meaning none at all, is what they get by not asking),
        and the only things that end such a run are
        :meth:`request_stop` and every session dying."""
        interactive = self.spec.interactive
        timeout = self.spec.quiescence_timeout_s
        deadline = None if timeout is None else time.monotonic() + timeout
        quiet_since: "float | None" = None
        started = time.monotonic()
        while deadline is None or time.monotonic() < deadline:
            if self.stopping():
                # An operator's stop is NOT a quiescence: the run ends
                # here, quiesced stays False, and the slots still
                # dispatched are handed to the same teardown the deadline
                # would have handed them to.
                break
            live = [s for s in self.slots if s.phase == PHASE_DISPATCHED]
            if not live:
                # Nothing left to wait on. In a prompted run that IS
                # quiescence; in an interactive one it means every
                # session died, which is an end but not a measurement --
                # and it is also what keeps a deadline-less wait from
                # being an infinite one.
                self.report.quiesced = not interactive
                break
            busy = False
            for slot in live:
                try:
                    async with asyncio.timeout(self.spec.poll_interval_s * 2):
                        quiet = await self.backend.is_quiet(slot)
                except (Exception, asyncio.TimeoutError) as exc:
                    # Not "still busy": a session that cannot answer a
                    # status call inside two poll intervals has stopped
                    # participating, and treating that as busy is how a
                    # run waits out its whole timeout on one dead socket.
                    slot.fail(PHASE_HUNG, exc)
                    continue
                if not quiet:
                    busy = True
            if interactive:
                quiet_since = None
            elif busy:
                quiet_since = None
            elif quiet_since is None:
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since >= self.spec.quiet_dwell_s:
                self.report.quiesced = True
                break
            if await self._sleep_or_stop(self.spec.poll_interval_s):
                break
        else:
            # Deadline. Every session still dispatched is hung by
            # definition: the run asked for an end and did not get one.
            for slot in self.slots:
                if slot.phase == PHASE_DISPATCHED:
                    slot.phase = PHASE_HUNG
                    slot.error = slot.error or (
                        f"still running at the {float(timeout or 0.0):.0f}s "
                        "quiescence deadline"
                    )
        for slot in self.slots:
            if slot.phase == PHASE_DISPATCHED:
                slot.phase = PHASE_QUIET
        self.report.quiescence_s = time.monotonic() - started
        return self.report.quiesced

    # -- phase 5: teardown ---------------------------------------------

    async def teardown(self) -> "list[int]":
        """End every session, escalating, and report what would not die.

        Three steps, and the third is not optional. ``stop`` is the polite
        one and runs the session's own finalize. A session that does not
        answer it inside ``stop_timeout_s`` gets SIGTERM and then SIGKILL,
        because a wedged daemon holds ~600 MB and a socket, and 640
        sessions' worth of those is how a machine stops being usable
        between runs.

        Returns the pids that survived all of it -- normally empty, and
        LOUD when it is not: "teardown leaves nothing running" is a
        property this returns evidence for rather than assumes."""

        async def one(slot: Slot) -> None:
            if slot.phase in (PHASE_PENDING, PHASE_FAILED) and slot.pid is None:
                return
            try:
                async with asyncio.timeout(self.spec.stop_timeout_s):
                    await self.backend.stop(slot)
                slot.phase = PHASE_STOPPED
            except (Exception, asyncio.TimeoutError) as exc:
                slot.error = slot.error or f"{type(exc).__name__}: {exc}"
                try:
                    gone = await self.backend.kill(slot)
                except Exception as kill_exc:
                    slot.error = f"{slot.error}; kill failed: {kill_exc}"
                    gone = False
                slot.phase = PHASE_KILLED if gone else PHASE_LEAKED

        await asyncio.gather(*(one(s) for s in self.slots))
        # Second pass: a slot that reported a clean stop but whose process
        # is still there is a leak too. Asked separately because "the stop
        # call returned" and "the process is gone" are different claims and
        # only the second one is the property.
        #
        # With a GRACE WINDOW first, measured rather than assumed -- but a
        # SHORT one now (issue #58). `one()`'s own `backend.stop()` call,
        # above, already waited out the daemon's finalize (the LORE review,
        # the worktree decision, the SDK client's __aexit__): DaemonBackend
        # .stop -> EngineClient.stop does not return until the daemon has
        # actually closed the connection, which it does only after that
        # work is done (see EngineClient.stop's docstring). What is left
        # for THIS window to cover is the reap tail between "the socket
        # closed" and "the OS agrees the pid is gone" -- process teardown,
        # not finalize -- which is why kill_grace_s could stay small while
        # stop_timeout_s (the real finalize budget now) is generous. Before
        # that fix, a liveness check taken the same tick as the `stop` ack
        # called every clean-but-still-finalizing shutdown a leak (observed
        # at N=4: all four with no grace window at all), which is the
        # smaller version of the exact defect issue #58 reports at LORE
        # timescales -- a grace window sized for the ack, not the finalize
        # it precedes.
        deadline = time.monotonic() + max(self.spec.kill_grace_s, 1.0)
        pending = [s for s in self.slots if s.pid and s.phase == PHASE_STOPPED]
        while pending and time.monotonic() < deadline:
            pending = [s for s in pending if not _gone(s.pid)]
            if pending:
                await asyncio.sleep(0.1)
        leaked: "list[int]" = []
        for slot in self.slots:
            if slot.pid and not _gone(slot.pid):
                with contextlib.suppress(Exception):
                    if await self.backend.kill(slot):
                        slot.phase = PHASE_KILLED
                        continue
                slot.phase = PHASE_LEAKED
                leaked.append(int(slot.pid))
        self.report.leaked_pids = tuple(leaked)
        return leaked

    # -- phase 6: collect ----------------------------------------------

    def collect(self) -> "list[Any]":
        """The run's ledger, whole.

        No filtering, and that is the payoff of a per-run DOXA_HOME: the
        file IS the run. A shared ledger would need a time window, and a
        time window is wrong the first time two runs overlap -- silently,
        in the direction of attributing one run's messages to another."""
        ledger = peerledger_mod.PeerLedger(path=self.spec.ledger_path)
        messages = ledger.snapshot()
        self.report.ledger_messages = len(messages)
        return messages

    def write_manifest(self) -> Path:
        """Record the run. Written LAST but never skipped -- including
        after a failure, because the assignment and the phases are the only
        interpretation a half-run ever gets.

        Also callable WHILE the run happens, which is how a watcher outside
        this process sees a run it is not running: the TUI's fleet tab
        reads this file and the run's ledger on a timer and never touches
        a :class:`FleetRun` object. ``finished_at`` is stamped only once
        :attr:`RunReport.live` is false, so a mid-run write cannot leave a
        finishing time on a run that has not finished."""
        if not self.report.live:
            self.report.finished_at = _iso_now()
        path = self.spec.manifest_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.report.to_obj(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.chmod(path, 0o600)
        return path

    # -- the whole thing ------------------------------------------------

    async def run(self) -> RunReport:
        """Every phase, in order, with teardown and the manifest guaranteed.

        The ``finally`` is the contract: whatever goes wrong in the middle,
        the sessions are ended and the run is written down. A harness that
        can leave thirty-two daemons behind on an exception is a harness
        that costs someone an afternoon the first time it throws."""
        self.prepare()
        try:
            await self.spawn_all()
            # A stop asked for mid-spawn skips the phases that would
            # follow rather than cancelling the one in flight: spawn and
            # arm are the two phases that CREATE processes, and a
            # half-created session is exactly what the teardown below has
            # the hardest time finding. So the run finishes making what it
            # started, prompts nobody, and tears the lot down.
            if not self.stopping():
                await self.arm_all()
            if not self.stopping():
                await self.dispatch()
            if not self.stopping():
                await self.await_quiescence()
        finally:
            self.report.live = False
            with contextlib.suppress(Exception):
                await self.teardown()
            with contextlib.suppress(Exception):
                self.collect()
            with contextlib.suppress(Exception):
                self.write_manifest()
        return self.report


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


async def run_fleet(
    spec: FleetSpec,
    backend: "FleetBackend | None" = None,
    *,
    force: bool = False,
    stop: "asyncio.Event | None" = None,
) -> RunReport:
    """One run, start to finish. The function a script calls."""
    return await FleetRun(spec, backend, force=force, stop=stop).run()


# -- the command line -------------------------------------------------


def _parse_pool(spec: str) -> "tuple[ModelSlot, ...]":
    """``claude:sonnet@8,claude:opus@1,deepseek:deepseek-chat@4``.

    Weight after ``@``, defaulting to 1. No default POOL exists anywhere:
    the assignment is the run's primary covariate, and a covariate nobody
    chose is a covariate nobody can defend."""
    out: "list[ModelSlot]" = []
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        body, _, weight = entry.partition("@")
        engine, _, model = body.partition(":")
        out.append(
            ModelSlot(
                engine=engine.strip(),
                model=model.strip() or None,
                weight=float(weight) if weight.strip() else 1.0,
            )
        )
    return tuple(out)


class FleetArgsError(ValueError):
    """A ``--flag`` that does not parse, or a missing required one.

    Exists because :func:`build_parser` is read by TWO callers with
    opposite failure modes: ``python -m doxa.fleet`` may print to stderr
    and exit 2, and the TUI's ``/fleet start`` may do neither -- a
    ``SystemExit`` raised inside a Textual worker takes the app down, and
    argparse's own message goes to a stderr nobody is looking at behind a
    full-screen terminal app. So the parser raises this instead of
    exiting, and each caller decides what that means: :func:`main` prints
    and returns 2 (argparse's own code, unchanged), the command mounts the
    text as a block."""


def _parse_supervisor(spec: str) -> ModelSlot:
    """``claude:opus`` -- ONE pool entry's grammar, for the one slot that
    is not drawn from the pool.

    Parsed by :func:`_parse_pool` rather than by a second reader, so
    ``--supervisor`` and ``--pool`` cannot disagree about what a colon
    means. Two entries are refused rather than silently reduced to the
    first: a run has exactly one supervisor, and a comma there is an
    operator who believes otherwise."""
    entries = _parse_pool(spec)
    if len(entries) != 1:
        raise FleetArgsError(
            f"--supervisor takes ONE engine[:model], got {spec!r} -- a run "
            "has exactly one supervisor. Use --pool for the workers."
        )
    return entries[0]


def build_parser() -> "argparse.ArgumentParser":
    """THE fleet argument grammar. One parser, both front ends.

    ``python -m doxa.fleet`` and the TUI's ``/fleet start`` build the same
    :class:`FleetSpec` from the same flags because there is only one place
    the flags are declared -- the drift this exists to prevent is the one
    where a flag lands on the command line and the TUI silently ignores
    it, or the two disagree about a default and two runs that look
    identical are not.

    ``--cwd`` has no default here on purpose: the CLI's is the process's
    working directory and the TUI's is the SESSION's repo, and that is the
    one thing the two front ends legitimately differ on. It is
    :func:`spec_from_args`' argument rather than a parser default."""
    parser = argparse.ArgumentParser(
        prog="doxa-fleet",
        description=(
            "Spawn N DOXA sessions, hand them one identical prompt at one "
            "instant, wait for quiescence, collect the ledger, tear down "
            "(docs/plans/emergent-organization.md)."
        ),
    )
    # Bound method shadowed on the INSTANCE rather than a subclass, which
    # keeps this a plain ArgumentParser for anything that reflects on it:
    # argparse calls ``self.error(message)`` for every parse failure it
    # has, including a missing required argument, so this is the single
    # interception point.
    def _refuse(message: str) -> "None":
        raise FleetArgsError(message)

    parser.error = _refuse  # type: ignore[method-assign]
    parser.add_argument("--prompt", default=None,
                        help="the task text. In a symmetric run it is "
                             "byte-identical for every session and never "
                             "formatted per session: a number is a position "
                             "and a position is a privilege. In a "
                             "--supervisor run it goes to the supervisor "
                             "ALONE, and may be omitted -- then the "
                             "operator attaches to the supervisor and types "
                             "it there")
    parser.add_argument("--prompt-file", default=None,
                        help="read the prompt from this file instead. One "
                             "of --prompt and --prompt-file is required "
                             "unless --supervisor is given; --prompt-file "
                             "wins when both are given")
    parser.add_argument("-n", type=int, default=DEFAULT_N,
                        help=f"sessions (default %(default)s). The "
                             f"experiment wants 32, which is about "
                             f"{32 * SESSION_RESIDENT_MB / 1024:.0f} GB "
                             "resident -- see --dry-run for the arithmetic "
                             "on THIS machine")
    parser.add_argument("--pool", required=True,
                        help="engine:model@weight, comma-separated, e.g. "
                             "'claude:sonnet@8,claude:opus@1'. Required: "
                             "the model assignment is the run's primary "
                             "covariate and there is no default for it")
    parser.add_argument("--supervisor", default=None,
                        help="ONE engine[:model], same grammar as a --pool "
                             "entry (e.g. claude:opus). Makes this a "
                             "SUPERVISOR run: n workers from --pool, plus a "
                             "supervisor at slot 0 which is the only session "
                             "the prompt goes to. It briefs the workers over "
                             "peer messages and integrates their replies. "
                             "Without it the run is symmetric -- every "
                             "session gets the identical prompt at one "
                             "instant, which is what the experiment needs "
                             "and what ordinary work does not")
    parser.add_argument("--seed", type=int, default=0,
                        help="the run seed. The model assignment and the "
                             "dispatch order are both drawn from it, so a "
                             "run reproduces from its manifest")
    parser.add_argument("--cwd", default=None, help="repo the fleet works in")
    parser.add_argument("--root", default=None,
                        help="where run directories go (default "
                             "$DOXA_HOME/fleet). Keep it SHORT -- a Unix "
                             "socket lives under it and AF_UNIX gives 108 "
                             "bytes")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--memory-off", type=int, default=0,
                        help="how many agents run with memory off "
                             "(doxa.daemon --no-lore). A shared LORE store "
                             "is a coordination channel the message ledger "
                             "cannot see, so this is a variable rather "
                             "than a switch")
    # DEFAULT None so that "the operator asked for 1800" and "the
    # operator asked for nothing" are distinguishable here -- they are
    # the same number in every run but one, and in that one (an
    # interactive supervisor run) the difference decides whether the run
    # ends on a clock at all. spec_from_args resolves it.
    parser.add_argument("--quiescence-timeout", type=float, default=None,
                        help="seconds before a run that will not go quiet "
                             "is torn down (default 1800). An interactive "
                             "--supervisor run -- one with no --prompt -- "
                             "has NO deadline unless this names one, "
                             "because its supervisor is waiting for you to "
                             "type")
    parser.add_argument("--quiet-dwell", type=float, default=20.0)
    parser.add_argument("--run-budget", type=float, default=None,
                        help="dollars for the WHOLE run, divided into a "
                             "per-session ceiling (run-budget/N) each "
                             "session stops starting turns at. Required "
                             "while inbound turn-starting is armed, which "
                             "it is by default -- see --allow-unbudgeted. "
                             "Not enforceable on engines that report no "
                             "cost (codex, deepseek, glm)")
    parser.add_argument("--allow-unbudgeted", action="store_true",
                        help="start a run that can wake its own sessions "
                             "with NOTHING bounding its spend. Recorded in "
                             "the manifest. Separate from --force on "
                             "purpose: overriding the memory arithmetic "
                             "says nothing about accepting an unbounded "
                             "bill")
    parser.add_argument("--force", action="store_true",
                        help="start even when the memory arithmetic says N "
                             "does not fit. Recorded in the manifest")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the capacity arithmetic and the model "
                             "assignment this seed would deal, and spawn "
                             "nothing")
    return parser


def spec_from_args(args: "argparse.Namespace", *, cwd: str) -> FleetSpec:
    """The parsed flags as the run they describe.

    ``cwd`` is the fallback for ``--cwd`` and is the caller's to supply:
    the shell's answer is ``os.getcwd()``, the TUI's is the session's own
    repository. Everything else comes from the one parser above, so the
    two front ends cannot deal a different fleet from the same words."""
    prompt = args.prompt
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    supervisor = (
        _parse_supervisor(args.supervisor)
        if getattr(args, "supervisor", None) else None
    )
    # A prompt is required in a SYMMETRIC run and optional in a supervisor
    # one -- and the refusal here is the same words it has always been,
    # because a symmetric run's behaviour is unchanged in every particular.
    if supervisor is None and not str(prompt or "").strip():
        raise FleetArgsError(
            "a fleet run needs a prompt: --prompt \"...\" or --prompt-file PATH"
        )
    # The deadline, resolved from "asked for" to "in force". An
    # interactive supervisor run that named no timeout gets None -- no
    # deadline at all -- because its end is a human saying so. Every
    # other run gets the number it has always had.
    timeout = args.quiescence_timeout
    if timeout is None:
        interactive = supervisor is not None and not str(prompt or "").strip()
        timeout = None if interactive else 1800.0
    return FleetSpec(
        prompt=prompt or "",
        cwd=args.cwd or cwd,
        n=args.n,
        pool=_parse_pool(args.pool),
        supervisor=supervisor,
        seed=args.seed,
        memory=MemoryPolicy(off_count=args.memory_off or None),
        root=Path(args.root) if args.root else None,
        run_id=args.run_id or "",
        run_budget_usd=args.run_budget,
        allow_unbudgeted=args.allow_unbudgeted,
        quiescence_timeout_s=timeout,
        quiet_dwell_s=args.quiet_dwell,
    )


def spec_from_argv(
    argv: "list[str]", *, cwd: str
) -> "tuple[FleetSpec, argparse.Namespace]":
    """``build_parser`` + ``spec_from_args`` in one call -- what a caller
    holding a list of words (a shell's ``sys.argv``, a ``/fleet start``
    line through ``shlex.split``) wants. Raises :class:`FleetArgsError`
    and never exits."""
    args = build_parser().parse_args(argv)
    return spec_from_args(args, cwd=cwd), args


def main(argv: "list[str] | None" = None) -> int:
    """``python -m doxa.fleet`` / ``doxa-fleet`` -- one run, from the shell.

    Prints the capacity arithmetic BEFORE spawning anything, because that
    is the moment an operator can still change their mind about N, and
    prints the manifest path after, because the manifest is the run."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        spec = spec_from_args(args, cwd=os.getcwd())
    except FleetArgsError as exc:
        # argparse's own shape, because that is what a shell and a wrapper
        # script already expect from this program: usage on stderr, exit 2.
        parser.print_usage(sys.stderr)
        print(f"{parser.prog}: error: {exc}", file=sys.stderr)
        return 2

    # AN INTERACTIVE SUPERVISOR RUN IS NOT A SHELL SHAPE, and this is
    # the honest refusal rather than a run that starts and then cannot be
    # used. Such a run's whole premise is that the operator ATTACHES to
    # the supervisor and types the task into it; this process has no way
    # to do that -- it is about to block inside asyncio.run() for the
    # length of the run -- so what it would produce is a supervisor
    # sitting idle, a fleet of briefed workers, and a bill. The TUI's
    # /fleet start can attach, so that is where this points.
    if spec.interactive:
        print(
            "doxa-fleet: --supervisor with no --prompt starts a run whose "
            "supervisor waits for you to attach and type -- and this "
            "process cannot attach to it. Start it from the TUI instead: "
            "/fleet start --supervisor <engine[:model]> --pool <...> -n <n>, "
            "then /fleet attach 0. Or give this run a --prompt.",
            file=sys.stderr,
        )
        return 2

    print(capacity_note(spec.session_count))
    # Printed beside the memory arithmetic and BEFORE --dry-run returns:
    # the two questions an operator has to answer before spawning are
    # "does it fit" and "what may it cost", and a dry run that answered
    # only the first would be the wrong half.
    print(budget_note(spec))
    if args.dry_run:
        for a in assign_for(spec):
            print(
                f"  slot {a.index:>3}  {a.role:<10}  {a.label:<28} "
                f"memory={'on' if a.lore else 'OFF'}"
            )
        return 0

    try:
        report = asyncio.run(run_fleet(spec, force=args.force))
    except CapacityRefused as exc:
        print(str(exc))
        return 2
    except BudgetRefused as exc:
        # Its own exit code, not shared with the capacity refusal: a
        # wrapper script that retries on 2 by lowering N would otherwise
        # "retry" its way past a missing budget forever.
        print(str(exc))
        return 3
    print(report.summary())
    print(f"manifest {spec.manifest_path}")
    print(f"ledger   {spec.ledger_path}")
    return 0 if not report.leaked_pids else 1


if __name__ == "__main__":
    raise SystemExit(main())
