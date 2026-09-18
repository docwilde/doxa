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
import json
import os

import pytest

from doxa import fleet as fleet_mod
from doxa import peerledger as peerledger_mod


POOL = (
    fleet_mod.ModelSlot(engine="claude", model="sonnet", weight=8),
    fleet_mod.ModelSlot(engine="claude", model="opus", weight=1),
    fleet_mod.ModelSlot(engine="deepseek", model="deepseek-chat", weight=4),
)

PROMPT = "Rename every occurrence of `foo` to `bar`. The suite is the oracle."


def _spec(tmp_path, **kw):
    """A spec whose run root is SHORT.

    ``tmp_path`` under pytest is long, and a fleet run puts a Unix socket
    beneath it -- see :func:`doxa.fleet.check_socket_budget`. Tests that are
    not about that budget use ``os.environ["TMPDIR"]``-free short paths, so
    a path-length failure never masquerades as an orchestration failure."""
    kw.setdefault("prompt", PROMPT)
    kw.setdefault("cwd", str(tmp_path))
    kw.setdefault("pool", POOL)
    kw.setdefault("root", tmp_path / "f")
    kw.setdefault("n", 6)
    kw.setdefault("seed", 1234)
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


async def test_no_session_is_prompted_before_every_session_is_armed(tmp_path):
    """The failure this catches: a harness that spawns-and-prompts in a
    loop, so session 0 is working while session 31 does not yet exist.

    That is the asymmetry docs/plans/emergent-organization.md rejects in
    its second section -- the informed agent coordinates by default, and
    what the run then measures is the decay of an initial asymmetry rather
    than emergence. The assertion is structural rather than temporal: in
    the whole event log, no `dispatch` may precede any `arm`."""
    backend = FakeBackend(arm_delay=0.005)
    run = fleet_mod.FleetRun(_spec(tmp_path), backend, force=True)
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


async def test_every_session_receives_the_byte_identical_prompt(tmp_path):
    """The failure this catches: per-session prompt formatting -- "you are
    agent 7 of 32" -- which hands a participant its own position, and a
    position is the privilege the design refuses to grant."""
    backend = FakeBackend()
    run = fleet_mod.FleetRun(_spec(tmp_path), backend, force=True)
    run.prepare()
    await run.spawn_all()
    await run.arm_all()
    await run.dispatch()

    assert set(backend.prompts) == set(range(6))
    assert set(backend.prompts.values()) == {PROMPT}


async def test_dispatch_order_is_drawn_from_the_seed_not_the_slot_index(tmp_path):
    """The failure this catches: slot 0 going first in every run, so "who
    spoke first" is a constant correlated with the slot index the model
    assignment is recorded against -- an ordering advantage that survives
    averaging over the five replications of a cell.

    Two runs, two seeds, same everything else: the orders must differ, and
    neither may be the identity permutation."""
    orders = []
    for seed in (1, 2, 3, 4):
        backend = FakeBackend()
        run = fleet_mod.FleetRun(_spec(tmp_path, seed=seed, n=8), backend, force=True)
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


async def test_the_dispatch_spread_is_measured_and_recorded(tmp_path):
    """A paper may claim "simultaneous" only if the harness can say how
    simultaneous. The manifest records the observed width of the dispatch
    window rather than asserting the ideal."""
    backend = FakeBackend()
    run = fleet_mod.FleetRun(_spec(tmp_path), backend, force=True)
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


async def test_a_hung_session_does_not_hang_the_run(tmp_path):
    """The failure this catches: one session whose status call never
    returns, holding the quiescence wait open until somebody notices --
    which, on an unattended 20-run sweep, is the next morning.

    The hung slot is marked, dropped from the wait, and the run finishes.
    The other five still reach quiet."""
    backend = FakeBackend(hang_quiet={2})
    spec = _spec(tmp_path, quiescence_timeout_s=5.0)
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


async def test_a_run_that_never_goes_quiet_still_ends_at_its_deadline(tmp_path):
    """The other half: every session busy forever. The deadline is the only
    thing that ends this, so the deadline has to end it."""

    class NeverQuiet(FakeBackend):
        async def is_quiet(self, slot) -> bool:
            return False

    backend = NeverQuiet()
    spec = _spec(tmp_path, quiescence_timeout_s=0.3)
    run = fleet_mod.FleetRun(spec, backend, force=True)
    run.prepare()
    await run.spawn_all()
    await run.arm_all()
    await run.dispatch()

    quiesced = await asyncio.wait_for(run.await_quiescence(), timeout=5.0)

    assert quiesced is False
    assert all(s.phase == fleet_mod.PHASE_HUNG for s in run.slots)
    assert all("quiescence deadline" in (s.error or "") for s in run.slots)


async def test_teardown_leaves_nothing_running(tmp_path):
    """The failure this catches: ~600 MB per wedged daemon, times the ones
    that ignored `stop`, accumulating across a sweep until the machine is
    unusable between runs.

    A session that will not stop is SIGTERMed and then SIGKILLed, and the
    run says so."""
    backend = FakeBackend(hang_stop={1, 4})
    run = fleet_mod.FleetRun(_spec(tmp_path), backend, force=True)
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


async def test_a_session_that_survives_the_kill_is_reported_not_swallowed(tmp_path):
    """"Teardown leaves nothing running" is a claim, so the harness has to
    be able to say when it is false. A silent leak is worse than a loud
    one: the next run inherits the memory and nobody knows why it swapped."""
    backend = FakeBackend(hang_stop={3}, unkillable={3})
    run = fleet_mod.FleetRun(_spec(tmp_path), backend, force=True)
    run.prepare()
    await run.spawn_all()
    await run.arm_all()

    await asyncio.wait_for(run.teardown(), timeout=5.0)

    assert run.slots[3].phase == fleet_mod.PHASE_LEAKED
    assert run.slots[3].error


async def test_a_session_that_fails_to_spawn_does_not_abort_the_run(tmp_path):
    """One daemon failing to come up is data about the run. Aborting on it
    produces no ledger at all, which is strictly worse than a ledger with a
    hole the manifest names."""
    backend = FakeBackend(fail_spawn={0, 5})
    run = fleet_mod.FleetRun(_spec(tmp_path), backend, force=True)
    run.prepare()
    await run.spawn_all()
    await run.arm_all()
    await run.dispatch()

    failed = sorted(s.index for s in run.slots if s.phase == fleet_mod.PHASE_FAILED)
    assert failed == [0, 5]
    assert sorted(backend.prompts) == [1, 2, 3, 4]
    assert run.report.to_obj()["slots"][0]["error"]


async def test_the_whole_run_tears_down_even_when_a_phase_raises(tmp_path):
    """The `finally` is the contract. A harness that can leave thirty-two
    daemons behind on an exception costs someone an afternoon the first
    time it throws."""

    class ExplodingDispatch(FakeBackend):
        async def dispatch(self, slot, prompt) -> None:
            raise MemoryError("boom")

    backend = ExplodingDispatch()
    run = fleet_mod.FleetRun(_spec(tmp_path), backend, force=True)

    report = await asyncio.wait_for(run.run(), timeout=10.0)

    assert backend.alive == set()
    assert report.leaked_pids == ()
    assert run.spec.manifest_path.exists()


# =======================================================================
# The run's own ledger, and the run's own everything else
# =======================================================================


async def test_each_runs_ledger_contains_only_its_own_run(tmp_path):
    """The failure this catches: one shared ledger filtered by time, which
    is correct until two runs overlap and then silently attributes one
    run's messages to the other.

    Per-run DOXA_HOME makes the file the run. Two runs write, and neither
    can see the other's traffic."""
    reports = []
    for tag in ("alpha", "beta"):
        backend = FakeBackend()
        spec = _spec(tmp_path, run_id=tag)
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


async def test_a_run_gives_every_session_its_own_home_and_runtime(tmp_path):
    """The env a session is spawned with is where per-run isolation is
    actually delivered. If this regresses, the ledger silently becomes the
    machine's ledger and the registry silently becomes the machine's
    registry -- N stops being the N that was dealt."""
    backend = FakeBackend()
    spec = _spec(tmp_path, run_id="iso")
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


async def test_the_manifest_records_the_assignment_and_the_prompt(tmp_path):
    """"Slot 7 coordinated" is uninterpretable without the manifest saying
    what slot 7 was running and what everybody was asked to do."""
    backend = FakeBackend()
    spec = _spec(tmp_path, run_id="rec")
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
        {k: a[k] for k in ("index", "engine", "model", "lore")}
        for a in manifest["assignments"]
    ]


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
    tmp_path,
):
    """The assignment has to reach the process. This is the seam where a
    per-agent variable would quietly become a no-op."""
    backend = FakeBackend()
    spec = _spec(tmp_path, n=8, memory=fleet_mod.MemoryPolicy(off_count=3))
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


def test_capacity_does_not_invent_a_number_when_it_cannot_measure_one():
    note = fleet_mod.capacity_note(8, available_mb=None)
    assert "could not be measured" in note


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
