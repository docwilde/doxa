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

import re
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


# -- chip-identity anchors -------------------------------------------------
#
# The folder chip (GitLine.folder_label, wired in
# PaneChipsMixin._status_chips) paints `dir <cwd name>` whenever a session
# is NOT inside a git repository -- and under pytest, `<cwd name>` IS the
# running test's own name (pytest names `tmp_path` after the test). A bare
# `needle in _status_plain(app)` or `_status_plain(app).index(needle)`
# check is therefore not anchored on any particular CHIP at all: it can
# match "peers"/"ctx"/"proposal"/etc. sitting right there inside the
# directory-name chip's own text, purely because that happens to be a
# substring of the test's own name -- nothing to do with whether the real
# peers/ctx/proposals chip is actually showing. The two helpers below
# anchor on the CHIP instead, the same way the bar resolves a real click or
# hover to a chip in production (StatusBar._tooltip_for_x).
_CLICK_ACTION_RE = re.compile(r"\[@click=([A-Za-z0-9_]+)\]")


def _chip_actions(app) -> "set[str]":
    """The set of click actions the status bar currently paints -- e.g.
    ``open_peers_picker``, ``compact_now``, ``open_pending_picker``. A
    chip's presence/absence is what a "chip shown/hidden" test means to
    assert; checking for the chip's OWN action name is immune to the
    folder chip's text (the directory name) ever containing the same
    word by coincidence, because the folder chip's own action is always
    ``open_repo_picker``, never any other chip's."""
    raw = str(app.query_one("#status-bar").renderable)
    return set(_CLICK_ACTION_RE.findall(raw))


def _chip_offset(app, text: str) -> "tuple[int, int]":
    """A click/hover x-offset landing on `text` INSIDE the first real chip
    that contains it -- found by walking the bar's own ``_chip_hints``
    (one ``(plain_text, tooltip)`` per painted chip, in PAINT ORDER,
    exactly what ``StatusBar._tooltip_for_x`` walks in production) and
    advancing a cursor past each chip's own span before moving on to the
    next. This is what makes it safe against the folder chip: `text` is
    searched for one WHOLE CHIP at a time, in order, so an occurrence
    sitting inside an EARLIER chip (the folder chip's directory-name text,
    under pytest the test's own name) is examined and rejected on its own
    turn -- unless `text` itself is a piece of THAT chip, which is on the
    caller to avoid by choosing a needle no other chip's text can contain
    (e.g. the ctx chip's actual `"ctx 42%"`, never the bare, ambiguous
    `"ctx"`). ``#status-bar``'s own `padding: 0 2` (theme.tcss) is why the
    returned x adds 2, matching the convention every click-offset helper
    in this suite already follows."""
    from doxa.ui.statusline import StatusBar

    bar = app.query_one("#status-bar", StatusBar)
    plain = _status_plain(app)
    cursor = 0
    for chip_text, _hint in bar._chip_hints:
        idx = plain.find(chip_text, cursor)
        if idx == -1:
            continue
        within = chip_text.find(text)
        if within != -1:
            return (2 + idx + within, 0)
        cursor = idx + len(chip_text)
    raise AssertionError(
        f"no chip on the status bar contains {text!r}: bar reads {plain!r}"
    )


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
