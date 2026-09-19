# SPDX-License-Identifier: AGPL-3.0-only
"""Issue #56 -- a fleet slot waits forever on a permission ask nobody can
answer.

THE DEFECT, and why each test below is the shape it is.
``doxa.engine.SessionEngine._on_can_use_tool`` parks on a ``needs_input``
event and waits for a human, forever, for the two cases the Claude CLI
would have shown its own interactive prompt for. In a TUI a pane answers.
In a fleet nobody did: ``doxa.fleet._drain`` consumed the event and threw
it away with everything else, and no fleet code ever called
``EngineClient.answer_needs_input``. The slot sat until the quiescence
deadline -- measured at 6 min 45 s on ``mcp__doxa__peer_list``.

So the properties under test are not "the desk has a method". They are:

* an ask arriving in a running fleet becomes a DECISION, within a bounded
  time, rather than a session that looks idle and is not;
* the DEFAULT decision is a refusal, and the refusal names the flag that
  would have allowed it -- a harness that silently allowed would be
  handing every spawned session an approval the operator refused to give
  one interactive session;
* the operator's claim is explicit, narrow and RECORDED: ``--approve
  peer`` covers this run's own peer tools and nothing else, and both the
  posture and every decision land in the manifest;
* a parked ask is VISIBLE -- in the manifest, in the fleet tab, and named
  by ``/fleet attach <slot>`` -- because a blocked slot is indistinguish-
  able from a finished one to everything else a watcher can see;
* a human still beats the clock, and the run records that a person
  decided it rather than the policy.

No process is spawned and no token is spent: the client seam is
``DaemonBackend.connect``, which exists so this file can drive the real
``arm``/``_drain``/``ApprovalDesk`` wiring against a scripted session.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import shutil
import tempfile

import pytest

from doxa import fleet as fleet_mod
from doxa import fleetview as fleetview_mod
from doxa.events import EngineEvent


POOL = (fleet_mod.ModelSlot(engine="claude", model="sonnet", weight=1),)
PROMPT = "list your peers, then say who you would ask for help."

_RUN_IDS = itertools.count()


@pytest.fixture
def short_root():
    """A run root short enough for an AF_UNIX socket to live under it --
    the same constraint ``tests/test_fleet.py`` documents."""
    root = tempfile.mkdtemp(prefix="dxfa", dir="/tmp")
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _spec(short_root, **kw):
    kw.setdefault("prompt", PROMPT)
    kw.setdefault("cwd", short_root)
    kw.setdefault("pool", POOL)
    kw.setdefault("root", short_root)
    kw.setdefault("run_id", f"a{next(_RUN_IDS)}")
    kw.setdefault("n", 2)
    kw.setdefault("seed", 7)
    kw.setdefault("run_budget_usd", 10.0)
    kw.setdefault("quiescence_timeout_s", 5.0)
    kw.setdefault("quiet_dwell_s", 0.0)
    kw.setdefault("poll_interval_s", 0.01)
    kw.setdefault("stop_timeout_s", 0.2)
    kw.setdefault("approval_grace_s", 0.2)
    return fleet_mod.FleetSpec(**kw)


# =======================================================================
# A scripted session: it parks on an ask exactly the way a real one does
# =======================================================================


class FakeClient:
    """One armed slot's ``EngineClient``, scripted rather than connected.

    It models the ONE behaviour the deadlock is made of: while an ask is
    parked the session is NOT idle (the SDK is inside a control request,
    so the daemon reports ``running``), and it goes idle only once the ask
    has an answer. A fake that always reported idle would let the run
    quiesce straight past the defect and prove nothing."""

    def __init__(self, asks: "list[dict]" = ()) -> None:
        self.asks = [dict(a) for a in asks]
        self.answers: "list[tuple[str, dict]]" = []
        self.oob: "asyncio.Queue" = asyncio.Queue()
        self.dispatched: "list[str]" = []
        self.stopped = False
        self._parked = 0
        self._closed = asyncio.Event()

    # -- the surface DaemonBackend uses --------------------------------

    async def dispatch(self, prompt: str) -> dict:
        self.dispatched.append(prompt)
        # Dispatch is what makes the model reach for a tool, so this is
        # where the scripted asks arrive -- on the out-of-band stream, the
        # same one SessionEngine._wait_for_answer puts them on.
        for ask in self.asks:
            self._parked += 1
            self.oob.put_nowait(EngineEvent("needs_input", dict(ask)))
        return {"ok": True}

    async def answer_needs_input(self, req_id: str, answer: dict) -> bool:
        self.answers.append((req_id, dict(answer)))
        self._parked = max(0, self._parked - 1)
        # The engine's own finally publishes this on the SAME stream --
        # see SessionEngine._wait_for_answer. The desk has to cope with
        # the echo of its own answer.
        self.oob.put_nowait(EngineEvent("needs_input_resolved", {"id": req_id}))
        return True

    async def refresh_status(self) -> dict:
        return {"running": bool(self._parked), "queued": 0}

    async def next_turn_event(self):
        await self._closed.wait()
        return None

    async def peer_events(self):
        while True:
            item = await self.oob.get()
            if item is None:
                return
            yield item

    async def stop(self) -> None:
        self.stopped = True
        self._closed.set()
        self.oob.put_nowait(None)

    # -- what an ATTACHED operator would do ----------------------------

    def answer_from_elsewhere(self, req_id: str) -> None:
        """Another client answered this id: the engine resolves the future
        and broadcasts ``needs_input_resolved`` to every attached client,
        including the fleet's."""
        self.oob.put_nowait(EngineEvent("needs_input_resolved", {"id": req_id}))


class ScriptedBackend(fleet_mod.DaemonBackend):
    """The REAL ``DaemonBackend`` with its two process-touching ends
    replaced -- ``spawn`` and ``connect``. Everything between them (arm,
    the drain, the approval desk, stop) is the shipped code."""

    def __init__(self, asks_by_slot: "dict[int, list[dict]]" = None) -> None:
        super().__init__()
        self.asks_by_slot = asks_by_slot or {}
        self.clients: "dict[int, FakeClient]" = {}
        self._next = itertools.count()

    async def spawn(self, slot, spec) -> None:
        slot.session_id = f"sess-{slot.index:04d}"
        slot.pid = None  # nothing to kill, and nothing for teardown to probe
        # The slot index travels in the socket NAME, because that is the
        # only thing `connect` is handed -- the same one-argument contract
        # the real EngineClient construction has.
        slot.socket_path = f"/tmp/fake-{slot.index}.sock"

    async def connect(self, socket_path: str):
        index = int(str(socket_path).rsplit("-", 1)[1].split(".")[0])
        client = FakeClient(self.asks_by_slot.get(index, []))
        self.clients[index] = client
        return client

    async def kill(self, slot) -> bool:
        return True


def _permission_ask(req_id: str, tool: str) -> dict:
    """The event ``SessionEngine._request_permission`` queues."""
    return {
        "id": req_id, "kind": "permission", "tool_name": tool,
        "input_summary": f"{tool}(...)", "title": f"Claude wants to use {tool}",
        "display_name": tool, "description": "",
    }


# =======================================================================
# The deadlock itself
# =======================================================================


async def test_an_ask_in_a_fleet_run_becomes_a_decision_instead_of_a_deadlock(
    short_root,
):
    """THE defect. A slot parks on a permission ask, nothing in the fleet
    answers it, and the run burns its whole quiescence deadline on a
    session that is neither working nor finished.

    On the fixed code the ask is decided inside the grace window and the
    run quiesces normally; the slot never reaches ``hung``."""
    asks = {0: [_permission_ask("r1", "mcp__doxa__peer_list")]}
    backend = ScriptedBackend(asks)
    spec = _spec(short_root)
    report = await fleet_mod.FleetRun(spec, backend, force=True).run()

    assert backend.clients[0].answers, (
        "nobody answered the parked ask -- this is the deadlock issue #56 "
        "names, and the run sat on it until its deadline"
    )
    assert report.quiesced is True
    assert [s.phase for s in report.slots] == [
        fleet_mod.PHASE_STOPPED, fleet_mod.PHASE_STOPPED,
    ]


async def test_the_default_posture_refuses_and_names_the_flag(short_root):
    """A run that was given no ``--approve`` approves NOTHING, and the
    refusal the model receives says which flag would have allowed the
    call. A bare denial teaches the operator nothing; this one is how
    they learn their run needed a flag."""
    asks = {0: [_permission_ask("r1", "mcp__doxa__peer_list")]}
    backend = ScriptedBackend(asks)
    spec = _spec(short_root)
    assert spec.approve == fleet_mod.APPROVE_NONE
    report = await fleet_mod.FleetRun(spec, backend, force=True).run()

    (req_id, answer), = backend.clients[0].answers
    assert req_id == "r1"
    assert answer["decision"] == "deny"
    assert "--approve peer" in answer["reason"]
    assert "--approve all" in answer["reason"]
    assert "/fleet attach 0" in answer["reason"]
    assert report.approval_counts()["refused"] == 1
    assert report.approval_counts()["auto_approved"] == 0


async def test_approve_peer_allows_the_runs_own_peer_tools_and_nothing_else(
    short_root,
):
    """The narrow claim, and the one that actually unblocks a supervisor
    run: ``peer_list``/``peer_send`` are reads and writes of the run's own
    ledger, not of the repository. A Bash call in the same run is still
    refused, and the refusal says the policy did not cover it."""
    asks = {0: [
        _permission_ask("peer", "mcp__doxa__peer_list"),
        _permission_ask("bash", "Bash"),
    ]}
    backend = ScriptedBackend(asks)
    spec = _spec(short_root, approve=fleet_mod.APPROVE_PEER)
    await fleet_mod.FleetRun(spec, backend, force=True).run()

    decisions = dict(backend.clients[0].answers)
    assert decisions["peer"]["decision"] == "allow"
    assert decisions["bash"]["decision"] == "deny"
    assert "--approve peer" in decisions["bash"]["reason"]
    assert "--approve all" in decisions["bash"]["reason"]


async def test_approve_all_is_the_blanket_claim_and_covers_an_ordinary_tool(
    short_root,
):
    asks = {0: [_permission_ask("bash", "Bash")]}
    backend = ScriptedBackend(asks)
    spec = _spec(short_root, approve=fleet_mod.APPROVE_ALL)
    report = await fleet_mod.FleetRun(spec, backend, force=True).run()

    (_, answer), = backend.clients[0].answers
    assert answer["decision"] == "allow"
    assert report.approval_counts()["auto_approved"] == 1
    assert report.approval_counts()["refused"] == 0


async def test_a_question_and_a_spawn_are_never_auto_approved_even_by_all(
    short_root,
):
    """Two carve-outs, each for its own reason.

    A question is not a permission: ``allow`` is not a reply to "which
    branch?", so a policy that answered one would be inventing an answer.
    A ``spawn_session`` is DOXA's own gate rather than the CLI's, and
    ``SessionEngine._confirm_spawn`` already says why -- "a fleet spawning
    further fleet with nobody watching is exactly the outcome nobody asked
    for". ``--approve all`` means every tool call the CLI asks about;
    starting more sessions is not one of those."""
    asks = {0: [
        {"id": "q", "kind": "ask_user", "tool_name": "AskUserQuestion",
         "questions": [{"question": "which branch?"}]},
        {"id": "s", "kind": "spawn", "tool_name": "spawn_session",
         "title": "start a second DOXA session in this repo?"},
    ]}
    backend = ScriptedBackend(asks)
    spec = _spec(short_root, approve=fleet_mod.APPROVE_ALL)
    await fleet_mod.FleetRun(spec, backend, force=True).run()

    decisions = dict(backend.clients[0].answers)
    # A question is DECLINED -- the graceful path the engine documents --
    # rather than denied as though it had been a tool call.
    assert decisions["q"]["declined"] is True
    assert "--approve only ever approves tool calls" in decisions["q"]["reason"]
    assert decisions["s"]["decision"] == "deny"
    assert "never auto-approves" in decisions["s"]["reason"]


async def test_an_operator_who_answers_beats_the_clock_and_is_recorded_as_the_decider(
    short_root,
):
    """``/fleet attach <slot>`` is a real answer path, which means two
    things have to hold: the policy's grace timer must be CANCELLED by a
    human's answer rather than racing it, and the run must record that a
    person -- not the policy -- decided."""
    slot = fleet_mod.Slot(assignment=fleet_mod.assign_for(_spec(short_root))[0])
    spec = _spec(short_root, approval_grace_s=30.0)
    client = FakeClient()
    desk = fleet_mod.ApprovalDesk(slot, spec, client)

    await desk.on_event(EngineEvent(
        "needs_input", _permission_ask("r1", "mcp__doxa__peer_list"),
    ))
    assert "r1" in slot.pending_asks
    await desk.on_event(EngineEvent("needs_input_resolved", {"id": "r1"}))

    assert not slot.pending_asks
    assert not client.answers, "the policy answered over a human's decision"
    (record,) = slot.approvals
    assert record["by"] == "operator"
    assert record["decision"] == "answered"
    desk.close()


async def test_an_ask_still_open_at_teardown_is_written_down_not_dropped(
    short_root,
):
    """A run that ended mid-ask has to say so. A manifest that silently
    dropped the pending entry would read as though nothing was ever
    asked, which is the same misreading the tab suffers from."""
    slot = fleet_mod.Slot(assignment=fleet_mod.assign_for(_spec(short_root))[0])
    spec = _spec(short_root, approval_grace_s=30.0)
    desk = fleet_mod.ApprovalDesk(slot, spec, FakeClient())
    await desk.on_event(EngineEvent(
        "needs_input", _permission_ask("r1", "Bash"),
    ))
    desk.close()

    assert not slot.pending_asks
    (record,) = slot.approvals
    assert record["by"] == "teardown"
    assert record["decision"] == "unanswered"


# =======================================================================
# The record: a run's approval posture must be readable after the fact
# =======================================================================


async def test_the_manifest_records_the_posture_and_every_decision(short_root):
    """``--allow-unbudgeted``'s precedent, applied: what a run was allowed
    to do on the operator's behalf belongs in the run's own record, not in
    a shell history."""
    asks = {0: [_permission_ask("peer", "mcp__doxa__peer_list"),
                _permission_ask("bash", "Bash")]}
    backend = ScriptedBackend(asks)
    spec = _spec(short_root, approve=fleet_mod.APPROVE_PEER)
    report = await fleet_mod.FleetRun(spec, backend, force=True).run()

    manifest = json.loads(spec.manifest_path.read_text(encoding="utf-8"))
    assert manifest["spec"]["approve"] == "peer"
    assert manifest["spec"]["approval_grace_s"] == spec.approval_grace_s
    approvals = manifest["approvals"]
    assert approvals["policy"] == "peer"
    assert approvals["asked"] == 2
    assert approvals["auto_approved"] == 1
    assert approvals["refused"] == 1
    assert "peer tools are auto-approved" in approvals["posture"]

    slot0 = next(s for s in manifest["slots"] if s["index"] == 0)
    by_tool = {r["tool"]: r for r in slot0["approvals"]}
    assert by_tool["mcp__doxa__peer_list"]["by"] == "policy"
    assert by_tool["Bash"]["by"] == "timeout"
    assert by_tool["Bash"]["delivered"] is True
    assert report.summary().count("REFUSED") == 1


def test_a_default_run_records_the_posture_it_always_had(short_root):
    """A run that asks for nothing must read exactly as it always has --
    and still SAY what it would have done, so "nothing was asked" and "we
    do not know what would have happened" are distinguishable."""
    report = fleet_mod.RunReport(
        run_id="r", spec=_spec(short_root), slots=[],
    )
    obj = report.to_obj()
    assert obj["approvals"]["policy"] == "none"
    assert obj["approvals"]["asked"] == 0
    assert "nothing is auto-approved" in obj["approvals"]["posture"]


# =======================================================================
# The ask has to be VISIBLE while it is parked
# =======================================================================


def test_a_parked_ask_reaches_the_fleet_tab_with_the_command_that_answers_it():
    """A parked slot is idle in every way a watcher can measure: no turn,
    no ledger line, ``is_quiet`` says yes. So the tab is the only place it
    can be told apart from a finished agent, and it has to name the slot,
    the tool and the command."""
    snapshot = fleetview_mod.RunSnapshot(
        run_root="/tmp/nowhere",
        manifest={
            "run_id": "r1", "live": True, "started_at": "2026-09-19T12:00:00Z",
            "approvals": {
                "policy": "none", "grace_s": 300.0, "asked": 1, "pending": 1,
                "auto_approved": 0, "refused": 0, "answered": 0,
                "ended_unanswered": 0,
                "posture": fleet_mod.approval_posture("none", 300.0),
            },
            "spec": {"n": 2, "cwd": "/repo", "seed": 1, "memory_off": 0},
            "slots": [{
                "index": 3, "role": "worker", "phase": "dispatched",
                "assignment": {"engine": "claude", "model": "opus", "lore": True},
                "pending_asks": [{
                    "id": "r1", "kind": "permission",
                    "tool": "mcp__doxa__peer_list",
                    "summary": "Claude wants to use mcp__doxa__peer_list",
                    "asked_at": "2026-09-19T12:00:10Z", "grace_s": 300.0,
                }],
                "approvals": [],
            }],
        },
        ledger=[],
    )
    text = fleetview_mod.render(snapshot, now=1789000000.0)
    assert "WAITING ON YOU" in text
    assert "mcp__doxa__peer_list" in text
    assert "/fleet attach 3" in text
    assert "--approve none" in text


def test_a_run_nobody_asked_anything_of_shows_no_waiting_banner():
    """The banner must not appear over nothing: most runs are never asked
    anything, and a heading with an empty list under it teaches a reader
    to stop looking at the heading."""
    snapshot = fleetview_mod.RunSnapshot(
        run_root="/tmp/nowhere",
        manifest={
            "run_id": "r2", "live": True, "started_at": "2026-09-19T12:00:00Z",
            "spec": {"n": 1, "cwd": "/repo", "seed": 1, "memory_off": 0},
            "slots": [{
                "index": 0, "role": "worker", "phase": "dispatched",
                "assignment": {"engine": "claude", "model": "opus"},
                "pending_asks": [], "approvals": [],
            }],
        },
        ledger=[],
    )
    text = fleetview_mod.render(snapshot)
    assert "WAITING ON YOU" not in text
    assert "permission asks:" not in text
    # A run written before --approve existed says so rather than claiming
    # a posture it never had.
    assert "predates --approve" in text


# =======================================================================
# The flag: its own words, refused when misspelled
# =======================================================================


def test_approve_is_its_own_flag_on_the_one_parser_both_front_ends_use():
    """``/fleet start`` and ``doxa-fleet`` share one parser, so a flag that
    lands on the command line and not in the TUI is impossible by
    construction -- the drift build_parser exists to prevent."""
    spec, _ = fleet_mod.spec_from_argv(
        ["--pool", "claude:sonnet@1", "--prompt", "x",
         "--approve", "peer", "--approval-grace", "45"],
        cwd="/tmp",
    )
    assert spec.approve == fleet_mod.APPROVE_PEER
    assert spec.approval_grace_s == 45.0

    bare, _ = fleet_mod.spec_from_argv(
        ["--pool", "claude:sonnet@1", "--prompt", "x"], cwd="/tmp",
    )
    assert bare.approve == fleet_mod.APPROVE_NONE
    assert bare.approval_grace_s == fleet_mod.APPROVAL_GRACE_S


def test_an_unknown_approve_policy_is_refused_rather_than_failing_open():
    """``may_auto_approve`` returns False for an unknown policy, which is
    the safe direction -- but a run whose manifest recorded
    ``approve="peeer"`` would be a run whose posture nobody can read. So
    both front ends refuse it."""
    with pytest.raises(fleet_mod.FleetArgsError):
        fleet_mod.spec_from_argv(
            ["--pool", "claude:sonnet@1", "--prompt", "x", "--approve", "peeer"],
            cwd="/tmp",
        )
    with pytest.raises(ValueError, match=r"unknown --approve policy"):
        fleet_mod.FleetSpec(
            prompt="x", cwd="/tmp", pool=POOL, approve="peeer",
        )


def test_the_peer_allowlist_is_pinned_to_the_names_the_model_actually_sees():
    """``--approve peer`` is spelled out in doxa.fleet rather than derived
    from doxa.operators, which this module deliberately does not import.
    That is a duplication, so it is pinned: renaming a peer tool fails
    here instead of silently narrowing the policy to nothing."""
    from doxa import operators as operators_mod

    offered = {
        f"mcp__{operators_mod.SDK_SERVER_NAME}__{name}"
        for name in ("peer_list", "peer_history", "peer_send")
    }
    assert fleet_mod.PEER_TOOLS == offered
    known = set(operators_mod.OPERATORS) | set(operators_mod.WRITE_OPERATORS)
    for name in fleet_mod.PEER_TOOLS:
        assert operators_mod.registry_name(name) in known


async def test_a_shell_run_writes_the_parked_ask_out_while_it_is_still_parked(
    short_root,
):
    """The TUI has a manifest heartbeat; ``doxa-fleet`` from a shell writes
    the manifest once, at the end. Without a write when an ask parks, a run
    blocked on one would be unreadable from outside for exactly as long as
    it was blocked -- the window a reader needs it most. So the quiescence
    loop writes on CHANGE, and this is the file a watcher then reads."""
    asks = {0: [_permission_ask("r1", "Bash")]}
    backend = ScriptedBackend(asks)
    spec = _spec(
        short_root, approval_grace_s=30.0, quiescence_timeout_s=10.0,
    )
    run = fleet_mod.FleetRun(spec, backend, force=True)
    task = asyncio.ensure_future(run.run())
    try:
        text = ""
        for _ in range(200):
            await asyncio.sleep(0.05)
            snapshot = fleetview_mod.RunSnapshot.read(spec.run_root)
            text = fleetview_mod.render(snapshot)
            if "WAITING ON YOU" in text:
                break
        assert "WAITING ON YOU" in text, text
        assert "Bash" in text
        assert "/fleet attach 0" in text
    finally:
        run.request_stop()
        await task


def test_the_parked_ask_row_does_not_repeat_the_tool_name_as_its_summary():
    """``doxa.engine`` falls back to the tool name when the CLI gave no
    prompt sentence, which under an MCP tool with no arguments is the
    column immediately to its left. A run's live breakdown also has to
    lead with the count that is still actionable: four zeros that do not
    add up to the total is a reader doing arithmetic to find the one fact
    they needed."""
    snapshot = fleetview_mod.RunSnapshot(
        run_root="/tmp/nowhere",
        manifest={
            "run_id": "r3", "live": True, "started_at": "2026-09-19T12:00:00Z",
            "approvals": {
                "policy": "none", "grace_s": 30.0, "asked": 1, "pending": 1,
                "auto_approved": 0, "refused": 0, "answered": 0,
                "ended_unanswered": 0, "posture": "x",
            },
            "spec": {"n": 1, "cwd": "/repo", "seed": 1, "memory_off": 0},
            "slots": [{
                "index": 0, "role": "worker", "phase": "dispatched",
                "assignment": {"engine": "claude", "model": "opus"},
                "pending_asks": [{
                    "id": "r1", "kind": "permission",
                    "tool": "mcp__doxa__peer_list",
                    "summary": "mcp__doxa__peer_list",
                    "asked_at": "2026-09-19T12:00:02Z", "grace_s": 30.0,
                }],
                "approvals": [],
            }],
        },
        ledger=[],
    )
    text = fleetview_mod.render(snapshot, now=1789000000.0)
    assert text.count("mcp__doxa__peer_list") == 1
    assert "permission asks: 1 — 1 WAITING," in text
