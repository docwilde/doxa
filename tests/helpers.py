# SPDX-License-Identifier: AGPL-3.0-only
"""Shared test fixtures/helpers for the beliefs and proposals surfaces --
the status-bar picker (tests/test_beliefs_picker.py) and the write-refresh
wiring that reads it (tests/test_memory_chip_writes.py).

Split out of tests/test_beliefs_picker.py (until v0.69.0,
tests/test_beliefs_browser.py) so a module that only needs the fixtures --
not the picker's own several hundred tests -- can import them without
importing a test module as if it were a library. That used to work by
accident (pytest puts a test file's own directory on sys.path, so
``from test_beliefs_browser import ...`` resolved); it stopped working the
moment the module doing the exporting was renamed out from under the
import, which is exactly what v0.69.0's beliefs-browser removal did. A
plain, non-test module is the fix that survives the next rename too.
"""

from __future__ import annotations

import time

from doxa.app import DoxaApp

DAY = 86400.0


def _stamp(secs_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - secs_ago))


def _status_plain(app) -> str:
    """The status bar as a reader sees it -- markup resolved. A chip
    asserted against raw markup is a chip whose colour can hide it."""
    from textual.content import Content

    return Content.from_markup(str(app.query_one("#status-bar").renderable)).plain


def _belief(bid, claim, *, subject="user", created_days=120, idle_days=40,
            via="derived", confidence=0.9, evidence_count=3,
            outcome=None, outcome_days=2, outcomes=0, source="dream"):
    """One belief as ``SessionEngine.list_beliefs`` hands it over.

    ``outcomes`` is ALWAYS present -- 0 meaning "the ledger was read and is
    empty", which is the ~95% case on a real store and the reason "never
    tested" is a state rather than a large age. A record with no
    ``outcomes`` key at all is a DIFFERENT thing (something predating the
    column) and is built explicitly where it is tested."""
    belief = {
        "id": bid, "subject": subject, "claim": claim, "confidence": confidence,
        "created": _stamp(created_days * DAY),
        "updated": _stamp(idle_days * DAY),
        "last_referenced": _stamp(idle_days * DAY),
        "via": via, "evidence_count": evidence_count,
        "outcomes": outcomes,
    }
    if outcome:
        belief.update({
            "outcome_event": outcome,
            "outcome_at": _stamp(outcome_days * DAY),
            "outcome_source": source,
            f"outcome_{outcome}s": max(1, outcomes),
            "outcomes": max(1, outcomes),
        })
    return belief


async def _open(monkeypatch, tmp_path, fake):
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    app = DoxaApp(cwd=str(tmp_path))
    return app


_NEXT_ID = [1000]


def _many(n, group="project:doxa", **kw):
    """n beliefs in one scope, with ids that are unique ACROSS calls --
    the picker keys its group map by row id, so two batches sharing ids
    would silently merge into one group and count wrong."""
    start = _NEXT_ID[0]
    _NEXT_ID[0] += n
    return [_belief(i, f"claim number {i:03d}", subject=group, **kw)
            for i in range(start, start + n)]


async def _picker(pilot, app, *, beliefs):
    from doxa.app import ChipPicker

    pane = app.active_pane
    await pane.open_beliefs_picker()
    for _ in range(200):
        picker = app.query_one("#chip-picker", ChipPicker)
        if picker.is_open:
            await pilot.pause()
            return pane, picker
        await pilot.pause(0.02)
    raise AssertionError("the beliefs picker never opened")


def _proposals(n, kind="memory", scope="user", start=0, **kw):
    out = []
    for i in range(start, start + n):
        item = {"pid": f"20260824-{i:03d}", "kind": kind, "action": "add",
                "text": f"proposal number {i:03d}",
                "created": _stamp(3 * DAY)}
        if kind == "memory":
            item["scope"] = scope
            item["project"] = "doxa"
        item.update(kw)
        out.append(item)
    return out


async def _pending_picker(pilot, app):
    from doxa.app import ChipPicker

    pane = app.active_pane
    await pane.open_pending_picker()
    for _ in range(200):
        picker = app.query_one("#chip-picker", ChipPicker)
        if picker.is_open:
            await pilot.pause()
            return pane, picker
        await pilot.pause(0.02)
    raise AssertionError("the proposals picker never opened")
