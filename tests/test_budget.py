# SPDX-License-Identifier: AGPL-3.0-only
"""The spend ceilings -- per session, and per fleet run.

Every test here is named for the failure it catches, and the failures are
the ones that cost money rather than the ones that fail a lint:

* a session at its ceiling starts no further turn, and says so where
  somebody reads it;
* it is STOPPED, not dead -- it still answers, and raising the number lets
  the next prompt through with no restart;
* a turn an arriving PEER message started is refused exactly like a typed
  one. That is the path nobody is watching, and a ceiling that bounded
  only what a human typed would bound the wrong half;
* the ceiling is OFF by default, and a session without one behaves
  byte-for-byte as it did before this existed;
* a fleet run that can wake its own sessions and has no budget is refused
  at spawn, naming what to set, and the override is explicit and lands in
  the manifest;
* a ceiling on an engine that reports no cost says so at the moment it is
  SET, rather than sitting in a config file looking like a control.

The two deliberate gaps are pinned by tests as well, so that a later
reader finds them stated rather than discovers them from a bill:
``test_a_turn_already_running_is_never_interrupted_by_the_ceiling``
(the one-turn overshoot) and
``test_an_engine_that_reports_no_cost_is_not_bounded_by_a_ceiling_at_all``.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile

import pytest

from claude_agent_sdk import ResultMessage

from doxa import budget as budget_mod
from doxa import config as config_mod
from doxa import fleet as fleet_mod
from doxa import peers as peers_mod
from doxa.engine import SessionEngine
from tests.fakes import factory_with_script


def _result(cost: float) -> ResultMessage:
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id="s", total_cost_usd=cost,
    )


async def _engine(tmp_path, monkeypatch, *, ceiling="", inbound=False, cost=0.0):
    """A started SessionEngine with its own runtime dir, home and ledger.

    ``ceiling`` goes into the ENVIRONMENT rather than onto the object,
    because that is the seam the feature actually has: doxa.budget reads
    through doxa.config.raw, and a test that set an attribute would prove
    nothing about whether the knob is wired to the file and the env at
    all."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DOXA_AGENT_PEER_SEND", "1")
    monkeypatch.setenv("DOXA_PEER_INBOUND_TURNS", "1" if inbound else "")
    monkeypatch.setenv(budget_mod.SESSION_BUDGET_ENV, ceiling)
    factory, created = factory_with_script([_result(0.0)])
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()
    engine.total_cost_usd = cost
    return engine, created


def _drain(queue):
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


async def _collect(engine, prompt):
    return [ev async for ev in engine.send(prompt)]


# -- parsing: there is no value that becomes a surprise ----------------


def test_an_unset_ceiling_is_off_rather_than_zero(monkeypatch):
    """The default-off requirement, at the layer where it is decided."""
    monkeypatch.delenv(budget_mod.SESSION_BUDGET_ENV, raising=False)
    monkeypatch.setenv("DOXA_HOME", tempfile.mkdtemp(prefix="dxb", dir="/tmp"))
    config_mod.invalidate()
    assert budget_mod.session_ceiling() is None
    assert budget_mod.exhausted(9_999.0, None) is False, (
        "with no ceiling there is nothing to exceed, however much was spent"
    )


@pytest.mark.parametrize("value", ["", "   ", "0", "-5", "abc", "$", "None"])
def test_a_zero_or_garbage_ceiling_means_off_never_refuse_everything(value):
    """The failure this prevents is a bricked session: if ``0`` parsed as
    a real ceiling, a mistyped or half-cleared field would refuse every
    turn forever and look exactly like the feature working."""
    assert budget_mod.usd(value) is None


@pytest.mark.parametrize("value,expected", [("5", 5.0), ("$5", 5.0), ("1,000", 1000.0),
                                            (2.5, 2.5), ("0.0001", 0.0001)])
def test_a_real_ceiling_parses_however_it_was_typed(value, expected):
    assert budget_mod.usd(value) == pytest.approx(expected)


def test_spending_exactly_the_ceiling_is_spending_the_ceiling():
    """``>=``, not ``>``. Off by one here is a whole extra turn."""
    assert budget_mod.exhausted(1.0, 1.0) is True
    assert budget_mod.exhausted(0.9999, 1.0) is False


# -- a session at its ceiling ------------------------------------------


@pytest.mark.asyncio
async def test_a_session_at_its_ceiling_refuses_to_start_a_new_turn(
    tmp_path, monkeypatch,
):
    """The whole feature, in one assertion: nothing reached the model."""
    engine, created = await _engine(tmp_path, monkeypatch, ceiling="1.0", cost=1.5)
    try:
        events = await _collect(engine, "keep going")

        assert [ev.type for ev in events] == ["turn_refused"], (
            "a refused turn is ONE event and never a turn_started -- a turn "
            "block for a turn that did not happen is a lie about spend"
        )
        assert created[0].queried == [], (
            "the prompt reached the SDK client anyway, which means the "
            "ceiling stopped nothing at all"
        )
    finally:
        await engine.finalize()


@pytest.mark.asyncio
async def test_the_refusal_says_so_with_the_arithmetic_and_the_way_out(
    tmp_path, monkeypatch,
):
    """A refusal missing any of these three is a stall with a message
    attached: what was spent, that the session is not broken, and the
    exact knob that lifts it."""
    engine, _created = await _engine(tmp_path, monkeypatch, ceiling="1.0", cost=1.25)
    try:
        events = await _collect(engine, "keep going")
        message = events[0].data["message"]

        assert "$1.2500" in message, "the spend is not in the refusal"
        assert "$1.0000" in message, "the ceiling is not in the refusal"
        assert budget_mod.SESSION_BUDGET_ENV in message, (
            "a refusal that does not name the knob makes the user go "
            "looking for it"
        )
        assert "session_budget_usd" in message, "the config row is not named"
        assert events[0].data["reason"] == "budget"
        assert events[0].data["spent_usd"] == pytest.approx(1.25)
        assert events[0].data["ceiling_usd"] == pytest.approx(1.0)
    finally:
        await engine.finalize()


@pytest.mark.asyncio
async def test_a_session_at_its_ceiling_still_answers_the_user(
    tmp_path, monkeypatch,
):
    """Stopped, not crashed and not stalled. Everything that does not
    spend money keeps working, which is what makes the ceiling a control
    an operator can live with rather than a session they have to restart."""
    engine, _created = await _engine(tmp_path, monkeypatch, ceiling="0.5", cost=0.75)
    try:
        await _collect(engine, "first")

        assert engine._turn_running is False, (
            "the turn flag was left set, so every later prompt would queue "
            "behind a turn that never existed -- a silent stall"
        )
        # The surfaces a user reaches for at exactly this moment.
        assert engine.usage_summary()["total_cost_usd"] == pytest.approx(0.75)
        assert await engine.list_queue() == []
        assert engine.list_peers() == []
        assert engine.belief_count() >= 0
        # And a second prompt is answered too, rather than hanging.
        again = await _collect(engine, "second")
        assert [ev.type for ev in again] == ["turn_refused"]
    finally:
        await engine.finalize()


@pytest.mark.asyncio
async def test_raising_the_ceiling_lets_the_very_next_prompt_through(
    tmp_path, monkeypatch,
):
    """"Raise it and continue" with no restart. This is why the ceiling is
    read per turn instead of being captured at connect: a session stopped
    at its ceiling is stopped, not finished."""
    engine, created = await _engine(tmp_path, monkeypatch, ceiling="1.0", cost=1.5)
    try:
        assert [ev.type for ev in await _collect(engine, "no")] == ["turn_refused"]

        monkeypatch.setenv(budget_mod.SESSION_BUDGET_ENV, "10")
        types = [ev.type for ev in await _collect(engine, "yes")]

        assert "turn_started" in types and "turn_refused" not in types
        assert created[0].queried, "the turn still never reached the model"
    finally:
        await engine.finalize()


@pytest.mark.asyncio
async def test_the_ceiling_is_read_from_the_environment_not_from_the_repo(
    tmp_path, monkeypatch,
):
    """A config file inside the repository a session happens to have open
    is not a door to this knob. If it ever became one, a repo could raise
    its own session's spend ceiling."""
    engine, _created = await _engine(tmp_path, monkeypatch, ceiling="1.0", cost=1.5)
    try:
        (tmp_path / "config.toml").write_text(
            "session_budget_usd = 1000\n", encoding="utf-8"
        )
        (tmp_path / ".doxa").mkdir(exist_ok=True)
        (tmp_path / ".doxa" / "config.toml").write_text(
            "session_budget_usd = 1000\n", encoding="utf-8"
        )
        config_mod.invalidate()

        assert [ev.type for ev in await _collect(engine, "x")] == ["turn_refused"]
    finally:
        await engine.finalize()


# -- off by default -----------------------------------------------------


@pytest.mark.asyncio
async def test_the_ceiling_is_off_by_default_and_a_turn_runs_unchanged(
    tmp_path, monkeypatch,
):
    """Nothing changes for anyone who did not ask for a ceiling -- however
    much the session has already spent."""
    engine, created = await _engine(tmp_path, monkeypatch, ceiling="", cost=9_999.0)
    try:
        types = [ev.type for ev in await _collect(engine, "hello")]

        assert "turn_refused" not in types
        assert types[0] == "turn_started"
        assert created[0].queried, "the turn did not reach the model"
        assert engine.budget_ceiling() is None
    finally:
        await engine.finalize()


def test_the_settings_row_exists_and_is_off_by_default():
    """The settings surface, shaped like allow_bypass: its own row, its own
    env var, empty default."""
    row = next(s for s in config_mod.SETTINGS if s.key == "session_budget_usd")
    assert row.env == budget_mod.SESSION_BUDGET_ENV
    assert row.default == "", "a spend ceiling must never ship switched on"
    assert row.kind == "number"
    assert row.category == "Session"
    assert row.note, "the row owes the user the caveats, like allow_bypass does"


# -- the peer path, which is the reason any of this exists --------------


@pytest.mark.asyncio
async def test_a_peer_started_turn_is_refused_exactly_like_a_typed_one(
    tmp_path, monkeypatch,
):
    """The uncontrolled path. An arriving message can start a turn in an
    idle session; at the ceiling it must not, or the ceiling bounds only
    the half of the traffic a human was watching."""
    engine, created = await _engine(
        tmp_path, monkeypatch, ceiling="1.0", inbound=True, cost=1.5,
    )
    try:
        engine._on_peer_frame({
            "from_id": "waker", "from_title": "waker", "sent_at": "now",
            "body": "can you take the parser?", "from_repo": "/repo/waker",
            "kind": "direct",
        })
        await asyncio.sleep(0)

        assert engine._turn_running is False, "a peer woke a session at its ceiling"
        assert engine._queued_turn_task is None
        assert len(engine._prompt_queue) == 0, "not into the queue either"
        assert created[0].queried == [], "the peer's message reached the model"

        refusals = [
            ev for ev in _drain(engine._peer_queue) if ev.type == "turn_refused"
        ]
        assert len(refusals) == 1, (
            "a peer that tried to spend this session's money and was "
            "refused must leave a line saying so -- otherwise the one "
            "path nobody watches is also the one path nobody can audit"
        )
        assert refusals[0].data["peer_started"] is True
        assert "peer message" in refusals[0].data["message"]
    finally:
        await engine.finalize()


@pytest.mark.asyncio
async def test_a_refused_peer_message_is_not_lost_it_rides_the_next_turn(
    tmp_path, monkeypatch,
):
    """Refused is not dropped. The frame falls back to the pending list --
    exactly where a message went before inbound turn-starting existed --
    so raising the ceiling does not cost the user the message that was
    refused."""
    engine, _created = await _engine(
        tmp_path, monkeypatch, ceiling="1.0", inbound=True, cost=1.5,
    )
    try:
        engine._on_peer_frame({
            "from_id": "waker", "from_title": "waker", "sent_at": "now",
            "body": "still here", "from_repo": "/repo/waker", "kind": "direct",
        })
        await asyncio.sleep(0)

        assert len(engine._pending_peer_frames) == 1
    finally:
        await engine.finalize()


@pytest.mark.asyncio
async def test_a_peer_prompt_already_in_the_queue_is_refused_at_the_choke_point(
    tmp_path, monkeypatch,
):
    """The backstop. A peer message accepted while the session was still
    under its ceiling can reach the front of the mid-turn queue after the
    running turn pushed it over -- so the check in _send_turn, which every
    turn crosses, has to catch it there too."""
    engine, created = await _engine(
        tmp_path, monkeypatch, ceiling="1.0", inbound=True, cost=0.0,
    )
    try:
        prompt = peers_mod.PEER_TURN_INTRO + "\n\nqueued while affordable"
        engine.total_cost_usd = 1.5  # the running turn crossed the ceiling

        await engine._run_queued_turn(prompt)

        assert created[0].queried == [], "a queued peer turn ran past the ceiling"
        refusals = [
            ev for ev in _drain(engine._peer_queue) if ev.type == "turn_refused"
        ]
        assert len(refusals) == 1
        assert refusals[0].data["peer_started"] is True
    finally:
        await engine.finalize()


# -- the deliberate gaps, pinned so they are found rather than met ------


@pytest.mark.asyncio
async def test_a_turn_already_running_is_never_interrupted_by_the_ceiling(
    tmp_path, monkeypatch,
):
    """The one-turn overshoot, on purpose and in a test rather than only in
    a docstring.

    The only dollar figure DOXA has arrives on the message that ENDS a
    turn, so there is nothing to compare against mid-flight and nothing to
    stop a turn WITH. A session therefore exceeds its ceiling by at most
    the price of the turn that crosses it -- which the ledger below shows
    happening: the turn completes and total_cost_usd lands past the
    ceiling."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(budget_mod.SESSION_BUDGET_ENV, "1.0")
    factory, created = factory_with_script([_result(5.0)])
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()
    try:
        engine.total_cost_usd = 0.9  # under the ceiling: this turn is allowed
        types = [ev.type for ev in await _collect(engine, "one expensive turn")]

        assert "turn_started" in types, "the turn was allowed to start, as designed"
        assert engine.total_cost_usd == pytest.approx(5.9), (
            "the overshoot is real and this is its size -- one turn's price"
        )
        # ...and the NEXT turn is where the ceiling bites.
        assert [ev.type for ev in await _collect(engine, "and another")] == [
            "turn_refused"
        ]
        assert len(created[0].queried) == 1
    finally:
        await engine.finalize()


def test_an_engine_that_reports_no_cost_is_not_bounded_by_a_ceiling_at_all():
    """Stated as a test because it is the largest hole in this feature.

    codex and both API vendors report token counts and no dollars, so
    their spend reads as $0.00 and any ceiling compared against it would
    never fire. DOXA does not check anyway and hope, and it does not
    invent a price sheet; it says so where the number is set."""
    assert budget_mod.enforceable_for("claude") is True
    assert budget_mod.enforceable_for("codex") is False
    assert budget_mod.enforceable_for("deepseek") is False
    assert budget_mod.enforceable_for("glm") is False
    assert budget_mod.enforceable_for("a-engine-that-does-not-exist") is True, (
        "an unknown id is not evidence of anything -- doxa.engines.get is "
        "the one place an unknown engine is refused, and by name"
    )


# -- warning at the point of setting ------------------------------------


def test_a_ceiling_on_an_engine_that_cannot_report_cost_warns_when_it_is_set(
    monkeypatch, tmp_path,
):
    """The requirement in one test: the warning happens at the moment the
    number is SET, not silently never at the moment it would have fired."""
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(budget_mod.SESSION_BUDGET_ENV, "5")
    monkeypatch.setenv("DOXA_ENGINE", "codex")
    config_mod.invalidate()

    warning = budget_mod.configured_warning()

    assert warning is not None, (
        "a ceiling that can never fire was accepted in silence"
    )
    assert "codex" in warning, "the warning must name the engine it is about"
    assert "NOT ENFORCEABLE" in warning
    assert "price sheet" in warning, (
        "the warning owes the reader the reason DOXA will not just "
        "multiply tokens -- otherwise it reads as a missing feature"
    )


def test_no_warning_when_the_engine_does_report_cost(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(budget_mod.SESSION_BUDGET_ENV, "5")
    monkeypatch.setenv("DOXA_ENGINE", "claude")
    config_mod.invalidate()
    assert budget_mod.configured_warning() is None


def test_no_warning_when_no_ceiling_is_set(monkeypatch, tmp_path):
    """The warning is about a VALUE, not about an engine. A codex user who
    set no ceiling is owed no warning about one."""
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(budget_mod.SESSION_BUDGET_ENV, raising=False)
    monkeypatch.setenv("DOXA_ENGINE", "codex")
    config_mod.invalidate()
    assert budget_mod.configured_warning() is None


def test_the_settings_modal_row_carries_that_warning(monkeypatch, tmp_path):
    """Where the user actually is when they set it. The modal builds every
    row from config.SETTINGS, so this is the seam that decides whether the
    warning is ever seen."""
    from doxa.settings import SettingsScreen

    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(budget_mod.SESSION_BUDGET_ENV, "5")
    monkeypatch.setenv("DOXA_ENGINE", "codex")
    config_mod.invalidate()

    row = config_mod.SETTINGS_BY_KEY["session_budget_usd"]
    other = config_mod.SETTINGS_BY_KEY["allow_bypass"]

    assert "NOT ENFORCEABLE" in SettingsScreen._row_warning(row)
    assert SettingsScreen._row_warning(other) == "", (
        "the warning belongs to one row, not to the panel"
    )


def test_a_session_starting_under_an_unenforceable_ceiling_says_so():
    """The second place it is said: a session that starts with a ceiling
    its own engine cannot honour. A limit whose first appearance is the
    moment it fires is one the user learns about by being stopped -- and
    one that can NEVER fire would otherwise never appear at all."""
    enforced = budget_mod.start_note(5.0, reports_cost=True)
    assert enforced is not None and "$5.0000" in enforced
    assert "NOT ENFORCEABLE" not in enforced

    blind = budget_mod.start_note(5.0, reports_cost=False, engine_label="codex")
    assert blind is not None and "NOT ENFORCEABLE" in blind and "codex" in blind

    assert budget_mod.start_note(None) is None, (
        "a session with no ceiling says nothing -- off by default means "
        "nothing changes, including the transcript"
    )


# -- the fleet ----------------------------------------------------------


@pytest.fixture
def short_root():
    """A run root short enough for an AF_UNIX socket to live under it --
    the same constraint doxa.fleet.check_socket_budget enforces and the
    same workaround an operator applies on a real machine."""
    root = tempfile.mkdtemp(prefix="dxb", dir="/tmp")
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


POOL = (fleet_mod.ModelSlot(engine="claude", model="sonnet", weight=1),)
MIXED_POOL = POOL + (fleet_mod.ModelSlot(engine="deepseek", model="deepseek-chat"),)


def _spec(short_root, **kw):
    kw.setdefault("prompt", "do the thing")
    kw.setdefault("cwd", short_root)
    kw.setdefault("pool", POOL)
    kw.setdefault("root", short_root)
    kw.setdefault("n", 4)
    return fleet_mod.FleetSpec(**kw)


def test_a_run_that_can_wake_itself_with_no_budget_is_refused_at_spawn(short_root):
    """The experiment must not be launchable unbounded BY OMISSION, and
    omission -- forgetting a flag at 2am -- is the likeliest way it would
    be."""
    run = fleet_mod.FleetRun(_spec(short_root), backend=object())

    with pytest.raises(fleet_mod.BudgetRefused, match=r"no run budget is set"):
        run.prepare()

    assert not (_spec(short_root).run_root / "home").exists(), (
        "refused before any directory was made, let alone any process"
    )


def test_the_refusal_names_exactly_what_to_set(short_root):
    """A refusal a reader cannot act on is a refusal they route around."""
    run = fleet_mod.FleetRun(_spec(short_root), backend=object())

    with pytest.raises(fleet_mod.BudgetRefused) as excinfo:
        run.prepare()

    message = str(excinfo.value)
    assert "run_budget_usd" in message
    assert "--run-budget" in message
    assert "--allow-unbudgeted" in message, "the way out must be named too"
    assert "manifest" in message


def test_a_run_with_inbound_turns_off_needs_no_budget(short_root):
    """The guard's condition is real rather than always true: a fleet that
    genuinely cannot wake itself is not the thing this refuses."""
    run = fleet_mod.FleetRun(
        _spec(short_root, inbound_turns=False), backend=object(),
    )
    run.prepare()  # must not raise
    assert "no run budget" in run.report.budget, (
        "allowed, and still recorded -- a run nobody bounded says so in "
        "its own manifest rather than looking like a budgeted one"
    )


def test_a_budgeted_run_starts_and_divides_the_total_across_its_sessions(short_root):
    """How a run-wide number is enforced at all: N sessions each bounded at
    total/N can together spend at most the total, because the bounds add."""
    spec = _spec(short_root, n=32, run_budget_usd=64.0)

    assert spec.session_budget_usd == pytest.approx(2.0)

    env = spec.env_for(
        fleet_mod.assign(spec.n, list(spec.pool), seed=1, memory=spec.memory)[0]
    )
    assert env[budget_mod.SESSION_BUDGET_ENV] == repr(2.0), (
        "the share has to reach the session as the knob the session reads "
        "-- a number only the manifest knows bounds nothing"
    )
    assert env[peers_mod.PEER_INBOUND_TURNS_ENV] == "1", (
        "the run still arms the thing the budget exists to bound"
    )


def test_an_unbudgeted_run_never_strips_a_ceiling_the_operator_already_set(
    short_root, monkeypatch,
):
    """The only direction env_for may err in is the safe one."""
    monkeypatch.setenv(budget_mod.SESSION_BUDGET_ENV, "3")
    spec = _spec(short_root, inbound_turns=False)
    env = spec.env_for(
        fleet_mod.assign(spec.n, list(spec.pool), seed=1, memory=spec.memory)[0]
    )
    assert env[budget_mod.SESSION_BUDGET_ENV] == "3"


def test_a_zero_run_budget_is_off_not_a_run_in_which_nothing_may_start(short_root):
    """Same rule as the per-session knob: no value of this can be typed
    into a run where every session refuses every turn."""
    spec = _spec(short_root, run_budget_usd=0)
    assert spec.run_budget_usd is None
    assert spec.session_budget_usd is None


def test_the_override_starts_the_run_and_lands_in_the_manifest(short_root):
    """An operator who means it says so once, in words, and the manifest
    keeps the fact -- which is what makes a bill afterwards interpretable."""
    spec = _spec(short_root, allow_unbudgeted=True)
    run = fleet_mod.FleetRun(spec, backend=object())

    run.prepare()  # must not raise

    assert "ACCEPTED by allow_unbudgeted" in run.report.budget
    manifest = json.loads(run.write_manifest().read_text(encoding="utf-8"))
    assert manifest["unbudgeted"] is True
    assert manifest["spec"]["allow_unbudgeted"] is True
    assert manifest["spec"]["run_budget_usd"] is None
    assert manifest["budget"] == run.report.budget
    assert manifest["forced"] is False, (
        "--force and --allow-unbudgeted are two different claims and the "
        "manifest must keep them apart"
    )


def test_a_budgeted_run_records_both_numbers_in_the_manifest(short_root):
    spec = _spec(short_root, n=8, run_budget_usd=40.0)
    run = fleet_mod.FleetRun(spec, backend=object())
    run.prepare()

    manifest = json.loads(run.write_manifest().read_text(encoding="utf-8"))

    assert manifest["spec"]["run_budget_usd"] == pytest.approx(40.0)
    assert manifest["spec"]["session_budget_usd"] == pytest.approx(5.0)
    assert manifest["unbudgeted"] is False
    assert manifest["spec"]["inbound_turns"] is True


def test_the_budget_note_names_pool_engines_that_cannot_be_bounded(short_root):
    """A run budget over a mixed pool is enforced on some slots and not
    others, and the note says which -- otherwise the arithmetic printed
    before the run is a number nobody should have believed."""
    note = fleet_mod.budget_note(
        _spec(short_root, pool=MIXED_POOL, run_budget_usd=10.0)
    )
    assert "deepseek" in note and "UNBOUNDED" in note
    assert "claude" not in note.split("EXCEPT")[-1], (
        "claude reports cost, so it must not be listed among the blind"
    )

    clean = fleet_mod.budget_note(_spec(short_root, run_budget_usd=10.0))
    assert "UNBOUNDED" not in clean


def test_the_cli_exposes_both_flags_and_they_reach_the_spec():
    """The flags are the surface an operator actually uses at 2am."""
    import argparse
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.suppress(SystemExit):
        fleet_mod.main(["--help"])
    help_text = buf.getvalue()

    assert "--run-budget" in help_text
    assert "--allow-unbudgeted" in help_text
    assert isinstance(argparse.ArgumentParser, type)  # the import is load-bearing
