# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.fleet -- the harness docs/plans/emergent-organization.md needs.

Every test here is named for the failure it catches, and each one is a
property the EXPERIMENT rests on rather than a property the code happens
to have:

* the symmetric start -- N sessions receive one identical prompt with no
  ordering advantage. This is the plan's whole methodological point: "If a
  coordinator appears out of that, it appeared through interaction."
* a hung session does not hang the run, and teardown leaves nothing
  running -- at 640 agent-sessions across 20 runs, a harness that can wedge
  is a harness that costs the study a day per incident.
* a run's ledger contains only its own run -- the per-DOXA_HOME ledger is
  what makes collection a file copy instead of a timestamp filter, and a
  timestamp filter is wrong the first time two runs overlap.
* the model assignment is recorded and reproducible from its seed --
  without it "slot 7 coordinated" is uninterpretable.
* memory is per agent, not per fleet -- a shared LORE store is a
  communication channel the ledger cannot see.

The backend is injected (:class:`doxa.fleet.FleetBackend`), so none of this
spawns a process or spends a token. That is deliberate and not a
compromise: the properties above are properties of the ORCHESTRATION, and a
test that needed thirty-two live sessions to check them would be a test
nobody runs.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import time
import shutil
import tempfile

import pytest

from doxa import fleet as fleet_mod
from doxa import peerledger as peerledger_mod


POOL = (
    fleet_mod.ModelSlot(engine="claude", model="sonnet", weight=8),
    fleet_mod.ModelSlot(engine="claude", model="opus", weight=1),
    fleet_mod.ModelSlot(engine="deepseek", model="deepseek-chat", weight=4),
)

PROMPT = "Rename every occurrence of `foo` to `bar`. The suite is the oracle."


_RUN_IDS = itertools.count()


@pytest.fixture
def short_root():
    """A run root SHORT enough for a Unix socket to live under it.

    ``tmp_path`` under pytest is ~60 characters before a run id is appended,
    and a fleet run puts an AF_UNIX socket beneath that -- see
    :func:`doxa.fleet.check_socket_budget`, and
    ``test_a_run_root_too_deep_for_a_unix_socket_is_refused_up_front``,
    which is the test that owns that failure. Every OTHER test needs a path
    that does not trip it, or a path-length refusal would masquerade as an
    orchestration failure. This is also exactly the workaround an operator
    has to apply on a real machine, so the suite runs the documented shape
    rather than a special one."""
    root = tempfile.mkdtemp(prefix="dxf", dir="/tmp")
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _spec(short_root, **kw):
    kw.setdefault("prompt", PROMPT)
    kw.setdefault("cwd", short_root)
    kw.setdefault("pool", POOL)
    kw.setdefault("root", short_root)
    kw.setdefault("run_id", f"r{next(_RUN_IDS)}")
    kw.setdefault("n", 6)
    kw.setdefault("seed", 1234)
    # A run that arms inbound turn-starting -- which every run here does --
    # must say what it may spend before prepare() will start it (see
    # doxa.fleet.check_run_budget, and tests/test_budget.py, which owns
    # that refusal). Every test in THIS file is about orchestration, so
    # each gets an ordinary budget rather than repeating the flag; the one
    # test that cares sets its own.
    kw.setdefault("run_budget_usd", 10.0)
    # Nothing here waits on a real model, so the deadlines are short; a
    # test that takes the timeout path should take it in milliseconds.
    kw.setdefault("quiescence_timeout_s", 2.0)
    kw.setdefault("quiet_dwell_s", 0.0)
    kw.setdefault("poll_interval_s", 0.01)
    kw.setdefault("stop_timeout_s", 0.2)
    return fleet_mod.FleetSpec(**kw)


class FakeBackend:
    """A backend that records what happened and when, and can be told to
    misbehave in exactly the ways a real fleet misbehaves."""

    def __init__(
        self,
        *,
        hang_quiet: "set[int] | None" = None,
        hang_stop: "set[int] | None" = None,
        unkillable: "set[int] | None" = None,
        fail_spawn: "set[int] | None" = None,
        arm_delay: float = 0.0,
    ) -> None:
        self.log: "list[tuple[str, int]]" = []
        self.prompts: "dict[int, str]" = {}
        self.spawn_env: "dict[int, dict[str, str]]" = {}
        self.alive: "set[int]" = set()
        self.hang_quiet = hang_quiet or set()
        self.hang_stop = hang_stop or set()
        self.unkillable = unkillable or set()
        self.fail_spawn = fail_spawn or set()
        self.arm_delay = arm_delay

    async def spawn(self, slot, spec) -> None:
        if slot.index in self.fail_spawn:
            raise RuntimeError("daemon exited during startup")
        self.spawn_env[slot.index] = spec.env_for(slot.assignment)
        slot.session_id = f"sess-{slot.index:04d}"
        slot.pid = 100_000 + slot.index
        slot.socket_path = f"/tmp/fake-{slot.index}.sock"
        self.alive.add(slot.index)
        self.log.append(("spawn", slot.index))

    async def arm(self, slot, spec) -> None:
        # Armed at DIFFERENT speeds on purpose: the property under test is
        # that no prompt is written while any of this is still happening.
        if self.arm_delay:
            await asyncio.sleep(self.arm_delay * (1 + slot.index))
        self.log.append(("arm", slot.index))

    async def dispatch(self, slot, prompt) -> None:
        self.prompts[slot.index] = prompt
        self.log.append(("dispatch", slot.index))

    async def is_quiet(self, slot) -> bool:
        if slot.index in self.hang_quiet:
            await asyncio.sleep(3600)  # never answers
        return True

    async def stop(self, slot) -> None:
        if slot.index in self.hang_stop:
            await asyncio.sleep(3600)
        self.alive.discard(slot.index)
        self.log.append(("stop", slot.index))

    async def kill(self, slot) -> bool:
        self.log.append(("kill", slot.index))
        if slot.index in self.unkillable:
            return False
        self.alive.discard(slot.index)
        return True


@pytest.fixture(autouse=True)
def _no_real_pids(monkeypatch):
    """The teardown's second pass asks the OS whether a pid is still alive.

    Fake slots carry invented pids (100_000 + index), and on a busy machine
    one of those can be a REAL unrelated process -- at which point the
    teardown test would try to kill it. Point the liveness probe at the
    fake backend's own bookkeeping instead."""
    import doxa.peers as peers_mod

    monkeypatch.setattr(peers_mod, "_pid_alive", lambda pid: False)


# =======================================================================
# The symmetric start -- the property the experiment rests on
# =======================================================================


async def test_no_session_is_prompted_before_every_session_is_armed(short_root):
    """The failure this catches: a harness that spawns-and-prompts in a
    loop, so session 0 is working while session 31 does not yet exist.

    That is the asymmetry docs/plans/emergent-organization.md rejects in
    its second section -- the informed agent coordinates by default, and
    what the run then measures is the decay of an initial asymmetry rather
    than emergence. The assertion is structural rather than temporal: in
    the whole event log, no `dispatch` may precede any `arm`."""
    backend = FakeBackend(arm_delay=0.005)
    run = fleet_mod.FleetRun(_spec(short_root), backend, force=True)
    run.prepare()
    await run.spawn_all()
    await run.arm_all()
    await run.dispatch()

    first_dispatch = min(i for i, (kind, _) in enumerate(backend.log) if kind == "dispatch")
    last_arm = max(i for i, (kind, _) in enumerate(backend.log) if kind == "arm")
    assert last_arm < first_dispatch, (
        "a session was prompted while another was still being armed: "
        f"{backend.log}"
    )
    assert len(backend.prompts) == 6


async def test_every_session_receives_the_byte_identical_prompt(short_root):
    """The failure this catches: per-session prompt formatting -- "you are
    agent 7 of 32" -- which hands a participant its own position, and a
    position is the privilege the design refuses to grant."""
    backend = FakeBackend()
    run = fleet_mod.FleetRun(_spec(short_root), backend, force=True)
    run.prepare()
    await run.spawn_all()
    await run.arm_all()
    await run.dispatch()

    assert set(backend.prompts) == set(range(6))
    assert set(backend.prompts.values()) == {PROMPT}


async def test_dispatch_order_is_drawn_from_the_seed_not_the_slot_index(short_root):
    """The failure this catches: slot 0 going first in every run, so "who
    spoke first" is a constant correlated with the slot index the model
    assignment is recorded against -- an ordering advantage that survives
    averaging over the five replications of a cell.

    Two runs, two seeds, same everything else: the orders must differ, and
    neither may be the identity permutation."""
    orders = []
    for seed in (1, 2, 3, 4):
        backend = FakeBackend()
        run = fleet_mod.FleetRun(_spec(short_root, seed=seed, n=8), backend, force=True)
        run.prepare()
        await run.spawn_all()
        await run.arm_all()
        await run.dispatch()
        orders.append(run.report.dispatch_order)

    assert len(set(orders)) > 1, f"every seed produced the same order: {orders}"
    assert all(sorted(o) == list(range(8)) for o in orders), orders
    assert tuple(range(8)) not in orders, (
        "dispatch ran in slot order -- the shuffle is not doing anything"
    )


async def test_the_dispatch_spread_is_measured_and_recorded(short_root):
    """A paper may claim "simultaneous" only if the harness can say how
    simultaneous. The manifest records the observed width of the dispatch
    window rather than asserting the ideal."""
    backend = FakeBackend()
    run = fleet_mod.FleetRun(_spec(short_root), backend, force=True)
    run.prepare()
    await run.spawn_all()
    await run.arm_all()
    await run.dispatch()

    assert run.report.dispatch_spread_s is not None
    assert run.report.dispatch_spread_s >= 0.0
    assert run.report.to_obj()["dispatch_spread_s"] == run.report.dispatch_spread_s


# =======================================================================
# Nothing hangs, and nothing is left behind
# =======================================================================


async def test_a_hung_session_does_not_hang_the_run(short_root):
    """The failure this catches: one session whose status call never
    returns, holding the quiescence wait open until somebody notices --
    which, on an unattended 20-run sweep, is the next morning.

    The hung slot is marked, dropped from the wait, and the run finishes.
    The other five still reach quiet."""
    backend = FakeBackend(hang_quiet={2})
    spec = _spec(short_root, quiescence_timeout_s=5.0)
    run = fleet_mod.FleetRun(spec, backend, force=True)
    run.prepare()
    await run.spawn_all()
    await run.arm_all()
    await run.dispatch()

    quiesced = await asyncio.wait_for(run.await_quiescence(), timeout=5.0)

    assert quiesced is True
    hung = [s.index for s in run.slots if s.phase == fleet_mod.PHASE_HUNG]
    assert hung == [2]
    assert run.slots[2].error, "a hung session must say why it was dropped"
    assert [s.index for s in run.slots if s.phase == fleet_mod.PHASE_QUIET] == [
        0, 1, 3, 4, 5
    ]


async def test_a_run_that_never_goes_quiet_still_ends_at_its_deadline(short_root):
    """The other half: every session busy forever. The deadline is the only
    thing that ends this, so the deadline has to end it."""

    class NeverQuiet(FakeBackend):
        async def is_quiet(self, slot) -> bool:
            return False

    backend = NeverQuiet()
    spec = _spec(short_root, quiescence_timeout_s=0.3)
    run = fleet_mod.FleetRun(spec, backend, force=True)
    run.prepare()
    await run.spawn_all()
    await run.arm_all()
    await run.dispatch()

    quiesced = await asyncio.wait_for(run.await_quiescence(), timeout=5.0)

    assert quiesced is False
    assert all(s.phase == fleet_mod.PHASE_HUNG for s in run.slots)
    assert all("quiescence deadline" in (s.error or "") for s in run.slots)


async def test_teardown_leaves_nothing_running(short_root):
    """The failure this catches: ~600 MB per wedged daemon, times the ones
    that ignored `stop`, accumulating across a sweep until the machine is
    unusable between runs.

    A session that will not stop is SIGTERMed and then SIGKILLed, and the
    run says so."""
    backend = FakeBackend(hang_stop={1, 4})
    run = fleet_mod.FleetRun(_spec(short_root), backend, force=True)
    run.prepare()
    await run.spawn_all()
    await run.arm_all()
    await run.dispatch()

    leaked = await asyncio.wait_for(run.teardown(), timeout=5.0)

    assert leaked == []
    assert backend.alive == set(), f"still running: {backend.alive}"
    killed = sorted(s.index for s in run.slots if s.phase == fleet_mod.PHASE_KILLED)
    assert killed == [1, 4]
    assert sorted(i for k, i in backend.log if k == "kill") == [1, 4]


async def test_a_session_that_survives_the_kill_is_reported_not_swallowed(short_root):
    """"Teardown leaves nothing running" is a claim, so the harness has to
    be able to say when it is false. A silent leak is worse than a loud
    one: the next run inherits the memory and nobody knows why it swapped."""
    backend = FakeBackend(hang_stop={3}, unkillable={3})
    run = fleet_mod.FleetRun(_spec(short_root), backend, force=True)
    run.prepare()
    await run.spawn_all()
    await run.arm_all()

    await asyncio.wait_for(run.teardown(), timeout=5.0)

    assert run.slots[3].phase == fleet_mod.PHASE_LEAKED
    assert run.slots[3].error


async def test_teardown_ends_a_slot_stopped_when_its_real_finalize_is_slow_but_clean(
    short_root, tmp_path, monkeypatch,
):
    """Issue #58, at the layer that actually broke: a real ``DaemonBackend``
    driving a real ``SessionDaemon`` over a real socket, standing in for a
    LORE-enabled session whose finalize (the review/index) is genuinely
    slow -- not wedged, just slow. Before the fix, ``EngineClient.stop()``
    returned the instant the daemon acknowledged the request, so
    ``FleetRun.teardown`` declared this slot done in milliseconds -- long
    before the finalize it was supposedly waiting on had actually run, and
    only ``kill_grace_s``'s fixed clock stood between that lie and the pid
    being SIGKILLed out from under a session that was shutting down
    cleanly. The fix makes ``backend.stop()`` (via ``EngineClient.stop``)
    block until the daemon really closes the connection, so teardown here
    must take at least as long as the finalize -- and end the slot
    `stopped`, never `killed`."""
    from doxa.engine import EngineEvent
    from tests.test_daemon import running_daemon

    FINALIZE_DELAY = 0.3

    async with running_daemon(tmp_path, monkeypatch, linger=600.0) as (
        daemon, created, serve_task,
    ):
        async def slow_finalize():
            await asyncio.sleep(FINALIZE_DELAY)
            daemon.engine._finalized = True
            return EngineEvent(
                "session_done", {"indexed": 0, "belief_count": 0}
            )

        daemon.engine.finalize = slow_finalize

        spec = _spec(short_root, n=1, stop_timeout_s=5.0, kill_grace_s=0.2)
        run = fleet_mod.FleetRun(spec, fleet_mod.DaemonBackend(), force=True)
        slot = run.slots[0]
        # No real OS process behind this slot -- the daemon runs in-process
        # against a real socket, as test_daemon.py's own harness does, so
        # `_gone`'s pid check has nothing to answer about and correctly
        # stays out of the way (see fleet.Slot.pid's guard in teardown).
        # What this test measures is the FIRST-pass wait inside `one()`,
        # not the second-pass reap check -- a real pid belongs to the
        # mandatory live-fleet verification, not a unit test.
        slot.socket_path = str(daemon.socket_path)
        slot.pid = None
        slot.phase = fleet_mod.PHASE_QUIET
        await run.backend.arm(slot, spec)

        started = time.monotonic()
        leaked = await asyncio.wait_for(run.teardown(), timeout=5.0)
        elapsed = time.monotonic() - started

        assert leaked == []
        assert slot.phase == fleet_mod.PHASE_STOPPED, (
            f"a slow-but-clean finalize must not be mistaken for a wedge "
            f"(error={slot.error!r})"
        )
        assert elapsed >= FINALIZE_DELAY - 0.05, (
            f"teardown() returned after {elapsed:.3f}s -- before the "
            f"{FINALIZE_DELAY}s finalize it was supposed to wait for"
        )
        await asyncio.wait_for(serve_task, 5)


async def test_teardown_still_kills_and_names_a_slot_whose_finalize_never_returns(
    short_root, tmp_path, monkeypatch,
):
    """The other half of issue #58's fix, and the property the fix must not
    trade away: a finalize that is not merely slow but genuinely over
    budget must still be escalated and still show up in the manifest.

    Before the fix this was WORSE than the reported defect, not better:
    ``EngineClient.stop()`` closed on the ack alone, so a session wedged
    INSIDE finalize was reported `stopped` forever and never revisited --
    a real daemon leaked with nobody told. The fix makes `stop()` wait for
    the daemon's own close, so a finalize that outruns ``stop_timeout_s``
    now bounds the wait the same way any other unresponsive session does,
    and still ends the slot `killed` with the reason on record."""
    from doxa.engine import EngineEvent
    from tests.test_daemon import running_daemon

    WEDGE_S = 1.0

    async with running_daemon(tmp_path, monkeypatch, linger=600.0) as (
        daemon, created, serve_task,
    ):
        async def wedged_finalize():
            await asyncio.sleep(WEDGE_S)  # bounded so the test cleans up
            daemon.engine._finalized = True
            return EngineEvent(
                "session_done", {"indexed": 0, "belief_count": 0}
            )

        daemon.engine.finalize = wedged_finalize

        spec = _spec(short_root, n=1, stop_timeout_s=0.15, kill_grace_s=0.2)
        run = fleet_mod.FleetRun(spec, fleet_mod.DaemonBackend(), force=True)
        slot = run.slots[0]
        slot.socket_path = str(daemon.socket_path)
        slot.pid = None  # nothing to signal for real; see the sibling test
        slot.phase = fleet_mod.PHASE_QUIET
        await run.backend.arm(slot, spec)

        leaked = await asyncio.wait_for(run.teardown(), timeout=5.0)

        assert slot.phase == fleet_mod.PHASE_KILLED
        assert slot.error, "a wedge must say why, not just that it happened"
        assert leaked == []  # slot.pid is None: nothing real left to report

        # The daemon is still finishing its (bounded) finalize in the
        # background; let it, rather than tearing the fixture down while
        # SessionDaemon._shutdown is mid-flight.
        await asyncio.wait_for(serve_task, 5)


async def test_a_session_that_fails_to_spawn_does_not_abort_the_run(short_root):
    """One daemon failing to come up is data about the run. Aborting on it
    produces no ledger at all, which is strictly worse than a ledger with a
    hole the manifest names."""
    backend = FakeBackend(fail_spawn={0, 5})
    run = fleet_mod.FleetRun(_spec(short_root), backend, force=True)
    run.prepare()
    await run.spawn_all()
    await run.arm_all()
    await run.dispatch()

    failed = sorted(s.index for s in run.slots if s.phase == fleet_mod.PHASE_FAILED)
    assert failed == [0, 5]
    assert sorted(backend.prompts) == [1, 2, 3, 4]
    assert run.report.to_obj()["slots"][0]["error"]


async def test_the_whole_run_tears_down_even_when_a_phase_raises(short_root):
    """The `finally` is the contract. A harness that can leave thirty-two
    daemons behind on an exception costs someone an afternoon the first
    time it throws."""

    class ExplodingDispatch(FakeBackend):
        async def dispatch(self, slot, prompt) -> None:
            raise MemoryError("boom")

    backend = ExplodingDispatch()
    run = fleet_mod.FleetRun(_spec(short_root), backend, force=True)

    report = await asyncio.wait_for(run.run(), timeout=10.0)

    assert backend.alive == set()
    assert report.leaked_pids == ()
    assert run.spec.manifest_path.exists()


# =======================================================================
# The run's own ledger, and the run's own everything else
# =======================================================================


async def test_each_runs_ledger_contains_only_its_own_run(short_root):
    """The failure this catches: one shared ledger filtered by time, which
    is correct until two runs overlap and then silently attributes one
    run's messages to the other.

    Per-run DOXA_HOME makes the file the run. Two runs write, and neither
    can see the other's traffic."""
    reports = []
    for tag in ("alpha", "beta"):
        backend = FakeBackend()
        spec = _spec(short_root, run_id=tag)
        run = fleet_mod.FleetRun(spec, backend, force=True)
        run.prepare()
        await run.spawn_all()
        ledger = peerledger_mod.PeerLedger(path=spec.ledger_path)
        for slot in run.slots:
            ledger.append(
                sender=peerledger_mod.Sender(session=f"{tag}-{slot.index}"),
                to=[f"{tag}-other"],
                body=f"{tag} says hello",
            )
        run.collect()
        reports.append((tag, run.report, spec))

    for tag, report, spec in reports:
        assert report.ledger_messages == 6
        assert spec.ledger_path.exists()
        bodies = {m.body for m in peerledger_mod.PeerLedger(path=spec.ledger_path).snapshot()}
        assert bodies == {f"{tag} says hello"}
        assert all(not b.startswith("alpha") for b in bodies) or tag == "alpha"

    assert reports[0][2].ledger_path != reports[1][2].ledger_path


async def test_a_run_gives_every_session_its_own_home_and_runtime(short_root):
    """The env a session is spawned with is where per-run isolation is
    actually delivered. If this regresses, the ledger silently becomes the
    machine's ledger and the registry silently becomes the machine's
    registry -- N stops being the N that was dealt."""
    backend = FakeBackend()
    spec = _spec(short_root, run_id="iso")
    run = fleet_mod.FleetRun(spec, backend, force=True)
    run.prepare()
    await run.spawn_all()

    for index, env in backend.spawn_env.items():
        assert env["DOXA_HOME"] == str(spec.home), index
        assert env["DOXA_RUNTIME_DIR"] == str(spec.runtime), index
    assert str(spec.home) != os.environ.get("DOXA_HOME")
    assert spec.ledger_path == spec.home / "peers" / "messages.jsonl"


# =======================================================================
# Model assignment: randomised per run, recorded, reproducible
# =======================================================================


def test_model_assignment_is_reproducible_from_its_seed():
    """The failure this catches: a draw nobody can recreate, which makes a
    run impossible to re-analyse or to re-run with the latency floor the
    plan names as its own mitigation."""
    first = fleet_mod.assign(32, list(POOL), seed=99)
    again = fleet_mod.assign(32, list(POOL), seed=99)
    other = fleet_mod.assign(32, list(POOL), seed=100)

    assert [a.to_obj() for a in first] == [a.to_obj() for a in again]
    assert [a.to_obj() for a in first] != [a.to_obj() for a in other]


def test_model_assignment_is_not_a_fixed_mapping_from_slot_index():
    """"Model is assigned to agent **randomly per run**, so role cannot be
    confounded with capability." A draw that gave slot 0 the same model
    every run would make "slot 0 coordinates" and "sonnet coordinates" the
    same observation forever."""
    per_slot = [
        {fleet_mod.assign(8, list(POOL), seed=s)[i].label for s in range(40)}
        for i in range(8)
    ]
    assert all(len(models) > 1 for models in per_slot), per_slot


def test_a_weighted_pool_favours_the_cheap_vendor_without_excluding_the_dear_one():
    """The plan's economics: "Sampling weights the inexpensive vendors
    heavily and includes costly ones at low probability, so the mix stays
    honest without the bill scaling with N.\""""
    drawn = [a.label for s in range(200) for a in fleet_mod.assign(8, list(POOL), seed=s)]
    counts = {label: drawn.count(label) for label in set(drawn)}
    assert counts["claude:sonnet"] > counts["deepseek:deepseek-chat"] > counts["claude:opus"]
    assert counts["claude:opus"] > 0, "a low weight must not be an exclusion"


async def test_the_manifest_records_the_assignment_and_the_prompt(short_root):
    """"Slot 7 coordinated" is uninterpretable without the manifest saying
    what slot 7 was running and what everybody was asked to do."""
    backend = FakeBackend()
    spec = _spec(short_root, run_id="rec")
    run = fleet_mod.FleetRun(spec, backend, force=True)

    await asyncio.wait_for(run.run(), timeout=10.0)

    manifest = json.loads(spec.manifest_path.read_text(encoding="utf-8"))
    assert manifest["run_id"] == "rec"
    assert manifest["spec"]["seed"] == spec.seed
    assert manifest["spec"]["prompt"] == PROMPT
    assert manifest["spec"]["prompt_sha256"]
    assert len(manifest["assignments"]) == spec.n
    assert [a["index"] for a in manifest["assignments"]] == list(range(spec.n))
    assert all(a["engine"] for a in manifest["assignments"])
    assert manifest["dispatch_order"]
    assert manifest["ledger"]["path"] == str(spec.ledger_path)

    replayed = fleet_mod.assign(
        manifest["spec"]["n"], list(POOL), seed=manifest["spec"]["seed"]
    )
    assert [a.to_obj() for a in replayed] == [
        {k: a[k] for k in ("index", "engine", "model", "lore", "role")}
        for a in manifest["assignments"]
    ]
    # A symmetric run has no supervisor and says so, rather than leaving a
    # reader to infer the mode from a missing key.
    assert manifest["mode"] == "symmetric"
    assert manifest["supervisor"] is None
    assert {a["role"] for a in manifest["assignments"]} == {"worker"}


# =======================================================================
# Memory is a per-AGENT variable
# =======================================================================


def test_memory_is_decided_per_agent_not_per_fleet():
    """A shared LORE store is a communication channel that does not appear
    in the ledger. Per-agent control is what turns that confound into a
    manipulable variable -- a cell can hold memory-off agents beside
    memory-on ones."""
    assignments = fleet_mod.assign(
        32, list(POOL), seed=7, memory=fleet_mod.MemoryPolicy(off_count=12)
    )
    off = [a.index for a in assignments if not a.lore]

    assert len(off) == 12
    assert 0 < len(off) < 32, "a per-fleet switch would be all or nothing"
    assert off == sorted(off)


def test_which_agents_lose_memory_is_reproducible_from_the_seed():
    policy = fleet_mod.MemoryPolicy(off_count=5)
    a = fleet_mod.assign(16, list(POOL), seed=3, memory=policy)
    b = fleet_mod.assign(16, list(POOL), seed=3, memory=policy)
    assert [x.to_obj() for x in a] == [x.to_obj() for x in b]


def test_memory_off_for_nobody_leaves_the_model_draw_untouched():
    """Adding memory as a variable must not silently perturb the model
    assignment of every run recorded before it existed."""
    without = fleet_mod.assign(16, list(POOL), seed=42)
    with_zero = fleet_mod.assign(
        16, list(POOL), seed=42, memory=fleet_mod.MemoryPolicy(off_count=0)
    )
    assert [a.to_obj() for a in without] == [a.to_obj() for a in with_zero]
    assert all(a.lore for a in without)


async def test_a_memory_off_agent_is_spawned_with_lore_off_and_its_neighbour_is_not(
    short_root,
):
    """The assignment has to reach the process. This is the seam where a
    per-agent variable would quietly become a no-op."""
    backend = FakeBackend()
    spec = _spec(short_root, n=8, memory=fleet_mod.MemoryPolicy(off_count=3))
    run = fleet_mod.FleetRun(spec, backend, force=True)
    run.prepare()
    await run.spawn_all()

    off = {s.index for s in run.slots if not s.assignment.lore}
    on = {s.index for s in run.slots if s.assignment.lore}
    assert len(off) == 3 and len(on) == 5

    for index in off:
        assert backend.spawn_env[index]["DOXA_LORE"] == "0"
    for index in on:
        assert "DOXA_LORE" not in backend.spawn_env[index]


# =======================================================================
# Capacity and paths: refusals that name their arithmetic
# =======================================================================


def test_the_default_n_is_not_the_experiments_n():
    """A default is what somebody runs by accident. N=32 is ~19 GB, which
    is fine on the 192 GB workstation and a swap storm on a 30 GB laptop --
    so the number that needs a deliberate decision is not the default."""
    assert fleet_mod.DEFAULT_N < 32


def test_an_n_that_does_not_fit_is_refused_with_the_arithmetic():
    """The failure this catches: a run that starts, swaps, and reports
    attrition that was actually paging."""
    with pytest.raises(fleet_mod.CapacityRefused, match=r"N=32"):
        fleet_mod.check_capacity(32, available_mb=4096)

    note = fleet_mod.check_capacity(2, available_mb=64_000)
    assert "N=2" in note and "GB" in note


def test_a_refusal_can_be_overridden_only_on_purpose():
    """The companion proof that the refusal is a real branch rather than a
    function that always raises."""
    note = fleet_mod.check_capacity(32, force=True, available_mb=4096)
    assert "N=32" in note


def test_capacity_does_not_invent_a_number_when_it_cannot_measure_one(monkeypatch):
    """A machine whose memory cannot be read gets an honest "unknown",
    never a plausible number -- the same rule doxa.ui.labels applies to an
    unmeasured context limit. And an unknown does not become a refusal:
    refusing every run on a platform without /proc would be inventing a
    number in the other direction."""
    monkeypatch.setattr(fleet_mod, "available_memory_mb", lambda: None)
    note = fleet_mod.capacity_note(8)
    assert "could not be measured" in note
    assert "N=32" in fleet_mod.check_capacity(32)


def test_a_run_root_too_deep_for_a_unix_socket_is_refused_up_front(tmp_path):
    """The failure this catches: a bind that fails inside asyncio, per
    session, minutes into a spawn loop, with a message about a path nobody
    chose by hand. AF_UNIX gives 108 bytes and a per-run directory under a
    per-run root is what spends them."""
    deep = tmp_path / ("x" * 90) / ("y" * 40)
    with pytest.raises(ValueError, match=r"AF_UNIX path budget"):
        fleet_mod.check_socket_budget(deep)

    fleet_mod.check_socket_budget("/tmp/dx/rt")


def test_a_fleet_needs_a_pool_and_a_prompt(tmp_path):
    """No default pool: an assignment that was not chosen is an assignment
    that cannot be reported."""
    with pytest.raises(ValueError, match=r"model pool"):
        fleet_mod.FleetSpec(prompt="x", cwd=str(tmp_path), pool=())
    with pytest.raises(ValueError, match=r"needs a prompt"):
        fleet_mod.FleetSpec(prompt="   ", cwd=str(tmp_path), pool=POOL)
    with pytest.raises(ValueError, match=r"at least one session"):
        fleet_mod.FleetSpec(prompt="x", cwd=str(tmp_path), pool=POOL, n=0)


# =======================================================================
# The two failures scaling actually found
# =======================================================================


def test_a_zombie_child_is_not_a_leaked_session():
    """MEASURED, at N=4, and it cost a whole scale run to find.

    ``os.kill(pid, 0)`` reports a ZOMBIE as alive, and every session a
    fleet spawns is this process's own child -- ``Popen(...,
    start_new_session=True)`` starts a new session, not a new parent, and
    nothing keeps the ``Popen`` object to wait on. So a daemon that
    accepted ``stop``, finalized and exited perfectly cleanly stayed
    "alive" to the liveness check: all four clean shutdowns were reported
    as leaks. A harness crying wolf about the exact property it exists to
    verify is worse than one that does not check."""
    import os

    def _really_alive(pid: int) -> bool:
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    pid = os.fork()
    if pid == 0:  # the child: exit at once and become a zombie
        os._exit(0)

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not fleet_mod._is_zombie(pid):
        time.sleep(0.01)

    assert _really_alive(pid), (
        "the companion: os.kill(pid, 0) still calls this exited process "
        "alive, which is exactly the wrong answer _gone exists to replace"
    )
    assert fleet_mod._gone(pid) is True


async def test_a_session_is_not_quiet_while_a_prompt_is_still_queued(short_root):
    """MEASURED, at N=32 with the fleet actually messaging each other: the
    run never quiesced.

    When a peer message has already started a turn, the daemon ENQUEUES an
    arriving prompt rather than running it -- so a session can be "not
    running" and still have work in front of it. A quiescence check that
    read only ``running`` would call that session idle and collect a
    ledger from the middle of an exchange."""
    answers = iter([
        {"running": True, "queued": 0},
        {"running": False, "queued": 2},
        {"running": False, "queued": 0},
    ])

    class _Client:
        async def refresh_status(self):
            return next(answers)

    backend = fleet_mod.DaemonBackend()
    slot = fleet_mod.Slot(
        assignment=fleet_mod.Assignment(index=0, engine="claude", model="sonnet")
    )
    backend._clients[0] = _Client()

    assert await backend.is_quiet(slot) is False   # running
    assert await backend.is_quiet(slot) is False   # queued behind a peer turn
    assert await backend.is_quiet(slot) is True    # genuinely idle


async def test_a_daemon_that_dies_mid_read_does_not_leave_an_unretrieved_exception():
    """MEASURED at N=128: every session's teardown raised a
    ``BrokenPipeError`` out of ``EngineClient._read_loop``, unretrieved --
    128 asyncio tracebacks on stderr, which is how a run's log stops being
    readable exactly when something real goes wrong in it.

    The read loop's own ``finally`` always did the right thing (unblock a
    waiting send, close out like a detach); it was the exception ESCAPING
    the task afterwards that was the defect."""
    from doxa.client import EngineClient

    client = EngineClient("/nonexistent.sock")

    class _DyingReader:
        async def readline(self):
            raise BrokenPipeError(32, "Broken pipe")

    client._reader = _DyingReader()
    task = asyncio.create_task(client._read_loop())
    await task

    assert task.exception() is None, (
        "the read loop let an OSError escape its task -- an unretrieved "
        "task exception per session is the noise this test exists to stop"
    )
    assert client._closed is True, "the finally must still close out"


# =======================================================================
# Issue #39 -- a slot runs the engine it was dealt
# =======================================================================


MIXED_POOL = (
    fleet_mod.ModelSlot(engine="claude", model="sonnet", weight=1),
    fleet_mod.ModelSlot(engine="deepseek", model="deepseek-chat", weight=1),
)


async def test_the_daemon_backend_spawns_each_slot_on_its_own_engine(
    short_root, monkeypatch,
):
    """The defect issue #39 names: DaemonBackend.spawn never passed
    slot.assignment.engine, and spawn_daemon had no parameter to take it,
    so a pool entry `deepseek:deepseek-chat` started CLAUDE with
    `--model deepseek-chat` -- and the manifest recorded an engine that
    never ran."""
    from doxa import daemon as daemon_mod

    calls = []

    def fake_spawn(**kwargs):
        calls.append(kwargs)
        return f"sess-{len(calls)}", f"/tmp/fake-{len(calls)}.sock"

    monkeypatch.setattr(daemon_mod, "spawn_daemon", fake_spawn)
    spec = _spec(short_root, pool=MIXED_POOL, n=6)
    run = fleet_mod.FleetRun(spec, fleet_mod.DaemonBackend(), force=True)
    run.prepare()
    backend = run.backend
    slots = run.slots
    for slot in slots:
        await backend.spawn(slot, spec)

    assert [c["engine"] for c in calls] == [s.assignment.engine for s in slots]
    # The draw has to have produced both, or this test would pass on a
    # single-engine run and prove nothing.
    assert set(c["engine"] for c in calls) == {"claude", "deepseek"}
    # And the model still travels with it, per slot.
    assert [c["model"] for c in calls] == [s.assignment.model for s in slots]


async def test_a_slot_whose_engine_refuses_to_start_is_failed_and_the_run_goes_on(
    short_root,
):
    """A vendor engine with no API key raises MissingCredential inside the
    daemon, which exits during startup; spawn_daemon turns that into a
    RuntimeError carrying the log tail. One slot that cannot start is data
    about the run -- recorded per slot, with the reason -- and the rest of
    the fleet still runs."""

    class _NoCredential(FakeBackend):
        async def spawn(self, slot, spec) -> None:
            if slot.assignment.engine != "claude":
                raise RuntimeError(
                    "doxa daemon exited during startup (code 1). Log tail:\n"
                    "MissingCredential: DEEPSEEK_API_KEY is not set"
                )
            await super().spawn(slot, spec)

    backend = _NoCredential()
    spec = _spec(short_root, pool=MIXED_POOL, n=6)
    run = fleet_mod.FleetRun(spec, backend, force=True)
    run.prepare()
    await run.spawn_all()
    await run.arm_all()
    await run.dispatch()

    failed = [s for s in run.slots if s.phase == fleet_mod.PHASE_FAILED]
    ran = [s for s in run.slots if s.phase == fleet_mod.PHASE_DISPATCHED]
    assert failed and ran, "the draw must contain both engines"
    assert all(s.assignment.engine != "claude" for s in failed)
    assert all(s.assignment.engine == "claude" for s in ran)
    assert all("DEEPSEEK_API_KEY" in (s.error or "") for s in failed)
    # And the manifest carries both the assignment and the reason.
    obj = run.report.to_obj()
    rows = {row["index"]: row for row in obj["slots"]}
    for slot in failed:
        assert rows[slot.index]["assignment"]["engine"] == slot.assignment.engine
        assert "MissingCredential" in rows[slot.index]["error"]


# =======================================================================
# A recorded pid is a claim about the past (audit finding 6)
# =======================================================================


def test_kill_pid_will_not_signal_a_pid_that_is_no_longer_a_daemon():
    """No probe: sending a real SIGKILL to a reused pid is the defect, and
    a probe that reproduced it would have to kill something. Asserted on
    the signal instead -- a recorded pid can outlive the daemon it named
    and the kernel can hand the number to anything, so teardown checks
    ``/proc/<pid>/cmdline`` before it signals."""
    import signal as signal_mod

    # This test process is emphatically not a doxa daemon.
    assert fleet_mod._is_doxa_daemon(os.getpid()) is False

    sent: list = []
    real_kill = os.kill

    def spy(pid, sig):
        sent.append((pid, sig))
        if sig == 0:
            return real_kill(pid, sig)
        raise AssertionError(f"signalled a non-daemon pid: {pid} {sig}")

    try:
        os.kill = spy
        assert fleet_mod._kill_pid(os.getpid(), grace_s=0.1) is True
    finally:
        os.kill = real_kill
    assert not [s for _pid, s in sent if s in (signal_mod.SIGTERM, signal_mod.SIGKILL)]


def test_a_daemon_pid_is_recognised_from_its_cmdline():
    """The other half: the check must not refuse every pid, or teardown
    stops working. ``python -m doxa.daemon`` is how spawn_daemon starts
    one, so the marker is in argv -- reproduced here as a process that
    carries the same string and then SLEEPS. Spawning a real
    ``-m doxa.daemon --help`` instead was racy under suite load: it exits
    at once, and a zombie's ``/proc/<pid>/cmdline`` reads back empty."""
    import subprocess
    import sys

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "-m", "doxa.daemon"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(200):
            if fleet_mod._is_doxa_daemon(proc.pid) is True:
                break
            time.sleep(0.01)
        assert fleet_mod._is_doxa_daemon(proc.pid) is True
    finally:
        proc.kill()
        proc.wait(timeout=30)

    # An empty or absent cmdline -- a reaped pid, a zombie, a kernel
    # thread -- is False, which is "do not signal it": the safe direction.
    assert fleet_mod._is_doxa_daemon(999999) is False


def test_an_unreadable_proc_entry_is_not_read_as_a_dead_process(monkeypatch):
    """``_is_zombie`` returned True on ANY OSError -- including one from a
    /proc that simply cannot be read -- so "cannot tell" was reported as
    "gone" and teardown walked away from a live daemon. It is consulted
    only after ``_pid_alive`` said the pid exists, so False (leave that
    answer standing) is the only safe direction."""
    from pathlib import Path as _Path

    real_read = _Path.read_text

    def unreadable(self, *args, **kwargs):
        if str(self).startswith("/proc/"):
            raise PermissionError("cannot read /proc")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(_Path, "read_text", unreadable)
    assert fleet_mod._is_zombie(os.getpid()) is False
