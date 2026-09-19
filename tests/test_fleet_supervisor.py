# SPDX-License-Identifier: AGPL-3.0-only
"""Supervisor mode: one session gets the prompt and distributes the work.

``doxa.fleet``'s original shape is an INSTRUMENT -- N sessions, one
identical prompt, one instant, nobody privileged -- and tests/test_fleet.py
pins every property that rests on. This file pins the second shape, the one
ordinary software work needs, and each test is named for the failure it
catches:

* **the symmetric run is untouched.** A missing prompt is still refused in
  the same words; a run with no ``--supervisor`` deals the same slots from
  the same seed. A mode that changed the other mode would be a mode nobody
  could trust either half of.
* **n means workers.** ``-n 3 --supervisor claude:opus`` is four sessions,
  and the capacity arithmetic, the budget division and the manifest all
  count four. The failure this catches is a run that quietly spends a
  worker on the coordinator, or a per-session ceiling that is one share too
  large.
* **no worker can be given work before it knows what work is.** Every
  worker is briefed and acknowledged before the supervisor is prompted at
  all. This is the inverse of the symmetric shuffle and the only ordering
  property supervisor mode actually rests on.
* **the two agents can address each other from their first turn.** A
  worker's briefing carries its own session id and its supervisor's; the
  supervisor's carries every worker's. A roster that had to be fetched is a
  roster a model may decide not to fetch.
* **an interactive run is not ended by quiet.** Its supervisor is waiting
  for a human to attach and type, which from outside is indistinguishable
  from a finished fleet -- and a dwell would tear down five sessions and a
  worktree each while the operator was still reading the tab.

The backend is the stub tests/test_fleet.py already drives, so nothing here
spawns a process or spends a token.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import shutil
import tempfile

import pytest

from doxa import fleet as fleet_mod
from tests.test_fleet import FakeBackend


POOL = (fleet_mod.ModelSlot(engine="claude", model="sonnet", weight=1),)
PROMPT = "Split the rename across the workers and report what landed where."

_RUN_IDS = itertools.count()


@pytest.fixture
def short_root():
    """A run root short enough for a Unix socket to live under it -- see
    :func:`doxa.fleet.check_socket_budget` and the fixture of the same name
    in tests/test_fleet.py, which owns that refusal."""
    root = tempfile.mkdtemp(prefix="dxs", dir="/tmp")
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(autouse=True)
def _no_real_pids(monkeypatch):
    """Stub slots carry invented pids and the teardown's second pass asks
    the OS about them; on a busy machine one of those is a real unrelated
    process. Same guard tests/test_fleet.py installs."""
    from doxa import peers as peers_mod

    monkeypatch.setattr(peers_mod, "_pid_alive", lambda pid: False)


def _spec(short_root, **kw):
    kw.setdefault("prompt", PROMPT)
    kw.setdefault("cwd", short_root)
    kw.setdefault("pool", POOL)
    kw.setdefault("root", short_root)
    kw.setdefault("run_id", f"s{next(_RUN_IDS)}")
    kw.setdefault("n", 3)
    kw.setdefault("seed", 99)
    kw.setdefault("supervisor", fleet_mod.ModelSlot(engine="claude", model="opus"))
    kw.setdefault("run_budget_usd", 10.0)
    kw.setdefault("quiescence_timeout_s", 2.0)
    kw.setdefault("quiet_dwell_s", 0.0)
    kw.setdefault("poll_interval_s", 0.01)
    kw.setdefault("stop_timeout_s", 0.2)
    return fleet_mod.FleetSpec(**kw)


async def _to_dispatch(spec, backend):
    run = fleet_mod.FleetRun(spec, backend, force=True)
    run.prepare()
    await run.spawn_all()
    await run.arm_all()
    await run.dispatch()
    return run


# =======================================================================
# The grammar, and what it refuses
# =======================================================================


def test_supervisor_is_one_pool_entrys_grammar(short_root):
    """The failure this catches: a second reader for ``engine:model``.

    ``--supervisor claude:opus`` has to mean exactly what the same words
    mean inside ``--pool``, or an operator who has learned one has learned
    the wrong thing about the other."""
    spec, _ = fleet_mod.spec_from_argv(
        ["--pool", "claude:sonnet@1", "--supervisor", "claude:opus",
         "-n", "3", "--root", short_root, "--prompt", "x"],
        cwd="/repo",
    )
    assert spec.supervisor == fleet_mod.ModelSlot(engine="claude", model="opus")
    assert spec.mode == fleet_mod.MODE_SUPERVISOR
    # A bare engine, exactly as in a pool entry.
    bare, _ = fleet_mod.spec_from_argv(
        ["--pool", "claude@1", "--supervisor", "codex",
         "--root", short_root, "--prompt", "x"],
        cwd="/repo",
    )
    assert bare.supervisor == fleet_mod.ModelSlot(engine="codex", model=None)


def test_a_comma_in_supervisor_is_refused_not_silently_truncated(short_root):
    """A run has exactly ONE supervisor. An operator who typed a list
    believes otherwise, and taking the first entry would confirm a belief
    the run does not implement."""
    with pytest.raises(fleet_mod.FleetArgsError, match=r"ONE engine"):
        fleet_mod.spec_from_argv(
            ["--pool", "claude@1", "--supervisor", "claude:opus,codex",
             "--root", short_root, "--prompt", "x"],
            cwd="/repo",
        )


def test_a_symmetric_run_still_refuses_a_missing_prompt():
    """The refusal supervisor mode must not have loosened. A symmetric run
    IS its prompt; spawning N sessions with nothing to do is a bill."""
    with pytest.raises(fleet_mod.FleetArgsError, match=r"needs a prompt"):
        fleet_mod.spec_from_argv(["--pool", "claude@1"], cwd="/repo")
    with pytest.raises(ValueError, match=r"needs a prompt"):
        fleet_mod.FleetSpec(prompt="", cwd="/repo", pool=POOL)


def test_a_supervisor_run_may_have_no_prompt_and_is_then_interactive(short_root):
    """The interactive shape: the operator attaches to the supervisor and
    types the task there, so the prompt is not required at spawn time."""
    spec, _ = fleet_mod.spec_from_argv(
        ["--pool", "claude@1", "--supervisor", "claude:opus", "-n", "2",
         "--root", short_root],
        cwd="/repo",
    )
    assert spec.mode == fleet_mod.MODE_SUPERVISOR
    assert spec.interactive is True
    # NO deadline, because the run's end is a human saying so.
    assert spec.quiescence_timeout_s is None


def test_an_explicit_quiescence_timeout_still_bounds_an_interactive_run(short_root):
    """``--quiescence-timeout`` is how an operator says "end it anyway
    after this long". The parser's default is None precisely so that
    asking for 1800 and asking for nothing are distinguishable here."""
    spec, _ = fleet_mod.spec_from_argv(
        ["--pool", "claude@1", "--supervisor", "claude:opus",
         "--root", short_root, "--quiescence-timeout", "45"],
        cwd="/repo",
    )
    assert spec.interactive is True
    assert spec.quiescence_timeout_s == 45.0
    # And a PROMPTED run keeps the default it has always had.
    prompted, _ = fleet_mod.spec_from_argv(
        ["--pool", "claude@1", "--supervisor", "claude:opus",
         "--root", short_root, "--prompt", "do the thing"],
        cwd="/repo",
    )
    assert prompted.interactive is False
    assert prompted.quiescence_timeout_s == 1800.0


def test_doxa_fleet_refuses_an_interactive_run_and_points_at_the_tui(
    capsys, short_root
):
    """``doxa-fleet`` blocks inside ``asyncio.run`` for the length of the
    run, so it cannot attach to the supervisor it just spawned -- and an
    interactive run's whole premise is that somebody does. The honest
    answer is a refusal naming the front end that can, never a fleet of
    briefed workers waiting on a session nobody will ever type into."""
    code = fleet_mod.main([
        "--pool", "claude@1", "--supervisor", "claude:opus",
        "-n", "2", "--root", short_root, "--run-budget", "5",
    ])
    assert code == 2
    err = capsys.readouterr().err
    assert "/fleet start" in err and "/fleet attach 0" in err


# =======================================================================
# n means workers, and the run counts n+1
# =======================================================================


def test_a_supervisor_run_is_n_workers_plus_one_supervisor(short_root):
    """The failure this catches: a supervisor taken OUT of the n the
    operator asked for, so ``-n 4`` silently buys three hands and a
    coordinator."""
    spec = _spec(short_root, n=4)
    assert spec.session_count == 5
    assignments = fleet_mod.assign_for(spec)
    assert [a.index for a in assignments] == [0, 1, 2, 3, 4]
    assert assignments[0].role == fleet_mod.ROLE_SUPERVISOR
    assert assignments[0].label == "claude:opus"
    assert [a.role for a in assignments[1:]] == [fleet_mod.ROLE_WORKER] * 4


def test_the_workers_are_the_same_draw_a_symmetric_run_would_have_dealt(
    short_root,
):
    """A supervisor run's workers come from the SAME seeded draw, merely
    shifted by one. The failure this catches is a mode that perturbs the
    pool draw, which would make a symmetric run and a supervisor run at
    one seed incomparable for no reason anybody chose."""
    pool = (
        fleet_mod.ModelSlot(engine="claude", model="sonnet", weight=8),
        fleet_mod.ModelSlot(engine="deepseek", model="deepseek-chat", weight=4),
    )
    plain = _spec(short_root, n=5, pool=pool, supervisor=None)
    boss = _spec(short_root, n=5, pool=pool)
    symmetric = fleet_mod.assign_for(plain)
    supervised = fleet_mod.assign_for(boss)
    assert [a.label for a in symmetric] == [a.label for a in supervised[1:]]
    assert [a.index for a in supervised[1:]] == [1, 2, 3, 4, 5]


def test_check_run_budget_divides_by_the_supervisor_too(short_root):
    """The failure this catches: a per-session share computed over ``n``
    while ``n+1`` sessions spend it, so the run can exceed its own ceiling
    by one whole share."""
    spec = _spec(short_root, n=3, run_budget_usd=8.0)
    assert spec.session_count == 4
    assert spec.session_budget_usd == pytest.approx(2.0)
    note = fleet_mod.check_run_budget(spec)
    assert "N=4" in note
    # And a symmetric run of the same n is unchanged.
    plain = _spec(short_root, n=3, supervisor=None, run_budget_usd=8.0)
    assert plain.session_budget_usd == pytest.approx(8.0 / 3)


def test_capacity_counts_every_session_the_run_starts(short_root):
    """~600 MB is what a session costs, and a coordinating session costs
    the same as a working one."""
    spec = _spec(short_root, n=3)
    run = fleet_mod.FleetRun(spec, FakeBackend(), force=True)
    note = run.prepare()
    assert "N=4" in note


def test_memory_off_is_drawn_from_the_workers_and_never_the_supervisor(
    short_root,
):
    """The supervisor holds the shape of the whole job across every
    worker's reply, which is exactly the continuity a memory-off agent
    does not have. A run that silently dealt the coordinator no memory
    would fail as "the supervisor forgot what it had already handed out"."""
    spec = _spec(
        short_root, n=4, memory=fleet_mod.MemoryPolicy(off_count=4)
    )
    assignments = fleet_mod.assign_for(spec)
    assert assignments[0].role == fleet_mod.ROLE_SUPERVISOR
    assert assignments[0].lore is True, "the supervisor must keep memory"
    assert [a.lore for a in assignments[1:]] == [False] * 4


async def test_the_supervisor_is_spawned_with_memory_on(short_root):
    """The env the draw actually turns into -- ``DOXA_LORE=0`` on the
    workers, absent on the supervisor."""
    backend = FakeBackend()
    spec = _spec(short_root, n=2, memory=fleet_mod.MemoryPolicy(off_count=2))
    run = fleet_mod.FleetRun(spec, backend, force=True)
    run.prepare()
    await run.spawn_all()
    assert "DOXA_LORE" not in backend.spawn_env[0]
    assert backend.spawn_env[1]["DOXA_LORE"] == "0"
    assert backend.spawn_env[2]["DOXA_LORE"] == "0"


# =======================================================================
# The protocol: workers first, then the supervisor
# =======================================================================


async def test_every_worker_is_briefed_before_the_supervisor_is_prompted(
    short_root,
):
    """THE ordering property of supervisor mode, and the exact inverse of
    the symmetric shuffle.

    The failure this catches: a supervisor prompted in the same breath as
    its workers, which can hand a task to a session that has not yet been
    told a task is coming -- and a peer message that arrives before the
    briefing lands ahead of it in the queue."""
    backend = FakeBackend()
    run = await _to_dispatch(_spec(short_root, n=3), backend)

    dispatches = [i for i, (kind, _) in enumerate(backend.log) if kind == "dispatch"]
    order = [backend.log[i][1] for i in dispatches]
    assert order == [1, 2, 3, 0], (
        f"workers must be briefed in slot order, supervisor last: {order}"
    )
    assert run.report.dispatch_order == (1, 2, 3, 0)
    assert all(s.phase == fleet_mod.PHASE_DISPATCHED for s in run.slots)


async def test_a_worker_briefing_names_the_supervisor_and_the_worker_itself(
    short_root,
):
    """Both ids, in every worker's own text: the worker has to be able to
    answer its supervisor by name in its first turn, and to identify
    itself in the ledger entry that answer becomes."""
    backend = FakeBackend()
    run = await _to_dispatch(_spec(short_root, n=3), backend)
    boss = run.slots[0]

    for slot in run.slots[1:]:
        text = backend.prompts[slot.index]
        assert str(boss.session_id) in text, "the supervisor is unnamed"
        assert str(slot.session_id) in text, "the worker cannot identify itself"
        assert f"worker {slot.index}" in text
        assert "peer_send" in text
        assert text.strip().endswith("ready")
        # A worker is told how the run is wired and NOTHING about the job.
        assert PROMPT not in text


async def test_the_supervisor_briefing_names_every_worker_and_ends_with_the_task(
    short_root,
):
    """A roster the supervisor would have to fetch is a roster it may
    decide not to fetch, and the operator's own words have to be the last
    thing in the prompt so that they are unambiguously separable from the
    harness's."""
    backend = FakeBackend()
    run = await _to_dispatch(_spec(short_root, n=3), backend)
    text = backend.prompts[0]

    for slot in run.slots[1:]:
        assert str(slot.session_id) in text, f"worker {slot.index} is unnamed"
        assert f"slot {slot.index:>3}" in text
    assert "peer_send" in text and "peer_list" in text
    assert fleet_mod.TASK_MARKER in text
    assert text.endswith(PROMPT)
    assert text.index(fleet_mod.TASK_MARKER) > text.index(str(run.slots[1].session_id))


async def test_an_interactive_supervisor_is_told_to_wait_for_the_operator(
    short_root,
):
    """With no prompt the task marker still appears and says so. A
    supervisor told nothing would otherwise invent a task, which at N
    workers is an expensive way to be wrong."""
    backend = FakeBackend()
    spec = _spec(short_root, prompt="", quiescence_timeout_s=0.2)
    run = await _to_dispatch(spec, backend)
    text = backend.prompts[0]

    assert fleet_mod.TASK_MARKER in text
    assert "attach" in text.lower() and "operator" in text.lower()
    assert "invent work" in text
    assert run.report.dispatch_order[-1] == 0


async def test_a_supervisor_that_never_armed_briefs_nobody(short_root):
    """Briefing four workers to report to a session that does not exist
    spends four turns to produce a run that cannot go anywhere. The
    manifest already carries the reason on the supervisor's own slot."""

    class NoSupervisor(FakeBackend):
        async def arm(self, slot, spec) -> None:
            if slot.index == 0:
                raise RuntimeError("the supervisor's socket never answered")
            await super().arm(slot, spec)

    backend = NoSupervisor()
    run = await _to_dispatch(_spec(short_root, n=3), backend)

    assert backend.prompts == {}
    assert run.report.dispatch_order == ()
    assert run.slots[0].phase == fleet_mod.PHASE_FAILED
    assert "never answered" in (run.slots[0].error or "")


# =======================================================================
# An interactive run ends when a human says so, not when it goes quiet
# =======================================================================


async def test_an_interactive_run_does_not_end_when_the_fleet_goes_quiet(
    short_root,
):
    """The failure this catches, and it is not subtle: every session goes
    idle seconds after being briefed, so a dwell would tear the run down
    -- five sessions and a worktree each -- while the operator was still
    reading the tab and had not yet typed anything."""
    backend = FakeBackend()  # every is_quiet() answers True immediately
    spec = _spec(short_root, prompt="", quiescence_timeout_s=None)
    run = await _to_dispatch(spec, backend)

    waiting = asyncio.create_task(run.await_quiescence())
    await asyncio.sleep(0.15)
    assert not waiting.done(), "a quiet interactive run ended itself"
    assert run.report.quiesced is False

    run.request_stop()
    quiesced = await asyncio.wait_for(waiting, timeout=2.0)

    assert quiesced is False, "a stop is not a quiescence"
    assert run.report.stopped is True
    assert [s.phase for s in run.slots] == [fleet_mod.PHASE_QUIET] * 4


async def test_an_interactive_run_with_an_explicit_deadline_still_ends(
    short_root,
):
    """``--quiescence-timeout`` is the other end an operator can ask for,
    and asking for it has to work in the one mode whose default is none."""
    backend = FakeBackend()
    spec = _spec(short_root, prompt="", quiescence_timeout_s=0.2)
    run = await _to_dispatch(spec, backend)

    quiesced = await asyncio.wait_for(run.await_quiescence(), timeout=5.0)

    assert quiesced is False
    assert all(s.phase == fleet_mod.PHASE_HUNG for s in run.slots)


async def test_a_prompted_supervisor_run_still_ends_on_quiet(short_root):
    """The other half: a run that was given a task has an end of its own,
    and supervisor mode must not have taken it away."""
    backend = FakeBackend()
    run = await _to_dispatch(_spec(short_root, n=2), backend)

    quiesced = await asyncio.wait_for(run.await_quiescence(), timeout=5.0)

    assert quiesced is True
    assert run.report.quiesced is True


async def test_an_interactive_run_ends_when_every_session_has_died(short_root):
    """A deadline-less wait must still be a bounded one when there is
    nothing left to wait for -- otherwise a run whose sessions all died is
    a coroutine nobody ever gets back."""

    class AllDead(FakeBackend):
        async def is_quiet(self, slot) -> bool:
            raise RuntimeError("socket gone")

    spec = _spec(short_root, prompt="", quiescence_timeout_s=None)
    run = await _to_dispatch(spec, AllDead())

    quiesced = await asyncio.wait_for(run.await_quiescence(), timeout=5.0)

    assert quiesced is False, "every session dying is an end, not a measurement"
    assert all(s.phase == fleet_mod.PHASE_HUNG for s in run.slots)


# =======================================================================
# What the manifest has to say about a run like this
# =======================================================================


async def test_the_manifest_records_the_mode_the_roles_and_the_supervisor(
    short_root,
):
    """"The supervisor integrated nothing" and "slot 0 integrated nothing"
    are the same observation only if the manifest says which slot was the
    supervisor -- and every other field in the document means something
    different under the other mode."""
    backend = FakeBackend()
    spec = _spec(short_root, n=3, run_id="sup-manifest")
    report = await asyncio.wait_for(
        fleet_mod.FleetRun(spec, backend, force=True).run(), timeout=10.0
    )
    manifest = json.loads(spec.manifest_path.read_text(encoding="utf-8"))

    assert manifest["mode"] == "supervisor"
    assert manifest["interactive"] is False
    assert manifest["supervisor"] == {
        "slot": 0, "session_id": "sess-0000",
        "engine": "claude", "model": "opus", "cwd": None,
    }
    assert manifest["spec"]["n"] == 3
    assert manifest["spec"]["sessions"] == 4
    assert manifest["spec"]["supervisor"] == {"engine": "claude", "model": "opus"}
    assert [s["role"] for s in manifest["slots"]] == [
        "supervisor", "worker", "worker", "worker",
    ]
    assert [a["role"] for a in manifest["assignments"]] == [
        "supervisor", "worker", "worker", "worker",
    ]
    # The protocol's own order, recorded like any other dispatch order.
    assert manifest["dispatch_order"] == [1, 2, 3, 0]
    assert report.summary().startswith("run sup-manifest [supervisor]")
    assert "3 workers + supervisor" in report.summary()


async def test_the_manifest_records_where_each_session_actually_worked(
    short_root,
):
    """A fleet's worktrees live under the RUN's own ``DOXA_HOME``, so
    nothing outside the run can find them by looking. The slot's ``cwd``
    is the run's only record of where the work ended up -- which branch,
    in which checkout, the operator has to go and read afterwards."""

    class WithWorktrees(FakeBackend):
        async def spawn(self, slot, spec) -> None:
            await super().spawn(slot, spec)
            slot.cwd = f"{spec.home}/worktrees/repo-{slot.index}"

    spec = _spec(short_root, n=2, run_id="sup-cwd")
    await asyncio.wait_for(
        fleet_mod.FleetRun(spec, WithWorktrees(), force=True).run(), timeout=10.0
    )
    manifest = json.loads(spec.manifest_path.read_text(encoding="utf-8"))

    assert [s["cwd"] for s in manifest["slots"]] == [
        f"{spec.home}/worktrees/repo-{i}" for i in range(3)
    ]
    assert manifest["supervisor"]["cwd"] == f"{spec.home}/worktrees/repo-0"


async def test_an_interactive_run_says_so_in_its_manifest(short_root):
    """The tab's own mode line reads this, and so does anyone opening the
    run afterwards and wondering why it never quiesced."""
    spec = _spec(short_root, prompt="", quiescence_timeout_s=0.2,
                 run_id="sup-interactive")
    await asyncio.wait_for(
        fleet_mod.FleetRun(spec, FakeBackend(), force=True).run(), timeout=10.0
    )
    manifest = json.loads(spec.manifest_path.read_text(encoding="utf-8"))

    assert manifest["mode"] == "supervisor"
    assert manifest["interactive"] is True
    assert manifest["spec"]["prompt"] == ""
    assert manifest["spec"]["quiescence_timeout_s"] == 0.2


# =======================================================================
# The one place a slot NUMBER is spoken aloud
# =======================================================================


def test_the_slot_range_an_attach_names_includes_the_supervisor():
    """The failure this catches: ``/fleet attach``'s refusal naming "0 to
    n-1" in a run whose slots are 0..n, so the range excludes exactly the
    slot an operator most often wants -- the supervisor's.

    Asserted on the arithmetic the message is built from, because the
    message is one f-string over it and the property is the arithmetic."""
    spec = fleet_mod.FleetSpec(
        prompt="x", cwd="/repo", n=3, pool=POOL,
        supervisor=fleet_mod.ModelSlot(engine="claude", model="opus"),
    )
    assert spec.session_count - 1 == 3
    assert [a.index for a in fleet_mod.assign_for(spec)] == [0, 1, 2, 3]
    plain = fleet_mod.FleetSpec(prompt="x", cwd="/repo", n=3, pool=POOL)
    assert plain.session_count - 1 == 2
