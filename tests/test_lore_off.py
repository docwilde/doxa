# SPDX-License-Identifier: AGPL-3.0-only
"""Memory off, per SESSION -- ``DOXA_LORE`` / ``doxa.daemon --no-lore``.

WHY THIS EXISTS, because it is measurement rather than hygiene. A LORE
store shared by every session is a communication channel that appears in no
ledger. Thirty-two agents reading and writing one belief store can
coordinate through memory instead of through messages, and the structure
docs/plans/emergent-organization.md measures IS the communication
structure -- so a hierarchy negotiated through shared memory would be
reported as emergence with nothing to show for it. Per-session control
turns that confound into a variable a run can manipulate: doxa.fleet runs
memory-on and memory-off agents side by side and asks whether shared memory
substitutes for messaging.

The strictness bar the owner set, and what each part is tested by below:

* no snapshot injected at start or on refresh --
  ``test_no_snapshot_reaches_the_system_prompt_when_memory_is_off``,
  ``test_the_per_turn_hook_injects_nothing_when_memory_is_off``
* the LORE operators are **not offered to the model at all**, genuinely
  absent from the projection the way ``peer_send`` is when unarmed --
  ``test_the_lore_tools_are_absent_from_the_projection_not_present_and_refusing``
* no writes of any kind --
  ``test_a_memory_off_session_runs_no_review``, ``..._schedules_no_derive``,
  ``..._indexes_nothing_on_finalize``, ``..._refuses_every_write_action``
* everything else still works --
  ``test_memory_off_does_not_disable_the_peer_tools``,
  ``test_a_memory_off_session_still_writes_its_own_transcript``

Every absence assertion has a companion that flips ONE input and proves the
same call site produces the thing -- the "verified against a deliberately
permissive build" rule tests/test_remote_policy.py states and
tests/test_shell.py's own vacuity note predates. An absence test with no
companion would pass identically if the projection were empty for some
unrelated reason, or if the tool had never existed at all.
"""

from __future__ import annotations

import pytest

from doxa import config as config_mod
from doxa import operators as operators_mod
from doxa.engine import SessionEngine
from tests.fakes import factory_with_script


# The two seams a LORE-configured engine names in the projection ctx
# (doxa.engine.SessionEngine._build_options). Restated here rather than
# imported so that a change to either side is a test failure and not a
# silently agreeing pair.
LORE_SEAMS = {"belief_store": object(), "lore_root": "/nowhere"}

#: Every operator whose whole job is LORE. Named explicitly rather than
#: derived with ``startswith("lore_")``, because a derived list would
#: shrink silently if an operator were renamed and the test would then
#: assert nothing about it.
LORE_TOOLS = {
    "lore_belief_search",
    "lore_belief_show",
    "lore_belief_neighbours",
    "lore_memory_list",
    "lore_session_search",
    "lore_remember",
}


def _projection(ctx: dict) -> "set[str]":
    """The tool names the model would actually be handed for this ctx."""
    return {
        tool.name
        for tool in operators_mod.to_sdk_tools(
            lambda name, args: {}, include_write=True, ctx=ctx
        )
    }


def _engine(tmp_path, *, lore: "bool | None" = None) -> SessionEngine:
    factory, _created = factory_with_script([])
    return SessionEngine(cwd=str(tmp_path), client_factory=factory, lore=lore)


# =======================================================================
# The tools are ABSENT, not refusing
# =======================================================================


def test_the_lore_tools_are_absent_from_the_projection_not_present_and_refusing():
    """The owner's exact bar: "not offered and refusing, genuinely absent
    from the projection, the way `peer_send` is absent when unarmed."

    A refusing tool still costs context, still tells the model the store
    exists, and still invites a retry. Absence tells it nothing."""
    absent = _projection({"peer_send": object()})
    assert LORE_TOOLS & absent == set(), f"still offered: {sorted(LORE_TOOLS & absent)}"


def test_the_same_projection_offers_every_lore_tool_when_the_seams_are_named():
    """The companion that makes the assertion above non-vacuous.

    Without this, the test above would pass identically if ``to_sdk_tools``
    returned nothing at all, or if the six operators had been deleted --
    tests/test_shell.py's own "this test would pass vacuously" lesson,
    applied to an absence rather than a refusal."""
    present = _projection({**LORE_SEAMS, "peer_send": object()})
    assert LORE_TOOLS <= present, f"missing: {sorted(LORE_TOOLS - present)}"


def test_every_lore_tool_is_gated_on_a_seam_none_rides_along_ungated():
    """The failure this catches, and it is the one that was actually there:
    two of the six belief readers (``lore_belief_show``,
    ``lore_belief_neighbours``) had NO ``is_configured`` predicate at all,
    so "memory off" would have meant "four of the six are gone" -- a
    partial absence the model discovers by calling one of the two that
    remained."""
    registry = {**operators_mod.OPERATORS, **operators_mod.WRITE_OPERATORS}
    for name in sorted(LORE_TOOLS):
        op = registry[name]
        assert op.is_configured({}) is False, (
            f"{name} is offered to a session with no LORE seam wired -- it "
            "has no is_configured predicate, or the predicate ignores the ctx"
        )
        assert op.is_configured({**LORE_SEAMS}) is True, name


def test_a_memory_off_engine_names_no_lore_seam(tmp_path, monkeypatch):
    """The projection is only as absent as the ctx the engine builds. This
    is the seam where "memory off" would quietly become a no-op."""
    seen: "list[dict]" = []

    def spy(executor, allowed=None, include_write=False, ctx=None, extra=()):
        seen.append(dict(ctx or {}))
        return []

    monkeypatch.setattr(operators_mod, "to_sdk_tools", spy)
    _engine(tmp_path, lore=False)._build_options()
    assert seen, "the engine never built a projection"
    assert "belief_store" not in seen[0]
    assert "lore_root" not in seen[0]

    seen.clear()
    _engine(tmp_path, lore=True)._build_options()
    assert "belief_store" in seen[0] and "lore_root" in seen[0]


# =======================================================================
# Nothing is injected
# =======================================================================


def test_no_snapshot_reaches_the_system_prompt_when_memory_is_off(tmp_path, monkeypatch):
    """The failure this catches: a snapshot built and then discarded, which
    still READ the shared store -- on a fleet run, precisely the access the
    switch exists to prevent."""
    import doxa.engine as engine_mod

    calls: "list[str]" = []
    monkeypatch.setattr(
        engine_mod.lore_context,
        "build_context",
        lambda cwd: calls.append(cwd) or "REMEMBERED THINGS",
    )

    off = _engine(tmp_path, lore=False)._build_options()
    assert calls == [], "the store was read even with memory off"
    assert "LORE SNAPSHOT" not in (off.system_prompt or {}).get("append", "")

    on = _engine(tmp_path, lore=True)._build_options()
    assert calls, "the companion proves the snapshot path is reachable"
    assert "LORE SNAPSHOT" in (on.system_prompt or {}).get("append", "")
    assert "REMEMBERED THINGS" in (on.system_prompt or {}).get("append", "")


async def test_the_per_turn_hook_injects_nothing_when_memory_is_off(
    tmp_path, monkeypatch
):
    """UserPromptSubmit is the ONE per-turn injection point, and three
    producers ride it (the throttled refresh, the act-time consult, the
    graph block). Gating them individually is how one of the three gets
    forgotten, so the hook is gated as a whole."""
    import doxa.engine as engine_mod

    monkeypatch.setattr(
        engine_mod.lore_context, "build_context", lambda cwd: "REMEMBERED THINGS"
    )
    monkeypatch.setattr(engine_mod.lore_context, "refresh_interval", lambda: 0.0)

    off = await _engine(tmp_path, lore=False)._on_user_prompt_submit({}, None, None)
    assert off == {}

    on = await _engine(tmp_path, lore=True)._on_user_prompt_submit({}, None, None)
    text = str(on.get("hookSpecificOutput", {}).get("additionalContext", ""))
    assert "REMEMBERED THINGS" in text, (
        "the companion proves the injection path is reachable -- without it "
        "the assertion above would pass on a hook that never injected"
    )


# =======================================================================
# Nothing is written
# =======================================================================


def test_a_memory_off_session_runs_no_review(tmp_path, monkeypatch):
    """The deriver is what stages proposals into the shared store. It is
    also the most expensive write, so a silent one is a bill as well as a
    confound."""
    import doxa.engine as engine_mod

    built: "list[object]" = []
    monkeypatch.setattr(
        engine_mod.lore_deriver,
        "build_review_job",
        lambda *a, **k: built.append(a) or None,
    )
    monkeypatch.setattr(engine_mod, "stage_disabled", lambda stage: False)

    _engine(tmp_path, lore=False)._run_review_sync(False)
    assert built == []

    _engine(tmp_path, lore=True)._run_review_sync(False)
    assert built, "the companion proves the review path is reachable"


def test_a_memory_off_session_schedules_no_derive(tmp_path, monkeypatch):
    import doxa.engine as engine_mod

    monkeypatch.setattr(engine_mod, "derive_interval", lambda: 0.0)

    off = _engine(tmp_path, lore=False)
    off._maybe_schedule_derive()
    assert off._derive_task is None

    on = _engine(tmp_path, lore=True)
    on._maybe_schedule_derive()
    assert on._derive_task is not None, (
        "the companion proves the scheduler is reachable"
    )
    on._derive_task.cancel()


async def test_a_memory_off_session_indexes_nothing_on_finalize(tmp_path, monkeypatch):
    """Indexing is the write that makes THIS session's conversation
    searchable by every OTHER session -- the invisible channel in its
    purest form."""
    import doxa.engine as engine_mod

    indexed: "list[object]" = []
    monkeypatch.setattr(engine_mod.lore_store, "db_connect", lambda *a, **k: object())
    monkeypatch.setattr(
        engine_mod.lore_store,
        "index_live",
        lambda conn, path: indexed.append(path) or (1, 1),
    )

    off = _engine(tmp_path, lore=False)
    await off.finalize()
    assert indexed == []

    on = _engine(tmp_path, lore=True)
    await on.finalize()
    assert indexed, "the companion proves the indexing path is reachable"


async def test_a_memory_off_session_refuses_every_write_action(tmp_path):
    """Defence in depth behind the absent tools. These four are driven by a
    human in the TUI rather than by the model, so the projection gate never
    sees them -- and a picker that wrote into the shared store from a
    memory-off session would reopen the channel by hand."""
    engine = _engine(tmp_path, lore=False)

    assert await engine.approve_pending("p1")
    assert await engine.reject_pending("p1")
    assert await engine.record_belief_outcome(1, "confirmed")
    assert await engine.retract_belief(1)
    assert await engine.list_beliefs() == []
    assert await engine.list_pending() == []
    assert await engine.belief_evidence(1) == []
    assert engine.belief_count() == 0


# =======================================================================
# Per SESSION, not per process
# =======================================================================


def test_two_sessions_in_one_process_can_disagree_about_memory(tmp_path):
    """THE requirement. A process-wide environment variable cannot express
    this, and a fleet run needs it: memory-off and memory-on agents in the
    same run on the same machine at the same moment."""
    off = _engine(tmp_path, lore=False)
    on = _engine(tmp_path, lore=True)
    assert off.lore is False and on.lore is True


def test_memory_is_on_when_nobody_says_otherwise(tmp_path, monkeypatch):
    """The default must be today's behaviour. This is the one switch in
    doxa.engine that REMOVES a capability, so its default is the opposite
    of every other one's."""
    monkeypatch.delenv("DOXA_LORE", raising=False)
    monkeypatch.setattr(config_mod, "raw", lambda env: "")
    assert _engine(tmp_path).lore is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF"])
def test_the_config_row_can_turn_memory_off_for_a_machine(tmp_path, monkeypatch, value):
    """The row is the DEFAULT layer; the constructor argument outranks it,
    which is what lets one fleet run disagree with the machine's setting."""
    monkeypatch.setenv("DOXA_LORE", value)
    assert _engine(tmp_path).lore is False
    assert _engine(tmp_path, lore=True).lore is True, (
        "an explicit argument must outrank the config row, or a fleet could "
        "not run a memory-on agent on a machine configured memory-off"
    )


def test_the_memory_row_is_a_real_setting_and_defaults_on():
    row = next(s for s in config_mod.SETTINGS if s.key == "lore")
    assert row.env == "DOXA_LORE"
    assert row.default == "1"
    assert row.category == "Memory"


def test_the_daemon_flag_is_absent_from_an_ordinary_sessions_argv(monkeypatch, tmp_path):
    """The same discipline every other spawn flag follows: a session that
    does not use a capability produces a byte-identical command line to the
    one this function built before the capability existed."""
    import subprocess

    from doxa import daemon as daemon_mod

    seen: "list[list[str]]" = []

    class _Proc:
        returncode = None

        def poll(self):
            return None

    def fake_popen(cmd, **kw):
        seen.append(list(cmd))
        return _Proc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    env = {**__import__("os").environ, "DOXA_RUNTIME_DIR": str(tmp_path / "rt")}

    for lore in (True, False):
        with pytest.raises(RuntimeError, match=r"did not become ready"):
            daemon_mod.spawn_daemon(
                cwd=str(tmp_path), wait_secs=0.05, env=env, lore=lore
            )

    assert "--no-lore" not in seen[0], "an ordinary session's argv grew a flag"
    assert "--no-lore" in seen[1]


def test_the_daemon_hands_its_memory_answer_to_the_engine_it_builds(monkeypatch):
    """The flag has to survive the last hop: argv -> SessionDaemon ->
    engine factory -> SessionEngine. This is the hop with no other test
    over it."""
    from doxa import daemon as daemon_mod

    seen: "list[dict]" = []
    monkeypatch.setattr(
        daemon_mod, "SessionEngine", lambda **kw: seen.append(kw) or object()
    )

    daemon_mod.SessionDaemon(cwd=".", lore=False)._engine_factory(".", "sid", "sock")
    assert seen[0]["lore"] is False

    daemon_mod.SessionDaemon(cwd=".", lore=None)._engine_factory(".", "sid", "sock")
    assert seen[1]["lore"] is None, (
        "None must reach the engine as None -- the engine, not the daemon, "
        "is what consults the config row, and a daemon that resolved it "
        "here would freeze the answer at spawn time"
    )


# =======================================================================
# Turning memory off must not break anything else
# =======================================================================


def test_memory_off_does_not_disable_the_peer_tools():
    """"A session with LORE off must still work completely otherwise."
    The peer surface is the one that matters most here -- it is what the
    experiment measures, and it merely happens to import the same package."""
    present = _projection({"peer_send": object()})
    assert {"peer_list", "peer_history", "peer_send"} <= present


async def test_a_memory_off_session_still_writes_its_own_transcript(tmp_path):
    """The line drawn deliberately: the transcript is DOXA's OWN session
    record -- /resume, /search's local half and the transcript pane read
    it -- and it is written under a path lore_core derives. Turning memory
    off stops DOXA putting anything INTO the store; it does not stop the
    session recording itself."""
    engine = _engine(tmp_path, lore=False)
    assert engine.transcript_path
    engine._persist_user_text("hello from a session with no memory")
    assert engine.transcript_path.exists()
    assert "no memory" in engine.transcript_path.read_text(encoding="utf-8")


def test_memory_off_does_not_mean_lore_core_can_be_absent(tmp_path):
    """The honest finding, written as a test so it cannot be forgotten.

    Twenty-seven modules under doxa/ import ``lore_core``, and nine of them
    import it at module level -- ``scrub_secrets`` runs on every received
    peer frame, every persisted transcript line and every ledger body, and
    ``project_slug``/``PROJECTS_DIR`` derive the transcript path itself.
    "Memory off" therefore means DOXA's memory BEHAVIOURS are off. It does
    not, and cannot today, mean the package is uninstalled."""
    import doxa.peers as peers_mod
    import doxa.transcript as transcript_mod

    engine = _engine(tmp_path, lore=False)
    assert engine.slug, "project_slug still derives this session's identity"
    assert transcript_mod.transcript_path(engine.session_id, str(tmp_path))
    assert peers_mod.scrub_secrets("plain text") == "plain text"
