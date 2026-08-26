# SPDX-License-Identifier: AGPL-3.0-only
"""A curated WRITE -- a proposal approved or rejected via the status bar's
own quick pickers, a belief outcome recorded, a belief retracted -- must
leave the status bar's chips correct afterwards, not just the store on
disk. Reported alongside the memory chip's startup/switch/resume defect
(tests/test_memory_chip_slug.py): none of the three write paths in
``doxa/session/chips.py`` triggered a status refresh at all, so a chip
stayed exactly as stale as whatever it last happened to read.

Scoped to ``doxa/session/chips.py``'s own write functions
(``_resolve_pending``, ``_record_belief_outcome``, ``_retract_belief``)
deliberately: those are reached from BOTH the picker's per-row action
sub-menu (tests/test_beliefs_browser.py) and v0.67.0's inline row actions
(tests/test_picker_row_actions.py) -- one write function, one refresh,
covering both surfaces without a test per entry point. The full beliefs
browser tab (``doxa/ui/beliefs.py``) is a separate surface with its own
call sites and is out of scope here.

Which chip moves depends on the action AND the proposal's kind, and
getting that wrong is its own bug (a chip moving for the wrong reason
reads as correct and is not):

* approving a MEMORY proposal writes MEMORY.md/USER.md (the memory-fill
  chip) and always drains the staging queue by one (the staged-proposals
  chip).
* rejecting a proposal (either kind) drains the queue and writes nothing
  else -- only the staged-proposals chip should move.
* approving a BELIEF proposal lands a new active belief (the belief
  chip) and drains the queue (the staged-proposals chip) -- never memory
  fill, which that proposal kind never touches.
* retracting a belief removes it from the active set (the belief chip)
  only -- there is no staging queue involved at all.

Each test below fakes ONLY the chip-reading helpers (``memory_fill``,
``staged_count``) with a small before/after switch keyed to whether the
write already landed, and lets the real engine ledger
(``fake.approved``/``fake.rejected``/``fake.retracted``) prove the write
itself still happens exactly once, on the CONFIRMING selection, never the
arming one. The chip assertion is therefore a direct read of
``_refresh_status``'s own effect: if the action's completion is what
triggers the reread, the chip changes IMMEDIATELY after the confirming
selection and never before it."""

from __future__ import annotations

import pytest

from test_beliefs_browser import (  # noqa: F401 -- reused fixtures/helpers
    _belief,
    _many,
    _open,
    _picker,
    _pending_picker,
    _proposals,
    _status_plain,
)
from tests.fakes import FakeEngine


async def _select(picker, rid: str) -> None:
    picker.select_row(next(i for i, (r, _l) in enumerate(picker._rows) if r == rid))


async def _wait(pilot, predicate, tries: int = 200) -> None:
    for _ in range(tries):
        if predicate():
            return
        await pilot.pause(0.02)
    assert predicate(), "condition never became true"


def _mem_span(app) -> str:
    text = _status_plain(app)
    if "mem u" not in text:
        return ""
    return text.split("mem u", 1)[1].split("  ", 1)[0]


async def _wait_status(pilot, app, needle: str, tries: int = 200) -> bool:
    for _ in range(tries):
        if needle in _status_plain(app):
            return True
        await pilot.pause(0.02)
    return needle in _status_plain(app)


@pytest.mark.asyncio
async def test_approving_a_memory_proposal_moves_memory_and_staged_chips(
    monkeypatch, tmp_path,
):
    """Asserts the MEMORY-FILL chip (gains its project half) and the
    STAGED-PROPOSALS chip (drops to zero and hides, per the hide-at-zero
    convention) -- both stores a memory-kind approval actually touches."""
    from doxa.session import chips as chips_mod

    written = {"done": False}

    def fake_memory_fill(scope, project=None):
        if scope == "user":
            return (100, 4500)
        return (200, 8800) if written["done"] else None

    def fake_staged_count(slug):
        return 0 if written["done"] else 3

    monkeypatch.setattr(chips_mod, "memory_fill", fake_memory_fill)
    monkeypatch.setattr(chips_mod, "staged_count", fake_staged_count)

    fake = FakeEngine([])
    fake.list_pending_result = _proposals(1, kind="memory", scope="project")
    pid = fake.list_pending_result[0]["pid"]

    async def approve(p):
        fake.approved.append(p)
        written["done"] = True
        return None

    fake.approve_pending = approve

    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        assert await _wait_status(pilot, app, "3 proposals")
        assert "mem u" in _status_plain(app) and " p" not in _mem_span(app)

        _pane, picker = await _pending_picker(pilot, app)
        rid = next(rid for rid, _l in picker._rows if rid.startswith("pending:"))
        await _select(picker, rid)
        for _ in range(150):
            if any(r == "act:approve" for r, _l in picker._rows):
                break
            await pilot.pause(0.02)
        await _select(picker, "act:approve")
        for _ in range(150):
            if any(r == "act:approve!" for r, _l in picker._rows):
                break
            await pilot.pause(0.02)
        assert fake.approved == [], "the first selection only arms"

        await _select(picker, "act:approve!")
        await _wait(pilot, lambda: fake.approved == [pid])

        await _wait(pilot, lambda: "3 proposals" not in _status_plain(app))
        assert "proposal" not in _status_plain(app), _status_plain(app)
        await _wait(pilot, lambda: " p" in _mem_span(app))
        assert "mem u" in _status_plain(app) and " p" in _mem_span(app)


@pytest.mark.asyncio
async def test_rejecting_a_proposal_moves_only_the_staged_chip(
    monkeypatch, tmp_path,
):
    """Asserts the STAGED-PROPOSALS chip drops, and the MEMORY-FILL chip's
    text is byte-for-byte unchanged -- a reject writes nothing, so nothing
    about the memory store should read differently before and after."""
    from doxa.session import chips as chips_mod

    rejected_flag = {"done": False}
    monkeypatch.setattr(
        chips_mod, "memory_fill",
        lambda scope, project=None: (
            (100, 4500) if scope == "user" else (200, 8800)
        ),
    )
    monkeypatch.setattr(
        chips_mod, "staged_count",
        lambda slug: 0 if rejected_flag["done"] else 3,
    )

    fake = FakeEngine([])
    fake.list_pending_result = _proposals(1, kind="memory", scope="project")
    pid = fake.list_pending_result[0]["pid"]

    async def reject(p):
        fake.rejected.append(p)
        rejected_flag["done"] = True
        return None

    fake.reject_pending = reject

    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        assert await _wait_status(pilot, app, "3 proposals")
        mem_before = _mem_span(app)
        assert " p" in mem_before  # both halves present from the start

        _pane, picker = await _pending_picker(pilot, app)
        rid = next(rid for rid, _l in picker._rows if rid.startswith("pending:"))
        await _select(picker, rid)
        for _ in range(150):
            if any(r == "act:reject" for r, _l in picker._rows):
                break
            await pilot.pause(0.02)
        await _select(picker, "act:reject")
        await _wait(pilot, lambda: fake.rejected == [pid])

        await _wait(pilot, lambda: "3 proposals" not in _status_plain(app))
        assert "proposal" not in _status_plain(app)
        assert _mem_span(app) == mem_before, (
            "the memory chip changed after a reject, which writes nothing"
        )


@pytest.mark.asyncio
async def test_approving_a_belief_proposal_moves_belief_and_staged_chips(
    monkeypatch, tmp_path,
):
    """Asserts the BELIEF-COUNT chip (a belief proposal's approval lands a
    new active belief) and the STAGED-PROPOSALS chip -- never the
    memory-fill chip, which a belief-kind proposal never writes."""
    from doxa.session import chips as chips_mod

    written = {"done": False}
    monkeypatch.setattr(
        chips_mod, "memory_fill",
        lambda scope, project=None: (
            (100, 4500) if scope == "user" else (200, 8800)
        ),
    )
    monkeypatch.setattr(
        chips_mod, "staged_count", lambda slug: 0 if written["done"] else 3,
    )

    fake = FakeEngine([])
    fake.list_pending_result = _proposals(1, kind="belief")
    pid = fake.list_pending_result[0]["pid"]
    fake.belief_count = lambda: 4 if written["done"] else 3

    async def approve(p):
        fake.approved.append(p)
        written["done"] = True
        return None

    fake.approve_pending = approve

    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        assert await _wait_status(pilot, app, "3 proposals")
        assert "3 beliefs" in _status_plain(app)
        mem_before = _mem_span(app)

        _pane, picker = await _pending_picker(pilot, app)
        rid = next(rid for rid, _l in picker._rows if rid.startswith("pending:"))
        await _select(picker, rid)
        for _ in range(150):
            if any(r == "act:approve" for r, _l in picker._rows):
                break
            await pilot.pause(0.02)
        await _select(picker, "act:approve")
        for _ in range(150):
            if any(r == "act:approve!" for r, _l in picker._rows):
                break
            await pilot.pause(0.02)
        await _select(picker, "act:approve!")
        await _wait(pilot, lambda: fake.approved == [pid])

        await _wait(pilot, lambda: "4 beliefs" in _status_plain(app))
        assert "proposal" not in _status_plain(app)
        assert _mem_span(app) == mem_before, (
            "a belief proposal's approval must never move the memory chip"
        )


@pytest.mark.asyncio
async def test_retracting_a_belief_moves_only_the_belief_chip(monkeypatch, tmp_path):
    """Asserts the BELIEF-COUNT chip drops by one and nothing about the
    memory chip (retracting writes no MEMORY.md) or the staged chip (a
    belief row is not a staging-queue entry at all) moves."""
    from doxa.session import chips as chips_mod

    retracted_flag = {"done": False}
    monkeypatch.setattr(
        chips_mod, "memory_fill",
        lambda scope, project=None: (
            (100, 4500) if scope == "user" else (200, 8800)
        ),
    )
    monkeypatch.setattr(chips_mod, "staged_count", lambda slug: None)

    fake = FakeEngine([])
    fake.list_beliefs_result = _many(1)
    fake.belief_count = lambda: 2 if retracted_flag["done"] else 3

    async def retract(bid, reason="retracted"):
        fake.retracted.append(bid)
        retracted_flag["done"] = True
        return None

    fake.retract_belief = retract

    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        assert await _wait_status(pilot, app, "3 beliefs")
        mem_before = _mem_span(app)

        _pane, picker = await _picker(pilot, app, beliefs=None)
        rid = next(rid for rid, _l in picker._rows if rid.startswith("belief:"))
        bid = int(rid.split(":", 1)[1])
        await _select(picker, rid)
        await pilot.pause()
        await _select(picker, "act:retract")
        for _ in range(150):
            if any(r == "act:retract!" for r, _l in picker._rows):
                break
            await pilot.pause(0.02)
        assert fake.retracted == [], "the first selection only arms"
        await _select(picker, "act:retract!")
        await _wait(pilot, lambda: fake.retracted == [bid])

        await _wait(pilot, lambda: "2 beliefs" in _status_plain(app))
        assert _mem_span(app) == mem_before
